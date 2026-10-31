"""peyrev.com/egitim/adimlar/ (adım rehberi) ve /egitim/deneme/ (deneme akışları) sayfalarını üretir.

Veri Studio deposundan gelir: ``scripts/egitim_icerigi.py`` adım kütüphanesinin açıklamalarını, kullanım notlarını ve
alan yardımlarını ``tools/egitim-verisi/adimlar.json``'a, deneme akışlarını ``akislar.json``'a ve
``web/egitim/deneme/akislar/*.json``'a yazar. Studio'da adımlar değişince orada o betik, burada bu araç çalıştırılır:

    python tools/egitim.py && python tools/site.py

tests/test_egitim_rehberi.py sayfaların veriyle aynı olduğunu denetler. Bütün metin html.escape ile yazılır.
"""

from __future__ import annotations

import json
import re
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
VERI = ROOT / "tools" / "egitim-verisi"
REHBER = WEB / "egitim" / "adimlar"
DENEME = WEB / "egitim" / "deneme"
DOSYALAR_ZIP = "peyrev-deneme-dosyalari.zip"
AKISLAR_ZIP = "peyrev-deneme-akislari.zip"
TR = str.maketrans("çğıöşüÇĞİÖŞÜâî", "cgiosuCGIOSUai")


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.translate(TR).lower()).strip("-")


def e(text: object) -> str:
    return escape(str(text), quote=True)


def kod(text: str) -> str:
    """${degisken} ve {{alan}} yazıları kod olarak gösterilir; geri kalan metin kaçışlıdır."""
    parts = re.split(r"(\$\{[^}]+\}|\{\{[^}]+\}\})", text)
    return "".join(f"<code>{e(part)}</code>" if index % 2 else e(part) for index, part in enumerate(parts))


def page(*, title: str, description: str, path: str, body: str) -> str:
    # Header and footer are placeholders; tools/site.py fills the shared ones and the stamps.
    return f"""<!doctype html>
<html lang="tr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; font-src 'self'; form-action 'self'; base-uri 'none'; object-src 'none'">
<meta name="color-scheme" content="light dark">
<meta name="description" content="{e(description)}">
<title>{e(title)}</title>
<link rel="canonical" href="https://peyrev.com{path}">
<link rel="icon" href="/assets/peyrev-mark-32.png">
<link rel="apple-touch-icon" href="/assets/peyrev-mark-180.png">
<link rel="stylesheet" href="/assets/site.css">
<script src="/assets/ziyaret.js" defer></script>
</head>
<body>
<a class="skip" href="#icerik">İçeriğe geç</a>

<header class="site-header"></header>

<main id="icerik" tabindex="-1">
{body}
</main>

<footer class="site-footer"></footer>
</body>
</html>
"""


def hero(eyebrow: str, title: str, lead: str, extra: str = "") -> str:
    return f"""  <section class="page-hero page-hero-small" aria-labelledby="sayfa-baslik">
    <div class="wrap">
      <p class="eyebrow"><span class="eyebrow-dot" aria-hidden="true"></span>{e(eyebrow)}</p>
      <h1 id="sayfa-baslik">{e(title)}</h1>
      <p class="lead">{lead}</p>{extra}
    </div>
  </section>"""


def yukle() -> tuple[dict, list[dict]]:
    adimlar = json.loads((VERI / "adimlar.json").read_text(encoding="utf-8"))
    akislar = json.loads((VERI / "akislar.json").read_text(encoding="utf-8"))
    return adimlar, akislar


def adim_adresi(step: dict) -> str:
    return f"/egitim/adimlar/{slug(step['kategori'])}/#adim-{step['numara']}"


