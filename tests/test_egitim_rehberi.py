"""Adım rehberi (/egitim/adimlar/) ve deneme akışları (/egitim/deneme/): sayfalar Studio'dan gelen veriyle güncel,
her adımın kendi çapası ve en az bir denemesi var, indirilen dosyalar yerinde, deneme sayfası akışların aradığı
hedefleri taşıyor (tools/egitim.py, Studio'da scripts/egitim_icerigi.py)."""

from __future__ import annotations

import importlib.util
import json
import re
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
spec = importlib.util.spec_from_file_location("egitim_tool", ROOT / "tools" / "egitim.py")
egitim = importlib.util.module_from_spec(spec)
spec.loader.exec_module(egitim)
site_spec = importlib.util.spec_from_file_location("site_tool", ROOT / "tools" / "site.py")
site = importlib.util.module_from_spec(site_spec)
site_spec.loader.exec_module(site)

ADIMLAR, AKISLAR = egitim.yukle()
TRIAL_PAGE = (WEB / "egitim" / "deneme" / "web" / "index.html").read_text(encoding="utf-8")
TRIAL_SCRIPT = (WEB / "assets" / "deneme.js").read_text(encoding="utf-8")


def test_pages_are_generated_from_the_current_data():
    stale = [str(path.relative_to(ROOT)) for path, text in egitim.sayfalar().items()
             if not path.exists() or site.render(path, text) != path.read_text(encoding="utf-8")]
    assert not stale, f"Eski sayfalar: {stale}. python tools/egitim.py && python tools/site.py çalıştırın."


def test_every_step_has_its_anchor_and_a_trial_or_an_exercise():
    numbers = [step["numara"] for step in ADIMLAR["adimlar"]]
    assert numbers == sorted(set(numbers)) and len(numbers) >= 100
    flows = {akis["slug"] for akis in AKISLAR}
    for step in ADIMLAR["adimlar"]:
        page = (WEB / "egitim" / "adimlar" / egitim.slug(step["kategori"]) / "index.html").read_text(encoding="utf-8")
        assert f'id="adim-{step["numara"]}"' in page, step["tur"]
        assert step["akislar"] or step["alistirma"], step["tur"]
        assert set(step["akislar"]) <= flows


def test_flow_files_and_zips_are_published():
    files = {path.stem for path in (WEB / "egitim" / "deneme" / "akislar").glob("*.json")}
    assert files == {akis["slug"] for akis in AKISLAR}
    for akis in AKISLAR:
        data = json.loads((WEB / "egitim" / "deneme" / "akislar" / f"{akis['slug']}.json").read_text(encoding="utf-8"))
        assert data["name"] == akis["ad"] and data["steps"] and data["notes"]
        assert akis["onceki"] is None or akis["onceki"] in files
    with zipfile.ZipFile(WEB / "egitim" / "deneme" / egitim.AKISLAR_ZIP) as archive:
        assert sorted(archive.namelist()) == sorted(f"peyrev-deneme-{slug}.json" for slug in files)
    with zipfile.ZipFile(WEB / "egitim" / "deneme" / egitim.DOSYALAR_ZIP) as archive:
        names = set(archive.namelist())
    assert {"fatura.pdf", "e-fatura.xml", "e-irsaliye.xml", "e-belgeler.zip", "teklif-sablonu.docx",
            "musteriler.xlsx", "hesap.xlsx", "deneme.db", "BENIOKU.txt"} <= names


def web_targets():
    def walk(steps):
        for step in steps:
            if step["action"].startswith("web.") and "target" in step["params"]:
                yield step["params"]["by"], step["params"]["target"]
            yield from walk(step["children"])
            yield from walk(step["otherwise"])

    for akis in AKISLAR:
        data = json.loads((WEB / "egitim" / "deneme" / "akislar" / f"{akis['slug']}.json").read_text(encoding="utf-8"))
        if any(step["action"] == "web.open" and "/egitim/deneme/web/" in step["params"]["url"]
               for step in data["steps"]):
            yield from walk(data["steps"])


def test_the_trial_page_has_every_target_the_flows_look_for():
    targets = list(web_targets())
    assert len(targets) >= 10
    for by, target in targets:
        if "${" in target or not target:
            continue  # filled in at run time from the flow's own data
        if by == "css":
            assert f'id="{target.removeprefix("#")}"' in TRIAL_PAGE, target
        elif by == "placeholder":
            assert f'placeholder="{target}"' in TRIAL_PAGE, target
        else:  # a visible text: in the page, or written by the page's script (the delayed report)
            assert (re.search(rf">\s*{re.escape(target)}\b", TRIAL_PAGE)
                    or f'"{target}"' in TRIAL_SCRIPT), (by, target)
    assert 'href="/egitim/deneme/peyrev-deneme-dosyalari.zip"' in TRIAL_PAGE


def test_the_trial_page_script_writes_text_only():
    assert not [word for word in ("innerHTML", "insertAdjacentHTML", "fetch(", "XMLHttpRequest") if word in TRIAL_SCRIPT]
