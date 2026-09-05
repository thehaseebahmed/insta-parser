# insta-parser

Small FastAPI microservice that downloads an Instagram post/reel, extracts
its audio + candidate frames, transcribes the audio (faster-whisper), and
OCRs the frames (Tesseract). Built to be called from n8n via HTTP.

Each job gets its own subfolder under `WORK_DIR` (a random `job_id`), so the
per-step endpoints can be called independently and re-run without stepping
on each other.

Agent-facing docs live in [`skills/insta-parser/`](skills/insta-parser/) —
`SKILL.md` for calling the service, `reference/operations.md` for running it.

## Endpoints

| Method | Path | Notes |
|---|---|---|
| `GET` | `/health` | Health check |
| `POST` | `/download` | Fetch a post and download its video. Synchronous. |
| `POST` | `/extract-audio` | Extract mp3 audio from the downloaded video |
| `POST` | `/transcribe` | Transcribe the audio with faster-whisper |
| `POST` | `/extract-frames` | Grab scene-change frames as PNGs |
| `POST` | `/ocr` | OCR the extracted frames, deduping near-identical results |
| `POST` | `/process` | **Async.** Queues the full pipeline, returns `202` + a `job_id`. Includes place-metadata enrichment (optional, see below) |
| `GET` | `/jobs/{job_id}` | Status/result of a `/process` run |
| `DELETE` | `/jobs/{job_id}` | Delete a job's files |

The per-step endpoints are synchronous — each finishes well inside a normal
HTTP timeout. `/process` is not: whisper transcription alone can run for
minutes, so it returns immediately and you poll `/jobs/{job_id}`.

Interactive API docs are served by FastAPI directly from the same request/
response models used above: Swagger UI at `/docs` (try requests against a
running instance right from the browser), ReDoc at `/redoc`, and the raw
OpenAPI schema at `/openapi.json`.

## Configuration (env vars)

| Var | Default | Notes |
|---|---|---|
| `WORK_DIR` | `/data` | Root dir for per-job files, mount as a volume |
| `IG_USERNAME` | unset | Instagram username for an authenticated session |
| `IG_SESSION_FILE` | unset | Path to an instaloader session file (see below) |
| `KEEP_FILES` | `false` | Keep job files after `/process` finishes |
| `JOB_TTL_HOURS` | `24` | Job folders older than this are swept on startup and hourly |
| `WHISPER_MODEL` | `base` | faster-whisper model size (`tiny`, `base`, `small`, ...) |
| `WHISPER_DEVICE` | `cpu` | faster-whisper device |
| `WHISPER_COMPUTE_TYPE` | `int8` | faster-whisper compute type |
| `WHISPER_VAD_FILTER` | `true` | Skip non-speech audio (silence, music, ASMR mouth sounds) via Silero VAD before decoding, to avoid whisper hallucinating text on those stretches |
| `TRANSCRIBE_CONCURRENCY` | `1` | How many transcriptions may run at once |
| `FFMPEG_TIMEOUT` | `300` | Per-invocation ffmpeg timeout, seconds |
| `MAX_FRAMES` | `60` | Cap on extracted frames per video |
| `FRAME_SCALE_HEIGHT` | `720` | Frames are downscaled to this height before OCR |
| `SCENE_THRESHOLD` | `0.3` | ffmpeg scene-change sensitivity (higher = fewer frames) |
| `OCR_DEDUPE_THRESHOLD` | `90` | rapidfuzz similarity (0-100) above which consecutive OCR text is dropped as a duplicate |
| `LOG_LEVEL` | `INFO` | Python logging level |
| `LITELLM_BASE_URL` | unset | Base URL of a litellm proxy, **without** a `/v1` suffix (e.g. `http://172.17.0.1:4000`) |
| `LITELLM_API_KEY` | unset | Bearer key for the litellm proxy, if it requires one |
| `LITELLM_MODEL` | unset | Model alias already registered in litellm (e.g. a local Ollama model) to use for place extraction |
| `LITELLM_TIMEOUT` | `60` | Timeout (seconds) for the litellm extraction call |
| `PLACE_EXTRACTION_MAX_CHARS` | `4000` | How much combined caption/transcript/OCR text to send to the model |
| `GOOGLE_MAPS_API_KEY` | unset | Google Maps Places API (New) key, used to resolve extracted places to a rating + Maps URL |
| `GOOGLE_MAPS_TIMEOUT` | `10` | Timeout (seconds) for each Places API lookup |

