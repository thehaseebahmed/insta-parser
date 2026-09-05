# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Python tests (repo root) — no network, binaries, or credentials needed; all mocked
pip install -r requirements.txt -r requirements-test.txt
pytest
pytest tests/test_pipeline.py::TestExtractShortcode::test_valid_urls   # single test
pytest -k places                                                       # by keyword

# Run the service locally (needs ffmpeg + tesseract, which the image installs)
docker compose up -d --build && docker compose logs -f   # → http://localhost:8420
```

There is no linter or formatter configured. CI (`.github/workflows/test.yml`) runs
`pytest -v` on every PR.

## Architecture

**Two layers, split on purpose.** `app/pipeline.py` holds every processing step as a
plain function taking a `job_dir: Path`; `app/main.py` is a thin HTTP layer that
validates input, calls those functions, and maps exceptions to status codes. The
`/process` orchestrator (`_run_pipeline`) calls the same functions directly rather
than going through HTTP. Put logic in `pipeline.py`, not in an endpoint.

**Steps hand off through files, not return values.** Each job is
`WORK_DIR/<32-hex job_id>/` containing `video.mp4`, `metadata.json`, `audio.mp3`,
`frames/frame_%04d.png`, `transcript.json`, `ocr.json`, `places.json`. A step reads
what its predecessor wrote and raises `PipelineError("... call /download first")` if
it's missing. This is what makes the per-step endpoints independently callable and
re-runnable. A new step should follow the same pattern: read inputs from `job_dir`,
write its own artifact there.

**Error taxonomy drives HTTP status.** `PipelineError` is the user-facing failure
type; `InvalidInputError` subclasses it. `main._http_error` maps `InvalidInputError`
→ 400 and everything else to the caller-supplied fallback: 502 for `/download`
(Instagram fetch failed) and 422 for later steps (ran out of order). 404 = unknown
`job_id`, 400 = malformed one. Raise the right exception type in `pipeline.py`
instead of raising `HTTPException` there.

**Job state is an in-memory dict** (`main._jobs`, guarded by `_jobs_lock`, mutated
only via `_set_job`). A restart loses it. `sweep_old_jobs()` runs at startup and
hourly, deleting expired job folders *and* evicting finished registry entries —
finished `/process` runs delete their own folder, so their record has nothing left to
expire with.

**Sync vs. async endpoints is a deliberate choice, not style.** `def` endpoints run
in FastAPI's threadpool (that's where the blocking pipeline work belongs).
`/health` and `GET /jobs/{job_id}` are `async def` so polling stays responsive while
threadpool workers are occupied by pipelines. `/process` is `async def` and spawns
`asyncio.create_task(asyncio.to_thread(...))`, keeping a strong reference in
`_background_tasks` — a bare task can be garbage-collected mid-run.

**Place enrichment (`app/places.py`) must never fail the pipeline.** Both integrations
(litellm extraction, Google Maps resolution) are independent and optional; unconfigured,
failed, or malformed responses all degrade to `[]` or `rating`/`maps_url` of `None`
with a `logger.warning`. Keep that: it's metadata on top of the core result, not
something callers can depend on.

**`app/schemas.py` is the response contract.** FastAPI validates and serializes every
endpoint's return dict against the matching model here — a pipeline field that isn't
declared on the model is silently dropped from the response. Fields stay optional
(`| None` / defaulted) so a partially-populated result still serializes, but adding a
field to a pipeline dict means adding it to the model too, or it never reaches the
caller.

**`app/config.py` reads env vars at import time.** `tests/test_config.py` uses
`importlib.reload(config)` to test parsing, so read values as `config.X` at call time
rather than `from .config import X`. The one import-time exception is
`pipeline._transcribe_semaphore`, built from `TRANSCRIBE_CONCURRENCY` (clamped to ≥1,
since `Semaphore(0)` would deadlock every transcription).

## Docs that must move with the code

`skills/insta-parser/SKILL.md` is a portable agent skill describing the live
service; `skills/insta-parser/reference/operations.md` covers running/tuning it.
Per `skills/README.md`, any change to the API surface, error codes, or env vars
must update the matching part **in the same commit** — a stale skill is worse
than none, because an agent follows it confidently.

Adding a config var means touching four places: `app/config.py`, the env-var
table in `README.md`, the commented block in `docker-compose.yaml`, and the
tuning table in `skills/insta-parser/reference/operations.md`.

## Releases

Tag `vX.Y.Z` publishes `ghcr.io/thehaseebahmed/insta-parser:X.Y.Z`. Pushes to
`main` republish the rolling `:main` tag; PR pushes publish
`:pr-<number>-<sha>`. There is no `:latest`.
