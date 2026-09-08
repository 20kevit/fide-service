"""SQLAlchemy handle for the FIDE Service. Own database, own models."""
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import event

db = SQLAlchemy()


def enable_sqlite_foreign_keys(engine) -> None:
    """Enforce declared FOREIGN KEYs on SQLite connections.

    MySQL/InnoDB enforces the model's ``ForeignKey`` constraints natively;
    SQLite ignores them unless ``PRAGMA foreign_keys=ON`` is set per
    connection. Without this, the test/dev database silently accepts
    orphan ratings that production would reject — a test-fidelity gap the
    suite used to document instead of close. Registered per engine (pool
    connections inherit it for their lifetime); other dialects untouched.
    """

    def _on_connect(dbapi_connection, _connection_record):
        try:
            import sqlite3
            if isinstance(dbapi_connection, sqlite3.Connection):
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.close()
        except Exception:
            pass  # never break engine startup for a best-effort pragma

    event.listen(engine, "connect", _on_connect)
