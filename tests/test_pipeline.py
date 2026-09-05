import json
import subprocess
from datetime import datetime
from unittest.mock import MagicMock, patch

import instaloader
import pytest
import pytesseract
from PIL import Image

from app import pipeline


def _make_frame(path):
    Image.new("RGB", (4, 4), color="white").save(path)


class TestExtractShortcode:
    @pytest.mark.parametrize(
        "url,expected",
        [
            ("https://www.instagram.com/reel/ABC123xyz/", "ABC123xyz"),
            ("https://www.instagram.com/p/XYZ_-9/", "XYZ_-9"),
            ("https://instagram.com/reels/CODE123", "CODE123"),
            ("https://www.instagram.com/tv/CODE456/", "CODE456"),
        ],
    )
    def test_valid_urls(self, url, expected):
        assert pipeline.extract_shortcode(url) == expected

    def test_invalid_url_raises_invalid_input_error(self):
        with pytest.raises(pipeline.InvalidInputError):
            pipeline.extract_shortcode("https://example.com/not-instagram")


class TestVideoPath:
    def test_missing_video_raises(self, tmp_path):
        with pytest.raises(pipeline.PipelineError, match="call /download first"):
            pipeline._video_path(tmp_path)

    def test_existing_video_is_returned(self, tmp_path):
        video = tmp_path / "video.mp4"
        video.touch()
        assert pipeline._video_path(tmp_path) == video


class TestRunFfmpeg:
    def test_missing_binary_raises_pipeline_error(self):
        with patch("app.pipeline.subprocess.run", side_effect=FileNotFoundError):
            with pytest.raises(pipeline.PipelineError, match="ffmpeg is not installed"):
                pipeline._run_ffmpeg(["ffmpeg"], step="test")

    def test_timeout_raises_pipeline_error(self):
        timeout_exc = subprocess.TimeoutExpired(cmd="ffmpeg", timeout=1)
        with patch("app.pipeline.subprocess.run", side_effect=timeout_exc):
            with pytest.raises(pipeline.PipelineError, match="timed out"):
                pipeline._run_ffmpeg(["ffmpeg"], step="test")

    def test_nonzero_exit_raises_pipeline_error(self):
        result = MagicMock(returncode=1, stderr="boom\n")
        with patch("app.pipeline.subprocess.run", return_value=result):
            with pytest.raises(pipeline.PipelineError, match="ffmpeg failed during test"):
                pipeline._run_ffmpeg(["ffmpeg"], step="test")

    def test_success_does_not_raise(self):
        result = MagicMock(returncode=0, stderr="")
        with patch("app.pipeline.subprocess.run", return_value=result) as mocked_run:
            pipeline._run_ffmpeg(["ffmpeg", "-y"], step="test")
        mocked_run.assert_called_once()


class TestOcrFrame:
    def test_no_scored_words_returns_empty(self, tmp_path):
        frame = tmp_path / "frame_0001.png"
        _make_frame(frame)
        data = {"text": ["", " "], "conf": ["-1", "-1"]}
        with patch("app.pipeline.pytesseract.image_to_data", return_value=data):
            text, confidence = pipeline._ocr_frame(frame)
        assert text == ""
        assert confidence == 0.0

    def test_extracts_text_and_mean_confidence(self, tmp_path):
        frame = tmp_path / "frame_0001.png"
        _make_frame(frame)
        data = {"text": ["Hello", "World", ""], "conf": ["90", "80", "-1"]}
        with patch("app.pipeline.pytesseract.image_to_data", return_value=data):
            text, confidence = pipeline._ocr_frame(frame)
        assert text == "Hello World"
        assert confidence == 85.0


