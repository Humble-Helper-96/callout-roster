"""Local development server: ``python -m app`` (use Gunicorn via wsgi.py in production)."""
from . import create_app

app = create_app()
cfg = app.config["APP"]
app.run(host=cfg.host, port=cfg.port, debug=False, threaded=True)
