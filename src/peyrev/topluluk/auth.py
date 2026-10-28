"""Topluluk üye oturumu: Peyrev hesabıyla giriş, yalnız aboneliği devam eden hesaplar.

Giriş (POST /api/topluluk/giris {kullanici, sifre}): ad kuralı, şifre denetimi ve hatalı deneme sınırı Studio girişiyle
aynıdır (users.find_by_login, rules.login_attempts_left: 15 dakikada hesap başına 10, adres başına 30). Abonelik Studio'nun
lisans kuralıyla denetlenir (licensing.license_refusal: MOD_RPA hakkı ve geçmemiş bitiş, yönetici satırsız açar); hakkı
olmayan hesap 403 ABONELIK_YOK alır.

Oturum: rastgele belirteç yalnız `peyrev_uye` çerezinde durur (HttpOnly, Secure, SameSite=Strict, Path=/api/topluluk);
uye_oturumlari belirtecin SHA-256 özetini saklar. Oturum 14 gün sonra ya da 3 gün istek gelmezse biter; hesap başına en
çok 5 oturum açık kalır. Her istekte hesap yeniden okunur: silinen, pasifleşen ya da şifresi değişen hesabın oturumu
silinir (401); aboneliği biten hesap 403 ABONELIK_YOK alır, oturumu kalır (uzatılınca yeniden açılır).

CSRF: durum değiştiren her istek `X-Peyrev-Topluluk: 1` başlığını ve sitenin kökünden Origin'i (yoksa Referer'i) taşır
(panelle aynı kural, admin.auth.same_origin); aksi 403 CSRF.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import uuid
from datetime import timedelta
from typing import Any

from flask import Response, request
from psycopg import Cursor

from .. import users
from ..accounts import rules
from ..accounts.licensing import license_refusal
from ..common import ApiError

COOKIE = "peyrev_uye"
PATH = "/api/topluluk"
HEADER = "X-Peyrev-Topluluk"
ABSOLUTE = timedelta(days=14)
IDLE = timedelta(days=3)
SESSIONS_PER_ACCOUNT = 5
TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

WRONG_LOGIN = "Kullanıcı adı veya şifre hatalı."
NO_SUBSCRIPTION = ("Topluluk, aboneliği devam eden Peyrev hesaplarına açıktır. Lisansınızı uzatmak için "
                   "peyrev.com/#basvuru formunu kullanın.")
SESSION_REQUIRED = "Topluluk için Peyrev hesabınızla giriş yapın."
SESSION_CLOSED = "Oturumunuz kapandı. Yeniden giriş yapın."


def digest(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def check_request() -> None:
    from ..admin.auth import same_origin  # imported here: the admin package imports this one for moderation
    from ..core import current_core

    if request.method in SAFE_METHODS:
        return
    if request.headers.get(HEADER) != "1" or not same_origin(current_core()):
        raise ApiError("İstek doğrulanamadı. Sayfayı yenileyip tekrar deneyin.", 403, "CSRF")


def set_cookie(response: Response, token: str) -> None:
    response.set_cookie(COOKIE, token, max_age=int(ABSOLUTE.total_seconds()), path=PATH, secure=True, httponly=True,
                        samesite="Strict")


def forget_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE, path=PATH, secure=True, httponly=True, samesite="Strict")


def display_name(user: dict[str, Any]) -> str:
    """Toplulukta görünen ad: ad soyad; yoksa giriş adının @ öncesi. E-posta adresi başka üyelere gösterilmez."""
    return (user["ad_soyad"] or "").strip() or user["giris"].split("@", 1)[0]


def profile(user: dict[str, Any]) -> dict[str, Any]:
    return {"ad": display_name(user), "yonetici": user["rol"] == "yonetici"}


def subscribed(core, cur: Cursor, user: dict[str, Any]) -> bool:
    return user["durum"] == "aktif" and license_refusal(cur, user, core.clock.today_tr())[0] is None


def sign_in(core, cur: Cursor, login: str, password: str, address: str, agent: str | None) -> Any:
    """(belirteç, hesap) ya da kaydedilip sonra fırlatılacak ret."""
    moment = core.clock.now()
    candidates = users.find_by_login(cur, login)
    rules.login_attempts_left(cur, login, address, moment, candidates)
    matched = [user for user in candidates if rules.matches(user["sifre_hash"], password)]
    if len(matched) > 1:
        raise ApiError(rules.AMBIGUOUS, 409, "BELIRSIZ")
    if not matched:
        rules.equalize([user["sifre_hash"] for user in candidates], password)
        rules.login_failed(cur, rules.failed_name(login, candidates), address, moment)
        return ApiError(WRONG_LOGIN, 401, "HATALI_GIRIS")
    user = matched[0]  # find_by_login returns active accounts only: a passive one is a wrong login, as in Studio
    if not subscribed(core, cur, user):
        return ApiError(NO_SUBSCRIPTION, 403, "ABONELIK_YOK")
    cur.execute("DELETE FROM uye_oturumlari WHERE bitis <= %s OR son_gorulme <= %s", (moment, moment - IDLE))
    token = secrets.token_urlsafe(32)
    cur.execute("INSERT INTO uye_oturumlari (id, kullanici_id, belirtec_ozeti, sifre_izi, ip, tarayici, olusturma, "
                "son_gorulme, bitis) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (uuid.uuid4().hex, user["id"], digest(token), users.fingerprint(user["sifre_hash"]), address[:64],
                 (agent or "")[:200], moment, moment, moment + ABSOLUTE))
    session_id = cur.fetchone()["id"]
    cur.execute("DELETE FROM uye_oturumlari WHERE kullanici_id = %s AND id <> %s AND id NOT IN ("
                "SELECT id FROM uye_oturumlari WHERE kullanici_id = %s AND id <> %s ORDER BY son_gorulme DESC, id "
                "LIMIT %s)", (user["id"], session_id, user["id"], session_id, SESSIONS_PER_ACCOUNT - 1))
    return token, user


def verify(core, cur: Cursor, token: Any) -> dict[str, Any] | ApiError:
    """Oturumun hesabı; geçersiz oturum silinir ve ret döner (silme kaydedildikten sonra fırlatılır)."""
    if not isinstance(token, str) or not TOKEN.fullmatch(token):
        raise ApiError(SESSION_REQUIRED, 401, "OTURUM_GEREKLI")
    cur.execute("SELECT * FROM uye_oturumlari WHERE belirtec_ozeti = %s", (digest(token),))
    session = cur.fetchone()
    if session is None:
        raise ApiError(SESSION_CLOSED, 401, "OTURUM_GECERSIZ")
    moment = core.clock.now()
    user = users.get(cur, session["kullanici_id"])
    if (moment >= session["bitis"] or moment - session["son_gorulme"] >= IDLE or user is None
            or user["durum"] != "aktif"
            or not hmac.compare_digest(users.fingerprint(user["sifre_hash"]), session["sifre_izi"])):
        cur.execute("DELETE FROM uye_oturumlari WHERE id = %s", (session["id"],))
        return ApiError(SESSION_CLOSED, 401, "OTURUM_GECERSIZ")
    cur.execute("UPDATE uye_oturumlari SET son_gorulme = %s WHERE id = %s", (moment, session["id"]))
    if not subscribed(core, cur, user):
        raise ApiError(NO_SUBSCRIPTION, 403, "ABONELIK_YOK")
    return user


def sign_out(cur: Cursor, token: Any) -> None:
    if isinstance(token, str) and TOKEN.fullmatch(token):
        cur.execute("DELETE FROM uye_oturumlari WHERE belirtec_ozeti = %s", (digest(token),))
