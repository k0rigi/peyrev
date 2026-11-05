"""peyrev.com'un dil seçenekleri: tanıtım sayfalarının (tools/site.py CEVRILEN) her dilde çevirisi web/<dil>/ altında
aynı yoldadır ve yapısı Türkçesiyle aynıdır (kimlikler, form alanları ve değerleri, onay metninin sürümü, görseller,
betikler); sayfanın dili ve yönü, dil seçici ve arama motorları için alternatif adresler doğrudur; formların bütün
mesajlarının her dilde karşılığı vardır (assets/ceviri.js).

Türkçe sayfaya bölüm eklenir ya da kimlik değişirse burada çeviri sayfaları düşer: çeviriler de güncellenmelidir.
"""

from __future__ import annotations

import importlib.util
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

from peyrev.accounts.applications import RESPONSE_RECEIVED
from peyrev.iletisim import RECEIVED

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
spec = importlib.util.spec_from_file_location("site_tool", ROOT / "tools" / "site.py")
site = importlib.util.module_from_spec(spec)
spec.loader.exec_module(site)
YABANCI = [kod for kod in site.DILLER if kod != "tr"]
CIFTLER = [(kod, yol) for kod in YABANCI for yol in site.CEVRILEN]
# Çeviride Türkçe kalabilecekler: Studio'nun arayüzü Türkçedir (kullanıcı bu yazıyı uygulamada arar; yanına çevirisi
# yazılır) ve özel adlar.
TURKCE_KALIR = ("Akışlar → İçe aktar", "Ayarlar → Peyrev lisansı", "Bağlantılar", "Şifre kodum var", "Öneri ve destek",
                "Taleplerim", "Her satır için", "Koşul", "Hata olursa", "Adıma git", "PDF'ten oku", "Dijital İkiz",
                "Paraşüt", "e-Arşiv", "Trendyol Satıcı API", "Hepsiburada Pazaryeri")


def dosya(yol: str, kod: str = "tr") -> Path:
    onek = "" if kod == "tr" else f"{kod}/"
    return WEB / f"{onek}{yol.strip('/')}/index.html".replace("//", "/").lstrip("/")


class Sayfa(HTMLParser):
    """Kimlikler, yapısal öğeler ve başlık / alt bilgi dışındaki görünen metin."""

    def __init__(self, metin: str):
        super().__init__(convert_charrefs=True)
        self.etiketler, self.ids, self.ozel, self.yazi = [], [], [], []
        self._ortak = 0
        self._gizli = None
        self.feed(metin)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        self.etiketler.append((tag, a))
        if "id" in a:
            self.ids.append(a["id"])
        if tag in ("header", "footer") and a.get("class") in ("site-header", "site-footer"):
            self._ortak += 1
        if tag in ("script", "style"):
            self._gizli = tag
        if tag in ("input", "select", "textarea", "option", "img", "source", "form", "button"):
            self.ozel.append((tag, a.get("name"), a.get("value"), a.get("src") or a.get("srcset"), a.get("type"),
                              a.get("data-surum")))

    def handle_endtag(self, tag):
        if tag in ("header", "footer") and self._ortak:
            self._ortak -= 1
        if tag == self._gizli:
            self._gizli = None

    def handle_data(self, data):
        if not self._ortak and not self._gizli and data.strip():
            self.yazi.append(data.strip())


def oku(yol: Path) -> Sayfa:
    return Sayfa(yol.read_text(encoding="utf-8"))


def betikler(s: Sayfa) -> list[str]:
    return [a["src"].split("?")[0] for t, a in s.etiketler if t == "script" and "src" in a]


@pytest.mark.parametrize(("kod", "yol"), CIFTLER, ids=[f"{k}{y}" for k, y in CIFTLER])
def test_every_marketing_page_is_translated_with_the_same_structure(kod, yol):
    ceviri, kaynak = dosya(yol, kod), dosya(yol)
    assert ceviri.is_file(), f"{ceviri.relative_to(ROOT)} yok: Türkçe sayfanın çevirisi eksik."
    c, k = oku(ceviri), oku(kaynak)
    assert c.ids == k.ids, f"Kimlikler Türkçe sayfadan farklı: {set(c.ids) ^ set(k.ids)}"
    assert c.ozel == k.ozel  # form alanları, seçenek değerleri, onay metni sürümü, görseller aynı
    beklenen = []
    for b in betikler(k):
        if b in ("/assets/site.js", "/assets/iletisim.js"):
            beklenen.append("/assets/ceviri.js")  # formun mesajları sayfanın dilinde
        beklenen.append(b)
    assert betikler(c) == beklenen
    [html] = [a for t, a in c.etiketler if t == "html"]
    assert html == {"lang": kod, "dir": site.DILLER[kod][1]}
    [baslik] = [a for t, a in c.etiketler if t == "meta" and a.get("name") == "description"]
    [kaynak_baslik] = [a for t, a in k.etiketler if t == "meta" and a.get("name") == "description"]
    assert baslik["content"] != kaynak_baslik["content"]


