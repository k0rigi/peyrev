"""Kaynak: orkestrai'nin MariaDB veritabanı; tek tutarlı, salt okunur okuma.

Bağlantı ayarı orkestrai'nin api/db.py dosyasındaki DB_CONFIG sözlüğünden ast ile okunur: dosya çalıştırılmaz ve içe
aktarılmaz. Değer olarak yalnız sabitler ve os.getenv("AD", "varsayılan") / os.environ.get("AD", "varsayılan")
anlaşılır; ortamda AD varsa o kullanılır (orkestrai'nin kendisi gibi).

Okuma tek işlemdir: REPEATABLE READ ve START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY. Bütün tablolar aynı anın
görüntüsüdür, sunucu her yazmayı reddeder ve işlem sonunda geri alınır. Taşıma kaynağı yalnız
`read() -> {tablo: [satır, ...]}` arayüzünden görür (testler sahte kaynak verir). Satırlar QUERIES'teki MySQL kolon
adlarıyla gelir; modül tablolarında ModulID yerine SYS_Moduller'deki ModulKodu bulunur.

Bağlantı bilgisi, MySQL hata metni ve satır içeriği hiçbir çıktıya yazılmaz; hatada yalnız tablo adı ve MySQL hata
numarası söylenir.
"""

from __future__ import annotations

import ast
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

CONFIG_NAME = "DB_CONFIG"
# Bağlantıya geçen anahtarlar; db.py'deki diğerleri (autocommit gibi) yok sayılır, işlemi araç kendisi açar.
KEYS = ("host", "port", "unix_socket", "user", "password", "database", "charset", "collation")
# MariaDB, Connector/Python 8.4'ün varsayılan harmanlamasını (utf8mb4_0900_ai_ci) tanımaz; orkestrai de bunları verir.
DEFAULTS = {"charset": "utf8mb4", "collation": "utf8mb4_general_ci"}
GETENV = ("os.getenv", "os.environ.get", "getenv")
CONNECT_SECONDS = 15
MODULES = "('MOD_RPA', 'MOD_AJAN')"

QUERIES = {
    "SYS_Firmalar": "SELECT FirmaID, Durum FROM SYS_Firmalar ORDER BY FirmaID",
    "SYS_FirmaModulleri": (
        "SELECT fm.FirmaID, m.ModulKodu, fm.BaslangicTarihi, fm.BitisTarihi FROM SYS_FirmaModulleri fm "
        f"JOIN SYS_Moduller m ON m.ModulID = fm.ModulID WHERE m.ModulKodu IN {MODULES} ORDER BY fm.FirmaID, m.ModulKodu"),
    "SYS_KullaniciModulleri": (
        "SELECT km.KullaniciID, m.ModulKodu, km.BitisTarihi FROM SYS_KullaniciModulleri km "
        f"JOIN SYS_Moduller m ON m.ModulID = km.ModulID WHERE m.ModulKodu IN {MODULES} "
        "ORDER BY km.KullaniciID, m.ModulKodu"),
    "SYS_Kullanicilar": ("SELECT KullaniciID, FirmaID, AdSoyad, Eposta, SifreHash, Rol, Durum, OlusturmaTarihi "
                         "FROM SYS_Kullanicilar ORDER BY KullaniciID"),
    "SYS_AjanOrganizasyonlari": "SELECT FirmaID, Durum FROM SYS_AjanOrganizasyonlari ORDER BY FirmaID",
    "SYS_RpaBasvurulari": ("SELECT BasvuruID, AdSoyad, Eposta, Telefon, SifreHash, Paket, Durum, IP, AydinlatmaSurumu, "
                           "Olusturma, KararZamani, KararVerenID, KullaniciID, YoneticiNotu FROM SYS_RpaBasvurulari "
                           "ORDER BY Olusturma, BasvuruID"),
    "SYS_RpaTalepler": ("SELECT TalepNo, KullaniciID, Tur, Baslik, Durum, AdimTuru, Surum, Platform, YayinSurumu, "
                        "SonYazan, Olusturma, Guncelleme, KullaniciGordu, YoneticiGordu FROM SYS_RpaTalepler "
                        "ORDER BY TalepNo"),
    "SYS_RpaTalepMesajlari": ("SELECT MesajID, TalepNo, YazanID, Yazan, Metin, Olusturma FROM SYS_RpaTalepMesajlari "
                              "ORDER BY TalepNo, Olusturma, MesajID"),
    "SYS_RpaTalepEkleri": ("SELECT EkID, TalepNo, Tur, Ad, Bicim, Boyut, Icerik, Olusturma FROM SYS_RpaTalepEkleri "
                           "ORDER BY TalepNo, EkID"),
    "SYS_AjanOfisleri": "SELECT OfisID, Ad, YetkiliID, Durum, Olusturma FROM SYS_AjanOfisleri ORDER BY OfisID",
    "SYS_AjanOfisUyeleri": "SELECT KullaniciID, OfisID, Olusturma FROM SYS_AjanOfisUyeleri ORDER BY OfisID, KullaniciID",
    "SYS_AjanEkipGorevleri": ("SELECT ID, OfisID, OlusturanID, AtananID, IstekID, IstekOzeti, Baslik, Talimat, Oncelik, "
                              "Durum, Surum, SonucOzeti, Olusturma, Baslama, Bitis, Guncelleme FROM SYS_AjanEkipGorevleri "
                              "ORDER BY ID"),
    "SYS_AjanEkipOnaylari": ("SELECT ID, OfisID, IsteyenID, OnayciID, OturumID, IstekID, IstekOzeti, CalismaID, AkisID, "
                             "AkisOzeti, GirdiOzeti, OnizlemeOzeti, BaglamOzeti, Onizleme, Gerekce, Deneme, Durum, Surum, "
                             "KararNedeni, Olusturma, Gecerlilik, KararZamani, TuketimZamani FROM SYS_AjanEkipOnaylari "
                             "ORDER BY ID"),
    "SYS_AjanEkipOlaylari": ("SELECT ID, KayitTuru, KayitID, KullaniciID, Aktor, Olay, Ayrinti, Olusturma "
                             "FROM SYS_AjanEkipOlaylari ORDER BY ID"),
    # Taşınmayan tablolar: yalnız rapordaki sayılar ve açık şifre kodu olan hesaplar için (kod özeti okunmaz).
    "SYS_RpaOturumlari": "SELECT KullaniciID, Durum FROM SYS_RpaOturumlari",
    "SYS_RpaSifreKodlari": "SELECT KullaniciID, Gecerlilik, Kullanildi FROM SYS_RpaSifreKodlari",
    "SYS_RpaHesapIzleri": "SELECT Tur FROM SYS_RpaHesapIzleri",
}
TABLES = tuple(QUERIES)


