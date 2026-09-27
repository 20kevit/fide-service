"""Data integrity & correctness tests (Phase 6, adversarial).

Proves the service preserves correct, complete, deterministic FIDE data:
identity rules, per-type isolation, period trust boundaries, duplicate
and malformed policies, batch boundaries, pagination, constraints,
pruning, transitions, API/DB consistency, and backup/restore — all on
disposable databases, never Production.

Policies documented here (not imposed): lenient verbatim parsing
(bad ints become 0, missing tags become ""), current-period-wins for
identity, last-write-wins for duplicate rows, no period metadata inside
the XML itself (job period is trusted; uploads now report a filename
verdict — see TestUploadPeriodMatch).
"""
import os
import zipfile

import pytest

from application import checks, import_service
from application.import_service import JobRunnerConfig
from infrastructure import storage
from infrastructure.database import db
from infrastructure.models import FidePlayerModel, FideRatingModel
from infrastructure.repositories import (
    FideImportJobRepository, FidePlayerRepository, FideRatingRepository,
)
from tests.test_import import make_cfg, run_period
from tests.test_recovery import _stage, _standard_xml

PERIOD = "2026-09"
PREV = "2026-08"


def _modified_standard(fixture_path, old, new):
    return _standard_xml(fixture_path).replace(old, new)


def _players_xml(n, start=70000000, country="IRI"):
    parts = ["<playerslist>"]
    for i in range(n):
        parts.append(
            "<player><fideid>%d</fideid><name>Bound, P%d</name>"
            "<country>%s</country><sex>M</sex><rating>%d</rating>"
            "<games>9</games><k>20</k></player>"
            % (start + i, i, country, 2000 + i))
    parts.append("</playerslist>")
    return "".join(parts).encode("utf-8")


def _snapshot():
    players = sorted(
        (p.fide_id, p.name, p.federation, p.sex, p.title, p.wtitle,
         p.otitle, p.foatitle, p.birth_year, p.inactive)
        for p in db.session.query(FidePlayerModel).all())
    ratings = sorted(
        (r.fide_id, r.period, r.rating_type, r.rating, r.games, r.k_factor)
        for r in db.session.query(FideRatingModel).all())
    return players, ratings


