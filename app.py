"""TubeGrab — YouTube downloader. Made by Jhames Martin."""
import os
import re
import shutil
import tempfile
import threading
import time
import copy
import gzip
import uuid
from io import BytesIO

from flask import Flask, abort, jsonify, redirect, render_template, request, send_file, send_from_directory, url_for
import yt_dlp

import engine

app = Flask(__name__)

DOWNLOAD_ROOT = os.path.join(tempfile.gettempdir(), "tubegrab")
os.makedirs(DOWNLOAD_ROOT, exist_ok=True)

MAX_DURATION = int(os.environ.get("MAX_DURATION_SECONDS", "3600"))
JOB_TTL = int(os.environ.get("JOB_TTL_SECONDS", "1800"))
AUDIO_BITRATES = ["320", "192", "128"]
INFO_TTL = int(os.environ.get("INFO_CACHE_SECONDS", "900"))
MAX_ACTIVE_JOBS = int(os.environ.get("MAX_ACTIVE_JOBS", "3"))

# Cookies help get past YouTube's "confirm you're not a bot" check on cloud IPs.
# yt-dlp rewrites the cookie file as it runs, so always work from a writable copy
# (Render Secret Files under /etc/secrets are read-only).
COOKIES_FILE = None
_cookie_src = os.environ.get("YTDLP_COOKIES_FILE")
_cookie_text = os.environ.get("YTDLP_COOKIES")
if _cookie_src and os.path.exists(_cookie_src):
    with open(_cookie_src) as f:
        _cookie_text = f.read()
if _cookie_text:
    COOKIES_FILE = os.path.join(DOWNLOAD_ROOT, "cookies.txt")
    with open(COOKIES_FILE, "w") as f:
        # env vars pasted as one line sometimes arrive with literal "\n"
        f.write(_cookie_text.replace("\\n", "\n").strip() + "\n")

YT_REGEX = re.compile(
    r"^(https?://)?((www|m|music)\.)?(youtube\.com/(watch\?|shorts/|live/|embed/)|youtu\.be/)\S+", re.I
)

JOBS = {}
ACTIVE = ("queued", "preparing", "downloading", "processing")
JOBS_LOCK = threading.Lock()


class Cancelled(Exception):
    pass


PROXY = os.environ.get("YTDLP_PROXY", "").strip()  # optional, e.g. a residential proxy
POT_URL = os.environ.get("POT_PROVIDER_URL", "http://127.0.0.1:4416").strip()

# If YouTube rejects the normal clients, try again with clients that don't need a PO token.
# visionos and web_embedded don't need a PO token for downloads; tv_samsung needs Deno (installed).
FALLBACK_CLIENTS = [c.strip() for c in os.environ.get(
    "YTDLP_FALLBACK_CLIENTS", "visionos,web_embedded,tv_samsung").split(",") if c.strip()]

# TubeGrab Engine (own scraper) first, yt-dlp as backup. ENGINE=ytdlp turns the engine off.
ENGINE_MODE = os.environ.get("ENGINE", "own").strip().lower()
ENGINE_COOLDOWN = int(os.environ.get("ENGINE_COOLDOWN_SECONDS", "600"))
ENGINE_CACHE = {}  # video id -> (time, info, formats)
ENGINE_STATE = {"blocked_until": 0.0, "last_ok": None, "last_error": None}

# When yt-dlp is blocked too, answer the next visitors right away instead of making each
# of them wait through every method again. Short, so recovery is noticed quickly.
BLOCK_MEMORY = int(os.environ.get("BLOCK_MEMORY_SECONDS", "120"))
YTDLP_BLOCK = {"until": 0.0, "message": None}
RETRYABLE = ("failed to extract any player response", "sign in to confirm", "not a bot",
             "http error 403", "http error 429", "unable to download api page", "player response")


def base_opts(fallback=False):
    opts = {"quiet": True, "no_warnings": True, "noprogress": True, "noplaylist": True,
            "socket_timeout": 30, "retries": 3, "extractor_retries": 2, "fragment_retries": 5,
            # Speed: YouTube throttles long single requests, so fetch in 10 MB ranges,
            # and grab fragmented streams 4 pieces at a time.
            "http_chunk_size": 10 * 1024 * 1024, "concurrent_fragment_downloads": 4}
    if COOKIES_FILE and os.path.exists(COOKIES_FILE):
        opts["cookiefile"] = COOKIES_FILE
    if PROXY:
        opts["proxy"] = PROXY
    youtube_args = {}
    if fallback and FALLBACK_CLIENTS:
        youtube_args["player_client"] = FALLBACK_CLIENTS
    if POT_URL:
        opts.setdefault("extractor_args", {})["youtubepot-bgutilhttp"] = {"base_url": [POT_URL]}
    if youtube_args:
        opts.setdefault("extractor_args", {})["youtube"] = youtube_args
    return opts


