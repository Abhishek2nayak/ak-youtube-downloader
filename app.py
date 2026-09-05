"""
AK YouTube Downloader
=====================
FastAPI + yt-dlp. No YouTube API, no third-party services, no API keys.

Local:       python app.py            -> http://127.0.0.1:8000
Production:  uvicorn app:app --host 0.0.0.0 --port $PORT
"""

from __future__ import annotations

import asyncio
import json
import os
import queue
import re
import shutil
import threading
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               PlainTextResponse, Response)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

import yt_dlp

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
SETTINGS_FILE = DATA_DIR / "settings.json"
HISTORY_FILE = DATA_DIR / "history.json"
MESSAGES_FILE = DATA_DIR / "messages.json"

SITE_NAME = os.environ.get("SITE_NAME", "AK YouTube Downloader")
SITE_URL = os.environ.get("SITE_URL", "http://127.0.0.1:8000").rstrip("/")
CONTACT_EMAIL = os.environ.get("CONTACT_EMAIL", "support@akyoutubedownloader.com")
# Delete finished files after this many minutes (0 = keep forever, good for local use)
RETENTION_MINUTES = int(os.environ.get("RETENTION_MINUTES", "0"))

# Path to a Netscape-format cookies.txt exported from a browser signed in to YouTube.
# This is what gets a server past "sign in to confirm you're not a bot".
COOKIES_FILE_SOURCE = os.environ.get("COOKIES_FILE", "")
# YouTube serves a different API to each client, and on a datacentre IP most of them now
# demand a PO token. Per the yt-dlp PO Token Guide the ones that work without a token are
# android_vr, web_embedded and tv (tv needs account cookies or every format comes back DRM'd),
# so those are tried first and the token-hungry ones last.
PLAYER_CLIENTS = [c.strip() for c in os.environ.get(
    "YTDLP_PLAYER_CLIENTS", "android_vr,tv,web_embedded,default").split(",") if c.strip()]

# Optional PO token provider (bgutil). Run the provider container, then set this to its URL,
# e.g. http://127.0.0.1:4416 . Without it, web/mweb/android/ios clients cannot stream.
POT_BASE_URL = os.environ.get("POT_BASE_URL", "").rstrip("/")
# Protects /api/diagnose. Leave unset to disable the endpoint entirely.
DEBUG_KEY = os.environ.get("DEBUG_KEY", "")

DEFAULT_SETTINGS = {
    "download_dir": os.environ.get("DOWNLOAD_DIR", str(BASE_DIR / "downloads")),
    "max_workers": int(os.environ.get("MAX_WORKERS", "3")),
    "rate_limit": "",
    "cookies_from_browser": "",
    "proxy": os.environ.get("PROXY", ""),
}


def load_json(path: Path, fallback):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return fallback


def save_json(path: Path, payload) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
    tmp.replace(path)


SETTINGS: Dict[str, Any] = {**DEFAULT_SETTINGS, **load_json(SETTINGS_FILE, {})}

# Environment variables win over anything saved in data/settings.json, so a proxy set
# in your host's dashboard always takes effect on the next restart.
for _key, _env in (("proxy", "PROXY"), ("download_dir", "DOWNLOAD_DIR")):
    if os.environ.get(_env):
        SETTINGS[_key] = os.environ[_env]
if os.environ.get("MAX_WORKERS"):
    SETTINGS["max_workers"] = int(os.environ["MAX_WORKERS"])
if os.environ.get("RATE_LIMIT"):
    SETTINGS["rate_limit"] = os.environ["RATE_LIMIT"]

Path(SETTINGS["download_dir"]).mkdir(parents=True, exist_ok=True)


def prepare_cookies(source: str) -> str:
    """
    yt-dlp writes the cookie jar back when it finishes, so the file it is given must be
    writable. Mounted secrets (Render Secret Files, Kubernetes secrets, read-only volumes)
    are not, so work on a private copy instead of the original.
    """
    if not source:
        return ""
    if not os.path.isfile(source):
        print(f"  [cookies] COOKIES_FILE is set but no file at {source}")
        return ""
    runtime = DATA_DIR / "cookies.runtime.txt"
    try:
        shutil.copyfile(source, runtime)
        os.chmod(runtime, 0o600)
        with open(runtime, "r", encoding="utf-8", errors="ignore") as fh:
            first = fh.readline()
        if "netscape" not in first.lower() and not first.startswith("#"):
            print("  [cookies] warning: file does not look like a Netscape cookies.txt")
        return str(runtime)
    except Exception as exc:                                        # noqa: BLE001
        print(f"  [cookies] could not copy {source}: {exc}")
        return source


COOKIES_FILE = prepare_cookies(COOKIES_FILE_SOURCE)


def locate_ffmpeg() -> tuple[Optional[str], str]:
    found = shutil.which("ffmpeg")
    if found:
        return os.path.dirname(found), "system"
    try:
        import imageio_ffmpeg
        src = imageio_ffmpeg.get_ffmpeg_exe()
        bin_dir = BASE_DIR / "bin"
        bin_dir.mkdir(exist_ok=True)
        dst = bin_dir / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
        if not dst.exists():
            try:
                os.symlink(src, dst)
            except Exception:
                shutil.copy2(src, dst)
            if os.name != "nt":
                os.chmod(dst, 0o755)
        return str(bin_dir), "bundled"
    except Exception:
        return None, "missing"


