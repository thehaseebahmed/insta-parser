"""Core processing steps: download, audio extraction, transcription, frame
extraction, and OCR. Kept separate from main.py so each step is a plain
function that FastAPI endpoints (and the /process orchestrator) can call
directly, without going through HTTP.

A post is a *list* of media items (one for a plain reel or photo, up to ten for
a carousel), so every step after /download iterates the manifest written into
metadata.json and keys its own artifacts off each item's 1-based index. Video
items get audio + a transcript + scene-change frames; image items get a single
normalised frame. Both end up as OCR input.
"""

import json
import logging
import re
import subprocess
import threading
from pathlib import Path

import instaloader
import pytesseract
from PIL import Image, ImageOps
from rapidfuzz import fuzz

from . import config
from . import places as places_module

logger = logging.getLogger("insta_parser")

SHORTCODE_RE = re.compile(r"instagram\.com/(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)")


class PipelineError(Exception):
    """A user-facing pipeline failure (rate-limited, ffmpeg error, ...)."""


class InvalidInputError(PipelineError):
    """Bad input from the caller — surfaces as a 400 rather than a 502."""


def extract_shortcode(url: str) -> str:
    match = SHORTCODE_RE.search(url)
    if not match:
        raise InvalidInputError(
            f"Could not find an Instagram shortcode in URL: {url!r} "
            "(expected something like instagram.com/p/<code>/ or /reel/<code>/)"
        )
    return match.group(1)


_MEDIA_FILE_RE = re.compile(r"^media(?:_(\d+))?\.(mp4|jpe?g|png|webp)$", re.IGNORECASE)
_VIDEO_EXTS = {"mp4"}


def _collect_media(job_dir: Path) -> list[dict]:
    """Find the file(s) instaloader dropped for this post, rename them to
    media_01.<ext>, media_02.<ext>... in carousel order, and classify each as
    "video" or "image". A single-item post (reel or photo) still gets media_01
    — there is no unnumbered alias, per the clean-break file layout."""
    candidates = []
    for path in job_dir.iterdir():
        match = _MEDIA_FILE_RE.match(path.name)
        if not match:
            continue
        # A lone reel/photo has no _N suffix at all; treat that as position 1
        # rather than sorting it ahead of/behind numbered siblings by name.
        position = int(match.group(1)) if match.group(1) else 1
        candidates.append((position, path, match.group(2).lower()))

    if not candidates:
        raise PipelineError(f"Instaloader reported success but no media file was found in {job_dir}")

    # Sort numerically (not lexically) so media_10 doesn't land before media_2.
    candidates.sort(key=lambda c: c[0])

    media = []
    for index, (_, path, ext) in enumerate(candidates, start=1):
        ext = "jpg" if ext == "jpeg" else ext
        media_type = "video" if ext in _VIDEO_EXTS else "image"
        new_path = job_dir / f"media_{index:02d}.{ext}"
        if path != new_path:
            path = path.rename(new_path)
        media.append({"index": index, "type": media_type, "path": str(path)})
    return media


def download_post(url: str, job_dir: Path) -> dict:
    shortcode = extract_shortcode(url)
    job_dir.mkdir(parents=True, exist_ok=True)

    loader = instaloader.Instaloader(
        dirname_pattern=str(job_dir),
        filename_pattern="media",
        download_videos=True,
        download_video_thumbnails=False,
        download_pictures=True,
        download_geotags=False,
        download_comments=False,
        save_metadata=False,
        post_metadata_txt_pattern="",
        quiet=True,
    )

    if config.IG_USERNAME and config.IG_SESSION_FILE:
        try:
            loader.load_session_from_file(config.IG_USERNAME, config.IG_SESSION_FILE)
            logger.info("Loaded Instagram session for %s", config.IG_USERNAME)
        except Exception as exc:
            logger.warning("Could not load IG session (%s), continuing anonymously", exc)

    try:
        post = instaloader.Post.from_shortcode(loader.context, shortcode)
    except instaloader.exceptions.InstaloaderException as exc:
        logger.error("Failed to fetch post %s: %s", shortcode, exc)
        raise PipelineError(
            f"Failed to fetch Instagram post '{shortcode}' "
            f"(private/removed post, or rate-limited?): {exc}"
        ) from exc

    try:
        loader.download_post(post, target=shortcode)
    except instaloader.exceptions.InstaloaderException as exc:
        logger.error("Failed to download media for %s: %s", shortcode, exc)
        raise PipelineError(f"Failed to download media for '{shortcode}': {exc}") from exc

    media = _collect_media(job_dir)
    try:
        expected = post.mediacount
    except instaloader.exceptions.InstaloaderException:
        expected = len(media)
    if expected and len(media) != expected:
        logger.warning(
            "Post %s reported %d media item(s) but only %d were downloaded",
            shortcode, expected, len(media),
        )

    # Every field below is a lazy property that can hit the network, so any of
    # them may fail with a rate-limit error even though the media downloaded.
    try:
        location = post.location.name if post.location else None
    except instaloader.exceptions.InstaloaderException as exc:
        logger.warning("Could not fetch location for %s: %s", shortcode, exc)
        location = None

    try:
        username = post.owner_username
    except instaloader.exceptions.InstaloaderException as exc:
        logger.warning("Could not fetch owner username for %s: %s", shortcode, exc)
        username = "Unknown"

    try:
        metadata = {
            "shortcode": shortcode,
            "username": username,
            "caption": post.caption or "",
            "timestamp": post.date_utc.isoformat(),
            "like_count": post.likes,
            "comment_count": post.comments,
            "location": location,
            "media": media,
        }
    except instaloader.exceptions.InstaloaderException as exc:
        logger.error("Failed to read metadata for %s: %s", shortcode, exc)
        raise PipelineError(f"Failed to read metadata for '{shortcode}': {exc}") from exc

    (job_dir / "metadata.json").write_text(json.dumps(metadata, indent=2))
    return metadata


