"""peyrev.com'un bütün statik sayfaları: ortak üst menü, alt bilgi ve stil/betik damgaları güncel (tools/site.py);
iç bağlantılar var olan sayfalara ve çapalara gider; her sayfa sıkı CSP taşır, satır içi betik ve stil yoktur, yüklediği
dosyalar vardır."""

from __future__ import annotations

import importlib.util
import json
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
spec = importlib.util.spec_from_file_location("site_tool", ROOT / "tools" / "site.py")
site = importlib.util.module_from_spec(spec)
spec.loader.exec_module(site)
PAGES = site.pages()
DYNAMIC = ("/api/", "/ikiz/")  # served by the application and by the Dijital İkiz service
# Extra sources a page may allow, beyond 'self' and 'none'.
ALLOWED = {"img-src": {"https://i.ytimg.com"}, "frame-src": {"https://www.youtube-nocookie.com"}}


class Tags(HTMLParser):
    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.tags, self.inline, self.ids, self._open = [], [], set(), None
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.tags.append((tag, attrs))
        if "id" in attrs:
            self.ids.add(attrs["id"])
        self._open = tag if tag in ("script", "style") else None

    def handle_endtag(self, tag):
        self._open = None

    def handle_data(self, data):
        if self._open and data.strip():
            self.inline.append(data)


def parsed(page: Path) -> Tags:
    return Tags(page.read_text(encoding="utf-8"))


def name(page: Path) -> str:
    return page.relative_to(WEB).as_posix()


def test_every_page_carries_the_current_header_footer_and_stamps():
    assert len(PAGES) >= 3
    stale = [name(page) for page in PAGES if site.render(page) != page.read_text(encoding="utf-8")]
    assert not stale, f"Ortak parçalar ya da damgalar eski: {stale}. python tools/site.py çalıştırın."
    for page in site.EXTRA_PAGES:
        assert site.render(page) == page.read_text(encoding="utf-8"), page


@pytest.mark.parametrize("page", PAGES, ids=name)
def test_the_page_is_strict_and_self_contained(page):
    tags = parsed(page)
    [content] = [attrs["content"] for tag, attrs in tags.tags
                 if tag == "meta" and attrs.get("http-equiv") == "Content-Security-Policy"]
    policy = {directive: sources for directive, *sources in (part.split() for part in content.split(";"))}
    assert policy["default-src"] == policy["script-src"] == ["'self'"] and policy["object-src"] == ["'none'"]
    for directive, sources in policy.items():
        extra = {source for source in sources if source not in ("'self'", "'none'")}
        assert extra <= ALLOWED.get(directive, set()), (directive, extra)
    assert tags.inline == [] and not [tag for tag, _ in tags.tags if tag == "style"]
    for tag, attrs in tags.tags:
        assert not [key for key in attrs if key.startswith("on") or key == "style"], (tag, attrs)
        if tag in ("link", "img", "script", "source"):
            value = attrs.get("href") or attrs.get("src") or attrs.get("srcset")
            if value and tag != "link" or (tag == "link" and attrs.get("rel") in ("stylesheet", "icon", "apple-touch-icon")):
                assert value.startswith("/") and (WEB / value.split("?")[0].removeprefix("/")).is_file(), value


@pytest.mark.parametrize("page", PAGES, ids=name)
def test_internal_links_reach_pages_and_anchors(page):
    tags = parsed(page)
    home_ids = parsed(WEB / "index.html").ids
    for tag, attrs in tags.tags:
        href = attrs.get("href") if tag == "a" else None
        if not href or href.startswith(("mailto:", "tel:", "https://peyrev.com/indir/")):
            continue
        assert not href.startswith(("http:", "//", "javascript:")), href
        if href.startswith("https://"):
            continue
        path, _, anchor = href.partition("#")
        if not path:
            assert anchor in tags.ids or not anchor, (name(page), href)
            continue
        if path.startswith(DYNAMIC):
            continue
        target = WEB / path.removeprefix("/")
        exists = (target / "index.html").is_file() if path.endswith("/") else target.is_file()
        assert exists, (name(page), href)
        if anchor and path == "/":
            assert anchor in home_ids, (name(page), href)


def script(name: str) -> str:
    return re.sub(r"/\*.*?\*/", "", (WEB / "assets" / name).read_text(encoding="utf-8"), flags=re.DOTALL)


@pytest.mark.parametrize("page", PAGES, ids=name)
def test_every_page_counts_its_visit_without_cookies(page):
    assert ("script", {"src": f"/assets/ziyaret.js?v={site.stamp('ziyaret.js')}", "defer": None}) in parsed(page).tags


def test_the_visit_script_sends_only_the_path_and_the_referring_site():
    code = script("ziyaret.js")
    assert re.findall(r"sendBeacon\('([^']+)'", code) == ["/api/ziyaret"]
    assert "JSON.stringify({ yol: location.pathname, kaynak: source })" in code
    assert "location.hostname !== 'peyrev.com'" in code
    for forbidden in ("cookie", "localStorage", "sessionStorage", "fetch(", "userAgent", "innerHTML", "http"):
        assert forbidden not in code, forbidden


def test_the_contact_script_writes_text_only_and_posts_only_the_form():
    code = script("iletisim.js")
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "localStorage", "http"):
        assert forbidden not in code, forbidden
    assert "const ENDPOINT = '/api/iletisim';" in code and re.findall(r"fetch\(([^,]+),", code) == ["ENDPOINT"]
    assert "credentials: 'omit'" in code
    assert re.search(r"const SENT = \['ad', 'eposta', 'konu', 'mesaj', 'web_sitesi'\];", code)
    form = parsed(WEB / "iletisim" / "index.html")
    names = {attrs["name"] for tag, attrs in form.tags if tag in ("input", "select", "textarea") and "name" in attrs}
    assert names == {"ad", "eposta", "konu", "mesaj", "web_sitesi"}
    options = [attrs["value"] for tag, attrs in form.tags if tag == "option"]
    from peyrev import iletisim
    assert options == list(iletisim.TOPICS)


TEMPLATES = sorted((WEB / "sablonlar").glob("*.json"))


def test_the_templates_are_importable_flows_and_each_is_offered_on_the_page():
    page = (WEB / "sablonlar" / "index.html").read_text(encoding="utf-8")
    assert len(TEMPLATES) == 6
    for template in TEMPLATES:
        flow = json.loads(template.read_text(encoding="utf-8"))
        # Studio's import replaces identity and timestamps; the published file carries none of them.
        assert set(flow) == {"name", "description", "department", "steps"}, template.name
        assert flow["steps"] and all({"id", "title", "action", "params"} <= set(step) for step in flow["steps"])
        assert template.stat().st_size < 16_000
        assert f'href="/sablonlar/{template.name}" download="peyrev-{template.name}"' in page
        assert (WEB / "assets" / "ekran" / "akis" / template.with_suffix(".webp").name).is_file()
