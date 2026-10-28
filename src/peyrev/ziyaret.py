"""Çerezsiz ziyaret sayımı: POST /api/ziyaret (site.js her sayfa açılışında bir kez gönderir).

Gün (Türkiye saati), sayfa yolu ve yönlendiren sitenin alan adı başına yalnız bir sayı tutulur; IP adresi, tarayıcı
kimliği ya da çerez yoktur. İstek aynı kökenden gelmelidir (Sec-Fetch-Site ve Origin/Referer, panelin kuralı); bilinen
tarayıcı olmayan istemciler (bot, tarayıcısız araçlar) sayılmaz. Yol sitenin sayfa biçimindedir (/, /egitim/, /yasal/kvkk/);
günde en çok DAILY_ROWS farklı satır açılır, sahte yollarla tablo şişirilemez. KEEP günden eski günler silinir.
Yanıt her durumda 204'tür: sayılmayan istek de hata görmez.
"""

from __future__ import annotations

import re
from datetime import timedelta

from flask import Blueprint, Response, request

from .common import LOG, parse_json

PATH = re.compile(r"/(?:[a-z0-9-]{1,40}/){0,3}")
HOST = re.compile(r"(?=.{3,100}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+")
BOTS = re.compile(r"bot|crawl|spider|slurp|preview|headless|lighthouse|python|curl|wget|httpclient|java/|go-http|"
                  r"facebookexternalhit|monitor|scan", re.IGNORECASE)
OWN = ("peyrev.com", "www.peyrev.com")
DAILY_ROWS = 2000
KEEP = timedelta(days=400)
LIMIT = 1024


def _same_origin(core) -> bool:
    from .admin.auth import same_origin  # the panel's rule; imported late, admin imports the application modules

    return same_origin(core)


def source(value: object) -> str:
    """Yönlendiren sitenin alan adı (www. olmadan); boş, peyrev.com ya da geçersizse ''."""
    if not isinstance(value, str):
        return ""
    host = value.strip().lower().removeprefix("www.")
    return "" if host in OWN or not HOST.fullmatch(host) else host


def count(core, path: str, referrer: str) -> None:
    today = core.clock.today_tr()
    with core.db.transaction() as cur:
        cur.execute("UPDATE ziyaret_sayilari SET sayi = sayi + 1 WHERE gun = %s AND yol = %s AND kaynak = %s",
                    (today, path, referrer))
        if cur.rowcount:
            return
        cur.execute("INSERT INTO ziyaret_sayilari (gun, yol, kaynak) SELECT %(gun)s, %(yol)s, %(kaynak)s "
                    "WHERE (SELECT count(*) FROM ziyaret_sayilari WHERE gun = %(gun)s) < %(sinir)s "
                    "ON CONFLICT (gun, yol, kaynak) DO UPDATE SET sayi = ziyaret_sayilari.sayi + 1",
                    {"gun": today, "yol": path, "kaynak": referrer, "sinir": DAILY_ROWS})
        cur.execute("DELETE FROM ziyaret_sayilari WHERE gun < %s", (today - KEEP,))


def create_blueprint(core) -> Blueprint:
    blueprint = Blueprint("ziyaret", __name__)

    @blueprint.post("/api/ziyaret")
    def visit():
        answer = Response(status=204)
        raw = request.get_data(cache=False)
        agent = request.headers.get("User-Agent") or ""
        if len(raw) > LIMIT or not agent.startswith("Mozilla/") or BOTS.search(agent) or not _same_origin(core):
            return answer
        try:
            body = parse_json(raw)
        except (ValueError, TypeError, UnicodeError, RecursionError):
            return answer
        if not isinstance(body, dict) or not isinstance(body.get("yol"), str) or not PATH.fullmatch(body["yol"]):
            return answer
        try:
            count(core, body["yol"], source(body.get("kaynak")))
        except Exception:  # noqa: BLE001 - a count never fails the page
            LOG.exception("Ziyaret sayılamadı.")
        return answer

    return blueprint
