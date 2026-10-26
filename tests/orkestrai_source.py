"""Taşıma testlerinin sahte orkestrai kaynağı.

source.Source arayüzünü uygular ve satırları source.QUERIES'in döndürdüğü biçimde verir: kolon adları ve sırası
sorgudan okunur, değerler gerçek tabloların (deploy/kaynak-inceleme.sh raporundaki CREATE TABLE'lar) türlerindedir:
saat dilimsiz DATETIME / DATETIME(6), DATE, CHAR(32) onaltılık kimlik, ENUM metni, TEXT içinde JSON, MEDIUMBLOB baytı.
Modül tablolarında ModulID yerine sorgunun JOIN ettiği ModulKodu bulunur.
"""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import date, datetime
from typing import Any

from werkzeug.security import generate_password_hash

from peyrev.tasima import source


def query_columns(query: str) -> tuple[str, ...]:
    head = query[len("SELECT "):query.index(" FROM ")]
    return tuple(part.strip().split(".")[-1] for part in head.split(","))


COLUMNS = {table: query_columns(query) for table, query in source.QUERIES.items()}
CREATED = datetime(2026, 3, 2, 9, 30)               # DATETIME (saniye), Türkiye saati
PRECISE = datetime(2026, 3, 2, 9, 30, 15, 123456)   # DATETIME(6)
START = date(2026, 1, 1)
_HASHES: dict[str, str] = {}


def password_hash(password: str) -> str:
    """Werkzeug özeti (orkestrai'nin biçimi; hızlı olsun diye az turlu). Aynı şifreye aynı özet."""
    if password not in _HASHES:
        _HASHES[password] = generate_password_hash(password, method="pbkdf2:sha256:1000")
    return _HASHES[password]


def hex_id(*parts: Any) -> str:
    return hashlib.md5(":".join(map(str, parts)).encode()).hexdigest()


DEFAULTS: dict[str, dict[str, Any]] = {
    "SYS_Firmalar": {"Durum": "Aktif"},
    "SYS_FirmaModulleri": {"BaslangicTarihi": START},
    "SYS_Kullanicilar": {"AdSoyad": "Ad Soyad", "Rol": "Personel", "Durum": "Aktif", "OlusturmaTarihi": CREATED},
    "SYS_AjanOrganizasyonlari": {"Durum": "Aktif"},
    "SYS_RpaBasvurulari": {"AdSoyad": "Başvuran Kişi", "Telefon": None, "Paket": "rpa", "Durum": "bekliyor",
                           "IP": "198.51.100.7", "AydinlatmaSurumu": "2026-10-01", "Olusturma": CREATED},
    "SYS_RpaTalepler": {"Tur": "soru", "Baslik": "Talep başlığı", "Durum": "acik", "Surum": "0.9.53",
                        "Platform": "macOS", "SonYazan": "kullanici", "Olusturma": CREATED, "Guncelleme": CREATED},
    "SYS_RpaTalepMesajlari": {"Yazan": "kullanici", "Metin": "Mesaj metni", "Olusturma": CREATED},
    "SYS_RpaTalepEkleri": {"Tur": "gorsel", "Ad": "ekran.png", "Bicim": "image/png", "Olusturma": CREATED},
    "SYS_AjanOfisleri": {"Durum": "Aktif", "Olusturma": CREATED},
    "SYS_AjanOfisUyeleri": {"Olusturma": CREATED},
    "SYS_AjanEkipGorevleri": {"IstekOzeti": "c" * 64, "Baslik": "Görev", "Talimat": "Talimat metni", "Oncelik": "normal",
                              "Durum": "queued", "Surum": 1, "Olusturma": PRECISE, "Guncelleme": PRECISE},
    "SYS_AjanEkipOnaylari": {"IstekOzeti": "d" * 64, "AkisOzeti": "e" * 64, "GirdiOzeti": "f" * 64,
                             "OnizlemeOzeti": "0" * 64, "BaglamOzeti": "1" * 64, "Onizleme": '{"adimlar":[]}',
                             "Gerekce": "Gerekçe metni", "Deneme": 1, "Durum": "consumed", "Surum": 2,
                             "KararNedeni": "user", "Olusturma": PRECISE, "Gecerlilik": datetime(2026, 3, 2, 9, 40)},
    "SYS_AjanEkipOlaylari": {"Aktor": "human", "Ayrinti": "{}", "Olusturma": PRECISE},
    "SYS_RpaOturumlari": {"Durum": "Aktif"},
    "SYS_RpaSifreKodlari": {"Gecerlilik": datetime(2026, 10, 27, 9, 0)},
    "SYS_RpaHesapIzleri": {"Tur": "sifre"},
}


