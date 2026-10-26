"""Studio lisansı: /api/rpa/lisans/{anahtar, giris, yenile, cikis} (orkestrai ile aynı sözleşme, protokol 2).

Studio her açılışta kullanıcı adı ve şifreyle giriş yapar, ardından her `aralik` saniyede (600) yenileme belirteciyle taze
lisans ister. Her yanıt, isteğin rastgele değerini (nonce) taşıyan Ed25519 imzalı bir lisanstır: eski bir yanıt yeniden
kullanılamaz. Onay alamayan Studio en fazla `tolerans` saniye (3600) çalışır. Lisans kuralı her istekte veritabanından
okunur (users.module_state): MOD_RPA satırı ve bitiş günü (Europe/Istanbul) dahil; yönetici rolü satırsız da açıktır.
Peyrev'e yalnız 1.0.0 ve sonrası bağlanır; eski Studio bildiği YETKI_YOK + neden ESKI_SURUM ile kilitlenir.

Studio yalnız SURE_DOLDU, YETKI_YOK ve OTURUM_GECERSIZ ile kilitlenir; yenilemede başka bir kod "bağlantı yok" sayılır.
Firma yoktur: lisanstaki firma_id = kullanıcının alan_id'si (Studio yerel ajan verisini bununla bölümler), firma_adi
boş, hesap_turu "bireysel".
"""

from __future__ import annotations

import base64
import hmac
import json
import re
from datetime import date, datetime, time, timedelta
from typing import Any

import jwt
from flask import Blueprint, request
from psycopg import Cursor

from .. import tokens, users
from ..agents import entitlement
from ..clock import TR
from ..common import BODY_LIMIT, LOG, ApiError, client_ip, ok, parse_json
from ..db import advisory_lock
from . import applications, rules, sessions

# İmzalı iddialardaki ürün kodu Studio'nun cihaz özetine de girer; değişmez.
PRODUCT = "rpa-orkestrai"
PROTOCOL = 2
REFRESH_SECONDS = 600
TOLERANCE_SECONDS = 3600
MINIMUM_VERSION = (1, 0, 0)
REFRESH_LIFETIME = timedelta(days=30)
REFRESH_AUDIENCE = "peyrev-yenileme"
REFRESH_CLAIMS = ["exp", "aud", "kullanici_id", "cihaz", "sifre_izi", "oturum", "sira"]
# Cihaz kimliği, istek başına rastgele değer (nonce) ve çalışan Studio'nun kimliği (ornek).
IDENTIFIER = re.compile(r"[0-9a-f]{32,64}")
VERSION = re.compile(r"(\d{1,4})\.(\d{1,4})\.(\d{1,4})")


def version_tuple(value: Any) -> tuple[int, ...]:
    """'1.0.0' -> (1, 0, 0), '1.12.3-beta' -> (1, 12, 3); okunamayan sürüm en eski sayılır."""
    match = VERSION.match(str(value or ""))
    return tuple(int(part) for part in match.groups()) if match else (0, 0, 0)


def valid_nonce(value: Any) -> str | None:
    return value if isinstance(value, str) and IDENTIFIER.fullmatch(value) else None


def outdated(core, data: dict) -> ApiError | None:
    """Desteklenmeyen Studio'nun reddi; sürüm, protokol ve istek başına rastgele değer birlikte gerekir."""
    if (version_tuple(data.get("surum")) >= MINIMUM_VERSION and type(data.get("protokol")) is int
            and data["protokol"] >= PROTOCOL and valid_nonce(data.get("nonce"))):
        return None
    return ApiError(f"Bu Peyrev sürümü artık desteklenmiyor. Güncel sürümü {core.settings.public_base}/indir/ "
                    "adresinden kurun.", 403, "YETKI_YOK", neden="ESKI_SURUM")


