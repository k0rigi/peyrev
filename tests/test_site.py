"""peyrev.com ana sayfası (web/): başvuru formunun sunucuyla sözleşmesi, onay metninin sürümü, yalnız aynı kökenden
kaynaklar ve sayfanın kendi CSP kuralıyla uyumu (satır içi betik ve stil yok).

Sunucunun kabul ettiği alanlar POST /api/rpa/basvuru ucunun kaynak kodundan okunur: form ya da uç değişip öbürü
değişmezse bu testler düşer.
"""

import ast
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

from peyrev.accounts import applications

WEB = Path(__file__).resolve().parents[1] / "web"
HTML = (WEB / "index.html").read_text(encoding="utf-8")
SCRIPT = (WEB / "assets" / "site.js").read_text(encoding="utf-8")
STYLE = (WEB / "assets" / "site.css").read_text(encoding="utf-8")
ENDPOINT = "/api/rpa/basvuru"
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
# Kullanıcının onayladığı metin, sürümüyle. Metin değişirse applications.CONSENT_VERSION ve formdaki data-surum yeni
# tarih olur ve metin buraya o tarihle eklenir: başvurularda saklanan sürüm hangi metnin onaylandığını gösterir.
CONSENT_TEXTS = {
    "2026-10-26": (
        "Ad soyad, e-posta ve telefon bilgilerim ile başvurunun gönderildiği internet adresi (IP), hesabımın açılması, "
        "lisansımın yönetimi, ödeme için benimle iletişim kurulması ve kötüye kullanımın önlenmesi amacıyla Peyrev "
        "tarafından işlenir ve saklanır. Şifrem yalnız geri çevrilemez bir özet olarak saklanır. Onaylanmayan "
        "başvurular 90 gün sonra silinir. Bu bilgilendirmeyi okudum ve kabul ediyorum."),
}


class Element:
    def __init__(self, tag, attrs, parent=None):
        self.tag, self.attrs, self.parent, self.children = tag, dict(attrs), parent, []

    def walk(self):
        yield self
        for child in self.children:
            if isinstance(child, Element):
                yield from child.walk()

    def text(self):
        parts = [child if isinstance(child, str) else child.text() for child in self.children]
        return " ".join(" ".join(parts).split())

    def ancestors(self):
        node = self.parent
        while node is not None:
            yield node
            node = node.parent


class Document(HTMLParser):
    """Küçük bir DOM: öğeler, öznitelikler, metin ve yorumlar."""

    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.root = Element("#document", {})
        self.comments = []
        self._stack = [self.root]
        self.feed(text)
        self.close()

    def handle_starttag(self, tag, attrs):
        element = Element(tag, attrs, self._stack[-1])
        self._stack[-1].children.append(element)
        if tag not in VOID:
            self._stack.append(element)

    def handle_endtag(self, tag):
        for index in range(len(self._stack) - 1, 0, -1):
            if self._stack[index].tag == tag:
                del self._stack[index:]
                return

    def handle_data(self, data):
        self._stack[-1].children.append(data)

    def handle_comment(self, data):
        self.comments.append(data)

    def all(self, tag=None, **attrs):
        return [node for node in self.root.walk() if (tag is None or node.tag == tag)
                and all(node.attrs.get(name.rstrip("_").replace("_", "-")) == value for name, value in attrs.items())]


PAGE = Document(HTML)
[FORM] = PAGE.all("form", id="basvuru")
CONTROLS = [node for node in FORM.walk() if node.tag in ("input", "select", "textarea")]


def control(name):
    found = [node for node in CONTROLS if node.attrs.get("name") == name]
    assert found, name
    return found


def accepted_fields():
    """Başvuru ucunun json_body(izinli, zorunlu) çağrısındaki alan kümeleri, sunucunun kaynak kodundan."""
    tree = ast.parse(Path(applications.__file__).read_text(encoding="utf-8"))
    for function in ast.walk(tree):
        if not isinstance(function, ast.FunctionDef):
            continue
        routes = [decorator.args[0].value for decorator in function.decorator_list
                  if isinstance(decorator, ast.Call) and getattr(decorator.func, "attr", "") == "post"
                  and decorator.args and isinstance(decorator.args[0], ast.Constant)]
        if routes == [ENDPOINT]:
            [call] = [node for node in ast.walk(function) if isinstance(node, ast.Call)
                      and getattr(node.func, "id", "") == "json_body"]
            allowed, required = (ast.literal_eval(argument) for argument in call.args)
            return set(allowed), set(required)
    raise AssertionError("POST " + ENDPOINT + " ucu bulunamadı")


def script_list(name):
    """site.js'teki `const NAME = ['a', 'b'];` dizisi."""
    match = re.search(r"const " + name + r" = \[([^\]]*)\];", SCRIPT)
    assert match, name
    return re.findall(r"'([^']*)'", match.group(1))