def retryable(err):
    low = str(err).lower()
    return any(k in low for k in RETRYABLE)


def valid_url(url):
    return bool(url) and bool(YT_REGEX.match(url.strip()))


def friendly_error(msg):
    low = msg.lower()
    if "failed to extract any player response" in low or "http error 429" in low or "too many requests" in low:
        return ("YouTube is blocking requests from this server right now. Wait a few minutes and try again. "
                "If it keeps happening, the site owner can add YouTube cookies or a proxy on Render.")
    if "your age" in low or "age-restricted" in low or "inappropriate for some users" in low:
        return "This video is age-restricted. Add YouTube cookies from a signed-in adult account to download it."
    if "requested format is not available" in low:
        return "That quality isn't available for this video. Pick another one."
    if "sign in to confirm" in low or "not a bot" in low:
        return ("YouTube blocked this server with a bot check. Add YouTube cookies in the "
                "YTDLP_COOKIES setting on Render, then try again.")
    if "private video" in low or "video is private" in low:
        return "This video is private, so it can't be downloaded."
    if "members-only" in low or "join this channel" in low:
        return "This video is for channel members only."
    if "http error 403" in low or "403: forbidden" in low:
        return "YouTube refused the download. Try again in a minute, or update yt-dlp by redeploying."
    if "no space left" in low:
        return "The server ran out of space. Try a lower quality or a shorter video."
    if "video unavailable" in low or "not available" in low:
        return "This video is unavailable. Check the link and try again."
    if "live event" in low or "is live" in low:
        return "Live streams can't be downloaded until they end."
    if "premieres in" in low:
        return "This video hasn't premiered yet."
    if "timed out" in low or "connection refused" in low or "failed to establish" in low or "connection reset" in low:
        return "The connection to YouTube dropped. Try again."
    return re.sub(r"^ERROR:\s*(\[\w+\]\s*\S+:\s*)?", "", msg)[:300]


# yt-dlp swaps characters Windows can't use for look-alike symbols (： ⧸ ？). Turn them into plain text.
_LOOKALIKES = {"：": " -", "⧸": "-", "⧹": "-", "？": "", "＊": "", "＂": "'", "＜": "", "＞": "", "｜": "-"}


def clean_filename(name):
    stem, ext = os.path.splitext(name)
    for a, b in _LOOKALIKES.items():
        stem = stem.replace(a, b)
    stem = re.sub(r"\s+", " ", stem).strip(" .-") or "download"
    return stem + ext


def video_label(fmt):
    """Label by the shorter side so vertical Shorts don't show as '1920p'."""
    w, h = fmt.get("width") or 0, fmt.get("height") or 0
    return min(w, h) if w and h else h


# ---------- pages ----------

@app.route("/")
def index():
    return render_template("index.html")


# ---------- PWA ----------

STATIC_DIR = os.path.join(app.root_path, "static")
FIND_YT = re.compile(r"https?://(?:www\.|m\.|music\.)?(?:youtube\.com|youtu\.be)/\S+", re.I)


@app.route("/sw.js")
def service_worker():
    # Served from the root so it can control the whole site; never cached so updates land fast.
    res = send_from_directory(STATIC_DIR, "sw.js", mimetype="text/javascript", max_age=0)
    res.headers["Cache-Control"] = "no-cache"
    res.headers["Service-Worker-Allowed"] = "/"
    return res


@app.route("/manifest.webmanifest")
def manifest():
    return send_from_directory(STATIC_DIR, "manifest.webmanifest", mimetype="application/manifest+json", max_age=3600)


@app.route("/favicon.ico")
def favicon():
    return send_from_directory(STATIC_DIR, "favicon.ico", max_age=86400)


@app.route("/apple-touch-icon.png")
@app.route("/apple-touch-icon-precomposed.png")
def apple_icon():
    return send_from_directory(os.path.join(STATIC_DIR, "icons"), "apple-touch-icon.png", max_age=86400)


