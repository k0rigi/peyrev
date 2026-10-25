"""Lisans yanıtındaki `ajan` uzantısı ve ajan belirtecinin bitişi (sözleşme; ajan modülü gerçek hesabı yapar).

license_extension(core, cur, user, session_id) -> (uzantı sözlüğü, belirteç bitişi | None)
  Uzantı Studio'nun beklediği biçimdedir: {"surum": 1, "aktif": bool, "firma_id": alan_id, "kullanici_id": id,
  "onayci_id": ..., "oturum_id": session_id, "bitis": "YYYY-MM-DD" | None, "yetkiler": [...], "ofis_id": ..., "ofis_adi": ...,
  ["ofisler": [{"id","ad","yetkili"}], "ofis_kurabilir": true]}. Aktif değilse belirteç verilmez (None).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any


def license_extension(core, cur, user: dict[str, Any], session_id: str) -> tuple[dict[str, Any], datetime | None]:
    inactive = {"surum": 1, "aktif": False, "firma_id": int(user["alan_id"]), "kullanici_id": int(user["id"]),
                "onayci_id": None, "oturum_id": session_id, "bitis": None, "yetkiler": [], "ofis_id": None,
                "ofis_adi": None}
    return inactive, None