def license_refusal(cur: Cursor, user: dict[str, Any], today: date) -> tuple[ApiError | None, date | None]:
    """(ret | None, bitiş günü): MOD_RPA hakkı her istekte yeniden okunur."""
    state, end = users.module_state(cur, user, users.RPA, today)
    if state == users.EXPIRED:
        return ApiError(f"Peyrev kullanım süreniz {end:%d.%m.%Y} tarihinde dolmuştur.", 403, "SURE_DOLDU",
                        bitis=end.isoformat()), end
    if state == users.MISSING:
        return ApiError("Bu kullanıcı için Peyrev lisansı tanımlı değil.", 403, "YETKI_YOK"), end
    return None, end


def session_expired() -> ApiError:
    return ApiError(sessions.SESSION_EXPIRED, 401, "OTURUM_GECERSIZ")


def sign(core, payload: dict[str, Any]) -> dict[str, str]:
    """Studio'nun doğruladığı zarf: imzalanan baytlar tam olarak payload'ın base64 çözümüdür (standart alfabe)."""
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {"payload": base64.b64encode(body).decode("ascii"),
            "signature": base64.b64encode(core.keys.sign(body)).decode("ascii")}


def refresh_token(core, user: dict[str, Any], device: str, session_id: str, number: int) -> str:
    """30 gün geçerli; her yenilemede yenisi verilir. Şifre izi değişince (yeni şifre) geçersizdir."""
    claims = {"aud": REFRESH_AUDIENCE, "kullanici_id": int(user["id"]), "cihaz": device,
              "sifre_izi": users.fingerprint(user["sifre_hash"]), "oturum": session_id, "sira": number,
              "exp": int((core.clock.now() + REFRESH_LIFETIME).timestamp())}
    return jwt.encode(claims, core.keys.derive(REFRESH_AUDIENCE), algorithm="HS256")


def refresh_claims(core, token: Any) -> dict[str, Any] | None:
    """Yenileme belirtecinin iddiaları; imza, hedef, süre (çekirdeğin saatiyle) veya biçim tutmazsa None."""
    if not isinstance(token, str) or not token or len(token) > 4096:
        return None
    try:
        claims = jwt.decode(token, core.keys.derive(REFRESH_AUDIENCE), algorithms=["HS256"], audience=REFRESH_AUDIENCE,
                            options={"require": REFRESH_CLAIMS, "verify_exp": False})
    except jwt.PyJWTError:
        return None
    if (not isinstance(claims["exp"], int) or claims["exp"] < core.clock.now().timestamp()
            or type(claims["kullanici_id"]) is not int or not isinstance(claims["cihaz"], str)
            or not isinstance(claims["oturum"], str) or not claims["oturum"]):
        return None
    return claims


def _agent_extension(core, cur: Cursor, user: dict[str, Any], session_id: str) -> tuple[dict[str, Any], Any]:
    """Ajan modülünün uzantısı; hesaplanamazsa ajanlar kapalı, RPA lisansı yine verilir (orkestrai'deki gibi)."""
    try:
        # A savepoint: a failing query here must not abort the sign-in transaction.
        with cur.connection.transaction():
            return entitlement.license_extension(core, cur, user, session_id)
    except Exception as error:  # noqa: BLE001 - agents being unavailable never costs the license
        LOG.warning("Ajan lisans uzantısı hesaplanamadı (%s); ajan erişimi kapalı.", type(error).__name__)
        return {"surum": 1, "aktif": False, "firma_id": int(user["alan_id"]), "kullanici_id": int(user["id"]),
                "onayci_id": None, "oturum_id": session_id, "bitis": None, "yetkiler": []}, None