# ----- form ve sunucu ------------------------------------------------------------------------------------------
def test_the_form_sends_exactly_the_fields_the_server_accepts():
    allowed, required = accepted_fields()
    sent = script_list("SENT")
    optional = set(script_list("OPTIONAL"))
    # site.js sends SENT and always onay: true; optional fields only when filled.
    assert "body = { onay: true };" in SCRIPT
    assert set(sent) | {"onay"} == allowed
    assert required <= (set(sent) - optional) | {"onay"}
    assert optional == allowed - required - {"onay"} == {"telefon", "web_sitesi"}
    names = {node.attrs["name"] for node in CONTROLS}
    assert set(sent) | {"onay"} <= names
    assert names - allowed == {"tekrar"}  # the repeated password is checked in the browser and never sent
    assert re.search(r"const ENDPOINT = '" + re.escape(ENDPOINT) + "';", SCRIPT)
    assert re.search(r"fetch\(ENDPOINT, \{\s*method: 'POST'", SCRIPT)


def test_the_form_controls_follow_the_server_rules():
    for name in ("ad_soyad", "eposta", "sifre", "tekrar"):
        [field] = control(name)
        assert "required" in field.attrs, name
    [phone] = control("telefon")
    assert "required" not in phone.attrs and phone.attrs["type"] == "tel"
    [email] = control("eposta")
    assert email.attrs["type"] == "email" and email.attrs["autocomplete"] == "email"
    for name in ("sifre", "tekrar"):
        [field] = control(name)
        assert (field.attrs["type"], field.attrs["autocomplete"]) == ("password", "new-password")
    packages = control("paket")
    assert {node.attrs["type"] for node in packages} == {"radio"}
    assert [node.attrs["value"] for node in packages] == list(applications.PACKAGES) == ["rpa", "rpa_ajan"]
    assert [node.attrs["value"] for node in packages if "checked" in node.attrs] == ["rpa"]
    [consent] = control("onay")
    assert consent.attrs["type"] == "checkbox" and "required" in consent.attrs
    # The trap field: hidden from people and screen readers, never focused by the keyboard, never filled in.
    [trap] = control("web_sitesi")
    assert "required" not in trap.attrs
    assert (trap.attrs["tabindex"], trap.attrs["autocomplete"]) == ("-1", "off")
    [label] = [node for node in trap.ancestors() if node.tag == "label"]
    assert label.attrs["class"] == "trap" and label.attrs["aria-hidden"] == "true"
    assert re.search(r"\.trap \{[^}]*position: absolute; left: -10000px;", STYLE)
    assert "novalidate" in FORM.attrs and "action" not in FORM.attrs and "method" not in FORM.attrs


@pytest.mark.parametrize(("field", "minimum", "maximum"), [("ad_soyad", 3, 120), ("eposta", None, 150),
                                                           ("telefon", None, 32), ("sifre", 8, 128)])
