"""Panelden topluluk moderasyonu: konuları ve yorumları görür (gizliler dahil, yazarın giriş adıyla), konuyu sabitler,
yoruma kapatır, gizler ya da siler; yorumu gizler ya da siler. Her işlem yonetim_olaylari'na yazılır (metin yazılmaz).
"""

from __future__ import annotations

from typing import Any

from ..common import ApiError
from ..topluluk import service as community
from . import events
from .service import require_admin

FLAGS = ("sabit", "kilitli", "gizli")


def listing(core, admin_id: int, *, kategori: Any, ara: Any, sayfa: int) -> dict[str, Any]:
    with core.db.transaction() as cur:
        require_admin(cur, admin_id)
        return community.listing(cur, kategori=kategori, ara=ara, sayfa=sayfa, admin=True)


def detail(core, admin_id: int, topic_id: int) -> dict[str, Any]:
    with core.db.transaction() as cur:
        admin = require_admin(cur, admin_id)
        row = community._topic(cur, topic_id, admin=True)
        return {"konu": community.topic_view(row, admin, full=True, admin=True),
                "yorumlar": community.comments(cur, topic_id, admin, admin=True)}


def set_flags(core, admin_id: int, topic_id: int, data: dict[str, Any]) -> dict[str, Any]:
    if not data or any(type(data[key]) is not bool for key in data):
        raise ApiError("Seçim geçersiz.")
    with core.db.transaction() as cur:
        require_admin(cur, admin_id)
        row = community._topic(cur, topic_id, admin=True, lock=True)
        changed = {key: value for key, value in data.items() if row[key] != value}
        if changed:
            cur.execute("UPDATE topluluk_konulari SET " + ", ".join(f"{key} = %({key})s" for key in changed)
                        + " WHERE id = %(id)s", {**changed, "id": topic_id})
            events.record(core, cur, admin_id, "topluluk_konu", f"topluluk:{topic_id}", **changed)
    return detail(core, admin_id, topic_id)


def delete_topic(core, admin_id: int, topic_id: int) -> dict[str, Any]:
    with core.db.transaction() as cur:
        require_admin(cur, admin_id)
        row = community._topic(cur, topic_id, admin=True, lock=True)
        cur.execute("DELETE FROM topluluk_konulari WHERE id = %s", (topic_id,))
        events.record(core, cur, admin_id, "topluluk_konu_silindi", f"topluluk:{topic_id}", baslik=row["baslik"])
    return {"id": topic_id, "silindi": True}


def hide_comment(core, admin_id: int, comment_id: int, hidden: Any) -> dict[str, Any]:
    if type(hidden) is not bool:
        raise ApiError("Seçim geçersiz.")
    with core.db.transaction() as cur:
        require_admin(cur, admin_id)
        cur.execute("SELECT konu_id, gizli FROM topluluk_yorumlari WHERE id = %s FOR UPDATE", (comment_id,))
        row = cur.fetchone()
        if row is None:
            raise ApiError("Yorum bulunamadı.", 404, "BULUNAMADI")
        if row["gizli"] != hidden:
            cur.execute("UPDATE topluluk_yorumlari SET gizli = %s WHERE id = %s", (hidden, comment_id))
            # The count members see follows the visible comments.
            cur.execute("UPDATE topluluk_konulari SET yanit_sayisi = greatest(yanit_sayisi + %s, 0) WHERE id = %s",
                        (-1 if hidden else 1, row["konu_id"]))
            events.record(core, cur, admin_id, "topluluk_yorum", f"topluluk:{row['konu_id']}", yorum=comment_id,
                          gizli=hidden)
    return detail(core, admin_id, row["konu_id"])


def delete_comment(core, admin_id: int, comment_id: int) -> dict[str, Any]:
    with core.db.transaction() as cur:
        require_admin(cur, admin_id)
        cur.execute("SELECT gizli FROM topluluk_yorumlari WHERE id = %s", (comment_id,))
        found = cur.fetchone()
        topic_id = community.delete_comment(cur, comment_id)
        if found and found["gizli"]:  # a hidden comment was already out of the count
            cur.execute("UPDATE topluluk_konulari SET yanit_sayisi = yanit_sayisi + 1 WHERE id = %s", (topic_id,))
        events.record(core, cur, admin_id, "topluluk_yorum_silindi", f"topluluk:{topic_id}", yorum=comment_id)
    return detail(core, admin_id, topic_id)
