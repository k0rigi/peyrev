"""Bütün uçların ortak kuralları: hata biçimi, sıkı JSON okuma, istemci adresi, güvenlik başlıkları, kanonik adres.

Hata gövdesi her zaman {"success": false, "kod": "...", "message": "..."} biçimindedir; mesajlar Türkçedir ve
gizli veri (SQL, istisna metni, anahtar) içermez. Beklenmeyen hata 503 SUNUCU döner: Studio 5xx'i
"sunucuya ulaşılamadı" sayar ve bağlantısız süre boyunca çalışmayı sürdürür.
"""

from __future__ import annotations

import json
import logging
import math
import traceback
from collections.abc import Iterable
from typing import Any
from urllib.parse import urlsplit

from flask import Flask, Response, current_app, jsonify, redirect, request
from werkzeug.exceptions import HTTPException

LOG = logging.getLogger("peyrev")
# Tarayıcı peyrev.com'u bu süre boyunca yalnız https ile açar. İlk aşamada kısa tutulur; sorunsuz geçtiği görülünce
# uzatılabilir (bir yıl yaygındır).
HSTS_SECONDS = 7 * 24 * 3600
BODY_LIMIT = 8 * 1024
INVALID_FIELDS = "İstek alanları geçersiz."


class ApiError(Exception):
    """İstemciye gösterilebilen hata. `extra` alanları gövdeye eklenir (ör. bitis, neden)."""

    def __init__(self, message: str, status: int = 400, kod: str = "GECERSIZ", **extra: Any):
        super().__init__(message)
        self.status = status
        self.kod = kod
        self.extra = extra


def ok(status: int = 200, **payload: Any) -> tuple[Response, int]:
    return jsonify(success=True, **payload), status


def fail(error: ApiError) -> tuple[Response, int]:
    return jsonify(success=False, kod=error.kod, message=str(error), **error.extra), error.status


def _unique(pairs: list[tuple[str, Any]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _no_constant(_value: str) -> Any:
    raise ValueError("NaN/Infinity")


def _finite(text: str) -> float:
    value = float(text)
    if math.isinf(value):  # 1e999 parses to infinity; databases and digests refuse it later
        raise ValueError("number out of range")
    return value


def _text(value: str) -> str:
    value.encode("utf-8")  # a lone surrogate ("\\ud800") would fail later in the database or a digest
    return value


def _bounded(value: Any, depth: int = 0, budget: list[int] | None = None) -> None:
    budget = [10000] if budget is None else budget
    budget[0] -= 1
    if depth > 20 or budget[0] < 0:
        raise ValueError("too deep or too many values")
    if isinstance(value, dict):
        for key, item in value.items():
            _text(key)
            _bounded(item, depth + 1, budget)
    elif isinstance(value, list):
        for item in value:
            _bounded(item, depth + 1, budget)
    elif isinstance(value, str):
        _text(value)


def parse_json(raw: bytes) -> Any:
    return json.loads(raw.decode("utf-8"), object_pairs_hook=_unique, parse_constant=_no_constant,
                      parse_float=_finite)


def json_body(allowed: Iterable[str], required: Iterable[str] = (), *, limit: int = BODY_LIMIT,
              message: str = INVALID_FIELDS) -> dict:
    """Gövdeyi sıkı okur: boyut sınırı, yinelenen anahtar ve NaN yok, bilinmeyen/eksik alan yok, en çok 20 düzey."""
    if request.content_length is not None and request.content_length > limit:
        raise ApiError("İstek çok büyük.", 413, "GOVDE_SINIRI")
    raw = request.get_data(cache=False)
    if len(raw) > limit:
        raise ApiError("İstek çok büyük.", 413, "GOVDE_SINIRI")
    try:
        body = parse_json(raw)
        if not isinstance(body, dict):
            raise TypeError("not an object")
        _bounded(body)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ApiError(message) from None
    allowed, required = set(allowed), set(required)
    if set(body) - allowed or required - set(body):
        raise ApiError(message)
    return body


def client_ip() -> str:
    """OpenLiteSpeed istemci adresini X-Forwarded-For'un SONUNA ekler; yalnız o öğeye güvenilir."""
    forwarded = (request.headers.get("X-Forwarded-For") or "").split(",")[-1].strip()
    return (forwarded or request.remote_addr or "")[:64]


def register(app: Flask) -> None:
    @app.errorhandler(ApiError)
    def api_error(error: ApiError):
        return fail(error)

    @app.errorhandler(HTTPException)
    def http_error(error: HTTPException):
        if request.path.startswith("/api/"):
            kod = {404: "BULUNAMADI", 405: "YONTEM", 413: "GOVDE_SINIRI"}.get(error.code or 0, "GECERSIZ")
            message = {404: "İstenen kayıt veya uç bulunamadı.", 405: "Bu yöntem bu uçta kullanılamaz.",
                       413: "İstek çok büyük."}.get(error.code or 0, "İstek işlenemedi.")
            return fail(ApiError(message, error.code or 400, kod))
        return error

    @app.errorhandler(Exception)
    def unexpected(error: Exception):
        # Neither SQL nor exception text reaches the client or the log (it may carry query parts and user
        # values); the log keeps the type and the innermost location only.
        frame = traceback.extract_tb(error.__traceback__)[-1] if error.__traceback__ else None
        where = f"{frame.filename.rsplit('/', 1)[-1]}:{frame.lineno}" if frame else "?"
        LOG.error("Beklenmeyen hata: %s %s (%s, %s)", request.method, request.path, type(error).__name__, where)
        return fail(ApiError("İşlem şu anda tamamlanamadı. Daha sonra tekrar deneyin.", 503, "SUNUCU"))

    @app.before_request
    def canonical_address():
        """www.peyrev.com ve http istekleri https://peyrev.com'a yönlenir (308: yöntem ve gövde korunur). Statik site
        bunu web/.htaccess ile yapar; OpenLiteSpeed'in vekil yolları (/api/, /yonetim/) .htaccess'e uğramaz. Şema,
        vekilin X-Forwarded-Proto başlığından okunur; sunucunun kendi denetimleri (127.0.0.1) yönlenmez."""
        base = urlsplit(current_app.extensions["peyrev"].settings.public_base)
        host = (request.host or "").lower()
        host = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
        insecure = request.headers.get("X-Forwarded-Proto", "").strip().lower() == "http"
        if host == "www." + (base.hostname or "") or (host == base.hostname and insecure):
            query = request.query_string.decode("latin-1")
            return redirect(f"{base.scheme}://{base.netloc}{request.path}" + (f"?{query}" if query else ""), 308)
        return None

    @app.after_request
    def headers(response: Response) -> Response:
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        response.headers.setdefault("Strict-Transport-Security", f"max-age={HSTS_SECONDS}")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("X-Frame-Options", "DENY")
        return response
