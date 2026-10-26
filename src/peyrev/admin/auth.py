"""Yönetim paneli kimliği: giriş, sunucu tarafı oturum, CSRF kuralı ve iki adımlı doğrulama.

Giriş (POST /api/yonetim/giris {giris, sifre, kod?}):
- Yalnız durumu 'aktif' ve rolü 'yonetici' olan hesap girer. Giriş adı kuralı Studio'yla aynıdır (users.find_by_login),
  şifre Werkzeug özetiyle denetlenir (rules.matches); aynı şifreyle birden çok hesap eşleşirse 409 BELIRSIZ.
- Deneme sınırı Studio girişiyle ortak sayaçtır (giris_denemeleri): 15 dakikada hesap başına 10, adres başına 30 hatalı
  deneme; sınırdaki doğru şifre de 429 COK_DENEME alır. Hesap sayacında hem yazılan ada hem eşleşen hesapların asıl
  giriş adına bakılır ve tek hesaba inen hatalı deneme asıl adla yazılır: kısa ad ile e-postanın tamamı arasında geçmek
  yeni deneme hakkı vermez. Yanlış iki adımlı kod da hatalı denemedir.
- Hesapta iki adımlı doğrulama açıksa (kullanicilar.totp_gizli) kod şifre doğrulandıktan sonra istenir: kod yoksa
  401 KOD_GEREKLI, yanlış ya da daha önce kullanılmışsa 401 KOD_HATALI.

Oturum: rastgele belirteç yalnız `peyrev_yonetim` çerezinde durur (HttpOnly, Secure, SameSite=Strict, Path=/);
yonetim_oturumlari belirtecin SHA-256 özetini saklar. Oturum girişten 12 saat sonra ya da 2 saat istek gelmezse biter.
Her istekte hesap yeniden okunur: pasifleşen, yönetici rolü kalkan ya da şifresi değişen hesabın oturumu silinir ve
401 OTURUM_GECERSIZ döner. Bir yöneticinin en çok 10 oturumu açık kalır; yeni giriş en eskisini kapatır.

CSRF: durum değiştiren her istek (GET, HEAD ve OPTIONS dışındakiler; giriş dahil) `X-Peyrev-Yonetim: 1` başlığını ve
public_base kökünden Origin'i (Origin yoksa Referer'i) taşımalıdır; tarayıcı Sec-Fetch-Site bildiriyorsa o da
'same-origin' olmalıdır; aksi 403 CSRF. Panel sayfası no-referrer kuralıyla sunulur; panelin istekleri
`referrerPolicy: 'same-origin'` ile gider, böylece Origin göndermeyen tarayıcıda da Referer denetlenir. Özel başlık
basit (CORS ön denetimsiz) istekle gönderilemez, çerez de SameSite=Strict'tir.

İki adımlı doğrulamayı açmak oturuma bağlı iki adımdır: şifre yeniden sorulur, gizli anahtar ve otpauth bağlantısı bir kez
gösterilir (10 dakika), ardından doğru kodla onaylanır; açılınca yöneticinin diğer panel oturumları kapanır. Kapatmak
şifre ve kod ister. Oturum içindeki yanlış şifre ve kod 403 döner (401 yalnız oturumun kendisi içindir).
"""

from __future__ import annotations

import functools
import hashlib
import hmac
import re
import secrets
import uuid
from datetime import datetime, timedelta
from typing import Any

from flask import Response, request
from psycopg import Cursor
from werkzeug.security import generate_password_hash

from .. import users
from ..accounts import rules
from ..common import LOG, ApiError
from . import events, totp

COOKIE = "peyrev_yonetim"
HEADER = "X-Peyrev-Yonetim"
ABSOLUTE = timedelta(hours=12)
IDLE = timedelta(hours=2)
SESSIONS_PER_ADMIN = 10
SETUP_LIFETIME = timedelta(minutes=10)
TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")  # secrets.token_urlsafe(32)
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