class TestRunOcr:
    def test_no_frames_raises(self, tmp_path):
        with pytest.raises(pipeline.PipelineError, match="call /extract-frames first"):
            pipeline.run_ocr(tmp_path)

    def test_dedupes_identical_consecutive_text_and_persists_results(self, tmp_path):
        frames_dir = tmp_path / "frames"
        frames_dir.mkdir()
        for i in range(3):
            _make_frame(frames_dir / f"frame_{i:04d}.png")

        side_effects = [
            ("Hello World", 90.0),
            ("Hello World", 88.0),  # identical to the previous kept result -> dropped
            ("Totally unrelated content", 70.0),
        ]
        with patch("app.pipeline._ocr_frame", side_effect=side_effects):
            results = pipeline.run_ocr(tmp_path)

        assert [r["text"] for r in results] == ["Hello World", "Totally unrelated content"]
        assert json.loads((tmp_path / "ocr.json").read_text()) == results

    def test_missing_tesseract_binary_raises_pipeline_error(self, tmp_path):
        frames_dir = tmp_path / "frames"
        frames_dir.mkdir()
        _make_frame(frames_dir / "frame_0001.png")
        with patch("app.pipeline._ocr_frame", side_effect=pytesseract.TesseractNotFoundError()):
            with pytest.raises(pipeline.PipelineError, match="tesseract is not installed"):
                pipeline.run_ocr(tmp_path)

    def test_per_frame_failure_is_skipped_not_fatal(self, tmp_path):
        frames_dir = tmp_path / "frames"
        frames_dir.mkdir()
        for i in range(2):
            _make_frame(frames_dir / f"frame_{i:04d}.png")

        with patch(
            "app.pipeline._ocr_frame",
            side_effect=[RuntimeError("corrupt frame"), ("Fine", 99.0)],
        ):
            results = pipeline.run_ocr(tmp_path)

        assert [r["text"] for r in results] == ["Fine"]


class FakeSegment:
    def __init__(self, start, end, text):
        self.start = start
        self.end = end
        self.text = text


class TestTranscribe:
    def test_missing_audio_raises(self, tmp_path):
        with pytest.raises(pipeline.PipelineError, match="call /extract-audio first"):
            pipeline.transcribe(tmp_path)

    def test_joins_segments_and_persists_transcript(self, tmp_path):
        (tmp_path / "audio.mp3").touch()
        fake_model = MagicMock()
        fake_info = MagicMock(language="en")
        fake_model.transcribe.return_value = (
            [FakeSegment(0.0, 1.0, " Hello "), FakeSegment(1.0, 2.0, "world ")],
            fake_info,
        )
        with patch("app.pipeline._get_whisper_model", return_value=fake_model):
            result = pipeline.transcribe(tmp_path)

        assert result["text"] == "Hello world"
        assert result["language"] == "en"
        assert [s["text"] for s in result["segments"]] == ["Hello", "world"]
        assert json.loads((tmp_path / "transcript.json").read_text()) == result

    def test_model_failure_wrapped_in_pipeline_error(self, tmp_path):
        (tmp_path / "audio.mp3").touch()
        fake_model = MagicMock()
        fake_model.transcribe.side_effect = RuntimeError("boom")
        with patch("app.pipeline._get_whisper_model", return_value=fake_model):
            with pytest.raises(pipeline.PipelineError, match="Transcription failed"):
                pipeline.transcribe(tmp_path)


class FakePost:
    """Stands in for instaloader.Post: is_video/caption/likes/comments are
    plain attributes (as on the real Post), while location/owner_username/
    date_utc are properties so tests can make them raise, mirroring how the
    real Post's lazy network-backed properties can fail independently of the
    initial fetch."""

    def __init__(self, is_video=True, location_error=False, username_error=False):
        self.is_video = is_video
        self.caption = "a caption"
        self.likes = 10
        self.comments = 2
        self._location_error = location_error
        self._username_error = username_error

    @property
    def date_utc(self):
        return datetime(2026, 1, 1)

    @property
    def location(self):
        if self._location_error:
            raise instaloader.exceptions.InstaloaderException("location fetch failed")
        return None

    @property
    def owner_username(self):
        if self._username_error:
            raise instaloader.exceptions.InstaloaderException("username fetch failed")
        return "someone"


