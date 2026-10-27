"""Panelden eğitim videoları: liste, ekleme, değiştirme ve silme. Her değişiklik yonetim_olaylari'na yazılır."""

from __future__ import annotations

from typing import Any

from .. import egitim
from ..common import ApiError
from . import events
from .service import require_admin

MISSING = ApiError("Video bulunamadı.", 404, "BULUNAMADI")


def listing(core, admin_id: int) -> list[dict[str, Any]]:
    with core.db.transaction() as cur:
        require_admin(cur, admin_id)
        cur.execute("SELECT * FROM egitim_videolari ORDER BY sira, id DESC")
        return [egitim.view(row, admin=True) for row in cur.fetchall()]


def create(core, admin_id: int, data: dict[str, Any]) -> dict[str, Any]:
    video = egitim.video_id(data.get("baglanti"))
    values = egitim.fields(data.get("baslik"), data.get("aciklama"), data.get("kategori"), data.get("sira"),
                           data.get("yayinda", True))
    with core.db.transaction() as cur:
        require_admin(cur, admin_id)
        moment = core.clock.now()
        cur.execute("INSERT INTO egitim_videolari (baslik, aciklama, kategori, video_kimligi, sira, yayinda, olusturma, "
                    "guncelleme) VALUES (%(baslik)s, %(aciklama)s, %(kategori)s, %(video)s, %(sira)s, %(yayinda)s, "
                    "%(an)s, %(an)s) RETURNING *", {**values, "video": video, "an": moment})
        row = cur.fetchone()
        events.record(core, cur, admin_id, "egitim_eklendi", f"egitim:{row['id']}", baslik=row["baslik"],
                      video=video, yayinda=row["yayinda"])
    return egitim.view(row, admin=True)


def update(core, admin_id: int, video_id: int, data: dict[str, Any]) -> dict[str, Any]:
    video = egitim.video_id(data.get("baglanti"))
    values = egitim.fields(data.get("baslik"), data.get("aciklama"), data.get("kategori"), data.get("sira"),
                           data.get("yayinda", True))
    with core.db.transaction() as cur:
        require_admin(cur, admin_id)
        cur.execute("SELECT * FROM egitim_videolari WHERE id = %s FOR UPDATE", (video_id,))
        before = cur.fetchone()
        if before is None:
            raise MISSING
        cur.execute("UPDATE egitim_videolari SET baslik = %(baslik)s, aciklama = %(aciklama)s, kategori = %(kategori)s, "
                    "video_kimligi = %(video)s, sira = %(sira)s, yayinda = %(yayinda)s, guncelleme = %(an)s "
                    "WHERE id = %(id)s RETURNING *", {**values, "video": video, "an": core.clock.now(), "id": video_id})
        row = cur.fetchone()
        changed = [key for key, column in (("baslik", "baslik"), ("aciklama", "aciklama"), ("kategori", "kategori"),
                                           ("video", "video_kimligi"), ("sira", "sira"), ("yayinda", "yayinda"))
                   if before[column] != row[column]]
        if changed:
            events.record(core, cur, admin_id, "egitim_degisti", f"egitim:{video_id}", alanlar=changed)
    return egitim.view(row, admin=True)


def delete(core, admin_id: int, video_id: int) -> dict[str, Any]:
    with core.db.transaction() as cur:
        require_admin(cur, admin_id)
        cur.execute("DELETE FROM egitim_videolari WHERE id = %s RETURNING baslik, video_kimligi", (video_id,))
        row = cur.fetchone()
        if row is None:
            raise MISSING
        events.record(core, cur, admin_id, "egitim_silindi", f"egitim:{video_id}", baslik=row["baslik"],
                      video=row["video_kimligi"])
    return {"id": video_id, "silindi": True}