def test_the_length_limits_of_the_form_are_accepted_by_the_server(client, field, minimum, maximum):
    [node] = control(field)
    assert (node.attrs.get("minlength"), node.attrs["maxlength"]) == (
        None if minimum is None else str(minimum), str(maximum))
    longest = {"ad_soyad": "A" * maximum, "eposta": "a" * 64 + "@" + "b" * (maximum - 70) + ".test",
               "telefon": "+90 " + "5" * (maximum - 4), "sifre": "a1" * (maximum // 2)}[field]
    assert len(longest) == maximum
    body = {"ad_soyad": "Ayşe Yılmaz", "eposta": "ayse@ornek.test", "sifre": "Guclu2026", "paket": "rpa",
            "onay": True, field: longest}
    response = client.post(ENDPOINT, json=body, headers={"X-Forwarded-For": "10.0.0.1"})
    assert response.status_code == 201, response.get_json()


def test_the_front_end_checks_say_what_the_server_says(client):
    """site.js'in önden denetim metinleri sunucunun aynı durumdaki yanıtıdır."""
    base = {"ad_soyad": "Ayşe Yılmaz", "eposta": "ayse@ornek.test", "sifre": "Guclu2026", "paket": "rpa", "onay": True}
    cases = [{"ad_soyad": "Ay"}, {"eposta": "ayse@ornek"}, {"sifre": "kisa1"}, {"sifre": "sadeceharf"},
             {"sifre": "12345678"}, {"onay": False}]
    for index, change in enumerate(cases):
        response = client.post(ENDPOINT, json={**base, **change}, headers={"X-Forwarded-For": f"10.0.1.{index}"})
        assert response.status_code == 400
        message = response.get_json()["message"]
        assert "'" + message + "'" in SCRIPT, message


# ----- onay metni ----------------------------------------------------------------------------------------------
def test_the_consent_text_is_the_version_the_server_records():
    [label] = PAGE.all("label", class_="consent")
    assert label.attrs["data-surum"] == applications.CONSENT_VERSION
    assert applications.CONSENT_VERSION in CONSENT_TEXTS, "Yeni onay metni sürümü CONSENT_TEXTS'e eklenmeli."
    assert label.text() == CONSENT_TEXTS[applications.CONSENT_VERSION], (
        "Onay metni değişti: applications.CONSENT_VERSION ve formdaki data-surum yeni tarih olmalı, metin "
        "CONSENT_TEXTS'e o tarihle eklenmeli.")
    # What the text promises is what the server does.
    assert f"{applications.KEEP_APPLICATIONS.days} gün sonra silinir" in label.text()
    assert [node for node in label.walk() if node.attrs.get("name") == "onay"]


# ----- kaynaklar ve CSP ----------------------------------------------------------------------------------------
def page_policy():
    [meta] = PAGE.all("meta", http_equiv="Content-Security-Policy")
    result = {}
    for part in meta.attrs["content"].split(";"):
        name, *sources = part.split()
        result[name] = sources
    return result


def test_the_page_loads_nothing_from_another_origin():
    loaded = []
    for node in PAGE.root.walk():
        for name in ("src", "srcset", "poster", "data"):
            if name in node.attrs:
                loaded.append((node.tag, node.attrs[name]))
        if node.tag == "link" and "href" in node.attrs:
            loaded.append((node.tag, node.attrs["href"]))
        assert node.tag not in ("iframe", "object", "embed", "base"), node.tag
    assert loaded and all(value.startswith("/") and not value.startswith("//") for _tag, value in loaded), loaded
    for _tag, value in loaded:
        assert (WEB / value.split("?")[0].removeprefix("/")).is_file(), value  # every file the page loads exists
    assert not re.search(r"@import|url\(|@font-face", STYLE)  # the style sheet loads nothing either; fonts are local
    assert not re.search(r"https?://", SCRIPT)


def test_the_page_follows_its_own_content_security_policy():
    policy = page_policy()
    assert policy["default-src"] == policy["script-src"] == policy["style-src"] == ["'self'"]
    assert policy["connect-src"] == policy["img-src"] == policy["font-src"] == policy["form-action"] == ["'self'"]
    assert policy["base-uri"] == policy["object-src"] == ["'none'"]
    assert not [source for sources in policy.values() for source in sources if source not in ("'self'", "'none'")]
    # No inline script, style or event handler: everything the page runs is /assets/site.js.
    scripts = PAGE.all("script")
    assert [{**node.attrs, "src": node.attrs["src"].split("?")[0]} for node in scripts] == [
        {"src": "/assets/site.js", "defer": None}]
    assert all(not node.text() for node in scripts) and PAGE.all("style") == []
    for node in PAGE.root.walk():
        assert not [name for name in node.attrs if name.startswith("on") or name == "style"], node.tag
        assert "javascript:" not in node.attrs.get("href", "").lower()
    # The script writes text only, builds no code and sets no inline style; its one request is same-origin.
    code = re.sub(r"/\*.*?\*/", "", SCRIPT, flags=re.DOTALL)
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function",
                      ".style.", "setAttribute('style'", "setAttribute('on"):
        assert forbidden not in code, forbidden
    assert not re.search(r"set(?:Timeout|Interval)\(\s*['\"`]", code)
    assert re.findall(r"fetch\(([^,]+),", code) == ["ENDPOINT"] and "credentials: 'omit'" in code


def test_the_links_inside_the_page_have_their_targets():
    ids = {node.attrs["id"] for node in PAGE.root.walk() if "id" in node.attrs}
    anchors = {node.attrs["href"][1:] for node in PAGE.all("a") if node.attrs.get("href", "").startswith("#")}
    assert anchors and anchors <= ids
    # Studio's "Lisansı uzat" opens https://peyrev.com/#basvuru: the form itself carries the anchor.
    assert FORM.attrs["id"] == "basvuru"
    for node in PAGE.root.walk():
        for name in ("aria-labelledby", "aria-describedby", "for"):
            if name in node.attrs:
                assert set(node.attrs[name].split()) <= ids, (node.tag, name)


def test_style_and_script_addresses_carry_the_current_content_stamp():
    """Tarayıcı stil ve betiği 7 gün saklar; adres içeriğin özetini taşımazsa değişiklik görünmez.
    Değiştirdiyseniz: python tools/surum-damgasi.py"""
    import hashlib

    pages = [WEB / "index.html", WEB / "egitim" / "index.html", WEB / "topluluk" / "index.html",
             WEB.parent / "deploy" / "indir" / "index.html"]
    found = []
    for page in pages:
        for name, stamp in re.findall(r'"/assets/([\w.-]+\.(?:css|js))(?:\?v=([0-9a-f]+))?"', page.read_text(encoding="utf-8")):
            expected = hashlib.sha256((WEB / "assets" / name).read_bytes()).hexdigest()[:10]
            assert stamp == expected, f"{page.name}: {name} damgası eski; python tools/surum-damgasi.py çalıştırın"
            found.append(name)
    assert {"site.css", "site.js", "egitim.js", "topluluk.js"} <= set(found)
