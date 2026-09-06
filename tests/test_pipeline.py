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


def _write_manifest(job_dir, media):
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "metadata.json").write_text(json.dumps({"media": media}))


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


class TestMediaManifest:
    def test_missing_metadata_raises(self, tmp_path):
        with pytest.raises(pipeline.PipelineError, match="call /download first"):
            pipeline._media_manifest(tmp_path)

    def test_empty_media_list_raises(self, tmp_path):
        (tmp_path / "metadata.json").write_text(json.dumps({"media": []}))
        with pytest.raises(pipeline.PipelineError, match="call /download first"):
            pipeline._media_manifest(tmp_path)

    def test_existing_manifest_is_returned(self, tmp_path):
        media = [{"index": 1, "type": "video", "path": "x"}]
        _write_manifest(tmp_path, media)
        assert pipeline._media_manifest(tmp_path) == media


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


class TestPreprocessForOcr:
    def test_upscales_a_small_frame(self):
        img = Image.new("RGB", (100, 100))
        result = pipeline._preprocess_for_ocr(img)
        assert result.height == 200  # min(2.0, 1500/100) = 2.0

    def test_leaves_a_tall_frame_alone(self):
        img = Image.new("RGB", (1200, 1200))
        result = pipeline._preprocess_for_ocr(img)
        assert result.size == (1200, 1200)

    def test_caps_the_upscale_factor_at_2x(self):
        img = Image.new("RGB", (10, 10))
        result = pipeline._preprocess_for_ocr(img)
        assert result.height == 20  # would be 150x without the 2.0 cap


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

    def test_tries_every_psm_mode(self, tmp_path):
        frame = tmp_path / "frame_0001.png"
        _make_frame(frame)
        data = {"text": ["Hello"], "conf": ["90"]}
        with patch("app.pipeline.pytesseract.image_to_data", return_value=data) as mocked:
            pipeline._ocr_frame(frame)
        assert mocked.call_count == len(pipeline._OCR_PSM_MODES)
        used_psms = {call.kwargs["config"] for call in mocked.call_args_list}
        assert used_psms == set(pipeline._OCR_PSM_MODES)

    def test_higher_confidence_psm_mode_is_kept(self, tmp_path):
        frame = tmp_path / "frame_0001.png"
        _make_frame(frame)
        worse = {"text": ["Bad"], "conf": ["60"]}
        better = {"text": ["Good"], "conf": ["85"]}
        with patch("app.pipeline.pytesseract.image_to_data", side_effect=[worse, better]):
            text, confidence = pipeline._ocr_frame(frame)
        assert text == "Good"
        assert confidence == 85.0

    def test_low_confidence_result_is_dropped(self, tmp_path):
        frame = tmp_path / "frame_0001.png"
        _make_frame(frame)
        data = {"text": ["garbled"], "conf": ["30"]}
        with patch("app.pipeline.pytesseract.image_to_data", return_value=data), patch.object(
            pipeline.config, "OCR_MIN_CONFIDENCE", 50.0
        ):
            text, confidence = pipeline._ocr_frame(frame)
        assert text == ""
        assert confidence == 0.0

    def test_result_at_or_above_the_confidence_floor_is_kept(self, tmp_path):
        frame = tmp_path / "frame_0001.png"
        _make_frame(frame)
        data = {"text": ["fine"], "conf": ["55"]}
        with patch("app.pipeline.pytesseract.image_to_data", return_value=data), patch.object(
            pipeline.config, "OCR_MIN_CONFIDENCE", 50.0
        ):
            text, confidence = pipeline._ocr_frame(frame)
        assert text == "fine"
        assert confidence == 55.0


