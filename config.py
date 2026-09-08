"""Independent FIDE Service configuration. No Swiss imports.

All secrets come from the environment — none have committed defaults.
Sensible non-secret defaults are provided only where safe.
"""
import os
from dataclasses import dataclass, field
from typing import List


def _get_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _get_history_federations() -> List[str]:
    raw = os.environ.get("HISTORY_FEDERATIONS", "IRI")
    if raw is None:
        return []
    return [c.strip().upper() for c in raw.split(",") if c.strip()]


@dataclass
class Settings:
    env: str = "development"
    secret_key: str = ""
    database_url: str = "sqlite:///fide.db"
    api_key: str = ""
    admin_key: str = ""
    host: str = "127.0.0.1"
    port: int = 5001
    data_directory: str = "./var/fide-data"
    import_timeout_seconds: int = 600
    import_batch_size: int = 500
    history_federations: List[str] = field(default_factory=lambda: ["IRI"])
    rate_limit_per_minute: int = 600
    auth_fail_limit_per_hour: int = 120
    upload_max_bytes: int = 120 * 1024 * 1024
    download_base_url: str = "https://ratings.fide.com/download/"
    connect_timeout: int = 10
    read_timeout: int = 30
    max_retries: int = 3
    raw_retention_days: int = 90

    @property
    def is_production(self) -> bool:
        return self.env == "production"

    @property
    def effective_admin_key(self) -> str:
        """Admin credential: dedicated key when set, else the read key."""
        return self.admin_key or self.api_key


def load_settings() -> Settings:
    """Build Settings from the environment (reads `.env` when present)."""
    try:
        from dotenv import load_dotenv  # optional dependency
        load_dotenv()
    except ImportError:
        pass
    env = os.environ.get("FLASK_ENV", "development")
    secret_key = os.environ.get("SECRET_KEY", "")
    database_url = os.environ.get("DATABASE_URL", "sqlite:///fide.db")
    if env == "production":
        if not secret_key:
            raise RuntimeError("SECRET_KEY is required in production.")
        if not os.environ.get("DATABASE_URL"):
            raise RuntimeError("DATABASE_URL is required in production.")
    return Settings(
        env=env,
        secret_key=secret_key,
        database_url=database_url,
        api_key=os.environ.get("FIDE_API_KEY", ""),
        admin_key=os.environ.get("FIDE_ADMIN_KEY", ""),
        host=os.environ.get("FIDE_SERVICE_HOST", "127.0.0.1"),
        port=_get_int("FIDE_SERVICE_PORT", 5001),
        data_directory=os.environ.get("FIDE_DATA_DIRECTORY", "./var/fide-data"),
        import_timeout_seconds=_get_int("IMPORT_TIMEOUT_SECONDS", 600),
        import_batch_size=_get_int("IMPORT_BATCH_SIZE", 500),
        history_federations=_get_history_federations(),
        rate_limit_per_minute=_get_int("RATE_LIMIT_PER_MINUTE", 600),
        auth_fail_limit_per_hour=_get_int("AUTH_FAIL_LIMIT_PER_HOUR", 120),
        upload_max_bytes=_get_int("FIDE_UPLOAD_MAX_BYTES", 120 * 1024 * 1024),
        download_base_url=os.environ.get(
            "FIDE_DOWNLOAD_BASE_URL", "https://ratings.fide.com/download/"),
        connect_timeout=_get_int("FIDE_CONNECT_TIMEOUT", 10),
        read_timeout=_get_int("FIDE_READ_TIMEOUT", 30),
        max_retries=_get_int("FIDE_MAX_RETRIES", 3),
    )
