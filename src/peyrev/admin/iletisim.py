"""Panelden iletişim mesajları ve ziyaret özeti.

Mesajlar en yeniden eskiye listelenir, okundu işaretlenir ve silinir; silme yonetim_olaylari'na yazılır (mesajın
içeriği yazılmaz). Ziyaret özeti son GUNLER gününün günlük toplamını, en çok açılan sayfaları ve yönlendiren siteleri
verir; ziyaretler kişiyi tanımlamaz.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from .. import iletisim
from ..accounts import rules
from ..common import ApiError
from . import events
from .service import require_admin

MISSING = ApiError("Mesaj bulunamadı.", 404, "BULUNAMADI")
LIST_LIMIT = 200
DAYS = (7, 30, 90)
TOP = 15


def _view(row: dict[str, Any]) -> dict[str, Any]:
    return {"id": row["id"], "ad": row["ad"], "eposta": row["eposta"], "konu": row["konu"],
            "konu_adi": iletisim.TOPICS[row["konu"]], "mesaj": row["mesaj"], "ip": row["ip"], "okundu": row["okundu"],
            "zaman": rules.stamp(row["olusturma"])}


def unread(cur) -> int:
    cur.execute("SELECT count(*) AS n FROM iletisim_mesajlari WHERE NOT okundu")
    return cur.fetchone()["n"]


def listing(core, admin_id: int) -> dict[str, Any]:
    with core.db.transaction() as cur:
        require_admin(cur, admin_id)
        cur.execute("SELECT * FROM iletisim_mesajlari ORDER BY olusturma DESC, id DESC LIMIT %s", (LIST_LIMIT,))
        messages = [_view(row) for row in cur.fetchall()]
        return {"mesajlar": messages, "okunmamis": unread(cur), "konular": iletisim.TOPICS}


def mark(core, admin_id: int, message_id: int, read: Any) -> dict[str, Any]:
    if type(read) is not bool:
        raise ApiError("Okundu seçimi geçersiz.")
    with core.db.transaction() as cur:
        require_admin(cur, admin_id)
        cur.execute("UPDATE iletisim_mesajlari SET okundu = %s WHERE id = %s RETURNING *", (read, message_id))
        row = cur.fetchone()
        if row is None:
            raise MISSING
        return {"mesaj": _view(row), "okunmamis": unread(cur)}


def delete(core, admin_id: int, message_id: int) -> dict[str, Any]:
    with core.db.transaction() as cur:
        require_admin(cur, admin_id)
        cur.execute("DELETE FROM iletisim_mesajlari WHERE id = %s RETURNING konu, olusturma", (message_id,))
        row = cur.fetchone()
        if row is None:
            raise MISSING
        events.record(core, cur, admin_id, "iletisim_silindi", f"iletisim:{message_id}", konu=row["konu"],
                      tarih=rules.stamp(row["olusturma"]))
        return {"id": message_id, "silindi": True, "okunmamis": unread(cur)}


def visits(core, admin_id: int, days: str | None) -> dict[str, Any]:
    if days in (None, ""):
        span = 30
    elif days in {str(value) for value in DAYS}:
        span = int(days)
    else:
        raise ApiError("Gün sayısı 7, 30 ya da 90 olmalıdır.")
    today = core.clock.today_tr()
    since = today - timedelta(days=span - 1)
    with core.db.transaction() as cur:
        require_admin(cur, admin_id)
        cur.execute("SELECT gun, sum(sayi)::int AS sayi FROM ziyaret_sayilari WHERE gun >= %s GROUP BY gun", (since,))
        by_day = {row["gun"]: row["sayi"] for row in cur.fetchall()}
        cur.execute("SELECT yol, sum(sayi)::int AS sayi FROM ziyaret_sayilari WHERE gun >= %s GROUP BY yol "
                    "ORDER BY sayi DESC, yol LIMIT %s", (since, TOP))
        pages = [dict(row) for row in cur.fetchall()]
        cur.execute("SELECT kaynak, sum(sayi)::int AS sayi FROM ziyaret_sayilari WHERE gun >= %s AND kaynak <> '' "
                    "GROUP BY kaynak ORDER BY sayi DESC, kaynak LIMIT %s", (since, TOP))
        sources = [dict(row) for row in cur.fetchall()]
    daily = [{"gun": (since + timedelta(days=offset)).isoformat(), "sayi": by_day.get(since + timedelta(days=offset), 0)}
             for offset in range(span)]
    return {"gun_sayisi": span, "toplam": sum(item["sayi"] for item in daily), "bugun": daily[-1]["sayi"],
            "gunluk": daily, "sayfalar": pages, "kaynaklar": sources}
