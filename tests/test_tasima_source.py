"""Taşıma kaynağı: db.py'nin çalıştırılmadan ayrıştırılması, MariaDB'nin tek tutarlı salt okunur okunması ve sahte
kaynağın gerçek tabloların yapısına (kaynak-inceleme raporundaki CREATE TABLE'lar) uyması."""

import sys
import types
from datetime import date, datetime

import pytest
from orkestrai_source import COLUMNS, DEFAULTS, Orkestrai

from peyrev.tasima import source

# Kaynak-inceleme raporundaki tablo yapıları (yalnız taşımanın okuduğu tablolar); ModulKodu SYS_Moduller'den gelir.
DDL = {
    "SYS_Firmalar": "FirmaID int, FirmaAdi varchar, VergiNo varchar, Adres text, Telefon varchar, Durum enum, "
                    "OlusturmaTarihi datetime",
    "SYS_FirmaModulleri": "FirmaID int, ModulID int, BaslangicTarihi date, BitisTarihi date",
    "SYS_KullaniciModulleri": "KullaniciID int, ModulID int, BitisTarihi date",
    "SYS_Kullanicilar": "KullaniciID int, FirmaID int, AdSoyad varchar, Eposta varchar, SifreHash varchar, Rol enum, "
                        "Durum enum, OlusturmaTarihi datetime",
    "SYS_AjanOrganizasyonlari": "FirmaID int, OnayciID int, PolitikaSurumu int, Durum varchar, Olusturma datetime",
    "SYS_RpaBasvurulari": "BasvuruID char, AdSoyad varchar, Eposta varchar, Telefon varchar, SifreHash varchar, "
                          "Paket varchar, Durum varchar, IP varchar, AydinlatmaSurumu varchar, Olusturma datetime, "
                          "KararZamani datetime, KararVerenID int, FirmaID int, KullaniciID int, YoneticiNotu varchar",
    "SYS_RpaTalepler": "TalepNo int, FirmaID int, KullaniciID int, Tur varchar, Baslik varchar, Durum varchar, "
                       "AdimTuru varchar, Surum varchar, Platform varchar, YayinSurumu varchar, SonYazan varchar, "
                       "Olusturma datetime, Guncelleme datetime, KullaniciGordu datetime, YoneticiGordu datetime",
    "SYS_RpaTalepMesajlari": "MesajID char, TalepNo int, YazanID int, Yazan varchar, Metin text, Olusturma datetime",
    "SYS_RpaTalepEkleri": "EkID char, TalepNo int, Tur varchar, Ad varchar, Bicim varchar, Boyut int, "
                          "Icerik mediumblob, Olusturma datetime",
    "SYS_AjanOfisleri": "OfisID char, FirmaID int, Ad varchar, YetkiliID int, Durum varchar, Olusturma datetime",
    "SYS_AjanOfisUyeleri": "FirmaID int, KullaniciID int, OfisID char, Olusturma datetime",
    "SYS_AjanEkipGorevleri": "ID char, FirmaID int, OlusturanID int, AtananID int, IstekID char, IstekOzeti char, "
                             "Baslik varchar, Talimat longtext, Oncelik varchar, Durum varchar, Surum int, "
                             "SonucOzeti text, Olusturma datetime, Baslama datetime, Bitis datetime, "
                             "Guncelleme datetime, OfisID char",
    "SYS_AjanEkipOnaylari": "ID char, FirmaID int, IsteyenID int, OnayciID int, OturumID char, IstekID char, "
                            "IstekOzeti char, CalismaID char, AkisID char, AkisOzeti char, GirdiOzeti char, "
                            "OnizlemeOzeti char, BaglamOzeti char, Onizleme longtext, Gerekce text, Deneme int, "
                            "Durum varchar, Surum int, KararNedeni varchar, Olusturma datetime, Gecerlilik datetime, "
                            "KararZamani datetime, TuketimZamani datetime, OfisID char",
    "SYS_AjanEkipOlaylari": "ID char, FirmaID int, KayitTuru varchar, KayitID char, KullaniciID int, Aktor varchar, "
                            "Olay varchar, Ayrinti text, Olusturma datetime",
    "SYS_RpaOturumlari": "OturumID char, KullaniciID int, FirmaID int, Cihaz varchar, Sira int, Ornek varchar, "
                         "Tekrar int, TekrarVar tinyint, Surum varchar, IP varchar, Olusturma datetime, "
                         "SonGorulme datetime, Durum varchar, KapanmaNedeni varchar",
    "SYS_RpaSifreKodlari": "KodID char, KullaniciID int, KodOzeti char, OlusturanID int, Olusturma datetime, "
                           "Gecerlilik datetime, Kullanildi datetime",
    "SYS_RpaHesapIzleri": "IzID char, Tur varchar, Anahtar varchar, Zaman datetime",
}
PYTHON = {"int": int, "tinyint": int, "varchar": str, "char": str, "text": str, "longtext": str, "enum": str,
          "date": date, "datetime": datetime, "mediumblob": bytes}


