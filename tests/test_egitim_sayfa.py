"""peyrev.com/egitim/ sayfası: CSP yalnız YouTube'un küçük resim ve nocookie oynatıcı adreslerine açılır, betik
sunucu verisini yalnız metin olarak yazar ve video kimliğini doğrulamadan adres kurmaz."""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / "web"
HTML = (WEB / "egitim" / "index.html").read_text(encoding="utf-8")
SCRIPT = (WEB / "assets" / "egitim.js").read_text(encoding="utf-8")


class Tags(HTMLParser):
    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.tags, self.inline, self._open = [], [], None
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))
        self._open = tag if tag in ("script", "style") else None

    def handle_endtag(self, tag):
        self._open = None

    def handle_data(self, data):
        if self._open and data.strip():
            self.inline.append(data)


PAGE = Tags(HTML)


def policy():
    [content] = [attrs["content"] for tag, attrs in PAGE.tags
                 if tag == "meta" and attrs.get("http-equiv") == "Content-Security-Policy"]
    return {name: sources for name, *sources in (part.split() for part in content.split(";"))}


def test_the_page_opens_only_youtube_thumbnails_and_the_nocookie_player():
    rules = policy()
    assert rules["default-src"] == rules["script-src"] == rules["style-src"] == rules["connect-src"] == ["'self'"]
    assert rules["img-src"] == ["'self'", "https://i.ytimg.com"]
    assert rules["frame-src"] == ["https://www.youtube-nocookie.com"]
    assert rules["object-src"] == rules["base-uri"] == ["'none'"]


def test_the_page_has_no_inline_code_and_its_files_exist():
    assert PAGE.inline == [] and not [tag for tag, _ in PAGE.tags if tag == "style"]
    assert [{**attrs, "src": attrs["src"].split("?")[0]} for tag, attrs in PAGE.tags if tag == "script"] == [
        {"src": "/assets/egitim.js", "defer": None}]
    for tag, attrs in PAGE.tags:
        assert not [name for name in attrs if name.startswith("on") or name == "style"], tag
        for name in ("src", "href"):
            value = attrs.get(name, "")
            if tag in ("link", "img", "script") and value:
                assert (WEB / value.split("?")[0].removeprefix("/")).is_file(), value


def test_the_script_writes_text_and_builds_addresses_only_from_checked_ids():
    code = re.sub(r"/\*.*?\*/", "", SCRIPT, flags=re.DOTALL)
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function",
                      ".style.", "setAttribute('style'", "srcdoc"):
        assert forbidden not in code, forbidden
    assert re.findall(r"fetch\(([^,]+),", code) == ["ENDPOINT"] and "const ENDPOINT = '/api/egitim';" in code
    assert "credentials: 'omit'" in code
    assert "const VIDEO = /^[A-Za-z0-9_-]{11}$/;" in code
    assert "data.videolar.filter((video) => video && VIDEO.test(String(video.video)))" in code
    assert set(re.findall(r"https://[a-z.-]+", code)) == {"https://www.youtube-nocookie.com", "https://i.ytimg.com"}
