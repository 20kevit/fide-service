"""Security tests: XML/ZIP safety, traversal, injection, secret hygiene."""
import io
import os
import zipfile

import pytest

from domain import xml_validation
from domain.errors import FideError, FideErrorCategory
from infrastructure import downloader, storage
from infrastructure.storage import FideStorageError


class TestXmlSafety:
    def test_entity_expansion_rejected(self, fixture_path):
        with pytest.raises(FideError) as exc:
            xml_validation.validate_xml_path(fixture_path("xxe.xml"))
        assert exc.value.category == FideErrorCategory.INVALID_XML

    def test_html_rejected(self, fixture_path):
        with pytest.raises(FideError) as exc:
            xml_validation.validate_xml_path(fixture_path("html_page.xml"))
        assert exc.value.category == FideErrorCategory.INVALID_XML

    def test_size_cap_enforced(self, fixture_path):
        with pytest.raises(FideError) as exc:
            xml_validation.validate_xml_path(
                fixture_path("small_standard.xml"), max_bytes=10)
        assert exc.value.category == FideErrorCategory.FILE_TOO_LARGE


class TestZipSafety:
    def _make_zip(self, tmp_path, members):
        zpath = str(tmp_path / "t.zip")
        with zipfile.ZipFile(zpath, "w") as zf:
            for name, data in members.items():
                zf.writestr(name, data)
        return zpath

    def test_traversal_member_rejected(self, tmp_path, fixture_path):
        xml = open(fixture_path("small_rapid.xml"), "rb").read()
        zpath = self._make_zip(tmp_path, {"../../evil.xml": xml})
        with pytest.raises(FideStorageError):
            storage.inspect_zip(zpath)

    def test_non_zip_rejected(self, tmp_path):
        zpath = str(tmp_path / "f.zip")
        open(zpath, "wb").write(b"this is not a zip file........")
        with pytest.raises(FideStorageError):
            storage.inspect_zip(zpath)

    def test_no_xml_member_rejected(self, tmp_path):
        zpath = self._make_zip(tmp_path, {"readme.txt": b"hello"})
        with pytest.raises(FideStorageError):
            storage.inspect_zip(zpath)

    def test_tmp_path_validated(self, tmp_path):
        base = str(tmp_path)
        with pytest.raises(ValueError):
            storage.tmp_zip_path(base, "2026-09", "classical")
        with pytest.raises(ValueError):
            storage.tmp_zip_path(base, "yesterday", "standard")
        path = storage.tmp_zip_path(base, "2026-09", "standard")
        assert os.path.abspath(path).startswith(os.path.abspath(base))


class TestUrlBuilder:
    def test_bad_inputs_rejected(self):
        with pytest.raises(ValueError):
            downloader.build_rating_url("http://x/", "classical", "2026-09")
        with pytest.raises(ValueError):
            downloader.build_rating_url("http://x/", "standard", "soon")

    def test_shape(self):
        url = downloader.build_rating_url(
            "http://ratings.fide.com/download", "standard", "2026-09")
        assert url == ("http://ratings.fide.com/download/"
                       "standard_sep26frl_xml.zip")


class TestInjection:
    def test_search_sql_literal_safe(self, client, seeded, auth_headers):
        resp = client.get("/api/v1/players/search",
                          query_string={"q": '" OR "1"="1'},
                          headers=auth_headers)
        assert resp.status_code == 200
        assert resp.get_json()["data"] == []

    def test_batch_non_string_ids_tolerated(self, client, seeded,
                                            auth_headers):
        resp = client.post("/api/v1/players/batch",
                           json={"ids": [None, 123, "12512294"]},
                           headers=auth_headers)
        assert resp.status_code == 200
        body = resp.get_json()["data"]
        assert [p["fide_id"] for p in body["found"]] == ["12512294"]

    def test_error_bodies_carry_no_secrets(self, client, auth_headers):
        resp = client.get("/api/v1/players/00000000", headers=auth_headers)
        text = resp.get_data(as_text=True)
        assert "test-read-key" not in text
        assert "Traceback" not in text
