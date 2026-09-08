"""Security tests: XML/ZIP safety, traversal, injection, secret hygiene."""
import io
import logging
import os
import zipfile

import pytest

from domain import xml_validation
from domain.errors import FideError, FideErrorCategory
from infrastructure import downloader, storage
from infrastructure.storage import FideStorageError
from interfaces.api.auth import reset_rate_limits
from interfaces.factory import create_app
from tests.conftest import ADMIN_KEY, READ_KEY, make_settings


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

    def _padded_dtd_path(self, tmp_path):
        path = str(tmp_path / "padded.xml")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write('<?xml version="1.0" encoding="UTF-8"?>\n<!--')
            fh.write("x" * (70 * 1024))  # push the DOCTYPE past 64KB
            fh.write('-->\n<!DOCTYPE playerslist [<!ENTITY x "boom">]>\n'
                     '<playerslist><player><fideid>1</fideid><name>A, B</name>'
                     '<country>IRI</country></player></playerslist>')
        return path

    def test_marker_after_64k_rejected(self, tmp_path):
        # The old head-only scan would miss this; the stream scan must not.
        with pytest.raises(FideError) as exc:
            xml_validation.validate_xml_path(self._padded_dtd_path(tmp_path))
        assert exc.value.category == FideErrorCategory.INVALID_XML

    def test_marker_after_64k_rejected_stream(self, tmp_path):
        path = self._padded_dtd_path(tmp_path)
        with pytest.raises(FideError) as exc:
            xml_validation.validate_stream(
                lambda: open(path, "rb"), "padded",
                size_bytes=os.path.getsize(path))
        assert exc.value.category == FideErrorCategory.INVALID_XML

    def test_scan_catches_split_marker(self):
        # Marker straddling tiny read() chunks (overlap logic).
        from domain.xml_validation import _MarkerScanningStream
        raw = b"<playerslist>" + b"y" * 100 + b"<!ENT" + b"ITY x>"
        stream = _MarkerScanningStream(io.BytesIO(raw))
        with pytest.raises(FideError) as exc:
            while stream.read(7):
                pass
        assert exc.value.category == FideErrorCategory.INVALID_XML

    def test_legit_large_xml_passes(self, tmp_path):
        path = str(tmp_path / "big.xml")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("<playerslist>")
            for i in range(3000):
                fh.write(
                    "<player><fideid>%d</fideid><name>Big, Player %d</name>"
                    "<country>IRI</country><rating>2500</rating><games>9</games>"
                    "<k>10</k></player>" % (20000000 + i, i))
            fh.write("</playerslist>")
        result = xml_validation.validate_stream(
            lambda: open(path, "rb"), "big",
            size_bytes=os.path.getsize(path))
        # Validator early-exits at 2000 once the sample is proven; the
        # point here is a multi-hundred-KB clean file streams through.
        assert result.players_found == 2000
        assert result.sample_ok is True


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

    def test_https_shape_preserved(self):
        url = downloader.build_rating_url(
            "https://ratings.fide.com/download", "standard", "2026-09")
        assert url == ("https://ratings.fide.com/download/"
                       "standard_sep26frl_xml.zip")

    def test_source_is_secure(self):
        assert downloader.source_is_secure(
            "https://ratings.fide.com/download/standard_sep26frl_xml.zip")
        assert not downloader.source_is_secure(
            "http://ratings.fide.com/download/standard_sep26frl_xml.zip")
        assert not downloader.source_is_secure("notaurl")

    def test_default_base_is_https(self):
        from config import Settings
        assert Settings().download_base_url.startswith("https://")


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


class TestTransportHeaders:
    def test_api_sets_minimal_headers(self, client, auth_headers):
        for path in ("/api/v1/healthz", "/api/v1/status"):
            resp = client.get(path, headers=auth_headers)
            assert resp.headers.get("X-Content-Type-Options") == "nosniff"
            assert resp.headers.get("Referrer-Policy") == "no-referrer"
            # HSTS/redirect are gateway-owned (see deployment/htaccess.sample).
            assert "Strict-Transport-Security" not in resp.headers


def _limited_app(tmp_path, **overrides):
    """App with a tiny pre-auth budget (deterministic blocking)."""
    args = dict(auth_fail_limit_per_hour=3)
    args.update(overrides)
    app = create_app(make_settings(tmp_path, **args))
    with app.app_context():
        from infrastructure import migrations
        from infrastructure.database import db
        migrations.upgrade(db.engine)
    return app


