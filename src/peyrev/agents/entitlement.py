"""Ajan yetkisi: lisans yanıtındaki `ajan` uzantısı ve ajan uçlarının kimlik doğrulaması.

Sözleşme (imzalar sabittir; diğer modüller yalnız bunları çağırır):

license_extension(core, cur, user, session_id, *, office=None, lock=False) -> (uzantı, belirteç_sınırı)
  Uzantı Studio'nun beklediği biçimdedir: {"surum": 1, "aktif": bool, "firma_id": alan_id, "kullanici_id": id,
  "onayci_id": ..., "oturum_id": session_id, "bitis": "YYYY-MM-DD" | None, "yetkiler": [...], "ofis_id": ..., "ofis_adi": ...,
  ["ofisler": [{"id","ad","yetkili"}], "ofis_kurabilir": true]}.
  Lisans modülü yalnız uzantı["aktif"] ise ajan_erisim üretir:
  tokens.issue(core, "ajan", user, cihaz, session_id, expires=belirteç_sınırı)  (None: yalnız 720 sn sınırı).

authenticate(core, cur, authorization, office=None, *, lock=False) -> AgentIdentity
  `Authorization: Bearer <ajan_erisim>` ve isteğe bağlı `X-Rpa-Ofis` değeriyle kimliği ve yetkiyi doğrular; geçersizse
  ApiError: 401 OTURUM_GEREKLI / OTURUM_GECERSIZ, 400 GECERSIZ_GIRDI (ofis kimliği biçimi), 403 OFIS_ERISIMI_YOK,
  403 AJAN_YETKISI_YOK. Ortak model (peyrev.llm) ve ajan uçları bunu kullanır.

Kurallar (firma yoktur; ofis tek bir hesaba aittir):
- Hesap aktif olmalı ve kullanıcının hem MOD_RPA hem MOD_AJAN satırı geçerli olmalıdır; yönetici ayrıcalığı yoktur.
  `bitis` iki satırın erken biteni (dahil, Europe/Istanbul); belirteç sınırı ertesi günün 00:00'ı (TR).
- Ofisler hesabın aktif ofisleridir (oluşturma, kimlik sırasıyla). Seçili ofis X-Rpa-Ofis'teki ya da ilk ofistir.
  Sahip her ofisinin yetkilisi ve onaycısıdır: ofisi olan hesap üç yetkiyi, ofissiz hesap yalnız agents.use'u alır.
  `onayci_id` her zaman hesabın kendisidir; `ofis_kurabilir` hesabın (pasif dahil) ofis sayısı hesabın ofis sınırının
  (kullanicilar.ofis_siniri, varsayılan 1; panel değiştirir) altındayken yazılır.
- Birden çok etkin ofisi olan hesapta oturumun Studio sürümü ISOLATED_OFFICES'tan (1.3.0) eskiyse ajan erişimi kapalıdır
  ("guncelleme_gerekli"; uçlar 403 SURUM_ESKI); RPA lisansı etkilenmez. Tek ofisli hesaplar her sürümle açar.
- lock=True: ilk kilit kullanıcı satırıdır; hesabın bütün ajan değişiklikleri burada sıraya girer. Ardından Studio
  oturumu, modül satırları ve ofisler kilitlenir. Kilit beklerken kapanan oturum, değişen şifre, dolan belirteç ve
  geçen gece yarısı kilit altında yeniden denetlenir.
"""

from __future__ import annotations

import hmac
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any

from .. import tokens, users
from ..clock import TR
from ..common import ApiError

CAPABILITIES = ("agents.use", "agents.manage", "agents.approve")
OFFICE_ID = re.compile(r"^[0-9a-f]{32}$")
# Studio names the office it acts for; the server checks on every request that the account still owns it.
OFFICE_HEADER = "X-Rpa-Ofis"
# An account owns at most this many offices; Studio drops the whole agent extension for a longer list. Each account
# has its own limit (kullanicilar.ofis_siniri, 1 by default) up to this: every office is an independent customer.
MAX_OFFICES = 50
OFFICE_LIMIT_REACHED = "Hesabınızın ofis sınırına ulaşıldı."
# Offices became independent customers in Studio 1.3.0 (no work or messages between offices). An account with several
# offices opens Ajanlar only from that version on: an earlier Studio would still let one office reach another.
ISOLATED_OFFICES = (1, 3, 0)
UPDATE_FOR_OFFICES = ("Birden çok ofisli hesap Ajanlar'ı Peyrev 1.3.0 ve sonrasıyla açar; uygulamayı güncelleyin "
                      "(güncelleme kendiliğinden iner, Peyrev'i kapatıp açın).")
