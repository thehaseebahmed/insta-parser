import asyncio
import logging
import re
import shutil
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException

from . import config, pipeline
from .schemas import (
    AudioResponse,
    DeleteResponse,
    DownloadResponse,
    FramesResponse,
    HealthResponse,
    JobRequest,
    JobStatus,
    OcrResponse,
    PlacesResponse,
    ProcessResponse,
    TranscribeResponse,
    UrlRequest,
)

logging.basicConfig(
    level=config.LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("insta_parser")

JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")
SWEEP_INTERVAL_SECONDS = 3600

# Status of /process runs, keyed by job_id. In memory on purpose: this is a
# single-container homelab tool, so a restart losing in-flight state is
# acceptable (and documented in the README).
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()

# Strong references to in-flight /process tasks. asyncio only holds a weak
# reference to a task once its creating coroutine drops it, so a bare
# `asyncio.create_task(...)` here risks the task being garbage-collected
# mid-run; keeping it in this set (and discarding on completion) prevents that.
_background_tasks: set[asyncio.Task] = set()


def sweep_old_jobs() -> int:
    """Delete job folders older than JOB_TTL_HOURS, and evict stale entries from
    the in-memory registry. Returns how many folders were removed."""
    cutoff = time.time() - config.JOB_TTL_HOURS * 3600
    removed = 0

    work_dir = Path(config.WORK_DIR)
    if work_dir.is_dir():
        for job_dir in work_dir.iterdir():
            if not job_dir.is_dir() or not JOB_ID_RE.match(job_dir.name):
                continue
            try:
                if job_dir.stat().st_mtime >= cutoff:
                    continue
                shutil.rmtree(job_dir, ignore_errors=True)
                removed += 1
                logger.info("Swept expired job dir %s", job_dir)
            except OSError as exc:
                logger.warning("Could not sweep %s: %s", job_dir, exc)

    # A finished /process run deletes its own folder, so its registry entry has
    # no directory left to expire with — evict it here or results accumulate
    # in memory for the life of the container.
    with _jobs_lock:
        stale = [
            jid for jid, job in _jobs.items()
            if job.get("status") in ("done", "error") and job.get("updated_at", 0) < cutoff
        ]
        for jid in stale:
            del _jobs[jid]
    if stale:
        logger.info("Evicted %d finished job record(s) from memory", len(stale))
    return removed


async def _sweep_loop():
    while True:
        await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
        try:
            await asyncio.to_thread(sweep_old_jobs)
        except Exception as exc:
            logger.warning("Job sweep failed: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    Path(config.WORK_DIR).mkdir(parents=True, exist_ok=True)
    removed = sweep_old_jobs()
    logger.info(
        "insta-parser started (work_dir=%s, ttl=%sh, swept %d expired job(s))",
        config.WORK_DIR, config.JOB_TTL_HOURS, removed,
    )
    sweeper = asyncio.create_task(_sweep_loop())
    try:
        yield
    finally:
        sweeper.cancel()


app = FastAPI(
    title="insta-parser",
    description=(
        "Turns an Instagram reel/post URL into text: metadata, a spoken-audio "
        "transcript, and OCR of on-screen text, with optional place-metadata "
        "enrichment. See the [README](https://github.com/thehaseebahmed/insta-parser) "
        "for the CLI and agent skill docs."
    ),
    version="0.1.0",
    lifespan=lifespan,
)


def validated_job_id(job_id: str) -> str:
    """Reject anything that isn't one of our generated ids, so a job_id can
    never traverse out of WORK_DIR (e.g. '../../etc')."""
    if not JOB_ID_RE.match(job_id):
        raise HTTPException(status_code=400, detail=f"Malformed job_id: {job_id!r}")
    return job_id


def job_dir_for(job_id: str) -> Path:
    job_dir = Path(config.WORK_DIR) / validated_job_id(job_id)
    if not job_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"Unknown job_id: {job_id}")
    return job_dir


def _http_error(exc: pipeline.PipelineError, fallback_status: int) -> HTTPException:
    status = 400 if isinstance(exc, pipeline.InvalidInputError) else fallback_status
    return HTTPException(status_code=status, detail=str(exc))


def _set_job(job_id: str, **fields) -> None:
    with _jobs_lock:
        _jobs.setdefault(job_id, {}).update(fields, updated_at=time.time())


# /health and GET /jobs are async so they run on the event loop rather than the
# threadpool: background /process runs occupy threadpool workers, and polling
# must stay responsive even when several pipelines are in flight.
@app.get("/health", response_model=HealthResponse, tags=["health"])
async def health():
    """Health check."""
    return {"status": "ok"}


@app.post("/download", response_model=DownloadResponse, tags=["pipeline"])
def download(req: UrlRequest):
    """Fetch an Instagram post/reel and download its video. Synchronous."""
    job_id = uuid.uuid4().hex
    job_dir = Path(config.WORK_DIR) / job_id
    logger.info("job=%s step=download url=%s", job_id, req.url)
    try:
        metadata = pipeline.download_post(req.url, job_dir)
    except pipeline.PipelineError as exc:
        logger.error("job=%s step=download failed: %s", job_id, exc)
        if job_dir.exists():
            shutil.rmtree(job_dir, ignore_errors=True)
        raise _http_error(exc, fallback_status=502)
    return {"job_id": job_id, "metadata": metadata}


@app.post("/extract-audio", response_model=AudioResponse, tags=["pipeline"])
def extract_audio(req: JobRequest):
    """Extract mp3 audio from the job's downloaded video."""
    job_dir = job_dir_for(req.job_id)
    logger.info("job=%s step=extract-audio", req.job_id)
    try:
        audio_path = pipeline.extract_audio(job_dir)
    except pipeline.PipelineError as exc:
        logger.error("job=%s step=extract-audio failed: %s", req.job_id, exc)
        raise _http_error(exc, fallback_status=422)
    return {"job_id": req.job_id, "audio_path": str(audio_path)}


@app.post("/transcribe", response_model=TranscribeResponse, tags=["pipeline"])
def transcribe(req: JobRequest):
    """Transcribe the job's audio with faster-whisper."""
    job_dir = job_dir_for(req.job_id)
    logger.info("job=%s step=transcribe", req.job_id)
    try:
        result = pipeline.transcribe(job_dir)
    except pipeline.PipelineError as exc:
        logger.error("job=%s step=transcribe failed: %s", req.job_id, exc)
        raise _http_error(exc, fallback_status=422)
    return {"job_id": req.job_id, **result}


@app.post("/extract-frames", response_model=FramesResponse, tags=["pipeline"])
def extract_frames(req: JobRequest):
    """Grab scene-change frames from the job's video as PNGs."""
    job_dir = job_dir_for(req.job_id)
    logger.info("job=%s step=extract-frames", req.job_id)
    try:
        frames = pipeline.extract_frames(job_dir)
    except pipeline.PipelineError as exc:
        logger.error("job=%s step=extract-frames failed: %s", req.job_id, exc)
        raise _http_error(exc, fallback_status=422)
    return {"job_id": req.job_id, "frames": [str(f) for f in frames]}


@app.post("/ocr", response_model=OcrResponse, tags=["pipeline"])
def ocr(req: JobRequest):
    """OCR the job's extracted frames, deduping near-identical consecutive results."""
    job_dir = job_dir_for(req.job_id)
    logger.info("job=%s step=ocr", req.job_id)
    try:
        results = pipeline.run_ocr(job_dir)
    except pipeline.PipelineError as exc:
        logger.error("job=%s step=ocr failed: %s", req.job_id, exc)
        raise _http_error(exc, fallback_status=422)
    return {"job_id": req.job_id, "results": results}


@app.post("/extract-places", response_model=PlacesResponse, tags=["pipeline"])
def extract_places(req: JobRequest):
    """Extract place mentions from the job's caption/transcript/OCR text (via
    litellm) and resolve each against the Google Maps Places API. Requires
    /download to have run; /transcribe and /ocr are used if they've run but
    are not required. Returns [] if extraction/Maps aren't configured."""
    job_dir = job_dir_for(req.job_id)
    logger.info("job=%s step=extract-places", req.job_id)
    try:
        places = pipeline.build_places(job_dir)
    except pipeline.PipelineError as exc:
        logger.error("job=%s step=extract-places failed: %s", req.job_id, exc)
        raise _http_error(exc, fallback_status=422)
    return {"job_id": req.job_id, "places": places}


def _run_pipeline(job_id: str, url: str) -> None:
    """The full pipeline, run in the background so /process can return at once."""
    job_dir = Path(config.WORK_DIR) / job_id
    try:
        _set_job(job_id, status="running", step="download")
        metadata = pipeline.download_post(url, job_dir)

        _set_job(job_id, step="extract-audio")
        pipeline.extract_audio(job_dir)

        _set_job(job_id, step="transcribe")
        transcript = pipeline.transcribe(job_dir)

        _set_job(job_id, step="extract-frames")
        pipeline.extract_frames(job_dir)

        _set_job(job_id, step="ocr")
        ocr_results = pipeline.run_ocr(job_dir)

        _set_job(job_id, step="extract-places")
        metadata["places"] = pipeline.build_places(job_dir)

        result = {"metadata": metadata, "transcript": transcript, "ocr_results": ocr_results}
        if not config.KEEP_FILES:
            # The files are about to go away, so don't hand back paths to them.
            result["metadata"] = {k: v for k, v in metadata.items() if k != "video_path"}
            result["ocr_results"] = [
                {k: v for k, v in item.items() if k != "frame"} for item in ocr_results
            ]
        _set_job(job_id, status="done", step=None, result=result, error=None)
        logger.info("job=%s step=process completed", job_id)
    except pipeline.PipelineError as exc:
        logger.error("job=%s step=process failed: %s", job_id, exc)
        _set_job(job_id, status="error", error=str(exc), result=None)
    except Exception as exc:
        logger.exception("job=%s step=process crashed", job_id)
        _set_job(job_id, status="error", error=f"Unexpected error: {exc}", result=None)
    finally:
        if not config.KEEP_FILES and job_dir.exists():
            logger.info("job=%s cleaning up %s", job_id, job_dir)
            shutil.rmtree(job_dir, ignore_errors=True)


@app.post("/process", status_code=202, response_model=ProcessResponse, tags=["pipeline"])
async def process(req: UrlRequest):
    """Kick off the whole pipeline and return immediately. Poll GET /jobs/{job_id}
    for the result — transcription alone can outlast n8n's HTTP timeout.

    Runs as an independent asyncio Task (the sync pipeline itself in a worker
    thread via asyncio.to_thread) rather than a Starlette BackgroundTask, so
    the multi-minute pipeline isn't tied to this request/response's ASGI
    cycle."""
    job_id = uuid.uuid4().hex
    logger.info("job=%s step=process queued url=%s", job_id, req.url)
    _set_job(job_id, status="queued", step=None, result=None, error=None)
    task = asyncio.create_task(asyncio.to_thread(_run_pipeline, job_id, req.url))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return {"job_id": job_id, "status": "queued"}


@app.get("/jobs/{job_id}", response_model=JobStatus, tags=["jobs"])
async def get_job(job_id: str):
    """Get the status/result of a /process job (or a per-step job's file-presence)."""
    validated_job_id(job_id)
    with _jobs_lock:
        job = dict(_jobs.get(job_id, {}))

    if not job:
        # No /process record, but the folder may exist from the per-step endpoints.
        if (Path(config.WORK_DIR) / job_id).is_dir():
            return {"job_id": job_id, "status": "files-only", "step": None,
                    "result": None, "error": None}
        raise HTTPException(status_code=404, detail=f"Unknown job_id: {job_id}")

    return {"job_id": job_id, **job}


@app.delete("/jobs/{job_id}", response_model=DeleteResponse, tags=["jobs"])
def delete_job(job_id: str):
    """Delete a job's files and any in-memory record of it."""
    job_dir = job_dir_for(job_id)
    logger.info("job=%s deleting %s", job_id, job_dir)
    shutil.rmtree(job_dir, ignore_errors=True)
    with _jobs_lock:
        _jobs.pop(job_id, None)
    return {"job_id": job_id, "status": "deleted"}
