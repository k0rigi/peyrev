"""Sunucu işleri (.github/workflows/sunucu.yml): her seçeneğin depoda bir betiği vardır ve betikler sözdizimince
geçerlidir."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = (ROOT / ".github/workflows/sunucu.yml").read_text()


def options() -> list[str]:
    block = WORKFLOW.split("options:", 1)[1].split("permissions:", 1)[0]
    return re.findall(r"^\s+- ([a-z0-9-]+)$", block, re.MULTILINE)


def test_every_option_runs_a_script_in_deploy():
    cases = dict(re.findall(r"^\s+([a-z0-9-]+)\)\s+betik=(deploy/[\w.-]+\.sh);", WORKFLOW, re.MULTILINE))
    assert options() and set(options()) == set(cases)
    for script in cases.values():
        assert (ROOT / script).is_file(), script


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash yok")
def test_scripts_parse():
    for script in sorted((ROOT / "deploy").glob("*.sh")):
        subprocess.run(["bash", "-n", str(script)], check=True)

