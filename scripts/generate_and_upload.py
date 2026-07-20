import argparse
import json
import os
import subprocess
import sys
import time
import datetime
import tempfile
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

HIGGSFIELD_API_BASE = "https://platform.higgsfield.ai"
# documented models: Soul (text-to-image), DoP (image-to-video with camera motion)
HF_IMAGE_MODEL = os.environ.get("HF_IMAGE_MODEL", "higgsfield-ai/soul/standard")
HF_VIDEO_MODEL = os.environ.get("HF_VIDEO_MODEL", "higgsfield-ai/dop/standard")
YOUTUBE_SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


def load_prompts():
    config_path = os.path.join(os.path.dirname(__file__), "..", "config", "prompts.json")
    with open(config_path) as f:
        return json.load(f)


def pick_prompt(prompts):
    day_of_year = datetime.datetime.utcnow().timetuple().tm_yday
    return prompts[day_of_year % len(prompts)]


# Higgsfield sits behind Cloudflare bot protection that bans Python HTTP
# clients (urllib/requests TLS fingerprints) with error 1010. curl's genuine
# signature passes, so all Higgsfield calls shell out to curl.
def hf_curl(method: str, path: str, payload: dict = None) -> dict:
    api_key = os.environ["HIGGSFIELD_API_KEY"]
    api_secret = os.environ["HIGGSFIELD_API_SECRET"]
    cmd = [
        "curl", "-s", "-X", method,
        f"{HIGGSFIELD_API_BASE}/{path}",
        "-H", f"Authorization: Key {api_key}:{api_secret}",
        "-H", "Content-Type: application/json",
        "-H", "Accept: application/json",
        "--max-time", "30",
    ]
    if payload is not None:
        cmd += ["-d", json.dumps(payload)]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise RuntimeError(f"curl failed ({result.returncode}): {result.stderr[:300]}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"Non-JSON response from {path}: {result.stdout[:300]}")


def hf_submit(model_path: str, payload: dict) -> str:
    data = hf_curl("POST", model_path, payload)
    request_id = data.get("request_id") or data.get("id")
    if not request_id:
        raise RuntimeError(f"No request_id in response: {data}")
    print(f"Submitted to {model_path}, request_id={request_id}")
    return request_id


def hf_poll(request_id: str, max_wait: int = 600) -> dict:
    deadline = time.time() + max_wait
    while time.time() < deadline:
        data = hf_curl("GET", f"requests/{request_id}/status")
        status = data.get("status", "")
        print(f"  status={status}")
        if status == "completed":
            return data
        if status in ("failed", "error", "nsfw", "cancelled"):
            raise RuntimeError(f"Generation failed: {data}")
        time.sleep(15)
    raise TimeoutError(f"Request {request_id} not ready after {max_wait}s")


def generate_video(prompt: str) -> str:
    # Stage A: text -> cinematic still (Soul)
    image_req = hf_submit(HF_IMAGE_MODEL, {
        "prompt": prompt,
        "aspect_ratio": "16:9",
        "resolution": "720p",
    })
    image_result = hf_poll(image_req)
    images = image_result.get("images") or []
    if not images or not images[0].get("url"):
        raise RuntimeError(f"Completed but no image URL: {image_result}")
    image_url = images[0]["url"]
    print(f"Image ready: {image_url}")

    # Stage B: still -> video with camera motion (DoP)
    video_req = hf_submit(HF_VIDEO_MODEL, {
        "image_url": image_url,
        "prompt": prompt,
        "duration": 5,
    })
    video_result = hf_poll(video_req)
    video = video_result.get("video") or {}
    url = video.get("url")
    if not url:
        raise RuntimeError(f"Completed but no video URL: {video_result}")
    return url


def download_video(url: str, dest_path: str):
    result = subprocess.run(
        ["curl", "-s", "-L", "--max-time", "300", "-o", dest_path, url],
        capture_output=True, text=True, timeout=320,
    )
    if result.returncode != 0 or not os.path.exists(dest_path) or os.path.getsize(dest_path) == 0:
        raise RuntimeError(f"Download failed ({result.returncode}): {result.stderr[:300]}")
    print(f"Downloaded video to {dest_path}")


def get_youtube_service():
    client_id = os.environ["YOUTUBE_CLIENT_ID"]
    client_secret = os.environ["YOUTUBE_CLIENT_SECRET"]
    refresh_token = os.environ["YOUTUBE_REFRESH_TOKEN"]

    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client_id,
        client_secret=client_secret,
        scopes=YOUTUBE_SCOPES,
    )
    return build("youtube", "v3", credentials=creds)


