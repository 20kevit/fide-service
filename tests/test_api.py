"""API tests: auth, resources, limits, batch, admin, error envelope."""
import pytest

from config import Settings
from interfaces.api.auth import reset_rate_limits
from interfaces.factory import create_app
from tests.conftest import ADMIN_KEY, READ_KEY, make_settings


def _get(client, path, headers):
    return client.get(path, headers=headers)


class TestAuth:
    def test_missing_key_401(self, client):
        resp = client.get("/api/v1/status")
        assert resp.status_code == 401
        body = resp.get_json()
        assert body["error"]["code"] == "UNAUTHORIZED"
        assert "request_id" in body["error"]

    def test_wrong_key_401(self, client):
        resp = client.get("/api/v1/status",
                          headers={"X-API-Key": "wrong"})
        assert resp.status_code == 401

    def test_key_never_leaks(self, client):
        resp = client.get("/api/v1/status",
                          headers={"X-API-Key": "wrong"})
        assert READ_KEY not in resp.get_data(as_text=True)

    def test_unconfigured_key_fails_closed(self, tmp_path):
        settings = make_settings(tmp_path, api_key="", admin_key="")
        app = create_app(settings)
        with app.app_context():
            from infrastructure import migrations
            from infrastructure.database import db
            migrations.upgrade(db.engine)
            cli = app.test_client()
            resp = cli.get("/api/v1/status",
                           headers={"X-API-Key": "anything"})
            assert resp.status_code == 503
            assert resp.get_json()["error"]["code"] == "SERVICE_UNAVAILABLE"

    def test_healthz_open(self, client):
        resp = client.get("/api/v1/healthz")
        assert resp.status_code == 200
        assert resp.get_json() == {"ok": True}

    def test_rate_limit_429(self, tmp_path):
        settings = make_settings(tmp_path, rate_limit_per_minute=2)
        app = create_app(settings)
        reset_rate_limits()
        with app.app_context():
            from infrastructure import migrations
            from infrastructure.database import db
            migrations.upgrade(db.engine)
            cli = app.test_client()
            headers = {"X-API-Key": READ_KEY}
            assert cli.get("/api/v1/status", headers=headers).status_code == 200
            assert cli.get("/api/v1/status", headers=headers).status_code == 200
            resp = cli.get("/api/v1/status", headers=headers)
            assert resp.status_code == 429
            assert resp.get_json()["error"]["code"] == "RATE_LIMITED"
            assert "Retry-After" in resp.headers
        reset_rate_limits()


class TestPlayer:
    def test_found(self, client, seeded, auth_headers):
        resp = _get(client, "/api/v1/players/12512294", auth_headers)
        assert resp.status_code == 200
        body = resp.get_json()
        data = body["data"]
        assert data["fide_id"] == "12512294"
        assert data["federation"] == "IRI"
        assert data["title"] == "GM"
        assert data["canonical_title"] == "GM"
        assert data["latest"]["standard"]["rating"] == 2612
        assert data["latest"]["standard"]["period"] == "2026-09"
        assert data["latest"]["rapid"]["rating"] == 2550
        assert body["dataset"]["period"] == "2026-09"
        assert body["dataset"]["source"] == "fide-official-xml"

    def test_wtitle_canonical(self, client, seeded, auth_headers):
        resp = _get(client, "/api/v1/players/12512295", auth_headers)
        assert resp.get_json()["data"]["canonical_title"] == "WIM"

    def test_not_found(self, client, seeded, auth_headers):
        resp = _get(client, "/api/v1/players/00000000", auth_headers)
        assert resp.status_code == 404
        assert resp.get_json()["error"]["code"] == "NOT_FOUND"

    def test_empty_id_bad_request(self, client, seeded, auth_headers):
        resp = _get(client, "/api/v1/players/%20", auth_headers)
        assert resp.status_code in (400, 404)


class TestSearch:
    def test_basic(self, client, seeded, auth_headers):
        resp = _get(client, "/api/v1/players/search?q=1251229",
                    auth_headers)
        assert resp.status_code == 200
        body = resp.get_json()
        assert {p["fide_id"] for p in body["data"]} == {
            "12512294", "12512295"}
        assert body["dataset"]["period"] == "2026-09"

    def test_short_query_rejected(self, client, seeded, auth_headers):
        resp = _get(client, "/api/v1/players/search?q=x", auth_headers)
        assert resp.status_code == 400
        resp = _get(client, "/api/v1/players/search", auth_headers)
        assert resp.status_code == 400

    def test_federation_filter(self, client, seeded, auth_headers):
        resp = _get(client, "/api/v1/players/search?q=test&federation=IRI",
                    auth_headers)
        assert {p["fide_id"] for p in resp.get_json()["data"]} == {
            "12512294", "12512295"}

    def test_limit_enforced(self, client, seeded, auth_headers):
        resp = _get(client, "/api/v1/players/search?q=1251229&limit=1",
                    auth_headers)
        assert len(resp.get_json()["data"]) == 1
        resp = _get(client, "/api/v1/players/search?q=1251229&limit=500",
                    auth_headers)
        assert resp.status_code == 400