def _media_manifest(job_dir: Path) -> list[dict]:
    """Read the media manifest /download wrote into metadata.json. Every step
    after /download keys off this rather than re-globbing job_dir, so a step
    called out of order fails with a clear "call /download first" instead of
    silently finding nothing."""
    metadata_path = job_dir / "metadata.json"
    if metadata_path.exists():
        media = json.loads(metadata_path.read_text()).get("media") or []
        if media:
            return media
    raise PipelineError(f"No downloaded media for this job (expected {metadata_path}); call /download first")


def _run_ffmpeg(cmd: list[str], step: str) -> None:
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=config.FFMPEG_TIMEOUT
        )
    except FileNotFoundError:
        logger.error("ffmpeg binary not found on PATH")
        raise PipelineError(
            "ffmpeg is not installed in this container — the image should install it via apt"
        )
    except subprocess.TimeoutExpired:
        logger.error("ffmpeg %s timed out after %ss (cmd=%s)", step, config.FFMPEG_TIMEOUT, " ".join(cmd))
        raise PipelineError(
            f"ffmpeg timed out after {config.FFMPEG_TIMEOUT}s during {step} "
            "(raise FFMPEG_TIMEOUT if this video is genuinely long)"
        )
    if result.returncode != 0:
        stderr_tail = "\n".join(result.stderr.strip().splitlines()[-10:])
        logger.error("ffmpeg %s failed (cmd=%s):\n%s", step, " ".join(cmd), stderr_tail)
        raise PipelineError(f"ffmpeg failed during {step}: {stderr_tail or 'unknown error'}")


def extract_audio(job_dir: Path) -> list[dict]:
    """Extract mp3 audio for every video item in the post. Image items have no
    audio; a post with no video items at all is a valid 200 no-op rather than
    an error, so /extract-audio and /transcribe can be called unconditionally
    in a fixed step order regardless of post type."""
    media = _media_manifest(job_dir)
    video_items = [item for item in media if item["type"] == "video"]
    if not video_items:
        return []

    audio_dir = job_dir / "audio"
    audio_dir.mkdir(exist_ok=True)
    results = []
    for item in video_items:
        audio_path = audio_dir / f"media_{item['index']:02d}.mp3"
        _run_ffmpeg(
            ["ffmpeg", "-y", "-i", item["path"], "-vn", "-acodec", "libmp3lame", str(audio_path)],
            step=f"audio extraction (media_{item['index']:02d})",
        )
        results.append({"index": item["index"], "path": str(audio_path)})
    return results