class TestIdentityRules:
    """Current period wins for identity; skips are explicit, never silent."""

    def test_changed_identity_overwritten_by_later_period(
            self, app, fixture_path):
        cfg = make_cfg(app, fixture_path)
        run_period(app, cfg, PREV)
        xml = _modified_standard(fixture_path, b"Alpha, Test One",
                                 b"Alpha, Renamed One")
        xml = xml.replace(
            b"<fideid>12512294</fideid><name>Alpha, Renamed One</name>"
            b"<country>IRI</country>",
            b"<fideid>12512294</fideid><name>Alpha, Renamed One</name>"
            b"<country>FRA</country>")
        xml = xml.replace(b"<title>GM</title>", b"<title>IM</title>", 1)
        xml = xml.replace(b"<birthday>1999</birthday>",
                          b"<birthday>1999-01-01</birthday>", 1)
        _stage(cfg, PERIOD, "standard", xml)
        import_service.ensure_period_jobs(PERIOD)
        assert import_service.tick(PERIOD, cfg)["status"] == "success"
        player = FidePlayerRepository.get_by_fide_id("12512294")
        assert (player.name, player.federation, player.title,
                player.birth_year) == (
                    "Alpha, Renamed One", "FRA", "IM", "1999-01-01")
        # Both periods of ratings retained (IRI history federation).
        periods = {r.period for r in
                   FideRatingRepository.history("12512294")}
        assert periods == {PREV, PERIOD}

    def test_federation_normalized_at_write_boundary(
            self, app, fixture_path):
        from application import player_service
        cfg = make_cfg(app, fixture_path)
        run_period(app, cfg, PREV)
        xml = _standard_xml(fixture_path).replace(
            b"<country>IRI</country>", b"<country> iri </country>")
        xml = xml.replace(b"<country>GER</country>", b"<country>ger</country>")
        _stage(cfg, PERIOD, "standard", xml)
        import_service.ensure_period_jobs(PERIOD)
        assert import_service.tick(PERIOD, cfg)["status"] == "success"
        # Stored uppercase without padding...
        assert (FidePlayerRepository.get_by_fide_id("12512294").federation
                == "IRI")
        assert (FidePlayerRepository.get_by_fide_id("99900001").federation
                == "GER")
        # ...searchable through the normalizing read filter...
        found = player_service.search_players("Alpha", federation="iri")
        assert [p["fide_id"] for p in found["data"]] == ["12512294"]
        # ...and pruned by the real federation: IRI history kept, the
        # lowercased GER history pruned like any other foreign row.
        assert {r.period for r in FideRatingRepository.history(
            "12512294", rating_type="standard")} == {PREV, PERIOD}
        assert {r.period for r in FideRatingRepository.history(
            "99900001", rating_type="standard")} == {PERIOD}

    def test_empty_name_skipped_not_stored(self, app, fixture_path):
        cfg = make_cfg(app, fixture_path)
        import_service.ensure_period_jobs(PERIOD)
        xml = (_standard_xml(fixture_path).replace(
            b"<name>Beta, Test Two</name>", b"<name>   </name>"))
        _stage(cfg, PERIOD, "standard", xml)
        assert import_service.tick(PERIOD, cfg)["status"] == "success"
        assert FidePlayerRepository.get_by_fide_id("12512295") is None
        assert FidePlayerRepository.count() == 3

    def test_padded_name_preserved_verbatim(self, app, fixture_path):
        # No trimming anywhere: what the source says is what is stored.
        cfg = make_cfg(app, fixture_path)
        import_service.ensure_period_jobs(PERIOD)
        xml = (_standard_xml(fixture_path).replace(
            b"<name>Beta, Test Two</name>", b"<name>  Spaced, Name  </name>"))
        _stage(cfg, PERIOD, "standard", xml)
        assert import_service.tick(PERIOD, cfg)["status"] == "success"
        player = FidePlayerRepository.get_by_fide_id("12512295")
        assert player.name == "  Spaced, Name  "

    def test_leading_zero_id_preserved_as_string(self, app, fixture_path):
        # IDs are strings, never integers: no normalization, no collision
        # between "00125" and "125".
        cfg = make_cfg(app, fixture_path)
        import_service.ensure_period_jobs(PERIOD)
        xml = (_standard_xml(fixture_path).replace(
            b"<fideid>12512295</fideid>", b"<fideid>0012512295</fideid>"))
        _stage(cfg, PERIOD, "standard", xml)
        assert import_service.tick(PERIOD, cfg)["status"] == "success"
        assert FidePlayerRepository.get_by_fide_id("0012512295") is not None
        assert FidePlayerRepository.get_by_fide_id("12512295") is None

    def test_missing_sex_defaults_male(self, app, fixture_path):
        cfg = make_cfg(app, fixture_path)
        import_service.ensure_period_jobs(PERIOD)
        xml = (_standard_xml(fixture_path).replace(
            b"<sex>F</sex>", b""))
        _stage(cfg, PERIOD, "standard", xml)
        assert import_service.tick(PERIOD, cfg)["status"] == "success"
        assert FidePlayerRepository.get_by_fide_id("12512295").sex == "M"


class TestRatingSemantics:
    """Standard/Rapid/Blitz can never cross-contaminate."""

    def test_rapid_values_never_become_standard(
            self, app, fixture_path):
        cfg = make_cfg(app, fixture_path)
        import_service.ensure_period_jobs(PERIOD)
        xml = open(fixture_path("small_rapid.xml"), "rb").read().replace(
            b"<rating>2210</rating>", b"<rating>2999</rating>")
        _stage(cfg, PERIOD, "rapid", xml)
        # Rapid file staged but standard claims first: stage standard too
        # from the real fixture, then run the whole period.
        _stage(cfg, PERIOD, "standard", _standard_xml(fixture_path))
        _stage(cfg, PERIOD, "blitz",
               open(fixture_path("small_blitz.xml"), "rb").read())
        assert [import_service.tick(PERIOD, cfg)["status"]
                for _ in range(3)] == ["success"] * 3
        std = FideRatingRepository.get("12512295", PERIOD, "standard")
        rpd = FideRatingRepository.get("12512295", PERIOD, "rapid")
        assert (std.rating, rpd.rating) == (2200, 2999)
        assert FideRatingRepository.get(
            "12512295", PERIOD, "blitz") is None

    def test_standard_only_import_creates_no_other_types(
            self, app, fixture_path):
        cfg = make_cfg(app, fixture_path)
        import_service.ensure_period_jobs(PERIOD)
        _stage(cfg, PERIOD, "standard", _standard_xml(fixture_path))
        assert import_service.tick(PERIOD, cfg)["status"] == "success"
        assert FideRatingRepository.count_by_type(PERIOD) == {"standard": 4}

    def test_zero_games_and_k_preserved(self, app, fixture_path):
        # Delta, Inactive One: games=0, k=40 — falsy but valid, kept as-is.
        cfg = make_cfg(app, fixture_path)
        run_period(app, cfg, PERIOD)
        row = FideRatingRepository.get("12512296", PERIOD, "standard")
        assert (row.rating, row.games, row.k_factor) == (2100, 0, 40)
        assert FidePlayerRepository.get_by_fide_id("12512296").inactive is True

    def test_malformed_numbers_become_zero(self, app, fixture_path):
        # Lenient verbatim policy: unparseable ints become 0, row kept.
        cfg = make_cfg(app, fixture_path)
        import_service.ensure_period_jobs(PERIOD)
        xml = (_standard_xml(fixture_path).replace(
            b"<rating>2200</rating>", b"<rating>abc</rating>").replace(
            b"<games>12</games>", b"<games>many</games>").replace(
            b"<k>20</k>", b"", 1))
        _stage(cfg, PERIOD, "standard", xml)
        assert import_service.tick(PERIOD, cfg)["status"] == "success"
        row = FideRatingRepository.get("12512295", PERIOD, "standard")
        assert (row.rating, row.games, row.k_factor) == (0, 0, 0)


