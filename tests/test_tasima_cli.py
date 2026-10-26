"""Taşıma çalıştırıcısı (python -m peyrev.tasima, deploy/tasima.sh): hedef adresinin db.env'den okunması, provanın
gerçek veritabanına yönelmemesi, çıkış kodları ve raporda kişisel veri bulunmaması."""

import io
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest
from conftest import START, make_user
from orkestrai_source import Orkestrai, hex_id, password_hash
from test_tasima import company, rows

from peyrev import users
from peyrev.tasima.__main__ import main
from peyrev.tasima.source import SourceError

ROOT = Path(__file__).resolve().parents[1]
DB_PY = ("import os\n\nDB_CONFIG = {\n    'host': os.getenv('ORKESTRAI_DB_HOST', '127.0.0.1'),\n"
         "    'database': 'orke_OrkestraDB',\n    'user': 'orke_kullanici',\n    'password': 'gizli-parola-123',\n"
         "    'autocommit': True\n}\n")
SETTINGS = {"host": "127.0.0.1", "database": "orke_OrkestraDB", "user": "orke_kullanici", "password": "gizli-parola-123",
            "charset": "utf8mb4", "collation": "utf8mb4_general_ci"}
UNREACHABLE = "postgresql://kimse@127.0.0.1:1/ulasilmaz"


@pytest.fixture
def source():
    return Orkestrai()


@pytest.fixture
def db_py(tmp_path):
    path = tmp_path / "db.py"
    path.write_text(DB_PY, encoding="utf-8")
    return path


def secrets(tmp_path, **values):
    """/etc/peyrev karşılığı: db.env (kurulum betiğinin yazdığı anahtarlarla)."""
    folder = tmp_path / "peyrev"
    folder.mkdir(exist_ok=True)
    (folder / "db.env").write_text("".join(f'{key}="{value}"\n' for key, value in values.items()), encoding="utf-8")
    return {"PEYREV_SECRETS_DIR": str(folder)}


def run(mode, db_py, environ, source=None, factory=None):
    out, seen = io.StringIO(), []

    def connect(settings):
        seen.append(settings)
        return source

    code = main(["--hedef", mode, "--kaynak-ayar", str(db_py)], environ=environ, source_factory=factory or connect,
                clock=lambda: START, out=out)
    return code, out.getvalue(), seen


def test_the_runner_takes_both_targets_from_db_env(core, source, prova_url, tmp_path, db_py):
    company(source)
    environ = secrets(tmp_path, DATABASE_URL=core.db.url, PROVA_DATABASE_URL=prova_url)
    code, text, seen = run("prova", db_py, environ, source)
    assert code == 0 and "kip prova, hedef veritabanı peyrev_tasima_prova" in text, text
    assert text.endswith("SONUÇ: başarılı (prova); bütün tablolarda sayılar ve denetimler uyuşuyor, veri yazıldı.\n")
    assert seen == [SETTINGS] and rows(core.db, "kullanicilar") == []   # the prova leaves the real database alone
    code, text, _ = run("gercek", db_py, environ, source)
    assert code == 0 and "kip gercek, hedef veritabanı peyrev_test" in text, text
    assert len(rows(core.db, "kullanicilar")) == 6
    # An address in the environment comes first; a target that cannot be reached is a failure without the address.
    code, text, _ = run("prova", db_py, {**environ, "PEYREV_PROVA_DATABASE_URL": UNREACHABLE}, source)
    assert code == 1 and "Hata: PostgreSQL: veritabanına bağlanılamadı ya da bağlantı koptu" in text
    assert "kimse" not in text and "ulasilmaz" not in text
    # orkestrai's own host override is honoured when the settings are read.
    _, _, seen = run("prova", db_py, {**environ, "ORKESTRAI_DB_HOST": "db.yerel"}, source)
    assert seen[0]["host"] == "db.yerel"


def test_a_prova_never_runs_on_the_real_database(core, source, tmp_path, db_py):
    company(source)
    make_user(core, "kalan@peyrev.test", user_id=900)
    code, text, seen = run("prova", db_py, secrets(tmp_path, DATABASE_URL=core.db.url, PROVA_DATABASE_URL=core.db.url),
                           source)
    assert (code, text) == (1, "Taşıma yapılmadı: PROVA_DATABASE_URL gerçek veritabanını gösteriyor; prova yapılmadı.\n")
    assert seen == [] and [row["id"] for row in rows(core.db, "kullanicilar")] == [900]   # nothing read, nothing dropped
    for address, message in (("postgresql://kimse@127.0.0.1:5432/", ("PROVA_DATABASE_URL veritabanı adı içermiyor; prova "
                                                                    "yapılmadı.")),
                             ("bozuk=adres sifre=gizli", "PROVA_DATABASE_URL geçerli bir PostgreSQL adresi değil.")):
        code, text, _ = run("prova", db_py, secrets(tmp_path, DATABASE_URL=core.db.url, PROVA_DATABASE_URL=address),
                            source)
        assert (code, text) == (1, f"Taşıma yapılmadı: {message}\n")


