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


class Metadata(BaseModel):
    shortcode: str | None = None
    username: str | None = None
    caption: str | None = None
    timestamp: str | None = None
    like_count: int | None = None
    comment_count: int | None = None
    location: str | None = None
    # Omitted from /process results when KEEP_FILES=false, since the file no
    # longer exists by the time the caller sees the result.
    video_path: str | None = None


class DownloadResponse(BaseModel):
    job_id: str
    metadata: Metadata


class AudioResponse(BaseModel):
    job_id: str
    audio_path: str


class TranscriptSegment(BaseModel):
    start: float | None = None
    end: float | None = None
    text: str | None = None


class Transcript(BaseModel):
    text: str | None = None
    segments: list[TranscriptSegment] = []
    language: str | None = None


class TranscribeResponse(Transcript):
    job_id: str


class FramesResponse(BaseModel):
    job_id: str
    frames: list[str] = []


class OcrResult(BaseModel):
    # Omitted from /process results when KEEP_FILES=false, same as
    # Metadata.video_path.
    frame: str | None = None
    text: str | None = None
    confidence: float | None = None


class OcrResponse(BaseModel):
    job_id: str
    results: list[OcrResult] = []


class ProcessResponse(BaseModel):
    job_id: str
    status: str


class ProcessResult(BaseModel):
    metadata: Metadata | None = None
    transcript: Transcript | None = None
    ocr_results: list[OcrResult] = []
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