class TestExtractFrames:
    def _fake_run(self, item_dir, name="frame_0001.png"):
        def run(cmd, **kwargs):
            (item_dir / name).touch()
            return MagicMock(returncode=0, stderr="")
        return run

    def test_image_item_is_not_downscaled(self, tmp_path):
        # Regression test: an image item produces exactly one frame, so there
        # is no frame-count/speed reason to downscale it before OCR, unlike
        # video's scene-change extraction.
        _write_manifest(tmp_path, [{"index": 1, "type": "image", "path": "/x/media_01.jpg"}])
        item_dir = tmp_path / "frames" / "media_01"
        item_dir.mkdir(parents=True)
        with patch("app.pipeline.subprocess.run", side_effect=self._fake_run(item_dir)) as mocked_run:
            pipeline.extract_frames(tmp_path)
        cmd = mocked_run.call_args.args[0]
        assert "-vf" not in cmd
        assert cmd[-1] == str(item_dir / "frame_0001.png")

    def test_video_item_still_uses_the_scene_select_filter(self, tmp_path):
        _write_manifest(tmp_path, [{"index": 1, "type": "video", "path": "/x/media_01.mp4"}])
        item_dir = tmp_path / "frames" / "media_01"
        item_dir.mkdir(parents=True)
        with patch("app.pipeline.subprocess.run", side_effect=self._fake_run(item_dir)) as mocked_run:
            pipeline.extract_frames(tmp_path)
        cmd = mocked_run.call_args.args[0]
        assert "-vf" in cmd
        assert any("select=" in part for part in cmd)


class TestRunOcr:
    def test_no_manifest_raises(self, tmp_path):
        with pytest.raises(pipeline.PipelineError, match="call /download first"):
            pipeline.run_ocr(tmp_path)

    def test_no_frames_for_an_item_raises(self, tmp_path):
        _write_manifest(tmp_path, [{"index": 1, "type": "image", "path": "x"}])
        with pytest.raises(pipeline.PipelineError, match="call /extract-frames first"):
            pipeline.run_ocr(tmp_path)

    def test_dedupes_identical_consecutive_text_within_one_item_and_persists_results(self, tmp_path):
        _write_manifest(tmp_path, [{"index": 1, "type": "video", "path": "x"}])
        item_dir = tmp_path / "frames" / "media_01"
        item_dir.mkdir(parents=True)
        for i in range(3):
            _make_frame(item_dir / f"frame_{i:04d}.png")

        side_effects = [
            ("Hello World", 90.0),
            ("Hello World", 88.0),  # identical to the previous kept result -> dropped
            ("Totally unrelated content", 70.0),
        ]
        with patch("app.pipeline._ocr_frame", side_effect=side_effects):
            results = pipeline.run_ocr(tmp_path)

        assert len(results) == 1
        assert results[0]["index"] == 1
        assert results[0]["type"] == "video"
        assert [r["text"] for r in results[0]["results"]] == ["Hello World", "Totally unrelated content"]
        assert json.loads((tmp_path / "ocr.json").read_text()) == results

    def test_identical_text_on_different_items_both_survive(self, tmp_path):
        # Regression test for carousel attribution: the dedupe cursor must
        # reset at each item boundary, or a second slide repeating the first
        # slide's text (a common carousel pattern) would be silently dropped.
        _write_manifest(tmp_path, [
            {"index": 1, "type": "image", "path": "x"},
            {"index": 2, "type": "image", "path": "y"},
        ])
        for idx in (1, 2):
            item_dir = tmp_path / "frames" / f"media_{idx:02d}"
            item_dir.mkdir(parents=True)
            _make_frame(item_dir / "frame_0001.png")

        with patch("app.pipeline._ocr_frame", side_effect=[("Same text", 90.0), ("Same text", 91.0)]):
            results = pipeline.run_ocr(tmp_path)

        assert [item["results"][0]["text"] for item in results] == ["Same text", "Same text"]

    def test_missing_tesseract_binary_raises_pipeline_error(self, tmp_path):
        _write_manifest(tmp_path, [{"index": 1, "type": "image", "path": "x"}])
        item_dir = tmp_path / "frames" / "media_01"
        item_dir.mkdir(parents=True)
        _make_frame(item_dir / "frame_0001.png")
        with patch("app.pipeline._ocr_frame", side_effect=pytesseract.TesseractNotFoundError()):
            with pytest.raises(pipeline.PipelineError, match="tesseract is not installed"):
                pipeline.run_ocr(tmp_path)

    def test_per_frame_failure_is_skipped_not_fatal(self, tmp_path):
        _write_manifest(tmp_path, [{"index": 1, "type": "video", "path": "x"}])
        item_dir = tmp_path / "frames" / "media_01"
        item_dir.mkdir(parents=True)
        for i in range(2):
            _make_frame(item_dir / f"frame_{i:04d}.png")

        with patch(
            "app.pipeline._ocr_frame",
            side_effect=[RuntimeError("corrupt frame"), ("Fine", 99.0)],
        ):
            results = pipeline.run_ocr(tmp_path)

        assert [r["text"] for r in results[0]["results"]] == ["Fine"]


