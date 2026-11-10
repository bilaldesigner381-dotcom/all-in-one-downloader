import copy
import hashlib
import hmac
import html as _html
import sqlite3
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections import defaultdict, deque
from urllib.parse import urlparse

import yt_dlp
from flask import Flask, Response, jsonify, render_template, request, send_file, after_this_request
from yt_dlp.utils import download_range_func

app = Flask(__name__)
# Behind Caddy/nginx the real visitor IP arrives in X-Forwarded-For. Without this every visitor looks like the proxy.
from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

SITE_NAME = os.environ.get("SITE_NAME", "ClipNest")
COOKIES_FILE = os.environ.get("COOKIES_FILE")   # optional: Netscape cookies.txt (helps with Instagram/Facebook)

# Limits can be changed from .env, e.g. MAX_FULL_SECONDS='300'
MAX_CLIP_SECONDS = int(os.environ.get("MAX_CLIP_SECONDS", "120"))     # longest clip in "Clip" mode
MAX_SHORT_SECONDS = int(os.environ.get("MAX_SHORT_SECONDS", "60"))    # longest Short in "Make a Short" mode
MAX_FULL_SECONDS = int(os.environ.get("MAX_FULL_SECONDS", "1800"))    # longest video in "Full video" mode (30 min)
RATE_LIMIT = 6                  # requests per window per IP
RATE_WINDOW = 60                # seconds
QUALITIES = {"best", "2160", "1440", "1080", "720", "480", "360", "240", "144", "mp3"}

# Only these sites are accepted. To add a platform, add its hostnames here.
# (A whitelist also stops people from pointing the server at internal/private addresses.)
PLATFORMS = {
    "YouTube": {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"},
    "Facebook": {"facebook.com", "www.facebook.com", "m.facebook.com", "web.facebook.com", "fb.watch"},
    "Instagram": {"instagram.com", "www.instagram.com"},
    "TikTok": {"tiktok.com", "www.tiktok.com", "m.tiktok.com", "vm.tiktok.com", "vt.tiktok.com"},
}

_hits = defaultdict(deque)

# ---------------------------------------------------------------- tracking (SQLite, no cookies)
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
DB_PATH = os.path.join(DATA_DIR, "events.db")
IP_SALT = os.environ.get("IP_SALT", "change-me")
GA_ID = os.environ.get("GA_ID", "").strip()
if not re.fullmatch(r"[A-Za-z0-9-]{4,20}", GA_ID):
    GA_ID = ""
os.makedirs(DATA_DIR, exist_ok=True)


def _db():
    return sqlite3.connect(DB_PATH, timeout=10)


with _db() as _c:
    _c.execute("""CREATE TABLE IF NOT EXISTS events(
        id INTEGER PRIMARY KEY, ts INTEGER, kind TEXT, platform TEXT, mode TEXT, quality TEXT,
        ok INTEGER, ms INTEGER, ip TEXT, ua TEXT, ref TEXT, extra TEXT)""")
    _c.execute("CREATE INDEX IF NOT EXISTS ix_events_ts ON events(ts)")


def log_event(kind, platform=None, mode=None, quality=None, ok=None, ms=None, extra=None):
    """Never let tracking break the site: all errors are swallowed."""
    try:
        ip = hashlib.sha256((IP_SALT + (request.remote_addr or "")).encode()).hexdigest()[:12]
        ref = urlparse(request.headers.get("Referer", "")).hostname
        with _db() as c:
            c.execute("INSERT INTO events(ts,kind,platform,mode,quality,ok,ms,ip,ua,ref,extra) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                      (int(time.time()), kind, platform, mode, quality, None if ok is None else int(bool(ok)), ms, ip,
                       request.headers.get("User-Agent", "")[:120], ref, None if extra is None else str(extra)[:80]))
    except Exception as e:
        print("TRACKING ERROR:", e)


def rate_limited(ip):
    now = time.time()
    q = _hits[ip]
    while q and now - q[0] > RATE_WINDOW:
        q.popleft()
    if len(q) >= RATE_LIMIT:
        return True
    q.append(now)
    return False


