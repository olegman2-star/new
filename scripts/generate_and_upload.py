import json
import os
import sys
import time
import datetime
import tempfile
import requests
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

HIGGSFIELD_API_BASE = "https://api.higgsfield.ai/v1"
YOUTUBE_SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


def load_prompts():
    config_path = os.path.join(os.path.dirname(__file__), "..", "config", "prompts.json")
    with open(config_path) as f:
        return json.load(f)


def pick_prompt(prompts):
    day_of_year = datetime.datetime.utcnow().timetuple().tm_yday
    return prompts[day_of_year % len(prompts)]


def generate_video(prompt: str, api_key: str) -> str:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "prompt": prompt,
        "aspect_ratio": "16:9",
        "duration": 6,
    }
    resp = requests.post(
        f"{HIGGSFIELD_API_BASE}/video/generate",
        headers=headers,
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    job_id = data.get("id") or data.get("job_id")
    if not job_id:
        raise RuntimeError(f"No job_id in response: {data}")
    print(f"Video generation started, job_id={job_id}")
    return poll_for_video(job_id, headers)


def poll_for_video(job_id: str, headers: dict, max_wait: int = 600) -> str:
    deadline = time.time() + max_wait
    while time.time() < deadline:
        resp = requests.get(
            f"{HIGGSFIELD_API_BASE}/video/{job_id}",
            headers=headers,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        status = data.get("status", "")
        print(f"  status={status}")
        if status == "completed":
            url = data.get("url") or data.get("video_url")
            if not url:
                raise RuntimeError(f"Completed but no video URL: {data}")
            return url
        if status in ("failed", "error"):
            raise RuntimeError(f"Video generation failed: {data}")
        time.sleep(15)
    raise TimeoutError(f"Video not ready after {max_wait}s")


def download_video(url: str, dest_path: str):
    resp = requests.get(url, stream=True, timeout=120)
    resp.raise_for_status()
    with open(dest_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=8192):
            f.write(chunk)
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


def upload_to_youtube(service, video_path: str, title: str, description: str):
    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": ["tech", "AI", "technology", "smart tech", "daily tech"],
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


def get_kronos_entry():
    if not os.environ.get("KRONOS_ENABLED"):
        return None
    try:
        from kronos_forecast import build_video_entry, run_kronos_forecast  # noqa: PLC0415
        forecast = run_kronos_forecast(pred_len=24)
        return build_video_entry(forecast)
    except Exception as exc:
        print(f"Kronos forecast failed, falling back to topic rotation: {exc}", file=sys.stderr)
        return None


def main():
    api_key = os.environ.get("HIGGSFIELD_API_KEY")
    if not api_key:
        print("ERROR: HIGGSFIELD_API_KEY is not set", file=sys.stderr)
        sys.exit(1)

    entry = get_kronos_entry()
    if entry is None:
        prompts = load_prompts()
        entry = pick_prompt(prompts)
    prompt = entry["prompt"]
    topic = entry["topic"]
    print(f"Today's topic: {topic}")
    print(f"Prompt: {prompt}")

    print("Generating video...")
    video_url = generate_video(prompt, api_key)
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
    main()