class TestPeriodTrust:
    """The XML carries no period: the job period is trusted completely."""

    def test_mislabeled_content_imports_under_job_period(
            self, app, fixture_path):
        # August bytes staged as September import AS September, with
        # August values. The download path cannot do this (URL encodes
        # the period); staged files rely on the operator + the upload
        # filename verdict (TestUploadPeriodMatch).
        cfg = make_cfg(app, fixture_path)
        import_service.ensure_period_jobs(PERIOD)
        august = open(fixture_path("small_rapid.xml"), "rb").read()
        _stage(cfg, PERIOD, "standard", august)
        assert import_service.tick(PERIOD, cfg)["status"] == "success"
        row = FideRatingRepository.get("12512294", PERIOD, "standard")
        assert row.period == PERIOD
        assert row.rating == 2560  # rapid-file value, standard slot


class TestUploadPeriodMatch:
    def test_official_names_match(self):
        from infrastructure.downloader import archive_matches_period
        assert archive_matches_period(
            "standard_sep26frl_xml.zip", "standard", "2026-09")
        assert archive_matches_period(
            "rapid_AUG26frl_xml.zip", "rapid", "2026-08")
        assert archive_matches_period(
            "BLITZ_Jan25FRL_XML.ZIP", "blitz", "2025-01")

    def test_mismatch_cases(self):
        from infrastructure.downloader import archive_matches_period
        assert not archive_matches_period(
            "standard_aug26frl_xml.zip", "standard", "2026-09")
        assert not archive_matches_period(
            "rapid_sep26frl_xml.zip", "standard", "2026-09")
        assert not archive_matches_period("rapid.zip", "rapid", "2026-09")
        assert not archive_matches_period("", "rapid", "2026-09")
        assert not archive_matches_period(
            "standard_sep26frl_xml.zip", "standard", "nope")

    def test_upload_reports_verdict_but_stages_anyway(
            self, client, admin_headers, fixture_path):
        import io
        xml = open(fixture_path("small_rapid.xml"), "rb").read()

        def upload_as(name):
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as zf:
                zf.writestr("players_list.xml", xml)
            buf.seek(0)
            return client.post(
                "/api/v1/admin/uploads",
                data={"period": "2026-09", "rating_type": "rapid",
                      "file": (buf, name)},
                headers=admin_headers,
                content_type="multipart/form-data")

        resp = upload_as("rapid_sep26frl_xml.zip")
        assert resp.status_code == 201
        assert resp.get_json()["data"]["period_match"] is True
        resp = upload_as("rapid.zip")
        assert resp.status_code == 201
        assert resp.get_json()["data"]["period_match"] is False


class TestBatchBoundaries:
    @pytest.mark.parametrize("count", [6, 7])
    def test_exact_and_partial_final_batches(
            self, app, fixture_path, count):
        cfg = make_cfg(app, fixture_path)
        cfg.batch_size = 3
        import_service.ensure_period_jobs(PERIOD)
        _stage(cfg, PERIOD, "standard", _players_xml(count))
        assert import_service.tick(PERIOD, cfg)["status"] == "success"
        assert FideRatingRepository.count_by_type(PERIOD) == {"standard": count}
        assert FidePlayerRepository.count() == count


