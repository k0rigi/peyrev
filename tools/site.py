"""peyrev.com statik sayfalarının ortak parçaları: üst menü, alt bilgi, ziyaret sayımı ve stil/betik sürüm damgası.

Her sayfanın <header class="site-header"> ve <footer class="site-footer"> bloğu buradaki şablonla değiştirilir (sayfanın
kendi adresi menüde aria-current alır); her sayfa çerezsiz ziyaret sayımını (assets/ziyaret.js) yükler; /assets/*.css ve *.js adresleri içerik özetini (?v=) taşır, böylece tarayıcı
7 gün sakladığı eski kopyayı bırakır. Sayfa eklediğinizde ya da stil/betik değiştirdiğinizde çalıştırın:

    python tools/site.py

tests/test_site_sayfalar.py ortak parçaların ve damgaların güncel olduğunu denetler.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
EXTRA_PAGES = [ROOT / "deploy" / "indir" / "index.html"]  # stamped only: it has its own small header

NAV = [("/#ozellikler", "Özellikler"), ("/cozumler/", "Çözümler"), ("/sablonlar/", "Şablonlar"), ("/egitim/", "Eğitim"),
       ("/topluluk/", "Topluluk"), ("/#paketler", "Fiyatlar")]
FOOTER = [
    ("Ürün", [("/#ozellikler", "Özellikler"), ("/#ajanlar", "Ajanlar"), ("/cozumler/", "Çözümler"),
              ("/sablonlar/", "Şablonlar"), ("/#paketler", "Fiyatlar"), ("/yenilikler/", "Yenilikler")]),
    ("Kaynaklar", [("/egitim/", "Eğitim videoları"), ("/topluluk/", "Topluluk"), ("/#indir", "Peyrev'i nasıl alırım?"),
                   ("/#sss", "Sık sorulanlar")]),
    ("Hesap", [("/hesabim/", "Hesabım"), ("/#basvuru", "Hesap başvurusu"), ("/iletisim/", "İletişim")]),
    ("Yasal", [("/yasal/kvkk/", "KVKK aydınlatma metni"), ("/yasal/gizlilik/", "Gizlilik ve çerezler"),
               ("/yasal/mesafeli-satis/", "Mesafeli satış sözleşmesi"), ("/yasal/iade/", "İade ve iptal")]),
]


def page_path(page: Path) -> str:
    relative = page.relative_to(WEB).parent.as_posix()
    return "/" if relative == "." else f"/{relative}/"


def header(path: str) -> str:
    def current(href: str) -> str:
        section = href.split("#")[0]
        return ' aria-current="page"' if section not in ("", "/") and path.startswith(section) else ""

    links = "\n".join(f'      <a href="{href}"{current(href)}>{label}</a>' for href, label in NAV)
    account = ' aria-current="page"' if path == "/hesabim/" else ""
    return f"""<header class="site-header">
  <div class="wrap header-row">
    <a class="brand" href="/">
      <img src="/assets/peyrev-mark-192.png" alt="" width="34" height="34">
      <span>Peyrev</span>
    </a>
    <nav class="site-nav" aria-label="Sayfalar">
{links}
    </nav>
    <div class="header-actions">
      <a class="button button-quiet button-small" href="/hesabim/"{account}>Hesabım</a>
      <a class="button button-gold button-small" href="/#basvuru">Başvuru</a>
    </div>
  </div>
</header>"""


def footer() -> str:
    columns = "\n".join(
        f'    <nav aria-label="{title}">\n      <p>{title}</p>\n'
        + "\n".join(f'      <a href="{href}">{label}</a>' for href, label in links) + "\n    </nav>"
        for title, links in FOOTER)
    return f"""<footer class="site-footer">
  <div class="wrap footer-grid">
    <div class="footer-brand">
      <span class="brand brand-small"><img src="/assets/peyrev-mark-32.png" alt="" width="22" height="22">Peyrev</span>
      <p>Windows ve macOS için masaüstü akış stüdyosu.</p>
      <p><a href="mailto:info@peyrev.com">info@peyrev.com</a></p>
    </div>
{columns}
  </div>
  <div class="wrap footer-note">© 2026 Peyrev · Fiyatlara KDV dahil değildir.</div>
</footer>"""


VISIT = '<script src="/assets/ziyaret.js" defer></script>'
ASSET = re.compile(r'(/assets/([\w.-]+\.(?:css|js)))(?:\?v=[0-9a-f]+)?"')


def stamp(name: str) -> str:
    return hashlib.sha256((WEB / "assets" / name).read_bytes()).hexdigest()[:10]


def pages() -> list[Path]:
    return sorted(page for page in WEB.rglob("*.html"))


def render(page: Path) -> str:
    text = page.read_text(encoding="utf-8")
    if page.is_relative_to(WEB):
        path = page_path(page)
        text = re.sub(r'<header class="site-header">.*?</header>', lambda _m: header(path), text, count=1, flags=re.DOTALL)
        text = re.sub(r'<footer class="site-footer">.*?</footer>', lambda _m: footer(), text, count=1, flags=re.DOTALL)
        if '/assets/ziyaret.js' not in text:
            text = text.replace("</head>", VISIT + "\n</head>", 1)
    return ASSET.sub(lambda match: f'{match.group(1)}?v={stamp(match.group(2))}"', text)


def main() -> None:
    for page in pages() + EXTRA_PAGES:
        updated = render(page)
        if updated != page.read_text(encoding="utf-8"):
            page.write_text(updated, encoding="utf-8")
            print("güncellendi:", page.relative_to(ROOT))


if __name__ == "__main__":
    main()