@app.route("/share")
def share_target():
    """Android 'Share' from the YouTube app lands here. The link can arrive in url, text or title."""
    for key in ("url", "text", "title"):
        m = FIND_YT.search(request.args.get(key, "") or "")
        if m:
            return redirect(url_for("index", url=m.group(0).rstrip(".,)")))
    return redirect(url_for("index", shared="none"))


@app.route("/offline")
def offline():
    return render_template("offline.html")


@app.after_request
def compress(res):
    """Gzip HTML/JSON/JS so the page loads faster on mobile data."""
    if (res.direct_passthrough or res.status_code < 200 or res.status_code >= 300
            or "Content-Encoding" in res.headers
            or "gzip" not in (request.headers.get("Accept-Encoding") or "").lower()):
        return res
    ctype = res.mimetype or ""
    if not (ctype.startswith("text/") or ctype in ("application/json", "application/manifest+json")):
        return res
    data = res.get_data()
    if len(data) < 1024:
        return res
    res.set_data(gzip.compress(data, compresslevel=6))
    res.headers["Content-Encoding"] = "gzip"
    res.headers["Vary"] = "Accept-Encoding"
    return res


@app.after_request
def cache_headers(res):
    if request.path.startswith("/static/") and res.status_code == 200:
        res.headers["Cache-Control"] = "public, max-age=604800"
    if request.path == "/":
        res.headers["Cache-Control"] = "no-cache"
    return res


@app.route("/healthz")
def health():
    return "ok"


@app.route("/api/status")
def status():
    """Quick check that everything the downloader needs is installed."""
    try:
        import yt_dlp_ejs  # noqa: F401
        ejs = True
    except ImportError:
        ejs = False
    active = sum(1 for j in JOBS.values() if j["status"] in ACTIVE)
    pot = False
    if POT_URL:
        import urllib.request
        try:
            with urllib.request.urlopen(POT_URL.rstrip("/") + "/ping", timeout=3) as r:
                pot = r.status == 200
        except Exception:  # noqa: BLE001
            pot = False
    try:
        import yt_dlp_plugins.extractor.getpot_bgutil_http  # noqa: F401
        pot_plugin = True
    except Exception:  # noqa: BLE001
        pot_plugin = False
    return jsonify(
        yt_dlp=yt_dlp.version.__version__,
        po_token_provider=pot,
        po_token_plugin=pot_plugin,
        proxy=bool(PROXY),
        engine=ENGINE_MODE,
        engine_resting_seconds=max(0, int(ENGINE_STATE["blocked_until"] - time.time())),
        engine_last_ok=bool(ENGINE_STATE["last_ok"]),
        engine_last_error=ENGINE_STATE["last_error"],
        ytdlp_blocked_seconds=max(0, int(YTDLP_BLOCK["until"] - time.time())),
        last_get_video=LAST_INFO,
        fallback_clients=FALLBACK_CLIENTS,
        yt_dlp_ejs=ejs,
        deno=bool(shutil.which("deno")),
        ffmpeg=bool(shutil.which("ffmpeg")),
        cookies=bool(COOKIES_FILE),
        active_downloads=active,
        max_active_downloads=MAX_ACTIVE_JOBS,
    )


# ---------- info ----------

@app.route("/api/info", methods=["POST"])
def info():
    url = (request.get_json(silent=True) or {}).get("url", "").strip()
    if not valid_url(url):
        return jsonify(error="That doesn't look like a YouTube link. Paste a link from youtube.com or youtu.be."), 400
    started = time.time()
    own = engine_info(url)
    if own is not None:
        LAST_INFO.update(via="TubeGrab Engine", seconds=round(time.time() - started, 2))
        return jsonify(own)
    cached, _ = cache_get(url)
    if cached is None and time.time() < YTDLP_BLOCK["until"]:
        # Everything was blocked a moment ago: say so at once.
        return jsonify(error=YTDLP_BLOCK["message"], retry_after=int(YTDLP_BLOCK["until"] - time.time())), 503
    try:
        data = extract(url)
    except yt_dlp.utils.DownloadError as e:
        app.logger.warning("info failed for %s: %s", url, e)
        msg = friendly_error(str(e))
        if retryable(e):
            YTDLP_BLOCK.update(until=time.time() + BLOCK_MEMORY, message=msg)
        LAST_INFO.update(via="failed", seconds=round(time.time() - started, 2))
        return jsonify(error=msg), 400
    except Exception as e:  # noqa: BLE001
        return jsonify(error=f"Something went wrong while reading the video: {e}"), 500
    YTDLP_BLOCK["until"] = 0.0
    LAST_INFO.update(via="yt-dlp", seconds=round(time.time() - started, 2))

    out = build_info(data)
    out["engine"] = "yt-dlp"
    return jsonify(out)