class TestMalformedPolicy:
    def test_missing_country_becomes_empty(self, app, fixture_path):
        cfg = make_cfg(app, fixture_path)
        import_service.ensure_period_jobs(PERIOD)
        xml = (_standard_xml(fixture_path).replace(
            b"<country>GER</country>", b"", 1))
        _stage(cfg, PERIOD, "standard", xml)
        assert import_service.tick(PERIOD, cfg)["status"] == "success"
        assert FidePlayerRepository.get_by_fide_id("99900001").federation == ""


class TestPaginationAndBatch:
    def _two_periods(self, app, fixture_path):
        cfg = make_cfg(app, fixture_path)
        run_period(app, cfg, PREV)
        run_period(app, cfg, PERIOD)
        return cfg

    def test_history_slicing(self, client, seeded, auth_headers):
        # Seeded: 12512294 standard has 2026-08 and 2026-09 rows. The
        # type filter is required here: unfiltered history interleaves
        # rapid/blitz rows in (period, type) order.
        base = "/api/v1/players/12512294/ratings/history"
        args = {"type": "standard", "limit": 1}
        first = client.get(base, query_string=dict(args, offset=0),
                           headers=auth_headers).get_json()
        second = client.get(base, query_string=dict(args, offset=1),
                            headers=auth_headers).get_json()
        assert first["data"]["history"]["standard"][0]["period"] == "2026-08"
        assert second["data"]["history"]["standard"][0]["period"] == "2026-09"
        empty = client.get(base, query_string=dict(args, offset=99, limit=10),
                           headers=auth_headers).get_json()
        assert empty["data"]["history"]["standard"] == []

    def test_history_limit_bounds(self, client, seeded, auth_headers):
        base = "/api/v1/players/12512294/ratings/history"
        assert client.get(base, query_string={"limit": 1000},
                          headers=auth_headers).status_code == 200
        assert client.get(base, query_string={"limit": 1001},
                          headers=auth_headers).status_code == 400
        assert client.get(base, query_string={"limit": 0},
                          headers=auth_headers).status_code == 400

    def test_batch_duplicates_and_order(self, client, seeded, auth_headers):
        resp = client.post(
            "/api/v1/players/batch",
            json={"ids": ["99900001", "12512294", "99900001", "nope-1"]},
            headers=auth_headers)
        body = resp.get_json()["data"]
        assert [p["fide_id"] for p in body["found"]] == [
            "99900001", "12512294"]
        assert body["missing"] == ["nope-1"]

    def test_batch_all_invalid_rejected(self, client, auth_headers):
        resp = client.post("/api/v1/players/batch", json={"ids": [None, 123]},
                           headers=auth_headers)
        assert resp.status_code == 400

    def test_search_limit_respected(self, client, seeded, auth_headers):
        resp = client.get("/api/v1/players/search",
                          query_string={"q": "Test", "limit": 1},
                          headers=auth_headers).get_json()
        assert len(resp["data"]) == 1
        assert resp["pagination"]["total"] == 1


class TestNullEmptyZero:
    def test_fixture_row_semantics(self, app, fixture_path):
        cfg = make_cfg(app, fixture_path)
        run_period(app, cfg, PERIOD)
        beta = FidePlayerRepository.get_by_fide_id("12512295")
        assert (beta.title, beta.wtitle) == ("", "WIM")
        assert beta.birth_year == "2005"
        assert beta.inactive is False
        gamma = FidePlayerRepository.get_by_fide_id("99900001")
        assert gamma.federation == "GER"


