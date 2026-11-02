# yt-uploader

A small Render web service that uploads a finished video (from a public URL) to a YouTube channel, with title, description, tags, scheduled publish time and custom thumbnail in one call.

## Endpoints

| Method | Path | Auth | What it does |
|---|---|---|---|
| GET | `/health` | none | Config check and the list of channels that have tokens |
| GET | `/auth/start?channel=sidequests&key=API_KEY` | key in query | One-time Google sign-in for a channel |
| GET | `/auth/callback` | Google | Shows the channel it signed into and the refresh token to save |
| POST | `/upload` | `Authorization: Bearer API_KEY` | Queue an upload, returns `job_id` |
| GET | `/jobs/{job_id}` | Bearer | Status, YouTube video id, thumbnail result, log |

### POST /upload body

```json
{
  "channel": "sidequests",
  "video_url": "https://…/video.mp4",
  "title": "🕳️ What if you fell into a black hole?",
  "description": "… #whatif #science #shorts",
  "tags": ["what if", "black hole"],
  "publish_at": "2026-11-09T23:00:00Z",
  "thumbnail_url": "https://…/thumb.jpg",
  "category_id": "28",
  "made_for_kids": false,
  "expected_channel_id": "UCpANCB3hR3tBbkbVsR3DyeA"
}
```

`publish_at` forces `privacy=private` until that time. `expected_channel_id` makes the job refuse to upload if the token belongs to a different channel. The same `channel` + `video_url` pair is deduped while the service is up.

## Environment variables

| Key | Value |
|---|---|
| `API_KEY` | long random string shared with the caller |
| `GOOGLE_CLIENT_ID` | OAuth client ID (Web application) |
| `GOOGLE_CLIENT_SECRET` | OAuth client secret |
| `PUBLIC_URL` | `https://<service>.onrender.com` |
| `YT_REFRESH_SIDEQUESTS` | refresh token from `/auth/callback` |
| `YT_REFRESH_ATLASNOTES` | refresh token from `/auth/callback` |

## Google setup (once)

1. console.cloud.google.com → new project `yt-uploader`.
2. APIs & Services → Library → enable **YouTube Data API v3**.
3. OAuth consent screen → External → app name, your email → scopes `youtube.upload` and `youtube.readonly` → add yourself as a test user → **Publish app** (In production). Testing mode refresh tokens expire after 7 days.
4. Credentials → Create OAuth client ID → Web application → Authorized redirect URI `https://<service>.onrender.com/auth/callback`.
5. Put the client ID and secret into Render env vars.
6. Open `/auth/start?channel=sidequests&key=API_KEY`, pick the Sidequests channel, copy the token into `YT_REFRESH_SIDEQUESTS`. Repeat with `channel=atlasnotes`.

Unverified apps show a "Google hasn't verified this app" screen once: Advanced → Go to yt-uploader.

## Notes

- Render free instances sleep after 15 minutes idle and take ~30–60 s to wake; the first call after a quiet period is slow. Job state is in memory and is lost on restart, so poll until `done`.
- Thumbnails over 2 MB are recompressed to JPEG before `thumbnails.set`.