STUDIO_VERSION = re.compile(r"(\d{1,4})\.(\d{1,4})\.(\d{1,4})")
NO_OFFICE_ACCESS = "Seçili ofise erişiminiz kalmadı. Ajanlar sayfasını yenileyip ofisinizi yeniden seçin."
NO_LICENSE = "Hesabınız için geçerli Peyrev ve ajan lisansı gerekli."
SESSION_LOCK = ("SELECT id FROM oturumlar WHERE id = %s AND kullanici_id = %s AND cihaz = %s AND durum = 'aktif' "
                "FOR UPDATE")


def _studio_version(cur, session_id: str) -> tuple[int, ...]:
    """The Studio version the session last reported (sign-in and every renewal); unknown counts as the oldest."""
    cur.execute("SELECT surum FROM oturumlar WHERE id = %s", (session_id,))
    row = cur.fetchone()
    match = STUDIO_VERSION.match(str((row or {}).get("surum") or ""))
    return tuple(int(part) for part in match.groups()) if match else (0, 0, 0)


def office_limit(user: dict[str, Any]) -> int:
    """Hesabın kurabileceği en çok ofis (pasifler dahil sayılır)."""
    return max(1, min(MAX_OFFICES, int(user.get("ofis_siniri") or 1)))


@dataclass
class AgentIdentity:
    user: dict[str, Any]
    claims: dict[str, Any]
    entitlement: dict[str, Any]
    office_id: str | None


def _module(cur, user_id: int, module: str, today: date, lock: bool) -> tuple[bool, date | None]:
    # Ajan yetkisinde yönetici ayrıcalığı yoktur: iki modülün de satırı gerekir.
    cur.execute("SELECT bitis FROM kullanici_modulleri WHERE kullanici_id = %s AND modul = %s"
                + (" FOR UPDATE" if lock else ""), (user_id, module))
    row = cur.fetchone()
    if row is None:
        return False, None
    return row["bitis"] is None or row["bitis"] >= today, row["bitis"]


def license_extension(core, cur, user: dict[str, Any], session_id: str, *, office: str | None = None,
                      lock: bool = False) -> tuple[dict[str, Any], datetime | None]:
    """The account's rights in one office: the requested one, otherwise its first office.

    A requested office the account no longer owns (or that is closed) yields an inactive result marked
    'ofis_erisimi_yok'; the request is refused instead of acting for another office. The license call never names
    an office, so the mark never reaches a signed license."""
    user_id = int(user["id"])
    result: dict[str, Any] = {"surum": 1, "aktif": False, "firma_id": int(user["alan_id"]), "kullanici_id": user_id,
                              "onayci_id": None, "oturum_id": session_id, "bitis": None, "yetkiler": [],
                              "ofis_id": None, "ofis_adi": None}
    if lock:
        user = users.get(cur, user_id, lock=True) or {}
    if user.get("durum") != "aktif":
        return result, None
    today = core.clock.today_tr()
    checks = [_module(cur, user_id, module, today, lock) for module in (users.RPA, users.AGENTS)]
    ends = [end for _active, end in checks if end is not None]
    end = min(ends) if ends else None
    result["bitis"] = end.isoformat() if end else None
    if not all(active for active, _end in checks):
        return result, None
    cur.execute("SELECT id, ad, durum FROM ajan_ofisleri WHERE sahip_id = %s ORDER BY olusturma, id"
                + (" FOR UPDATE" if lock else ""), (user_id,))
    owned = cur.fetchall()
    offices = [row for row in owned if row["durum"] == "aktif"][:MAX_OFFICES]
    if len(offices) > 1 and _studio_version(cur, session_id) < ISOLATED_OFFICES:
        result["guncelleme_gerekli"] = True
        return result, None
    if office is None:
        chosen = offices[0] if offices else None
    else:
        chosen = next((row for row in offices if row["id"] == office), None)
        if chosen is None:
            result["ofis_erisimi_yok"] = True
            return result, None
    # A transaction may have waited across midnight while acquiring the locks.
    if end is not None and end < core.clock.today_tr():
        return result, None
    result.update(aktif=True, onayci_id=user_id, yetkiler=["agents.use"])
    if chosen is not None:
        result.update(ofis_id=chosen["id"], ofis_adi=chosen["ad"],
                      ofisler=[{"id": row["id"], "ad": row["ad"], "yetkili": True} for row in offices])
        result["yetkiler"].extend(["agents.manage", "agents.approve"])
    # Closed offices still count: the limit is what creating another office checks.
    if len(owned) < office_limit(user):
        result["ofis_kurabilir"] = True
    limit = datetime.combine(end + timedelta(days=1), time(), TR) if end else None
    return result, limit