LAST_INFO = {"via": None, "seconds": None}


def engine_ready():
    return ENGINE_MODE == "own" and time.time() >= ENGINE_STATE["blocked_until"]


def engine_info(url, use_cache=True):
    """Read the video with TubeGrab Engine. Returns page info, or None to let yt-dlp try."""
    vid = video_id_of(url)
    if not vid or not engine_ready():
        return None
    with INFO_LOCK:
        hit = ENGINE_CACHE.get(vid)
    if use_cache and hit and time.time() - hit[0] < INFO_TTL:
        return copy.deepcopy(hit[1])
    try:
        info, formats = engine.get_info(vid, MAX_DURATION)
    except engine.EngineError as e:
        ENGINE_STATE["last_error"] = str(e)
        if e.blocked:  # give the engine a rest instead of wasting seconds on every request
            ENGINE_STATE["blocked_until"] = time.time() + ENGINE_COOLDOWN
        app.logger.warning("TubeGrab Engine couldn't read %s (%s); using yt-dlp", vid, e)
        return None
    except Exception as e:  # noqa: BLE001 - never let the engine break the site
        ENGINE_STATE["last_error"] = repr(e)
        app.logger.exception("TubeGrab Engine crashed on %s; using yt-dlp", vid)
        return None
    ENGINE_STATE["last_ok"] = time.time()
    with INFO_LOCK:
        ENGINE_CACHE[vid] = (time.time(), info, formats)
        for k in sorted(ENGINE_CACHE, key=lambda k: ENGINE_CACHE[k][0])[:-40]:
            ENGINE_CACHE.pop(k, None)
    return copy.deepcopy(info)


VIDEO_ID_IN_URL = re.compile(r"(?:v=|youtu\.be/|shorts/|live/|embed/)([A-Za-z0-9_-]{11})")
INFO_CACHE = {}  # video id -> (time, info, used_fallback)
INFO_LOCK = threading.Lock()
# Big fields we never use; dropping them keeps the cache small on a 512 MB server.
_HEAVY = ("automatic_captions", "subtitles", "heatmap", "chapters", "storyboards", "comments", "description")


def video_id_of(url):
    m = VIDEO_ID_IN_URL.search(url or "")
    return m.group(1) if m else None


def cache_get(url):
    vid = video_id_of(url)
    with INFO_LOCK:
        hit = INFO_CACHE.get(vid) if vid else None
        if hit and time.time() - hit[0] < INFO_TTL:
            return copy.deepcopy(hit[1]), hit[2]
    return None, False


def cache_put(info, used_fallback):
    vid = info.get("id")
    if not vid:
        return
    slim = yt_dlp.YoutubeDL.sanitize_info({k: v for k, v in info.items() if k not in _HEAVY})
    with INFO_LOCK:
        INFO_CACHE[vid] = (time.time(), slim, used_fallback)
        if len(INFO_CACHE) > 40:  # drop the oldest
            for k, _ in sorted(INFO_CACHE.items(), key=lambda kv: kv[1][0])[: len(INFO_CACHE) - 40]:
                INFO_CACHE.pop(k, None)


def read_raw(ydl, url):
    """Ask YouTube for the video's details without choosing a format yet ("raw" info).
    Raw info can be reused later for any format: MP4 at any height, MP3 or M4A."""
    return ydl.extract_info(url, download=False, process=False)