class FakeSegment:
    def __init__(self, start, end, text):
        self.start = start
        self.end = end
        self.text = text


class TestTranscribe:
    def test_no_manifest_raises(self, tmp_path):
        with pytest.raises(pipeline.PipelineError, match="call /download first"):
            pipeline.transcribe(tmp_path)

    def test_image_only_post_is_a_noop(self, tmp_path):
        _write_manifest(tmp_path, [{"index": 1, "type": "image", "path": "x"}])
        result = pipeline.transcribe(tmp_path)
        assert result == {"media": []}
        assert json.loads((tmp_path / "transcript.json").read_text()) == result

    def test_missing_audio_for_video_item_raises(self, tmp_path):
        _write_manifest(tmp_path, [{"index": 1, "type": "video", "path": "x"}])
        with pytest.raises(pipeline.PipelineError, match="call /extract-audio first"):
            pipeline.transcribe(tmp_path)

    def test_joins_segments_and_persists_transcript(self, tmp_path):
        _write_manifest(tmp_path, [{"index": 1, "type": "video", "path": "x"}])
        (tmp_path / "audio").mkdir()
        (tmp_path / "audio" / "media_01.mp3").touch()
        fake_model = MagicMock()
        fake_info = MagicMock(language="en")
        fake_model.transcribe.return_value = (
            [FakeSegment(0.0, 1.0, " Hello "), FakeSegment(1.0, 2.0, "world ")],
            fake_info,
        )
        with patch("app.pipeline._get_whisper_model", return_value=fake_model):
            result = pipeline.transcribe(tmp_path)

        assert len(result["media"]) == 1
        item = result["media"][0]
        assert item["index"] == 1
        assert item["text"] == "Hello world"
        assert item["language"] == "en"
        assert [s["text"] for s in item["segments"]] == ["Hello", "world"]
        assert json.loads((tmp_path / "transcript.json").read_text()) == result

    def test_only_video_items_are_transcribed(self, tmp_path):
        _write_manifest(tmp_path, [
            {"index": 1, "type": "video", "path": "x"},
            {"index": 2, "type": "image", "path": "y"},
            {"index": 3, "type": "video", "path": "z"},
        ])
        (tmp_path / "audio").mkdir()
        (tmp_path / "audio" / "media_01.mp3").touch()
        (tmp_path / "audio" / "media_03.mp3").touch()
        fake_model = MagicMock()
        fake_model.transcribe.return_value = ([], MagicMock(language="en"))
        with patch("app.pipeline._get_whisper_model", return_value=fake_model):
            result = pipeline.transcribe(tmp_path)

        assert [item["index"] for item in result["media"]] == [1, 3]

    def test_model_failure_wrapped_in_pipeline_error(self, tmp_path):
        _write_manifest(tmp_path, [{"index": 1, "type": "video", "path": "x"}])
        (tmp_path / "audio").mkdir()
        (tmp_path / "audio" / "media_01.mp3").touch()
        fake_model = MagicMock()
        fake_model.transcribe.side_effect = RuntimeError("boom")
        with patch("app.pipeline._get_whisper_model", return_value=fake_model):
            with pytest.raises(pipeline.PipelineError, match="Transcription failed"):
                pipeline.transcribe(tmp_path)

    def test_passes_vad_filter_and_disables_condition_on_previous_text(self, tmp_path):
        # Regression test for hallucinated garbage transcripts on silent/ASMR
        # audio: VAD should skip non-speech stretches instead of decoding them,
        # and condition_on_previous_text=False keeps one bad window from
        # dragging the rest into a repetition loop.
        _write_manifest(tmp_path, [{"index": 1, "type": "video", "path": "x"}])
        (tmp_path / "audio").mkdir()
        audio_path = tmp_path / "audio" / "media_01.mp3"
        audio_path.touch()
        fake_model = MagicMock()
        fake_model.transcribe.return_value = ([], MagicMock(language="en"))
        with patch("app.pipeline._get_whisper_model", return_value=fake_model), patch.object(
            pipeline.config, "WHISPER_VAD_FILTER", True
        ):
            pipeline.transcribe(tmp_path)

        fake_model.transcribe.assert_called_once_with(
            str(audio_path),
            vad_filter=True,
            condition_on_previous_text=False,
        )

    def test_vad_filter_reflects_config_toggle(self, tmp_path):
        _write_manifest(tmp_path, [{"index": 1, "type": "video", "path": "x"}])
        (tmp_path / "audio").mkdir()
        (tmp_path / "audio" / "media_01.mp3").touch()
        fake_model = MagicMock()
        fake_model.transcribe.return_value = ([], MagicMock(language="en"))
        with patch("app.pipeline._get_whisper_model", return_value=fake_model), patch.object(
            pipeline.config, "WHISPER_VAD_FILTER", False
        ):
            pipeline.transcribe(tmp_path)

        assert fake_model.transcribe.call_args.kwargs["vad_filter"] is False


