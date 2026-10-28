"""peyrev.com/hesabim/ betiği: hesap verisini yalnız metin olarak yazar, yalnız /api/uye/ uçlarına ve Studio'yla
ortak şifre ucuna (/api/rpa/hesap/sifre, çerezsiz) gider; sayfa arama motorlarına kapalıdır."""

from __future__ import annotations

import re
from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / "web"
HTML = (WEB / "hesabim" / "index.html").read_text(encoding="utf-8")
SCRIPT = (WEB / "assets" / "hesabim.js").read_text(encoding="utf-8")


def test_the_account_script_writes_text_only_and_calls_only_account_endpoints():
    code = re.sub(r"/\*.*?\*/", "", SCRIPT, flags=re.DOTALL)
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function",
                      ".style.", "localStorage", "http://", "https://"):
        assert forbidden not in code, forbidden
    assert "const API = '/api/';" in code and re.findall(r"fetch\(([^,]+),", code) == ["API + path"]
    assert set(re.findall(r"api\('([a-z]+/[a-z]+)", code)) == {"uye/giris", "uye/hesap", "uye/indirme", "uye/uzatma",
                                                                 "uye/cikis", "rpa/hesap"}
    # The shared password endpoint is called without the session cookie.
    assert "{ anonymous: true }" in code and "credentials: settings.anonymous ? 'omit' : 'same-origin'" in code
    assert '<meta name="robots" content="noindex, nofollow">' in HTML
