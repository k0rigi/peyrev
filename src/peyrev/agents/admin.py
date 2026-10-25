"""Yönetim paneli için ajan ofisi işlemleri. Uç değildir: panel modülü yönetici oturumunu kendisi denetleyip çağırır.

user_offices(core, user_id) -> dict
  Hesabın bütün ofisleri (pasif dahil, Studio'daki sırayla) ve her ofisteki görev ve onay sayıları; iş metni yoktur.
set_office_status(core, admin_id, office_id, durum) -> dict
  Ofisi 'pasif' yapar (sahibi ve Studio o ofise artık erişemez; kayıtları kalır, sınırda sayılmaya devam eder) ya da
  yeniden 'aktif' yapar. Her değişiklik yonetim_olaylari'na yazılır; aynı duruma getirmek bir şey yazmaz.
"""

from __future__ import annotations

import json
from datetime import UTC

from .. import users
from ..common import ApiError
from .entitlement import MAX_OFFICES, OFFICE_ID


def _timestamp(value):
    return None if value is None else value.astimezone(UTC).isoformat()


def user_offices(core, user_id: int) -> dict:
    with core.db.transaction() as cur:
        if users.get(cur, user_id) is None:
            raise ApiError("Kullanıcı bulunamadı.", 404, "BULUNAMADI")
        cur.execute("SELECT id, ad, durum, olusturma, guncelleme FROM ajan_ofisleri WHERE sahip_id = %s "
                    "ORDER BY olusturma, id", (user_id,))
        offices = cur.fetchall()
        cur.execute("SELECT ofis_id, count(*) AS gorev, count(*) FILTER (WHERE durum IN ('queued', 'running', "
                    "'waiting_user')) AS acik_gorev FROM ajan_gorevleri WHERE sahip_id = %s AND durum <> 'deleted' "
                    "GROUP BY ofis_id", (user_id,))
        tasks = {row.pop("ofis_id"): row for row in cur.fetchall()}
        cur.execute("SELECT ofis_id, count(*) AS onay, count(*) FILTER (WHERE durum IN ('pending', 'approved') "
                    "AND gecerlilik > %s) AS bekleyen_onay FROM ajan_onaylari WHERE sahip_id = %s GROUP BY ofis_id",
                    (core.clock.now(), user_id))
        approvals = {row.pop("ofis_id"): row for row in cur.fetchall()}
    empty = {"gorev": 0, "acik_gorev": 0, "onay": 0, "bekleyen_onay": 0}
    return {"kullanici_id": user_id, "ofis_siniri": MAX_OFFICES,
            "ofisler": [{"id": office["id"], "ad": office["ad"], "durum": office["durum"],
                         "olusturma": _timestamp(office["olusturma"]), "guncelleme": _timestamp(office["guncelleme"]),
                         **empty, **tasks.get(office["id"], {}), **approvals.get(office["id"], {})}
                        for office in offices]}


def set_office_status(core, admin_id: int, office_id: str, durum: str) -> dict:
    if not isinstance(office_id, str) or not OFFICE_ID.fullmatch(office_id):
        raise ApiError("Ofis kimliği geçersiz.", 400, "GECERSIZ_GIRDI")
    if durum not in ("aktif", "pasif"):
        raise ApiError("Ofis durumu 'aktif' ya da 'pasif' olmalı.", 400, "GECERSIZ_GIRDI")
    with core.db.transaction() as cur:
        cur.execute("SELECT sahip_id FROM ajan_ofisleri WHERE id = %s", (office_id,))
        found = cur.fetchone()
        if found is None:
            raise ApiError("Ofis bulunamadı.", 404, "BULUNAMADI")
        # The owner's account row is the first lock, as for every change the owner makes in Studio.
        users.get(cur, found["sahip_id"], lock=True)
        cur.execute("SELECT id, sahip_id, ad, durum FROM ajan_ofisleri WHERE id = %s FOR UPDATE", (office_id,))
        office = cur.fetchone()
        if office is None:
            raise ApiError("Ofis bulunamadı.", 404, "BULUNAMADI")
        changed = office["durum"] != durum
        if changed:
            now = core.clock.now()
            cur.execute("UPDATE ajan_ofisleri SET durum = %s, guncelleme = %s WHERE id = %s", (durum, now, office_id))
            cur.execute("INSERT INTO yonetim_olaylari (yonetici_id, tur, hedef, ayrinti, zaman) "
                        "VALUES (%s, 'ajan_ofisi_durumu', %s, %s::jsonb, %s)",
                        (admin_id, "ajan_ofisi:" + office_id,
                         json.dumps({"kullanici_id": office["sahip_id"], "onceki": office["durum"], "durum": durum}),
                         now))
    return {"id": office["id"], "kullanici_id": office["sahip_id"], "ad": office["ad"], "durum": durum,
            "degisti": changed}