@pytest.mark.parametrize(("kod", "yol"), CIFTLER, ids=[f"{k}{y}" for k, y in CIFTLER])
def test_a_translated_page_has_no_turkish_left_and_links_within_its_language(kod, yol):
    s = oku(dosya(yol, kod))
    turkce = []
    for yazi in s.yazi:
        yazi = " ".join(yazi.split())
        for kalir in TURKCE_KALIR:
            yazi = yazi.replace(kalir, "")
        if re.search(r"[ğışĞİŞ]", yazi):
            turkce.append(yazi)
    assert not turkce, f"Çevrilmemiş metin: {turkce[:5]}"
    govde = dosya(yol, kod).read_text(encoding="utf-8").split("<body", 1)[1]
    for href in re.findall(r'href="(/[^"]*)"', govde):
        temel = href.partition("#")[0] or "/"
        assert temel not in site.CEVRILEN, f"{href}: çevrilmiş sayfaya giden bağlantı dil önekini taşımalı"


@pytest.mark.parametrize("sayfa", site.pages(), ids=lambda p: p.relative_to(WEB).as_posix())
def test_every_page_offers_the_languages_and_names_its_own(sayfa):
    s = oku(sayfa)
    yol = site.page_path(sayfa)
    kod, temel = site.dil(yol)
    secenekler = [a for t, a in s.etiketler if t == "a" and "hreflang" in a]
    assert [a["hreflang"] for a in secenekler] == list(site.DILLER)
    assert [a["hreflang"] for a in secenekler if a.get("aria-current") == "true"] == [kod]
    for a in secenekler:
        hedef = a["href"]
        assert (WEB / hedef.strip("/") / "index.html").is_file() or hedef == "/", hedef
    alternatif = {a["hreflang"]: a["href"] for t, a in s.etiketler if t == "link" and a.get("rel") == "alternate"}
    if temel in site.CEVRILEN:
        assert alternatif == {**{k: f"https://peyrev.com{site.yerel(temel, k)}" for k in site.DILLER},
                              "x-default": f"https://peyrev.com{temel}"}
    else:
        assert alternatif == {}
    if kod == "tr":
        assert "/assets/ceviri.js" not in betikler(s)


def test_every_form_message_has_a_translation_in_every_language():
    kod = (WEB / "assets" / "ceviri.js").read_text(encoding="utf-8")

    def liste(ad: str) -> list[str]:
        govde = re.search(rf"\n {{2,4}}{ad}: \[\n(.*?)\n {{2,4}}\]", kod, flags=re.DOTALL).group(1)
        return re.findall(r'^\s*"((?:[^"\\]|\\.)*)",$', govde, flags=re.MULTILINE)

    kaynak = re.findall(r'^\s*"((?:[^"\\]|\\.)*)",$',
                        re.search(r"const KAYNAK = \[\n(.*?)\n  \];", kod, flags=re.DOTALL).group(1), flags=re.MULTILINE)
    assert len(kaynak) == len(set(kaynak))
    for dil in YABANCI:
        cevrilen = liste(dil)
        assert len(cevrilen) == len(kaynak), dil
        assert all(m.strip() for m in cevrilen) and len(set(cevrilen)) == len(cevrilen), dil
    beklenen = {RESPONSE_RECEIVED, RECEIVED}
    for ad in ("site.js", "iletisim.js"):
        metin = (WEB / "assets" / ad).read_text(encoding="utf-8")
        beklenen |= set(re.findall(r"'([^']*[A-ZÇĞİÖŞÜa-zçğıöşü][^']*[.…])'", metin))
    eksik = {m for m in beklenen if m not in kaynak and not m.startswith(("/", "button"))}
    assert not eksik, f"ceviri.js'te karşılığı olmayan form metni: {eksik}"
    for ad in ("site.js", "iletisim.js"):
        metin = (WEB / "assets" / ad).read_text(encoding="utf-8")
        assert "const t = window.peyrevCeviri || ((metin) => metin);" in metin
        assert "show(t(answer.message || FAILED, FAILED), 'error');" in metin
    for yasak in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "fetch(", "http"):
        assert yasak not in kod, yasak
