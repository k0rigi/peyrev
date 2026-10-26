"""Yönetim paneli sayfası (GET /yonetim/ ve yanındaki iki dosya): güvenlik başlıkları, dosya listesi dışındaki her yolun
reddi ve sayfa ile betiğin içerik kuralı (satır içi betik ve stil yok, kullanıcı verisi DOM'a yalnız metin olarak yazılır)."""

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

from peyrev.admin import page

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
HTML = (page.STATIC / "index.html").read_text(encoding="utf-8")
SCRIPT = (page.STATIC / "panel.js").read_text(encoding="utf-8")
STYLE = (page.STATIC / "panel.css").read_text(encoding="utf-8")
SECURITY = {"X-Frame-Options": "DENY", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
            "Cross-Origin-Opener-Policy": "same-origin"}


def directives(policy):
    """CSP metni → {yönerge: [kaynaklar]}."""
    result = {}
    for part in policy.split(";"):
        name, *sources = part.split()
        assert name not in result, name
        result[name] = sources
    return result


class Elements(HTMLParser):
    """Sayfadaki öğeler (ad, öznitelikler) ve <script>/<style> içindeki metin."""

    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.elements, self.inline, self._open = [], [], None
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))
        self._open = tag if tag in ("script", "style") else None

    def handle_endtag(self, tag):
        self._open = None

    def handle_data(self, data):
        if self._open and data.strip():
            self.inline.append((self._open, data))


def assert_secure(response):
    for name, value in SECURITY.items():
        assert response.headers[name] == value, name
    assert response.headers["Content-Security-Policy"] == page.POLICY
    assert response.headers["Permissions-Policy"] == "camera=(), microphone=(), geolocation=(), payment=()"
    assert "Set-Cookie" not in response.headers


def test_the_panel_page_is_served_with_strict_security_headers(client):
    response = client.get("/yonetim/")
    assert response.status_code == 200 and response.mimetype == "text/html"
    assert response.get_data(as_text=True) == HTML
    assert response.headers["Cache-Control"] == "no-store"  # a page that shows accounts is never cached
    assert_secure(response)
    policy = directives(response.headers["Content-Security-Policy"])
    assert policy["default-src"] == ["'self'"] and policy["frame-ancestors"] == ["'none'"]
    assert policy["script-src"] == policy["style-src"] == ["'self'"]
    assert policy["object-src"] == policy["base-uri"] == ["'none'"]
    assert policy["connect-src"] == policy["img-src"] == policy["form-action"] == ["'self'"]
    sources = [source for values in policy.values() for source in values]
    assert not [source for source in sources if source not in ("'self'", "'none'")]  # no inline, eval, host or data:


def test_the_bare_address_leads_to_the_panel(client):
    response = client.get("/yonetim")
    # Relative: behind the proxy the request reaches the API over plain http.
    assert (response.status_code, response.headers["Location"]) == (308, "/yonetim/")


@pytest.mark.parametrize(("name", "media"), [("panel.js", "text/javascript"), ("panel.css", "text/css")])
def test_the_script_and_the_style_are_revalidated_on_every_visit(client, name, media):
    response = client.get("/yonetim/" + name)
    assert response.status_code == 200 and response.mimetype == media
    assert response.data == (page.STATIC / name).read_bytes()
    assert response.headers["Cache-Control"] == "no-cache" and response.headers["ETag"]
    assert_secure(response)
    again = client.get("/yonetim/" + name, headers={"If-None-Match": response.headers["ETag"]})
    assert again.status_code == 304 and again.data == b""


LEAKS = (b"def page_blueprint", b"[project]", b"create_blueprint", b"root:", SCRIPT[:200].encode(), STYLE[:200].encode(),
         HTML[:200].encode())


@pytest.mark.parametrize("name", ["index.html", "yok.js", "PANEL.JS", "panel.js%00.css", "page.py", "__init__.py",
                                  "%2e%2e", "..%5c..%5c__init__.py", "..%5cpage.py"])
def test_any_other_name_is_refused_by_the_panel(client, name):
    response = client.get("/yonetim/" + name)
    assert response.status_code == 404 and not any(leak in response.get_data() for leak in LEAKS)
    assert_secure(response)  # the refusal carries the panel's headers too