class TestRatings:
    def test_latest_all(self, client, seeded, auth_headers):
        resp = _get(client, "/api/v1/players/12512294/ratings", auth_headers)
        assert resp.status_code == 200
        ratings = resp.get_json()["data"]["ratings"]
        assert ratings["standard"] == {"rating": 2612, "games": 9,
                                       "k_factor": 10, "period": "2026-09"}
        assert ratings["blitz"]["rating"] == 2501

    def test_latest_single_type(self, client, seeded, auth_headers):
        resp = _get(client, "/api/v1/players/12512294/ratings?type=rapid",
                    auth_headers)
        assert list(resp.get_json()["data"]["ratings"]) == ["rapid"]

    def test_bad_type(self, client, seeded, auth_headers):
        resp = _get(client, "/api/v1/players/12512294/ratings?type=nope",
                    auth_headers)
        assert resp.status_code == 400

    def test_period_snapshot(self, client, seeded, auth_headers):
        resp = _get(client,
                    "/api/v1/players/12512294/ratings?period=2026-08",
                    auth_headers)
        body = resp.get_json()
        assert body["data"]["ratings"]["standard"]["rating"] == 2601
        assert body["data"]["ratings"]["rapid"] is None

    def test_history(self, client, seeded, auth_headers):
        resp = _get(client, "/api/v1/players/12512294/ratings/history",
                    auth_headers)
        hist = resp.get_json()["data"]["history"]
        assert [p["period"] for p in hist["standard"]] == [
            "2026-08", "2026-09"]
        assert hist["rapid"][0]["rating"] == 2550

    def test_history_type_and_range(self, client, seeded, auth_headers):
        resp = _get(client, "/api/v1/players/12512294/ratings/history"
                            "?type=standard&from=2026-09&to=2026-09",
                    auth_headers)
        hist = resp.get_json()["data"]["history"]
        assert list(hist) == ["standard"]
        assert len(hist["standard"]) == 1

    def test_unknown_player_404(self, client, seeded, auth_headers):
        resp = _get(client, "/api/v1/players/000/ratings/history",
                    auth_headers)
        assert resp.status_code == 404


class TestBatch:
    def test_found_and_missing(self, client, seeded, auth_headers):
        resp = client.post("/api/v1/players/batch",
                           json={"ids": ["12512294", "00000000"]},
                           headers=auth_headers)
        assert resp.status_code == 200
        body = resp.get_json()
        assert [p["fide_id"] for p in body["data"]["found"]] == ["12512294"]
        assert body["data"]["missing"] == ["00000000"]
        assert body["data"]["found"][0]["latest"]["standard"]["rating"] == 2612

    def test_alias_key(self, client, seeded, auth_headers):
        resp = client.post("/api/v1/players/batch",
                           json={"fide_ids": ["12512295"]},
                           headers=auth_headers)
        assert resp.status_code == 200
        assert resp.get_json()["data"]["missing"] == []

    def test_too_many(self, client, seeded, auth_headers):
        resp = client.post("/api/v1/players/batch",
                           json={"ids": ["1"] * 101}, headers=auth_headers)
        assert resp.status_code == 400

    def test_empty(self, client, seeded, auth_headers):
        resp = client.post("/api/v1/players/batch", json={"ids": []},
                           headers=auth_headers)
        assert resp.status_code == 400

    def test_non_json(self, client, seeded, auth_headers):
        resp = client.post("/api/v1/players/batch", data="nope",
                           headers=auth_headers)
        assert resp.status_code == 400


class TestStatus:
    def test_status(self, client, seeded, auth_headers):
        resp = _get(client, "/api/v1/status", auth_headers)
        body = resp.get_json()["data"]
        assert body["players"] == 3
        assert body["latest_period"] == "2026-09"
        assert body["by_type"]["standard"] == 5