def detect_platform(url):
    try:
        u = urlparse(url)
        if u.scheme not in ("http", "https"):
            return None
        host = (u.hostname or "").lower()
    except Exception:
        return None
    for name, hosts in PLATFORMS.items():
        if host in hosts:
            return name
    return None


def parse_time(value):
    """Accepts 90, '90', '1:30' or '0:01:30' and returns seconds."""
    value = str(value).strip()
    if re.fullmatch(r"\d+(\.\d+)?", value):
        return float(value)
    parts = value.split(":")
    if not 2 <= len(parts) <= 3 or not all(p.isdigit() for p in parts):
        raise ValueError("Time format galat hai. 90 ya 1:30 likhein.")
    secs = 0
    for p in parts:
        secs = secs * 60 + int(p)
    return float(secs)


# Optional partner/CPA link. Opens in a NEW TAB when the visitor presses the download button.
CPA_URL = os.environ.get("CPA_URL", "").strip()
if not CPA_URL.lower().startswith(("http://", "https://")):
    CPA_URL = ""
CPA_CAP_MINUTES = int(os.environ.get("CPA_CAP_MINUTES", "10"))   # show at most once per N minutes per visitor (0 = every click)

FAST_CUT = os.environ.get("FAST_CUT") == "1"   # 1 = faster clips, but a clip may start up to a few seconds early
COOKIES_BROWSER = os.environ.get("COOKIES_BROWSER")   # optional, local use only: chrome / edge / firefox


def base_opts():
    o = {"quiet": True, "noplaylist": True, "concurrent_fragment_downloads": 8}
    if COOKIES_FILE and os.path.exists(COOKIES_FILE):
        o["cookiefile"] = COOKIES_FILE
    elif COOKIES_BROWSER:
        o["cookiesfrombrowser"] = (COOKIES_BROWSER,)
    return o


def explain_error(e):
    """Turn a yt-dlp error into a short, useful message."""
    t = str(e).lower()
    if "confirm you" in t and "bot" in t:
        return "YouTube ne bot-check laga diya hai. Thori der baad try karein ya cookies use karein (README/chat dekhein)."
    if "429" in t or "too many requests" in t:
        return "Platform ne temporarily rok diya (too many requests). Kuch minute ruk kar try karein."
    if "private" in t:
        return "Yeh video private hai. Sirf public videos support hoti hain."
    if "login" in t or "log in" in t or "cookies" in t:
        return "Is platform ko login chahiye. Sirf public videos chalti hain; Instagram/Facebook par kabhi login-check aa jata hai."
    if "unavailable" in t or "removed" in t or "not available" in t:
        return "Yeh video available nahi hai (hata di gayi ya aap ke region mein blocked)."
    if "unsupported url" in t:
        return "Yeh link video ka link nahi lagta. Poora video link paste karein."
    if "ffmpeg" in t:
        return "ffmpeg nahi mila. ffmpeg install karke nayi cmd window mein app chalayein."
    return "Video nahi mili. Terminal mein INFO ERROR ki line dekhein, ya yt-dlp update karein."


_info_cache = {}
CACHE_TTL = 300  # seconds


def get_info(url):
    """Look up video info once and reuse it for a few minutes (saves a slow second lookup)."""
    now = time.time()
    hit = _info_cache.get(url)
    if hit and now - hit[0] < CACHE_TTL:
        return hit[1]
    with yt_dlp.YoutubeDL(base_opts()) as ydl:
        info = ydl.extract_info(url, download=False)
    if len(_info_cache) > 50:
        _info_cache.pop(min(_info_cache, key=lambda k: _info_cache[k][0]))
    _info_cache[url] = (now, info)
    return info


def run_download(ydl, url):
    hit = _info_cache.get(url)
    if hit and time.time() - hit[0] < CACHE_TTL:
        try:
            ydl.process_ie_result(copy.deepcopy(hit[1]), download=True)
            return
        except Exception as e:  # fall back to a normal fresh download
            print("Cached download failed, retrying fresh:", e)
    ydl.download([url])