def extract(url):
    """Video details for the page (cached for a few minutes). If YouTube rejects the
    normal clients, retry once with fallback clients."""
    raw, used_fallback = cache_get(url)
    if raw is None:
        used_fallback = False
        try:
            with yt_dlp.YoutubeDL({**base_opts(), "skip_download": True}) as ydl:
                raw = read_raw(ydl, url)
        except yt_dlp.utils.DownloadError as e:
            if not (retryable(e) and FALLBACK_CLIENTS):
                raise
            app.logger.warning("retrying %s with fallback clients after: %s", url, e)
            used_fallback = True
            with yt_dlp.YoutubeDL({**base_opts(fallback=True), "skip_download": True}) as ydl:
                raw = read_raw(ydl, url)
        cache_put(raw, used_fallback)
    # Fill in computed fields (sizes, best thumbnail). Local only: no network.
    try:
        with yt_dlp.YoutubeDL({**base_opts(used_fallback), "skip_download": True}) as ydl:
            return ydl.process_ie_result(copy.deepcopy(raw), download=False)
    except Exception:  # noqa: BLE001
        return raw


def build_info(data):
    duration = int(data.get("duration") or 0)
    formats = data.get("formats") or []

    best_audio = 0
    for f in formats:
        if f.get("vcodec") == "none" and f.get("acodec") not in (None, "none"):
            best_audio = max(best_audio, f.get("filesize") or f.get("filesize_approx") or 0)

    by_height = {}
    for f in formats:
        h = f.get("height")
        if not h or f.get("vcodec") in (None, "none") or h < 144:
            continue
        size = f.get("filesize") or f.get("filesize_approx") or 0
        if size and f.get("acodec") in (None, "none"):
            size += best_audio
        cur = by_height.get(h)
        if not cur or size > cur["size"]:
            by_height[h] = {"height": h, "label": video_label(f), "size": size, "fps": f.get("fps")}

    resolutions = sorted(by_height.values(), key=lambda r: r["height"], reverse=True)
    audio = [{"bitrate": b, "size": duration * int(b) * 125} for b in AUDIO_BITRATES]
    audio.append({"bitrate": "m4a", "size": best_audio or duration * 129 * 125})

    thumb = data.get("thumbnail")
    thumbs = [t for t in (data.get("thumbnails") or [])
              if t.get("url") and (t.get("width") or 0) >= 480 and ".webp" not in t["url"]]
    if thumbs:
        thumb = max(thumbs, key=lambda t: t.get("width") or 0)["url"]

    return {
        "id": data.get("id"),
        "title": data.get("title"),
        "channel": data.get("uploader") or data.get("channel"),
        "duration": duration,
        "views": data.get("view_count"),
        "likes": data.get("like_count"),
        "upload_date": data.get("upload_date"),  # YYYYMMDD
        "thumbnail": thumb,
        "resolutions": resolutions,
        "audio": audio,
        "too_long": bool(duration and duration > MAX_DURATION),
        "max_minutes": MAX_DURATION // 60,
    }


# ---------- thumbnail ----------

VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


