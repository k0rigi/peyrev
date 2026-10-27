"""Ücretsiz eğitim videoları: herkese açık liste (GET /api/egitim) ve YouTube bağlantısının okunması.

Videolar YouTube'da durur; peyrev.com yalnız video kimliğini saklar ve /egitim/ sayfası onu youtube-nocookie.com
üzerinden, ziyaretçi oynat'a basınca yükler. Panelden ekleme, değiştirme ve silme admin/egitim.py'dedir.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs, urlsplit

from flask import Blueprint

from .accounts import rules
from .common import ApiError, ok

VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be", "www.youtube-nocookie.com",
         "youtube-nocookie.com"}
PATHS = re.compile(r"/(?:embed|shorts|live|v)/([A-Za-z0-9_-]{11})(?:[/?#].*)?")
INVALID_LINK = "YouTube video bağlantısı okunamadı. Ör. https://www.youtube.com/watch?v=… ya da https://youtu.be/… yapıştırın."
DEFAULT_CATEGORY = "Genel"


def video_id(link: Any) -> str:
    """YouTube bağlantısından (watch, youtu.be, shorts, embed, live) ya da çıplak kimlikten 11 karakterlik kimlik."""
    if not isinstance(link, str) or not link.strip() or len(link) > 500:
        raise ApiError(INVALID_LINK)
    link = link.strip()
    if VIDEO_ID.fullmatch(link):
        return link
    parts = urlsplit(link if "://" in link else "https://" + link)
    if parts.scheme not in ("http", "https") or (parts.hostname or "").lower() not in HOSTS:
        raise ApiError(INVALID_LINK)
    host = parts.hostname.lower()
    if host == "youtu.be":
        candidate = parts.path.strip("/").split("/")[0]
    elif parts.path == "/watch":
        candidate = (parse_qs(parts.query).get("v") or [""])[0]
    else:
        match = PATHS.fullmatch(parts.path)
        candidate = match.group(1) if match else ""
    if not VIDEO_ID.fullmatch(candidate):
        raise ApiError(INVALID_LINK)
    return candidate


def fields(baslik: Any, aciklama: Any, kategori: Any, sira: Any, yayinda: Any) -> dict[str, Any]:
    """Panelden gelen alanlar; kurallar tablonun kısıtlarıyla aynıdır."""
    title = rules.text(baslik, "Başlık", 3, 150)
    if aciklama in (None, ""):
        description = ""
    else:
        if not isinstance(aciklama, str) or len(aciklama.strip()) > 1000:
            raise ApiError("Açıklama en çok 1000 karakter olabilir.")
        description = "\n".join(" ".join(line.split()) for line in aciklama.strip().splitlines()).strip()
        if any(ord(char) < 32 and char != "\n" for char in description) or "<" in description or ">" in description:
            raise ApiError("Açıklama geçersiz karakter içeriyor.")
    category = DEFAULT_CATEGORY if kategori in (None, "") else rules.text(kategori, "Kategori", 1, 60)
    if sira in (None, ""):
        order = 0
    elif type(sira) is int and 0 <= sira <= 9999:
        order = sira
    else:
        raise ApiError("Sıra 0-9999 arasında bir sayı olmalıdır.")
    if type(yayinda) is not bool:
        raise ApiError("Yayın seçimi geçersiz.")
    return {"baslik": title, "aciklama": description, "kategori": category, "sira": order, "yayinda": yayinda}


def view(row: dict[str, Any], *, admin: bool = False) -> dict[str, Any]:
    item = {"id": row["id"], "baslik": row["baslik"], "aciklama": row["aciklama"], "kategori": row["kategori"],
            "video": row["video_kimligi"], "tarih": rules.stamp(row["olusturma"])[:10]}
    if admin:
        item.update(sira=row["sira"], yayinda=row["yayinda"], guncelleme=rules.stamp(row["guncelleme"]))
    return item


def published(core) -> list[dict[str, Any]]:
    with core.db.transaction() as cur:
        cur.execute("SELECT * FROM egitim_videolari WHERE yayinda ORDER BY sira, id DESC")
        return [view(row) for row in cur.fetchall()]


def create_blueprint(core) -> Blueprint:
    blueprint = Blueprint("egitim", __name__)

    @blueprint.get("/api/egitim")
    def listing():
        videos = published(core)
        categories = list(dict.fromkeys(item["kategori"] for item in videos))
        return ok(videolar=videos, kategoriler=categories)

    return blueprint
