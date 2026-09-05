# AK YouTube Downloader

A multi-page YouTube downloader website. Paste a link, choose MP4 or MP3, and the finished file
saves straight to the visitor's device. Built with FastAPI, yt-dlp and FFmpeg — no YouTube Data
API, no API keys, no paid services.

```
FastAPI + Jinja2  →  yt-dlp  →  FFmpeg  →  the visitor's browser
```

---

## Contents

- [Run it locally](#run-it-locally)
- [Project structure](#project-structure)
- [Configuration](#configuration)
- [Deploy to production](#deploy-to-production)
- [Push to a new GitHub repo](#push-to-a-new-github-repo)
- [After you go live: SEO checklist](#after-you-go-live-seo-checklist)
- [Replacing the logo](#replacing-the-logo)
- [Troubleshooting](#troubleshooting)

---

## Run it locally

**Windows** — double-click `run.bat`.
If it says *"Python was not found… Microsoft Store"*:

1. `winget install Python.Python.3.12` (or python.org, ticking **Add python.exe to PATH**)
2. Settings → Apps → Advanced app settings → App execution aliases → turn **off** `python.exe`
   and `python3.exe`
3. Reopen the terminal and run `run.bat` again.

**macOS / Linux**

```bash
chmod +x run.sh
./run.sh
```

Then open <http://127.0.0.1:8000>. FFmpeg does not need installing — `imageio-ffmpeg` ships a
static build that the app finds automatically, and a system FFmpeg is used when present.

---

## Project structure

```
app.py                  FastAPI app: pages, API, job queue, sitemap, robots
templates/              Jinja2 templates (one per page) + _tool.html partial
static/css/style.css    Design system and responsive rules
static/js/app.js        Downloader flow, auto-save, mobile nav, contact form
static/img/             logo.svg and favicon.svg
Dockerfile              Production image (includes ffmpeg)
render.yaml             One-click config for Render
Procfile                For Railway / Heroku-style hosts
.env.example            Every environment variable, documented
```

### Pages

| URL | Purpose |
|---|---|
| `/` | Home — hero, live tool, features, mini FAQ |
| `/youtube-downloader` | Main MP4 tool + resolution guide |
| `/youtube-to-mp3` | MP3 converter + bitrate guide |
| `/youtube-shorts-downloader` | Shorts landing page |
| `/how-to-download` | Step-by-step guide (desktop, Android, iPhone, troubleshooting) |
| `/faq` | 22 questions with FAQ structured data |
| `/about` | About us |
| `/contact` | Contact form (saved to `data/messages.json`) + email |
| `/privacy-policy`, `/terms-and-conditions`, `/disclaimer` | Legal pages |
| `/sitemap.xml`, `/robots.txt` | Generated automatically from the page list |

Every page has its own title, meta description, canonical URL, Open Graph tags, breadcrumbs and
JSON-LD. Add a page by adding one entry to `PAGES` in `app.py` and one template — the sitemap and
navigation pick it up automatically.

---

## Configuration

Set these as environment variables (locally in `.env`, in production in your host's dashboard).
See `.env.example`.

| Variable | Default | What it does |
|---|---|---|
| `SITE_NAME` | AK YouTube Downloader | Brand name in titles and schema |
| `SITE_URL` | http://127.0.0.1:8000 | **Set this in production** — used for canonical URLs, sitemap and OG tags |
| `CONTACT_EMAIL` | support@… | Shown on the contact and legal pages |
| `PORT` / `HOST` | 8000 / 127.0.0.1 | Bind address; hosts set `PORT` for you |
| `MAX_WORKERS` | 3 | Downloads processed in parallel |
| `RETENTION_MINUTES` | 0 | Delete prepared files after N minutes. **Use 15–60 in production**, 0 locally |
| `DOWNLOAD_DIR` | ./downloads | Where files are prepared |
| `PROXY` | *(empty)* | Route YouTube requests through a proxy if the host IP gets rate-limited. `http://`, `https://`, `socks5://` or `socks5h://`, with optional `user:pass@` |
| `RATE_LIMIT` | *(empty)* | Global download speed cap, e.g. `5M` |
| `COOKIES_FILE` | *(empty)* | Path to a `cookies.txt` from a signed-in YouTube session — the main fix for the bot check |
| `YTDLP_PLAYER_CLIENTS` | default,android_vr,web_safari,tv,mweb | Which YouTube clients to try, in order |
| `POT_BASE_URL` | *(empty)* | URL of a bgutil PO-token provider, e.g. `http://127.0.0.1:4416` |
| `DEBUG_KEY` | *(empty)* | Enables `/api/diagnose?key=…&url=…`. Leave empty in normal operation |
| `NO_BROWSER` | — | Set to `1` on a server so it does not try to open a browser |

---

## Deploy to production

The app is a standard ASGI application: `uvicorn app:app --host 0.0.0.0 --port $PORT`.
Pick one of these.

### Option A — Render (easiest free start)

1. Push the code to GitHub (see the next section).
2. Go to <https://render.com> → **New → Web Service** → connect the repo.
3. Render reads `render.yaml` and uses the Dockerfile automatically. If you create the service
   manually instead, choose **Docker** as the runtime.
4. Set the environment variables — at minimum `SITE_URL` to the real URL Render gives you.
5. Deploy. Health check is `/api/health`.

Notes on the free tier: the service sleeps after ~15 minutes of inactivity (first request then
takes ~30 s to wake) and disk is ephemeral, which is fine because files are temporary anyway.
Set `RETENTION_MINUTES=30` and `MAX_WORKERS=2`.

### Option B — Railway

1. <https://railway.app> → **New Project → Deploy from GitHub repo**.
2. Railway detects the Dockerfile. Add the same environment variables.
3. Generate a domain under **Settings → Networking**, then set `SITE_URL` to it and redeploy.

### Option C — Fly.io

```bash
fly launch --no-deploy          # accept the detected Dockerfile
fly secrets set SITE_URL=https://your-app.fly.dev RETENTION_MINUTES=30 MAX_WORKERS=2
fly deploy
```

### Option D — Your own VPS (most control, best for YouTube reliability)

```bash
# on the server (Ubuntu)
sudo apt update && sudo apt install -y python3 python3-venv ffmpeg nginx
git clone https://github.com/<you>/ak-youtube-downloader.git
cd ak-youtube-downloader
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# systemd service
sudo tee /etc/systemd/system/akytd.service >/dev/null <<'EOF'
[Unit]
Description=AK YouTube Downloader
After=network.target

[Service]
WorkingDirectory=/root/ak-youtube-downloader
Environment=SITE_URL=https://your-domain.com
Environment=RETENTION_MINUTES=30
Environment=NO_BROWSER=1
ExecStart=/root/ak-youtube-downloader/.venv/bin/uvicorn app:app --host 127.0.0.1 --port 8000
Restart=always

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl enable --now akytd
```

Then put Nginx in front (proxy `/` to `127.0.0.1:8000`, and pass WebSocket headers for `/ws`):

```nginx
server {
  server_name your-domain.com;
  client_max_body_size 0;
  location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_read_timeout 900s;
  }
}
```

Finish with HTTPS: `sudo certbot --nginx -d your-domain.com`.

### Docker anywhere

```bash
docker build -t ak-youtube-downloader .
docker run -d -p 8000:8000 \
  -e SITE_URL=https://your-domain.com \
  -e RETENTION_MINUTES=30 \
  --name akytd ak-youtube-downloader
```

### Things to know before going public

- **Bandwidth is yours.** Every download runs through your server twice — once from YouTube, once
  to the visitor. A busy free tier will hit its limit quickly.
- **Shared-host IPs get rate-limited.** Cloud provider IP ranges are heavily used by bots, so
  YouTube may return "sign in to confirm you're not a bot". A VPS or a residential proxy
  (`PROXY` env var) is far more reliable than a free PaaS.
- **Keep yt-dlp fresh.** YouTube changes often. Redeploy monthly, or run
  `pip install -U yt-dlp` on a VPS from a cron job.
- **Legal exposure is real.** Read `/disclaimer` and `/terms-and-conditions` and make sure you are
  comfortable operating the service in your country before advertising it.

---

## Push to a new GitHub repo

The repository is already initialised with a first commit. To publish it:

**With the GitHub CLI**

```bash
gh auth login
gh repo create ak-youtube-downloader --public --source=. --remote=origin --push
```

**Without the CLI**

1. Create an empty repo at <https://github.com/new> named `ak-youtube-downloader`
   (no README, no .gitignore — this project has both).
2. Then:

```bash
git remote add origin https://github.com/<your-username>/ak-youtube-downloader.git
git branch -M main
git push -u origin main
```

`.gitignore` already excludes `.venv/`, `data/`, `downloads/`, `bin/` and `.env`, so no
downloaded media or private settings can be committed by accident.

---

## Using a proxy

Cloud hosting IPs are shared with bots, so YouTube often answers them with "sign in to confirm
you're not a bot". Routing requests through a proxy — ideally a **residential** or **mobile** one —
fixes it, because those IPs look like ordinary home connections.

Set one variable; no code change is needed:

```
PROXY=http://username:password@gate.provider.com:7000
```

Accepted schemes: `http://`, `https://`, `socks5://`, `socks5h://` (use `socks5h` so DNS is
resolved by the proxy). Credentials are optional if the provider authorises by IP instead.

**Where to set it**

| Host | How |
|---|---|
| Render | Dashboard → your service → **Environment** → Add Environment Variable → Save (redeploys automatically) |
| Railway | Project → service → **Variables** → New Variable |
| Fly.io | `fly secrets set PROXY="http://user:pass@host:port"` |
| Docker | `docker run -e PROXY="http://user:pass@host:port" ...` |
| VPS + systemd | Add `Environment=PROXY=http://user:pass@host:port` to the unit, then `systemctl daemon-reload && systemctl restart akytd` |
| Windows (local test) | `$env:PROXY="http://user:pass@host:port"` then `.\.venv\Scripts\python.exe app.py` |
| macOS / Linux (local) | `PROXY="http://user:pass@host:port" ./run.sh` |

The value is read once at startup, so restart or redeploy after changing it. Environment variables
override anything stored in `data/settings.json`.

**Checking it works:** open `/api/health` (the service should still report `ok`), then try a
download. If the proxy is wrong you get an immediate "Unable to connect to proxy" error rather
than a YouTube error — that difference tells you which side is failing.

**Choosing a provider:** datacenter proxies are cheap but usually just as blocked as your host's
own IP. Residential or mobile pools (Webshare, IPRoyal, Bright Data, Smartproxy and similar) are
what actually work, typically a few dollars a month for the traffic a small site uses. Rotate on
each request if your provider offers it.

## When YouTube blocks your server

The most common production failure is this message:

> YouTube is blocking this server (bot check)

Nothing is wrong with the code — the same link works on your PC. YouTube treats requests from
datacentre IP ranges (Render, Railway, Fly, AWS, every free host) as bots, because those ranges are
full of scrapers. Your home IP is trusted; a shared cloud IP is not.

Three fixes, cheapest first.

### 1. cookies.txt from a signed-in account (free, works immediately)

Give the server a logged-in YouTube session and the bot check goes away.

1. Create a **throwaway Google account** — never use your main one. Cookies grant full access to
   that account and Google may flag it for unusual activity.
2. Install the browser extension **"Get cookies.txt LOCALLY"** (Chrome or Firefox).
3. Open a **private/incognito window**, sign in to youtube.com with the throwaway account, open any
   video, then click the extension and export cookies for `youtube.com`. You get a `cookies.txt`.
4. Do **not** log out in that window — just close it. Logging out invalidates the cookies.
5. Put the file on the server and point `COOKIES_FILE` at it:

| Host | How |
|---|---|
| Render | Service → **Environment** → *Secret Files* → Add, filename `cookies.txt`, paste contents. Then set `COOKIES_FILE=/etc/secrets/cookies.txt` |
| Railway | Variables → add the file through a volume, or commit it to a private repo and set the path |
| Docker / VPS | Copy the file next to `app.py` and set `COOKIES_FILE=/app/cookies.txt` |

Never commit `cookies.txt` to a public repo — anyone who reads it is signed in as that account.
`.gitignore` already excludes it.

**Read-only secrets are handled for you.** Render Secret Files (and Kubernetes secrets, and any
read-only volume) cannot be written to, but yt-dlp saves the cookie jar back when it finishes — so
pointing it straight at `/etc/secrets/cookies.txt` fails with
`[Errno 30] Read-only file system`. The app copies the file to `data/cookies.runtime.txt` at
startup and uses that copy, so the mounted secret stays untouched. Update the secret and restart
to pick up new cookies.

Cookies expire, typically in a few weeks, and faster if the account is used from a very different
location than the server. Re-export when downloads start failing again.

### 2. Residential proxy (paid, most reliable)

Set `PROXY` (see the previous section). Residential and mobile IPs look like ordinary home
connections, so the bot check does not trigger. Combining a proxy with cookies is the setup most
production downloaders actually run.

### 3. A VPS instead of a PaaS

A $4–6/month VPS from Hetzner, Contabo, DigitalOcean or Oracle Cloud's always-free tier often just
works, because those IPs are less abused than free PaaS ranges. Cheaper than a proxy plan, and you
get real bandwidth.

### Also built in

- **Player-client fallback.** YouTube exposes a different API to each client (web, android_vr,
  tv, mweb). If one is refused, the app automatically retries with the next. Override the order
  with `YTDLP_PLAYER_CLIENTS=default,android_vr,tv`.
- **Fresh yt-dlp.** Extraction breaks whenever YouTube changes something and yt-dlp patches it
  within days. Redeploy monthly — that reinstalls the latest version.
- Check `/api/health` to confirm what the server has: `"cookies": true` means the cookie file was
  found, `"proxy": true` means a proxy is configured.

### Diagnosing it properly

Guessing wastes time. Set `DEBUG_KEY` to any random string, redeploy, then open:

```
https://your-app.onrender.com/api/diagnose?key=YOUR_KEY&url=https://www.youtube.com/watch?v=VIDEO_ID
```

It tries every player client against that video and returns, per client, either the formats it
found or the raw error — plus the server's outbound IP, whether the cookie file loaded and how
many YouTube cookie lines it contains. It takes up to a minute because each attempt is a real
request. Unset `DEBUG_KEY` when you are done; the endpoint is off whenever it is empty.

### Player clients and PO tokens

Per the [yt-dlp PO Token Guide](https://github.com/yt-dlp/yt-dlp/wiki/PO-Token-Guide), YouTube now
requires a **proof-of-origin token** from most clients on flagged IPs. The ones that still work
without a token:

| Client | Needs PO token? | Notes |
|---|---|---|
| `android_vr` | No | Best first choice on a server |
| `web_embedded` | No | Only videos the uploader allows to be embedded |
| `tv` | No | Needs account cookies, otherwise every format comes back DRM-protected |
| `web`, `web_safari` | Yes (streaming + subtitles) | |
| `mweb`, `android`, `ios`, `tv_simply` | Yes | `android`/`ios` also ignore cookies |

The default order is therefore `android_vr,tv,web_embedded,default`. Override with
`YTDLP_PLAYER_CLIENTS`.

### Running a PO token provider

If the clients above are exhausted, run the bgutil provider next to the app and the token-gated
clients start working:

```bash
docker run --name bgutil-provider -d --init -p 4416:4416 brainicism/bgutil-ytdlp-pot-provider
pip install -U bgutil-ytdlp-pot-provider     # the yt-dlp plugin that talks to it
```

Then set `POT_BASE_URL=http://127.0.0.1:4416` (or the provider's URL) and add
`web_safari` back into `YTDLP_PLAYER_CLIENTS`.

`docker-compose.yml` in this repo runs both containers together — `docker compose up -d`.

### The honest summary

On a free PaaS, cookies alone often are not enough any more: the IP is flagged before the cookies
are even read, and using cookies from a flagged IP can get that Google account restricted. The
setups that actually stay up are, in order of reliability:

1. **VPS with a clean IP** (₹400–600 / $5 a month) + cookies — simplest thing that keeps working.
2. **Residential proxy** + cookies — works from any host, costs a few dollars a month.
3. **PO token provider** + cookies — helps, but does not fix a badly flagged IP on its own.

## After you go live: SEO checklist

1. Set `SITE_URL` to the final HTTPS domain and redeploy — canonical tags and the sitemap depend
   on it.
2. Add the site to [Google Search Console](https://search.google.com/search-console) and submit
   `https://your-domain.com/sitemap.xml`.
3. Do the same in [Bing Webmaster Tools](https://www.bing.com/webmasters) (it also feeds several
   smaller engines).
4. Test the FAQ and HowTo markup with the
   [Rich Results Test](https://search.google.com/test/rich-results).
5. Run [PageSpeed Insights](https://pagespeed.web.dev/) — the pages are static HTML with one CSS
   and one JS file, so scores should be high; check the mobile column.
6. Pick one canonical hostname (with or without `www`) and 301-redirect the other at your DNS or
   Nginx layer.
7. Write for the long tail. The pages already target "youtube video downloader", "youtube to mp3"
   and "youtube shorts downloader"; adding honest guide pages ("download a YouTube video on
   iPhone", "best format for archiving lectures") is what actually earns traffic.
8. Keep the content truthful. Search engines and users both punish downloader sites that promise
   8K from a 720p upload.

---

## Replacing the logo

The brand mark lives in three places, all of them plain SVG:

- `static/img/logo.svg` — full wordmark, used for Open Graph and sharing previews
- `static/img/favicon.svg` — the browser tab icon
- the inline `<svg>` inside `.brand-mark` in `templates/base.html` — header and footer

Drop your own artwork into the first two files and paste its paths into the third. The brand
colour is defined once, as `--brand` in `static/css/style.css`.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| "YouTube asked this server to sign in" | The host's IP is flagged. Use a VPS, or set `PROXY` |
| "Too many requests" | Lower `MAX_WORKERS`, add a proxy, or wait it out |
| Download works locally, fails when deployed | Almost always the IP issue above — not a code problem |
| MP3 option missing | FFmpeg not found; the Docker image installs it, otherwise `pip install imageio-ffmpeg` |
| Progress bar never moves behind Nginx | Add the WebSocket `Upgrade`/`Connection` headers shown above |
| Old video suddenly fails | `pip install -U yt-dlp` and redeploy |

---

## Licence and credits

Built on [yt-dlp](https://github.com/yt-dlp/yt-dlp) (Unlicense), [FFmpeg](https://ffmpeg.org/)
(LGPL/GPL) and [FastAPI](https://fastapi.tiangolo.com/) (MIT). Not affiliated with YouTube or
Google LLC.
