"""Dijital İkiz dağıtım iş akışı (.github/workflows/ikiz.yml): sunucu işleriyle aynı sıraya girer, gizli depoyu yalnız
okuma anahtarıyla çeker, gizli kodun testini bu açık depoda çalıştırmaz ve kullanıcı girdisini kabuk komutuna koymaz."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AKIS = (ROOT / ".github/workflows/ikiz.yml").read_text(encoding="utf-8")


def test_runs_in_the_server_queue_and_reports_encrypted():
    assert "group: peyrev-sunucu" in AKIS and "cancel-in-progress: false" in AKIS
    assert "python .github/scripts/uzak.py" in AKIS and "BETIK: deploy/ikiz-dagit.sh" in AKIS


def test_private_code_is_fetched_read_only_and_never_tested_here():
    assert "repository: k0rigi/peyrev-ikiz" in AKIS
    assert "ssh-key: ${{ secrets.IKIZ_DEPLOY_KEY }}" in AKIS
    assert AKIS.count("persist-credentials: false") == 2
    assert "pytest" not in AKIS


def test_inputs_never_reach_a_shell_command():
    for blok in re.findall(r"run: \|\n((?:\s{10}.*\n)+)", AKIS) + re.findall(r"run: (.+)", AKIS):
        assert "${{ inputs" not in blok and "${{ github.event" not in blok
