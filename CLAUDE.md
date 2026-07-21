# Smart Tech Daily — daily video pipeline

Daily AI-generated tech video channel. One GitHub Actions run per day produces a
video and publishes it.

The pipeline is split by what actually needs judgment. Research, writing, and
promotion are creative work and run through Claude. Generating and publishing
are deterministic API calls with one correct outcome, so they are plain Python
steps — an LLM adds cost and failure modes there, not quality.

## The run

| # | Step | Kind | Produces |
|---|------|------|----------|
| 1 | Preflight | script | nothing — validates credentials before anything costs money |
| 2 | Write content | Claude (Sonnet, one pass) | `research.md`, `video_prompt.txt`, `metadata.json`, `promotion.md` |
| 3 | Verify content | script | fails the run if step 2 came up short |
| 4 | Generate video | script | `video.mp4` via Higgsfield |
| 5 | Publish | script | YouTube upload, appends `pipeline/history.json` |
| 6 | Commit | script | commits the text artifacts (never the mp4) |

Step 2 reads the briefs in `.claude/agents/` (`researcher`, `scriptwriter`,
`promoter`) and the skills in `.claude/skills/` those briefs name. Editing a
brief changes the output — that is the intended way to tune the channel's voice.

`pipeline/today/` is rebuilt every run. `pipeline/history.json` is append-only
and is both the dedupe source (the researcher must not repeat the last 14
topics) and the idempotency guard.

## Design rules

- **Preflight before spend.** Credentials are checked while failure is free.
  A dead OAuth token once surfaced only after a 16 MB video had been paid for.
- **Never publish twice.** `preflight` and `upload` both refuse if
  `history.json` already holds today's date; the workflow uses a concurrency
  group so two runs cannot overlap.
- **The paid artifact survives failure.** `video.mp4` is uploaded as a run
  artifact, so a failed publish never forces regenerating the same video.
- **Green means published.** Every step fails loudly; the run only succeeds if
  a video reached YouTube.

## Environment

- `HIGGSFIELD_API_KEY` + `HIGGSFIELD_API_SECRET` — keys from
  **cloud.higgsfield.ai**, a separate product from the consumer higgsfield.ai
  app. Generation is two-stage: Soul text-to-image, then DoP image-to-video.
  Override models with `HF_IMAGE_MODEL` / `HF_VIDEO_MODEL`.
  Calls shell out to curl: Cloudflare rejects Python HTTP clients by TLS
  fingerprint (error 1010) regardless of User-Agent.
- `YOUTUBE_CLIENT_ID` / `YOUTUBE_CLIENT_SECRET` / `YOUTUBE_REFRESH_TOKEN` —
  regenerate the token with `scripts/get_youtube_token.py`. If the Google Cloud
  OAuth consent screen sits in "Testing", refresh tokens expire after 7 days;
  set it to "In production".
- `ANTHROPIC_API_KEY` — step 2 only.

## Commands

```bash
pip install -r scripts/requirements.txt
python scripts/pipeline.py preflight
python scripts/pipeline.py generate --prompt-file <txt> --out <mp4>
python scripts/pipeline.py upload --video <mp4> --metadata <json>
```