class TestConstraintsAndVerify:
    def test_invalid_rating_type_flagged_not_stored_by_app(
            self, client, seeded, admin_headers):
        # No CHECK constraint: the DB accepts it, verification fails it.
        db.session.add(FideRatingModel(
            fide_id="12512294", period="2026-09", rating_type="classical",
            rating=2500))
        db.session.commit()
        body = client.get("/api/v1/admin/verify",
                          headers=admin_headers).get_json()["data"]
        assert body["ok"] is False
        assert body["invalid_rating_types"] == ["classical"]
        # Row persists (test database is discarded with the fixture).

    def test_unknown_job_state_is_inert(self, app, fixture_path):
        # Manually corrupted predecessor: no crash, no claim, recoverable.
        cfg = make_cfg(app, fixture_path)
        import_service.ensure_period_jobs(PERIOD)
        FideImportJobRepository.get(PERIOD, "standard").state = "weird"
        db.session.commit()
        result = import_service.tick(PERIOD, cfg)
        assert result["status"] == "idle"
        # "weird" matches no branch, so the pending rapid job sits blocked
        # behind its unrecognized predecessor — inert, no crash, no claim.
        assert result["reason"] == "blocked"
        FideImportJobRepository.get(PERIOD, "standard").state = "failed"
        db.session.commit()
        import_service.retry_job(PERIOD, "standard")
        assert import_service.tick(PERIOD, cfg)["status"] == "success"

    def test_empty_database_verify_is_vacuous(self, app):
        # Vacuous truth: no rows means no violations. The import-side
        # verify gate (processed>0/stored>0) is what fails closed instead.
        report = checks.verify_data("")
        assert report["ok"] is True
        assert (report["players"], report["ratings"]) == (0, 0)


class TestPruneExtended:
    def test_multi_federation_retention(self, app, fixture_path):
        cfg = make_cfg(app, fixture_path)
        run_period(app, cfg, PREV)
        run_period(app, cfg, PERIOD)

        def periods_of(fid, rtype="standard"):
            return {r.period for r in FideRatingRepository.history(
                fid, rating_type=rtype)}

        assert periods_of("12512294") == {PREV, PERIOD}  # IRI kept
        assert periods_of("99900001") == {PERIOD}  # GER old pruned...
        assert (PERIOD, "standard") in {
            (r.period, r.rating_type)
            for r in FideRatingRepository.history("99900001")}  # current kept

    def test_empty_keep_list_prunes_nothing(self, app, fixture_path):
        cfg = make_cfg(app, fixture_path)
        cfg.history_federations = []
        run_period(app, cfg, PREV)
        run_period(app, cfg, PERIOD)
        assert {r.period for r in FideRatingRepository.history(
            "99900001", rating_type="standard")} == {PREV, PERIOD}


class TestPruneScale:
    """Chunked prune at scale: progress, correctness, idempotency.

    Seeds hundreds of players across old + current periods (GER prunable,
    IRI protected) and prunes with a small chunk size so multiple chunks
    are exercised on the disposable test database.
    """

    OLD_PERIODS = ("2026-06", "2026-07")
    CURRENT = "2026-09"
    N_GER = 400
    N_IRI = 100

    def _seed_scale(self):
        from tests.conftest import seed_player, seed_rating
        for i in range(self.N_GER):
            fid = "9900%04d" % i
            seed_player(fid, name="Scale, Ger %d" % i, federation="GER")
            for per in self.OLD_PERIODS:
                seed_rating(fid, per, "standard", 2400)
            seed_rating(fid, self.CURRENT, "standard", 2410)
            if i < 10:
                # Other rating types must survive a standard prune.
                seed_rating(fid, self.OLD_PERIODS[0], "rapid", 2350)
        for i in range(self.N_IRI):
            fid = "1250%04d" % i
            seed_player(fid, name="Scale, Iri %d" % i, federation="IRI")
            for per in self.OLD_PERIODS:
                seed_rating(fid, per, "standard", 2300)
            seed_rating(fid, self.CURRENT, "standard", 2310)
        db.session.commit()

    def test_chunked_prune_reports_progress(self, app):
        self._seed_scale()
        seen = []
        deleted = FideRatingRepository.prune_history(
            self.CURRENT, "standard", ["IRI"], chunk_size=150,
            progress_cb=seen.append)
        assert deleted == self.N_GER * len(self.OLD_PERIODS)
        assert len(seen) > 1  # several chunks, not one big statement
        assert seen == sorted(seen)  # cumulative and monotonic
        assert seen[-1] == deleted  # final callback reports the total

    def test_chunked_prune_keeps_protected_rows(self, app):
        self._seed_scale()
        FideRatingRepository.prune_history(
            self.CURRENT, "standard", ["IRI"], chunk_size=150)
        db.session.commit()
        # GER old standard rows are gone...
        assert db.session.query(FideRatingModel).filter(
            FideRatingModel.period.in_(self.OLD_PERIODS),
            FideRatingModel.rating_type == "standard",
            FideRatingModel.fide_id.like("9900%")).count() == 0
        # ...IRI history survives...
        assert db.session.query(FideRatingModel).filter(
            FideRatingModel.period.in_(self.OLD_PERIODS),
            FideRatingModel.rating_type == "standard",
            FideRatingModel.fide_id.like("1250%")).count() == (
                self.N_IRI * len(self.OLD_PERIODS))
        # ...current ratings survive for every federation...
        assert db.session.query(FideRatingModel).filter(
            FideRatingModel.period == self.CURRENT).count() == (
                self.N_GER + self.N_IRI)
        # ...and other rating types are untouched.
        assert db.session.query(FideRatingModel).filter(
            FideRatingModel.rating_type == "rapid").count() == 10

    def test_prune_is_idempotent(self, app):
        self._seed_scale()
        first = FideRatingRepository.prune_history(
            self.CURRENT, "standard", ["IRI"], chunk_size=150)
        assert first == self.N_GER * len(self.OLD_PERIODS)
        db.session.commit()
        second_seen = []
        second = FideRatingRepository.prune_history(
            self.CURRENT, "standard", ["IRI"], chunk_size=150,
            progress_cb=second_seen.append)
        assert second == 0
        assert second_seen == []  # nothing left: no chunk, no callback
        db.session.commit()

    def test_prune_empty_keep_list(self, app):
        self._seed_scale()
        before = FideRatingRepository.count()
        assert FideRatingRepository.prune_history(
            self.CURRENT, "standard", [], chunk_size=150) == 0
        assert FideRatingRepository.count() == before


