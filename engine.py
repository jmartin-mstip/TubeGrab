"""TubeGrab Engine: TubeGrab's own YouTube reader and downloader. Made by Jhames Martin.

How it works
1. Gets a visitor ID from youtube.com (cached for an hour) so requests look like a normal visit.
2. Asks YouTube's player API for the video using the visionOS app client. That client
   returns direct stream links with no JavaScript puzzle and no PO token.
3. Picks the right streams (H.264 MP4 when available, best audio).
4. Downloads video and audio at the same time, each in 10 MB pieces over 4 connections.
5. Joins, converts or tags the result with ffmpeg.

Anything that goes wrong raises EngineError; app.py then falls back to yt-dlp.
"""
import json
import os
import re
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests

YT_BASE = os.environ.get("TUBEGRAB_YT_BASE", "https://www.youtube.com").rstrip("/")
THUMB_BASE = os.environ.get("TUBEGRAB_THUMB_BASE", "https://i.ytimg.com").rstrip("/")
PROXY = os.environ.get("YTDLP_PROXY", "").strip()

CHUNK = 10 * 1024 * 1024        # bytes per request; YouTube slows down long single requests
WORKERS_PER_FILE = 4            # connections per stream
TIMEOUT = 30

# The visionOS app client (same values yt-dlp uses): direct links, no JS, no PO token.
CLIENT = {
    "clientName": "VISIONOS",
    "clientVersion": "1.02",
    "deviceMake": "Apple",
    "deviceModel": "RealityDevice17,1",
    "userAgent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 15_7_3) AppleWebKit/605.1.15 "
                  "(KHTML, like Gecko) Version/26.0 Safari/605.1.15"),
    "osName": "visionOS",
    "osVersion": "26.5.23O471",
}
CLIENT_ID = "101"
CLIENT_NAME = os.environ.get("TUBEGRAB_CLIENT_NAME", CLIENT["clientName"])
CLIENT_VERSION = os.environ.get("TUBEGRAB_CLIENT_VERSION", CLIENT["clientVersion"])


class EngineError(Exception):
    """Something the engine couldn't do. `blocked` means YouTube refused this server."""

    def __init__(self, message, blocked=False):
        super().__init__(message)
        self.blocked = blocked


class Cancelled(Exception):
    pass


def _session():
    s = requests.Session()
    s.headers.update({"User-Agent": CLIENT["userAgent"], "Accept-Language": "en-US,en;q=0.9"})
    s.cookies.set("SOCS", "CAI", domain=".youtube.com")  # skip the EU consent page
    if PROXY:
        s.proxies.update({"http": PROXY, "https": PROXY})
    return s


_visitor = {"value": None, "at": 0.0}
_visitor_lock = threading.Lock()


def visitor_data(session):
    with _visitor_lock:
        if _visitor["value"] and time.time() - _visitor["at"] < 3600:
            return _visitor["value"]
    try:
        r = session.get(f"{YT_BASE}/?hl=en", timeout=TIMEOUT)
        m = re.search(r'"(?:VISITOR_DATA|visitorData)"\s*:\s*"([^"]+)"', r.text)
        value = m.group(1) if m else None
    except requests.RequestException:
        value = None
    with _visitor_lock:
        _visitor.update(value=value, at=time.time())
    return value


# ---------------------------------------------------------------- reading a video

def player(video_id):
    """Ask YouTube's player API about one video. Returns the raw JSON."""
    s = _session()
    vd = visitor_data(s)
    headers = {
        "Content-Type": "application/json",
        "Origin": "https://www.youtube.com",
        "X-YouTube-Client-Name": CLIENT_ID,
        "X-YouTube-Client-Version": CLIENT_VERSION,
    }
    if vd:
        headers["X-Goog-Visitor-Id"] = vd
    body = {
        "context": {"client": {**CLIENT, "clientName": CLIENT_NAME, "clientVersion": CLIENT_VERSION,
                               "hl": "en", "gl": "US", "timeZone": "UTC", "utcOffsetMinutes": 0,
                               **({"visitorData": vd} if vd else {})}},
        "videoId": video_id,
        "playbackContext": {"contentPlaybackContext": {"html5Preference": "HTML5_PREF_WANTS"}},
        "contentCheckOk": True,
        "racyCheckOk": True,
    }
    try:
        r = s.post(f"{YT_BASE}/youtubei/v1/player?prettyPrint=false", json=body, headers=headers, timeout=TIMEOUT)
    except requests.RequestException as e:
        raise EngineError(f"Couldn't reach YouTube: {e}") from e
    if r.status_code in (403, 429):
        raise EngineError(f"YouTube answered HTTP {r.status_code}", blocked=True)
    if r.status_code != 200:
        raise EngineError(f"YouTube answered HTTP {r.status_code}")
    try:
        data = r.json()
    except ValueError as e:
        raise EngineError("YouTube sent something unexpected") from e

    ps = data.get("playabilityStatus") or {}
    status = ps.get("status")
    if status != "OK":
        reason = ps.get("reason") or " ".join(
            t.get("text", "") for t in ((ps.get("errorScreen") or {}).get("playerErrorMessageRenderer", {})
                                        .get("subreason", {}).get("runs") or [])) or status or "unknown"
        blocked = status == "LOGIN_REQUIRED" and "bot" in reason.lower()
        raise EngineError(f"{status}: {reason}", blocked=blocked)
    if not (data.get("streamingData") or {}).get("adaptiveFormats") and not (data.get("streamingData") or {}).get("formats"):
        raise EngineError("YouTube returned no streams")
    return data