# ----- adım rehberi --------------------------------------------------------------------------------------------------
def adim_karti(step: dict, akislar: dict[str, dict]) -> str:
    parts = [f'      <article class="guide-step" id="adim-{step["numara"]}">',
             '        <div class="guide-step-head">',
             f'          <span class="guide-no" aria-label="Adım {step["numara"]}">{step["numara"]}</span>',
             f'          <h2>{e(step["ad"])}</h2>']
    if step["ajan"]:
        parts.append('          <span class="guide-badge">Ajanlar hakkı gerekir</span>')
    parts += ['        </div>', f'        <p class="guide-lead">{kod(step["aciklama"])}</p>']
    if step["nasil"]:
        parts.append("        <h3>Nasıl kullanılır?</h3>\n        <ol>")
        parts += [f"          <li>{kod(item)}</li>" for item in step["nasil"]]
        parts.append("        </ol>")
    if step["sonuc"]:
        parts.append(f'        <h3>Sonuç</h3>\n        <p>{kod(step["sonuc"])}</p>')
    if step["ipucu"]:
        parts.append(f'        <p class="guide-tip"><strong>İpucu.</strong> {kod(step["ipucu"])}</p>')
    if step["alanlar"]:
        parts.append(f'        <details class="guide-fields">\n          <summary>Alanlar ({len(step["alanlar"])})</summary>'
                     '\n          <dl>')
        for alan in step["alanlar"]:
            mark = ' <span class="guide-required">zorunlu</span>' if alan["zorunlu"] else ""
            detail = kod(alan["yardim"]) if alan["yardim"] else ""
            if alan["secenekler"]:
                options = ", ".join(e(option) for option in alan["secenekler"])
                detail = f'{detail} <span class="guide-options">Seçenekler: {options}.</span>'.strip()
            parts.append(f"            <dt>{e(alan['ad'])}{mark}</dt>\n            <dd>{detail or '—'}</dd>")
        parts.append("          </dl>\n        </details>")
    if step["akislar"]:
        links = ", ".join(f'<a href="/egitim/deneme/#deneme-{e(slug_)}">{e(akislar[slug_]["ad"])}</a>'
                          for slug_ in step["akislar"])
        parts.append(f'        <p class="guide-try"><strong>Uygulamalı deneme:</strong> {links}</p>')
    if step["alistirma"]:
        parts.append(f'        <p class="guide-try"><strong>Alıştırma:</strong> {kod(step["alistirma"])}</p>')
    parts.append("      </article>")
    return "\n".join(parts)


def kategori_sayfasi(veri: dict, kategori: str, akislar: dict[str, dict]) -> str:
    steps = [step for step in veri["adimlar"] if step["kategori"] == kategori]
    order = veri["kategoriler"]
    index = order.index(kategori)
    toc = "\n".join(f'        <li><a href="#adim-{step["numara"]}"><span>{step["numara"]}</span> {e(step["ad"])}</a></li>'
                    for step in steps)
    cards = "\n".join(adim_karti(step, akislar) for step in steps)
    nav = []
    if index > 0:
        nav.append(f'<a href="/egitim/adimlar/{slug(order[index - 1])}/">← {e(order[index - 1])}</a>')
    nav.append('<a href="/egitim/adimlar/">Bütün kategoriler</a>')
    if index < len(order) - 1:
        nav.append(f'<a href="/egitim/adimlar/{slug(order[index + 1])}/">{e(order[index + 1])} →</a>')
    body = hero("Adım rehberi", kategori,
                f"{len(steps)} adım: ne yaptığı, nasıl kullanıldığı, ne sonuç verdiği ve hangi deneme akışında "
                f"denendiği. Peyrev {e(veri['surum'])}.") + f"""
  <section class="band band-tight">
    <div class="wrap guide">
      <nav class="guide-toc" aria-label="{e(kategori)} adımları">
        <ol>
{toc}
        </ol>
      </nav>
      <div class="guide-steps">
{cards}
        <nav class="guide-pager" aria-label="Kategoriler">{" ".join(nav)}</nav>
      </div>
    </div>
  </section>"""
    return page(title=f"{kategori} adımları · Peyrev", path=f"/egitim/adimlar/{slug(kategori)}/",
                description=f"Peyrev adım rehberi: {kategori} kategorisindeki {len(steps)} adımın kullanımı.",
                body=body)


