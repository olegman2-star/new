"""Smart Tech Daily pipeline — deterministic stages.

The creative stages (research, writing, promotion) run once through Claude in
CI. Everything here is mechanical: it costs credits and API calls, not tokens,
so it is plain Python with explicit retries and no LLM in the loop.

Ordering matters for cost. `preflight` validates every credential before
`generate` spends a single Higgsfield credit — a dead YouTube token used to
surface only after a 16 MB video had already been paid for and thrown away.

Subcommands:
    preflight   verify Higgsfield auth + YouTube OAuth, spending nothing
    generate    prompt -> video.mp4 (Soul text-to-image, then DoP image-to-video)
    upload      video.mp4 + metadata.json -> YouTube, then append history.json
"""

import argparse
import datetime
import json
import os
import subprocess
import sys
import time

HIGGSFIELD_API_BASE = "https://platform.higgsfield.ai"
HF_IMAGE_MODEL = os.environ.get("HF_IMAGE_MODEL", "higgsfield-ai/soul/standard")
HF_VIDEO_MODEL = os.environ.get("HF_VIDEO_MODEL", "higgsfield-ai/dop/standard")
YOUTUBE_SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]

HISTORY_PATH = "pipeline/history.json"


def fail(msg: str) -> "NoReturn":
    """Exit non-zero with a GitHub-Actions-annotated error."""
    print(f"::error::{msg}", file=sys.stderr)
    sys.exit(1)


def require_env(*names) -> None:
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        fail(f"missing required environment variable(s): {', '.join(missing)}")


# --------------------------------------------------------------------------
# Higgsfield
#
# platform.higgsfield.ai sits behind Cloudflare bot protection that rejects
# Python HTTP clients by TLS fingerprint (error 1010) no matter what
# User-Agent they send. curl's genuine signature passes, so every Higgsfield
# call shells out rather than using requests/urllib.
# --------------------------------------------------------------------------
def hf_curl(method: str, path: str, payload: dict = None, retries: int = 3) -> dict:
    require_env("HIGGSFIELD_API_KEY", "HIGGSFIELD_API_SECRET")
    auth = f"Key {os.environ['HIGGSFIELD_API_KEY']}:{os.environ['HIGGSFIELD_API_SECRET']}"
    cmd = [
        "curl", "-sS", "-X", method, f"{HIGGSFIELD_API_BASE}/{path}",
        "-H", f"Authorization: {auth}",
        "-H", "Content-Type: application/json",
        "-H", "Accept: application/json",
        "-w", "\n%{http_code}",
        "--max-time", "45",
    ]
    if payload is not None:
        cmd += ["-d", json.dumps(payload)]

    last_error = ""
    for attempt in range(1, retries + 1):
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        if result.returncode == 0:
            body, _, code = result.stdout.rpartition("\n")
            code = code.strip()
            try:
                data = json.loads(body) if body.strip() else {}
            except json.JSONDecodeError:
                data = {"_raw": body[:300]}
            # 4xx is a decision (bad auth, no credits) — retrying cannot help.
            if code.startswith("4"):
                return {"_http": int(code), **data}
            if code.startswith("2"):
                return {"_http": int(code), **data}
            last_error = f"HTTP {code}: {body[:200]}"
        else:
            last_error = f"curl exit {result.returncode}: {result.stderr[:200]}"

        if attempt < retries:
            backoff = 5 * (2 ** (attempt - 1))
            print(f"  retry {attempt}/{retries - 1} in {backoff}s ({last_error})")
            time.sleep(backoff)

    fail(f"Higgsfield {method} {path} failed after {retries} attempts — {last_error}")


def hf_submit(model_path: str, payload: dict) -> str:
    data = hf_curl("POST", model_path, payload)
    if data.get("_http", 200) >= 400:
        detail = data.get("detail") or data
        fail(f"Higgsfield rejected the {model_path} request: {detail}")
    request_id = data.get("request_id") or data.get("id")
    if not request_id:
        fail(f"no request_id in Higgsfield response: {data}")
    print(f"  submitted {model_path} -> {request_id}")
    return request_id


def hf_poll(request_id: str, max_wait: int = 900) -> dict:
    deadline = time.time() + max_wait
    last_status = None
    while time.time() < deadline:
        data = hf_curl("GET", f"requests/{request_id}/status")
        status = data.get("status", "")
        if status != last_status:
            print(f"  {request_id[:8]}: {status}")
            last_status = status
        if status == "completed":
            return data
        if status in ("failed", "error", "nsfw", "cancelled"):
            fail(f"Higgsfield generation {status}: {data}")
        time.sleep(15)
    fail(f"Higgsfield request {request_id} still unfinished after {max_wait}s")


def generate_video(prompt: str, out_path: str) -> None:
    """Text -> cinematic still (Soul) -> video with camera motion (DoP)."""
    print("Stage A: text -> image")
    image = hf_poll(hf_submit(HF_IMAGE_MODEL, {
        "prompt": prompt,
        "aspect_ratio": "16:9",
        "resolution": "720p",
    }))
    images = image.get("images") or []
    if not images or not images[0].get("url"):
        fail(f"Soul completed without an image URL: {image}")
    image_url = images[0]["url"]
    print(f"  image: {image_url}")

    print("Stage B: image -> video")
    video = hf_poll(hf_submit(HF_VIDEO_MODEL, {
        "image_url": image_url,
        "prompt": prompt,
        "duration": 5,
    }))
    video_url = (video.get("video") or {}).get("url")
    if not video_url:
        fail(f"DoP completed without a video URL: {video}")

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    result = subprocess.run(
        ["curl", "-sS", "-L", "--retry", "3", "--max-time", "300", "-o", out_path, video_url],
        capture_output=True, text=True, timeout=330,
    )
    size = os.path.getsize(out_path) if os.path.exists(out_path) else 0
    if result.returncode != 0 or size < 100_000:
        fail(f"video download failed (exit {result.returncode}, {size} bytes): {result.stderr[:200]}")
    print(f"  saved {out_path} ({size / 1_048_576:.1f} MB)")