### Optional authenticated Instagram session

Anonymous access works for public posts but is more likely to get
rate-limited. To use a logged-in session:

```bash
pip install instaloader
instaloader --login=<your_ig_username>
# creates ~/.config/instaloader/session-<your_ig_username>
```

Copy that session file into the `WORK_DIR` volume (so it persists) and set:

```yaml
environment:
    IG_USERNAME: "your_ig_username"
    IG_SESSION_FILE: "/data/session-your_ig_username"
```

## Running

### From the published image

```bash
docker run -d --name insta-parser -p 8420:8000 \
  -v ${HOME}/volumes/insta-parser/data:/data \
  -e TZ=Europe/Amsterdam \
  ghcr.io/thehaseebahmed/insta-parser:main
```

`:main` tracks the latest commit on `main`. For a stable deployment, pin a
released version instead (`:X.Y.Z` — see [Releases](#releases)); there is no
`:latest` tag.

Or use a `docker-compose.yaml` pointing `image:` at
`ghcr.io/thehaseebahmed/insta-parser:<version>` — see this repo's own
[`docker-compose.yaml`](docker-compose.yaml) for the env var layout (it builds
locally for development; swap `build:` for a pinned `image:` for deployment).

### From source (development)

```bash
docker compose up -d --build
docker compose logs -f
```

The service listens on `8000` inside the container. It's deliberately not
exposed via a reverse proxy or Tailscale by default — there's no auth on it.

## Releases

Pushing a `vX.Y.Z` tag builds and publishes the Docker image to
`ghcr.io/thehaseebahmed/insta-parser:X.Y.Z`.

Every push to `main` also rebuilds and republishes
`ghcr.io/thehaseebahmed/insta-parser:main` as a rolling image.

Every push to an open PR's branch builds and publishes
`ghcr.io/thehaseebahmed/insta-parser:pr-<number>-<short sha>` (e.g.
`pr-1-460bbe1`) — an image of that exact commit, to pull and test before
merging. See [`.github/workflows/release.yml`](.github/workflows/release.yml).

## Testing

Every pull request runs [`.github/workflows/test.yml`](.github/workflows/test.yml):
the Python test suite, independent of any real Instagram/ffmpeg/tesseract/
whisper calls (those are mocked — no external services, binaries, or
credentials needed).

```bash
# Python (FastAPI app + pipeline + places enrichment)
pip install -r requirements.txt -r requirements-test.txt
pytest
```

## Example usage

```bash
BASE=http://localhost:8420

# Health check
curl -s $BASE/health
# => {"status":"ok"}
```

### All-in-one (async)

```bash
# Queue the job — returns immediately with 202
curl -sX POST $BASE/process \
  -H 'Content-Type: application/json' \
  -d '{"url": "https://www.instagram.com/reel/ABC123xyz/"}'
# => {"job_id": "3f1c...", "status": "queued"}

# Poll until status is "done" (or "error")
curl -s $BASE/jobs/3f1c...
# => {"job_id":"3f1c...","status":"running","step":"transcribe","result":null,"error":null}
# => {"job_id":"3f1c...","status":"done","step":null,"result":{"metadata":{"username":"...", ...},
#     "transcript":{"text":"...","segments":[...]},"ocr_results":[...],
#     "places":[{"name":"Joe's Pizza","city":"Rome","country":"Italy","rating":4.6,
#                "maps_url":"https://maps.google.com/?cid=..."}]},"error":null}
```

In n8n: an HTTP Request node for `POST /process`, then a Wait node, then an
HTTP Request node for `GET /jobs/{{ $json.job_id }}` in a loop with an IF node
checking `status == "done"`.

### Step by step (synchronous)

```bash
# 1. Download
curl -sX POST $BASE/download \
  -H 'Content-Type: application/json' \
  -d '{"url": "https://www.instagram.com/reel/ABC123xyz/"}'
# => {"job_id": "...", "metadata": {"username": "...", "caption": "...", "timestamp": "...", ...}}

JOB_ID=<job_id from above>

# 2. Extract audio
curl -sX POST $BASE/extract-audio \
  -H 'Content-Type: application/json' \
  -d "{\"job_id\": \"$JOB_ID\"}"

# 3. Transcribe
curl -sX POST $BASE/transcribe \
  -H 'Content-Type: application/json' \
  -d "{\"job_id\": \"$JOB_ID\"}"

# 4. Extract frames
curl -sX POST $BASE/extract-frames \
  -H 'Content-Type: application/json' \
  -d "{\"job_id\": \"$JOB_ID\"}"

# 5. OCR
curl -sX POST $BASE/ocr \
  -H 'Content-Type: application/json' \
  -d "{\"job_id\": \"$JOB_ID\"}"

# 6. Clean up when the workflow is done with it
curl -sX DELETE $BASE/jobs/$JOB_ID
# => {"job_id": "...", "status": "deleted"}
```

Place-metadata enrichment (below) only runs as part of `/process` — there's no
per-step endpoint for it.

## Place-metadata enrichment (optional)

If configured, `/process` pulls place mentions — restaurants, landmarks,
cities — out of the reel's caption, tagged location, transcript, and OCR
text, and attaches them as `result.places`: an array of
`{name, city, country, rating, maps_url}`.

This is two independent, optional pieces:

1. **Extraction** goes through a self-hosted `litellm` proxy rather than
   adding a new model dependency to this service — point `LITELLM_MODEL` at
   whatever model alias you've registered there (a local Ollama model or
   otherwise), and `LITELLM_BASE_URL` at the proxy's base URL.
2. **Resolution** looks each extracted place up via the [Google Maps Places
   API (New)](https://developers.google.com/maps/documentation/places/web-service/text-search)
   for a rating and canonical Maps URL. Requires a `GOOGLE_MAPS_API_KEY` with
   that API enabled on its Google Cloud project — each lookup is a billable
   request.

Leave either unset and that piece is simply skipped: no `LITELLM_MODEL` means
extraction doesn't run at all and `places` comes back `[]`; no
`GOOGLE_MAPS_API_KEY` means extracted places still come back but with
`rating`/`maps_url` set to `null`. A failed litellm or Maps call is logged and
treated the same as unconfigured — it never fails `/process`. Since `[]` means
both "not configured" and "configured, found nothing", don't treat an empty
`places` as proof the reel has none — see the
[`insta-parser` skill](skills/insta-parser/SKILL.md) for how to read it.

## Notes

- **Job state is in memory.** A container restart loses the status of any
  in-flight or completed `/process` run (the files on the volume survive).
  Fine for a homelab tool; don't build a long-running workflow that assumes
  otherwise. Finished job records are evicted after `JOB_TTL_HOURS`, so
  fetch a result before then — the same sweep keeps them from accumulating.
- **Cleanup.** `/process` deletes its job folder when it finishes unless
  `KEEP_FILES=true`. The per-step endpoints don't, so either call
  `DELETE /jobs/{job_id}` at the end of your workflow or let the
  `JOB_TTL_HOURS` sweep collect them.
- **Response paths.** When `KEEP_FILES=false`, `/process` omits `video_path`
  and per-frame paths from its result, since those files no longer exist.
- **Concurrency.** Each in-flight `/process` run holds one of FastAPI's
  threadpool workers, and `TRANSCRIBE_CONCURRENCY` (default 1) queues the
  whisper step so parallel jobs don't thrash the CPU. `/health` and
  `GET /jobs/{job_id}` are async, so polling stays responsive no matter how
  many pipelines are running.
- **Error codes.** `400` = bad input (unparseable URL, malformed `job_id`,
  non-video post), `404` = unknown `job_id`, `422` = a step ran out of order
  (e.g. `/ocr` before `/extract-frames`), `502` = Instagram fetch failed
  (rate-limited, private, or removed post). Errors are also logged with the
  `job_id` and step, visible via `docker logs`.
- **ffmpeg deprecation.** Frame extraction uses `-vsync vfr`; ffmpeg 6+ warns
  and prefers `-fps_mode vfr`. Debian's build still accepts `-vsync`.