def ddl(table):
    return dict(part.split() for part in DDL[table].split(", "))


def test_queries_read_existing_columns_and_the_fake_source_has_their_shape():
    assert set(source.QUERIES) == set(DDL)
    for table, columns in COLUMNS.items():
        known = ddl(table) | ({"ModulKodu": "varchar"} if "ModulID" in ddl(table) else {})
        assert set(columns) <= set(known), table
        for column, value in DEFAULTS.get(table, {}).items():
            assert column in columns, (table, column)
            assert value is None or isinstance(value, PYTHON[known[column]]), (table, column)
    # The JOINed module code replaces ModulID; FirmaID of agent and support tables is not read (no firms in Peyrev).
    assert COLUMNS["SYS_KullaniciModulleri"] == ("KullaniciID", "ModulKodu", "BitisTarihi")
    assert "FirmaID" not in COLUMNS["SYS_AjanOfisleri"] + COLUMNS["SYS_RpaTalepler"] + COLUMNS["SYS_AjanEkipGorevleri"]
    for table in ("SYS_FirmaModulleri", "SYS_KullaniciModulleri"):
        assert "('MOD_RPA', 'MOD_AJAN')" in source.QUERIES[table]
    # Password reset codes are never read, only who has one and until when.
    assert "KodOzeti" not in source.QUERIES["SYS_RpaSifreKodlari"]


def test_the_fake_source_refuses_rows_the_queries_do_not_return():
    fake = Orkestrai()
    with pytest.raises(AssertionError):
        fake.add("SYS_Kullanicilar", KullaniciID=1, ModulID=27)
    fake.add("SYS_Firmalar", FirmaID=1)
    fake.tables["SYS_Firmalar"][0]["FirmaAdi"] = "eklenmiş kolon"
    with pytest.raises(AssertionError):
        fake.read()


# ----- db.py -----------------------------------------------------------------------------------------------------

ORKESTRAI_DB_PY = '''# MySQL bağlantı yöneticisi
import os
import mysql.connector

DB_CONFIG = {
    'host': os.getenv('ORKESTRAI_DB_HOST', '127.0.0.1'),
    'database': 'orke_OrkestraDB',
    'user': 'orke_kullanici',
    'password': 'p@ss$w0rd-gizli',
    'charset': 'utf8mb4',
    'collation': 'utf8mb4_general_ci',
    'autocommit': True
}

def get_connection():
    conn = mysql.connector.connect(**DB_CONFIG)
    return conn
'''


def write(tmp_path, text):
    path = tmp_path / "db.py"
    path.write_text(text, encoding="utf-8")
    return path