WRONG_LOGIN = "Kullanıcı adı veya şifre hatalı."
ADMIN_ONLY = "Bu panel yalnız platform yöneticisine açıktır."
CODE_REQUIRED = "İki adımlı doğrulama kodunu yazın."
WRONG_CODE = "Doğrulama kodu hatalı ya da kullanılmış. Uygulamadaki güncel kodu yazın."
WRONG_PASSWORD = "Şifre hatalı."
SESSION_REQUIRED = "Yönetim oturumu gerekli. Giriş yapın."
SESSION_CLOSED = "Oturumunuz kapandı. Yeniden giriş yapın."
SESSION_EXPIRED = "Oturumunuzun süresi doldu. Yeniden giriş yapın."
UNREADABLE = ("İki adımlı doğrulama bilgisi okunamadı. Başka bir yöneticinin kullanıcı ayrıntısından sıfırlaması "
              "gerekir.")


def digest(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


# ----- CSRF ve çerez -----------------------------------------------------------------------------
def same_origin(core) -> bool:
    if request.headers.get("Sec-Fetch-Site", "same-origin") != "same-origin":
        return False
    base = core.settings.public_base.rstrip("/")
    origin = request.headers.get("Origin")
    if origin is not None:
        return origin == base
    referer = request.headers.get("Referer") or ""
    return referer == base or referer.startswith(base + "/")


def check_request(core) -> None:
    if request.method in SAFE_METHODS:
        return
    if request.headers.get(HEADER) != "1" or not same_origin(core):
        raise ApiError("İstek doğrulanamadı. Sayfayı yenileyip tekrar deneyin.", 403, "CSRF")


def set_cookie(response: Response, token: str) -> None:
    response.set_cookie(COOKIE, token, max_age=int(ABSOLUTE.total_seconds()), path="/", secure=True, httponly=True,
                        samesite="Strict")


def forget_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE, path="/", secure=True, httponly=True, samesite="Strict")


# ----- görünümler --------------------------------------------------------------------------------
def profile(admin: dict[str, Any]) -> dict[str, Any]:
    return {"kullanici_id": admin["id"], "giris": admin["giris"], "ad_soyad": admin["ad_soyad"],
            "iki_adim": bool(admin["totp_gizli"])}


