import json
from unittest.mock import MagicMock, patch

import pytest
import requests

from app import places


@pytest.fixture(autouse=True)
def _clear_places_config(monkeypatch):
    """None of these integrations should be "configured" unless a test opts
    in, regardless of what real env vars happen to be set on the runner."""
    monkeypatch.setattr(places.config, "LITELLM_BASE_URL", None)
    monkeypatch.setattr(places.config, "LITELLM_MODEL", None)
    monkeypatch.setattr(places.config, "LITELLM_API_KEY", None)
    monkeypatch.setattr(places.config, "GOOGLE_MAPS_API_KEY", None)


class TestConfigured:
    def test_places_not_configured_by_default(self):
        assert places.places_configured() is False

    def test_places_configured_needs_both_base_url_and_model(self, monkeypatch):
        monkeypatch.setattr(places.config, "LITELLM_BASE_URL", "http://x")
        assert places.places_configured() is False
        monkeypatch.setattr(places.config, "LITELLM_MODEL", "model")
        assert places.places_configured() is True

    def test_maps_not_configured_by_default(self):
        assert places.maps_configured() is False

    def test_maps_configured_when_key_set(self, monkeypatch):
        monkeypatch.setattr(places.config, "GOOGLE_MAPS_API_KEY", "key")
        assert places.maps_configured() is True


class TestBuildContext:
    def test_combines_caption_location_transcript_and_ocr_for_a_single_item_post(self):
        # A single-item post's context has no "Item N" labels, so extraction
        # behaviour for the common (non-carousel) case is unaffected by the
        # media-aware rewrite.
        metadata = {"caption": "Cap", "location": "Rome", "media": [{"index": 1, "type": "video"}]}
        transcript = {"media": [{"index": 1, "text": "Spoken text"}]}
        ocr_results = [{"index": 1, "type": "video", "results": [{"text": "On screen"}, {"text": ""}]}]
        context = places._build_context(metadata, transcript, ocr_results)
        assert "Cap" in context
        assert "Tagged location: Rome" in context
        assert "Spoken text" in context
        assert "On screen" in context
        assert "Item" not in context

    def test_labels_and_orders_carousel_items(self):
        metadata = {
            "caption": "Cap",
            "media": [{"index": 1, "type": "image"}, {"index": 2, "type": "video"}],
        }
        transcript = {"media": [{"index": 2, "text": "Spoken text"}]}
        ocr_results = [
            {"index": 1, "type": "image", "results": [{"text": "Slide one text"}]},
            {"index": 2, "type": "video", "results": [{"text": "Slide two text"}]},
        ]
        context = places._build_context(metadata, transcript, ocr_results)
        assert context.index("Item 1") < context.index("Item 2 transcript")
        assert "Item 1 on-screen text: Slide one text" in context
        assert "Item 2 transcript: Spoken text" in context
        assert "Item 2 on-screen text: Slide two text" in context

    def test_skips_missing_pieces(self):
        context = places._build_context({"caption": "Only caption"}, None, None)
        assert context == "Only caption"

    def test_truncates_to_max_chars(self, monkeypatch):
        monkeypatch.setattr(places.config, "PLACE_EXTRACTION_MAX_CHARS", 5)
        context = places._build_context({"caption": "0123456789"}, None, None)
        assert context == "01234"


