# TubeGrab

YouTube to MP4 / MP3 downloader and installable app (PWA).

**Made by Jhames Martin** · [Portfolio](https://jhamesmartinph.vercel.app) · [GitHub @jrmph13](https://github.com/jrmph13)

Paste a link, see the thumbnail, choose **Video MP4** (every resolution the video really has, 4K down to 144p, with file sizes) or **Audio** (MP3 320 / 192 / 128 kbps, or M4A with no conversion), and download. Progress is real: it comes straight from the downloader, with speed, time left and the processing step (joining video and audio, converting to MP3).

Built with Flask, TubeGrab Engine (its own YouTube downloader), yt-dlp as backup, Deno (solves YouTube's player challenges) and ffmpeg. Icons: [Lucide](https://lucide.dev) (ISC license, see `LICENSE-lucide.txt`).

MP3s include the video thumbnail as cover art.

## Install it as an app
- **Android (Chrome):** open the site, tap **Install app** at the top (or menu, then Install app). After installing,
  tap **Share** in the YouTube app and choose **TubeGrab**: it opens with the video already loaded.
- **iPhone/iPad (Safari):** tap **Install app** for the steps: Share, then **Add to Home Screen**.
- **Desktop (Chrome/Edge):** click **Install app** or the install icon in the address bar.

The app works offline for its own screens; downloading still needs the internet.

## Deploy to Render
1. Put this folder in a GitHub repo.
2. On render.com: **New → Blueprint**, pick the repo. It reads `render.yaml` and builds the Docker image (ffmpeg included).
3. Open `https://<name>.onrender.com/api/status`. Everything should say `true` (except `cookies` until you add them):
   `yt_dlp_ejs`, `deno`, `ffmpeg`. If `deno` is false, the YouTube downloads will fail, so redeploy.
4. Open your `https://<name>.onrender.com` link and paste a video.

### If you see "YouTube blocked this server with a bot check"
YouTube often blocks cloud servers like Render. Give the app cookies from a browser:
1. Install the **Get cookies.txt LOCALLY** extension (Chrome/Firefox).
2. Sign in to youtube.com (use a spare Google account), open any video, export cookies for youtube.com.
3. Render → your service → **Environment** → add `YTDLP_COOKIES` and paste the whole file contents → Save.

Cookies expire every few weeks; export again if the error returns.
If downloads break after a YouTube change, use **Manual Deploy → Clear build cache & deploy** to get the newest yt-dlp.

## TubeGrab Engine
TubeGrab has its own YouTube reader and downloader (`engine.py`), used first:
- Talks to YouTube's player API as the visionOS app, which gives direct stream links
  (no JavaScript puzzle, no PO token).
- Downloads video and audio **at the same time**, each in 10 MB pieces over 4 connections.
- Joins, converts and tags with ffmpeg (MP4, MP3 with cover art, M4A with cover art).

If the engine can't do a video (for example YouTube blocks the server), TubeGrab switches to
yt-dlp automatically, with cookies, PO token and fallback clients. When YouTube blocks the
engine, it rests for 10 minutes so visitors don't wait on it. `/api/status` shows
`engine_resting_seconds` and the last engine error. Set `ENGINE=ytdlp` to turn the engine off.

## Speed
- The app starts answering right away; yt-dlp updates itself in the background and reloads when nobody is downloading.
- "Get video" results are remembered for 15 minutes, so opening the same video again (or from Recent) is instant,
  and the download starts without reading the video from YouTube a second time.
- Downloads are fetched in 10 MB pieces (avoids YouTube's slow-down on long requests), with 4 fragments at a time.
- **M4A** is the fastest audio: it keeps YouTube's own AAC audio, no conversion. MP3 has to be converted,
  which is slow on Render's free CPU; TubeGrab uses LAME's faster mode (about 35% quicker in tests).
- Pages and data are gzip-compressed (the page drops from about 75 KB to about 20 KB).

### Keep the free server awake (optional)
Render's free plan sleeps after 15 minutes without visitors, and waking takes about a minute.
To keep it awake, set up a free ping, which works from a phone:
1. Go to **cron-job.org** and make a free account.
2. Create a cron job with the URL `https://<name>.onrender.com/healthz`, every **10 minutes**.

One service awake all month uses about 720 of the 750 free hours, so keep only one free service awake.
Also watch your Render **bandwidth**: every downloaded file is outbound traffic from Render.

## If downloads fail on Render
Open `https://<name>.onrender.com/api/status` first. You want `deno`, `ffmpeg`, `yt_dlp_ejs`,
`po_token_provider` and `po_token_plugin` all `true`.

**"YouTube is blocking requests from this server" / "Failed to extract any player response"**
YouTube often blocks cloud servers. TubeGrab already does three things on its own:
1. Updates yt-dlp to the newest version (nightly included) every time the server starts.
2. Runs a PO token provider (bgutil) next to the app so requests look like a normal browser.
3. If YouTube still refuses, retries with other YouTube clients (`visionos`, `web_embedded`, `tv_samsung`).

If it still fails:
1. Wait 10 to 30 minutes. Render's IP may be rate-limited for a while.
2. Restart the service (Render → Manual Deploy → Restart) to pull a newer yt-dlp.
3. Add cookies in `YTDLP_COOKIES` (from a spare account). If that makes things worse, remove them:
   cookies plus PO tokens sometimes clash.
4. Last resort: set `YTDLP_PROXY` to a residential proxy. YouTube judges the IP address, and
   datacenter IPs (like Render's) are the ones it blocks.

## Run on your computer
```bash
pip install -r requirements.txt   # needs ffmpeg installed too
python app.py                     # http://localhost:5000
```
or `docker build -t tubegrab . && docker run -p 10000:10000 tubegrab`

## Settings (environment variables)
| Name | Default | What it does |
|---|---|---|
| `MAX_DURATION_SECONDS` | 3600 | Longest video allowed |
| `JOB_TTL_SECONDS` | 1800 | How long finished files are kept for re-download |
| `MAX_ACTIVE_JOBS` | 3 | Downloads allowed at the same time (keeps the free plan from running out of memory) |
| `YTDLP_COOKIES` | none | Contents of cookies.txt |
| `YTDLP_PROXY` | none | Proxy for YouTube requests, e.g. `http://user:pass@host:port` |
| `UPDATE_YTDLP_ON_START` | 1 | Update yt-dlp each time the server starts |
| `POT_PROVIDER` | 1 | Run the PO token provider |
| `YTDLP_FALLBACK_CLIENTS` | visionos,web_embedded,tv_samsung | Clients to retry with when YouTube refuses |
| `ENGINE` | own | `own` = TubeGrab Engine first, yt-dlp as backup; `ytdlp` = yt-dlp only |
| `ENGINE_COOLDOWN_SECONDS` | 600 | How long the engine rests after YouTube blocks it |
| `INFO_CACHE_SECONDS` | 900 | How long "Get video" results are remembered |
| `YTDLP_COOKIES_FILE` | none | Or a path, e.g. a Render Secret File at `/etc/secrets/cookies.txt` |

## How it works
- `POST /api/info` reads title, thumbnail and the real list of formats.
- `GET /api/thumbnail/<video id>` downloads the best available thumbnail as a JPG (maxres, then sd, hq, mq).
- `POST /api/jobs` starts a download in the background; `GET /api/jobs/<id>` returns live progress; `POST /api/jobs/<id>/cancel` stops it; `GET /api/jobs/<id>/file` sends the finished file.
- Video downloads prefer H.264 + AAC so the MP4 plays on every phone and PC.
- The server runs one worker process (with many threads) so all requests see the same download jobs. Keep it that way, or move jobs to Redis before adding workers.

## Free plan limits
Render's free plan sleeps when idle (first visit takes 30–60 s) and has 512 MB RAM and limited disk, so 4K or very long videos may fail there. A paid instance handles them fine.

Only download videos you own or have permission to use.