def extract_frames(job_dir: Path) -> list[dict]:
    """Produce OCR-ready frames for every media item: scene-change frames for
    a video (capped at MAX_FRAMES per item), a single normalised frame for an
    image."""
    media = _media_manifest(job_dir)
    frames_root = job_dir / "frames"
    frames_root.mkdir(exist_ok=True)

    results = []
    for item in media:
        index, media_type, path = item["index"], item["type"], item["path"]
        item_dir = frames_root / f"media_{index:02d}"
        item_dir.mkdir(exist_ok=True)

        if media_type == "video":
            # -vsync vfr keeps only the frames the select filter passes. Newer
            # ffmpeg builds prefer -fps_mode vfr but still accept -vsync with
            # a warning.
            _run_ffmpeg(
                [
                    "ffmpeg", "-y", "-i", path,
                    "-vf", f"select='gt(scene,{config.SCENE_THRESHOLD})',scale=-2:{config.FRAME_SCALE_HEIGHT}",
                    "-vsync", "vfr",
                    "-frames:v", str(config.MAX_FRAMES),
                    "-an",
                    str(item_dir / "frame_%04d.png"),
                ],
                step=f"frame extraction (media_{index:02d})",
            )
            frames = sorted(item_dir.glob("frame_*.png"))
            if len(frames) >= config.MAX_FRAMES:
                logger.warning(
                    "media_%02d hit the MAX_FRAMES cap (%d); later scene changes were dropped",
                    index, config.MAX_FRAMES,
                )
        else:
            # A still image contributes exactly one frame — no scene-count
            # motivation to downscale it like video frames, and native
            # resolution helps OCR read small overlay text.
            _run_ffmpeg(
                ["ffmpeg", "-y", "-i", path, "-frames:v", "1", str(item_dir / "frame_0001.png")],
                step=f"frame extraction (media_{index:02d})",
            )
            frames = sorted(item_dir.glob("frame_*.png"))

        results.append({"index": index, "type": media_type, "frames": [str(f) for f in frames]})
    return results


_whisper_model = None
_whisper_model_lock = threading.Lock()
# Sync endpoints run in FastAPI's threadpool, so this bounds how many
# transcriptions compete for CPU at once.
_transcribe_semaphore = threading.Semaphore(config.TRANSCRIBE_CONCURRENCY)


def _get_whisper_model():
    global _whisper_model
    # Double-checked locking: without the lock two threadpool workers can each
    # build a WhisperModel (hundreds of MB) and one of them is then orphaned.
    if _whisper_model is None:
        with _whisper_model_lock:
            if _whisper_model is None:
                from faster_whisper import WhisperModel

                logger.info(
                    "Loading faster-whisper model '%s' (device=%s, compute_type=%s)",
                    config.WHISPER_MODEL, config.WHISPER_DEVICE, config.WHISPER_COMPUTE_TYPE,
                )
                _whisper_model = WhisperModel(
                    config.WHISPER_MODEL,
                    device=config.WHISPER_DEVICE,
                    compute_type=config.WHISPER_COMPUTE_TYPE,
                )
    return _whisper_model


def transcribe(job_dir: Path) -> dict:
    """Transcribe every video item's audio. A post with no video items is a
    valid 200 no-op (empty media list), matching /extract-audio."""
    media = _media_manifest(job_dir)
    video_items = [item for item in media if item["type"] == "video"]
    if not video_items:
        result = {"media": []}
        (job_dir / "transcript.json").write_text(json.dumps(result, indent=2))
        return result

    model = _get_whisper_model()
    items = []
    for item in video_items:
        index = item["index"]
        audio_path = job_dir / "audio" / f"media_{index:02d}.mp3"
        if not audio_path.exists():
            raise PipelineError(
                f"No extracted audio for this job (expected {audio_path}); call /extract-audio first"
            )

        try:
            with _transcribe_semaphore:
                segments_gen, info = model.transcribe(
                    str(audio_path),
                    vad_filter=config.WHISPER_VAD_FILTER,
                    # Disabled regardless of VAD: once a window hallucinates, conditioning
                    # on its (bad) text can drag subsequent windows into a repetition
                    # loop. Short reels don't need cross-window consistency badly enough
                    # to be worth that risk.
                    condition_on_previous_text=False,
                )
                # transcribe() returns a lazy generator; it must be drained inside
                # the semaphore or the real work happens after we release it.
                segments = [
                    {"start": seg.start, "end": seg.end, "text": seg.text.strip()}
                    for seg in segments_gen
                ]
        except Exception as exc:
            logger.error("Transcription failed for %s (media_%02d): %s", job_dir, index, exc)
            raise PipelineError(f"Transcription failed: {exc}") from exc

        items.append({
            "index": index,
            "text": " ".join(seg["text"] for seg in segments).strip(),
            "segments": segments,
            "language": info.language,
        })

    result = {"media": items}
    (job_dir / "transcript.json").write_text(json.dumps(result, indent=2))
    return result


_OCR_PSM_MODES = ("--psm 3", "--psm 11")  # 3 = fully automatic; 11 = sparse text
# Tesseract accuracy drops sharply on small stylized text; a video frame that
# was downscaled for frame-count reasons, or a small source image, both land
# under this height often enough to be worth recovering.
_OCR_UPSCALE_BELOW_HEIGHT = 1000
_OCR_UPSCALE_TARGET_HEIGHT = 1500
_OCR_MAX_UPSCALE = 2.0


