"""Pydantic request/response models for the HTTP API.

These exist for two reasons: request validation (already true before this
file existed) and generating an accurate OpenAPI schema so Swagger UI
(`/docs`) and ReDoc (`/redoc`) show real response shapes instead of an empty
schema.

Every response model uses `extra="allow"` and makes its fields optional:
they document every field the API is known to return, but the goal is
accurate documentation, not a strict contract — an endpoint's actual dict
is never filtered or rejected just because a field here is missing or a
field it returns isn't declared here.
"""

from pydantic import BaseModel, ConfigDict, Field


class UrlRequest(BaseModel):
    url: str = Field(..., examples=["https://www.instagram.com/reel/ABC123xyz/"])


class JobRequest(BaseModel):
    job_id: str = Field(..., examples=["3f1c9a2b8e7d4c6fa1b2c3d4e5f6a7b8"])


class HealthResponse(BaseModel):
    status: str


class Place(BaseModel):
    model_config = ConfigDict(extra="allow")
    name: str | None = None
    city: str | None = None
    country: str | None = None
    rating: float | None = None
    maps_url: str | None = None


class Metadata(BaseModel):
    model_config = ConfigDict(extra="allow")
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
    # Only present once /extract-places (or /process) has run.
    places: list[Place] | None = None


class DownloadResponse(BaseModel):
    job_id: str
    metadata: Metadata


class AudioResponse(BaseModel):
    job_id: str
    audio_path: str


class TranscriptSegment(BaseModel):
    model_config = ConfigDict(extra="allow")
    start: float | None = None
    end: float | None = None
    text: str | None = None


class Transcript(BaseModel):
    model_config = ConfigDict(extra="allow")
    text: str | None = None
    segments: list[TranscriptSegment] = []
    language: str | None = None


class TranscribeResponse(Transcript):
    job_id: str


class FramesResponse(BaseModel):
    job_id: str
    frames: list[str] = []


class OcrResult(BaseModel):
    model_config = ConfigDict(extra="allow")
    # Omitted from /process results when KEEP_FILES=false, same as
    # Metadata.video_path.
    frame: str | None = None
    text: str | None = None
    confidence: float | None = None


class OcrResponse(BaseModel):
    job_id: str
    results: list[OcrResult] = []


class PlacesResponse(BaseModel):
    job_id: str
    places: list[Place] = []


class ProcessResponse(BaseModel):
    job_id: str
    status: str


class ProcessResult(BaseModel):
    model_config = ConfigDict(extra="allow")
    metadata: Metadata | None = None
    transcript: Transcript | None = None
    ocr_results: list[OcrResult] = []


class JobStatus(BaseModel):
    model_config = ConfigDict(extra="allow")
    job_id: str
    status: str
    step: str | None = None
    result: ProcessResult | None = None
    error: str | None = None
    updated_at: float | None = None


class DeleteResponse(BaseModel):
    job_id: str
    status: str