# --------------------------------------------------------------------------
# YouTube
# --------------------------------------------------------------------------
def youtube_credentials():
    from google.oauth2.credentials import Credentials

    require_env("YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN")
    return Credentials(
        token=None,
        refresh_token=os.environ["YOUTUBE_REFRESH_TOKEN"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["YOUTUBE_CLIENT_ID"],
        client_secret=os.environ["YOUTUBE_CLIENT_SECRET"],
        scopes=YOUTUBE_SCOPES,
    )


def youtube_service():
    from googleapiclient.discovery import build

    return build("youtube", "v3", credentials=youtube_credentials())


def load_history() -> list:
    if not os.path.exists(HISTORY_PATH):
        return []
    try:
        with open(HISTORY_PATH) as f:
            return json.load(f)
    except json.JSONDecodeError:
        fail(f"{HISTORY_PATH} is not valid JSON — refusing to append and risk losing history")


def today() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")


# --------------------------------------------------------------------------
# Subcommands
# --------------------------------------------------------------------------
def cmd_preflight(args) -> None:
    """Validate every credential before anything costs money.

    Higgsfield auth is checked with a status lookup for a non-existent request:
    valid credentials yield 404 (unknown id), bad ones yield 401/403. No
    generation is queued, so no credits are spent.
    """
    print("== preflight ==")

    probe = hf_curl("GET", "requests/00000000-0000-0000-0000-000000000000/status")
    code = probe.get("_http")
    if code in (401, 403):
        fail(f"Higgsfield credentials rejected (HTTP {code}) — check HIGGSFIELD_API_KEY/SECRET")
    print(f"  higgsfield auth OK (probe HTTP {code})")

    from google.auth.transport.requests import Request
    from google.auth.exceptions import RefreshError

    creds = youtube_credentials()
    try:
        creds.refresh(Request())
    except RefreshError as e:
        fail(
            f"YouTube OAuth refresh failed ({e}). The refresh token is expired, revoked, "
            "or mismatched with the client credentials. Regenerate it with "
            "scripts/get_youtube_token.py. If the Google Cloud OAuth consent screen is in "
            "'Testing' status, tokens expire after 7 days — set it to 'In production'."
        )
    print("  youtube oauth OK (token refreshed)")

    if any(e.get("date") == today() for e in load_history()):
        fail(f"history.json already has an entry for {today()} — refusing to publish twice")
    print(f"  no existing upload for {today()}")

    print("preflight passed — safe to spend credits")


def cmd_generate(args) -> None:
    with open(args.prompt_file) as f:
        prompt = f.read().strip()
    if not prompt:
        fail(f"{args.prompt_file} is empty")
    print(f"prompt: {prompt[:160]}{'...' if len(prompt) > 160 else ''}")
    generate_video(prompt, args.out)


def cmd_upload(args) -> None:
    from googleapiclient.http import MediaFileUpload

    with open(args.metadata) as f:
        meta = json.load(f)

    for field in ("topic", "title", "description"):
        if not meta.get(field):
            fail(f"metadata.json is missing '{field}'")
    if len(meta["title"]) > 100:
        fail(f"title is {len(meta['title'])} chars — YouTube's limit is 100")

    history = load_history()
    if any(e.get("date") == today() for e in history):
        fail(f"history.json already has an entry for {today()} — refusing to publish twice")

    size = os.path.getsize(args.video) if os.path.exists(args.video) else 0
    if size < 100_000:
        fail(f"{args.video} is {size} bytes — not a usable video")

    body = {
        "snippet": {
            "title": meta["title"],
            "description": meta["description"],
            "tags": meta.get("tags") or ["tech", "AI", "technology"],
            "categoryId": "28",  # Science & Technology
        },
        "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False},
    }
    media = MediaFileUpload(args.video, mimetype="video/mp4", resumable=True)
    request = youtube_service().videos().insert(part="snippet,status", body=body, media_body=media)

    print(f"uploading: {meta['title']}")
    response, last_pct = None, -10
    while response is None:
        status, response = request.next_chunk()
        if status and int(status.progress() * 100) >= last_pct + 10:
            last_pct = int(status.progress() * 100)
            print(f"  {last_pct}%")

    video_id = response["id"]
    url = f"https://www.youtube.com/watch?v={video_id}"
    print(f"published: {url}")

    history.append({
        "date": today(),
        "topic": meta["topic"],
        "title": meta["title"],
        "video_id": video_id,
        "url": url,
    })
    os.makedirs(os.path.dirname(HISTORY_PATH), exist_ok=True)
    with open(HISTORY_PATH, "w") as f:
        json.dump(history, f, indent=2)
        f.write("\n")
    print(f"recorded in {HISTORY_PATH}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Smart Tech Daily pipeline")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("preflight", help="verify credentials without spending anything")

    p_gen = sub.add_parser("generate", help="generate a video from a prompt file")
    p_gen.add_argument("--prompt-file", required=True)
    p_gen.add_argument("--out", required=True)

    p_up = sub.add_parser("upload", help="upload a video and record it in history")
    p_up.add_argument("--video", required=True)
    p_up.add_argument("--metadata", required=True)

    args = parser.parse_args()
    {"preflight": cmd_preflight, "generate": cmd_generate, "upload": cmd_upload}[args.command](args)


if __name__ == "__main__":
    main()