@pytest.mark.parametrize("path", ["/yonetim/panel.js/", "/yonetim/static/panel.js", "/yonetim/../app.py",
                                  "/yonetim/..%2fapp.py", "/yonetim/%2e%2e%2f%2e%2e%2fpyproject.toml",
                                  "/yonetim//etc/passwd", "/yonetim/%2fetc%2fpasswd", "/yonetim/./panel.js/.."])
def test_paths_that_leave_the_panel_folder_match_nothing(client, path):
    response = client.get(path)
    assert response.status_code == 404 and not any(leak in response.get_data() for leak in LEAKS)


def test_the_page_has_no_inline_script_or_style_and_loads_only_same_origin_files():
    parsed = Elements(HTML)
    assert parsed.inline == []
    assert not [tag for tag, _attrs in parsed.elements if tag == "style"]
    scripts = [attrs for tag, attrs in parsed.elements if tag == "script"]
    assert scripts == [{"src": "/yonetim/panel.js", "defer": None}]
    for tag, attrs in parsed.elements:
        assert not [name for name in attrs if name.startswith("on") or name == "style"], (tag, attrs)
        for name in ("src", "href", "action", "srcset"):
            value = attrs.get(name)
            if value is None or (tag == "a" and value.startswith("#")):
                continue
            # Same origin only: an absolute path, never another host or a script address.
            assert value.startswith("/") and not value.startswith("//"), (tag, value)
            assert "javascript:" not in value.lower()
    resources = sorted(attrs.get("href") or attrs["src"] for tag, attrs in parsed.elements
                       if tag in ("link", "img", "script"))
    assert resources == ["/assets/peyrev-mark-192.png", "/assets/peyrev-mark-192.png", "/assets/peyrev-mark-32.png",
                         "/yonetim/panel.css", "/yonetim/panel.js"]
    for path in resources:
        if path.startswith("/assets/"):
            assert (WEB / path.removeprefix("/")).is_file(), path  # the static site serves the brand images
    assert ("meta", {"name": "robots", "content": "noindex, nofollow"}) in parsed.elements


def test_the_panel_leaves_plain_http_before_anything_is_shown():
    """Vekil düz http'yi uygulamaya bildirmez: panel betiği ilk iş olarak https adresine geçer (giriş formu kurulmadan)."""
    head = SCRIPT.split("const API = ", 1)[0]
    assert "if (location.protocol !== 'https:' && location.hostname === 'peyrev.com') {" in head
    assert "location.replace('https://peyrev.com' + location.pathname + location.search + location.hash);" in head
    assert head.rstrip().endswith("return;\n  }")


def test_the_panel_script_writes_user_data_into_the_page_only_as_text():
    code = re.sub(r"/\*.*?\*/", "", SCRIPT, flags=re.DOTALL)  # the header comment names the rule itself
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "DOMParser",
                      "createContextualFragment", "srcdoc", "eval(", "new Function", "javascript:", ".style.",
                      "cssText", "setAttribute('style'", "setAttribute('on", "localStorage", "sessionStorage"):
        assert forbidden not in code, forbidden
    assert not re.search(r"set(?:Timeout|Interval)\(\s*['\"`]", code)  # no code in strings
    # Every element comes from the h() helper or createElement; text goes in as textContent or text nodes.
    helper = re.search(r"function h\(tag, props, children\) \{(.*?)\n  \}\n", code, re.DOTALL).group(1)
    assert "document.createElement(tag)" in helper and "node.textContent = String(value)" in helper
    assert "node.append(child instanceof Node ? child : String(child))" in helper
    assert "addEventListener" in helper  # handlers are functions, never attribute text
    writes = re.findall(r"\.(textContent|innerText|value)\s*=", code)
    assert writes and set(writes) <= {"textContent", "value"}
    # Links made from server data keep a fixed scheme or anchor.
    for href in re.findall(r"href: ([^,}]+)", code):
        assert re.match(r"('(mailto:|tel:|#)|url\b|data\.baglanti\b|settings\.back\[0\])", href.strip()), href
    assert "/^otpauth:\\/\\//.test(data.baglanti)" in code


def test_the_panel_style_loads_nothing_from_elsewhere():
    assert "@import" not in STYLE and "url(" not in STYLE and "@font-face" not in STYLE