_MIME = re.compile(r'^(?P<type>video|audio)/(?P<ext>[\w-]+);\s*codecs="(?P<codecs>[^"]+)"')


def parse_formats(data):
    """Turn YouTube's stream list into simple dicts. Skips anything that needs JavaScript."""
    sd = data.get("streamingData") or {}
    out = []
    for f in (sd.get("adaptiveFormats") or []) + (sd.get("formats") or []):
        url = f.get("url")
        if not url:  # signatureCipher streams need the JS player, which the engine doesn't run
            continue
        m = _MIME.match(f.get("mimeType") or "")
        if not m:
            continue
        track = f.get("audioTrack") or {}
        if track and not track.get("audioIsDefault", True):  # dubbed/alternate language tracks
            continue
        if "drc" in (f.get("xtags") or "") or f.get("isDrc"):  # volume-normalised duplicates
            continue
        codecs = m["codecs"]
        progressive = "," in codecs
        out.append({
            "itag": f.get("itag"),
            "url": url,
            "kind": "both" if progressive else m["type"],
            "ext": m["ext"],
            "vcodec": codecs.split(",")[0].strip() if m["type"] == "video" else None,
            "acodec": (codecs.split(",")[-1].strip() if progressive else codecs) if (m["type"] == "audio" or progressive) else None,
            "height": f.get("height"),
            "width": f.get("width"),
            "fps": f.get("fps"),
            "bitrate": f.get("bitrate") or f.get("averageBitrate") or 0,
            "size": int(f["contentLength"]) if str(f.get("contentLength", "")).isdigit() else 0,
        })
    return out


def get_info(video_id, max_duration):
    """Video details in the same shape the page expects, plus the stream list for downloading."""
    data = player(video_id)
    vd = data.get("videoDetails") or {}
    mf = (data.get("microformat") or {}).get("playerMicroformatRenderer") or {}
    formats = parse_formats(data)
    if not formats:
        raise EngineError("No direct streams for this video")
    duration = int(vd.get("lengthSeconds") or 0)
    if vd.get("isLive") or vd.get("isLiveContent") and not duration:
        raise EngineError("is live")

    audio = [f for f in formats if f["kind"] == "audio"]
    best_audio = max(audio, key=lambda f: f["bitrate"], default=None)
    best_m4a = max((f for f in audio if f["ext"] == "mp4"), key=lambda f: f["bitrate"], default=best_audio)
    by_height = {}
    for f in formats:
        if f["kind"] == "audio" or not f["height"]:
            continue
        h = f["height"]
        size = f["size"] + (best_m4a["size"] if (f["kind"] == "video" and best_m4a) else 0)
        label = min(h, f["width"] or h)
        cur = by_height.get(h)
        if not cur or (f["fps"] or 0) > (cur["fps"] or 0) or size > cur["size"]:
            by_height[h] = {"height": h, "label": label, "size": size if f["size"] else 0, "fps": f["fps"]}

    thumbs = (vd.get("thumbnail") or {}).get("thumbnails") or []
    thumb = max(thumbs, key=lambda t: t.get("width") or 0)["url"] if thumbs else f"{THUMB_BASE}/vi/{video_id}/hqdefault.jpg"
    upload = (mf.get("uploadDate") or mf.get("publishDate") or "")[:10].replace("-", "")

    info = {
        "id": video_id,
        "title": vd.get("title"),
        "channel": vd.get("author"),
        "duration": duration,
        "views": int(vd["viewCount"]) if str(vd.get("viewCount", "")).isdigit() else None,
        "likes": None,
        "upload_date": upload if len(upload) == 8 else None,
        "thumbnail": thumb,
        "resolutions": sorted(by_height.values(), key=lambda r: r["height"], reverse=True),
        "audio": [{"bitrate": b, "size": duration * int(b) * 125} for b in ("320", "192", "128")]
                 + [{"bitrate": "m4a", "size": (best_m4a or {}).get("size") or duration * 129 * 125}],
        "too_long": bool(duration and duration > max_duration),
        "max_minutes": max_duration // 60,
        "engine": "TubeGrab Engine",
    }
    return info, formats


