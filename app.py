"""yt-uploader: push rendered videos to YouTube with metadata, thumbnail and schedule.

Endpoints
  GET  /health
  GET  /auth/start?channel=<name>&key=<API_KEY>   one-time Google sign-in per channel
  GET  /auth/callback                             Google redirects here; shows the refresh token
  POST /upload            (Bearer API_KEY)        queue an upload job, returns job_id
  GET  /jobs/{job_id}     (Bearer API_KEY)        job status / youtube video id

Env vars
  API_KEY                         shared secret for callers
  GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET
  PUBLIC_URL                      e.g. https://yt-uploader.onrender.com (used for the OAuth redirect)
  YT_REFRESH_<CHANNEL>            refresh token per channel, e.g. YT_REFRESH_SIDEQUESTS, YT_REFRESH_ATLASNOTES
"""
import hashlib
import hmac
import io
import os
import secrets
import tempfile
import threading
import time
import traceback
import urllib.parse
import uuid

import httpx
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

API_KEY = os.environ.get("API_KEY", "")
CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")
SCOPES = "https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.readonly"
TOKEN_URL = "https://oauth2.googleapis.com/token"
CHUNK = 8 * 1024 * 1024  # resumable upload chunk (multiple of 256 KiB)
THUMB_MAX = 2 * 1024 * 1024

app = FastAPI(title="yt-uploader")
JOBS: dict[str, dict] = {}
BY_URL: dict[str, str] = {}  # dedupe: (channel|video_url) -> job_id
RUNTIME_TOKENS: dict[str, str] = {}  # tokens captured via /auth/callback since last restart
LOCK = threading.Lock()


# ---------------------------------------------------------------- helpers
def require_key(authorization: str | None):
    if not API_KEY:
        raise HTTPException(500, "API_KEY not configured")
    tok = (authorization or "").removeprefix("Bearer ").strip()
    if not hmac.compare_digest(tok, API_KEY):
        raise HTTPException(401, "bad key")


def norm(channel: str) -> str:
    return "".join(c for c in channel.upper() if c.isalnum())


def refresh_token_for(channel: str) -> str:
    c = norm(channel)
    tok = RUNTIME_TOKENS.get(c) or os.environ.get(f"YT_REFRESH_{c}", "")
    if not tok:
        raise RuntimeError(f"no refresh token for channel {channel} (set YT_REFRESH_{c})")
    return tok


def access_token(channel: str) -> str:
    r = httpx.post(TOKEN_URL, data={
        "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
        "refresh_token": refresh_token_for(channel), "grant_type": "refresh_token"}, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"token refresh failed: {r.status_code} {r.text[:300]}")
    return r.json()["access_token"]


def sign_state(channel: str) -> str:
    nonce = secrets.token_hex(8)
    mac = hmac.new(API_KEY.encode(), f"{channel}.{nonce}".encode(), hashlib.sha256).hexdigest()[:24]
    return f"{channel}.{nonce}.{mac}"


def check_state(state: str) -> str:
    try:
        channel, nonce, mac = state.split(".")
    except ValueError:
        raise HTTPException(400, "bad state")
    good = hmac.new(API_KEY.encode(), f"{channel}.{nonce}".encode(), hashlib.sha256).hexdigest()[:24]
    if not hmac.compare_digest(mac, good):
        raise HTTPException(400, "bad state")
    return channel


def log(job: dict, msg: str):
    job["log"].append(f"{time.strftime('%H:%M:%S')} {msg}")


# ---------------------------------------------------------------- public pages (for the OAuth consent screen)
PAGE = """<html><head><title>{t}</title></head><body style="font-family:system-ui;max-width:720px;margin:40px auto;line-height:1.6">
<h1>{t}</h1>{b}</body></html>"""