class FakePost:
    """Stands in for instaloader.Post: is_video/typename/mediacount/caption/
    likes/comments are plain attributes (as on the real Post), while
    date_utc/location/owner_username are properties so tests can make them
    raise, mirroring how the real Post's lazy network-backed properties can
    fail independently of the initial fetch."""

    def __init__(
        self, is_video=True, typename=None, mediacount=1, location_error=False, username_error=False
    ):
        self.is_video = is_video
        self.typename = typename or ("GraphVideo" if is_video else "GraphImage")
        self.mediacount = mediacount
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

    def test_fetch_failure_wrapped_as_pipeline_error(self, tmp_path):
        exc = instaloader.exceptions.InstaloaderException("rate limited")
        with patch("app.pipeline.instaloader.Post.from_shortcode", side_effect=exc):
            with pytest.raises(pipeline.PipelineError, match="Failed to fetch"):
                pipeline.download_post(self.URL, tmp_path / "job")

    def test_successful_video_download_writes_metadata_and_media_manifest(self, tmp_path):
        job_dir = tmp_path / "job"
        post = FakePost()

        def fake_download(self_loader, post_arg, target):
            # Simulate instaloader dropping a video file for a plain reel: no
            # _N suffix, since it isn't a sidecar.
            job_dir.mkdir(parents=True, exist_ok=True)
            (job_dir / "media.mp4").write_bytes(b"fake video")

        with patch("app.pipeline.instaloader.Post.from_shortcode", return_value=post), patch.object(
            instaloader.Instaloader, "download_post", autospec=True, side_effect=fake_download
        ):
            metadata = pipeline.download_post(self.URL, job_dir)

        assert metadata["shortcode"] == "ABC123"
        assert metadata["username"] == "someone"
        assert metadata["caption"] == "a caption"
        assert metadata["location"] is None
        assert metadata["media"] == [
            {"index": 1, "type": "video", "path": str(job_dir / "media_01.mp4")}
        ]
        assert (job_dir / "media_01.mp4").exists()
        assert json.loads((job_dir / "metadata.json").read_text()) == metadata

    def test_image_post_downloads_successfully(self, tmp_path):
        # Regression test: a photo-only post used to hard-fail with
        # InvalidInputError ("has no video") before the caller ever got a
        # job_id — it must now download like any other post.
        job_dir = tmp_path / "job"
        post = FakePost(is_video=False, typename="GraphImage")

        def fake_download(self_loader, post_arg, target):
            job_dir.mkdir(parents=True, exist_ok=True)
            (job_dir / "media.jpg").write_bytes(b"fake image")

        with patch("app.pipeline.instaloader.Post.from_shortcode", return_value=post), patch.object(
            instaloader.Instaloader, "download_post", autospec=True, side_effect=fake_download
        ):
            metadata = pipeline.download_post(self.URL, job_dir)

        assert metadata["media"] == [
            {"index": 1, "type": "image", "path": str(job_dir / "media_01.jpg")}
        ]

    def test_carousel_download_orders_items_naturally_and_classifies_types(self, tmp_path):
        # media_10 must sort after media_2, not before it lexically, and a
        # mixed image/video carousel must keep every item, not just the first.
        job_dir = tmp_path / "job"
        post = FakePost(typename="GraphSidecar", mediacount=3)

        def fake_download(self_loader, post_arg, target):
            job_dir.mkdir(parents=True, exist_ok=True)
            (job_dir / "media_1.jpg").write_bytes(b"img1")
            (job_dir / "media_2.mp4").write_bytes(b"vid2")
            (job_dir / "media_10.jpg").write_bytes(b"img10")

        with patch("app.pipeline.instaloader.Post.from_shortcode", return_value=post), patch.object(
            instaloader.Instaloader, "download_post", autospec=True, side_effect=fake_download
        ):
            metadata = pipeline.download_post(self.URL, job_dir)

        assert [item["type"] for item in metadata["media"]] == ["image", "video", "image"]
        assert [item["index"] for item in metadata["media"]] == [1, 2, 3]
        assert (job_dir / "media_01.jpg").exists()
        assert (job_dir / "media_02.mp4").exists()
        assert (job_dir / "media_03.jpg").exists()

    def test_no_media_found_raises_pipeline_error(self, tmp_path):
        job_dir = tmp_path / "job"
        post = FakePost()
        with patch("app.pipeline.instaloader.Post.from_shortcode", return_value=post), patch.object(
            instaloader.Instaloader, "download_post", autospec=True
        ):
            with pytest.raises(pipeline.PipelineError, match="no media file was found"):
                pipeline.download_post(self.URL, job_dir)

    def test_location_fetch_failure_falls_back_to_none(self, tmp_path):
        job_dir = tmp_path / "job"
        post = FakePost(location_error=True)

        def fake_download(self_loader, post_arg, target):
            job_dir.mkdir(parents=True, exist_ok=True)
            (job_dir / "media.mp4").write_bytes(b"fake video")

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
            (job_dir / "media.mp4").write_bytes(b"fake video")

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
        (tmp_path / "transcript.json").write_text(json.dumps({"media": [{"index": 1, "text": "spoken"}]}))
        (tmp_path / "ocr.json").write_text(json.dumps([{"index": 1, "results": [{"text": "on screen"}]}]))

        with patch("app.pipeline.places_module.build_places_metadata", return_value=[]) as mocked:
            pipeline.build_places(tmp_path)

        _, args, _ = mocked.mock_calls[0]
        metadata_arg, transcript_arg, ocr_arg = args
        assert metadata_arg == {"caption": "hi"}
        assert transcript_arg == {"media": [{"index": 1, "text": "spoken"}]}
        assert ocr_arg == [{"index": 1, "results": [{"text": "on screen"}]}]
