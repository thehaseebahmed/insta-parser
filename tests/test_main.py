import time
from unittest.mock import patch

from app import main as main_module
from app import pipeline


def _job_id(char="a"):
    return char * 32


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_malformed_job_id_is_400(client):
    resp = client.get("/jobs/not-a-valid-id")
    assert resp.status_code == 400


def test_unknown_job_id_is_404(client):
    resp = client.get(f"/jobs/{_job_id()}")
    assert resp.status_code == 404


def test_jobs_files_only_status_when_dir_exists_without_a_registry_entry(client, tmp_path):
    job_id = _job_id("d")
    (tmp_path / job_id).mkdir()
    resp = client.get(f"/jobs/{job_id}")
    assert resp.status_code == 200
    assert resp.json()["status"] == "files-only"


def test_download_success_returns_job_id_and_metadata(client):
    metadata = {"shortcode": "ABC", "username": "u", "caption": "c"}
    with patch.object(pipeline, "download_post", return_value=metadata):
        resp = client.post("/download", json={"url": "https://www.instagram.com/reel/ABC/"})
    assert resp.status_code == 200
    body = resp.json()
    # The response model documents fields (like "places") this mock never
    # set, so they're present but null rather than absent — check the ones
    # this test actually cares about instead of exact dict equality.
    assert body["metadata"]["shortcode"] == "ABC"
    assert body["metadata"]["username"] == "u"
    assert body["metadata"]["caption"] == "c"
    assert len(body["job_id"]) == 32


def test_download_failure_cleans_up_the_job_dir(client, tmp_path):
    # Regression test for the reviewed bug: a partially-populated job dir must
    # not be left behind when /download fails, since the caller never gets a
    # job_id back to clean it up with.
    def boom(url, job_dir):
        job_dir.mkdir(parents=True, exist_ok=True)
        (job_dir / "partial.mp4").write_bytes(b"x")
        raise pipeline.PipelineError("rate limited")

    with patch.object(pipeline, "download_post", side_effect=boom):
        resp = client.post("/download", json={"url": "https://www.instagram.com/reel/ABC/"})

    assert resp.status_code == 502
    assert list(tmp_path.iterdir()) == []


def test_download_invalid_input_is_400(client):
    with patch.object(pipeline, "download_post", side_effect=pipeline.InvalidInputError("bad url")):
        resp = client.post("/download", json={"url": "https://example.com/nope"})
    assert resp.status_code == 400


def test_extract_audio_unknown_job_is_404(client):
    resp = client.post("/extract-audio", json={"job_id": _job_id()})
    assert resp.status_code == 404


def test_extract_audio_success(client, tmp_path):
    job_id = _job_id("b")
    (tmp_path / job_id).mkdir()
    with patch.object(pipeline, "extract_audio", return_value=tmp_path / job_id / "audio.mp3"):
        resp = client.post("/extract-audio", json={"job_id": job_id})
    assert resp.status_code == 200
    assert resp.json()["job_id"] == job_id


def test_extract_audio_pipeline_error_is_422(client, tmp_path):
    job_id = _job_id("b")
    (tmp_path / job_id).mkdir()
    with patch.object(pipeline, "extract_audio", side_effect=pipeline.PipelineError("no video")):
        resp = client.post("/extract-audio", json={"job_id": job_id})
    assert resp.status_code == 422


def test_ocr_success(client, tmp_path):
    job_id = _job_id("e")
    (tmp_path / job_id).mkdir()
    with patch.object(pipeline, "run_ocr", return_value=[{"text": "hi", "confidence": 90.0}]):
        resp = client.post("/ocr", json={"job_id": job_id})
    assert resp.status_code == 200
    results = resp.json()["results"]
    assert len(results) == 1
    assert results[0]["text"] == "hi"
    assert results[0]["confidence"] == 90.0


def test_extract_places_endpoint_is_removed(client, tmp_path):
    # Regression test: /extract-places was folded into /process's result
    # (result.places) since places are derived from data the pipeline
    # already produces, not something worth a separate call/endpoint.
    job_id = _job_id("f")
    (tmp_path / job_id).mkdir()
    resp = client.post("/extract-places", json={"job_id": job_id})
    assert resp.status_code == 404


def test_delete_job_removes_dir_and_registry_entry(client, tmp_path):
    job_id = _job_id("c")
    job_dir = tmp_path / job_id
    job_dir.mkdir()
    main_module._set_job(job_id, status="done")

    resp = client.delete(f"/jobs/{job_id}")

    assert resp.status_code == 200
    assert not job_dir.exists()
    assert job_id not in main_module._jobs


def _poll_until_terminal(client, job_id, timeout=5.0):
    deadline = time.time() + timeout
    job = None
    while time.time() < deadline:
        job = client.get(f"/jobs/{job_id}").json()
        if job.get("status") in ("done", "error"):
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} never reached a terminal status: {job}")