# ---------------------------------------------------------------- choosing streams

def pick_video(formats, height):
    vids = [f for f in formats if f["kind"] == "video" and f["height"] and f["height"] <= height]
    if not vids:
        vids = [f for f in formats if f["kind"] == "video" and f["height"]]
    if not vids:
        return None
    top = max(f["height"] for f in vids)
    same = [f for f in vids if f["height"] == top]
    # H.264 first (plays everywhere), then higher fps, then bitrate.
    return max(same, key=lambda f: ((f["vcodec"] or "").startswith("avc1"), f["fps"] or 0, f["bitrate"]))


def pick_audio(formats, prefer_m4a=True):
    auds = [f for f in formats if f["kind"] == "audio"]
    if not auds:
        return None
    if prefer_m4a:
        m4a = [f for f in auds if f["ext"] == "mp4"]
        if m4a:
            return max(m4a, key=lambda f: f["bitrate"])
    return max(auds, key=lambda f: f["bitrate"])


# ---------------------------------------------------------------- downloading

class Progress:
    """Adds up bytes from several streams downloading at once and reports speed and time left."""

    def __init__(self, total, report, should_stop):
        self.total, self.done = total, 0
        self.report, self.should_stop = report, should_stop
        self.lock = threading.Lock()
        self.started = time.time()
        self.last = 0.0

    def add(self, n):
        if self.should_stop():
            raise Cancelled()
        with self.lock:
            self.done += n
            now = time.time()
            if now - self.last < 0.25 and self.done < self.total:
                return
            self.last = now
            elapsed = max(now - self.started, 0.001)
            speed = self.done / elapsed
            eta = int((self.total - self.done) / speed) if speed and self.total else None
        self.report(self.done, self.total, speed, eta)


def _content_length(session, url):
    r = session.get(url, headers={"Range": "bytes=0-0"}, timeout=TIMEOUT, stream=True)
    r.close()
    if r.status_code in (403, 429):
        raise EngineError(f"YouTube refused the stream (HTTP {r.status_code})", blocked=True)
    m = re.search(r"/(\d+)$", r.headers.get("Content-Range", ""))
    return int(m.group(1)) if m else int(r.headers.get("Content-Length") or 0)


def download_stream(fmt, path, progress):
    """Download one stream in CHUNK-sized ranges over several connections."""
    s = _session()
    size = fmt["size"] or _content_length(s, fmt["url"])
    if not size:
        raise EngineError("Unknown stream size")
    with open(path, "wb") as f:
        f.truncate(size)
    ranges = [(start, min(start + CHUNK, size) - 1) for start in range(0, size, CHUNK)]

    def fetch(rng):
        start, end = rng
        for attempt in range(4):
            try:
                with s.get(fmt["url"], headers={"Range": f"bytes={start}-{end}"}, timeout=TIMEOUT, stream=True) as r:
                    if r.status_code in (403, 429):
                        raise EngineError(f"YouTube refused the stream (HTTP {r.status_code})", blocked=True)
                    if r.status_code not in (200, 206):
                        raise EngineError(f"Stream answered HTTP {r.status_code}")
                    pos = start
                    with open(path, "r+b") as out:
                        out.seek(start)
                        for block in r.iter_content(256 * 1024):
                            if not block:
                                continue
                            if r.status_code == 200 and pos + len(block) > end + 1:  # server ignored Range
                                block = block[: end + 1 - pos]
                            out.write(block)
                            pos += len(block)
                            progress.add(len(block))
                            if pos > end:
                                break
                    if pos <= end:
                        raise EngineError("Connection closed early")
                    return
            except (requests.RequestException, EngineError) as e:
                if isinstance(e, EngineError) and e.blocked or attempt == 3:
                    raise e if isinstance(e, EngineError) else EngineError(f"Download failed: {e}")
                time.sleep(1 + attempt)

    with ThreadPoolExecutor(WORKERS_PER_FILE) as pool:
        for fut in [pool.submit(fetch, r) for r in ranges]:
            fut.result()
    return path


def fetch_thumbnail(video_id, folder, fallback_url=None):
    s = _session()
    for name in ("maxresdefault", "sddefault", "hqdefault"):
        try:
            r = s.get(f"{THUMB_BASE}/vi/{video_id}/{name}.jpg", timeout=10)
            if r.status_code == 200 and len(r.content) > 2000:
                p = os.path.join(folder, "cover.jpg")
                with open(p, "wb") as f:
                    f.write(r.content)
                return p
        except requests.RequestException:
            continue
    if fallback_url:
        try:
            r = s.get(fallback_url, timeout=10)
            if r.status_code == 200 and len(r.content) > 2000:
                p = os.path.join(folder, "cover.jpg")
                with open(p, "wb") as f:
                    f.write(r.content)
                return p
        except requests.RequestException:
            pass
    return None


