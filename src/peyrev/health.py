"""Sağlık ucu: dağıtım sonrası denetim ve izleme bunu çağırır."""

from __future__ import annotations

from flask import Blueprint

from . import __version__
from .common import ok


def create_blueprint(core) -> Blueprint:
    blueprint = Blueprint("saglik", __name__)

    @blueprint.get("/api/saglik")
    def health():
        with core.db.transaction() as cur:
            cur.execute("SELECT 1 AS ok")
            cur.fetchone()
        return ok(durum="ok", surum=__version__)

    return blueprint