def test_process_runs_the_pipeline_in_the_background_and_completes(client):
    # Regression test for the reviewed /process fix: the pipeline now runs as
    # an independent asyncio.Task (via asyncio.to_thread) rather than a
    # Starlette BackgroundTask. This exercises that path end to end.
    fake_metadata = {"shortcode": "ABC", "username": "u", "caption": "c", "video_path": "/x/video.mp4"}
    fake_transcript = {"text": "hi", "segments": [], "language": "en"}
    fake_places = [{"name": "Joe's Pizza", "city": "Rome", "country": "Italy",
                    "rating": 4.6, "maps_url": "https://maps.google.com/?cid=1"}]

    with patch.object(pipeline, "download_post", return_value=dict(fake_metadata)), patch.object(
        pipeline, "extract_audio", return_value=None
    ), patch.object(pipeline, "transcribe", return_value=fake_transcript), patch.object(
        pipeline, "extract_frames", return_value=[]
    ), patch.object(
        pipeline, "run_ocr", return_value=[]
    ), patch.object(
        pipeline, "build_places", return_value=fake_places
    ):
        resp = client.post("/process", json={"url": "https://www.instagram.com/reel/ABC/"})
        assert resp.status_code == 202
        body = resp.json()
        assert body["status"] == "queued"
        job_id = body["job_id"]

        job = _poll_until_terminal(client, job_id)

    assert job["status"] == "done"
    assert job["result"]["metadata"]["username"] == "u"
    # KEEP_FILES=False (set by the `client` fixture) -> video_path is stripped
    # from the underlying result; the response model still declares the key,
    # so it comes back null rather than being absent.
    assert job["result"]["metadata"]["video_path"] is None
    # places is a sibling of metadata/transcript/ocr_results, not nested
    # under metadata.
    assert job["result"]["places"] == fake_places
    assert "places" not in job["result"]["metadata"]


def test_process_pipeline_error_marks_the_job_as_error(client):
    with patch.object(pipeline, "download_post", side_effect=pipeline.PipelineError("rate limited")):
        resp = client.post("/process", json={"url": "https://www.instagram.com/reel/ABC/"})
        job_id = resp.json()["job_id"]

        job = _poll_until_terminal(client, job_id)

    assert job["status"] == "error"
    assert "rate limited" in job["error"]


def test_process_keeps_video_path_when_keep_files_is_true(client, monkeypatch):
    # Contrasts with the KEEP_FILES=False case above: video_path should be a
    # real value, not just always-null, proving the response model didn't
    # erase the stripping behavior it documents.
    monkeypatch.setattr(main_module.config, "KEEP_FILES", True)
    fake_metadata = {"shortcode": "ABC", "username": "u", "caption": "c", "video_path": "/x/video.mp4"}
    fake_transcript = {"text": "hi", "segments": [], "language": "en"}

    with patch.object(pipeline, "download_post", return_value=dict(fake_metadata)), patch.object(
        pipeline, "extract_audio", return_value=None
    ), patch.object(pipeline, "transcribe", return_value=fake_transcript), patch.object(
        pipeline, "extract_frames", return_value=[]
    ), patch.object(
        pipeline, "run_ocr", return_value=[]
    ), patch.object(
        pipeline, "build_places", return_value=[]
    ):
        resp = client.post("/process", json={"url": "https://www.instagram.com/reel/ABC/"})
        job_id = resp.json()["job_id"]
        job = _poll_until_terminal(client, job_id)

    assert job["result"]["metadata"]["video_path"] == "/x/video.mp4"


def test_swagger_ui_is_served(client):
    resp = client.get("/docs")
    assert resp.status_code == 200
    assert "swagger" in resp.text.lower()


def test_openapi_schema_documents_response_shapes(client):
    # Regression test: before response models were added, every endpoint's
    # response schema in the OpenAPI doc was an empty {} (Swagger showed no
    # shape at all). This pins down that it's a real, named schema now.
    schema = client.get("/openapi.json").json()
    assert schema["info"]["title"] == "insta-parser"

    process_schema = schema["paths"]["/process"]["post"]["responses"]["202"]["content"]["application/json"]["schema"]
    assert process_schema == {"$ref": "#/components/schemas/ProcessResponse"}

    job_schema = schema["components"]["schemas"]["JobStatus"]["properties"]
    assert "job_id" in job_schema
    assert "updated_at" in job_schema


def test_no_response_schema_advertises_additional_properties(client):
    # Regression test: response models used to set extra="allow", which
    # pydantic renders as additionalProperties: true and Swagger UI then
    # fills in with a placeholder "additionalProp1" key in the example.
    # Response models are now the real contract, so no schema should do this.
    schema = client.get("/openapi.json").json()
    for name, model_schema in schema["components"]["schemas"].items():
        assert model_schema.get("additionalProperties") is not True, (
            f"{name} still advertises additionalProperties"
        )


def test_extract_places_route_is_gone(client):
    schema = client.get("/openapi.json").json()
    assert "/extract-places" not in schema["paths"]

    job_schema = schema["components"]["schemas"]["JobStatus"]["properties"]
    assert "job_id" in job_schema
    assert "updated_at" in job_schema