class TestDownloadPost:
    URL = "https://www.instagram.com/reel/ABC123/"

    def test_invalid_url_raises_before_any_io(self, tmp_path):
        with pytest.raises(pipeline.InvalidInputError):
            pipeline.download_post("https://example.com/nope", tmp_path / "job")

    def test_non_video_post_raises_invalid_input(self, tmp_path):
        post = FakePost(is_video=False)
        with patch("app.pipeline.instaloader.Post.from_shortcode", return_value=post):
            with pytest.raises(pipeline.InvalidInputError, match="no video"):
                pipeline.download_post(self.URL, tmp_path / "job")

    def test_fetch_failure_wrapped_as_pipeline_error(self, tmp_path):
        exc = instaloader.exceptions.InstaloaderException("rate limited")
        with patch("app.pipeline.instaloader.Post.from_shortcode", side_effect=exc):
            with pytest.raises(pipeline.PipelineError, match="Failed to fetch"):
                pipeline.download_post(self.URL, tmp_path / "job")

    def test_successful_download_writes_metadata_and_renames_video(self, tmp_path):
        job_dir = tmp_path / "job"
        post = FakePost()

        def fake_download(self_loader, post_arg, target):
            # Simulate instaloader dropping a video file named after `target`.
            job_dir.mkdir(parents=True, exist_ok=True)
            (job_dir / f"{target}.mp4").write_bytes(b"fake video")

        with patch("app.pipeline.instaloader.Post.from_shortcode", return_value=post), patch.object(
            instaloader.Instaloader, "download_post", autospec=True, side_effect=fake_download
        ):
            metadata = pipeline.download_post(self.URL, job_dir)

        assert metadata["shortcode"] == "ABC123"
        assert metadata["username"] == "someone"
        assert metadata["caption"] == "a caption"
        assert metadata["location"] is None
        assert metadata["video_path"] == str(job_dir / "video.mp4")
        assert (job_dir / "video.mp4").exists()
        assert json.loads((job_dir / "metadata.json").read_text()) == metadata

    def test_no_mp4_found_raises_pipeline_error(self, tmp_path):
        job_dir = tmp_path / "job"
        post = FakePost()
        with patch("app.pipeline.instaloader.Post.from_shortcode", return_value=post), patch.object(
            instaloader.Instaloader, "download_post", autospec=True
        ):
            with pytest.raises(pipeline.PipelineError, match="no .mp4 file was found"):
                pipeline.download_post(self.URL, job_dir)

    def test_location_fetch_failure_falls_back_to_none(self, tmp_path):
        job_dir = tmp_path / "job"
        post = FakePost(location_error=True)

        def fake_download(self_loader, post_arg, target):
            job_dir.mkdir(parents=True, exist_ok=True)
            (job_dir / f"{target}.mp4").write_bytes(b"fake video")

        with patch("app.pipeline.instaloader.Post.from_shortcode", return_value=post), patch.object(
            instaloader.Instaloader, "download_post", autospec=True, side_effect=fake_download
        ):
            metadata = pipeline.download_post(self.URL, job_dir)

        assert metadata["location"] is None

    def test_username_fetch_failure_falls_back_to_unknown(self, tmp_path):
        job_dir = tmp_path / "job"
        post = FakePost(username_error=True)

        def fake_download(self_loader, post_arg, target):
            job_dir.mkdir(parents=True, exist_ok=True)
            (job_dir / f"{target}.mp4").write_bytes(b"fake video")

        with patch("app.pipeline.instaloader.Post.from_shortcode", return_value=post), patch.object(
            instaloader.Instaloader, "download_post", autospec=True, side_effect=fake_download
        ):
            metadata = pipeline.download_post(self.URL, job_dir)

        assert metadata["username"] == "Unknown"


class TestBuildPlaces:
    def test_missing_metadata_raises(self, tmp_path):
        with pytest.raises(pipeline.PipelineError, match="call /download first"):
            pipeline.build_places(tmp_path)

    def test_delegates_to_places_module_and_persists_result(self, tmp_path):
        (tmp_path / "metadata.json").write_text(json.dumps({"caption": "hi"}))
        with patch(
            "app.pipeline.places_module.build_places_metadata", return_value=[{"name": "X"}]
        ) as mocked:
            places = pipeline.build_places(tmp_path)

        assert places == [{"name": "X"}]
        mocked.assert_called_once()
        assert json.loads((tmp_path / "places.json").read_text()) == [{"name": "X"}]

    def test_passes_through_transcript_and_ocr_when_present(self, tmp_path):
        (tmp_path / "metadata.json").write_text(json.dumps({"caption": "hi"}))
        (tmp_path / "transcript.json").write_text(json.dumps({"text": "spoken"}))
        (tmp_path / "ocr.json").write_text(json.dumps([{"text": "on screen"}]))

        with patch("app.pipeline.places_module.build_places_metadata", return_value=[]) as mocked:
            pipeline.build_places(tmp_path)

        _, args, _ = mocked.mock_calls[0]
        metadata_arg, transcript_arg, ocr_arg = args
        assert metadata_arg == {"caption": "hi"}
        assert transcript_arg == {"text": "spoken"}
        assert ocr_arg == [{"text": "on screen"}]
