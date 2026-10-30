"""Studio erişim belirteçleri (destek, ajan, robot): lisans yanıtıyla verilir, 12 dakika geçerlidir.

İçerik: aud, surum=1, kullanici_id, firma_id (= alan_id, Studio uyumu), cihaz, oturum, sifre_izi, iat, exp.
Her istekte yeniden denetlenir: hesap aktif, şifre izi aynı, Studio oturumu (oturumlar) hâlâ aktif. Anahtar amaca özeldir
(HMAC(jwt_secret, aud)); bir amacın belirteci diğerinde geçmez.
"""

from __future__ import annotations

import hmac
import re
from datetime import datetime, timedelta
from typing import Any

import jwt
from psycopg import Cursor

from .common import ApiError
from .users import fingerprint

TOKEN_SECONDS = 720
AUDIENCES = {"destek": "peyrev-studio-destek", "ajan": "peyrev-studio-ajanlar", "robot": "peyrev-studio-robot"}
IDENTIFIER = re.compile(r"^[0-9a-f]{32,64}$")
REQUIRED = ["exp", "iat", "aud", "kullanici_id", "firma_id", "cihaz", "oturum", "sifre_izi"]


def issue(core, purpose: str, user: dict[str, Any], device: str, session_id: str,
          expires: datetime | None = None) -> str:
    audience = AUDIENCES[purpose]
    moment = core.clock.now()
    limit = moment + timedelta(seconds=TOKEN_SECONDS)
    payload = {"aud": audience, "surum": 1, "kullanici_id": int(user["id"]), "firma_id": int(user["alan_id"]),
               "cihaz": device, "oturum": session_id, "sifre_izi": fingerprint(user["sifre_hash"]),
               "iat": int(moment.timestamp()), "exp": int(min(limit, expires or limit).timestamp())}
    return jwt.encode(payload, core.keys.derive(audience), algorithm="HS256")


def verify(core, cur: Cursor, purpose: str, authorization: str | None) -> tuple[dict[str, Any], dict[str, Any]]:
    """(kullanıcı satırı, belirteç iddiaları); geçersizse ApiError 401."""
    if not isinstance(authorization, str) or len(authorization) > 8192 or not authorization.startswith("Bearer "):
        raise ApiError("Studio oturumu gerekli.", 401, "OTURUM_GEREKLI")
    audience = AUDIENCES[purpose]
    try:
        claims = _decode(core, authorization[7:], audience)
        if claims.get("surum") != 1 or type(claims["kullanici_id"]) is not int or type(claims["firma_id"]) is not int:
            raise ValueError("claims")
        for name in ("cihaz", "oturum"):
            if not isinstance(claims[name], str) or not IDENTIFIER.match(claims[name]):
                raise ValueError("identity")
    except (jwt.PyJWTError, ValueError, TypeError, KeyError):
        raise ApiError("Studio oturumu geçersiz veya süresi dolmuş.", 401, "OTURUM_GECERSIZ") from None
    cur.execute("SELECT * FROM kullanicilar WHERE id = %s AND durum = 'aktif'", (claims["kullanici_id"],))
    user = cur.fetchone()
    if (user is None or int(user["alan_id"]) != claims["firma_id"] or not isinstance(claims["sifre_izi"], str)
            or not hmac.compare_digest(fingerprint(user["sifre_hash"]), claims["sifre_izi"])):
        raise ApiError("Studio oturumu kapatılmış.", 401, "OTURUM_GECERSIZ")
    cur.execute("SELECT id FROM oturumlar WHERE id = %s AND kullanici_id = %s AND cihaz = %s AND durum = 'aktif'",
                (claims["oturum"], user["id"], claims["cihaz"]))
    if cur.fetchone() is None:
        raise ApiError("Studio oturumu kapatılmış.", 401, "OTURUM_GECERSIZ")
    return user, claims


def _decode(core, token: str, audience: str) -> dict[str, Any]:
    """İmza ve hedef PyJWT ile; süre çekirdeğin saatine göre (testlerde sabit saat kullanılabilir)."""
    claims = jwt.decode(token, core.keys.derive(audience), algorithms=["HS256"], audience=audience,
                        options={"require": REQUIRED, "verify_exp": False, "verify_iat": False})
    if not isinstance(claims.get("exp"), int) or claims["exp"] < core.clock.now().timestamp():
        raise jwt.ExpiredSignatureError("expired")
    return claims