def test_settings_come_from_orkestrai_db_py_like_orkestrai_reads_them(tmp_path):
    path = write(tmp_path, ORKESTRAI_DB_PY)
    assert source.read_settings(path, {}) == {
        "host": "127.0.0.1", "database": "orke_OrkestraDB", "user": "orke_kullanici", "password": "p@ss$w0rd-gizli",
        "charset": "utf8mb4", "collation": "utf8mb4_general_ci"}
    # The same environment override orkestrai honours; autocommit is left out (the reader opens its own transaction).
    assert source.read_settings(path, {"ORKESTRAI_DB_HOST": "db.yerel"})["host"] == "db.yerel"
    # Without charset/collation the MariaDB-safe defaults are used (Connector 8.4 would pick utf8mb4_0900_ai_ci).
    text = "DB_CONFIG = {'unix_socket': '/var/lib/mysql/mysql.sock', 'port': '3307', 'user': 'u', 'password': '',"
    settings = source.read_settings(write(tmp_path, text + " 'database': 'd'}\n"), {})
    assert settings == {"unix_socket": "/var/lib/mysql/mysql.sock", "port": 3307, "user": "u", "password": "",
                        "database": "d", "charset": "utf8mb4", "collation": "utf8mb4_general_ci"}


def test_db_py_is_parsed_and_never_run(tmp_path):
    marker = tmp_path / "calisti"
    text = (f"open({str(marker)!r}, 'w').write('x')\nimport sys\nsys.exit(3)\n"
            "DB_CONFIG = {'host': 'h', 'user': 'u', 'password': 'p', 'database': 'd',\n"
            f"             'autocommit': open({str(marker)!r}, 'w')}}\n")
    assert source.read_settings(write(tmp_path, text), {})["host"] == "h"
    assert not marker.exists()
    # A value that would need running code is refused by name, and not evaluated either.
    for value in (f"open({str(marker)!r}, 'w').write('x')", "__import__('os').getenv('X')", "os.getenv(KEY)",
                  "'p' + 'q'", "os.getenv('X', default='y')", "secrets['db']"):
        with pytest.raises(source.SourceError) as caught:
            source.read_settings(write(tmp_path, f"DB_CONFIG = {{'host': 'h', 'password': {value}}}\n"), {})
        assert str(caught.value).startswith("DB_CONFIG['password'] anlaşılamadı")
        assert not marker.exists()


def test_reading_errors_never_echo_the_file(tmp_path):
    secret = "p@ss$w0rd-gizli"
    cases = {
        f"DB_CONFIG = {{'password': '{secret}' ,,}}\n": "okunamadı ya da ayrıştırılamadı",
        f"CONFIG = {{'password': '{secret}'}}\n": "DB_CONFIG sözlüğü bulunamadı",
        f"DB_CONFIG = dict(password='{secret}')\n": "DB_CONFIG sözlüğü bulunamadı",
        f"DB_CONFIG = {{'password': '{secret}', 'port': 'üç'}}\n": "DB_CONFIG['port'] geçersiz",
        f"DB_CONFIG = {{'password': '{secret}', 'host': 'h'}}\n": "eksik ya da boş: user, database",
        "DB_CONFIG = {'user': 'u', 'database': 'd', 'password': os.getenv('YOK')}\n": "eksik ya da boş: host, password",
    }
    for text, message in cases.items():
        with pytest.raises(source.SourceError) as caught:
            source.read_settings(write(tmp_path, text), {})
        assert message in str(caught.value) and secret not in str(caught.value), text
        # No chained exception (a SyntaxError carries the offending line) would be shown with it.
        assert caught.value.__cause__ is None
        assert caught.value.__context__ is None or caught.value.__suppress_context__
    with pytest.raises(source.SourceError):
        source.read_settings(tmp_path / "yok.py", {})


# ----- MariaDB okuma ---------------------------------------------------------------------------------------------


class FakeMysqlError(Exception):
    def __init__(self, errno, message):
        super().__init__(message)
        self.errno = errno


