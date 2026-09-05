"""Pydantic request/response models for the HTTP API.

These are the response contract: FastAPI validates and serializes every
endpoint's return dict against the matching model here, so a pipeline field
that isn't declared on the model is silently dropped from the response, and
an endpoint that returns an undeclared shape fails loudly. Adding a field to
a pipeline result means adding it here too.

Fields are optional (`| None` / defaulted) because a partially-populated
result — e.g. metadata Instagram rate-limited us out of — still needs to
serialize rather than error.
"""

from pydantic import BaseModel, Field


class UrlRequest(BaseModel):
    url: str = Field(..., examples=["https://www.instagram.com/reel/ABC123xyz/"])


class JobRequest(BaseModel):
    job_id: str = Field(..., examples=["3f1c9a2b8e7d4c6fa1b2c3d4e5f6a7b8"])


class HealthResponse(BaseModel):
    status: str


class Place(BaseModel):
    name: str | None = None
    city: str | None = None
    country: str | None = None
    rating: float | None = None
    maps_url: str | None = None


class MediaItem(BaseModel):
    """One item of a post's media manifest, as returned by /download — index
    1 for a plain reel/photo, 1..N in carousel order for a sidecar post.
    `path` is a real, current file location: per-step job files aren't
    cleaned up until deleted or swept."""

    index: int | None = None
    type: str | None = None  # "video" or "image"
    path: str | None = None


class Metadata(BaseModel):
    shortcode: str | None = None
    username: str | None = None
    caption: str | None = None
    timestamp: str | None = None
    like_count: int | None = None
    comment_count: int | None = None
    location: str | None = None


class DownloadResponse(BaseModel):
    job_id: str
    metadata: Metadata
    media: list[MediaItem] = []


class MediaAudio(BaseModel):
    index: int | None = None
    path: str | None = None


class AudioResponse(BaseModel):
    job_id: str
    # One entry per video item; empty for an image-only post.
    media: list[MediaAudio] = []


class TranscriptSegment(BaseModel):
    start: float | None = None
    end: float | None = None
    text: str | None = None


class Transcript(BaseModel):
    text: str | None = None
    segments: list[TranscriptSegment] = []
    language: str | None = None


class MediaTranscript(Transcript):
    index: int | None = None


class TranscribeResponse(BaseModel):
    job_id: str
    # One entry per video item; empty for an image-only post.
    media: list[MediaTranscript] = []


class MediaFrames(BaseModel):
    index: int | None = None
    type: str | None = None
    frames: list[str] = []


class FramesResponse(BaseModel):
    job_id: str
    media: list[MediaFrames] = []


class OcrResult(BaseModel):
    # Omitted from /process results when KEEP_FILES=false, same as
    # MediaItem.path.
    frame: str | None = None
    text: str | None = None
    confidence: float | None = None


class MediaOcr(BaseModel):
    index: int | None = None
    type: str | None = None
    results: list[OcrResult] = []


class OcrResponse(BaseModel):
    job_id: str
    media: list[MediaOcr] = []


class ProcessResponse(BaseModel):
    job_id: str
    status: str


class ProcessOcrResult(BaseModel):
    """Same as OcrResult but without `frame` — /process deletes the job's
    files by default, so a frame path in this response would either always
    be null or a debug-only value; use the per-step /ocr endpoint instead if
    you need frame paths."""

    text: str | None = None
    confidence: float | None = None


class MediaResult(BaseModel):
    """One media item's full result, as returned by /process — the join of
    the download manifest, its transcript (video items only), and its OCR.
    No `path`, for the same reason as ProcessOcrResult.frame above."""

    index: int | None = None
    type: str | None = None
    transcript: Transcript | None = None
    ocr: list[ProcessOcrResult] = []


class ProcessResult(BaseModel):
    metadata: Metadata | None = None
    media: list[MediaResult] = []
    places: list[Place] = []


class JobStatus(BaseModel):
    job_id: str
    status: str
    step: str | None = None
    result: ProcessResult | None = None
    error: str | None = None
    updated_at: float | None = None


class DeleteResponse(BaseModel):
    job_id: str
    status: str
