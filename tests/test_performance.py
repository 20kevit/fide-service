"""Performance regression tests (Phase 5): deterministic query shapes.

No timing thresholds (hardware-sensitive). Instead each test asserts the
exact number of SQL statements per endpoint, which fails loudly if an
N+1 query, a duplicated lookup, or an unbounded fetch is introduced.
Measured latencies live in docs/15-performance-capacity.md.
"""
import pytest
from sqlalchemy import event

from infrastructure.database import db


@pytest.fixture()
def query_counter(app):
    """Count SQL statements executed inside the test."""
    counts = {"n": 0}

    def before_cursor_execute(conn, cursor, statement, parameters,
                              context, executemany):
        counts["n"] += 1

    event.listen(db.engine, "before_cursor_execute",
                 before_cursor_execute)
    try:
        yield counts
    finally:
        event.remove(db.engine, "before_cursor_execute",
                     before_cursor_execute)


class TestQueryShapes:
    def test_healthz_zero_queries(self, client, query_counter):
        assert client.get("/api/v1/healthz").status_code == 200
        assert query_counter["n"] == 0

    def test_lookup_four_queries(self, client, seeded, auth_headers,
                                 query_counter):
        resp = client.get("/api/v1/players/12512294", headers=auth_headers)
        assert resp.status_code == 200
        # player + latest-per-type + dataset(2). Was 6 before the
        # single-dataset fix — any increase is a regression.
        assert query_counter["n"] == 4

    def test_lookup_missing_one_query(self, client, seeded, auth_headers,
                                      query_counter):
        resp = client.get("/api/v1/players/00000000", headers=auth_headers)
        assert resp.status_code == 404
        assert query_counter["n"] == 1

    def test_search_bounded_queries(self, client, seeded, auth_headers,
                                   query_counter):
        resp = client.get("/api/v1/players/search",
                          query_string={"q": "Test"}, headers=auth_headers)
        assert resp.status_code == 200
        # search + bulk-latest + dataset(2); independent of result size.
        assert query_counter["n"] == 4

    def test_ratings_four_queries(self, client, seeded, auth_headers,
                                  query_counter):
        resp = client.get("/api/v1/players/12512294/ratings",
                          headers=auth_headers)
        assert resp.status_code == 200
        assert query_counter["n"] == 4

    def test_history_four_queries(self, client, seeded, auth_headers,
                                  query_counter):
        resp = client.get("/api/v1/players/12512294/ratings/history",
                          headers=auth_headers)
        assert resp.status_code == 200
        assert query_counter["n"] == 4

    def test_batch_no_n_plus_one(self, client, seeded, auth_headers,
                                 query_counter):
        # 50 IDs must cost the same 4 queries as 3 IDs (IN + GROUP BY).
        ids = (["12512294", "12512295", "99900001"] * 17)[:50]
        resp = client.post("/api/v1/players/batch", json={"ids": ids},
                           headers=auth_headers)
        assert resp.status_code == 200
        assert query_counter["n"] == 4

    def test_status_eight_queries(self, client, seeded, auth_headers,
                                  query_counter):
        resp = client.get("/api/v1/status", headers=auth_headers)
        assert resp.status_code == 200
        assert query_counter["n"] == 8

    def test_admin_jobs_one_query(self, client, admin_headers,
                                  query_counter):
        resp = client.get("/api/v1/admin/jobs", headers=admin_headers)
        assert resp.status_code == 200
        assert query_counter["n"] == 1