def session_view(session: dict[str, Any]) -> dict[str, Any]:
    return {"acilis": rules.stamp(session["olusturma"]), "bitis": rules.stamp(session["bitis"]),
            "bosta_dakika": int(IDLE.total_seconds() // 60)}


def sessions_of(cur: Cursor, admin_id: int, current_id: str) -> list[dict[str, Any]]:
    """Yöneticinin açık panel oturumları (Hesabım): en son kullanılan önce."""
    cur.execute("SELECT id, ip, tarayici, olusturma, son_gorulme, bitis FROM yonetim_oturumlari "
                "WHERE kullanici_id = %s ORDER BY son_gorulme DESC, id", (admin_id,))
    return [{"id": row["id"], "bu_oturum": row["id"] == current_id, "acilis": rules.stamp(row["olusturma"]),
             "son_islem": rules.stamp(row["son_gorulme"]), "bitis": rules.stamp(row["bitis"]), "ip": row["ip"],
             "tarayici": row["tarayici"]} for row in cur.fetchall()]


# ----- giriş ve oturum ---------------------------------------------------------------------------
@functools.cache
def _decoy() -> str:
    """Giriş adı bulunmadığında da bir özet denetlenir: yanıt süresi adın varlığını belli etmez."""
    return generate_password_hash(secrets.token_urlsafe(16))


def _agent(value: str | None) -> str:
    return "".join(char for char in (value or "") if char.isprintable())[:200]


def _use_step(cur: Cursor, user_id: int, step: int, *, replace: bool = False) -> bool:
    """Kabul edilen TOTP adımını yazar; aynı ya da daha eski bir adım ikinci kez geçmez (eş zamanlı isteklerde de)."""
    guard = "" if replace else " WHERE yonetim_iki_adim.son_adim < EXCLUDED.son_adim"
    cur.execute("INSERT INTO yonetim_iki_adim (kullanici_id, son_adim) VALUES (%s, %s) ON CONFLICT (kullanici_id) "
                "DO UPDATE SET son_adim = EXCLUDED.son_adim" + guard + " RETURNING son_adim", (user_id, step))
    return cur.fetchone() is not None


def _check_code(core, cur: Cursor, admin: dict[str, Any], code: Any, address: str, moment: datetime,
                status: int) -> ApiError | None:
    """Açık iki adımlı doğrulamanın kodu; None: kod geçti ve adımı kullanıldı. Yanlış kod hatalı deneme sayılır."""
    if code is None or code == "":
        return ApiError(CODE_REQUIRED, status, "KOD_GEREKLI")
    secret = totp.unseal(core.keys, admin["id"], admin["totp_gizli"])
    if secret is None:
        LOG.error("Yöneticinin iki adımlı doğrulama anahtarı açılamadı (kullanıcı %s).", admin["id"])
        raise ApiError(UNREADABLE, 409, "IKI_ADIM_OKUNAMADI")
    cur.execute("SELECT son_adim FROM yonetim_iki_adim WHERE kullanici_id = %s", (admin["id"],))
    used = cur.fetchone()
    step = totp.matching_step(secret, code, moment, after=used["son_adim"] if used else None)
    if step is None or not _use_step(cur, admin["id"], step):
        rules.login_failed(cur, admin["giris"], address, moment)
        return ApiError(WRONG_CODE, status, "KOD_HATALI")
    return None


def _open(cur: Cursor, admin: dict[str, Any], address: str, agent: str | None,
          moment: datetime) -> tuple[str, dict[str, Any]]:
    cur.execute("DELETE FROM yonetim_oturumlari WHERE bitis <= %s OR son_gorulme <= %s", (moment, moment - IDLE))
    token, session_id = secrets.token_urlsafe(32), uuid.uuid4().hex
    cur.execute("INSERT INTO yonetim_oturumlari (id, kullanici_id, belirtec_ozeti, sifre_izi, ip, tarayici, olusturma, "
                "son_gorulme, bitis) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *",
                (session_id, admin["id"], digest(token), users.fingerprint(admin["sifre_hash"]), address[:64],
                 _agent(agent), moment, moment, moment + ABSOLUTE))
    session = cur.fetchone()
    cur.execute("DELETE FROM yonetim_oturumlari WHERE kullanici_id = %s AND id <> %s AND id NOT IN ("
                "SELECT id FROM yonetim_oturumlari WHERE kullanici_id = %s AND id <> %s "
                "ORDER BY son_gorulme DESC, id LIMIT %s)",
                (admin["id"], session_id, admin["id"], session_id, SESSIONS_PER_ADMIN - 1))
    return token, session


def sign_in(core, cur: Cursor, login: str, password: str, code: Any, address: str, agent: str | None,
            previous: str | None = None) -> tuple[str, dict[str, Any], dict[str, Any]] | ApiError:
    """(çerez belirteci, yönetici, oturum) ya da hatalı denemeyi yazmış ret (işlem kaydedildikten sonra fırlatılır)."""
    moment = core.clock.now()
    candidates = users.find_by_login(cur, login)
    for name in dict.fromkeys([login] + [user["giris"] for user in candidates]):
        rules.login_attempts_left(cur, name, address, moment)
    matched = [user for user in candidates if rules.matches(user["sifre_hash"], password)]
    if not candidates:
        rules.matches(_decoy(), password)
    if len(matched) > 1:
        raise ApiError(rules.AMBIGUOUS, 409, "BELIRSIZ")
    if not matched:
        rules.login_failed(cur, candidates[0]["giris"] if len(candidates) == 1 else login, address, moment)
        return ApiError(WRONG_LOGIN, 401, "HATALI_GIRIS")
    admin = matched[0]
    if admin["rol"] != "yonetici":
        raise ApiError(ADMIN_ONLY, 403, "YETKI_YOK")
    if admin["totp_gizli"]:
        refusal = _check_code(core, cur, admin, code, address, moment, 401)
        if refusal is not None:
            return refusal
    if isinstance(previous, str) and TOKEN.fullmatch(previous):
        # The browser's earlier session ends with the new sign-in.
        cur.execute("DELETE FROM yonetim_oturumlari WHERE belirtec_ozeti = %s", (digest(previous),))
    token, session = _open(cur, admin, address, agent, moment)
    events.record(core, cur, admin["id"], "yonetim_girisi", f"kullanici:{admin['id']}", ip=address[:64],
                  iki_adim=bool(admin["totp_gizli"]))
    return token, admin, session


def verify(core, cur: Cursor, token: Any) -> tuple[dict[str, Any], dict[str, Any]] | ApiError:
    """(yönetici, oturum); geçersiz oturum silinir ve ret döner (silme kaydedildikten sonra fırlatılır)."""
    if not isinstance(token, str) or not TOKEN.fullmatch(token):
        raise ApiError(SESSION_REQUIRED, 401, "OTURUM_GEREKLI")
    cur.execute("SELECT * FROM yonetim_oturumlari WHERE belirtec_ozeti = %s", (digest(token),))
    session = cur.fetchone()
    if session is None:
        raise ApiError(SESSION_CLOSED, 401, "OTURUM_GECERSIZ")
    moment = core.clock.now()
    admin = users.get(cur, session["kullanici_id"])
    if moment >= session["bitis"] or moment - session["son_gorulme"] >= IDLE:
        refusal = ApiError(SESSION_EXPIRED, 401, "OTURUM_GECERSIZ")
    elif (admin is None or admin["durum"] != "aktif" or admin["rol"] != "yonetici"
          or not hmac.compare_digest(users.fingerprint(admin["sifre_hash"]), session["sifre_izi"])):
        refusal = ApiError(SESSION_CLOSED, 401, "OTURUM_GECERSIZ")
    else:
        cur.execute("UPDATE yonetim_oturumlari SET son_gorulme = %s WHERE id = %s", (moment, session["id"]))
        session["son_gorulme"] = moment
        return admin, session
    # A session that failed once never comes back, not even if the account is restored later.
    cur.execute("DELETE FROM yonetim_oturumlari WHERE id = %s", (session["id"],))
    return refusal


def sign_out(core, cur: Cursor, admin_id: int, session_id: str) -> None:
    cur.execute("DELETE FROM yonetim_oturumlari WHERE id = %s", (session_id,))
    events.record(core, cur, admin_id, "yonetim_cikisi", f"kullanici:{admin_id}")


def end_sessions(cur: Cursor, user_id: int, *, keep: str | None = None) -> int:
    """Hesabın panel oturumlarını (`keep` dışındakileri) kapatır; kapanan sayısı."""
    if keep is None:
        cur.execute("DELETE FROM yonetim_oturumlari WHERE kullanici_id = %s", (user_id,))
    else:
        cur.execute("DELETE FROM yonetim_oturumlari WHERE kullanici_id = %s AND id <> %s", (user_id, keep))
    return cur.rowcount


def close_others(core, cur: Cursor, admin_id: int, session_id: str) -> int:
    closed = end_sessions(cur, admin_id, keep=session_id)
    if closed:
        events.record(core, cur, admin_id, "yonetim_oturumlari_kapatildi", f"kullanici:{admin_id}", adet=closed)
    return closed


# ----- iki adımlı doğrulama ----------------------------------------------------------------------
def _current(cur: Cursor, admin_id: int) -> dict[str, Any]:
    """Yönetici satırı kilitli okunur: aynı hesabın iki adımlı doğrulama işlemleri sırayla işlenir."""
    admin = users.get(cur, admin_id, lock=True)
    if admin is None or admin["durum"] != "aktif" or admin["rol"] != "yonetici":
        raise ApiError(SESSION_CLOSED, 401, "OTURUM_GECERSIZ")
    return admin


def _check_password(cur: Cursor, admin: dict[str, Any], password: Any, address: str,
                    moment: datetime) -> ApiError | None:
    rules.login_attempts_left(cur, admin["giris"], address, moment)
    if isinstance(password, str) and 0 < len(password) <= 256 and rules.matches(admin["sifre_hash"], password):
        return None
    rules.login_failed(cur, admin["giris"], address, moment)
    return ApiError(WRONG_PASSWORD, 403, "SIFRE_HATALI")


def start_two_factor(core, cur: Cursor, admin_id: int, session_id: str, password: Any,
                     address: str) -> dict[str, Any] | ApiError:
    """Yeni gizli anahtar: yalnız bu yanıtta görünür, bu oturumda 10 dakika onay bekler."""
    admin = _current(cur, admin_id)
    if admin["totp_gizli"]:
        raise ApiError("İki adımlı doğrulama zaten açık.", 409, "IKI_ADIM_ACIK")
    moment = core.clock.now()
    refusal = _check_password(cur, admin, password, address, moment)
    if refusal is not None:
        return refusal
    secret, until = totp.new_secret(), moment + SETUP_LIFETIME
    cur.execute("UPDATE yonetim_oturumlari SET totp_bekleyen = %s, totp_bekleyen_bitis = %s WHERE id = %s",
                (totp.seal(core.keys, admin_id, secret), until, session_id))
    return {"gizli": totp.readable(secret), "baglanti": totp.link(secret, admin["giris"]),
            "gecerlilik": rules.stamp(until)}


def confirm_two_factor(core, cur: Cursor, admin_id: int, session_id: str, code: Any,
                       address: str) -> dict[str, Any] | ApiError:
    admin = _current(cur, admin_id)
    if admin["totp_gizli"]:
        raise ApiError("İki adımlı doğrulama zaten açık.", 409, "IKI_ADIM_ACIK")
    moment = core.clock.now()
    cur.execute("SELECT totp_bekleyen, totp_bekleyen_bitis FROM yonetim_oturumlari WHERE id = %s", (session_id,))
    pending = cur.fetchone() or {}
    secret = totp.unseal(core.keys, admin_id, pending.get("totp_bekleyen"))
    if secret is None or pending["totp_bekleyen_bitis"] <= moment:
        raise ApiError("Kurulumun süresi doldu. İki adımlı doğrulamayı yeniden başlatın.", 409, "KURULUM_YOK")
    rules.login_attempts_left(cur, admin["giris"], address, moment)
    step = totp.matching_step(secret, code, moment)
    if step is None:
        rules.login_failed(cur, admin["giris"], address, moment)
        return ApiError(WRONG_CODE, 403, "KOD_HATALI")
    cur.execute("UPDATE kullanicilar SET totp_gizli = %s, guncelleme = %s WHERE id = %s",
                (pending["totp_bekleyen"], moment, admin_id))
    _use_step(cur, admin_id, step, replace=True)
    cur.execute("UPDATE yonetim_oturumlari SET totp_bekleyen = NULL, totp_bekleyen_bitis = NULL WHERE id = %s",
                (session_id,))
    # Sessions opened with the password alone end: from now on every sign-in needs the code.
    closed = end_sessions(cur, admin_id, keep=session_id)
    events.record(core, cur, admin_id, "iki_adim_acildi", f"kullanici:{admin_id}", kapatilan_panel_oturumu=closed)
    return {"iki_adim": True, "kapatilan": closed}


def stop_two_factor(core, cur: Cursor, admin_id: int, password: Any, code: Any,
                    address: str) -> dict[str, Any] | ApiError:
    admin = _current(cur, admin_id)
    if not admin["totp_gizli"]:
        raise ApiError("İki adımlı doğrulama zaten kapalı.", 409, "IKI_ADIM_KAPALI")
    moment = core.clock.now()
    refusal = (_check_password(cur, admin, password, address, moment)
               or _check_code(core, cur, admin, code, address, moment, 403))
    if refusal is not None:
        return refusal
    cur.execute("UPDATE kullanicilar SET totp_gizli = NULL, guncelleme = %s WHERE id = %s", (moment, admin_id))
    events.record(core, cur, admin_id, "iki_adim_kapatildi", f"kullanici:{admin_id}")
    return {"iki_adim": False}
