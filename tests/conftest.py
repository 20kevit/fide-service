"""Shared pytest fixtures: isolated app + temp SQLite + temp data dir."""
import os

import pytest

from config import Settings
from infrastructure import migrations
from infrastructure.database import db
from infrastructure.models import FidePlayerModel, FideRatingModel
from interfaces.factory import create_app

READ_KEY = "test-read-key-abc123"
ADMIN_KEY = "test-admin-key-xyz789"


def make_settings(tmp_path, **overrides):
    data_dir = tmp_path / "fide-data"
    data_dir.mkdir(exist_ok=True)
    args = dict(
        database_url="sqlite:///%s" % (tmp_path / "test.db"),
        api_key=READ_KEY,
        admin_key=ADMIN_KEY,
        secret_key="test-secret",
        data_directory=str(data_dir),
        rate_limit_per_minute=0,  # disabled unless a test opts in
        auth_fail_limit_per_hour=0,  # disabled unless a test opts in
        history_federations=["IRI"],
    )
    args.update(overrides)
    return Settings(**args)


@pytest.fixture()
def app(tmp_path):
    application = create_app(make_settings(tmp_path))
    application.config["TESTING"] = True
    with application.app_context():
        migrations.upgrade(db.engine)
        yield application
        db.session.remove()


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def auth_headers():
    return {"X-API-Key": READ_KEY}


@pytest.fixture()
def admin_headers():
    return {"X-Admin-Key": ADMIN_KEY}


@pytest.fixture()
def fixture_path():
    base = os.path.join(os.path.dirname(__file__), "fixtures")
    return lambda name: os.path.join(base, name)


def seed_player(fide_id="12512294", name="Alpha, Test One", federation="IRI",
                sex="M", title="GM", wtitle="", otitle="", foatitle="",
                birth_year="1999", inactive=False):
    player = FidePlayerModel(
        fide_id=fide_id, name=name, federation=federation, sex=sex,
        title=title, wtitle=wtitle, otitle=otitle, foatitle=foatitle,
        birth_year=birth_year, inactive=inactive)
    db.session.add(player)
    db.session.flush()
    return player


def seed_rating(fide_id, period, rating_type, rating, games=9, k_factor=20):
    row = FideRatingModel(
        fide_id=fide_id, period=period, rating_type=rating_type,
        rating=rating, games=games, k_factor=k_factor)
    db.session.add(row)
    db.session.flush()
    return row


@pytest.fixture()
def seeded(app):
    """Two periods of IRI+GER data for read-path tests."""
    seed_player()
    seed_player("12512295", name="Beta, Test Two", federation="IRI",
                sex="F", title="", wtitle="WIM", birth_year="2005")
    seed_player("99900001", name="Gamma, Foreign Player", federation="GER",
                title="IM", birth_year="1990")
    seed_rating("12512294", "2026-08", "standard", 2601, k_factor=10)
    seed_rating("12512294", "2026-09", "standard", 2612, k_factor=10)
    seed_rating("12512294", "2026-09", "rapid", 2550)
    seed_rating("12512294", "2026-09", "blitz", 2501)
    seed_rating("12512295", "2026-09", "standard", 2200)
    seed_rating("99900001", "2026-08", "standard", 2440)
    seed_rating("99900001", "2026-09", "standard", 2450)
    db.session.commit()
    return True