class TestPeriodTransition:
    def test_values_follow_latest_period(self, app, fixture_path):
        cfg = make_cfg(app, fixture_path)
        run_period(app, cfg, PREV)
        xml = _modified_standard(fixture_path, b"<rating>2612</rating>",
                                 b"<rating>2680</rating>")
        _stage(cfg, PERIOD, "standard", xml)
        import_service.ensure_period_jobs(PERIOD)
        assert import_service.tick(PERIOD, cfg)["status"] == "success"
        row = FideRatingRepository.get("12512294", PERIOD, "standard")
        assert row.rating == 2680
        old = FideRatingRepository.get("12512294", PREV, "standard")
        assert old.rating == 2612

    def test_failed_new_period_leaves_old_usable(
            self, app, fixture_path):
        from application import player_service
        cfg = make_cfg(app, fixture_path)
        run_period(app, cfg, PREV)
        import_service.ensure_period_jobs(PERIOD)
        _stage(cfg, PERIOD, "standard", b"<playerslist><broken")
        assert import_service.tick(PERIOD, cfg)["status"] == "error"
        latest = player_service.get_latest_ratings("12512294")
        assert latest["data"]["ratings"]["standard"]["rating"] == 2612
        assert latest["data"]["ratings"]["standard"]["period"] == PREV
        assert player_service.get_dataset()["period"] == PREV


class TestApiConsistency:
    def test_api_matches_database(self, client, seeded, auth_headers):
        from domain.titles import canonical_title
        player = FidePlayerRepository.get_by_fide_id("12512294")
        body = client.get("/api/v1/players/12512294",
                          headers=auth_headers).get_json()
        data = body["data"]
        assert (data["fide_id"], data["name"], data["federation"],
                data["sex"], data["title"], data["birth_year"],
                data["inactive"]) == (
                    player.fide_id, player.name, player.federation,
                    player.sex, player.title, player.birth_year,
                    player.inactive)
        assert data["canonical_title"] == canonical_title(
            player.title, player.wtitle, player.otitle, player.foatitle)
        assert data["latest"]["standard"]["rating"] == 2612
        assert body["dataset"]["source"] == "fide-official-xml"
        assert body["dataset"]["period"] == "2026-09"


class TestBackupRestore:
    def test_sqlite_backup_restore_cycle(
            self, app, fixture_path, tmp_path):
        import sqlite3
        from config import Settings
        from interfaces.factory import create_app
        from tests.conftest import make_settings
        cfg = make_cfg(app, fixture_path)
        run_period(app, cfg, PERIOD)
        before = _snapshot()
        src = app.config["SQLALCHEMY_DATABASE_URI"].replace("sqlite:///", "")
        dst = str(tmp_path / "restored.db")
        db.session.remove()
        source = sqlite3.connect(src)
        target = sqlite3.connect(dst)
        with target:
            source.backup(target)
        target.close()
        source.close()
        app2 = create_app(make_settings(
            tmp_path, database_url="sqlite:///" + dst))
        with app2.app_context():
            assert FidePlayerRepository.count() == 4
            assert FideRatingRepository.count() == 9
            assert checks.verify_data(PERIOD)["ok"] is True
            assert _snapshot() == before