class Orkestrai:
    """Sahte kaynak: `read()` her çağrıda tabloların kopyasını verir (gerçek kaynak gibi aynı anın görüntüsü)."""

    def __init__(self) -> None:
        self.tables: dict[str, list[dict[str, Any]]] = {table: [] for table in source.QUERIES}

    def read(self) -> dict[str, list[dict[str, Any]]]:
        for table, rows in self.tables.items():
            for row in rows:
                assert tuple(row) == COLUMNS[table], table
        return copy.deepcopy(self.tables)

    def add(self, table: str, **values: Any) -> dict[str, Any]:
        unknown = set(values) - set(COLUMNS[table])
        assert not unknown, f"{table}: {sorted(unknown)}"
        defaults = DEFAULTS.get(table, {})
        row = {column: values[column] if column in values else defaults.get(column) for column in COLUMNS[table]}
        self.tables[table].append(row)
        return row

    def find(self, table: str, **match: Any) -> dict[str, Any]:
        rows = [row for row in self.tables[table] if all(row[key] == value for key, value in match.items())]
        assert len(rows) == 1, (table, match, len(rows))
        return rows[0]

    def drop(self, table: str, **match: Any) -> None:
        self.tables[table].remove(self.find(table, **match))

    # --- kısayollar ----------------------------------------------------------------------------------------------

    def firm(self, firm_id: int, durum: str | None = "Aktif", modules: dict[str, date | None] | None = None,
             start: date | None = START) -> None:
        """Firma ve modül satırları; `modules={"MOD_RPA": None}` süresiz satırdır, anahtarı olmayan modülün satırı yok."""
        self.add("SYS_Firmalar", FirmaID=firm_id, Durum=durum)
        for module, end in (modules if modules is not None else {"MOD_RPA": None, "MOD_AJAN": None}).items():
            self.add("SYS_FirmaModulleri", FirmaID=firm_id, ModulKodu=module, BaslangicTarihi=start, BitisTarihi=end)

    def user(self, user_id: int, firm_id: int, email: str, *, rol: str | None = "Personel",
             modules: dict[str, date | None] | None = None, password: str = "Eski12345", **values: Any) -> dict[str, Any]:
        row = self.add("SYS_Kullanicilar", KullaniciID=user_id, FirmaID=firm_id, Eposta=email,
                       SifreHash=password_hash(password), Rol=rol, **values)
        for module, end in (modules or {}).items():
            self.add("SYS_KullaniciModulleri", KullaniciID=user_id, ModulKodu=module, BitisTarihi=end)
        return row

    def office(self, office_id: str, owner: int, name: str, *, members: dict[int, datetime] | None = None,
               **values: Any) -> dict[str, Any]:
        row = self.add("SYS_AjanOfisleri", OfisID=office_id, Ad=name, YetkiliID=owner, **values)
        for member, joined in (members or {}).items():
            self.add("SYS_AjanOfisUyeleri", KullaniciID=member, OfisID=office_id, Olusturma=joined)
        return row

    def event(self, event_id: str, kind: str, record_id: str, user_id: int | None, olay: str, details: Any = None,
              **values: Any) -> dict[str, Any]:
        return self.add("SYS_AjanEkipOlaylari", ID=event_id, KayitTuru=kind, KayitID=record_id, KullaniciID=user_id,
                        Olay=olay, Ayrinti=json.dumps(details if details is not None else {}), **values)