def answer(core, cur: Cursor, user: dict[str, Any], device: str, data: dict, end: date | None, session_id: str,
           number: int) -> dict[str, Any]:
    """Başarılı giriş ve yenilemenin yanıtı: imzalı lisans, yenileme belirteci, ajan ve destek erişimi."""
    moment = core.clock.now_tr().replace(microsecond=0)
    valid_until = moment + timedelta(seconds=TOLERANCE_SECONDS)
    if end is not None:
        valid_until = min(valid_until, datetime.combine(end, time(23, 59, 59), TR))
    payload = {
        "surum": 1, "urun": PRODUCT, "modul": users.RPA, "kullanici_id": int(user["id"]), "kullanici": user["giris"],
        "ad_soyad": user["ad_soyad"], "firma_id": int(user["alan_id"]), "firma_adi": "",
        "bitis": end.isoformat() if end else None, "hesap_turu": "bireysel", "cihaz": device,
        "verildi": moment.isoformat(), "gecerlilik": valid_until.isoformat(), "tolerans": TOLERANCE_SECONDS,
        "aralik": REFRESH_SECONDS,
        # The random value of this request is signed: an earlier answer can never be replayed.
        "nonce": valid_nonce(data.get("nonce")),
    }
    payload["ajan"], agent_limit = _agent_extension(core, cur, user, session_id)
    result = {"lisans": sign(core, payload), "yenileme": refresh_token(core, user, device, session_id, number)}
    if payload["ajan"].get("aktif") is True:
        try:
            result["ajan_erisim"] = tokens.issue(core, "ajan", user, device, session_id, expires=agent_limit)
        except Exception as error:  # noqa: BLE001 - the license answer never fails because of this token
            LOG.warning("Ajan erişim belirteci üretilemedi (%s).", type(error).__name__)
    try:
        result["destek_erisim"] = tokens.issue(core, "destek", user, device, session_id)
    except Exception as error:  # noqa: BLE001
        LOG.warning("Destek erişim belirteci üretilemedi (%s).", type(error).__name__)
    return result


def _body() -> dict:
    """Esnek okuma (orkestrai gibi): JSON nesnesi olmayan gövde boş sayılır, bilinmeyen alanlar yok sayılır; böylece
    yeni bir Studio alan eklese de giriş sürer. Boyut sınırı ve sıkı JSON (yinelenen anahtar, NaN yok) ortak kuraldır."""
    if request.content_length is not None and request.content_length > BODY_LIMIT:
        raise ApiError("İstek çok büyük.", 413, "GOVDE_SINIRI")
    raw = request.get_data(cache=False)
    if len(raw) > BODY_LIMIT:
        raise ApiError("İstek çok büyük.", 413, "GOVDE_SINIRI")
    try:
        body = parse_json(raw)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return {}
    return body if isinstance(body, dict) else {}


def _login(core, cur: Cursor, data: dict, login: str, password: str, device: str, address: str) -> Any:
    moment = core.clock.now()
    # The same user name may belong to several accounts (ali@a.test, ali@b.test): the password picks one.
    candidates = users.find_by_login(cur, login)
    waiting = applications.pending(cur, login)
    rules.login_attempts_left(cur, login, address, moment, candidates + waiting)
    matched = [user for user in candidates if rules.matches(user["sifre_hash"], password)]
    if len(matched) > 1:
        raise ApiError(rules.AMBIGUOUS, 409, "BELIRSIZ")
    if not matched:
        hashes = [row["sifre_hash"] for row in waiting]
        if any(rules.matches(value, password) for value in hashes):
            raise ApiError(applications.PENDING_MESSAGE, 403, "BASVURU_BEKLIYOR")
        rules.equalize([user["sifre_hash"] for user in candidates] + hashes, password)
        rules.login_failed(cur, rules.failed_name(login, candidates + waiting), address, moment)
        return ApiError("Kullanıcı adı veya şifre hatalı.", 401, "HATALI_GIRIS")
    user = matched[0]
    refusal, end = license_refusal(cur, user, core.clock.today_tr())
    # The version check runs after the password check: an old Studio with a wrong password hears HATALI_GIRIS.
    refusal = outdated(core, data) or refusal
    if refusal:
        raise refusal
    # Concurrent sign-ins of one account are taken in turn (FOR UPDATE locks no row of an empty result).
    advisory_lock(cur, f"peyrev-oturum-{user['id']}")
    session_id, number = sessions.start(cur, user, device, data.get("surum"), address, moment,
                                        instance=valid_nonce(data.get("ornek")))
    return answer(core, cur, user, device, data, end, session_id, number)