@app.route("/api/thumbnail/<video_id>")
def thumbnail(video_id):
    """Fetch the best available thumbnail from YouTube and send it as a JPG download."""
    import urllib.request
    if not VIDEO_ID.match(video_id):
        abort(404)
    for name in ("maxresdefault", "sddefault", "hqdefault", "mqdefault"):
        try:
            req = urllib.request.Request(f"https://i.ytimg.com/vi/{video_id}/{name}.jpg",
                                         headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as r:
                data = r.read()
            # YouTube returns a tiny grey placeholder (120x90, ~1 KB) when a size is missing
            if len(data) > 2000:
                title = re.sub(r'[\\/:*?"<>|]+', "", request.args.get("title", "") or video_id)[:100].strip()
                return send_file(BytesIO(data), mimetype="image/jpeg", as_attachment=True,
                                 download_name=f"{title or video_id} thumbnail.jpg")
        except Exception:  # noqa: BLE001
            continue
    return jsonify(error="Couldn't get the thumbnail for this video."), 404


# ---------- jobs ----------

def new_job(url, kind, quality):
    job_id = uuid.uuid4().hex[:16]
    job = {
        "id": job_id, "url": url, "type": kind, "quality": quality,
        "status": "queued", "stage": "Starting", "percent": 0.0,
        "speed": None, "eta": None, "downloaded": 0, "total": 0,
        "filename": None, "size": 0, "error": None, "started": time.time(), "elapsed": None, "engine": "yt-dlp",
        "cancel": False, "created": time.time(),
        "dir": os.path.join(DOWNLOAD_ROOT, job_id),
    }
    os.makedirs(job["dir"])
    with JOBS_LOCK:
        JOBS[job_id] = job
    return job


def public(job):
    keys = ("id", "type", "quality", "status", "stage", "percent", "speed", "eta",
            "downloaded", "total", "filename", "size", "error", "elapsed", "engine")
    return {k: job[k] for k in keys}


def build_opts(job, fallback=False):
    opts = {
        **base_opts(fallback),
        "outtmpl": os.path.join(job["dir"], "%(title).120B.%(ext)s"),
        "windowsfilenames": True,  # file names that also work on Windows phones/PCs
    }
    if job["type"] == "audio" and job["quality"] == "m4a":
        # Fastest: keep YouTube's own AAC audio, no re-encoding.
        opts["format"] = "bestaudio[ext=m4a]/bestaudio[acodec^=mp4a]/bestaudio/best"
        opts["writethumbnail"] = True
        opts["postprocessors"] = [
            {"key": "FFmpegExtractAudio", "preferredcodec": "m4a"},  # copies when already AAC
            {"key": "FFmpegMetadata"},
            {"key": "FFmpegThumbnailsConvertor", "format": "jpg", "when": "before_dl"},
            {"key": "EmbedThumbnail"},
        ]
    elif job["type"] == "audio":
        bitrate = job["quality"] if job["quality"] in AUDIO_BITRATES else "192"
        opts["format"] = "bestaudio/best"
        opts["writethumbnail"] = True  # used as the MP3's cover art
        opts["postprocessors"] = [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": bitrate},
            {"key": "FFmpegMetadata"},
            {"key": "FFmpegThumbnailsConvertor", "format": "jpg", "when": "before_dl"},
            {"key": "EmbedThumbnail"},
        ]
        # LAME's faster encoding mode: quicker on Render's small CPU, same bitrate.
        opts["postprocessor_args"] = {"extractaudio": ["-compression_level", "7"]}
    else:
        h = int(job["quality"]) if str(job["quality"]).isdigit() else 100000
        # Prefer H.264 + AAC for maximum compatibility (phones, Windows, editors), then anything.
        opts["format"] = (
            f"bv*[height<={h}][vcodec^=avc1]+ba[ext=m4a]/"
            f"bv*[height<={h}][ext=mp4]+ba[ext=m4a]/"
            f"bv*[height<={h}]+ba/"
            f"b[height<={h}]/b"
        )
        opts["merge_output_format"] = "mp4"
        opts["postprocessors"] = [{"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"}]
    return opts


def run_engine(job):
    """Try the job with TubeGrab Engine. True = file is ready; False = let yt-dlp do it."""
    vid = video_id_of(job["url"])
    if not vid or not engine_ready():
        return False
    with INFO_LOCK:
        hit = ENGINE_CACHE.get(vid)
    if not hit or time.time() - hit[0] >= INFO_TTL:
        if engine_info(job["url"], use_cache=False) is None:
            return False
        with INFO_LOCK:
            hit = ENGINE_CACHE.get(vid)
    _, info, formats = hit
    if info.get("too_long"):
        raise yt_dlp.utils.DownloadError(f"Videos longer than {MAX_DURATION // 60} minutes are turned off on this site.")

    def report(done, total, speed, eta):
        job.update(status="downloading", percent=round(min(done / total * 92, 92), 1) if total else 0,
                   downloaded=done, total=total, speed=speed, eta=eta)

    def set_stage(status, stage):
        job.update(status=status, stage=stage, **({"speed": None, "eta": None,
                                                     "percent": max(job["percent"], 94.0)} if status == "processing" else {}))

    try:
        engine.download(info, copy.deepcopy(formats), job["type"], job["quality"], job["dir"],
                        report, set_stage, lambda: job["cancel"])
        job["engine"] = "TubeGrab Engine"
        return True
    except engine.Cancelled:
        raise Cancelled()
    except Exception as e:  # noqa: BLE001
        if job["cancel"]:
            raise Cancelled()
        ENGINE_STATE["last_error"] = str(e)
        if isinstance(e, engine.EngineError) and e.blocked:
            ENGINE_STATE["blocked_until"] = time.time() + ENGINE_COOLDOWN
        with INFO_LOCK:
            ENGINE_CACHE.pop(vid, None)
        app.logger.warning("TubeGrab Engine download failed for job %s (%s); using yt-dlp", job["id"], e)
        for f in os.listdir(job["dir"]):
            os.remove(os.path.join(job["dir"], f))
        job.update(status="preparing", stage="Getting the video ready", percent=0.0, downloaded=0, total=0)
        return False


def run_job(job):
    state = {"done_files": 0, "n_files": 1}

    def progress_hook(d):
        if job["cancel"]:
            raise Cancelled()
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            got = d.get("downloaded_bytes") or 0
            if total:
                frac = min(got / total, 1)
            elif d.get("fragment_count"):  # streams sent in pieces have no total size
                frac = (d.get("fragment_index") or 0) / d["fragment_count"]
            else:
                frac = 0
            overall = (state["done_files"] + frac) / state["n_files"]
            fmt = d.get("info_dict") or {}
            is_audio = fmt.get("vcodec") == "none"
            job.update(
                status="downloading",
                stage="Downloading audio" if is_audio else "Downloading video",
                percent=round(min(overall * 92, 92), 1),  # last 8% is reserved for processing
                speed=d.get("speed"), eta=d.get("eta"),
                downloaded=got, total=total,
            )
        elif d["status"] == "finished":
            state["done_files"] += 1
            job["percent"] = round(min(state["done_files"] / state["n_files"] * 92, 92), 1)

    def pp_hook(d):
        if job["cancel"]:
            raise Cancelled()
        if d["status"] == "started":
            name = d.get("postprocessor", "")
            stage = {
                "Merger": "Joining video and audio",
                "ExtractAudio": "Finishing M4A" if job["quality"] == "m4a" else "Converting to MP3",
                "VideoRemuxer": "Finishing MP4",
                "Metadata": "Adding song info",
                "EmbedThumbnail": "Adding cover art",
            }.get(name)
            if stage:
                job.update(status="processing", stage=stage, speed=None, eta=None,
                           percent=max(job["percent"], 94.0))

    def attempt(fallback, info=None):
        opts = build_opts(job, fallback)
        opts["progress_hooks"] = [progress_hook]
        opts["postprocessor_hooks"] = [pp_hook]
        state["done_files"] = 0
        with yt_dlp.YoutubeDL(opts) as ydl:
            if info is None:
                info = read_raw(ydl, job["url"])
                cache_put(info, fallback)
            # else: reuse what "Get video" already read, so no second trip to YouTube.
            probe = ydl.process_ie_result(copy.deepcopy(info), download=False)  # picks the format, no network
            if probe.get("is_live"):
                raise yt_dlp.utils.DownloadError("is live")
            if (probe.get("duration") or 0) > MAX_DURATION:
                raise yt_dlp.utils.DownloadError(
                    f"Videos longer than {MAX_DURATION // 60} minutes are turned off on this site.")
            state["n_files"] = len(probe.get("requested_formats") or []) or 1
            ydl.process_ie_result(info, download=True)

    try:
        job.update(status="preparing", stage="Getting the video ready")
        if run_engine(job):
            cached, cached_fallback = "used", False
        else:
            cached, cached_fallback = cache_get(job["url"])
        if cached is not None and cached != "used":
            try:
                attempt(cached_fallback, info=cached)
                cached = "used"
            except yt_dlp.utils.DownloadError as e:
                if job["cancel"]:
                    raise
                app.logger.info("cached info failed for job %s, reading again: %s", job["id"], e)
                for f in os.listdir(job["dir"]):
                    os.remove(os.path.join(job["dir"], f))
                cached = None
        try:
            if cached != "used":
                attempt(fallback=cached_fallback)
        except yt_dlp.utils.DownloadError as e:
            if job["cancel"] or not (retryable(e) and FALLBACK_CLIENTS):
                raise
            app.logger.warning("job %s retrying with fallback clients after: %s", job["id"], e)
            for f in os.listdir(job["dir"]):  # clear partial files from the first try
                os.remove(os.path.join(job["dir"], f))
            job.update(status="preparing", stage="Trying another way", percent=0.0)
            attempt(fallback=True)

        want = (".m4a" if job["quality"] == "m4a" else ".mp3") if job["type"] == "audio" else ".mp4"
        files = [f for f in os.listdir(job["dir"]) if f.lower().endswith(want)]
        if not files:  # fall back to whatever media file exists
            files = [f for f in os.listdir(job["dir"])
                     if not f.endswith((".part", ".ytdl", ".temp", ".webp", ".jpg", ".png"))]
        files = sorted(files, key=lambda f: os.path.getsize(os.path.join(job["dir"], f)), reverse=True)
        if not files:
            raise RuntimeError("The download finished but no file was created.")
        nice = clean_filename(files[0])
        path = os.path.join(job["dir"], nice)
        os.replace(os.path.join(job["dir"], files[0]), path)
        job.update(status="done", stage="Ready", percent=100.0, filename=nice,
                   elapsed=round(time.time() - job["started"], 1),
                   size=os.path.getsize(path), speed=None, eta=None)
    except Cancelled:
        job.update(status="cancelled", stage="Cancelled", speed=None, eta=None)
        shutil.rmtree(job["dir"], ignore_errors=True)
    except yt_dlp.utils.DownloadError as e:
        if job["cancel"]:
            job.update(status="cancelled", stage="Cancelled")
        else:
            app.logger.warning("job %s failed: %s", job["id"], e)
            job.update(status="error", stage="Failed", error=friendly_error(str(e)))
        shutil.rmtree(job["dir"], ignore_errors=True)
    except Exception as e:  # noqa: BLE001
        job.update(status="error", stage="Failed", error=f"Something went wrong: {e}")
        shutil.rmtree(job["dir"], ignore_errors=True)


@app.route("/api/jobs", methods=["POST"])
def create_job():
    body = request.get_json(silent=True) or {}
    url = (body.get("url") or "").strip()
    kind = body.get("type")
    quality = str(body.get("quality") or "")
    if not valid_url(url):
        return jsonify(error="That doesn't look like a YouTube link."), 400
    if kind not in ("video", "audio"):
        return jsonify(error="Choose Video or Audio."), 400
    if sum(1 for j in JOBS.values() if j["status"] in ACTIVE) >= MAX_ACTIVE_JOBS:
        return jsonify(error="The server is busy with other downloads. Try again in a minute."), 429
    job = new_job(url, kind, quality)
    threading.Thread(target=run_job, args=(job,), daemon=True).start()
    return jsonify(public(job)), 202


@app.route("/api/jobs/<job_id>")
def job_status(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify(error="This download has expired. Start it again."), 404
    return jsonify(public(job))


@app.route("/api/jobs/<job_id>/cancel", methods=["POST"])
def job_cancel(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify(error="Not found"), 404
    job["cancel"] = True
    return jsonify(public(job))


@app.route("/api/jobs/<job_id>/file")
def job_file(job_id):
    job = JOBS.get(job_id)
    if not job or job["status"] != "done":
        abort(404)
    path = os.path.join(job["dir"], job["filename"])
    if not os.path.exists(path):
        abort(404)
    return send_file(path, as_attachment=True, download_name=job["filename"])


# ---------- housekeeping ----------

def janitor():
    while True:
        time.sleep(120)
        now = time.time()
        with JOBS_LOCK:
            expired = [j for j in JOBS.values() if now - j["created"] > JOB_TTL]
            for j in expired:
                j["cancel"] = True
                JOBS.pop(j["id"], None)
        for j in expired:
            shutil.rmtree(j["dir"], ignore_errors=True)
        live = {j["dir"] for j in JOBS.values()}
        for name in os.listdir(DOWNLOAD_ROOT):
            p = os.path.join(DOWNLOAD_ROOT, name)
            if os.path.isdir(p) and p not in live and now - os.path.getmtime(p) > JOB_TTL:
                shutil.rmtree(p, ignore_errors=True)


threading.Thread(target=janitor, daemon=True).start()


def warm_up():
    """At start: get the visitor ID ready and check whether YouTube accepts the engine."""
    if os.environ.get("WARMUP", "1") != "1" or ENGINE_MODE != "own":
        return
    time.sleep(2)
    try:
        engine.get_info(os.environ.get("WARMUP_VIDEO", "jNQXAC9IVRw"), MAX_DURATION)
        ENGINE_STATE["last_ok"] = time.time()
        app.logger.info("warm-up: TubeGrab Engine works")
    except engine.EngineError as e:
        ENGINE_STATE["last_error"] = str(e)
        if e.blocked:
            ENGINE_STATE["blocked_until"] = time.time() + ENGINE_COOLDOWN
        app.logger.warning("warm-up: TubeGrab Engine not available yet (%s)", e)
    except Exception as e:  # noqa: BLE001
        app.logger.warning("warm-up failed: %r", e)


threading.Thread(target=warm_up, daemon=True).start()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False, threaded=True)
