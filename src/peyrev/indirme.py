"""Kişiye özel indirme bağlantıları: peyrev.com/api/indir/<anahtar> (ödeme onaylanınca e-postayla gönderilir).

Kurulum dosyaları /indir/releases altında kalır, çünkü kurulu uygulamalar güncellemeyi imzalı guncelleme.manifest'teki
bu adreslerden indirir; Peyrev'i açmak için yine lisanslı hesap gerekir. Sitede ve /indir/ sayfasında dosya bağlantısı
yoktur: kişi kendi bağlantısıyla gelir, Windows ya da macOS'u seçer ve güncel kurulum dosyasına yönlendirilir.

Bağlantı DAYS gün geçerlidir; hesap başına bir tane açık kalır (yenisi eskisini iptal eder); anahtarın yalnız SHA-256
özeti saklanır; pasif hesabın bağlantısı çalışmaz; her indirme sayılır. Güncel dosya adresleri guncelleme.manifest'ten
okunur (CACHE_SECONDS saniye bellekte tutulur) ve yalnız /indir/releases/ altındaki https adresleri kabul edilir.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import re
import secrets
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from flask import Blueprint, Response, redirect
from psycopg import Cursor

from .accounts import rules
from .common import LOG

DAYS = 30
TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")
PLATFORMS = {"windows": ("windows-x64", "Windows", "Windows 10 ve 11 (64 bit)"),
             "mac": ("macos-arm64", "macOS", "Apple işlemcili Mac (M1 ve sonrası)")}
CACHE_SECONDS = 300
MANIFEST_LIMIT = 128 * 1024
# The site's style sheet with the same content stamp as the static pages (tools/site.py).
_SITE_CSS = Path(__file__).resolve().parents[2] / "web" / "assets" / "site.css"
STYLE = "/assets/site.css" + (f"?v={hashlib.sha256(_SITE_CSS.read_bytes()).hexdigest()[:10]}" if _SITE_CSS.is_file() else "")
PAGE_POLICY = ("default-src 'none'; style-src 'self'; img-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
               "form-action 'none'")


def tr_day(moment: datetime) -> str:
    """Türkiye saatiyle gün: 27.11.2026."""
    stamp = rules.stamp(moment)
    return f"{stamp[8:10]}.{stamp[5:7]}.{stamp[:4]}"


def digest(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def address(core, token: str) -> str:
    return f"{core.settings.public_base.rstrip('/')}/api/indir/{token}"


def issue(core, cur: Cursor, user_id: int, admin_id: int | None) -> tuple[str, datetime]:
    """Yeni bağlantı: (anahtar, son geçerlilik). Hesabın açık bağlantıları iptal olur."""
    moment = core.clock.now()
    cur.execute("UPDATE indirme_baglantilari SET iptal = %s WHERE kullanici_id = %s AND iptal IS NULL", (moment, user_id))
    token = secrets.token_urlsafe(32)
    expires = moment + timedelta(days=DAYS)
    cur.execute("INSERT INTO indirme_baglantilari (ozet, kullanici_id, olusturan_id, olusturma, gecerlilik) "
                "VALUES (%s, %s, %s, %s, %s)", (digest(token), user_id, admin_id, moment, expires))
    return token, expires


def status(cur: Cursor, user_id: int, moment: datetime) -> dict[str, Any] | None:
    """Hesabın son bağlantısı (anahtarsız): geçerli mi, ne zamana kadar, kaç kez indirildi."""
    cur.execute("SELECT * FROM indirme_baglantilari WHERE kullanici_id = %s ORDER BY olusturma DESC LIMIT 1", (user_id,))
    row = cur.fetchone()
    if row is None:
        return None
    return {"gecerli": row["iptal"] is None and row["gecerlilik"] > moment, "olusturma": rules.stamp(row["olusturma"]),
            "gecerlilik": rules.stamp(row["gecerlilik"]), "kullanim": row["kullanim"],
            "son_kullanim": rules.stamp(row["son_kullanim"]) if row["son_kullanim"] else None}


def _fetch(core) -> bytes:
    request = Request(core.settings.public_base.rstrip("/") + "/indir/guncelleme.manifest",
                      headers={"User-Agent": "Peyrev-sunucu"})
    with urlopen(request, timeout=10) as response:  # fixed https address of this site
        return response.read(MANIFEST_LIMIT + 1)


def installers(core) -> dict[str, str]:
    """{"surum": "1.2.1", "windows": url, "mac": url} — güncel kurulum dosyaları."""
    cached = core.extras.get("_indirme_onbellek")
    if cached and time.monotonic() - cached[0] < CACHE_SECONDS:
        return cached[1]
    raw = core.extras.get("indirme_bildirimi", _fetch)(core)
    if len(raw) > MANIFEST_LIMIT:
        raise ValueError("bildirim çok büyük")
    payload = json.loads(base64.b64decode(json.loads(raw)["payload"], validate=True))
    version = payload["version"]
    if not isinstance(version, str) or not re.fullmatch(r"\d{1,4}\.\d{1,4}\.\d{1,4}", version):
        raise ValueError("sürüm geçersiz")
    prefix = core.settings.public_base.rstrip("/") + "/indir/releases/"
    found = {"surum": version}
    for key, (asset, _label, _note) in PLATFORMS.items():
        url = payload["assets"][asset]["url"]
        if not isinstance(url, str) or not url.startswith(prefix) or not re.fullmatch(r"[A-Za-z0-9._/:-]{1,300}", url):
            raise ValueError("dosya adresi beklenmiyor")
        found[key] = url
    core.extras["_indirme_onbellek"] = (time.monotonic(), found)
    return found


def _valid(core, cur: Cursor, token: str) -> dict[str, Any] | None:
    if not TOKEN.fullmatch(token):
        return None
    cur.execute("SELECT b.*, k.durum FROM indirme_baglantilari b JOIN kullanicilar k ON k.id = b.kullanici_id "
                "WHERE b.ozet = %s", (digest(token),))
    row = cur.fetchone()
    if row is None or row["iptal"] is not None or row["gecerlilik"] <= core.clock.now() or row["durum"] != "aktif":
        return None
    return row


def _page(title: str, body: str, status: int) -> Response:
    document = f"""<!doctype html>