def _renew(core, cur: Cursor, data: dict, claims: dict[str, Any], device: str) -> Any:
    user = users.get(cur, claims["kullanici_id"])
    # A new password or a passive account ends the kept session.
    if (user is None or user["durum"] != "aktif" or not isinstance(claims["sifre_izi"], str)
            or not hmac.compare_digest(users.fingerprint(user["sifre_hash"]), claims["sifre_izi"])):
        raise session_expired()
    refusal, end = license_refusal(cur, user, core.clock.today_tr())
    refusal = outdated(core, data) or refusal
    if refusal:
        # The session stays open and its number does not move: a restored license resumes without a new sign-in.
        raise refusal
    outcome, value = sessions.renew(cur, claims["oturum"], claims["sira"], user["id"], device, data.get("surum"),
                                    client_ip(), core.clock.now(), instance=valid_nonce(data.get("ornek")))
    if outcome != sessions.OK:
        return ApiError(sessions.CLOSE_MESSAGES.get(value, sessions.SESSION_EXPIRED), 401, "OTURUM_GECERSIZ", neden=value)
    return answer(core, cur, user, device, data, end, claims["oturum"], value)


def create_blueprint(core) -> Blueprint:
    blueprint = Blueprint("lisans", __name__, url_prefix="/api/rpa/lisans")

    @blueprint.get("/anahtar")
    def public_key():
        """Studio sürümüne gömülen açık anahtar (gizli değildir); işletim denetimi gömülü değerle karşılaştırır."""
        return ok(anahtar=core.keys.license_public_key, protokol=PROTOCOL,
                  asgari_surum=".".join(str(part) for part in MINIMUM_VERSION))

    # A refusal that wrote something (a failed attempt, a session closed as a copy) is returned, not raised, so the
    # transaction keeps the write; it is raised once the transaction has committed.
    @blueprint.post("/giris")
    def sign_in():
        data = _body()
        login, password, device = str(data.get("kullanici") or "").strip(), data.get("sifre"), str(data.get("cihaz") or "")
        if not login or not isinstance(password, str) or not password:
            raise ApiError("Kullanıcı adı ve şifre gereklidir.", 400, "EKSIK")
        if len(login) > 150 or len(password) > 256 or not IDENTIFIER.fullmatch(device):
            raise ApiError("Geçersiz giriş isteği.", 400, "EKSIK")
        with core.db.transaction() as cur:
            result = _login(core, cur, data, login, password, device, client_ip())
        if isinstance(result, ApiError):
            raise result
        return ok(**result)

    @blueprint.post("/yenile")
    def renew():
        data = _body()
        device, claims = str(data.get("cihaz") or ""), refresh_claims(core, data.get("token"))
        if claims is None or not IDENTIFIER.fullmatch(device) or claims["cihaz"] != device:
            raise session_expired()
        with core.db.transaction() as cur:
            result = _renew(core, cur, data, claims, device)
        if isinstance(result, ApiError):
            raise result
        return ok(**result)

    @blueprint.post("/cikis")
    def sign_out():
        """Bu bilgisayarın oturumu sunucuda da kapanır; Studio sonucu beklemez, yanıt her zaman başarıdır."""
        data = _body()
        claims = refresh_claims(core, data.get("token"))
        if claims is not None and claims["cihaz"] == str(data.get("cihaz") or ""):
            try:
                with core.db.transaction() as cur:
                    sessions.end(cur, claims["oturum"], claims["kullanici_id"], core.clock.now())
            except Exception as error:  # noqa: BLE001 - signing out works even without the server
                LOG.warning("Studio oturumu kapatılamadı (%s).", type(error).__name__)
        return ok()

    return blueprint