def format_selector(quality):
    if quality == "mp3":
        return "bestaudio/best"
    if quality == "best":
        return "bv*[ext=mp4]+ba[ext=m4a]/bv*+ba/b"
    h = int(quality)
    return (f"bv*[height<={h}][ext=mp4]+ba[ext=m4a]/"
            f"bv*[height<={h}]+ba/b[height<={h}]/b")


def safe_name(title):
    name = re.sub(r'[\\/:*?"<>|]+', "", title or "video").strip()
    return name[:80] or "video"


def make_short(src, dst, style):
    """Convert any video to vertical 9:16 (1080x1920)."""
    if style == "crop":
        vf = "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1"
        cmd_filter = ["-vf", vf]
    else:  # blurred background, original video centred on top
        fc = ("[0:v]split[a][b];"
              "[a]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,boxblur=8:2,scale=1080:1920[bg];"
              "[b]scale=1080:1920:force_original_aspect_ratio=decrease[fg];"
              "[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1[v]")
        cmd_filter = ["-filter_complex", fc, "-map", "[v]", "-map", "0:a?"]
    cmd = ["ffmpeg", "-y", "-i", src, *cmd_filter,
           "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23", "-threads", "0", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", dst]
    subprocess.run(cmd, check=True, capture_output=True, timeout=300)


FAQS = [
    ("Which websites can I download from?",
     "You can paste public video links from YouTube, Facebook, Instagram and TikTok. The site detects the platform automatically."),
    ("Can I download only part of a video?",
     f"Yes. Paste the link, drag the handles on the timeline (or type start and end times), pick a quality and download. "
     f"Only the selected section is saved, up to {MAX_CLIP_SECONDS} seconds per clip."),
    ("How do I turn a YouTube video into a Short?",
     f"Choose Make a Short, select the moment you want (up to {MAX_SHORT_SECONDS} seconds) and pick a style: blurred background keeps the whole frame, "
     "crop fills the vertical screen. You get a 1080x1920 MP4 ready for YouTube Shorts, Reels or TikTok."),
    ("Which video qualities are available?",
     "After you paste a link, the quality menu lists the resolutions that video really offers, from 144p up to 4K when the platform provides it. "
     "You can also choose Best available or audio only."),
    ("Can I convert a video to MP3?",
     "Yes. Choose Audio only (MP3) in the quality menu. It works for both full videos and clips."),
    ("Can I download private videos?",
     "No. Only publicly available videos can be downloaded. Private, friends-only and login-protected posts are not supported."),
    ("How long can a full video be?",
     f"Full video mode supports videos up to {MAX_FULL_SECONDS // 60} minutes. For longer videos, use Clip mode to save the section you need."),
    ("Is this video downloader free?",
     "Yes. There is no signup and no software to install. It works in any modern browser on phone, tablet and computer."),
    ("Is it legal to download videos?",
     "Download only videos you own, videos in the public domain or under a Creative Commons licence, or videos you have permission to use. "
     "Downloading other people’s content can break the platform’s Terms of Service and copyright law, and you are responsible for how you use the files."),
]


@app.get("/")
def index():
    log_event("view", extra=request.args.get("utm_source") or request.args.get("ref"))
    base = request.url_root.rstrip("/")
    title = f"Video Downloader & Shorts Maker – YouTube, Facebook, Instagram | {SITE_NAME}"
    desc = ("Free video downloader for YouTube, Facebook, Instagram and TikTok. Download a clip or the full video in MP4 or MP3, "
            "or convert it into a vertical Short. No signup, works on mobile.")
    ld = [
        {"@context": "https://schema.org", "@type": "WebApplication", "name": SITE_NAME,
         "url": base + "/", "applicationCategory": "MultimediaApplication", "operatingSystem": "Any",
         "description": desc, "offers": {"@type": "Offer", "price": "0", "priceCurrency": "USD"}},
        {"@context": "https://schema.org", "@type": "FAQPage",
         "mainEntity": [{"@type": "Question", "name": q,
                         "acceptedAnswer": {"@type": "Answer", "text": a}} for q, a in FAQS]},
    ]
    return render_template("index.html", site_name=SITE_NAME, title=title, desc=desc, base=base, ld=ld, faqs=FAQS,
                           max_clip=MAX_CLIP_SECONDS, max_short=MAX_SHORT_SECONDS, max_full=MAX_FULL_SECONDS // 60,
                           cpa_url=CPA_URL, cpa_cap=CPA_CAP_MINUTES, ga_id=GA_ID)


@app.get("/robots.txt")
def robots():
    return Response(f"User-agent: *\nAllow: /\nDisallow: /api/\nDisallow: /admin\nSitemap: {request.url_root}sitemap.xml\n",
                    mimetype="text/plain")


@app.get("/sitemap.xml")
def sitemap():
    xml = ('<?xml version="1.0" encoding="UTF-8"?>'
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
           f'<url><loc>{request.url_root}</loc><changefreq>weekly</changefreq><priority>1.0</priority></url>'
           '</urlset>')
    return Response(xml, mimetype="application/xml")


def _info():
    url = (request.json or {}).get("url", "").strip()
    platform = detect_platform(url)
    if not platform:
        return jsonify(error="Sirf YouTube, Facebook, Instagram ya TikTok ka link daalein."), 400
    try:
        d = get_info(url)
    except Exception as e:
        print("INFO ERROR:", e)
        return jsonify(error=explain_error(e)), 400

    heights = sorted(
        {f["height"] for f in d.get("formats", [])
         if f.get("height") and f.get("vcodec") not in (None, "none")},
        reverse=True,
    )
    return jsonify(
        platform=platform,
        title=d.get("title") or f"{platform} video",
        thumbnail=d.get("thumbnail"),
        duration=d.get("duration"),
        channel=d.get("uploader"),
        heights=heights,
        is_live=bool(d.get("is_live")),
    )


def _download():
    if rate_limited(request.remote_addr):
        return jsonify(error="Thora ruk kar dobara try karein."), 429

    body = request.json or {}
    url = body.get("url", "").strip()
    mode = body.get("mode", "clip")
    quality = str(body.get("quality", "best"))
    style = body.get("style", "blur")
    if not detect_platform(url):
        return jsonify(error="Sirf YouTube, Facebook, Instagram ya TikTok ka link daalein."), 400
    if quality not in QUALITIES:
        return jsonify(error="Quality galat hai."), 400
    if mode not in ("clip", "full", "short"):
        return jsonify(error="Mode galat hai."), 400

    opts = base_opts()
    opts["merge_output_format"] = "mp4"
    # Shorts are 1080 wide, so there is no point downloading anything bigger.
    opts["format"] = format_selector("1080" if mode == "short" else quality)

    title = "clip"
    if mode in ("clip", "short"):
        limit = MAX_SHORT_SECONDS if mode == "short" else MAX_CLIP_SECONDS
        try:
            start = parse_time(body.get("start", 0))
            end = parse_time(body.get("end", 0))
        except ValueError as e:
            return jsonify(error=str(e)), 400
        if end <= start:
            return jsonify(error="End time start se zyada hona chahiye."), 400
        if end - start > limit:
            return jsonify(error=f"Is mode mein {limit} second se lamba hissa nahi ban sakta."), 400
        opts["download_ranges"] = download_range_func(None, [(start, end)])
        opts["force_keyframes_at_cuts"] = not FAST_CUT
        title = safe_name(body.get("title"))
    else:
        try:
            d = get_info(url)
        except Exception as e:
            print("INFO ERROR:", e)
            return jsonify(error=explain_error(e)), 400
        if d.get("is_live"):
            return jsonify(error="Live stream download nahi ho sakti."), 400
        if (d.get("duration") or 0) > MAX_FULL_SECONDS:
            return jsonify(error=f"Full video mode mein {MAX_FULL_SECONDS // 60} minute se lambi video allowed nahi. Clip mode use karein."), 400
        title = safe_name(d.get("title"))

    tmp = tempfile.mkdtemp(prefix="dl_")
    opts["outtmpl"] = os.path.join(tmp, "src.%(ext)s")
    if mode != "short" and quality == "mp3":
        opts["postprocessors"] = [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
        ]

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            run_download(ydl, url)
        files = [f for f in os.listdir(tmp) if f.startswith("src.")]
        if not files:
            raise RuntimeError("no output file")
        path = os.path.join(tmp, files[0])

        if mode == "short":
            out = os.path.join(tmp, "short.mp4")
            make_short(path, out, style)
            path = out
    except subprocess.CalledProcessError as e:
        print("FFMPEG ERROR:", e.stderr.decode(errors="ignore")[-800:])
        shutil.rmtree(tmp, ignore_errors=True)
        return jsonify(error="Short convert nahi ho saka. Dobara try karein."), 500
    except Exception as e:
        print("DOWNLOAD ERROR:", e)
        shutil.rmtree(tmp, ignore_errors=True)
        return jsonify(error=explain_error(e)), 500

    @after_this_request
    def cleanup(response):
        shutil.rmtree(tmp, ignore_errors=True)
        return response

    if mode == "short":
        name = f"{title}-short.mp4"
    elif quality == "mp3":
        name = f"{title}.mp3"
    else:
        name = f"{title}.mp4"
    return send_file(path, as_attachment=True, download_name=name)


def _status(resp):
    if isinstance(resp, tuple):
        return resp[1]
    return getattr(resp, "status_code", 200)


@app.post("/api/info")
def info():
    t0 = time.time()
    resp = _info()
    body = request.get_json(silent=True) or {}
    log_event("lookup", platform=detect_platform(str(body.get("url", ""))), ok=_status(resp) == 200,
              ms=int((time.time() - t0) * 1000))
    return resp


@app.post("/api/download")
def download():
    t0 = time.time()
    resp = _download()
    body = request.get_json(silent=True) or {}
    code = _status(resp)
    log_event("download", platform=detect_platform(str(body.get("url", ""))), mode=str(body.get("mode", ""))[:10],
              quality=str(body.get("quality", ""))[:10], ok=code == 200, ms=int((time.time() - t0) * 1000), extra=code)
    return resp


@app.post("/api/event")
def event():
    body = request.get_json(silent=True) or {}
    if body.get("kind") == "cpa_click":
        log_event("cpa_click", extra=str(body.get("extra", ""))[:64])
    return ("", 204)


# ---------------------------------------------------------------- admin dashboard
def _admin_ok():
    pw = os.environ.get("ADMIN_PASSWORD", "")
    a = request.authorization
    return bool(pw) and bool(a) and hmac.compare_digest((a.password or ""), pw)


def _table(title, head, rows):
    h = "".join(f"<th>{_html.escape(str(x))}</th>" for x in head)
    b = "".join("<tr>" + "".join(f"<td>{_html.escape(str(x))}</td>" for x in r) + "</tr>" for r in rows) \
        or f"<tr><td colspan={len(head)}>No data yet</td></tr>"
    return f"<h2>{_html.escape(title)}</h2><div class=w><table><tr>{h}</tr>{b}</table></div>"


@app.get("/admin")
def admin():
    if not os.environ.get("ADMIN_PASSWORD"):
        return Response("Not found", 404)
    if not _admin_ok():
        return Response("Login required", 401, {"WWW-Authenticate": 'Basic realm="admin"'})
    now = int(time.time())
    with _db() as c:
        def q(sql, args=()):
            return c.execute(sql, args).fetchall()

        summary = []
        for label, days in (("Today", 1), ("7 days", 7), ("30 days", 30)):
            t = now - days * 86400
            r = q("""SELECT SUM(kind='view'), COUNT(DISTINCT CASE WHEN kind='view' THEN ip END), SUM(kind='lookup'),
                            SUM(kind='download' AND ok=1), SUM(kind='download' AND ok=0), SUM(kind='cpa_click')
                     FROM events WHERE ts>=?""", (t,))[0]
            summary.append([label] + [x or 0 for x in r])
        t30 = now - 30 * 86400
        by_platform = q("SELECT COALESCE(platform,'?'), SUM(ok=1), SUM(ok=0) FROM events WHERE kind='download' AND ts>=? GROUP BY 1 ORDER BY 2 DESC", (t30,))
        by_mode = q("SELECT COALESCE(mode,'?'), COUNT(*) FROM events WHERE kind='download' AND ok=1 AND ts>=? GROUP BY 1 ORDER BY 2 DESC", (t30,))
        by_quality = q("SELECT COALESCE(quality,'?'), COUNT(*) FROM events WHERE kind='download' AND ok=1 AND ts>=? GROUP BY 1 ORDER BY 2 DESC LIMIT 8", (t30,))
        refs = q("SELECT COALESCE(ref,'(direct)'), COUNT(*), COUNT(DISTINCT ip) FROM events WHERE kind='view' AND ts>=? GROUP BY 1 ORDER BY 2 DESC LIMIT 10", (t30,))
        utm = q("SELECT extra, COUNT(*) FROM events WHERE kind='view' AND extra IS NOT NULL AND ts>=? GROUP BY 1 ORDER BY 2 DESC LIMIT 10", (t30,))
        errors = q("SELECT datetime(ts,'unixepoch'), COALESCE(platform,'?'), COALESCE(mode,'?'), extra FROM events WHERE kind='download' AND ok=0 ORDER BY id DESC LIMIT 15")
        daily = q("""SELECT date(ts,'unixepoch') d, SUM(kind='view'), COUNT(DISTINCT CASE WHEN kind='view' THEN ip END),
                            SUM(kind='download' AND ok=1), SUM(kind='cpa_click')
                     FROM events WHERE ts>=? GROUP BY d ORDER BY d DESC""", (now - 14 * 86400,))
        speed = q("SELECT COALESCE(mode,'?'), CAST(AVG(ms)/1000 AS INT) FROM events WHERE kind='download' AND ok=1 AND ts>=? GROUP BY 1", (t30,))
    page = ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            "<meta name=robots content=noindex><title>Admin</title>"
            "<style>body{font:15px system-ui;margin:0;padding:20px;background:#f1f0fb;color:#17163a;max-width:900px;margin:auto}"
            "h1{font-size:1.4rem}h2{font-size:1.05rem;margin:26px 0 8px}.w{overflow-x:auto;background:#fff;border-radius:12px}"
            "table{border-collapse:collapse;width:100%}th,td{text-align:left;padding:9px 12px;border-bottom:1px solid #e3e1f5}"
            "th{background:#e7e4fb;font-size:.8rem}</style>"
            f"<h1>{_html.escape(SITE_NAME)} stats</h1>"
            + _table("Summary", ["Period", "Page views", "Visitors", "Lookups", "Downloads OK", "Downloads failed", "CPA clicks"], summary)
            + _table("Last 14 days", ["Date", "Views", "Visitors", "Downloads OK", "CPA clicks"], daily)
            + _table("Downloads by platform (30 days)", ["Platform", "OK", "Failed"], by_platform)
            + _table("Downloads by mode (30 days)", ["Mode", "Count"], by_mode)
            + _table("Popular qualities (30 days)", ["Quality", "Count"], by_quality)
            + _table("Average time per download, seconds (30 days)", ["Mode", "Seconds"], speed)
            + _table("Traffic sources (30 days)", ["Referrer", "Views", "Visitors"], refs)
            + _table("utm_source / ref tags (30 days)", ["Tag", "Views"], utm)
            + _table("Latest failed downloads", ["Time (UTC)", "Platform", "Mode", "HTTP code"], errors))
    return Response(page, mimetype="text/html")


if __name__ == "__main__":
    print("yt-dlp version:", yt_dlp.version.__version__)
    app.run(debug=True, port=5000)