class FakeConnector:
    """mysql.connector yerine: bağlantı ayarlarını ve gönderilen deyimleri kaydeder."""

    Error = FakeMysqlError

    def __init__(self, tables, fail_on=None, refuse=False):
        self.tables, self.fail_on, self.refuse = tables, fail_on, refuse
        self.log, self.options, self.closed = [], None, False

    def connect(self, **options):
        if self.refuse:
            raise FakeMysqlError(1045, "Access denied for user 'orke_kullanici'@'localhost' (using password: YES)")
        self.options = options
        return self

    def cursor(self, dictionary=False):
        assert dictionary is True
        return self

    def execute(self, statement):
        self.log.append(statement)
        if self.fail_on and self.fail_on in statement:
            raise FakeMysqlError(1146, "Table 'orke_OrkestraDB.SYS_RpaTalepler' doesn't exist; değer: gizli@ornek.test")
        table = next((name for name, query in source.QUERIES.items() if query == statement), None)
        self.rows = self.tables.get(table, [])

    def fetchall(self):
        return self.rows

    def rollback(self):
        self.log.append("ROLLBACK")

    def close(self):
        self.closed = True


def install(monkeypatch, connector):
    package = types.ModuleType("mysql")
    module = types.ModuleType("mysql.connector")
    module.connect, module.Error = connector.connect, FakeMysqlError
    package.connector = module
    monkeypatch.setitem(sys.modules, "mysql", package)
    monkeypatch.setitem(sys.modules, "mysql.connector", module)


SETTINGS = {"host": "127.0.0.1", "user": "orke_kullanici", "password": "p@ss$w0rd-gizli", "database": "orke_OrkestraDB",
            "charset": "utf8mb4", "collation": "utf8mb4_general_ci"}


def test_the_source_is_read_in_one_consistent_read_only_transaction(monkeypatch):
    rows = {"SYS_Firmalar": [{"FirmaID": 2, "Durum": "Aktif"}]}
    connector = FakeConnector(rows)
    install(monkeypatch, connector)
    reader = source.MysqlSource(SETTINGS)
    assert repr(reader) == "MysqlSource(<gizli>)" and "gizli-" not in repr(reader)
    tables = reader.read()
    assert tables["SYS_Firmalar"] == [{"FirmaID": 2, "Durum": "Aktif"}]
    assert set(tables) == set(source.QUERIES) and tables["SYS_Kullanicilar"] == []
    assert connector.options == {**SETTINGS, "autocommit": True, "connection_timeout": source.CONNECT_SECONDS}
    assert connector.log == ["SET TRANSACTION ISOLATION LEVEL REPEATABLE READ",
                             "START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY",
                             *source.QUERIES.values(), "ROLLBACK"]
    assert connector.closed
    # Only SELECTs: the server would refuse a write anyway (READ ONLY), and nothing is committed.
    assert all(query.startswith("SELECT ") for query in source.QUERIES.values())


def test_source_errors_name_the_table_and_the_error_number_only(monkeypatch):
    connector = FakeConnector({}, refuse=True)
    install(monkeypatch, connector)
    with pytest.raises(source.SourceError) as caught:
        source.MysqlSource(SETTINGS).read()
    assert str(caught.value) == "Kaynak veritabanına bağlanılamadı (MySQL hata 1045)."
    connector = FakeConnector({}, fail_on="FROM SYS_RpaTalepler ")
    install(monkeypatch, connector)
    with pytest.raises(source.SourceError) as caught:
        source.MysqlSource(SETTINGS).read()
    assert str(caught.value) == "Kaynak tablo okunamadı: SYS_RpaTalepler (MySQL hata 1146)."
    assert "gizli" not in str(caught.value) and connector.closed and "ROLLBACK" not in connector.log
    monkeypatch.setitem(sys.modules, "mysql", None)
    monkeypatch.setitem(sys.modules, "mysql.connector", None)
    with pytest.raises(source.SourceError, match="mysql-connector-python kurulu değil"):
        source.MysqlSource(SETTINGS).read()