class SourceError(Exception):
    """Kaynak okunamadı. Metin bağlantı bilgisi, değer ya da satır içeriği taşımaz."""


class Source(Protocol):
    def read(self) -> dict[str, list[dict[str, Any]]]:
        """TABLES'taki bütün tablolar, aynı anın görüntüsü."""


def read_settings(path: Path | str, environ: Mapping[str, str] = os.environ) -> dict[str, Any]:
    """db.py'deki DB_CONFIG'ten bağlantı ayarları. Dosya çalıştırılmaz; hata metinleri yalnız anahtar adı içerir."""
    try:
        tree = ast.parse(Path(path).read_bytes(), filename="db.py")
    except (OSError, SyntaxError, ValueError, RecursionError, MemoryError):
        # A syntax error carries the offending line, which may be the password: its text is never shown.
        raise SourceError("Kaynak ayar dosyası (db.py) okunamadı ya da ayrıştırılamadı.") from None
    found = None
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
        if any(isinstance(target, ast.Name) and target.id == CONFIG_NAME for target in targets):
            found = node.value
    if not isinstance(found, ast.Dict):
        raise SourceError("db.py içinde DB_CONFIG sözlüğü bulunamadı.")
    settings: dict[str, Any] = dict(DEFAULTS)
    for key, value in zip(found.keys, found.values, strict=True):
        if isinstance(key, ast.Constant) and key.value in KEYS:
            settings[key.value] = _value(value, key.value, environ)
    if "port" in settings:
        port = settings["port"]
        port = int(port) if isinstance(port, str) and port.isdigit() else port
        if type(port) is not int or not 0 < port < 65536:
            raise SourceError("DB_CONFIG['port'] geçersiz.")
        settings["port"] = port
    missing = [name for name in ("user", "database") if not settings.get(name)]
    if not (settings.get("host") or settings.get("unix_socket")):
        missing.insert(0, "host")
    if not isinstance(settings.get("password"), str):
        missing.append("password")
    if missing:
        raise SourceError("DB_CONFIG'te eksik ya da boş: " + ", ".join(missing) + ".")
    return settings


def _value(node: ast.expr, key: str, environ: Mapping[str, str]) -> Any:
    if isinstance(node, ast.Constant) and (isinstance(node.value, str) or type(node.value) is int):
        return node.value
    if (isinstance(node, ast.Call) and _dotted(node.func) in GETENV and not node.keywords and 1 <= len(node.args) <= 2
            and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
        default = _value(node.args[1], key, environ) if len(node.args) == 2 else None
        return environ.get(node.args[0].value, default)
    raise SourceError(f"DB_CONFIG['{key}'] anlaşılamadı: yalnız sabit değer ya da os.getenv(\"AD\", \"varsayılan\") "
                      "okunur.")


def _dotted(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return _dotted(node.value) + "." + node.attr
    return ""


class MysqlSource:
    """orkestrai veritabanı (mysql-connector-python yalnız okuma anında yüklenir). Ayarlar nesnenin dışına çıkmaz."""

    def __init__(self, settings: Mapping[str, Any]):
        self._settings = dict(settings)

    def __repr__(self) -> str:
        return "MysqlSource(<gizli>)"

    def read(self) -> dict[str, list[dict[str, Any]]]:
        try:
            import mysql.connector
        except ImportError:
            raise SourceError("mysql-connector-python kurulu değil (deploy/requirements.lock).") from None
        try:
            connection = mysql.connector.connect(**self._settings, autocommit=True,
                                                 connection_timeout=CONNECT_SECONDS)
        except mysql.connector.Error as error:
            raise SourceError(f"Kaynak veritabanına bağlanılamadı (MySQL hata {error.errno}).") from None
        table = "-"
        try:
            cursor = connection.cursor(dictionary=True)
            cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            cursor.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY")
            tables: dict[str, list[dict[str, Any]]] = {}
            for table, query in QUERIES.items():
                cursor.execute(query)
                tables[table] = [dict(row) for row in cursor.fetchall()]
            connection.rollback()
            return tables
        except mysql.connector.Error as error:
            raise SourceError(f"Kaynak tablo okunamadı: {table} (MySQL hata {error.errno}).") from None
        finally:
            connection.close()