<html lang="tr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<meta name="color-scheme" content="light dark">
<title>{html.escape(title)} · Peyrev</title>
<link rel="icon" href="/assets/peyrev-mark-32.png">
<link rel="stylesheet" href="{STYLE}">
</head>
<body>
<header class="site-header"><div class="wrap header-row"><a class="brand" href="/">
<img src="/assets/peyrev-mark-192.png" alt="" width="34" height="34"><span>Peyrev</span></a></div></header>
<main class="band"><div class="wrap download-page">{body}</div></main>
</body>
</html>
"""
    response = Response(document, status=status, mimetype="text/html")
    response.headers["Content-Security-Policy"] = PAGE_POLICY
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    response.headers["X-Frame-Options"] = "DENY"
    return response


EXPIRED = ("<section class=\"card\"><h1>Bağlantı geçersiz</h1><p>Bu indirme bağlantısının süresi dolmuş ya da yerine yenisi "
           "gönderilmiş. Yeni bağlantı için <a href=\"mailto:info@peyrev.com\">info@peyrev.com</a> adresine yazın.</p>"
           "</section>")


def create_blueprint(core) -> Blueprint:
    blueprint = Blueprint("indirme", __name__)

    @blueprint.get("/api/indir/<token>")
    def landing(token: str):
        with core.db.transaction() as cur:
            row = _valid(core, cur, token)
        if row is None:
            return _page("Bağlantı geçersiz", EXPIRED, 404)
        try:
            version = installers(core)["surum"]
        except Exception as error:  # noqa: BLE001 - the page still offers the buttons; the redirect reports again
            LOG.warning("İndirme bildirimi okunamadı: %s", type(error).__name__)
            version = ""
        buttons = "".join(
            f'<a class="download-choice" href="/api/indir/{token}/{key}"><strong>{label}</strong>'
            f'<span>{html.escape(note)}</span></a>' for key, (_asset, label, note) in PLATFORMS.items())
        body = (f'<section class="card"><p class="kicker">Kişisel indirme bağlantınız</p><h1>Peyrev\'i indirin</h1>'
                f'<p>{"Güncel sürüm " + html.escape(version) + ". " if version else ""}Bilgisayarınızın sistemini seçin; '
                f'kurulum dosyası iner.</p><div class="download-choices">{buttons}</div>'
                f'<p class="aside">Bağlantı {tr_day(row["gecerlilik"])} tarihine kadar geçerlidir '
                f've size özeldir. Windows için Microsoft Edge WebView2 gerekir; Mac\'te pencereleri tanımak için Ekran '
                f'Kaydı ve Erişilebilirlik izinleri istenir. Kurulu Peyrev yeni sürümleri kendiliğinden indirir.</p>'
                f'</section>')
        return _page("Peyrev'i indirin", body, 200)

    @blueprint.get("/api/indir/<token>/<platform>")
    def download(token: str, platform: str):
        if platform not in PLATFORMS:
            return _page("Bağlantı geçersiz", EXPIRED, 404)
        with core.db.transaction() as cur:
            row = _valid(core, cur, token)
            if row is None:
                return _page("Bağlantı geçersiz", EXPIRED, 404)
            try:
                target = installers(core)[platform]
            except Exception as error:  # noqa: BLE001 - shown as a page, the reason goes to the log only
                LOG.warning("İndirme bildirimi okunamadı: %s", type(error).__name__)
                return _page("Şu an indirilemiyor", "<section class=\"card\"><h1>Şu an indirilemiyor</h1><p>Kurulum "
                             "dosyası şu an alınamadı. Birkaç dakika sonra yeniden deneyin.</p></section>", 503)
            cur.execute("UPDATE indirme_baglantilari SET kullanim = kullanim + 1, son_kullanim = %s WHERE ozet = %s",
                        (core.clock.now(), row["ozet"]))
        response = redirect(target, 302)
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    return blueprint
