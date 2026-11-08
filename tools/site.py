"""peyrev.com statik sayfalarının ortak parçaları: üst menü, alt bilgi, ziyaret sayımı ve stil/betik sürüm damgası.

Her sayfanın <header class="site-header"> ve <footer class="site-footer"> bloğu buradaki şablonla değiştirilir (sayfanın
kendi adresi menüde aria-current alır; sağ üstte dil seçici vardır); her sayfa çerezsiz ziyaret sayımını (assets/ziyaret.js) yükler; /assets/*.css ve *.js adresleri içerik özetini (?v=) taşır, böylece tarayıcı
7 gün sakladığı eski kopyayı bırakır. Sayfa eklediğinizde ya da stil/betik değiştirdiğinizde çalıştırın:

    python tools/site.py

Diller: Türkçe kök adrestedir; tanıtım sayfalarının (CEVRILEN) çevirileri web/<dil>/ altında aynı yoldadır (ör.
web/en/cozumler/index.html). Çeviri sayfalarında <html lang dir>, alternatif dil bağlantıları (hreflang), çevrilmiş
sayfalara giden iç bağlantıların dil öneki ve formların mesaj çevirisi (assets/ceviri.js) buradan yazılır; çevrilmemiş
sayfalara (eğitim, topluluk, hesap, yasal) giden bağlantılar Türkçe sayfaya gider ve "TR" ile işaretlenir.

tests/test_site_sayfalar.py ortak parçaların ve damgaların güncel olduğunu denetler.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
EXTRA_PAGES = [ROOT / "deploy" / "indir" / "index.html"]  # stamped only: it has its own small header

SITE = "https://peyrev.com"
# Dil kodu → (kendi dilindeki adı, yazı yönü). İlki varsayılandır ve kök adrestedir.
DILLER = {"tr": ("Türkçe", "ltr"), "en": ("English", "ltr"), "fr": ("Français", "ltr"), "es": ("Español", "ltr"),
          "ar": ("العربية", "rtl"), "fa": ("فارسی", "rtl")}
# Çevirisi olan sayfalar (Türkçe yolu); bunlara giden bağlantılar çeviri sayfasında aynı dile gider.
CEVRILEN = ("/", "/cozumler/", "/cozumler/muhasebe-finans/", "/cozumler/insan-kaynaklari/", "/cozumler/e-ticaret/",
            "/sablonlar/", "/iletisim/")

NAV = [("/#ozellikler", "ozellikler"), ("/cozumler/", "cozumler"), ("/sablonlar/", "sablonlar"), ("/egitim/", "egitim"),
       ("/topluluk/", "topluluk"), ("/#paketler", "paketler")]
FOOTER = [
    ("urun", [("/#ozellikler", "ozellikler"), ("/#ajanlar", "ajanlar"), ("/cozumler/", "cozumler"),
              ("/sablonlar/", "sablonlar"), ("/#paketler", "paketler"), ("/yenilikler/", "yenilikler")]),
    ("kaynaklar", [("/egitim/", "videolar"), ("/egitim/adimlar/", "rehber"), ("/egitim/deneme/", "deneme"),
                   ("/topluluk/", "topluluk"), ("/#indir", "nasil_alirim"), ("/#sss", "sss")]),
    ("hesap", [("/hesabim/", "hesabim"), ("/#basvuru", "basvuru_formu"), ("/iletisim/", "iletisim")]),
    ("yasal", [("/yasal/kvkk/", "kvkk"), ("/yasal/gizlilik/", "gizlilik"), ("/yasal/mesafeli-satis/", "mesafeli"),
               ("/yasal/iade/", "iade")]),
]
METIN = {
    "tr": {"ozellikler": "Özellikler", "cozumler": "Çözümler", "sablonlar": "Şablonlar", "egitim": "Eğitim",
           "topluluk": "Topluluk", "paketler": "Paketler", "ajanlar": "Ajanlar", "yenilikler": "Yenilikler",
           "videolar": "Eğitim videoları", "rehber": "Adım rehberi", "deneme": "Deneme akışları",
           "nasil_alirim": "Peyrev'i nasıl alırım?", "sss": "Sık sorulanlar", "hesabim": "Hesabım",
           "basvuru_formu": "Hesap başvurusu", "iletisim": "İletişim", "kvkk": "KVKK aydınlatma metni",
           "gizlilik": "Gizlilik ve çerezler", "mesafeli": "Mesafeli satış sözleşmesi", "iade": "İade ve iptal",
           "urun": "Ürün", "kaynaklar": "Kaynaklar", "hesap": "Hesap", "yasal": "Yasal", "sayfalar": "Sayfalar",
           "basvuru": "Başvuru", "dil": "Dil", "turkce": "Türkçe sayfa",
           "slogan": "Windows ve macOS için masaüstü akış stüdyosu.",
           "not": "© 2026 Peyrev"},
    "en": {"ozellikler": "Features", "cozumler": "Solutions", "sablonlar": "Templates", "egitim": "Training",
           "topluluk": "Community", "paketler": "Plans", "ajanlar": "Agents", "yenilikler": "What's new",
           "videolar": "Training videos", "rehber": "Step guide", "deneme": "Practice flows",
           "nasil_alirim": "How do I get Peyrev?", "sss": "FAQ", "hesabim": "My account",
           "basvuru_formu": "Account application", "iletisim": "Contact", "kvkk": "Personal data notice (KVKK)",
           "gizlilik": "Privacy and cookies", "mesafeli": "Distance sales agreement", "iade": "Refunds and cancellation",
           "urun": "Product", "kaynaklar": "Resources", "hesap": "Account", "yasal": "Legal", "sayfalar": "Pages",
           "basvuru": "Apply", "dil": "Language", "turkce": "Page in Turkish",
           "slogan": "Desktop flow studio for Windows and macOS.",
           "not": "© 2026 Peyrev"},
    "fr": {"ozellikler": "Fonctionnalités", "cozumler": "Solutions", "sablonlar": "Modèles", "egitim": "Formation",
           "topluluk": "Communauté", "paketler": "Formules", "ajanlar": "Agents", "yenilikler": "Nouveautés",
           "videolar": "Vidéos de formation", "rehber": "Guide des étapes", "deneme": "Flux d'exercice",
           "nasil_alirim": "Comment obtenir Peyrev ?", "sss": "Questions fréquentes", "hesabim": "Mon compte",
           "basvuru_formu": "Demande de compte", "iletisim": "Contact",
           "kvkk": "Information sur les données personnelles (KVKK)", "gizlilik": "Confidentialité et cookies",
           "mesafeli": "Contrat de vente à distance", "iade": "Remboursement et annulation",
           "urun": "Produit", "kaynaklar": "Ressources", "hesap": "Compte", "yasal": "Mentions légales",
           "sayfalar": "Pages", "basvuru": "Demande", "dil": "Langue", "turkce": "Page en turc",
           "slogan": "Studio de flux de bureau pour Windows et macOS.",
           "not": "© 2026 Peyrev"},
    "es": {"ozellikler": "Funciones", "cozumler": "Soluciones", "sablonlar": "Plantillas", "egitim": "Formación",
           "topluluk": "Comunidad", "paketler": "Paquetes", "ajanlar": "Agentes", "yenilikler": "Novedades",
           "videolar": "Vídeos de formación", "rehber": "Guía de pasos", "deneme": "Flujos de práctica",
           "nasil_alirim": "¿Cómo consigo Peyrev?", "sss": "Preguntas frecuentes", "hesabim": "Mi cuenta",
           "basvuru_formu": "Solicitud de cuenta", "iletisim": "Contacto",
           "kvkk": "Aviso de datos personales (KVKK)", "gizlilik": "Privacidad y cookies",
           "mesafeli": "Contrato de venta a distancia", "iade": "Reembolsos y cancelación",
           "urun": "Producto", "kaynaklar": "Recursos", "hesap": "Cuenta", "yasal": "Legal", "sayfalar": "Páginas",
           "basvuru": "Solicitar", "dil": "Idioma", "turkce": "Página en turco",
           "slogan": "Estudio de flujos de escritorio para Windows y macOS.",
           "not": "© 2026 Peyrev"},
    "ar": {"ozellikler": "الميزات", "cozumler": "الحلول", "sablonlar": "القوالب", "egitim": "التدريب",
           "topluluk": "المجتمع", "paketler": "الباقات", "ajanlar": "الوكلاء", "yenilikler": "الجديد",
           "videolar": "فيديوهات التدريب", "rehber": "دليل الخطوات", "deneme": "تدفقات للتجربة",
           "nasil_alirim": "كيف أحصل على Peyrev؟", "sss": "الأسئلة الشائعة", "hesabim": "حسابي",
           "basvuru_formu": "طلب حساب", "iletisim": "اتصل بنا", "kvkk": "إشعار البيانات الشخصية (KVKK)",
           "gizlilik": "الخصوصية وملفات تعريف الارتباط", "mesafeli": "عقد البيع عن بُعد",
           "iade": "الاسترداد والإلغاء", "urun": "المنتج", "kaynaklar": "الموارد", "hesap": "الحساب",
           "yasal": "قانوني", "sayfalar": "الصفحات", "basvuru": "قدّم طلبًا", "dil": "اللغة",
           "turkce": "صفحة باللغة التركية", "slogan": "استوديو تدفقات سطح المكتب لنظامي Windows وmacOS.",
           "not": "© 2026 Peyrev"},
    "fa": {"ozellikler": "ویژگی‌ها", "cozumler": "راهکارها", "sablonlar": "الگوها", "egitim": "آموزش",
           "topluluk": "انجمن", "paketler": "بسته‌ها", "ajanlar": "عامل‌ها", "yenilikler": "تازه‌ها",
           "videolar": "ویدیوهای آموزشی", "rehber": "راهنمای گام‌ها", "deneme": "جریان‌های تمرینی",
           "nasil_alirim": "چگونه Peyrev را تهیه کنم؟", "sss": "پرسش‌های متداول", "hesabim": "حساب من",
           "basvuru_formu": "درخواست حساب", "iletisim": "تماس", "kvkk": "اطلاعیه داده‌های شخصی (KVKK)",
           "gizlilik": "حریم خصوصی و کوکی‌ها", "mesafeli": "قرارداد فروش از راه دور", "iade": "بازپرداخت و لغو",
           "urun": "محصول", "kaynaklar": "منابع", "hesap": "حساب", "yasal": "حقوقی", "sayfalar": "صفحه‌ها",
           "basvuru": "درخواست", "dil": "زبان", "turkce": "صفحه به زبان ترکی",
           "slogan": "استودیوی جریان کار دسکتاپ برای Windows و macOS.",
           "not": "© 2026 Peyrev"},
}


def dil(path: str) -> tuple[str, str]:
    """Sayfa yolunun dili ve Türkçe karşılığı: /en/cozumler/ → ("en", "/cozumler/")."""
    ilk = path.strip("/").split("/", 1)[0]
    if ilk in DILLER and ilk != "tr":
        return ilk, path[len(ilk) + 1:]
    return "tr", path


def yerel(href: str, kod: str) -> str:
    """Türkçe iç bağlantının o dildeki adresi: çevrilmiş sayfaya dil öneki gelir, çevrilmemiş Türkçe kalır."""
    yol, isaret, capa = href.partition("#")
    if kod == "tr" or (yol or "/") not in CEVRILEN or not href.startswith("/"):
        return href
    return f"/{kod}{yol or '/'}{isaret}{capa}"


def page_path(page: Path) -> str:
    relative = page.relative_to(WEB).parent.as_posix()
    return "/" if relative == "." else f"/{relative}/"


def dil_secici(path: str) -> str:
    kod, temel = dil(path)
    secenekler = []
    for diger, (ad, _yon) in DILLER.items():
        hedef = yerel(temel, diger) if temel in CEVRILEN else ("/" if diger == "tr" else f"/{diger}/")
        if diger == "tr" and temel not in CEVRILEN:
            hedef = temel
        akim = ' aria-current="true"' if diger == kod else ""
        secenekler.append(f'          <li><a href="{hedef}" hreflang="{diger}" lang="{diger}"{akim}>{ad}</a></li>')
    return f"""      <details class="dil-secici">
        <summary aria-label="{METIN[kod]['dil']}: {DILLER[kod][0]}"><svg class="dil-ikon" viewBox="0 0 24 24" width="16" height="16" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c2.4 2.6 3.6 5.6 3.6 9s-1.2 6.4-3.6 9c-2.4-2.6-3.6-5.6-3.6-9S9.6 5.6 12 3z"/></svg><span class="dil-kodu">{kod.upper()}</span></summary>
        <ul>
{chr(10).join(secenekler)}
        </ul>
      </details>"""


def _etiket(href: str, anahtar: str, kod: str) -> str:
    """Bağlantının yazısı; çeviri sayfasından Türkçe sayfaya gidiyorsa "TR" işareti."""
    yazi = METIN[kod][anahtar]
    yol = href.partition("#")[0] or "/"
    if kod != "tr" and yol not in CEVRILEN:
        return f'{yazi} <small class="tr-isareti" title="{METIN[kod]["turkce"]}">TR</small>'
    return yazi


def header(path: str) -> str:
    kod, temel = dil(path)

    def current(href: str) -> str:
        section = href.split("#")[0]
        return ' aria-current="page"' if section not in ("", "/") and temel.startswith(section) else ""

    links = "\n".join(f'      <a href="{yerel(href, kod)}"{current(href)}>{_etiket(href, anahtar, kod)}</a>'
                      for href, anahtar in NAV)
    account = ' aria-current="page"' if temel == "/hesabim/" else ""
    ana = yerel("/", kod)
    return f"""<header class="site-header">
  <div class="wrap header-row">
    <a class="brand" href="{ana}">
      <img src="/assets/peyrev-mark-192.png" alt="" width="34" height="34">
      <span>Peyrev</span>
    </a>
    <nav class="site-nav" aria-label="{METIN[kod]['sayfalar']}">
{links}
    </nav>
    <div class="header-actions">
      <a class="button button-quiet button-small" href="/hesabim/"{account}>{METIN[kod]['hesabim']}</a>
      <a class="button button-gold button-small" href="{yerel('/#basvuru', kod)}">{METIN[kod]['basvuru']}</a>
{dil_secici(path)}
    </div>
  </div>