class TestAdmin:
    def test_ensure_and_list(self, client, admin_headers):
        resp = client.post("/api/v1/admin/jobs/ensure",
                           json={"period": "2026-09"}, headers=admin_headers)
        assert resp.status_code == 202
        assert len(resp.get_json()["data"]["jobs"]) == 3
        resp = client.get("/api/v1/admin/jobs?period=2026-09",
                          headers=admin_headers)
        assert len(resp.get_json()["data"]["jobs"]) == 3

    def test_bad_period(self, client, admin_headers):
        resp = client.post("/api/v1/admin/jobs/ensure",
                           json={"period": "nope"}, headers=admin_headers)
        assert resp.status_code == 400

    def test_retry_only_failed(self, client, admin_headers):
        client.post("/api/v1/admin/jobs/ensure", json={"period": "2026-09"},
                    headers=admin_headers)
        resp = client.post("/api/v1/admin/jobs/retry",
                           json={"period": "2026-09",
                                 "rating_type": "standard"},
                           headers=admin_headers)
        assert resp.status_code == 400  # pending, not failed

    def test_read_key_rejected_on_admin(self, client, auth_headers):
        # Authenticated but not authorized -> 403, not 401.
        resp = client.get("/api/v1/admin/jobs", headers=auth_headers)
        assert resp.status_code == 403
        assert resp.get_json()["error"]["code"] == "FORBIDDEN"

    def test_admin_key_accepted(self, client, admin_headers):
        resp = client.get("/api/v1/admin/jobs", headers=admin_headers)
        assert resp.status_code == 200

    def test_no_credential_401(self, client):
        resp = client.get("/api/v1/admin/jobs")
        assert resp.status_code == 401

    def test_failed_job_error_sanitized(self, client, admin_headers):
        from domain.errors import public_error_summary
        from infrastructure.database import db
        from infrastructure.models import FideImportJobModel
        client.post("/api/v1/admin/jobs/ensure", json={"period": "2026-09"},
                    headers=admin_headers)
        job = db.session.query(FideImportJobModel).filter_by(
            period="2026-09", rating_type="standard").first()
        job.state = "failed"
        job.error_code = "DATABASE_ERROR"
        job.error_message = (
            'DATABASE_ERROR: (pymysql.err.DataError) (1406, "Data too long")'
            " [SQL: INSERT INTO fide_players (name) VALUES ('Secret Player')]")
        db.session.commit()
        jobs = client.get("/api/v1/admin/jobs?period=2026-09",
                          headers=admin_headers).get_json()["data"]["jobs"]
        std = [j for j in jobs if j["rating_type"] == "standard"][0]
        assert std["error_code"] == "DATABASE_ERROR"
        assert std["error_message"] == public_error_summary("DATABASE_ERROR")
        for leaked in ("INSERT", "Secret Player", "1406", "pymysql", "fide_players"):
            assert leaked not in std["error_message"]

    def test_verify_endpoint(self, client, seeded, admin_headers):
        resp = client.get("/api/v1/admin/verify", headers=admin_headers)
        body = resp.get_json()["data"]
        assert body["ok"] is True
        assert body["players"] == 3

    def test_upload_stages_zip(self, client, admin_headers, fixture_path):
        import io
        xml = open(fixture_path("small_rapid.xml"), "rb").read()
        buf = io.BytesIO()
        import zipfile
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("rapid_sep26frl.xml", xml)
        buf.seek(0)
        resp = client.post("/api/v1/admin/uploads",
                           data={"period": "2026-09",
                                 "rating_type": "rapid",
                                 "file": (buf, "rapid.zip")},
                           headers=admin_headers,
                           content_type="multipart/form-data")
        assert resp.status_code == 201
        assert resp.get_json()["data"]["rating_type"] == "rapid"

    def test_upload_rejects_non_zip(self, client, admin_headers,
                                    fixture_path):
        import io
        buf = io.BytesIO(b"not a zip at all")
        resp = client.post("/api/v1/admin/uploads",
                           data={"period": "2026-09",
                                 "rating_type": "rapid",
                                 "file": (buf, "rapid.zip")},
                           headers=admin_headers,
                           content_type="multipart/form-data")
        assert resp.status_code == 400


class TestEnvelope:
    def test_unknown_route(self, client):
        resp = client.get("/api/v1/nope")
        assert resp.status_code == 404
        assert set(resp.get_json()["error"]) == {
            "code", "message", "details", "request_id"}

    def test_wrong_method(self, client, auth_headers):
        resp = client.post("/api/v1/status", headers=auth_headers)
        assert resp.status_code == 400
        assert resp.get_json()["error"]["code"] == "BAD_REQUEST"
