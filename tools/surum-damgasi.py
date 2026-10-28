"""Statik sayfalardaki /assets/*.css ve *.js adreslerine içerik özetini (?v=) yazar: dosya değişince adres de değişir,
tarayıcı 7 gün sakladığı eski kopyayı bırakır. Stil ya da betik değişince çalıştırın: python tools/surum-damgasi.py
(tests/test_site.py damgaların güncel olduğunu denetler)."""

import hashlib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAGES = [ROOT / "web" / "index.html", ROOT / "web" / "egitim" / "index.html", ROOT / "web" / "topluluk" / "index.html",
         ROOT / "deploy" / "indir" / "index.html"]
ASSET = re.compile(r'(/assets/([\w.-]+\.(?:css|js)))(?:\?v=[0-9a-f]+)?"')


def stamp(name: str) -> str:
    return hashlib.sha256((ROOT / "web" / "assets" / name).read_bytes()).hexdigest()[:10]


def main() -> None:
    for page in PAGES:
        text = page.read_text(encoding="utf-8")
        updated = ASSET.sub(lambda match: f'{match.group(1)}?v={stamp(match.group(2))}"', text)
        if updated != text:
            page.write_text(updated, encoding="utf-8")
            print("güncellendi:", page.relative_to(ROOT))


if __name__ == "__main__":
    main()