def rehber_sayfasi(veri: dict) -> str:
    groups = []
    for kategori in veri["kategoriler"]:
        steps = [step for step in veri["adimlar"] if step["kategori"] == kategori]
        items = "\n".join(
            f'          <li><a href="{adim_adresi(step)}"><span>{step["numara"]}</span> {e(step["ad"])}</a>'
            f'<small>{kod(step["aciklama"])}</small></li>' for step in steps)
        groups.append(f"""      <section class="guide-group" aria-labelledby="k-{slug(kategori)}">
        <h2 id="k-{slug(kategori)}"><a href="/egitim/adimlar/{slug(kategori)}/">{e(kategori)}</a>
          <span>{len(steps)} adım</span></h2>
        <ul class="guide-list">
{items}
        </ul>
      </section>""")
    common = "\n".join(f"          <dt>{e(item['ad'])}</dt>\n          <dd>{kod(item['yardim'])}</dd>"
                       for item in veri["ortak_alanlar"])
    total = len(veri["adimlar"])
    body = hero("Eğitim", "Adım rehberi",
                f"Peyrev {e(veri['surum'])}'daki {total} adımın her biri: ne yapar, nasıl kullanılır, ne sonuç verir. "
                "Her adım bir <a href=\"/egitim/deneme/\">deneme akışında</a> gerçek bir işte çalışır; indirip "
                "kendi bilgisayarınızda deneyebilirsiniz.") + f"""
  <section class="band band-tight">
    <div class="wrap guide-index">
      <div class="guide-intro">
        <p>Adımlar Peyrev'de sol taraftaki kütüphanede bu kategorilerle durur; adımın numarası hiç değişmez. Bir adımı
          akışa ekleyip Bu adımı test et ile tek başına deneyebilirsiniz. Değişkenler <code>${{ad}}</code> biçiminde
          yazılır; adımların sonuçları sonraki adımlarda böyle kullanılır.</p>
        <details class="guide-fields">
          <summary>Birçok adımda olan ortak alanlar</summary>
          <dl>
{common}
          </dl>
        </details>
      </div>
{chr(10).join(groups)}
    </div>
  </section>"""
    return page(title="Adım rehberi · Peyrev", path="/egitim/adimlar/",
                description=f"Peyrev'in {total} adımının her biri için kullanım rehberi ve uygulamalı deneme akışları.",
                body=body)


# ----- deneme akışları ------------------------------------------------------------------------------------------------
def akis_karti(akis: dict, adimlar: dict[int, dict], akislar: dict[str, dict]) -> str:
    needs = "\n".join(f"            <li>{kod(item)}</li>" for item in akis["gerekenler"])
    how = "\n".join(f"            <li>{kod(item)}</li>" for item in akis["nasil"])
    steps = " ".join(f'<a href="{adim_adresi(adimlar[number])}" title="{e(adimlar[number]["ad"])}">'
                     f'{number} · {e(adimlar[number]["ad"])}</a>' for number in sorted(akis["turler"]))
    before = (f'\n          <p class="trial-before">Önce <a href="#deneme-{e(akis["onceki"])}">'
              f'{e(akislar[akis["onceki"]]["ad"])}</a> çalıştırılmış olmalı.</p>' if akis["onceki"] else "")
    return f"""        <li class="trial-flow" id="deneme-{e(akis['slug'])}">
          <div class="trial-flow-head">
            <p class="example-dept">{e(akis['bolum'])} · {e(akis['platform'])}</p>
            <h3>{e(akis['ad'])}</h3>
            <p>{kod(akis['aciklama'])}</p>{before}
          </div>
          <div class="trial-flow-body">
            <div>
              <h4>Gerekenler</h4>
              <ul>
{needs}
              </ul>
            </div>
            <div>
              <h4>Nasıl denenir?</h4>
              <ol>
{how}
              </ol>
            </div>
          </div>
          <p class="trial-expected"><strong>Beklenen sonuç:</strong> {kod(akis['beklenen'])}</p>
          <details class="guide-fields">
            <summary>Denediği adımlar ({len(akis['turler'])})</summary>
            <p class="trial-steps">{steps}</p>
          </details>
          <div class="template-foot">
            <p><strong>{akis['adim_sayisi']} adım.</strong> Diyagramdaki not kutuları her bölümü anlatır.</p>
            <a class="button button-small button-dark" href="/egitim/deneme/akislar/{e(akis['slug'])}.json"
              download="peyrev-deneme-{e(akis['slug'])}.json">Akışı indir</a>
          </div>
        </li>"""