class TestExtractPlaceMentions:
    def test_returns_empty_when_not_configured(self):
        assert places.extract_place_mentions({"caption": "x"}, None, None) == []

    def test_returns_empty_when_no_context_text(self, monkeypatch):
        monkeypatch.setattr(places.config, "LITELLM_BASE_URL", "http://x")
        monkeypatch.setattr(places.config, "LITELLM_MODEL", "m")
        assert places.extract_place_mentions({"caption": ""}, None, None) == []

    def test_posts_to_the_v1_chat_completions_path(self, monkeypatch):
        # Regression test for the reviewed bug: LITELLM_BASE_URL is documented
        # as having no /v1 suffix, so the code must add it.
        monkeypatch.setattr(places.config, "LITELLM_BASE_URL", "http://x:4000")
        monkeypatch.setattr(places.config, "LITELLM_MODEL", "m")
        fake_response = MagicMock()
        fake_response.raise_for_status.return_value = None
        fake_response.json.return_value = {"choices": [{"message": {"content": "[]"}}]}
        with patch("app.places.requests.post", return_value=fake_response) as mocked_post:
            places.extract_place_mentions({"caption": "hi"}, None, None)
        called_url = mocked_post.call_args.args[0]
        assert called_url == "http://x:4000/v1/chat/completions"

    def test_parses_and_filters_a_valid_response(self, monkeypatch):
        monkeypatch.setattr(places.config, "LITELLM_BASE_URL", "http://x")
        monkeypatch.setattr(places.config, "LITELLM_MODEL", "m")
        payload = [
            {"name": "Joe's Pizza", "city": "Rome", "country": "Italy"},
            {"name": None, "city": None, "country": None},  # dropped: nothing usable
            "not even an object",  # dropped: wrong type
        ]
        fake_response = MagicMock()
        fake_response.raise_for_status.return_value = None
        fake_response.json.return_value = {"choices": [{"message": {"content": json.dumps(payload)}}]}
        with patch("app.places.requests.post", return_value=fake_response):
            result = places.extract_place_mentions({"caption": "reel about Joe's Pizza"}, None, None)
        assert result == [{"name": "Joe's Pizza", "city": "Rome", "country": "Italy"}]

    def test_request_failure_returns_empty_list(self, monkeypatch):
        monkeypatch.setattr(places.config, "LITELLM_BASE_URL", "http://x")
        monkeypatch.setattr(places.config, "LITELLM_MODEL", "m")
        with patch("app.places.requests.post", side_effect=requests.ConnectionError("down")):
            assert places.extract_place_mentions({"caption": "hi"}, None, None) == []

    def test_non_json_content_returns_empty_list(self, monkeypatch):
        monkeypatch.setattr(places.config, "LITELLM_BASE_URL", "http://x")
        monkeypatch.setattr(places.config, "LITELLM_MODEL", "m")
        fake_response = MagicMock()
        fake_response.raise_for_status.return_value = None
        fake_response.json.return_value = {"choices": [{"message": {"content": "no json here"}}]}
        with patch("app.places.requests.post", return_value=fake_response):
            assert places.extract_place_mentions({"caption": "hi"}, None, None) == []

    def test_non_list_json_returns_empty_list(self, monkeypatch):
        monkeypatch.setattr(places.config, "LITELLM_BASE_URL", "http://x")
        monkeypatch.setattr(places.config, "LITELLM_MODEL", "m")
        fake_response = MagicMock()
        fake_response.raise_for_status.return_value = None
        fake_response.json.return_value = {"choices": [{"message": {"content": '{"not": "a list"}'}}]}
        with patch("app.places.requests.post", return_value=fake_response):
            assert places.extract_place_mentions({"caption": "hi"}, None, None) == []


class TestResolvePlaceOnMaps:
    def test_returns_unchanged_when_not_configured(self):
        place = {"name": "X", "city": "Y", "country": "Z"}
        result = places.resolve_place_on_maps(place)
        assert result == {**place, "rating": None, "maps_url": None}

    def test_enriches_with_first_candidate(self, monkeypatch):
        monkeypatch.setattr(places.config, "GOOGLE_MAPS_API_KEY", "key")
        fake_response = MagicMock()
        fake_response.raise_for_status.return_value = None
        fake_response.json.return_value = {
            "places": [{"displayName": {"text": "Joe's Pizza"}, "rating": 4.6, "googleMapsUri": "https://maps/x"}]
        }
        with patch("app.places.requests.post", return_value=fake_response):
            result = places.resolve_place_on_maps({"name": "Joe's Pizza", "city": "Rome", "country": "Italy"})
        assert result["rating"] == 4.6
        assert result["maps_url"] == "https://maps/x"
        assert result["name"] == "Joe's Pizza"

    def test_no_candidates_returns_none_fields(self, monkeypatch):
        monkeypatch.setattr(places.config, "GOOGLE_MAPS_API_KEY", "key")
        fake_response = MagicMock()
        fake_response.raise_for_status.return_value = None
        fake_response.json.return_value = {"places": []}
        with patch("app.places.requests.post", return_value=fake_response):
            result = places.resolve_place_on_maps({"name": "X"})
        assert result["rating"] is None
        assert result["maps_url"] is None

    def test_lookup_failure_returns_none_fields(self, monkeypatch):
        monkeypatch.setattr(places.config, "GOOGLE_MAPS_API_KEY", "key")
        with patch("app.places.requests.post", side_effect=requests.Timeout()):
            result = places.resolve_place_on_maps({"name": "X"})
        assert result["rating"] is None
        assert result["maps_url"] is None

    def test_empty_query_short_circuits_without_a_request(self, monkeypatch):
        monkeypatch.setattr(places.config, "GOOGLE_MAPS_API_KEY", "key")
        with patch("app.places.requests.post") as mocked_post:
            result = places.resolve_place_on_maps({"name": None, "city": None, "country": None})
        mocked_post.assert_not_called()
        assert result["rating"] is None


class TestBuildPlacesMetadata:
    def test_resolves_every_extracted_mention(self, monkeypatch):
        monkeypatch.setattr(places.config, "LITELLM_BASE_URL", "http://x")
        monkeypatch.setattr(places.config, "LITELLM_MODEL", "m")
        monkeypatch.setattr(places.config, "GOOGLE_MAPS_API_KEY", "key")

        mentions = [{"name": "A", "city": None, "country": None}, {"name": "B", "city": None, "country": None}]
        with patch("app.places.extract_place_mentions", return_value=mentions), patch(
            "app.places.resolve_place_on_maps", side_effect=lambda p: {**p, "rating": 5.0, "maps_url": "u"}
        ) as mocked_resolve:
            result = places.build_places_metadata({"caption": "x"}, None, None)

        assert mocked_resolve.call_count == 2
        assert all(place["rating"] == 5.0 for place in result)