def test_runner_errors_say_what_is_missing_and_exit_with_one(tmp_path, db_py, source):
    folder = tmp_path / "bos"
    code, text, _ = run("gercek", db_py, {"PEYREV_SECRETS_DIR": str(folder)}, source)
    assert (code, text) == (1, (f"Taşıma yapılmadı: Hedef adresi bulunamadı: {folder / 'db.env'} içinde DATABASE_URL ya da"
                               " ortamda PEYREV_DATABASE_URL gerekli.\n"))
    code, text, _ = run("gercek", tmp_path / "yok.py", {"PEYREV_DATABASE_URL": UNREACHABLE}, source)
    assert (code, text) == (1, "Taşıma yapılmadı: Kaynak ayar dosyası (db.py) okunamadı ya da ayrıştırılamadı.\n")

    def refused(_settings):
        raise SourceError("Kaynak veritabanına bağlanılamadı (MySQL hata 2003).")

    code, text, _ = run("gercek", db_py, {"PEYREV_DATABASE_URL": UNREACHABLE}, factory=refused)
    assert (code, text) == (1, "Taşıma yapılmadı: Kaynak veritabanına bağlanılamadı (MySQL hata 2003).\n")

    def broken(_settings):
        raise ValueError("satır değeri: gizli@ornek.test")

    code, text, _ = run("gercek", db_py, {"PEYREV_DATABASE_URL": UNREACHABLE}, factory=broken)
    assert code == 1 and text.startswith("Taşıma yapılmadı: beklenmeyen hata ValueError (test_tasima_cli.py:")
    assert "gizli" not in text
    with pytest.raises(SystemExit) as caught:
        main(["--hedef", "deneme", "--kaynak-ayar", str(db_py)], environ={}, out=io.StringIO())
    assert caught.value.code == 2


def test_deploy_script_runs_the_module_with_these_options(tmp_path):
    script = (ROOT / "deploy" / "tasima.sh").read_text(encoding="utf-8")
    assert '-m peyrev.tasima \\\n  --hedef "$KIP" --kaynak-ayar "$SOURCE"' in script
    assert "PEYREV_SECRETS_DIR=/etc/peyrev" in script and "runuser -u peyrev" in script
    environ = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "PEYREV_SECRETS_DIR": str(tmp_path)}
    command = [sys.executable, "-m", "peyrev.tasima", "--hedef", "prova", "--kaynak-ayar", str(tmp_path / "db.py")]
    finished = subprocess.run(command, capture_output=True, text=True, env=environ, timeout=60, check=False)
    assert (finished.returncode, finished.stdout) == (
        1, "Taşıma yapılmadı: Kaynak ayar dosyası (db.py) okunamadı ya da ayrıştırılamadı.\n")
    command[4] = "deneme"
    finished = subprocess.run(command, capture_output=True, text=True, env=environ, timeout=60, check=False)
    assert finished.returncode == 2 and "--hedef" in finished.stderr


# ----- kişisel veri --------------------------------------------------------------------------------------------------

EMAIL = "ozel.kisi@gizli-alan.test"
PHONE, ADDRESS, PASSWORD = "+90 555 010 77 88", "203.0.113.99", "Parola.Gizli-77"


