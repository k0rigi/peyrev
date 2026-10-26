"""Panel sayfası: GET /yonetim/ ve yanındaki iki dosya (static/; derleme adımı yok).

OpenLiteSpeed /yonetim/ isteklerini Gunicorn'a iletir; sayfa statik siteden değil buradan, güvenlik başlıklarıyla
gelir. Sayfada satır içi betik ve stil yoktur; veriler yalnız /api/yonetim uçlarından gelir ve DOM'a textContent ile
yazılır. Peyrev işareti statik sitenin /assets/ klasöründen (aynı köken) yüklenir.
"""

from __future__ import annotations

from pathlib import Path

from flask import Blueprint, Response, abort, redirect, send_file

STATIC = Path(__file__).with_name("static")
FILES = {"panel.css": "text/css", "panel.js": "text/javascript"}
POLICY = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; "
          "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")


def page_blueprint() -> Blueprint:
    blueprint = Blueprint("yonetim_sayfasi", __name__)

    @blueprint.get("/yonetim")
    def bare():
        return redirect("/yonetim/", 308)  # relative: behind the proxy the request reaches us over plain http

    @blueprint.get("/yonetim/")
    def panel():
        response = send_file(STATIC / "index.html", mimetype="text/html", conditional=False, etag=False)
        response.headers["Cache-Control"] = "no-store"
        return response

    @blueprint.get("/yonetim/<name>")
    def asset(name: str):
        if name not in FILES:
            abort(404)
        response = send_file(STATIC / name, mimetype=FILES[name], max_age=0)
        response.headers["Cache-Control"] = "no-cache"  # every deploy is picked up; ETag saves the transfer
        return response

    @blueprint.after_request
    def headers(response: Response) -> Response:
        response.headers["Content-Security-Policy"] = POLICY
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=()"
        return response

    return blueprint