FFMPEG_DIR, FFMPEG_SOURCE = locate_ffmpeg()
FFMPEG_AVAILABLE = FFMPEG_DIR is not None


# --------------------------------------------------------------------------- #
# Jobs
# --------------------------------------------------------------------------- #

@dataclass
class Job:
    id: str
    url: str
    opts: Dict[str, Any]
    title: str = ""
    thumbnail: str = ""
    uploader: str = ""
    status: str = "queued"
    stage: str = ""
    progress: float = 0.0
    speed: float = 0.0
    eta: int = 0
    downloaded: int = 0
    total: int = 0
    filepath: str = ""
    filename: str = ""
    filesize: int = 0
    error: str = ""
    created_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    cancel: bool = False

    def public(self) -> Dict[str, Any]:
        d = {k: v for k, v in self.__dict__.items() if k not in ("opts", "cancel")}
        d["mode"] = self.opts.get("mode", "video")
        d["quality"] = self.opts.get("quality", "")
        return d


class Cancelled(Exception):
    pass


JOBS: Dict[str, Job] = {}
JOB_ORDER: List[str] = []
JOBS_LOCK = threading.Lock()
WORK_QUEUE: "queue.Queue[str]" = queue.Queue()
EVENT_QUEUE: "queue.Queue[Dict[str, Any]]" = queue.Queue()


class Hub:
    def __init__(self) -> None:
        self.clients: set[WebSocket] = set()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self.clients.add(ws)

    def disconnect(self, ws: WebSocket) -> None:
        self.clients.discard(ws)

    async def broadcast(self, message: Dict[str, Any]) -> None:
        for ws in list(self.clients):
            try:
                await ws.send_json(message)
            except Exception:
                self.disconnect(ws)


hub = Hub()


def emit(job: Job) -> None:
    EVENT_QUEUE.put({"type": "job", "job": job.public()})


# --------------------------------------------------------------------------- #
# yt-dlp
# --------------------------------------------------------------------------- #

def base_opts(client: Optional[str] = None) -> Dict[str, Any]:
    o: Dict[str, Any] = {"quiet": True, "no_warnings": True, "noplaylist": True}
    if SETTINGS.get("cookies_from_browser"):
        o["cookiesfrombrowser"] = (SETTINGS["cookies_from_browser"],)
    if COOKIES_FILE and os.path.isfile(COOKIES_FILE):
        o["cookiefile"] = COOKIES_FILE
    if SETTINGS.get("proxy"):
        o["proxy"] = SETTINGS["proxy"]
    if FFMPEG_DIR:
        o["ffmpeg_location"] = FFMPEG_DIR
    extractor_args: Dict[str, Any] = {}
    if client and client != "default":
        extractor_args["youtube"] = {"player_client": [client]}
    if POT_BASE_URL:
        extractor_args["youtubepot-bgutilhttp"] = {"base_url": [POT_BASE_URL]}
    if extractor_args:
        o["extractor_args"] = extractor_args
    return o


def is_blocked_error(exc: Exception) -> bool:
    """True when YouTube refused the server rather than the video being unavailable."""
    low = str(exc).lower()
    return any(t in low for t in (
        "sign in to confirm", "not a bot", "confirm you're not", "failed to extract",
        "player response", "unable to extract", "http error 403", "http error 429",
        "requested format is not available", "no video formats",
    ))


def extract(url: str, download: bool, opts_for: Any) -> Dict[str, Any]:
    """
    Run yt-dlp, retrying with different YouTube player clients when the first one is
    blocked. YouTube serves each client a different API surface, so a client that is
    refused on a datacentre IP often succeeds on the next one.

    `opts_for(client)` must return a fresh options dict for that client.
    """
    last: Optional[Exception] = None
    for client in PLAYER_CLIENTS:
        try:
            with yt_dlp.YoutubeDL(opts_for(client)) as ydl:
                return ydl.extract_info(url, download=download)
        except Cancelled:
            raise
        except Exception as exc:                                    # noqa: BLE001
            if not is_blocked_error(exc):
                raise
            last = exc
    raise last if last else RuntimeError("Extraction failed")


def format_selector(mode: str, quality: str) -> str:
    if mode == "audio":
        return "bestaudio/best"
    hf = "" if quality in ("best", "") else f"[height<={quality}]"
    return (f"bestvideo{hf}[ext=mp4]+bestaudio[ext=m4a]/"
            f"bestvideo{hf}+bestaudio/best{hf}/best")