def requested_office(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not OFFICE_ID.fullmatch(value):
        raise ApiError("Seçili ofis kimliği geçersiz.", 400, "GECERSIZ_GIRDI")
    return value


def authenticate(core, cur, authorization: str | None, office: str | None = None, *,
                 lock: bool = False) -> AgentIdentity:
    user, claims = tokens.verify(core, cur, "ajan", authorization)
    office = requested_office(office)
    if lock:
        return _locked(core, cur, user["id"], claims, office)
    extension, _limit = license_extension(core, cur, user, claims["oturum"], office=office)
    return _identity(core, user, claims, extension)


def relock(core, cur, identity: AgentIdentity) -> AgentIdentity:
    """The identity of an authenticated request once more, under the account lock: every record change starts here.

    It acts for the office the request was authenticated for; an office closed or deleted meanwhile is refused."""
    return _locked(core, cur, identity.user["id"], identity.claims, identity.office_id)


def require(identity: AgentIdentity, capability: str) -> None:
    if capability not in CAPABILITIES or capability not in identity.entitlement["yetkiler"]:
        raise ApiError("Bu işlem için yetkiniz yok.", 403, "AJAN_YETKISI_YOK")


def _locked(core, cur, user_id: int, claims: dict[str, Any], office: str | None) -> AgentIdentity:
    user = users.get(cur, user_id, lock=True)
    cur.execute(SESSION_LOCK, (claims["oturum"], user_id, claims["cihaz"]))
    session = cur.fetchone()
    if user is None or user["durum"] != "aktif" or session is None:
        raise ApiError("Studio oturumu kapatılmış.", 401, "OTURUM_GECERSIZ")
    extension, _limit = license_extension(core, cur, user, claims["oturum"], office=office, lock=True)
    return _identity(core, user, claims, extension, locked=True)


def _identity(core, user: dict[str, Any], claims: dict[str, Any], extension: dict[str, Any], *,
              locked: bool = False) -> AgentIdentity:
    if extension.get("ofis_erisimi_yok"):
        raise ApiError(NO_OFFICE_ACCESS, 403, "OFIS_ERISIMI_YOK")
    if extension.get("guncelleme_gerekli"):
        raise ApiError(UPDATE_FOR_OFFICES, 403, "SURUM_ESKI")
    # A request may have waited for the locks past its token's lifetime; a password change ends it as well.
    if locked and (core.clock.now().timestamp() >= claims["exp"]
                   or not hmac.compare_digest(users.fingerprint(user["sifre_hash"]), claims["sifre_izi"])):
        raise ApiError("Studio oturumu kapatılmış veya süresi dolmuş.", 401, "OTURUM_GECERSIZ")
    if not extension["aktif"]:
        raise ApiError(NO_LICENSE, 403, "AJAN_YETKISI_YOK")
    return AgentIdentity(user=user, claims=claims, entitlement=extension, office_id=extension["ofis_id"])