def _preprocess_for_ocr(img: Image.Image) -> Image.Image:
    """Grayscale + autocontrast, and upscale a small frame — cheap
    preprocessing that noticeably helps Tesseract on stylized/low-contrast
    social media text overlays."""
    img = img.convert("L")
    img = ImageOps.autocontrast(img, cutoff=1)
    if img.height < _OCR_UPSCALE_BELOW_HEIGHT:
        scale = min(_OCR_MAX_UPSCALE, _OCR_UPSCALE_TARGET_HEIGHT / img.height)
        img = img.resize((round(img.width * scale), round(img.height * scale)), Image.LANCZOS)
    return img


def _score_ocr_data(data: dict) -> tuple[str, float]:
    # Pair each word with its own confidence; tesseract emits rows for
    # whitespace-only boxes whose low confidence would skew the average.
    scored = [
        (word.strip(), float(conf))
        for word, conf in zip(data["text"], data["conf"])
        if word.strip() and str(conf) != "-1"
    ]
    if not scored:
        return "", 0.0

    text = " ".join(word for word, _ in scored)
    confidence = round(sum(conf for _, conf in scored) / len(scored), 1)
    return text, confidence


def _ocr_frame(frame: Path) -> tuple[str, float]:
    """OCR one frame, returning its text and mean word confidence. Tries a
    couple of Tesseract page-segmentation modes on the preprocessed frame and
    keeps whichever scores higher; a result below OCR_MIN_CONFIDENCE is
    treated as no text rather than surfaced as noise."""
    # Image.open is lazy: PIL releases the fd once the image is fully read, but
    # not if the consumer raises first. The context manager makes it deterministic.
    with Image.open(frame) as img:
        img = _preprocess_for_ocr(img)
        best_text, best_confidence = "", 0.0
        for psm in _OCR_PSM_MODES:
            data = pytesseract.image_to_data(img, config=psm, output_type=pytesseract.Output.DICT)
            text, confidence = _score_ocr_data(data)
            if confidence > best_confidence:
                best_text, best_confidence = text, confidence

    if best_confidence < config.OCR_MIN_CONFIDENCE:
        return "", 0.0
    return best_text, best_confidence


def run_ocr(job_dir: Path) -> list[dict]:
    media = _media_manifest(job_dir)
    frames_root = job_dir / "frames"

    results = []
    for item in media:
        index, media_type = item["index"], item["type"]
        item_dir = frames_root / f"media_{index:02d}"
        frames = sorted(item_dir.glob("frame_*.png"))
        if not frames:
            raise PipelineError(
                f"No extracted frames for this job (expected files under {item_dir}); call /extract-frames first"
            )

        item_results = []
        # Reset per item: two different carousel slides that happen to carry
        # the same text should both survive, since attribution to a specific
        # slide is the point — only consecutive frames *within* one item are
        # deduped.
        last_text = None
        for frame in frames:
            try:
                text, confidence = _ocr_frame(frame)
            except pytesseract.TesseractNotFoundError:
                # Not a per-frame problem: every frame would fail the same way, and
                # swallowing it would return an empty result set as if it succeeded.
                logger.error("tesseract binary not found on PATH")
                raise PipelineError(
                    "tesseract is not installed in this container — the image should install it via apt"
                )
            except Exception as exc:
                logger.warning("Tesseract failed on %s: %s", frame, exc)
                continue

            if not text:
                continue
            if last_text is not None and fuzz.ratio(text, last_text) >= config.OCR_DEDUPE_THRESHOLD:
                continue

            item_results.append({"frame": str(frame), "text": text, "confidence": confidence})
            last_text = text

        results.append({"index": index, "type": media_type, "results": item_results})

    (job_dir / "ocr.json").write_text(json.dumps(results, indent=2))
    return results


def build_places(job_dir: Path) -> list[dict]:
    metadata_path = job_dir / "metadata.json"
    if not metadata_path.exists():
        raise PipelineError(f"No downloaded metadata for this job (expected {metadata_path}); call /download first")
    metadata = json.loads(metadata_path.read_text())

    transcript_path = job_dir / "transcript.json"
    transcript = json.loads(transcript_path.read_text()) if transcript_path.exists() else None

    ocr_path = job_dir / "ocr.json"
    ocr_results = json.loads(ocr_path.read_text()) if ocr_path.exists() else None

    places = places_module.build_places_metadata(metadata, transcript, ocr_results)
    (job_dir / "places.json").write_text(json.dumps(places, indent=2))
    return places