def private(source):
    """Bütün metin kolonlarında kişisel bilgi (hepsi 'gizli' içerir; telefon, adres ve şifre ayrıca aranır)."""
    office, task, approval = hex_id("ofis", "gizli"), hex_id("gorev", "gizli"), hex_id("onay", "gizli")
    source.firm(2, modules={users.RPA: None, users.AGENTS: None})
    source.add("SYS_AjanOrganizasyonlari", FirmaID=2)
    source.user(3, 2, EMAIL, AdSoyad="Gizemli Gizlioğlu", modules={users.RPA: None, users.AGENTS: None},
                password=PASSWORD)
    source.user(4, 2, " bosluk.gizli@gizli-alan.test ", AdSoyad="Boşluk Gizli", modules={users.RPA: None})
    source.user(5, 2, "yabanci.gizli@gizli-alan.test", AdSoyad="Yabancı Gizli", modules={users.RPA: None})
    source.find("SYS_Kullanicilar", KullaniciID=5)["SifreHash"] = "md5$gizlituz$gizliozet"
    source.office(office, 3, "Gizli Oda", members={3: datetime(2026, 2, 1, 8, 0)})
    source.add("SYS_AjanEkipGorevleri", ID=task, OfisID=office, OlusturanID=3, AtananID=3, IstekID=hex_id("istek", 9),
               Baslik="Gizli görev başlığı", Talimat="Gizli talimat metni", SonucOzeti="Gizli sonuç", Durum="succeeded")
    source.add("SYS_AjanEkipOnaylari", ID=approval, OfisID=office, IsteyenID=3, OnayciID=3, OturumID=hex_id("o", 9),
               IstekID=hex_id("onay-istek", 9), CalismaID=hex_id("c", 9), AkisID=hex_id("a", 9),
               Onizleme=json.dumps({"alan": "gizli önizleme"}), Gerekce="Gizli gerekçe metni", Durum="rejected")
    source.event(hex_id("olay", "gizli"), "task", task, 3, "task_finished", {"not": "Olaydaki gizli ayrıntı"})
    source.add("SYS_RpaTalepler", TalepNo=1001, KullaniciID=3, Baslik="Gizli talep başlığı", AdimTuru="gizli.adim")
    source.add("SYS_RpaTalepMesajlari", MesajID=hex_id("mesaj", "gizli"), TalepNo=1001, YazanID=3,
               Metin="Mesajdaki gizli bilgi")
    source.add("SYS_RpaTalepEkleri", EkID=hex_id("ek", "gizli"), TalepNo=1001, Ad="gizli-ek.png", Boyut=12,
               Icerik=b"gizli-icerik")
    source.add("SYS_RpaBasvurulari", BasvuruID=hex_id("basvuru", "gizli"), AdSoyad="Başvuran Gizli",
               Eposta="basvuru.gizli@gizli-alan.test", Telefon=PHONE, IP=ADDRESS, SifreHash=password_hash(PASSWORD),
               YoneticiNotu="Yönetici gizli notu")


def test_the_report_carries_no_personal_data(core, source, prova_url, tmp_path, db_py):
    private(source)
    # First a failing run: a Peyrev account holds account 3's login, and rows Peyrev cannot hold carry personal data.
    make_user(core, EMAIL.upper(), user_id=900, ad_soyad="Peyrevli Gizli")
    source.user(6, 2, "cok-uzun-gizli" * 12 + "@gizli-alan.test", modules={users.RPA: None})
    source.office(hex_id("ofis", "kisa"), 4, "G")
    # The event of the rejected office still has a migrated owner (4), so its broken JSON is reported.
    source.event(hex_id("olay", "bozuk"), "office", hex_id("ofis", "kisa"), 4, "office_renamed", '{"gizli": ')
    environ = secrets(tmp_path, DATABASE_URL=core.db.url, PROVA_DATABASE_URL=prova_url)
    failed, failure, _ = run("gercek", db_py, environ, source)
    assert failed == 1 and "kullanicilar 3: giriş adı hedefteki 900 numaralı hesabınkiyle aynı" in failure
    assert "kullanicilar 6: giriş adı (Eposta) 3-150 karakter olmalı" in failure
    assert f"ajan_ofisleri {hex_id('ofis', 'kisa')}: ad 2-120 karakter olmalı" in failure
    assert f"ajan_olaylari {hex_id('olay', 'bozuk')}: kimlik, aktör, olay, ayrıntı (JSON) ya da zaman geçersiz" in failure
    # Then the same data without those rows, in both modes; the notes about logins and hashes name ids only.
    with core.db.transaction() as cur:
        cur.execute("DELETE FROM kullanicilar WHERE id = 900")
    source.drop("SYS_Kullanicilar", KullaniciID=6)
    source.drop("SYS_KullaniciModulleri", KullaniciID=6)
    source.drop("SYS_AjanOfisleri", OfisID=hex_id("ofis", "kisa"))
    source.drop("SYS_AjanEkipOlaylari", ID=hex_id("olay", "bozuk"))
    real, success, _ = run("gercek", db_py, environ, source)
    prova, rehearsal, _ = run("prova", db_py, environ, source)
    assert (real, prova) == (0, 0), success + rehearsal
    assert "kullanicilar: giriş adının başında ya da sonunda boşluk olan hesaplar: 4" in success
    assert "kullanicilar: şifre özeti Werkzeug biçiminde olmayan hesaplar: 5" in success
    for text in (failure, success, rehearsal):
        assert "gizli" not in text.lower()
        for value in (PHONE, ADDRESS, PASSWORD, password_hash(PASSWORD), "pbkdf2:", "md5$", "Gizemli", "Saklı",
                      "ozel.kisi", "orke_kullanici", core.db.url, prova_url):
            assert value not in text, value
