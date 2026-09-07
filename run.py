"""Local entry point: python run.py (never imports Swiss code)."""
from config import load_settings
from interfaces.factory import create_app

settings = load_settings()
app = create_app(settings)

if __name__ == "__main__":
    app.run(host=settings.host, port=settings.port, debug=(not settings.is_production))
