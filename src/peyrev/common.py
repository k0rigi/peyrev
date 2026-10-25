"""Bütün uçların ortak kuralları: hata biçimi, sıkı JSON okuma, istemci adresi, güvenlik başlıkları.

Hata gövdesi her zaman {"success": false, "kod": "...", "message": "..."} biçimindedir; mesajlar Türkçedir ve
gizli veri (SQL, istisna metni, anahtar) içermez. Beklenmeyen hata 503 SUNUCU döner: Studio 5xx'i
"sunucuya ulaşılamadı" sayar ve bağlantısız süre boyunca çalışmayı sürdürür.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from typing import Any

from flask import Flask, Response, jsonify, request
from werkzeug.exceptions import HTTPException

LOG = logging.getLogger("peyrev")
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


def _bounded(value: Any, depth: int = 0, budget: list[int] | None = None) -> None:
    budget = [10000] if budget is None else budget
    budget[0] -= 1
    if depth > 20 or budget[0] < 0:
        raise ValueError("too deep or too many values")
    if isinstance(value, dict):
        for item in value.values():
            _bounded(item, depth + 1, budget)
    elif isinstance(value, list):
        for item in value:
            _bounded(item, depth + 1, budget)


def parse_json(raw: bytes) -> Any:
    return json.loads(raw.decode("utf-8"), object_pairs_hook=_unique, parse_constant=_no_constant)


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
        # Neither SQL nor exception text reaches the client; the log keeps the type and location only.
        LOG.exception("Beklenmeyen hata: %s %s", request.method, request.path)
        return fail(ApiError("İşlem şu anda tamamlanamadı. Daha sonra tekrar deneyin.", 503, "SUNUCU"))

    @app.after_request
    def headers(response: Response) -> Response:
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("X-Frame-Options", "DENY")
        return response
