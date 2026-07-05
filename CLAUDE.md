# Smart Tech Daily — agent pipeline

Daily AI-generated tech video channel. Video generation is Higgsfield API; upload is YouTube Data API. The daily run is orchestrated by five expert agents (in `.claude/agents/`), each backed by YouTube-creator skills (in `.claude/skills/`).

## Pipeline (run stages in this order, each via its agent)

| Stage | Agent | Reads | Writes |
|-------|-------|-------|--------|
| 1. Research | `researcher` | web trends, `pipeline/history.json` | `pipeline/today/research.md` |
| 2. Write | `scriptwriter` | `research.md` | `pipeline/today/video_prompt.txt`, `metadata.json` |
| 3. Produce | `producer` | `video_prompt.txt` | `pipeline/today/video.mp4`, `thumbnail.md` |
| 4. Publish | `publisher` | `video.mp4`, `metadata.json` | YouTube upload, `pipeline/history.json` |
| 5. Promote | `promoter` | `research.md`, `metadata.json`, `history.json` | `pipeline/today/promotion.md` |

Rules for the orchestrator:
- Stages are strictly sequential; each depends on the previous stage's artifacts.
- If a stage fails, stop the pipeline and report — never upload without a verified video, never promote without a successful upload.
- `pipeline/today/` is recreated each run; `pipeline/history.json` is append-only and is the dedupe source (last 14 topics must not repeat).
- After a successful run, commit `pipeline/history.json` and `pipeline/today/` artifacts (not `video.mp4`) back to the repo.

## Environment

- `HIGGSFIELD_API_KEY` + `HIGGSFIELD_API_SECRET` — video generation via the Higgsfield Cloud API (`platform.higgsfield.ai`, keys from cloud.higgsfield.ai; a separate product from the consumer higgsfield.ai app). Generation is two-stage: Soul text-to-image, then DoP image-to-video. Models overridable via `HF_IMAGE_MODEL` / `HF_VIDEO_MODEL`.
- `YOUTUBE_CLIENT_ID` / `YOUTUBE_CLIENT_SECRET` / `YOUTUBE_REFRESH_TOKEN` — upload (token helper: `scripts/get_youtube_token.py`)
- `ANTHROPIC_API_KEY` — the agent orchestration in CI

## Commands

```bash
pip install -r scripts/requirements.txt
python scripts/generate_and_upload.py generate --prompt-file <f> --out <mp4>   # stage 3
python scripts/generate_and_upload.py upload --video <mp4> --metadata <json>   # stage 4
python scripts/generate_and_upload.py   # legacy: full run off rotating config/prompts.json
```

The legacy mode (`config/prompts.json` rotation, `.github/workflows/daily_youtube.yml` manual dispatch) is the fallback if the agent pipeline is down.
