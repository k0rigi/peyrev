"""Üye oturumu: Peyrev hesabıyla siteye giriş (Hesabım ve Topluluk) ve /api/uye/* uçları (Hesabım).

Giriş (POST /api/uye/giris {kullanici, sifre}): ad kuralı, şifre denetimi ve hatalı deneme sınırı Studio girişiyle
aynıdır (users.find_by_login yalnız etkin hesapları bulur; rules.login_attempts_left: 15 dakikada hesap başına 10,
adres başına 30). Lisansı bitmiş hesap da girer: Hesabım'dan lisansını görür ve uzatma ister. Topluluk ayrıca
aboneliği her istekte denetler (subscribed: Studio'nun lisans kuralı, licensing.license_refusal).

Oturum: rastgele belirteç yalnız `peyrev_hesap` çerezinde durur (HttpOnly, Secure, SameSite=Strict, Path=/api);
uye_oturumlari belirtecin SHA-256 özetini saklar. Oturum 14 gün sonra ya da 3 gün istek gelmezse biter; hesap başına en
çok 5 oturum açık kalır. Her istekte hesap yeniden okunur: silinen, pasifleşen ya da şifresi değişen hesabın oturumu
silinir (401).

CSRF: durum değiştiren her istek `X-Peyrev-Uye: 1` başlığını ve sitenin kökünden Origin'i (yoksa Referer'i) taşır
(panelle aynı kural, admin.auth.same_origin); aksi 403 CSRF.

Hesabım: hesap ve lisans durumu, açık Studio oturumları, kişisel indirme bağlantısı (abonelik sürerken; günde en çok
LINKS_PER_DAY), lisans uzatma isteği (yöneticiye bildirilir, olay kaydına yazılır; günde en çok bir). Şifre, Studio'yla
ortak /api/rpa/hesap/sifre ucuyla değişir.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import uuid
from datetime import timedelta
from typing import Any

from flask import Blueprint, Response, g, request
from psycopg import Cursor
from psycopg.types.json import Jsonb

from . import indirme, users
from .accounts import rules
from .accounts.applications import PACKAGES
from .accounts.licensing import license_refusal
from .common import ApiError, client_ip, json_body, ok
from .support import notify

COOKIE = "peyrev_hesap"
PATH = "/api"
HEADER = "X-Peyrev-Uye"
LINKS_PER_DAY = 5
ABSOLUTE = timedelta(days=14)
IDLE = timedelta(days=3)
SESSIONS_PER_ACCOUNT = 5
TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

WRONG_LOGIN = "Kullanıcı adı veya şifre hatalı."
NO_SUBSCRIPTION = ("Topluluk, aboneliği devam eden Peyrev hesaplarına açıktır. Lisansınızı uzatmak için "
                   "peyrev.com/#basvuru formunu kullanın.")
SESSION_REQUIRED = "Peyrev hesabınızla giriş yapın."
SESSION_CLOSED = "Oturumunuz kapandı. Yeniden giriş yapın."


def digest(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def check_request() -> None:
    from .admin.auth import same_origin  # imported here: the admin package imports topluluk, which imports this
    from .core import current_core

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


def verify(core, cur: Cursor, token: Any, *, subscription: bool = False) -> dict[str, Any] | ApiError:
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
    if subscription and not subscribed(core, cur, user):
        raise ApiError(NO_SUBSCRIPTION, 403, "ABONELIK_YOK")
    return user


def sign_out(cur: Cursor, token: Any) -> None:
    if isinstance(token, str) and TOKEN.fullmatch(token):
        cur.execute("DELETE FROM uye_oturumlari WHERE belirtec_ozeti = %s", (digest(token),))


def _committed(core, work):
    with core.db.transaction() as cur:
        result = work(cur)
    if isinstance(result, ApiError):
        raise result
    return result


def _day(value) -> str | None:
    return value.isoformat() if value else None


def account(core, cur: Cursor, user: dict[str, Any]) -> dict[str, Any]:
    """Hesabım: hesap, lisans hakları, açık Studio oturumları ve indirme bağlantısının durumu."""
    today = core.clock.today_tr()
    rights = {}
    for module, key in ((users.RPA, "peyrev"), (users.AGENTS, "ajanlar")):
        state, end = users.module_state(cur, user, module, today)
        row = users.modules(cur, user["id"]).get(module)
        rights[key] = {"durum": state, "bitis": _day(end if end else (row or {}).get("bitis")),
                       "suresiz": bool(row) and row["bitis"] is None}
    cur.execute("SELECT cihaz, surum, son_gorulme FROM oturumlar WHERE kullanici_id = %s AND durum = 'aktif' "
                "ORDER BY son_gorulme DESC LIMIT 5", (user["id"],))
    studio = [{"cihaz": row["cihaz"][:8], "surum": row["surum"], "son_gorulme": rules.stamp(row["son_gorulme"])}
              for row in cur.fetchall()]
    return {"giris": user["giris"], "ad": display_name(user), "ad_soyad": user["ad_soyad"], "telefon": user["telefon"],
            "yonetici": user["rol"] == "yonetici", "abone": subscribed(core, cur, user), "lisans": rights,
            "studio": studio, "indirme": indirme.status(cur, user["id"], core.clock.now())}


def create_blueprint(core) -> Blueprint:
    blueprint = Blueprint("uye", __name__, url_prefix="/api/uye")

    @blueprint.before_request
    def guard():
        check_request()
        g.member = None
        if request.endpoint not in ("uye.sign_in_route", "uye.sign_out_route"):
            g.member = _committed(core, lambda cur: verify(core, cur, request.cookies.get(COOKIE)))

    @blueprint.after_request
    def headers(response: Response) -> Response:
        response.headers.setdefault("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
        response.headers["Referrer-Policy"] = "no-referrer"
        if response.status_code == 401 and COOKIE in request.cookies:
            forget_cookie(response)
        return response

    @blueprint.post("/giris")
    def sign_in_route():
        data = json_body({"kullanici", "sifre"}, {"kullanici", "sifre"})
        login, password = data["kullanici"], data["sifre"]
        if (not isinstance(login, str) or not login.strip() or len(login) > 150 or not isinstance(password, str)
                or not password or len(password) > 256):
            raise ApiError("Kullanıcı adınızı ve şifrenizi yazın.", 400, "EKSIK")
        token, user = _committed(core, lambda cur: sign_in(core, cur, login.strip(), password, client_ip(),
                                                           request.headers.get("User-Agent")))
        with core.db.transaction() as cur:
            subscriber = subscribed(core, cur, user)
        response, status = ok(uye={**profile(user), "abone": subscriber})
        set_cookie(response, token)
        return response, status


    @blueprint.post("/cikis")
    def sign_out_route():
        json_body(set())
        with core.db.transaction() as cur:
            sign_out(cur, request.cookies.get(COOKIE))
        response, status = ok()
        forget_cookie(response)
        return response, status

    @blueprint.get("/hesap")
    def account_route():
        with core.db.transaction() as cur:
            return ok(hesap=account(core, cur, g.member))

    @blueprint.post("/indirme")
    def download_route():
        json_body(set())
        with core.db.transaction() as cur:
            if not subscribed(core, cur, g.member):
                raise ApiError("İndirme bağlantısı aboneliği devam eden hesaplara verilir. Lisansınızı uzatmak için "
                               "uzatma isteği gönderin.", 403, "ABONELIK_YOK")
            moment = core.clock.now()
            cur.execute("SELECT count(*) AS n FROM indirme_baglantilari WHERE kullanici_id = %s AND olusturma > %s",
                        (g.member["id"], moment - timedelta(days=1)))
            if cur.fetchone()["n"] >= LINKS_PER_DAY:
                raise ApiError("Bugün yeterince indirme bağlantısı oluşturdunuz. Yarın yeniden deneyin.", 429, "COK_SIK")
            token, expires = indirme.issue(core, cur, g.member["id"], None)
        return ok(indirme={"baglanti": indirme.address(core, token), "gecerlilik": rules.stamp(expires)})

    @blueprint.post("/uzatma")
    def renewal_route():
        data = json_body({"paket"}, {"paket"})
        package = data["paket"]
        if not isinstance(package, str) or package not in PACKAGES:
            raise ApiError("Paket seçin.")
        user = g.member
        with core.db.transaction() as cur:
            moment = core.clock.now()
            cur.execute("SELECT 1 FROM yonetim_olaylari WHERE tur = 'uzatma_istegi' AND hedef = %s AND zaman > %s",
                        (f"kullanici:{user['id']}", moment - timedelta(days=1)))
            if cur.fetchone():
                raise ApiError("Uzatma isteğinizi aldık; ödeme bilgisini size iletiyoruz.", 429, "COK_SIK")
            cur.execute("INSERT INTO yonetim_olaylari (yonetici_id, tur, hedef, ayrinti, zaman) "
                        "VALUES (NULL, 'uzatma_istegi', %s, %s, %s)",
                        (f"kullanici:{user['id']}", Jsonb({"paket": package, "kaynak": "hesabim"}), moment))
        notify.deliver(core, "Lisans uzatma isteği", f"{display_name(user)} · {PACKAGES[package]} (Hesabım; giriş "
                       f"yapmış hesap)\nHesap: {core.settings.public_base}/yonetim/#kullanici-{user['id']}")
        return ok(message="Uzatma isteğiniz alındı. Ödeme bilgisini e-posta adresinize iletiyoruz.")

    return blueprint
