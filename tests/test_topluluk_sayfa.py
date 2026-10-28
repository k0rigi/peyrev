"""peyrev.com/topluluk/ sayfası: sıkı CSP (yalnız aynı köken), satır içi kod yok, betik sunucu verisini yalnız metin
olarak yazar ve yalnız /api/topluluk/ uçlarına gider; arama motorlarına kapalıdır (üyelere özel)."""

from __future__ import annotations

import re
from pathlib import Path

from test_egitim_sayfa import Tags

WEB = Path(__file__).resolve().parents[1] / "web"
HTML = (WEB / "topluluk" / "index.html").read_text(encoding="utf-8")
SCRIPT = (WEB / "assets" / "topluluk.js").read_text(encoding="utf-8")
PAGE = Tags(HTML)


def test_the_page_loads_only_its_own_origin_and_is_not_indexed():
    [content] = [attrs["content"] for tag, attrs in PAGE.tags
                 if tag == "meta" and attrs.get("http-equiv") == "Content-Security-Policy"]
    rules = {name: sources for name, *sources in (part.split() for part in content.split(";"))}
    assert rules["default-src"] == rules["script-src"] == rules["connect-src"] == rules["img-src"] == ["'self'"]
    assert not [source for sources in rules.values() for source in sources if source not in ("'self'", "'none'")]
    assert ("meta", {"name": "robots", "content": "noindex, nofollow"}) in PAGE.tags
    assert PAGE.inline == [] and not [tag for tag, _ in PAGE.tags if tag == "style"]
    assert [{**attrs, "src": attrs["src"].split("?")[0]} for tag, attrs in PAGE.tags if tag == "script"] == [
        {"src": "/assets/topluluk.js", "defer": None}]
    for tag, attrs in PAGE.tags:
        assert not [name for name in attrs if name.startswith("on") or name == "style"], tag
        if tag in ("link", "img", "script"):
            value = attrs.get("href") or attrs.get("src")
            assert (WEB / value.split("?")[0].removeprefix("/")).is_file(), value


def test_the_script_writes_member_content_only_as_text():
    code = re.sub(r"/\*.*?\*/", "", SCRIPT, flags=re.DOTALL)
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function",
                      ".style.", "setAttribute('style'", "srcdoc", "localStorage", "https://", "http://"):
        assert forbidden not in code, forbidden
    assert "const API = '/api/topluluk/';" in code and re.findall(r"fetch\(([^,]+),", code) == ["API + path"]
    assert "credentials: 'same-origin'" in code and "init.headers['X-Peyrev-Topluluk'] = '1';" in code
    helper = re.search(r"function el\(tag, props, children\) \{(.*?)\n  \}\n", code, re.DOTALL).group(1)
    assert "node.textContent = String(value)" in helper and "node.append(child instanceof Node ? child : String(child))" in helper
    # Links built from server data stay inside the site: an anchor, the API or a fixed page.
    for href in re.findall(r"href: ([^,}]+)", code):
        assert re.match(r"('(#|/)|'#konu-' \+ item\.id|url\b)", href.strip()), href