def build_ydl_opts(job: Job, client: Optional[str] = None) -> Dict[str, Any]:
    o = job.opts
    out_dir = Path(SETTINGS["download_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)

    ydl = base_opts(client)
    ydl.update({
        "format": format_selector(o.get("mode", "video"), str(o.get("quality", "best"))),
        "outtmpl": {"default": str(out_dir / "%(title).120B [%(id)s].%(ext)s")},
        "noprogress": True,
        "retries": 5,
        "fragment_retries": 10,
        "concurrent_fragment_downloads": 4,
        "windowsfilenames": True,
        "continuedl": True,
        "progress_hooks": [make_progress_hook(job)],
        "postprocessor_hooks": [make_pp_hook(job)],
        "postprocessors": [],
    })
    if SETTINGS.get("rate_limit"):
        rate = parse_rate(SETTINGS["rate_limit"])
        if rate:
            ydl["ratelimit"] = rate

    pps: List[Dict[str, Any]] = []
    if o.get("mode") == "audio":
        pps.append({"key": "FFmpegExtractAudio",
                    "preferredcodec": o.get("audio_format", "mp3"),
                    "preferredquality": str(o.get("audio_quality", "192"))})
        ydl["writethumbnail"] = True
        pps.append({"key": "FFmpegThumbnailsConvertor", "format": "jpg"})
        pps.append({"key": "EmbedThumbnail", "already_have_thumbnail": False})
        pps.append({"key": "FFmpegMetadata", "add_metadata": True})
    else:
        ydl["merge_output_format"] = "mp4"
        pps.append({"key": "FFmpegMetadata", "add_metadata": True})
    ydl["postprocessors"] = pps
    return ydl


def parse_rate(text: str) -> Optional[int]:
    m = re.match(r"^\s*([\d.]+)\s*([KMG]?)B?/?s?\s*$", text, re.I)
    if not m:
        return None
    mult = {"": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3}[m.group(2).upper()]
    return int(float(m.group(1)) * mult)


def make_progress_hook(job: Job):
    last = [0.0]

    def hook(d: Dict[str, Any]) -> None:
        if job.cancel:
            raise Cancelled()
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            job.status = "downloading"
            job.stage = "Downloading"
            job.downloaded = done
            job.total = int(total)
            job.progress = round(done / total * 100, 1) if total else 0.0
            job.speed = d.get("speed") or 0.0
            job.eta = d.get("eta") or 0
            now = time.time()
            if now - last[0] > 0.35:
                last[0] = now
                emit(job)
        elif d["status"] == "finished":
            job.progress = 100.0
            job.status = "processing"
            job.stage = "Preparing file"
            emit(job)

    return hook


def make_pp_hook(job: Job):
    labels = {"FFmpegExtractAudio": "Converting to MP3", "EmbedThumbnail": "Adding cover art",
              "FFmpegMetadata": "Writing tags", "FFmpegMerger": "Merging video and audio",
              "MoveFiles": "Finishing"}

    def hook(d: Dict[str, Any]) -> None:
        if job.cancel:
            raise Cancelled()
        if d.get("status") == "started":
            job.status = "processing"
            job.stage = labels.get(d.get("postprocessor", ""), "Processing")
            emit(job)

    return hook


def resolve_output(info: Dict[str, Any]) -> str:
    rd = info.get("requested_downloads") or []
    if rd and rd[0].get("filepath"):
        return rd[0]["filepath"]
    return info.get("filepath") or info.get("_filename") or ""


def run_job(job: Job) -> None:
    try:
        job.status = "downloading"
        job.stage = "Starting"
        emit(job)

        info = extract(job.url, True, lambda c: build_ydl_opts(job, c))
        if info.get("_type") == "playlist" and info.get("entries"):
            info = [e for e in info["entries"] if e][0]
        job.title = info.get("title") or job.title
        job.thumbnail = info.get("thumbnail") or job.thumbnail
        job.uploader = info.get("uploader") or job.uploader
        path = resolve_output(info)

        if job.cancel:
            raise Cancelled()

        if path and not os.path.exists(path):
            stem = Path(path).with_suffix("")
            matches = [m for m in sorted(stem.parent.glob(stem.name + ".*"))
                       if m.suffix not in (".part", ".ytdl", ".webp", ".jpg", ".png")]
            if matches:
                path = str(matches[0])

        if path and os.path.exists(path):
            job.filepath = path
            job.filename = os.path.basename(path)
            job.filesize = os.path.getsize(path)

        job.status = "done"
        job.stage = "Ready"
        job.progress = 100.0
        job.finished_at = time.time()
        emit(job)
        append_history(job)

    except Cancelled:
        job.status, job.stage = "cancelled", "Cancelled"
        job.finished_at = time.time()
        emit(job)
    except Exception as exc:                                        # noqa: BLE001
        if "Cancelled" in str(exc):
            job.status, job.stage = "cancelled", "Cancelled"
        else:
            job.status, job.stage = "error", "Failed"
            job.error = friendly_error(str(exc))
        job.finished_at = time.time()
        emit(job)


def friendly_error(msg: str) -> str:
    msg = re.sub(r"\x1b\[[0-9;]*m", "", msg)
    low = msg.lower()
    if "incomplete youtube id" in low or "unsupported url" in low or "is not a valid url" in low:
        return "That doesn't look like a YouTube video link. Copy the link straight from YouTube."
    if "sign in to confirm" in low or ("bot" in low and "confirm" in low):
        return ("YouTube is blocking this server (bot check). The server needs a "
                "cookies.txt from a signed-in YouTube session, or a residential proxy. "
                "See the README section 'When YouTube blocks your server'.")
    if "private video" in low:
        return "This video is private, so it cannot be downloaded."
    if "video unavailable" in low or "removed" in low:
        return "This video is unavailable — it may be deleted or blocked in this region."
    if "members-only" in low or "join this channel" in low:
        return "This is a members-only video and cannot be downloaded."
    if "live" in low and "not yet" in low:
        return "This is an upcoming or live stream. Try again once it has finished."
    if "ffmpeg" in low:
        return "FFmpeg is missing on the server, so this format cannot be prepared."
    if "429" in low:
        return "Too many requests right now. Please wait a minute and try again."
    return msg.replace("ERROR: ", "").strip()[:300]


def worker_loop() -> None:
    while True:
        job = JOBS.get(WORK_QUEUE.get())
        if job is not None:
            if job.cancel:
                job.status = "cancelled"
                emit(job)
            else:
                run_job(job)
        WORK_QUEUE.task_done()


def cleaner_loop() -> None:
    """Optional retention sweep for public deployments."""
    while True:
        time.sleep(120)
        if RETENTION_MINUTES <= 0:
            continue
        cutoff = time.time() - RETENTION_MINUTES * 60
        folder = Path(SETTINGS["download_dir"])
        try:
            for f in folder.iterdir():
                if f.is_file() and f.name != ".gitkeep" and f.stat().st_mtime < cutoff:
                    f.unlink(missing_ok=True)
        except Exception:
            pass


HISTORY_LOCK = threading.Lock()


def append_history(job: Job) -> None:
    with HISTORY_LOCK:
        hist = load_json(HISTORY_FILE, [])
        hist.insert(0, {
            "id": job.id, "url": job.url, "title": job.title, "thumbnail": job.thumbnail,
            "uploader": job.uploader, "mode": job.opts.get("mode"),
            "quality": job.opts.get("quality"), "filename": job.filename,
            "filepath": job.filepath, "filesize": job.filesize,
            "finished_at": job.finished_at,
        })
        save_json(HISTORY_FILE, hist[:100])


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #

class InfoRequest(BaseModel):
    url: str


class DownloadRequest(BaseModel):
    url: str
    mode: str = "video"
    quality: str = "1080"
    audio_format: str = "mp3"
    audio_quality: str = "192"


class SettingsRequest(BaseModel):
    download_dir: Optional[str] = None
    max_workers: Optional[int] = None
    rate_limit: Optional[str] = None
    cookies_from_browser: Optional[str] = None
    proxy: Optional[str] = None


class ContactRequest(BaseModel):
    name: str
    email: str
    subject: str = ""
    message: str


# --------------------------------------------------------------------------- #
# App + page metadata
# --------------------------------------------------------------------------- #

@asynccontextmanager
async def lifespan(_: FastAPI):
    for _i in range(max(1, int(SETTINGS.get("max_workers", 3)))):
        threading.Thread(target=worker_loop, daemon=True).start()
    threading.Thread(target=cleaner_loop, daemon=True).start()
    pump = asyncio.create_task(event_pump())
    yield
    pump.cancel()


app = FastAPI(title=SITE_NAME, docs_url=None, redoc_url=None, lifespan=lifespan)
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


async def event_pump() -> None:
    loop = asyncio.get_running_loop()
    while True:
        try:
            msg = await loop.run_in_executor(None, EVENT_QUEUE.get)
            await hub.broadcast(msg)
        except Exception:
            await asyncio.sleep(0.2)


PAGES: Dict[str, Dict[str, str]] = {
    "/": {
        "template": "home.html",
        "title": f"{SITE_NAME} — Download YouTube Videos in MP4 and MP3, Free",
        "description": ("Free YouTube video downloader. Paste a link and save videos in "
                        "MP4 up to 4K or convert them to MP3 audio. No signup, no software, "
                        "no watermarks."),
        "keywords": "youtube downloader, youtube video downloader, youtube to mp3, download youtube videos",
    },
    "/youtube-downloader": {
        "template": "downloader.html",
        "title": f"YouTube Video Downloader — MP4 up to 4K | {SITE_NAME}",
        "description": ("Download any public YouTube video as MP4 in 360p, 720p, 1080p, 2K "
                        "or 4K. Fast, free and works in your browser — no app to install."),
        "keywords": "youtube video downloader, download youtube video mp4, 1080p youtube downloader, 4k",
    },
    "/youtube-to-mp3": {
        "template": "mp3.html",
        "title": f"YouTube to MP3 Converter — 320kbps Audio | {SITE_NAME}",
        "description": ("Convert YouTube videos to MP3 at up to 320 kbps with cover art and "
                        "song tags included. Free, no registration, no limits on length."),
        "keywords": "youtube to mp3, youtube mp3 converter, 320kbps, youtube audio downloader",
    },
    "/youtube-shorts-downloader": {
        "template": "shorts.html",
        "title": f"YouTube Shorts Downloader — Save Shorts Without Watermark | {SITE_NAME}",
        "description": ("Download YouTube Shorts in full quality MP4 straight to your phone "
                        "or computer. Paste the Shorts link and save it in seconds."),
        "keywords": "youtube shorts downloader, download shorts, shorts to mp4, save youtube shorts",
    },
    "/how-to-download": {
        "template": "how-to.html",
        "title": f"How to Download YouTube Videos — Step by Step Guide | {SITE_NAME}",
        "description": ("A plain-English guide to downloading YouTube videos on Windows, Mac, "
                        "Android and iPhone, plus MP3 conversion and fixes for common errors."),
        "keywords": "how to download youtube videos, save youtube video, youtube download guide",
    },
    "/faq": {
        "template": "faq.html",
        "title": f"FAQ — Formats, Quality, Limits and Privacy | {SITE_NAME}",
        "description": ("Answers about supported formats, download quality, mobile support, "
                        "failed downloads, file limits, privacy and what we store."),
        "keywords": "youtube downloader faq, download quality, supported formats, is it safe",
    },
    "/about": {
        "template": "about.html",
        "title": f"About Us — Who Builds {SITE_NAME}",
        "description": ("AK YouTube Downloader is a small, independent tool built on open "
                        "source software. Here is who makes it and how it works."),
        "keywords": "about ak youtube downloader, open source youtube downloader",
    },
    "/contact": {
        "template": "contact.html",
        "title": f"Contact Us — Support and Feedback | {SITE_NAME}",
        "description": ("Report a bug, request a feature or send a copyright notice. We read "
                        "every message and usually reply within two working days."),
        "keywords": "contact ak youtube downloader, support, report a bug",
    },
    "/privacy-policy": {
        "template": "privacy.html",
        "title": f"Privacy Policy | {SITE_NAME}",
        "description": ("What data AK YouTube Downloader collects, how long files are kept, "
                        "cookies, analytics and your rights."),
        "keywords": "privacy policy, data collection, cookies",
    },
    "/terms-and-conditions": {
        "template": "terms.html",
        "title": f"Terms & Conditions | {SITE_NAME}",
        "description": ("The rules for using AK YouTube Downloader, acceptable use, "
                        "intellectual property and limitation of liability."),
        "keywords": "terms and conditions, terms of service",
    },
    "/disclaimer": {
        "template": "disclaimer.html",
        "title": f"Disclaimer | {SITE_NAME}",
        "description": ("AK YouTube Downloader is not affiliated with YouTube or Google. "
                        "Read what we are and are not responsible for."),
        "keywords": "disclaimer, not affiliated with youtube",
    },
}

FAQS: List[Dict[str, str]] = [
    {"g": "Getting started",
     "q": "How do I download a YouTube video?",
     "a": "Copy the video link from YouTube, paste it into the box on any page of this site and "
          "press Get video. Choose MP4 or MP3, pick a quality, then press Download. The file is "
          "prepared and saved to your device automatically."},
    {"g": "Getting started",
     "q": "Do I need to create an account or install software?",
     "a": "No. There is no signup, no email, no browser extension and no app. Everything happens "
          "in your browser and on our server."},
    {"g": "Getting started",
     "q": "Is AK YouTube Downloader really free?",
     "a": "Yes. Every resolution and every format is free, with no daily limit and no paid plan. "
          "There is nothing to unlock."},
    {"g": "Formats and quality",
     "q": "Which formats are supported?",
     "a": "Video downloads are delivered as MP4, which plays on virtually every device and "
          "editor. Audio downloads are delivered as MP3 at 128, 192, 256 or 320 kbps."},
    {"g": "Formats and quality",
     "q": "What is the highest quality I can download?",
     "a": "Whatever the uploader published. Most videos offer 1080p, many channels publish 1440p "
          "or 2160p (4K), and older uploads may top out at 720p or 480p. The dropdown only ever "
          "lists resolutions that genuinely exist for that video."},
    {"g": "Formats and quality",
     "q": "Why is 1080p missing for some videos?",
     "a": "Because the channel never uploaded a 1080p version. No downloader can create detail "
          "that was not published; tools that offer it anyway are upscaling, which makes the "
          "picture softer, not sharper."},
    {"g": "Formats and quality",
     "q": "Does converting to 320 kbps MP3 improve the sound?",
     "a": "It preserves what is there, but it cannot add detail. YouTube's own audio is usually "
          "around 128–160 kbps, so 192 kbps is a sensible default and 320 kbps mainly makes the "
          "file bigger."},
    {"g": "Formats and quality",
     "q": "Will the file have a watermark?",
     "a": "No. We do not add watermarks, intros, outros or branding of any kind. If a logo "
          "appears in the picture, the creator put it there and it is part of the footage."},
    {"g": "Devices",
     "q": "Does this work on mobile?",
     "a": "Yes. On Android the file goes to your Downloads folder and normally appears in the "
          "Gallery. On iPhone, Safari saves it into the Files app under On My iPhone → Downloads; "
          "from there you can share it into Photos."},
    {"g": "Devices",
     "q": "Which browsers are supported?",
     "a": "Chrome, Edge, Firefox, Safari, Brave, Opera and Samsung Internet, on desktop and "
          "mobile. Very old browsers may not support the live progress bar."},
    {"g": "Devices",
     "q": "Can I use it without an internet connection?",
     "a": "No. The video has to be fetched from YouTube, so a connection is required. Once a file "
          "is on your device it plays offline forever."},
    {"g": "Limits and failures",
     "q": "Why did my download fail?",
     "a": "The usual reasons are: the video is private, members-only or age-restricted; it was "
          "deleted or is blocked in the server's region; it is a live stream that has not "
          "finished; or YouTube temporarily rate-limited the server. The error message on screen "
          "names which one it was."},
    {"g": "Limits and failures",
     "q": "Is there a limit on video length or file size?",
     "a": "There is no hard limit, but very long videos in 4K take several minutes to prepare and "
          "produce files of several gigabytes. If a download times out, try a lower resolution."},
    {"g": "Limits and failures",
     "q": "Can I download private, paid or members-only videos?",
     "a": "No. Only public videos can be processed. Anything that requires signing in, paying or "
          "joining a channel is refused by design."},
    {"g": "Limits and failures",
     "q": "Can I download a whole playlist or channel at once?",
     "a": "Not from the website — links are processed one video at a time to keep the tool fast "
          "and to discourage bulk scraping. Paste each video link separately."},
    {"g": "Limits and failures",
     "q": "Can I download live streams?",
     "a": "Only after the stream has ended and YouTube has published the recording. Streams that "
          "are still running cannot be saved."},
    {"g": "Privacy and safety",
     "q": "Do you store the videos I download?",
     "a": "The file exists on the server only long enough to be sent to you, and is then deleted. "
          "We do not keep a copy and we do not build a library of what people download."},
    {"g": "Privacy and safety",
     "q": "What data do you collect?",
     "a": "No account details, because there are no accounts. Standard server logs may briefly "
          "record an IP address and the time of a request for security and abuse prevention. The "
          "privacy policy sets out the details."},
    {"g": "Privacy and safety",
     "q": "Is it safe? Will I get a virus?",
     "a": "The file you receive is the video itself — an MP4 or MP3 produced by FFmpeg. There are "
          "no bundled installers, no fake download buttons and no pop-up redirects on this site."},
    {"g": "Legal",
     "q": "Is downloading YouTube videos legal?",
     "a": "It depends on the video and where you live. Downloading content you own, content in "
          "the public domain, or content licensed for reuse is generally fine. Saving copyrighted "
          "material without permission may breach YouTube's Terms of Service and local copyright "
          "law. You are responsible for how you use the tool."},
    {"g": "Legal",
     "q": "Are you affiliated with YouTube?",
     "a": "No. AK YouTube Downloader is an independent project with no connection to YouTube, "
          "Google LLC or any of their trademarks."},
    {"g": "Legal",
     "q": "I own a video and want it removed. What do I do?",
     "a": "We do not host videos, so there is nothing stored to remove — but if you believe the "
          "service is being misused against your content, contact us and we will respond."},
]

NAV = [
    ("/", "Home"),
    ("/youtube-downloader", "Downloader"),
    ("/youtube-to-mp3", "YouTube to MP3"),
    ("/how-to-download", "How to Download"),
    ("/faq", "FAQ"),
]


def render(request: Request, path: str, **extra) -> HTMLResponse:
    meta = PAGES[path]
    ctx = {
        "request": request, "site_name": SITE_NAME, "site_url": SITE_URL,
        "contact_email": CONTACT_EMAIL, "nav": NAV, "path": path,
        "title": meta["title"], "description": meta["description"],
        "keywords": meta.get("keywords", ""), "canonical": SITE_URL + path,
        "year": datetime.now(timezone.utc).year,
        "ffmpeg": FFMPEG_AVAILABLE,
        **extra,
    }
    if path == "/faq":
        groups: List[tuple] = []
        for item in FAQS:
            if not groups or groups[-1][0] != item["g"]:
                groups.append((item["g"], []))
            groups[-1][1].append(item)
        ctx["faq_groups"] = groups
        ctx["faqs"] = FAQS
    return templates.TemplateResponse(request, meta["template"], ctx)


def page_route(path: str):
    async def handler(request: Request):
        return render(request, path)
    return handler


for _path in PAGES:
    app.add_api_route(_path, page_route(_path), methods=["GET"],
                      response_class=HTMLResponse, include_in_schema=False)


@app.exception_handler(404)
async def not_found(request: Request, _exc):
    ctx = {"request": request, "site_name": SITE_NAME, "site_url": SITE_URL,
           "contact_email": CONTACT_EMAIL, "nav": NAV, "path": "/404",
           "title": f"Page not found | {SITE_NAME}",
           "description": "The page you are looking for does not exist.",
           "keywords": "", "canonical": SITE_URL + "/404",
           "year": datetime.now(timezone.utc).year, "ffmpeg": FFMPEG_AVAILABLE}
    return templates.TemplateResponse(request, "404.html", ctx, status_code=404)


@app.get("/robots.txt", response_class=PlainTextResponse, include_in_schema=False)
async def robots() -> str:
    return f"User-agent: *\nAllow: /\nDisallow: /api/\n\nSitemap: {SITE_URL}/sitemap.xml\n"


@app.get("/sitemap.xml", include_in_schema=False)
async def sitemap() -> Response:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    prio = {"/": "1.0", "/youtube-downloader": "0.9", "/youtube-to-mp3": "0.9",
            "/youtube-shorts-downloader": "0.8", "/how-to-download": "0.8", "/faq": "0.7"}
    urls = "".join(
        f"<url><loc>{SITE_URL}{p}</loc><lastmod>{today}</lastmod>"
        f"<changefreq>weekly</changefreq><priority>{prio.get(p, '0.5')}</priority></url>"
        for p in PAGES
    )
    xml = ('<?xml version="1.0" encoding="UTF-8"?>'
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
           f'{urls}</urlset>')
    return Response(xml, media_type="application/xml")


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #

@app.get("/api/health")
async def health() -> Dict[str, Any]:
    return {"ok": True, "ffmpeg": FFMPEG_AVAILABLE, "ffmpeg_source": FFMPEG_SOURCE,
            "ytdlp": yt_dlp.version.__version__,
            "cookies": bool(COOKIES_FILE and os.path.isfile(COOKIES_FILE)),
            "cookies_path_set": bool(COOKIES_FILE_SOURCE),
            "proxy": bool(SETTINGS.get("proxy")),
            "player_clients": PLAYER_CLIENTS,
            "pot_provider": bool(POT_BASE_URL),
            "diagnose": bool(DEBUG_KEY)}


@app.post("/api/info")
async def info(req: InfoRequest) -> JSONResponse:
    url = req.url.strip()
    if not url:
        raise HTTPException(400, "Paste a YouTube link first.")
    if not url.startswith("http"):
        url = "https://" + url
    try:
        data = await asyncio.get_running_loop().run_in_executor(None, probe, url)
    except Exception as exc:                                        # noqa: BLE001
        raise HTTPException(400, friendly_error(str(exc)))
    return JSONResponse(data)


def probe(url: str) -> Dict[str, Any]:
    def opts_for(client: Optional[str]) -> Dict[str, Any]:
        o = base_opts(client)
        o["skip_download"] = True
        return o

    v = extract(url, False, opts_for)
    if v.get("_type") == "playlist" and v.get("entries"):
        v = [e for e in v["entries"] if e][0]
    heights = sorted({f.get("height") for f in (v.get("formats") or [])
                      if f.get("vcodec") not in (None, "none") and f.get("height")},
                     reverse=True)
    return {
        "id": v.get("id"), "url": v.get("webpage_url") or url,
        "title": v.get("title") or "Untitled",
        "uploader": v.get("uploader") or v.get("channel") or "",
        "duration": int(v.get("duration") or 0),
        "view_count": v.get("view_count") or 0,
        "thumbnail": v.get("thumbnail") or "",
        "heights": [h for h in heights if h >= 144],
        "is_live": bool(v.get("is_live")),
    }


@app.post("/api/download")
async def download(req: DownloadRequest) -> Dict[str, Any]:
    if req.mode == "audio" and not FFMPEG_AVAILABLE:
        raise HTTPException(400, "MP3 conversion is unavailable on this server right now.")
    with JOBS_LOCK:
        for jid in JOB_ORDER:
            j = JOBS.get(jid)
            if (j and j.url == req.url and j.opts.get("mode") == req.mode
                    and j.status in ("queued", "downloading", "processing")):
                return {"job": j.public(), "duplicate": True}

    job = Job(id=uuid.uuid4().hex[:12], url=req.url, opts=req.model_dump(), title=req.url)
    with JOBS_LOCK:
        JOBS[job.id] = job
        JOB_ORDER.append(job.id)
        if len(JOB_ORDER) > 200:                       # keep memory bounded
            old = JOB_ORDER.pop(0)
            JOBS.pop(old, None)
    WORK_QUEUE.put(job.id)
    EVENT_QUEUE.put({"type": "job", "job": job.public()})
    return {"job": job.public()}


@app.get("/api/jobs")
async def jobs() -> Dict[str, Any]:
    with JOBS_LOCK:
        return {"jobs": [JOBS[i].public() for i in JOB_ORDER if i in JOBS]}


@app.post("/api/jobs/{job_id}/cancel")
async def cancel(job_id: str) -> Dict[str, Any]:
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Unknown download")
    job.cancel = True
    if job.status == "queued":
        job.status, job.stage = "cancelled", "Cancelled"
        emit(job)
    return {"ok": True}


@app.post("/api/jobs/clear")
async def clear_jobs() -> Dict[str, Any]:
    with JOBS_LOCK:
        for jid in list(JOB_ORDER):
            if JOBS.get(jid) and JOBS[jid].status in ("done", "error", "cancelled"):
                JOBS.pop(jid, None)
                JOB_ORDER.remove(jid)
    return {"ok": True}


@app.get("/api/file/{job_id}")
async def get_file(job_id: str):
    job = JOBS.get(job_id)
    path = job.filepath if job else ""
    if not path:
        rec = next((h for h in load_json(HISTORY_FILE, []) if h["id"] == job_id), None)
        path = rec["filepath"] if rec else ""
    if not path or not os.path.isfile(path):
        raise HTTPException(404, "That file is no longer available. Download it again.")
    return FileResponse(path, filename=os.path.basename(path),
                        media_type="application/octet-stream")


@app.get("/api/history")
async def history() -> Dict[str, Any]:
    hist = load_json(HISTORY_FILE, [])
    for h in hist:
        h["exists"] = bool(h.get("filepath")) and os.path.exists(h["filepath"])
    return {"history": hist}


@app.post("/api/history/clear")
async def clear_history() -> Dict[str, Any]:
    save_json(HISTORY_FILE, [])
    return {"ok": True}


@app.get("/api/settings")
async def get_settings() -> Dict[str, Any]:
    return {**SETTINGS, "ffmpeg": FFMPEG_AVAILABLE, "ffmpeg_source": FFMPEG_SOURCE}


@app.post("/api/settings")
async def set_settings(req: SettingsRequest) -> Dict[str, Any]:
    for key, value in req.model_dump(exclude_none=True).items():
        SETTINGS[key] = value
    Path(SETTINGS["download_dir"]).mkdir(parents=True, exist_ok=True)
    save_json(SETTINGS_FILE, SETTINGS)
    return {"ok": True, **SETTINGS}


@app.post("/api/contact")
async def contact(req: ContactRequest) -> Dict[str, Any]:
    if not req.name.strip() or not req.message.strip():
        raise HTTPException(400, "Please fill in your name and message.")
    if "@" not in req.email:
        raise HTTPException(400, "Please enter a valid email address.")
    msgs = load_json(MESSAGES_FILE, [])
    msgs.insert(0, {"name": req.name[:120], "email": req.email[:160],
                    "subject": req.subject[:200], "message": req.message[:4000],
                    "at": datetime.now(timezone.utc).isoformat()})
    save_json(MESSAGES_FILE, msgs[:500])
    return {"ok": True}


@app.get("/api/diagnose")
async def diagnose(url: str, key: str = "") -> Dict[str, Any]:
    """
    Try every player client on one URL and report the raw error each gives.
    Disabled unless DEBUG_KEY is set; call as /api/diagnose?key=...&url=...
    Takes up to a minute because it really contacts YouTube once per client.
    """
    if not DEBUG_KEY or key != DEBUG_KEY:
        raise HTTPException(403, "Diagnostics are disabled. Set DEBUG_KEY and pass ?key=")

    def run() -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "ytdlp": yt_dlp.version.__version__,
            "cookies_env": COOKIES_FILE_SOURCE or None,
            "cookies_loaded": bool(COOKIES_FILE and os.path.isfile(COOKIES_FILE)),
            "cookie_lines": None,
            "proxy": SETTINGS.get("proxy") or None,
            "pot_provider": POT_BASE_URL or None,
            "outbound_ip": None,
            "attempts": [],
        }
        if out["cookies_loaded"]:
            try:
                with open(COOKIES_FILE, "r", encoding="utf-8", errors="ignore") as fh:
                    lines = [ln for ln in fh if "youtube" in ln.lower()]
                out["cookie_lines"] = len(lines)
            except Exception:
                pass
        try:
            import urllib.request
            out["outbound_ip"] = urllib.request.urlopen(
                "https://api.ipify.org", timeout=8).read().decode()[:40]
        except Exception as exc:                                    # noqa: BLE001
            out["outbound_ip"] = f"unknown ({exc})"

        for client in PLAYER_CLIENTS:
            entry: Dict[str, Any] = {"client": client}
            started = time.time()
            try:
                o = base_opts(client)
                o["skip_download"] = True
                with yt_dlp.YoutubeDL(o) as ydl:
                    info = ydl.extract_info(url, download=False)
                fmts = info.get("formats") or []
                entry["ok"] = True
                entry["formats"] = len(fmts)
                entry["heights"] = sorted({f.get("height") for f in fmts
                                           if f.get("height")}, reverse=True)[:6]
                entry["title"] = (info.get("title") or "")[:80]
            except Exception as exc:                                # noqa: BLE001
                entry["ok"] = False
                entry["error"] = re.sub(r"\s+", " ", str(exc))[:400]
            entry["seconds"] = round(time.time() - started, 1)
            out["attempts"].append(entry)
        return out

    return await asyncio.get_running_loop().run_in_executor(None, run)


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await hub.connect(ws)
    try:
        with JOBS_LOCK:
            snapshot = [JOBS[i].public() for i in JOB_ORDER if i in JOBS]
        await ws.send_json({"type": "snapshot", "jobs": snapshot})
        while True:
            await ws.receive_text()
    except (WebSocketDisconnect, Exception):
        hub.disconnect(ws)


app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    print(f"\n  {SITE_NAME}")
    print(f"  Open        http://{host}:{port}")
    print(f"  Saving to   {SETTINGS['download_dir']}")
    print(f"  FFmpeg      {FFMPEG_SOURCE}")
    print("  Ctrl+C to stop.\n")
    if os.environ.get("NO_BROWSER") != "1":
        threading.Timer(1.2, lambda: __import__("webbrowser").open(
            f"http://{'127.0.0.1' if host == '0.0.0.0' else host}:{port}")).start()
    uvicorn.run(app, host=host, port=port, log_level="warning")
