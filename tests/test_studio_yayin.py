"""Studio yayını (.github/scripts/studio-yayin.py): sunucuda çalışan kod iki bildirimi ayrı yayımlar. guncelleme.manifest
(1.1.0 ve sonrası) indirme sayfasıyla gelir; stable.manifest (1.0.x) sayfasız gelir. Aynı sürümün iki arşivi aynı
releases/<sürüm> klasörünü paylaşır: ortak paket aynı bayt olmalıdır, yayımlanmış hiçbir dosya değişmez."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import io
import json
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / ".github/scripts/studio-yayin.py"
SPEC = importlib.util.spec_from_file_location("studio_yayin", SCRIPT)
studio = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(studio)


@pytest.fixture
def remote(tmp_path):
    """The remote program with its fixed root moved into a temporary folder."""
    namespace = {"__name__": "remote"}
    exec(compile(studio.REMOTE_CODE, "remote", "exec"), namespace)  # noqa: S102 - the script's own constant
    namespace["ROOT"] = tmp_path / "indir"
    namespace["ROOT"].mkdir()
    return namespace


def manifest(version: str, packages: dict[str, bytes]) -> bytes:
    now = datetime.now(UTC).replace(microsecond=0)
    stamp = lambda moment: moment.isoformat().replace("+00:00", "Z")
    types = {"exe": "windows-x64", "dmg": "macos-arm64"}
    assets = {types[name.rsplit(".", 1)[1]]: {
        "url": f"https://peyrev.com/indir/releases/{version}/{name}", "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(), "type": name.rsplit(".", 1)[1]} for name, data in packages.items()}
    payload = json.dumps({"schema": 1, "channel": "stable", "version": version, "published_at": stamp(now),
                          "expires_at": stamp(now + timedelta(days=30)), "assets": assets}).encode()
    return json.dumps({"payload": base64.b64encode(payload).decode(),
                       "signature": base64.b64encode(b"s" * 64).decode()}).encode()


def bundle(path: Path, version: str, feed: str, packages: dict[str, bytes], *, page: bool | None = None) -> Path:
    files = {f"releases/{version}/{name}": data for name, data in packages.items()}
    files[feed] = manifest(version, packages)
    if page if page is not None else feed == "guncelleme.manifest":
        files["index.html"] = b"<!doctype html><title>Peyrev</title>"
    with tarfile.open(path, "w:gz") as archive:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return path


def publish(remote, path: Path, version: str) -> dict:
    token = "a" * 32
    upload = remote["ROOT"] / f".upload-{token}.tar.gz"
    upload.write_bytes(path.read_bytes())
    return remote["publish"](version, hashlib.sha256(path.read_bytes()).hexdigest(), token)


WINDOWS = {"Peyrev-Setup-1.1.0-Windows-x64.exe": b"installer"}
DOWNLOAD = {**WINDOWS, "Peyrev-1.1.0-macOS-arm64.dmg": b"download image"}
UPDATE = {**WINDOWS, "Peyrev-1.1.0-macOS-arm64-guncelleme.dmg": b"update image"}
OLD = {"Peyrev-Setup-1.0.0-Windows-x64.exe": b"old", "Peyrev-1.0.0-macOS-arm64.dmg": b"old image"}


def test_both_bundles_of_a_version_share_its_folder_and_each_publishes_only_its_feed(remote, tmp_path):
    root = remote["ROOT"]
    result = publish(remote, bundle(tmp_path / "gecis.tar.gz", "1.1.0", "stable.manifest", UPDATE), "1.1.0")
    assert result["feed"] == "stable.manifest" and not (root / "index.html").exists()
    assert remote["current_version"]("stable.manifest") == "1.1.0" and remote["current_version"]() is None
    result = publish(remote, bundle(tmp_path / "peyrev.tar.gz", "1.1.0", "guncelleme.manifest", DOWNLOAD), "1.1.0")
    assert result["feed"] == "guncelleme.manifest" and (root / "index.html").is_file()
    assert sorted(item.name for item in (root / "releases" / "1.1.0").iterdir()) == sorted({**DOWNLOAD, **UPDATE})
    assert remote["inspect_root"]()["transition_version"] == remote["inspect_root"]()["current_version"] == "1.1.0"


def test_a_package_both_bundles_carry_must_be_the_same_bytes(remote, tmp_path):
    publish(remote, bundle(tmp_path / "gecis.tar.gz", "1.1.0", "stable.manifest", UPDATE), "1.1.0")
    changed = {**DOWNLOAD, "Peyrev-Setup-1.1.0-Windows-x64.exe": b"another installer"}
    with pytest.raises(ValueError, match="immutable"):
        publish(remote, bundle(tmp_path / "peyrev.tar.gz", "1.1.0", "guncelleme.manifest", changed), "1.1.0")
    assert (remote["ROOT"] / "releases/1.1.0/Peyrev-Setup-1.1.0-Windows-x64.exe").read_bytes() == b"installer"
    assert not (remote["ROOT"] / "guncelleme.manifest").exists()


@pytest.mark.parametrize("feed, page", [("stable.manifest", True), ("guncelleme.manifest", False)])
def test_the_page_comes_only_with_the_current_feed(remote, tmp_path, feed, page):
    with pytest.raises(ValueError, match="page or manifest"):
        publish(remote, bundle(tmp_path / "b.tar.gz", "1.1.0", feed, DOWNLOAD, page=page), "1.1.0")


def test_a_bundle_holds_exactly_one_manifest(remote, tmp_path):
    path = bundle(tmp_path / "b.tar.gz", "1.1.0", "guncelleme.manifest", DOWNLOAD)
    with tarfile.open(tmp_path / "two.tar.gz", "w:gz") as archive:
        with tarfile.open(path) as source:
            for member in source:
                archive.addfile(member, source.extractfile(member))
        data = manifest("1.1.0", DOWNLOAD)
        info = tarfile.TarInfo("stable.manifest")
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
    with pytest.raises(ValueError, match="exactly one manifest"):
        publish(remote, tmp_path / "two.tar.gz", "1.1.0")


def test_prune_keeps_every_version_a_feed_names(remote, tmp_path):
    publish(remote, bundle(tmp_path / "old.tar.gz", "1.0.0", "stable.manifest",
                           OLD), "1.0.0")
    publish(remote, bundle(tmp_path / "gecis.tar.gz", "1.1.0", "stable.manifest", UPDATE), "1.1.0")
    publish(remote, bundle(tmp_path / "peyrev.tar.gz", "1.2.0", "guncelleme.manifest",
                           {"Peyrev-Setup-1.2.0-Windows-x64.exe": b"new", "Peyrev-1.2.0-macOS-arm64.dmg": b"n"}), "1.2.0")
    with pytest.raises(ValueError, match="current feed"):
        remote["prune"]("1.0.0")
    result = remote["prune"]("1.2.0")
    assert result["kept"] == ["1.1.0", "1.2.0"] and result["removed"] == ["1.0.0"]
    assert remote["published_versions"]() == ["1.1.0", "1.2.0"]


def test_a_feed_never_goes_back_to_an_older_version(remote, tmp_path):
    publish(remote, bundle(tmp_path / "gecis.tar.gz", "1.1.0", "stable.manifest", UPDATE), "1.1.0")
    with pytest.raises(ValueError, match="older stable version"):
        publish(remote, bundle(tmp_path / "old.tar.gz", "1.0.0", "stable.manifest",
                               OLD), "1.0.0")
    # The other feed starts on its own: its first release may be the same version.
    publish(remote, bundle(tmp_path / "peyrev.tar.gz", "1.1.0", "guncelleme.manifest", DOWNLOAD), "1.1.0")