DENETIM = [
    ("Açılışta başlama", ("Ayarlar'da <strong>Bilgisayar açılınca başlat</strong>'ı açın, bilgisayarı yeniden başlatın: "
     "Peyrev kendiliğinden, simge durumunda açılmalı.")),
    ("Kilitli ekran", ("Ekran kilitliyken Deneme 10'u çalıştırın: akış fareye ve klavyeye dokunmadan, ekranın kilitli "
     "olduğunu yazarak durmalı. Kilidi açınca aynı akış baştan sona çalışmalı. Robot bilgisayarında uyku ve ekran kilidi kapalı olmalı.")),
    ("Robot Paneli", ("Ayarlar'da <strong>Robot Paneline durum bildir</strong> açıkken Deneme 02'yi çalıştırın: "
     "peyrev.com → Hesabım → Robotlarım'da bilgisayar ve çalışma görünmeli.")),
    ("Hata haberi", ("Telegram bağlıyken ve akışın Akış bilgileri'nde <strong>Hata verirse Telegram'a bildir</strong> "
     "açıkken Deneme 05'i Peyrev Deneme klasörünü silip çalıştırın: akış nedenini yazarak durmalı ve Telegram'a "
     "hata haberi gelmeli.")),
    ("İş kuyruğu", ("Deneme 10 çalışırken Deneme 09'u başlatın: ekran kullanan ikinci akış sırada beklemeli, ilki "
     "bitince başlamalı. Ekran kullanmayan Deneme 02 ise beklemeden çalışmalı.")),
    ("Ofis yetkilisi", ("Ajanlar → Telegram'da ofis yetkilisini davet edin ve Deneme 15'i çalıştırın: onay sorusu "
     "yetkilinin Telegram'ına gitmeli; verdiği yanıt ve adı çalışma notuna yazılmalı.")),
]