def _ffmpeg(args):
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise EngineError(f"ffmpeg failed: {r.stderr.strip()[-300:]}")


def safe_name(title):
    name = (title or "download").replace(":", " -").replace("/", "-").replace("\\", "-").replace("|", "-")
    name = re.sub(r'[*?"<>\x00-\x1f]+', "", name)
    name = re.sub(r"\s+", " ", name).strip(" .-")[:120]
    return name or "download"


def download(info, formats, kind, quality, folder, report, set_stage, should_stop):
    """Download one job. Returns the finished file's path."""
    title = info.get("title") or info["id"]
    meta = ["-metadata", f"title={title}", "-metadata", f"artist={info.get('channel') or ''}"]

    if kind == "video":
        h = int(quality) if str(quality).isdigit() else 100000
        v = pick_video(formats, h)
        if not v:
            raise EngineError("No video stream")
        a = pick_audio(formats, prefer_m4a=True) if v["kind"] == "video" else None
        jobs = [(v, os.path.join(folder, "v.part"))] + ([(a, os.path.join(folder, "a.part"))] if a else [])
    else:
        a = pick_audio(formats, prefer_m4a=(quality == "m4a"))
        if not a:
            raise EngineError("No audio stream")
        jobs = [(a, os.path.join(folder, "a.part"))]

    sizes = []
    s = _session()
    for f, _ in jobs:
        sizes.append(f["size"] or _content_length(s, f["url"]))
        f["size"] = sizes[-1]
    progress = Progress(sum(sizes), report, should_stop)
    set_stage("downloading", "Downloading video and audio" if len(jobs) == 2 else
              "Downloading audio" if kind == "audio" else "Downloading video")

    # Video and audio at the same time.
    with ThreadPoolExecutor(len(jobs)) as pool:
        for fut in [pool.submit(download_stream, f, p, progress) for f, p in jobs]:
            fut.result()
    if should_stop():
        raise Cancelled()

    if kind == "video":
        out = os.path.join(folder, safe_name(title) + ".mp4")
        if len(jobs) == 2:
            set_stage("processing", "Joining video and audio")
            _ffmpeg(["-i", jobs[0][1], "-i", jobs[1][1], "-map", "0:v:0", "-map", "1:a:0",
                     "-c", "copy", "-movflags", "+faststart", *meta, out])
        else:
            set_stage("processing", "Finishing MP4")
            _ffmpeg(["-i", jobs[0][1], "-c", "copy", "-movflags", "+faststart", *meta, out])
    else:
        set_stage("processing", "Adding cover art")
        cover = fetch_thumbnail(info["id"], folder, info.get("thumbnail"))
        src = jobs[0][1]
        if quality == "m4a":
            out = os.path.join(folder, safe_name(title) + ".m4a")
            set_stage("processing", "Finishing M4A")
            acodec = ["-c:a", "copy"] if (a["acodec"] or "").startswith("mp4a") else ["-c:a", "aac", "-b:a", "192k"]
            if cover:
                _ffmpeg(["-i", src, "-i", cover, "-map", "0:a:0", "-map", "1:v:0", *acodec, "-c:v", "mjpeg",
                         "-disposition:v:0", "attached_pic", *meta, out])
            else:
                _ffmpeg(["-i", src, "-map", "0:a:0", *acodec, *meta, out])
        else:
            br = quality if quality in ("320", "192", "128") else "192"
            out = os.path.join(folder, safe_name(title) + ".mp3")
            set_stage("processing", "Converting to MP3")
            base = ["-i", src] + (["-i", cover] if cover else [])
            maps = ["-map", "0:a:0"] + (["-map", "1:v:0", "-c:v", "mjpeg", "-disposition:v:0", "attached_pic",
                                         "-metadata:s:v", "title=Album cover"] if cover else [])
            _ffmpeg([*base, *maps, "-c:a", "libmp3lame", "-b:a", f"{br}k", "-compression_level", "7",
                     "-id3v2_version", "3", *meta, out])

    for _, p in jobs:
        try:
            os.remove(p)
        except OSError:
            pass
    for extra in ("cover.jpg",):
        try:
            os.remove(os.path.join(folder, extra))
        except OSError:
            pass
    return out


def available():
    return bool(shutil.which("ffmpeg"))


if __name__ == "__main__":  # quick manual check: python engine.py VIDEO_ID
    import sys
    i, fm = get_info(sys.argv[1], 36000)
    print(json.dumps({k: i[k] for k in ("title", "channel", "duration")}, indent=1), len(fm), "streams")
