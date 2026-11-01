import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
PYTHON_ROOT = ROOT / "app" / "src" / "main" / "python"
os.environ.setdefault("NOCTURNE_DB", str(Path(tempfile.gettempdir()) / "nocturne_40_tests.db"))
sys.path.insert(0, str(PYTHON_ROOT))

# Keep the unit tests runnable on machines which have not installed Flask.
try:
    import flask  # noqa: F401
except ModuleNotFoundError:
    import types
    fake_flask = types.ModuleType("flask")

    class _FakeFlask:
        def __init__(self, *args, **kwargs):
            self.config = {}

        def get(self, *args, **kwargs):
            return lambda fn: fn

        def post(self, *args, **kwargs):
            return lambda fn: fn

        def delete(self, *args, **kwargs):
            return lambda fn: fn

    fake_flask.Flask = _FakeFlask
    fake_flask.jsonify = lambda value: value
    fake_flask.render_template_string = lambda *args, **kwargs: ""
    fake_flask.request = types.SimpleNamespace(args={})
    sys.modules["flask"] = fake_flask

import otakudesu_gothic as appmod  # noqa: E402


class ProviderNormalizationTests(unittest.TestCase):
    def test_first_list_handles_array_and_jsonapi(self):
        self.assertEqual(appmod._first_list([{"id": "1"}, "bad"]), [{"id": "1"}])
        self.assertEqual(appmod._first_list({"data": [{"id": "2"}]}), [{"id": "2"}])

    @patch.object(appmod, "_external_json")
    def test_mitenime_search_handles_attributes(self, get_json):
        get_json.return_value = {"data": [{"slug": "sample-series", "attributes": {
            "title": "Sample", "poster": "https://img.example/p.jpg", "rating": 8.2
        }}]}
        row = appmod._mitenime_search("Sample")[0]
        self.assertEqual(row["provider_id"], "sample-series")
        self.assertEqual(row["title"], "Sample")

    @patch.object(appmod, "_external_json")
    def test_animap_mapping_without_removed_sources(self, get_json):
        get_json.return_value = [{"mal_id": [1], "anilist_id": [21], "titles": ["Cowboy Bebop"], "year": 1998}]
        row = appmod._animap_search("Cowboy Bebop")[0]
        self.assertEqual(row["title"], "Cowboy Bebop")
        self.assertEqual(row["mapping"]["anilist_id"], [21])
        self.assertNotIn("kitsu_id", row["mapping"])

    @patch.object(appmod, "_external_text")
    def test_ann_xml_parser(self, get_text):
        get_text.return_value = '<ann><anime id="13"><info type="Main title">Cowboy Bebop</info></anime></ann>'
        row = appmod._ann_search("Cowboy Bebop")[0]
        self.assertEqual(row["provider_id"], "13")
        self.assertEqual(row["title"], "Cowboy Bebop")


class AggregationTests(unittest.TestCase):
    @patch.object(appmod, "mal_search", return_value=[])
    @patch.object(appmod, "_ann_search", return_value=[])
    @patch.object(appmod, "_animap_search", return_value=[])
    @patch.object(appmod, "_mitenime_search", side_effect=RuntimeError("provider offline"))
    @patch.object(appmod, "_anilist_search", return_value=[])
    def test_multisearch_keeps_other_providers_when_one_fails(self, *_mocks):
        result = appmod.multiverse_search("Naruto")
        self.assertEqual(result["query"], "Naruto")
        self.assertEqual(len(result["providers"]), 5)
        self.assertFalse(result["providers"]["MiteNime"]["ok"])
        self.assertEqual(result["provider_count"], 4)

    def test_multisearch_rejects_short_query(self):
        with self.assertRaises(ValueError):
            appmod.multiverse_search("a")

    def test_health_route_uses_new_app_name(self):
        response = appmod.api_health()
        payload = response.get_json() if hasattr(response, "get_json") else response
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["app"], "NOCTURNE: streaming anime")


class BrandingTests(unittest.TestCase):
    def test_banner_asset_is_bundled(self):
        banner = PYTHON_ROOT / "kolom" / "banner.svg"
        self.assertTrue(banner.is_file())
        self.assertIn("<svg", banner.read_text(encoding="utf-8"))

    def test_ui_has_local_auth_profile_and_play(self):
        self.assertIn("Selamat datang di NOCTURNE", appmod.PAGE)
        self.assertIn("id=\"authGate\"", appmod.PAGE)
        self.assertIn("Foto profil", appmod.PAGE)
        self.assertIn("Banner profil", appmod.PAGE)
        self.assertIn("Tampilan Beranda", appmod.PAGE)
        self.assertIn('accept="video/mp4,image/gif,.mp4,.gif"', appmod.PAGE)
        self.assertIn("nocturne-home-media", appmod.PAGE)
        self.assertIn("async function applyHomeBanner()", appmod.PAGE)
        self.assertIn("async function saveHomeMedia(file,kind)", appmod.PAGE)
        self.assertIn("Banner profil diperbarui.", appmod.PAGE)
        self.assertNotIn("Banner profil dan beranda diperbarui", appmod.PAGE)

    def test_android_webview_accepts_uploaded_media_types(self):
        java = (ROOT / "app/src/main/java/com/nocturne/archive/MainActivity.java").read_text(encoding="utf-8")
        self.assertIn("fileChooserParams.getAcceptTypes()", java)
        self.assertIn("Intent.EXTRA_MIME_TYPES", java)
        self.assertIn("setMediaPlaybackRequiresUserGesture(false)", java)
        self.assertIn("▶ PLAY", appmod.PAGE)
        self.assertNotIn("Buka player di tab baru", appmod.PAGE)


if __name__ == "__main__":
    unittest.main(verbosity=2)