def deneme_sayfasi(veri: dict, akislar_listesi: list[dict]) -> str:
    adimlar = {step["numara"]: step for step in veri["adimlar"]}
    akislar = {akis["slug"]: akis for akis in akislar_listesi}
    serbest = [akis for akis in akislar_listesi if akis["bolum"] != "Bağlantı" and akis["bolum"] != "Ajan"]
    baglantili = [akis for akis in akislar_listesi if akis not in serbest]
    covered = {number for akis in akislar_listesi for number in akis["turler"]}
    exercises = [step for step in veri["adimlar"] if step["alistirma"]]
    exercise_items = "\n".join(f'          <li><a href="{adim_adresi(step)}">{step["numara"]} · {e(step["ad"])}</a>'
                               f'<small>{kod(step["alistirma"])}</small></li>' for step in exercises)
    checks = "\n".join(f"          <li><h3>{e(title)}</h3><p>{text}</p></li>" for title, text in DENETIM)
    body = hero("Eğitim", "Deneme akışları",
                f"Her deneme akışı Peyrev'in adımlarını gerçek bir işte çalıştırır ve sonucu kendisi denetler: "
                f"beklenen sonuç çıkmazsa nedenini yazarak durur. {len(akislar_listesi)} akış {len(covered)} adımı "
                "dener; hem öğrenmek hem de kurulumu denetlemek için.",
                f"""
      <p class="hero-actions"><a class="button button-gold" href="/egitim/deneme/{AKISLAR_ZIP}"
        download="{AKISLAR_ZIP}">Bütün akışları indir</a>
        <a class="button button-ghost" href="/egitim/deneme/{DOSYALAR_ZIP}" download="{DOSYALAR_ZIP}">Deneme
        dosyalarını indir</a></p>""") + f"""
  <section class="band band-tight">
    <div class="wrap">
      <ol class="steps steps-compact" aria-label="Deneme akışlarını kullanmak">
        <li><h3>İndirin ve aktarın</h3><p>Akışları indirin; Peyrev'de <strong>Akışlar → İçe aktar</strong> ile ekleyin.
          Zip'i indirdiyseniz önce açın. Akışlar aktarılınca kendiliğinden çalışmaz.</p></li>
        <li><h3>Deneme 01 ile başlayın</h3><p>Deneme 01 deneme dosyalarını masaüstünüzdeki <strong>Peyrev Deneme</strong>
          klasörüne indirir; diğer akışlar bu klasörü kullanır.</p></li>
        <li><h3>Çalıştırın ve inceleyin</h3><p>Çalışma notunda her adımın sonucu yazar. Diyagram görünümündeki not
          kutuları her bölümü anlatır; adımlara tıklayıp alanlarını inceleyin.</p></li>
      </ol>

      <h2 class="trial-heading">Bağlantı gerektirmeyenler</h2>
      <ul class="trial-flows">
{chr(10).join(akis_karti(akis, adimlar, akislar) for akis in serbest)}
      </ul>

      <h2 class="trial-heading">Bağlantı gerektirenler</h2>
      <p class="trial-note">Bu akışlar Ayarlar → Bağlantılar'da tanımladığınız e-posta, veritabanı, Google Sheets,
        FTP ve yapay zekâ bağlantılarını kullanır. Adımlar varsayılan bağlantıyı seçer; birden fazla bağlantınız varsa
        adımın Bağlantı alanından seçin.</p>
      <ul class="trial-flows">
{chr(10).join(akis_karti(akis, adimlar, akislar) for akis in baglantili)}
      </ul>

      <h2 class="trial-heading">Uygulamalı alıştırmalar</h2>
      <p class="trial-note">Bu adımların hedefi ekrandan seçilir veya ekrandan görsel kaydedilir; her bilgisayarda
        farklı olduğu için hazır akışa konmaz. Kendi programınızda böyle deneyin:</p>
      <ul class="guide-list guide-list-wide">
{exercise_items}
      </ul>

      <h2 class="trial-heading">Robot bilgisayarı denetimi</h2>
      <p class="trial-note">Peyrev'i başında kimse olmayan bir bilgisayarda çalıştıracaksanız, kurulumdan sonra bunları
        da deneyin.</p>
      <ol class="trial-checks">
{checks}
      </ol>
    </div>
  </section>"""
    return page(title="Deneme akışları · Peyrev", path="/egitim/deneme/",
                description="Peyrev'in adımlarını gerçek işlerde çalıştıran, sonucu kendisi denetleyen indirilebilir "
                            "deneme akışları ve deneme dosyaları.", body=body)


def sayfalar() -> dict[Path, str]:
    veri, akislar = yukle()
    akis_map = {akis["slug"]: akis for akis in akislar}
    result = {REHBER / "index.html": rehber_sayfasi(veri), DENEME / "index.html": deneme_sayfasi(veri, akislar)}
    for kategori in veri["kategoriler"]:
        result[REHBER / slug(kategori) / "index.html"] = kategori_sayfasi(veri, kategori, akis_map)
    return result


def main() -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location("site_tool", ROOT / "tools" / "site.py")
    site = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(site)
    for path, text in sayfalar().items():
        rendered = site.render(path, text)
        if not path.exists() or rendered != path.read_text(encoding="utf-8"):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(rendered, encoding="utf-8")
            print("güncellendi:", path.relative_to(ROOT))


if __name__ == "__main__":
    main()