@app.get("/", response_class=HTMLResponse)
def home():
    return PAGE.format(t="yt-uploader", b="<p>A private tool used by Sidequest Labs LLC to upload its own videos to its own YouTube channels. It is not offered to other users.</p><p><a href='/privacy'>Privacy policy</a> &middot; <a href='/terms'>Terms</a></p><p>Contact: mishuk77@gmail.com</p>")


@app.get("/privacy", response_class=HTMLResponse)
def privacy():
    return PAGE.format(t="Privacy policy", b="<p>yt-uploader is used only by its owner to upload videos to the owner's YouTube channels through the YouTube Data API.</p><p>It requests the YouTube upload and read-only scopes. The only Google data it uses is the authorization token needed to upload, and the channel name and ID to confirm the right channel. Tokens are stored as encrypted environment variables on the hosting provider and are never shared or sold. No other personal data is collected.</p><p>You can revoke access at any time at <a href='https://myaccount.google.com/permissions'>myaccount.google.com/permissions</a>. Use of Google data follows the <a href='https://developers.google.com/terms/api-services-user-data-policy'>Google API Services User Data Policy</a>, and use of YouTube follows the <a href='https://www.youtube.com/t/terms'>YouTube Terms of Service</a>.</p><p>Contact: mishuk77@gmail.com</p>")


@app.get("/terms", response_class=HTMLResponse)
def terms():
    return PAGE.format(t="Terms of service", b="<p>yt-uploader is a private tool operated by Sidequest Labs LLC for uploading its own content. It is provided as is, with no warranty, and is not available for public use. Uploaded content must follow the <a href='https://www.youtube.com/t/terms'>YouTube Terms of Service</a>.</p><p>Contact: mishuk77@gmail.com</p>")


# ---------------------------------------------------------------- auth flow
@app.get("/health")
def health():
    chans = sorted({k[len("YT_REFRESH_"):] for k in os.environ if k.startswith("YT_REFRESH_")} | set(RUNTIME_TOKENS))
    return {"ok": True, "channels": chans, "configured": bool(CLIENT_ID and CLIENT_SECRET and API_KEY and PUBLIC_URL)}