def upload_to_youtube(service, video_path: str, title: str, description: str, tags=None):
    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": tags or ["tech", "AI", "technology", "smart tech", "daily tech"],
            "categoryId": "28",  # Science & Technology
        },
        "status": {
            "privacyStatus": "public",
            "selfDeclaredMadeForKids": False,
        },
    }
    media = MediaFileUpload(video_path, mimetype="video/mp4", resumable=True)
    request = service.videos().insert(part="snippet,status", body=body, media_body=media)

    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            print(f"  Upload progress: {int(status.progress() * 100)}%")

    video_id = response.get("id")
    print(f"Uploaded: https://www.youtube.com/watch?v={video_id}")
    return video_id


def build_title_and_description(topic: str) -> tuple[str, str]:
    today = datetime.datetime.utcnow().strftime("%B %d, %Y")
    title = f"Smart Tech Daily | {topic} | {today}"
    description = (
        f"Welcome to Smart Tech Daily!\n\n"
        f"Today's topic: {topic}\n\n"
        f"Stay ahead of the curve with daily AI-generated tech insights and visuals. "
        f"Subscribe for your daily dose of the future.\n\n"
        f"#SmartTechDaily #{topic.replace(' ', '')} #AI #Technology #FutureTech"
    )
    return title, description


def require_hf_creds():
    missing = [v for v in ("HIGGSFIELD_API_KEY", "HIGGSFIELD_API_SECRET") if not os.environ.get(v)]
    if missing:
        print(f"ERROR: {', '.join(missing)} not set", file=sys.stderr)
        sys.exit(1)


def cmd_generate(args):
    """Pipeline stage: generate a video from a prompt file and save it locally."""
    require_hf_creds()

    with open(args.prompt_file) as f:
        prompt = f.read().strip()
    print(f"Prompt: {prompt}")

    video_url = generate_video(prompt)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    download_video(video_url, args.out)
    size = os.path.getsize(args.out)
    print(f"OK: {args.out} ({size} bytes)")


def cmd_upload(args):
    """Pipeline stage: upload a local video with metadata from a JSON file."""
    with open(args.metadata) as f:
        meta = json.load(f)

    service = get_youtube_service()
    print(f"Uploading: {meta['title']}")
    video_id = upload_to_youtube(
        service,
        args.video,
        meta["title"],
        meta["description"],
        tags=meta.get("tags"),
    )
    print(json.dumps({"video_id": video_id, "url": f"https://www.youtube.com/watch?v={video_id}"}))


def main():
    require_hf_creds()

    prompts = load_prompts()
    entry = pick_prompt(prompts)
    prompt = entry["prompt"]
    topic = entry["topic"]
    print(f"Today's topic: {topic}")
    print(f"Prompt: {prompt}")

    print("Generating video...")
    video_url = generate_video(prompt)
    print(f"Video URL: {video_url}")

    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        download_video(video_url, tmp_path)

        print("Authenticating with YouTube...")
        service = get_youtube_service()

        title, description = build_title_and_description(topic)
        print(f"Uploading: {title}")
        upload_to_youtube(service, tmp_path, title, description)
    finally:
        os.unlink(tmp_path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Smart Tech Daily video pipeline")
    sub = parser.add_subparsers(dest="command")

    p_gen = sub.add_parser("generate", help="generate a video from a prompt file")
    p_gen.add_argument("--prompt-file", required=True)
    p_gen.add_argument("--out", required=True)

    p_up = sub.add_parser("upload", help="upload a video with metadata JSON")
    p_up.add_argument("--video", required=True)
    p_up.add_argument("--metadata", required=True)

    args = parser.parse_args()
    if args.command == "generate":
        cmd_generate(args)
    elif args.command == "upload":
        cmd_upload(args)
    else:
        # legacy mode: rotate through config/prompts.json and do the full run
        main()
