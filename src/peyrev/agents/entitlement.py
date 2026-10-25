"""Ajan yetkisi: lisans yanıtındaki `ajan` uzantısı ve ajan uçlarının kimlik doğrulaması.

Sözleşme (imzalar sabittir; ajan modülü gövdeyi gerçek kurallarla doldurur, diğer modüller yalnız bunları çağırır):

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

Aşağıdaki gövde geçicidir (ofis yok, yalnız modül hakları); ajan modülü ofis kurallarıyla değiştirir.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from .. import tokens, users
from ..clock import TR
from ..common import ApiError

OFFICE_ID = re.compile(r"^[0-9a-f]{32}$")


@dataclass
class AgentIdentity:
    user: dict[str, Any]
    claims: dict[str, Any]
    entitlement: dict[str, Any]
    office_id: str | None


def _row_state(cur, user: dict[str, Any], module: str, today: date) -> tuple[bool, date | None]:
    # Ajan yetkisinde yönetici ayrıcalığı yoktur: iki modülün de satırı gerekir.
    cur.execute("SELECT bitis FROM kullanici_modulleri WHERE kullanici_id = %s AND modul = %s", (user["id"], module))
    row = cur.fetchone()
    if row is None:
        return False, None
    return row["bitis"] is None or row["bitis"] >= today, row["bitis"]


def license_extension(core, cur, user: dict[str, Any], session_id: str, *, office: str | None = None,
                      lock: bool = False) -> tuple[dict[str, Any], datetime | None]:
    result: dict[str, Any] = {"surum": 1, "aktif": False, "firma_id": int(user["alan_id"]), "kullanici_id": int(user["id"]),
                              "onayci_id": None, "oturum_id": session_id, "bitis": None, "yetkiler": [], "ofis_id": None,
                              "ofis_adi": None}
    if user.get("durum") != "aktif":
        return result, None
    today = core.clock.today_tr()
    rpa_active, rpa_end = _row_state(cur, user, users.RPA, today)
    agents_active, agents_end = _row_state(cur, user, users.AGENTS, today)
    ends = [end for end in (rpa_end, agents_end) if end is not None]
    end = min(ends) if ends else None
    result["bitis"] = end.isoformat() if end else None
    if not (rpa_active and agents_active):
        return result, None
    result.update(aktif=True, onayci_id=int(user["id"]), yetkiler=["agents.use"], ofis_kurabilir=True)
    limit = datetime.combine(end + timedelta(days=1), datetime.min.time(), TR) if end else None
    return result, limit


def authenticate(core, cur, authorization: str | None, office: str | None = None, *, lock: bool = False) -> AgentIdentity:
    user, claims = tokens.verify(core, cur, "ajan", authorization)
    if office is not None and not OFFICE_ID.match(office):
        raise ApiError("Seçili ofis kimliği geçersiz.", 400, "GECERSIZ_GIRDI")
    extension, _limit = license_extension(core, cur, user, claims["oturum"], office=office, lock=lock)
    if not extension["aktif"]:
        raise ApiError("Hesabınız için geçerli Peyrev ve ajan lisansı gerekli.", 403, "AJAN_YETKISI_YOK")
    return AgentIdentity(user=user, claims=claims, entitlement=extension, office_id=extension.get("ofis_id"))
