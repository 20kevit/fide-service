"""Flask application factory. Own app, own config, own extensions."""
import logging

from flask import Flask

from config import Settings
from infrastructure.database import db
from interfaces.api import admin, players, status
from interfaces.api.errors import register_error_handlers

log = logging.getLogger(__name__)


def create_app(settings: Settings = None) -> Flask:
    """Build the independent FIDE Service Flask application."""
    from config import load_settings
    settings = settings or load_settings()

    app = Flask(__name__)
    app.config["SECRET_KEY"] = settings.secret_key or "dev-only-insecure"
    app.config["SQLALCHEMY_DATABASE_URI"] = settings.database_url
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"pool_pre_ping": True}
    app.config["MAX_CONTENT_LENGTH"] = max(
        8 * 1024 * 1024, settings.upload_max_bytes)
    app.config["FIDE_SETTINGS"] = settings

    db.init_app(app)

    app.register_blueprint(players.bp, url_prefix="/api/v1")
    app.register_blueprint(status.bp, url_prefix="/api/v1")
    app.register_blueprint(admin.bp, url_prefix="/api/v1")
    register_error_handlers(app)

    from interfaces import cli as _cli
    _cli.register(app)

    @app.get("/")
    def index():
        return {"service": "fide-service", "api": "/api/v1",
                "health": "/api/v1/healthz"}

    return app