@app.get("/auth/start")
def auth_start(channel: str, key: str):
    require_key(f"Bearer {key}")
    params = {
        "client_id": CLIENT_ID, "redirect_uri": f"{PUBLIC_URL}/auth/callback",
        "response_type": "code", "scope": SCOPES, "access_type": "offline",
        "prompt": "consent select_account", "include_granted_scopes": "true",
        "state": sign_state(norm(channel)),
    }
    return RedirectResponse("https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(params))


@app.get("/auth/callback", response_class=HTMLResponse)
def auth_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    if error:
        return HTMLResponse(f"<h2>Google returned an error: {error}</h2>", 400)
    channel = check_state(state)
    r = httpx.post(TOKEN_URL, data={
        "code": code, "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
        "redirect_uri": f"{PUBLIC_URL}/auth/callback", "grant_type": "authorization_code"}, timeout=30)
    if r.status_code != 200:
        return HTMLResponse(f"<h2>Token exchange failed</h2><pre>{r.text[:500]}</pre>", 400)
    tok = r.json()
    refresh = tok.get("refresh_token", "")
    me = httpx.get("https://www.googleapis.com/youtube/v3/channels", params={"part": "snippet", "mine": "true"},
                   headers={"Authorization": f"Bearer {tok['access_token']}"}, timeout=30).json()
    items = me.get("items", [])
    title = items[0]["snippet"]["title"] if items else "(no channel found)"
    cid = items[0]["id"] if items else ""
    if refresh:
        RUNTIME_TOKENS[channel] = refresh
    return HTMLResponse(f"""<html><body style="font-family:system-ui;max-width:720px;margin:40px auto;line-height:1.5">
<h2>Signed in as YouTube channel: {title}</h2><p>Channel ID: <code>{cid}</code> &middot; saved under key <b>{channel}</b></p>
<p>It works right now. To keep it after the service restarts, add this in Render &rarr; yt-uploader &rarr; Environment:</p>
<p><b>Key:</b> <code>YT_REFRESH_{channel}</code></p>
<p><b>Value:</b></p><textarea rows="4" style="width:100%" onclick="this.select()">{refresh or 'NO REFRESH TOKEN RETURNED - remove the app at myaccount.google.com/permissions and sign in again'}</textarea>
<p>If the wrong channel is shown, open the sign-in link again and pick the right channel or brand account.</p>
</body></html>""")


# ---------------------------------------------------------------- upload
class UploadReq(BaseModel):
    channel: str
    video_url: str
    title: str = Field(max_length=100)
    description: str = ""
    tags: list[str] = []
    category_id: str = "28"          # Science & Technology
    privacy: str = "private"
    publish_at: str | None = None    # RFC3339 UTC, requires privacy=private
    thumbnail_url: str | None = None
    made_for_kids: bool = False
    notify_subscribers: bool = True
    language: str = "en"
    expected_channel_id: str | None = None  # safety: refuse if the token's channel differs


@app.post("/upload")
def upload(req: UploadReq, bg: BackgroundTasks, authorization: str | None = Header(None)):
    require_key(authorization)
    key = f"{norm(req.channel)}|{req.video_url}"
    with LOCK:
        if key in BY_URL and JOBS[BY_URL[key]]["status"] in ("queued", "running", "done"):
            return {"job_id": BY_URL[key], "deduped": True, **public(JOBS[BY_URL[key]])}
        jid = uuid.uuid4().hex[:12]
        JOBS[jid] = {"id": jid, "status": "queued", "req": req.model_dump(), "log": [], "video_id": None,
                     "thumbnail": None, "error": None, "created": time.time()}
        BY_URL[key] = jid
    bg.add_task(run_job, jid)
    return {"job_id": jid, "deduped": False, "status": "queued"}


def public(job: dict) -> dict:
    return {k: job[k] for k in ("id", "status", "video_id", "thumbnail", "error", "log")} | {
        "title": job["req"]["title"], "url": f"https://youtube.com/shorts/{job['video_id']}" if job["video_id"] else None}


@app.get("/jobs/{jid}")
def job_status(jid: str, authorization: str | None = Header(None)):
    require_key(authorization)
    if jid not in JOBS:
        raise HTTPException(404, "unknown job (service may have restarted)")
    return public(JOBS[jid])


@app.get("/jobs")
def jobs(authorization: str | None = Header(None)):
    require_key(authorization)
    return [public(j) for j in sorted(JOBS.values(), key=lambda j: j["created"])]


def run_job(jid: str):
    job = JOBS[jid]
    req = job["req"]
    job["status"] = "running"
    path = None
    try:
        tok = access_token(req["channel"])
        H = {"Authorization": f"Bearer {tok}"}
        if req.get("expected_channel_id"):
            me = httpx.get("https://www.googleapis.com/youtube/v3/channels", params={"part": "id", "mine": "true"},
                           headers=H, timeout=30).json()
            got = (me.get("items") or [{}])[0].get("id")
            if got != req["expected_channel_id"]:
                raise RuntimeError(f"token belongs to channel {got}, expected {req['expected_channel_id']}")

        # 1. download source video to disk (streaming)
        fd, path = tempfile.mkstemp(suffix=".mp4")
        with os.fdopen(fd, "wb") as f, httpx.stream("GET", req["video_url"], timeout=120, follow_redirects=True) as r:
            r.raise_for_status()
            for chunk in r.iter_bytes(1 << 20):
                f.write(chunk)
        size = os.path.getsize(path)
        log(job, f"downloaded {size/1e6:.1f} MB")

        # 2. start resumable session with metadata
        status = {"privacyStatus": req["privacy"], "selfDeclaredMadeForKids": req["made_for_kids"]}
        if req.get("publish_at"):
            status["privacyStatus"] = "private"
            status["publishAt"] = req["publish_at"]
        body = {"snippet": {"title": req["title"], "description": req["description"], "tags": req["tags"],
                            "categoryId": req["category_id"], "defaultLanguage": req["language"],
                            "defaultAudioLanguage": req["language"]},
                "status": status}
        r = httpx.post("https://www.googleapis.com/upload/youtube/v3/videos",
                       params={"uploadType": "resumable", "part": "snippet,status",
                               "notifySubscribers": str(req["notify_subscribers"]).lower()},
                       headers=H | {"X-Upload-Content-Type": "video/mp4", "X-Upload-Content-Length": str(size)},
                       json=body, timeout=60)
        if r.status_code != 200:
            raise RuntimeError(f"session start failed {r.status_code}: {r.text[:500]}")
        session = r.headers["Location"]

        # 3. send chunks, resuming on transient errors
        offset, tries, video = 0, 0, None
        with open(path, "rb") as f:
            while offset < size:
                f.seek(offset)
                data = f.read(CHUNK)
                end = offset + len(data) - 1
                try:
                    r = httpx.put(session, content=data, timeout=300,
                                  headers={"Content-Range": f"bytes {offset}-{end}/{size}"})
                except httpx.TransportError as e:
                    r = None
                    log(job, f"transport error {e!r}")
                if r is not None and r.status_code in (200, 201):
                    video = r.json()
                    break
                if r is not None and r.status_code == 308:
                    rng = r.headers.get("Range")
                    offset = int(rng.split("-")[1]) + 1 if rng else 0
                    tries = 0
                    continue
                tries += 1
                if tries > 6 or (r is not None and r.status_code < 500 and r.status_code != 429):
                    raise RuntimeError(f"upload failed {getattr(r, 'status_code', '?')}: {getattr(r, 'text', '')[:500]}")
                time.sleep(2 ** tries)
                q = httpx.put(session, headers={"Content-Range": f"bytes */{size}"}, timeout=60)
                rng = q.headers.get("Range")
                offset = int(rng.split("-")[1]) + 1 if (q.status_code == 308 and rng) else 0
        if not video:
            raise RuntimeError("upload ended without a video resource")
        job["video_id"] = video["id"]
        log(job, f"uploaded video {video['id']} ({video['status'].get('privacyStatus')}, publishAt={video['status'].get('publishAt')})")

        # 4. thumbnail
        if req.get("thumbnail_url"):
            try:
                img = httpx.get(req["thumbnail_url"], timeout=60, follow_redirects=True).content
                ctype = "image/jpeg"
                if len(img) > THUMB_MAX:
                    from PIL import Image
                    im = Image.open(io.BytesIO(img)).convert("RGB")
                    q = 90
                    while True:
                        buf = io.BytesIO()
                        im.save(buf, "JPEG", quality=q, optimize=True)
                        if buf.tell() <= THUMB_MAX or q <= 50:
                            break
                        q -= 8
                    img = buf.getvalue()
                elif img[:8].startswith(b"\x89PNG"):
                    ctype = "image/png"
                tr = httpx.post("https://www.googleapis.com/upload/youtube/v3/thumbnails/set",
                                params={"videoId": video["id"], "uploadType": "media"},
                                headers=H | {"Content-Type": ctype}, content=img, timeout=120)
                job["thumbnail"] = "set" if tr.status_code == 200 else f"failed {tr.status_code}: {tr.text[:300]}"
            except Exception as e:  # thumbnail failure should not fail the upload
                job["thumbnail"] = f"failed: {e!r}"
            log(job, f"thumbnail {job['thumbnail']}")
        job["status"] = "done"
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)
        log(job, traceback.format_exc()[-800:])
        with LOCK:  # allow a retry with the same url
            BY_URL.pop(f"{norm(req['channel'])}|{req['video_url']}", None)
    finally:
        if path and os.path.exists(path):
            os.remove(path)
