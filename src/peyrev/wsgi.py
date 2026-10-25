"""Gunicorn giriş noktası: `gunicorn peyrev.wsgi:app`."""

from .app import create_app

app = create_app()