</header>"""


def footer(path: str = "/") -> str:
    kod, _temel = dil(path)
    columns = "\n".join(
        f'    <nav aria-label="{METIN[kod][title]}">\n      <p>{METIN[kod][title]}</p>\n'
        + "\n".join(f'      <a href="{yerel(href, kod)}">{_etiket(href, anahtar, kod)}</a>' for href, anahtar in links)
        + "\n    </nav>"
        for title, links in FOOTER)
    return f"""<footer class="site-footer">
  <div class="wrap footer-grid">
    <div class="footer-brand">
      <span class="brand brand-small"><img src="/assets/peyrev-mark-32.png" alt="" width="22" height="22">Peyrev</span>
      <p>{METIN[kod]['slogan']}</p>
      <p><a href="mailto:info@peyrev.com">info@peyrev.com</a></p>
    </div>
{columns}
  </div>
  <div class="wrap footer-note">{METIN[kod]['not']}</div>
</footer>"""


def alternatifler(path: str) -> str:
    """Çevrilmiş sayfanın bütün dillerdeki adresleri (arama motorları için)."""
    _kod, temel = dil(path)
    satirlar = [f'<link rel="alternate" hreflang="{kod}" href="{SITE}{yerel(temel, kod)}">' for kod in DILLER]
    satirlar.append(f'<link rel="alternate" hreflang="x-default" href="{SITE}{temel}">')
    return "\n".join(satirlar)


CEVIRI = '<script src="/assets/ceviri.js" defer></script>'
FORM_BETIGI = re.compile(r'<script src="/assets/(?:site|iletisim)\.js[^"]*" defer></script>')


def _dil_isle(text: str, path: str) -> str:
    """<html lang dir>, alternatif dil bağlantıları; çeviri sayfasında iç bağlantıların dili ve form çevirisi."""
    kod, temel = dil(path)
    text = re.sub(r"<html[^>]*>", f'<html lang="{kod}" dir="{DILLER[kod][1]}">', text, count=1)
    text = re.sub(r'<link rel="alternate" hreflang="[^"]*" href="[^"]*">\n', "", text)
    if temel in CEVRILEN:
        text = text.replace("</head>", alternatifler(path) + "\n</head>", 1)
    if kod == "tr":
        return text
    bas, govde = text.split("<body", 1)
    govde = re.sub(r'href="(/[^"]*)"', lambda m: f'href="{yerel(m.group(1), kod)}"', govde)
    text = bas + "<body" + govde
    if "/assets/ceviri.js" not in text and FORM_BETIGI.search(text):
        text = FORM_BETIGI.sub(lambda m: CEVIRI + "\n" + m.group(0), text, count=1)
    return text


VISIT = '<script src="/assets/ziyaret.js" defer></script>'
ASSET = re.compile(r'(/assets/([\w.-]+\.(?:css|js)))(?:\?v=[0-9a-f]+)?"')


def stamp(name: str) -> str:
    return hashlib.sha256((WEB / "assets" / name).read_bytes()).hexdigest()[:10]


def pages() -> list[Path]:
    return sorted(page for page in WEB.rglob("*.html"))


def render(page: Path, text: str | None = None) -> str:
    """The page with the shared parts and stamps; ``text`` is the page's source when it is not yet on disk."""
    text = page.read_text(encoding="utf-8") if text is None else text
    if page.is_relative_to(WEB):
        path = page_path(page)
        text = re.sub(r'<header class="site-header">.*?</header>', lambda _m: header(path), text, count=1, flags=re.DOTALL)
        text = re.sub(r'<footer class="site-footer">.*?</footer>', lambda _m: footer(path), text, count=1,
                      flags=re.DOTALL)
        if '/assets/ziyaret.js' not in text:
            text = text.replace("</head>", VISIT + "\n</head>", 1)
        text = _dil_isle(text, path)
    return ASSET.sub(lambda match: f'{match.group(1)}?v={stamp(match.group(2))}"', text)


def main() -> None:
    for page in pages() + EXTRA_PAGES:
        updated = render(page)
        if updated != page.read_text(encoding="utf-8"):
            page.write_text(updated, encoding="utf-8")
            print("güncellendi:", page.relative_to(ROOT))


if __name__ == "__main__":
    main()