class TestPreAuthLimiter:
    def test_bad_key_blocked_after_budget(self, tmp_path):
        reset_rate_limits()
        cli = _limited_app(tmp_path).test_client()
        bad = {"X-API-Key": "nope"}
        try:
            # Budget 3 allows 3 + 1 counted rejections; then 429.
            for _ in range(4):
                assert cli.get("/api/v1/status", headers=bad).status_code == 401
            resp = cli.get("/api/v1/status", headers=bad)
            assert resp.status_code == 429
            assert resp.get_json()["error"]["code"] == "RATE_LIMITED"
            assert "Retry-After" in resp.headers
        finally:
            reset_rate_limits()

    def test_rejection_logs_are_safe(self, tmp_path, caplog):
        reset_rate_limits()
        cli = _limited_app(tmp_path).test_client()
        guess = "super-secret-guess-xyz"
        try:
            with caplog.at_level(logging.WARNING,
                                 logger="interfaces.api.auth"):
                assert cli.get("/api/v1/status",
                               headers={"X-API-Key": guess}).status_code == 401
            assert guess not in caplog.text
            assert "key_fp=" in caplog.text
            assert "event=bad_read_key" in caplog.text
            assert "status=401" in caplog.text
        finally:
            reset_rate_limits()

    def test_403_logged_without_credential(self, tmp_path, caplog):
        reset_rate_limits()
        cli = _limited_app(tmp_path).test_client()
        try:
            with caplog.at_level(logging.WARNING,
                                 logger="interfaces.api.auth"):
                resp = cli.get("/api/v1/admin/jobs",
                               headers={"X-API-Key": READ_KEY})
                assert resp.status_code == 403
            assert READ_KEY not in caplog.text
            assert "event=read_key_on_admin" in caplog.text
        finally:
            reset_rate_limits()

    def test_forwarded_for_ignored(self, tmp_path):
        reset_rate_limits()
        cli = _limited_app(tmp_path).test_client()
        try:
            # Spoofed XFF must not buy a fresh budget: the bucket keys on
            # the direct peer (127.0.0.1 in tests) only.
            for i in range(4):
                resp = cli.get(
                    "/api/v1/status", headers={
                        "X-API-Key": "nope",
                        "X-Forwarded-For": "10.9.9.%d" % i})
                assert resp.status_code == 401
            resp = cli.get("/api/v1/status", headers={
                "X-API-Key": "nope", "X-Forwarded-For": "10.9.9.99"})
            assert resp.status_code == 429
        finally:
            reset_rate_limits()

    def test_reset_clears_failures(self, tmp_path):
        reset_rate_limits()
        cli = _limited_app(tmp_path).test_client()
        bad = {"X-API-Key": "nope"}
        try:
            for _ in range(5):
                cli.get("/api/v1/status", headers=bad)
            assert cli.get("/api/v1/status", headers=bad).status_code == 429
            reset_rate_limits()
            assert cli.get("/api/v1/status", headers=bad).status_code == 401
        finally:
            reset_rate_limits()

    def test_post_auth_429_transition_logged_once(self, tmp_path, caplog):
        reset_rate_limits()
        app = create_app(make_settings(
            tmp_path, rate_limit_per_minute=2,
            auth_fail_limit_per_hour=1000))
        with app.app_context():
            from infrastructure import migrations
            from infrastructure.database import db
            migrations.upgrade(db.engine)
            cli = app.test_client()
            headers = {"X-API-Key": READ_KEY}
            try:
                with caplog.at_level(logging.WARNING,
                                     logger="interfaces.api.auth"):
                    assert cli.get("/api/v1/status",
                                   headers=headers).status_code == 200
                    assert cli.get("/api/v1/status",
                                   headers=headers).status_code == 200
                    assert cli.get("/api/v1/status",
                                   headers=headers).status_code == 429
                    assert cli.get("/api/v1/status",
                                   headers=headers).status_code == 429
                hits = [r for r in caplog.records
                        if "rate limit exceeded" in r.getMessage()]
                assert len(hits) == 1
                assert READ_KEY not in caplog.text
            finally:
                reset_rate_limits()
        reset_rate_limits()
