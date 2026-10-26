"""Taşıma kuralları: kaynak satırlarından hedefe yazılacak satırlar, atlananların nedenleri ve beklenen lisanslar.

Veritabanına dokunmaz. Eşlemeler geçiş dosyalarının başındaki tablolardır (0002 hesaplar, 0003 destek, 0004 ajanlar):

- Hesap: MOD_RPA ya da MOD_AJAN kullanıcı satırı olan ya da rolü Admin olan her hesap. id = KullaniciID,
  giris = Eposta, sifre_hash = SifreHash (aynen), rol Admin → yonetici, diğerleri (firma yöneticisi Yonetici dahil)
  → kullanici, durum Aktif → aktif (başka her değer pasif), alan_id = FirmaID, kaynak = 'orkestrai'.
  Daha önce taşınmış bir hesap (`kept`) hakkı kalkmış olsa da kaynakla eşitlenir; kaynakta artık yoksa `gone`
  listesine girer (hedef onu pasif yapar). Böylece bir kez taşınan hesap ve kayıtları Peyrev'de kalır.
- Modül hakkı: hesabın satırı VE firmasının aynı modül satırı gerekir (orkestrai kuralı); bitiş ikisinin erkeni
  (ikisi de boşsa süresiz), başlangıç firmanın başlangıcı. Firma pasifse hesabın hiçbir hakkı, firmanın ajan kaydı
  (SYS_AjanOrganizasyonlari) kapalıysa MOD_AJAN taşınmaz. Admin'in satırsız RPA hakkını Peyrev'in yönetici rolü verir.
- Başvurular: hepsi; aynı e-postayla birden çok bekleyen varsa en eskisi. Kararı verilmiş başvurunun şifre özeti
  boş yazılır; hesabı taşınmayan başvurunun kullanici_id'si boş kalır.
- Destek talepleri: hesabı taşınanlar, numarası korunur; mesaj ve ekler talepleriyle gelir.
- Ajanlar: ofisin sahibi YetkiliID'dir, olusturma sahibin üyelik zamanıdır (Studio'nun ofis sırası korunur); diğer
  üyelikler taşınmaz. Görev ve onayın sahibi ofisin yetkilisi, ofis yoksa görevde OlusturanID, onayda OnayciID;
  onaylardan yalnız rejected/consumed, olaylardan yalnız task/approval/office taşınır. Sahibi taşınmayan satır atlanır.
- DATETIME değerleri saat dilimsiz Türkiye saatidir (Europe/Istanbul) ve timestamptz olur.

Hedefin kısıtlarına uymayan satır yazılmaz; sorun olarak raporlanır ve taşıma başarısız sayılır. Hiçbir metin satır
içeriği (e-posta, ad, özet, başlık) taşımaz; kayıtlar yalnız kimlik numarasıyla anılır.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from .. import users
from ..accounts import applications
from ..agents.entitlement import MAX_OFFICES
from ..clock import TR
from ..support import service

TARGETS = ("kullanicilar", "kullanici_modulleri", "basvurular", "destek_talepleri", "destek_mesajlari", "destek_ekleri",
           "ajan_ofisleri", "ajan_gorevleri", "ajan_onaylari", "ajan_olaylari")
SOURCES = {"kullanicilar": "SYS_Kullanicilar", "kullanici_modulleri": "SYS_KullaniciModulleri",
           "basvurular": "SYS_RpaBasvurulari", "destek_talepleri": "SYS_RpaTalepler",
           "destek_mesajlari": "SYS_RpaTalepMesajlari", "destek_ekleri": "SYS_RpaTalepEkleri",
           "ajan_ofisleri": "SYS_AjanOfisleri", "ajan_gorevleri": "SYS_AjanEkipGorevleri",
           "ajan_onaylari": "SYS_AjanEkipOnaylari", "ajan_olaylari": "SYS_AjanEkipOlaylari"}
MODULES = (users.RPA, users.AGENTS)
ADMIN, ACTIVE = "Admin", "Aktif"
ID = re.compile(r"[0-9a-f]{32}")
SAFE_KEY = re.compile(r"[0-9A-Za-z_-]{1,64}")
# Werkzeug özeti "yöntem$tuz$özet"; Peyrev'in Werkzeug'u pbkdf2 ve scrypt yöntemlerini doğrular.
WERKZEUG = re.compile(r"(pbkdf2:[a-z0-9]+(:\d+)?|scrypt(:\d+){0,3})\$[^$]+\$[0-9a-f]+")
TASK_STATES = ("queued", "running", "waiting_user", "succeeded", "failed", "cancelled", "deleted")
PRIORITIES = ("low", "normal", "high")
KEPT_APPROVALS = ("rejected", "consumed")
APPROVAL_STATES = ("pending", "approved") + KEPT_APPROVALS
DECISIONS = (None, "user", "timeout", "owner_changed", "withdrawn")
EVENT_KINDS = ("task", "approval", "office")
OPERATOR_EVENTS = ("license", "account")  # operatör araçlarının kayıtları; account_created giriş adı içerir
ACTORS = ("human", "system")
ATTACHMENT_KINDS = ("akis", "hata_kaydi", "gorsel")
ATTACHMENT_TYPES = ("application/json", "image/png", "image/jpeg")
WRITERS = ("kullanici", "yonetici")

# Atlama nedenleri: raporda tablo başına sayılır.
NO_RIGHTS = "RPA ya da Ajan satırı yok ve Admin değil"
INVALID = "hedefin kurallarına uymuyor (sorunlara bakın)"
CONFLICT = "hedefte çakışma (sorunlara bakın)"
NO_ACCOUNT = "hesabı taşınmadı"
FIRM_CLOSED = "firma pasif"
AGENTS_CLOSED = "firmanın ajan kaydı kapalı"
NO_FIRM_ROW = "firma satırı yok"
OLDER_PENDING = "aynı e-postayla daha eski bekleyen başvuru var"
PEYREV_PENDING = "hedefte aynı e-postayla Peyrev'de açılmış bekleyen başvuru var"
NO_TICKET = "talebi taşınmadı"
NO_OWNER = "sahibi taşınmadı"
OWNER_UNKNOWN = "sahibi bulunamadı (silinmiş ofis ya da kayıt)"
OPEN_APPROVAL = "bekleyen ya da onaylanmış (eski oturuma bağlı)"


@dataclass
class Rows:
    """Bir hedef tablonun beklenen satırları (anahtar → kolon değerleri) ve kaynak sayıları."""

    source: int = 0
    rows: dict[Any, dict[str, Any]] = field(default_factory=dict)
    skipped: Counter = field(default_factory=Counter)

    def skip(self, reason: str) -> None:
        self.skipped[reason] += 1


@dataclass
class Plan:
    now: datetime
    tables: dict[str, Rows] = field(default_factory=lambda: {name: Rows() for name in TARGETS})
    # Hesap → (modül → (durum, bitiş)): orkestrai kuralıyla beklenen; hedefte users.module_state ile karşılaştırılır.
    licenses: dict[int, dict[str, tuple[str, date | None]]] = field(default_factory=dict)
    # Hesap → SHA-256(SifreHash), kaynaktaki özet dizgesinden.
    passwords: dict[int, str] = field(default_factory=dict)
    # Daha önce taşınmış, kaynakta artık olmayan hesaplar: hedefte kalır, pasif olur, hakları kalkar.
    gone: list[int] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def today(self) -> date:
        return self.now.astimezone(TR).date()

    @property
    def accounts(self) -> set[int]:
        """Bu çalıştırmadan sonra hedefte orkestrai'nin olan hesaplar: kayıtları bunlara bağlanabilir."""
        return set(self.tables["kullanicilar"].rows) | set(self.gone)

    def reject(self, table: str, key: Any, reasons: list[str]) -> None:
        self.problems.append(f"{table} {label(key)}: " + "; ".join(reasons))
        self.tables[table].skip(INVALID)


def label(key: Any) -> str:
    """Raporda kayıt anahtarı: tamsayı ya da kimlik; beklenmedik bir değer yazılmaz."""
    if isinstance(key, tuple):
        return "/".join(label(part) for part in key)
    text = str(key)
    return text if type(key) is int or SAFE_KEY.fullmatch(text) else "(geçersiz kimlik)"


def ids(values) -> str:
    values = sorted(values)
    return ", ".join(str(value) for value in values) if values else "yok"


def _rows(tables: dict[str, list[dict[str, Any]]], name: str) -> list[dict[str, Any]]:
    return tables.get(name) or []


def _moment(value: Any) -> datetime | None:
    """Saat dilimsiz MySQL DATETIME (Türkiye saati) → saat dilimli an."""
    if value is None:
        return None
    if not isinstance(value, datetime):
        raise TypeError("DATETIME bekleniyordu")
    return value if value.tzinfo is not None else value.replace(tzinfo=TR)


def _day(value: Any) -> date | None:
    if value is None or (isinstance(value, date) and not isinstance(value, datetime)):
        return value
    if isinstance(value, datetime):
        return value.date()
    return date.fromisoformat(str(value)[:10])


def _earliest(*days: Any) -> date | None:
    known = [day for day in (_day(value) for value in days) if day is not None]
    return min(known) if known else None


def _hex(value: Any) -> bool:
    return isinstance(value, str) and ID.fullmatch(value) is not None


def _text(value: Any, low: int, high: int) -> bool:
    return isinstance(value, str) and low <= len(value) <= high


def _optional(value: Any, high: int) -> bool:
    return value is None or _text(value, 0, high)


def _nul(value: Any) -> bool:
    """PostgreSQL metin ve jsonb değerleri NUL karakteri saklayamaz (MySQL saklar)."""
    if isinstance(value, str):
        return "\x00" in value
    if isinstance(value, dict):
        return any(_nul(key) or _nul(item) for key, item in value.items())
    if isinstance(value, list):
        return any(_nul(item) for item in value)
    return False


def _no_constants(_value: str) -> Any:
    raise ValueError("NaN/Infinity")


def _accept(plan: Plan, table: str, key: Any, values: dict[str, Any]) -> bool:
    if _nul(values):
        plan.reject(table, key, ["metinde NUL karakteri var (PostgreSQL saklayamaz)"])
        return False
    plan.tables[table].rows[key] = values
    return True


class _Index:
    def __init__(self, tables: dict[str, list[dict[str, Any]]]):
        self.users = {row["KullaniciID"]: row for row in _rows(tables, "SYS_Kullanicilar")}
        self.firms = {row["FirmaID"]: row for row in _rows(tables, "SYS_Firmalar")}
        self.user_modules: dict[int, dict[str, dict[str, Any]]] = defaultdict(dict)
        for row in _rows(tables, "SYS_KullaniciModulleri"):
            self.user_modules[row["KullaniciID"]][row["ModulKodu"]] = row
        self.firm_modules = {(row["FirmaID"], row["ModulKodu"]): row for row in _rows(tables, "SYS_FirmaModulleri")}
        self.closed_agents = {row["FirmaID"] for row in _rows(tables, "SYS_AjanOrganizasyonlari")
                              if row["Durum"] != ACTIVE}
        self.offices = {row["OfisID"]: row for row in _rows(tables, "SYS_AjanOfisleri")}

    def withheld(self, user: dict[str, Any], module: str) -> str | None:
        """Hesabın satırı olan bir modül neden taşınmıyor; taşınacaksa None."""
        firm = self.firms.get(user["FirmaID"])
        if firm is None or firm["Durum"] != ACTIVE:
            return FIRM_CLOSED
        if module == users.AGENTS and user["FirmaID"] in self.closed_agents:
            return AGENTS_CLOSED
        if (user["FirmaID"], module) not in self.firm_modules:
            return NO_FIRM_ROW
        return None

    def owner(self, office_id: Any, fallback: Any) -> Any:
        """Görev ve onayın sahibi: ofisin yetkilisi; ofis yoksa (boş ya da silinmiş) verilen hesap."""
        office = self.offices.get(office_id) if office_id is not None else None
        return office["YetkiliID"] if office is not None else fallback


def expected_license(index: _Index, user: dict[str, Any], module: str, today: date) -> tuple[str, date | None]:
    """orkestrai'nin kararı (_rpa_lisans_durumu; ajanlarda module_access) ve taşımanın firma kuralları:
    (aktif | sure_doldu | yetki_yok, bitiş). Taşınan satırlardan değil, doğrudan kaynak satırlarından hesaplanır."""
    if module == users.RPA and user["Rol"] == ADMIN:
        return users.ACTIVE, None
    account = index.user_modules.get(user["KullaniciID"], {}).get(module)
    if account is None or index.withheld(user, module):
        return users.MISSING, None
    end = _earliest(account["BitisTarihi"], index.firm_modules[(user["FirmaID"], module)]["BitisTarihi"])
    if end is not None and end < today:
        return users.EXPIRED, end
    return users.ACTIVE, end


def build(tables: dict[str, list[dict[str, Any]]], now: datetime, *, kept: Collection[int] = (),
          blocked: dict[str, dict[Any, str]] | None = None) -> Plan:
    """`now`: taşıma anı (saat dilimli; lisans "bugün"ü Türkiye günüdür). `kept`: hedefte zaten kaynak='orkestrai'
    olan hesaplar. `blocked`: hedefle çakıştığı için yazılmayacak kayıtlar (tablo → anahtar → atlama nedeni)."""
    blocked = blocked or {}
    plan, index = Plan(now=now), _Index(tables)
    _users(index, plan, set(kept), blocked.get("kullanicilar", {}))
    _modules(index, tables, plan)
    _applications(tables, plan, blocked.get("basvurular", {}))
    _support(tables, plan, blocked.get("destek_talepleri", {}))
    _agents(index, tables, plan, blocked.get("ajan_ofisleri", {}))
    _notes(index, tables, plan)
    return plan


def _users(index: _Index, plan: Plan, kept: set[int], blocked: dict[Any, str]) -> None:
    out = plan.tables["kullanicilar"]
    logins: dict[str, int] = {}
    unentitled = []
    for user_id, row in sorted(index.users.items()):
        out.source += 1
        entitled = bool(index.user_modules.get(user_id)) or row["Rol"] == ADMIN
        if not entitled and user_id not in kept:
            out.skip(NO_RIGHTS)
            continue
        if user_id in blocked:
            out.skip(blocked[user_id])
            continue
        login, name, digest, area = row["Eposta"], row["AdSoyad"], row["SifreHash"], row["FirmaID"]
        reasons = []
        if not _text(login, 3, 150):
            reasons.append("giriş adı (Eposta) 3-150 karakter olmalı")
        elif login.lower() in logins:
            reasons.append(f"giriş adı {logins[login.lower()]} numaralı hesapla aynı (büyük/küçük harf duyarsız)")
        if not _text(name, 0, 120):
            reasons.append("ad soyad en çok 120 karakter olabilir")
        if not isinstance(digest, str):
            reasons.append("şifre özeti yok")
        if type(area) is not int or area < 1:
            reasons.append("FirmaID geçersiz")
        if reasons:
            plan.reject("kullanicilar", user_id, reasons)
            continue
        values = {"giris": login, "ad_soyad": name, "sifre_hash": digest,
                  "rol": "yonetici" if row["Rol"] == ADMIN else "kullanici",
                  "durum": "aktif" if row["Durum"] == ACTIVE else "pasif", "alan_id": area, "kaynak": "orkestrai",
                  # NULL creation time (the column has only a default): the target keeps its own value.
                  "olusturma": _moment(row["OlusturmaTarihi"])}
        if not _accept(plan, "kullanicilar", user_id, values):
            continue
        logins[login.lower()] = user_id
        if not entitled:
            unentitled.append(user_id)
        plan.passwords[user_id] = hashlib.sha256(digest.encode("utf-8")).hexdigest()
        plan.licenses[user_id] = {module: expected_license(index, row, module, plan.today) for module in MODULES}
    plan.gone = sorted(kept - set(index.users))
    accounts = out.rows
    spaced = [user_id for user_id, values in accounts.items() if values["giris"] != values["giris"].strip()]
    if spaced:
        plan.notes.append(f"kullanicilar: giriş adının başında ya da sonunda boşluk olan hesaplar: {ids(spaced)} "
                          "(Peyrev giriş adını aynen arar; gerekirse yönetim panelinden düzeltin).")
    foreign = [user_id for user_id, values in accounts.items() if not WERKZEUG.fullmatch(values["sifre_hash"])]
    if foreign:
        plan.notes.append(f"kullanicilar: şifre özeti Werkzeug biçiminde olmayan hesaplar: {ids(foreign)} (şifreyle "
                          "giremezler; yönetim panelinden şifre kodu üretin).")
    if unentitled:
        plan.notes.append(f"kullanicilar: daha önce taşınmış, kaynakta artık RPA ya da Ajan hakkı olmayan hesaplar: "
                          f"{ids(unentitled)} (hesap kaynakla eşitlendi, modül hakları kaldırıldı).")
    if plan.gone:
        plan.notes.append(f"kullanicilar: daha önce taşınmış, kaynakta silinmiş hesaplar: {ids(plan.gone)} (hesap ve "
                          "kayıtları kalır; pasif yapılır, rolü kullanici olur, modül hakları kaldırılır).")


def _modules(index: _Index, tables: dict[str, list[dict[str, Any]]], plan: Plan) -> None:
    out, accounts = plan.tables["kullanici_modulleri"], plan.tables["kullanicilar"].rows
    for row in sorted(_rows(tables, "SYS_KullaniciModulleri"), key=lambda item: (item["KullaniciID"], item["ModulKodu"])):
        out.source += 1
        user_id, module = row["KullaniciID"], row["ModulKodu"]
        if user_id not in accounts:
            out.skip(NO_ACCOUNT)
            continue
        user = index.users[user_id]
        reason = index.withheld(user, module)
        if reason:
            out.skip(reason)
            continue
        company = index.firm_modules[(user["FirmaID"], module)]
        out.rows[(user_id, module)] = {"baslangic": _day(company["BaslangicTarihi"]),
                                       "bitis": _earliest(row["BitisTarihi"], company["BitisTarihi"])}


def _applications(tables: dict[str, list[dict[str, Any]]], plan: Plan, blocked: dict[Any, str]) -> None:
    out, accounts = plan.tables["basvurular"], plan.accounts
    waiting: set[str] = set()
    linked: dict[int, str] = {}
    unlinked = 0
    for row in sorted(_rows(tables, "SYS_RpaBasvurulari"), key=lambda item: (str(item["Olusturma"]), str(item["BasvuruID"]))):
        out.source += 1
        application_id, state = row["BasvuruID"], row["Durum"]
        reasons = []
        if not _hex(application_id):
            reasons.append("kimlik 32 küçük onaltılık karakter olmalı")
        if state not in (applications.PENDING, applications.APPROVED, applications.REJECTED):
            reasons.append("durum geçersiz")
        if row["Paket"] not in applications.PACKAGES:
            reasons.append("paket geçersiz")
        if not _text(row["AdSoyad"], 0, 120) or not _text(row["Eposta"], 0, 150) or not _optional(row["Telefon"], 32):
            reasons.append("ad, e-posta ya da telefon uzunluğu geçersiz")
        if not _optional(row["YoneticiNotu"], 300) or not isinstance(row["AydinlatmaSurumu"], str):
            reasons.append("yönetici notu ya da aydınlatma sürümü geçersiz")
        if not isinstance(row["IP"], str) or not isinstance(row["SifreHash"], str) or row["Olusturma"] is None:
            reasons.append("adres, şifre özeti ya da oluşturma zamanı yok")
        if reasons:
            plan.reject("basvurular", application_id, reasons)
            continue
        if state == applications.PENDING:
            email = row["Eposta"].lower()
            if email in waiting:
                out.skip(OLDER_PENDING)
                continue
            waiting.add(email)
        if application_id in blocked:
            out.skip(blocked[application_id])
            continue
        user_id = row["KullaniciID"] if row["KullaniciID"] in accounts else None
        unlinked += row["KullaniciID"] is not None and user_id is None
        if user_id is not None and user_id in linked:
            plan.reject("basvurular", application_id, [f"hesap {user_id} başka bir başvuruya da bağlı ({linked[user_id]})"])
            continue
        if user_id is not None:
            linked[user_id] = application_id
        _accept(plan, "basvurular", application_id, {
            "ad_soyad": row["AdSoyad"], "eposta": row["Eposta"], "telefon": row["Telefon"],
            "sifre_hash": row["SifreHash"] if state == applications.PENDING else "", "paket": row["Paket"],
            "durum": state, "ip": row["IP"], "aydinlatma_surumu": row["AydinlatmaSurumu"],
            "olusturma": _moment(row["Olusturma"]), "karar_zamani": _moment(row["KararZamani"]),
            "karar_veren_id": row["KararVerenID"], "kullanici_id": user_id, "yonetici_notu": row["YoneticiNotu"]})
    # Decided applications are written before waiting ones: one leaving 'bekliyor' frees its address first.
    out.rows = dict(sorted(out.rows.items(), key=lambda item: item[1]["durum"] == applications.PENDING))
    if unlinked:
        plan.notes.append(f"basvurular: hesabı taşınmayan {unlinked} başvurunun kullanici_id'si boş yazıldı.")


def _support(tables: dict[str, list[dict[str, Any]]], plan: Plan, blocked: dict[Any, str]) -> None:
    tickets, messages, files = (plan.tables[name] for name in ("destek_talepleri", "destek_mesajlari", "destek_ekleri"))
    accounts = plan.accounts
    for row in sorted(_rows(tables, "SYS_RpaTalepler"), key=lambda item: item["TalepNo"]):
        tickets.source += 1
        number = row["TalepNo"]
        if row["KullaniciID"] not in accounts:
            tickets.skip(NO_ACCOUNT)
            continue
        if number in blocked:
            tickets.skip(blocked[number])
            continue
        reasons = []
        if type(number) is not int or not 1 <= number <= service.NUMBER_LIMIT:
            reasons.append("talep numarası geçersiz")
        if row["Tur"] not in service.TYPES or row["Durum"] not in service.STATES or row["SonYazan"] not in WRITERS:
            reasons.append("tür, durum ya da son yazan geçersiz")
        if not _text(row["Baslik"], 1, service.TITLE_LIMIT) or not _optional(row["AdimTuru"], 100):
            reasons.append("başlık ya da adım türü uzunluğu geçersiz")
        if not _text(row["Surum"], 0, 20) or not _text(row["Platform"], 0, 20) or not _optional(row["YayinSurumu"], 20):
            reasons.append("sürüm ya da platform uzunluğu geçersiz")
        elif row["YayinSurumu"] is not None and row["Durum"] != "yayinlandi":
            reasons.append("yayın sürümü yalnız 'yayinlandi' durumunda olabilir")
        if row["Olusturma"] is None or row["Guncelleme"] is None:
            reasons.append("zaman yok")
        if reasons:
            plan.reject("destek_talepleri", number, reasons)
            continue
        _accept(plan, "destek_talepleri", number, {
            "kullanici_id": row["KullaniciID"], "tur": row["Tur"], "baslik": row["Baslik"], "durum": row["Durum"],
            "adim_turu": row["AdimTuru"], "surum": row["Surum"], "platform": row["Platform"],
            "yayin_surumu": row["YayinSurumu"], "son_yazan": row["SonYazan"], "olusturma": _moment(row["Olusturma"]),
            "guncelleme": _moment(row["Guncelleme"]), "kullanici_gordu": _moment(row["KullaniciGordu"]),
            "yonetici_gordu": _moment(row["YoneticiGordu"])})
    for row in sorted(_rows(tables, "SYS_RpaTalepMesajlari"), key=lambda item: str(item["MesajID"])):
        messages.source += 1
        if row["TalepNo"] not in tickets.rows:
            messages.skip(NO_TICKET)
            continue
        if (not _hex(row["MesajID"]) or row["Yazan"] not in WRITERS or type(row["YazanID"]) is not int
                or not _text(row["Metin"], 1, service.TEXT_LIMIT) or row["Olusturma"] is None):
            plan.reject("destek_mesajlari", row["MesajID"], ["kimlik, yazan, metin ya da zaman geçersiz"])
            continue
        _accept(plan, "destek_mesajlari", row["MesajID"], {
            "talep_no": row["TalepNo"], "yazan_id": row["YazanID"], "yazan": row["Yazan"], "metin": row["Metin"],
            "olusturma": _moment(row["Olusturma"])})
    for row in sorted(_rows(tables, "SYS_RpaTalepEkleri"), key=lambda item: str(item["EkID"])):
        files.source += 1
        if row["TalepNo"] not in tickets.rows:
            files.skip(NO_TICKET)
            continue
        content = bytes(row["Icerik"]) if isinstance(row["Icerik"], (bytes, bytearray, memoryview)) else None
        if (not _hex(row["EkID"]) or row["Tur"] not in ATTACHMENT_KINDS or row["Bicim"] not in ATTACHMENT_TYPES
                or not _text(row["Ad"], 1, 120) or content is None or row["Boyut"] != len(content)
                or row["Olusturma"] is None):
            plan.reject("destek_ekleri", row["EkID"], ["kimlik, tür, biçim, ad, boyut ya da zaman geçersiz"])
            continue
        _accept(plan, "destek_ekleri", row["EkID"], {
            "talep_no": row["TalepNo"], "tur": row["Tur"], "ad": row["Ad"], "bicim": row["Bicim"], "boyut": row["Boyut"],
            "icerik": content, "olusturma": _moment(row["Olusturma"])})


def _agents(index: _Index, tables: dict[str, list[dict[str, Any]]], plan: Plan, blocked: dict[Any, str]) -> None:
    accounts = plan.accounts
    offices, tasks, approvals, events = (plan.tables[name] for name in
                                         ("ajan_ofisleri", "ajan_gorevleri", "ajan_onaylari", "ajan_olaylari"))
    joined = {(row["OfisID"], row["KullaniciID"]): row["Olusturma"] for row in _rows(tables, "SYS_AjanOfisUyeleri")}
    names: dict[tuple[Any, str], str] = {}
    for office_id, row in sorted(index.offices.items(), key=lambda item: str(item[0])):
        offices.source += 1
        owner = row["YetkiliID"]
        if owner not in accounts:
            offices.skip(NO_OWNER)
            continue
        if office_id in blocked:
            offices.skip(blocked[office_id])
            continue
        created = _moment(joined.get((office_id, owner)) or row["Olusturma"])
        reasons = []
        if not _hex(office_id):
            reasons.append("kimlik 32 küçük onaltılık karakter olmalı")
        if not _text(row["Ad"], 2, 120):
            reasons.append("ad 2-120 karakter olmalı")
        elif (owner, row["Ad"].casefold()) in names:
            reasons.append(f"ad sahibin {names[(owner, row['Ad'].casefold())]} ofisiyle aynı")
        if created is None:
            reasons.append("oluşturma zamanı yok")
        if reasons:
            plan.reject("ajan_ofisleri", office_id, reasons)
            continue
        if _accept(plan, "ajan_ofisleri", office_id, {
                "sahip_id": owner, "ad": row["Ad"], "durum": "aktif" if row["Durum"] == ACTIVE else "pasif",
                "olusturma": created, "guncelleme": created}):
            names[(owner, row["Ad"].casefold())] = office_id
    for owner, count in sorted(Counter(values["sahip_id"] for values in offices.rows.values()).items()):
        if count > MAX_OFFICES:
            plan.problems.append(f"ajan_ofisleri: {owner} numaralı hesabın {count} ofisi var (en çok {MAX_OFFICES}).")

    task_owner: dict[Any, Any] = {}
    requests: dict[tuple[Any, Any], Any] = {}
    for row in sorted(_rows(tables, "SYS_AjanEkipGorevleri"), key=lambda item: str(item["ID"])):
        tasks.source += 1
        owner = task_owner[row["ID"]] = index.owner(row["OfisID"], row["OlusturanID"])
        if owner not in accounts:
            tasks.skip(NO_OWNER)
            continue
        reasons = []
        if (not _hex(row["ID"]) or not (row["OfisID"] is None or _hex(row["OfisID"])) or not _hex(row["IstekID"])
                or not isinstance(row["IstekOzeti"], str) or not _text(row["Baslik"], 1, 160)
                or not _text(row["Talimat"], 0, 20000) or row["Oncelik"] not in PRIORITIES
                or row["Durum"] not in TASK_STATES or type(row["Surum"]) is not int or row["Surum"] < 1
                or type(row["OlusturanID"]) is not int or type(row["AtananID"]) is not int
                or row["Olusturma"] is None or row["Guncelleme"] is None):
            reasons.append("kimlik, metin, öncelik, durum, sürüm ya da zaman geçersiz")
        elif (row["OlusturanID"], row["IstekID"]) in requests:
            reasons.append(f"aynı oluşturan ve istek kimliği {requests[(row['OlusturanID'], row['IstekID'])]} "
                           "göreviyle aynı")
        if reasons:
            plan.reject("ajan_gorevleri", row["ID"], reasons)
            continue
        if _accept(plan, "ajan_gorevleri", row["ID"], {
                "sahip_id": owner, "ofis_id": row["OfisID"], "olusturan_id": row["OlusturanID"],
                "atanan_id": row["AtananID"], "istek_id": row["IstekID"], "istek_ozeti": row["IstekOzeti"],
                "baslik": row["Baslik"], "talimat": row["Talimat"], "oncelik": row["Oncelik"], "durum": row["Durum"],
                "surum": row["Surum"], "sonuc_ozeti": row["SonucOzeti"], "olusturma": _moment(row["Olusturma"]),
                "baslama": _moment(row["Baslama"]), "bitis": _moment(row["Bitis"]),
                "guncelleme": _moment(row["Guncelleme"])}):
            requests[(row["OlusturanID"], row["IstekID"])] = row["ID"]

    approval_owner: dict[Any, Any] = {}
    requests = {}
    for row in sorted(_rows(tables, "SYS_AjanEkipOnaylari"), key=lambda item: str(item["ID"])):
        approvals.source += 1
        owner = approval_owner[row["ID"]] = index.owner(row["OfisID"], row["OnayciID"])
        if row["Durum"] not in KEPT_APPROVALS:
            approvals.skip(OPEN_APPROVAL if row["Durum"] in APPROVAL_STATES else "durum geçersiz")
            continue
        if owner not in accounts:
            approvals.skip(NO_OWNER)
            continue
        reasons = []
        if (not _hex(row["ID"]) or not (row["OfisID"] is None or _hex(row["OfisID"])) or not _hex(row["IstekID"])
                or type(row["Deneme"]) is not int or not 1 <= row["Deneme"] <= 100 or row["KararNedeni"] not in DECISIONS
                or type(row["Surum"]) is not int or row["Surum"] < 1 or type(row["IsteyenID"]) is not int
                or type(row["OnayciID"]) is not int or row["Olusturma"] is None or row["Gecerlilik"] is None):
            reasons.append("kimlik, deneme, karar nedeni, sürüm ya da zaman geçersiz")
        elif (row["IsteyenID"], row["IstekID"]) in requests:
            reasons.append(f"aynı isteyen ve istek kimliği {requests[(row['IsteyenID'], row['IstekID'])]} onayıyla aynı")
        if reasons:
            plan.reject("ajan_onaylari", row["ID"], reasons)
            continue
        if _accept(plan, "ajan_onaylari", row["ID"], {
                "sahip_id": owner, "ofis_id": row["OfisID"], "isteyen_id": row["IsteyenID"],
                "onayci_id": row["OnayciID"], "oturum_id": row["OturumID"], "istek_id": row["IstekID"],
                "istek_ozeti": row["IstekOzeti"], "calisma_id": row["CalismaID"], "akis_id": row["AkisID"],
                "akis_ozeti": row["AkisOzeti"], "girdi_ozeti": row["GirdiOzeti"],
                "onizleme_ozeti": row["OnizlemeOzeti"], "baglam_ozeti": row["BaglamOzeti"],
                "onizleme": row["Onizleme"], "gerekce": row["Gerekce"], "deneme": row["Deneme"], "durum": row["Durum"],
                "surum": row["Surum"], "karar_nedeni": row["KararNedeni"], "olusturma": _moment(row["Olusturma"]),
                "gecerlilik": _moment(row["Gecerlilik"]), "karar_zamani": _moment(row["KararZamani"]),
                "tuketim_zamani": _moment(row["TuketimZamani"])}):
            requests[(row["IsteyenID"], row["IstekID"])] = row["ID"]

    all_events = _rows(tables, "SYS_AjanEkipOlaylari")
    founders = {row["KayitID"]: row["KullaniciID"] for row in all_events
                if row["KayitTuru"] == "office" and row["Olay"] == "office_created"}
    for row in sorted(all_events, key=lambda item: str(item["ID"])):
        events.source += 1
        kind = row["KayitTuru"]
        if kind not in EVENT_KINDS:
            events.skip(f"'{kind}' olayı (taşınmaz)" if kind in OPERATOR_EVENTS else "bilinmeyen kayıt türü")
            continue
        if kind == "task":
            owner = task_owner.get(row["KayitID"])
        elif kind == "approval":
            owner = approval_owner.get(row["KayitID"])
        else:
            office = index.offices.get(row["KayitID"])
            owner = office["YetkiliID"] if office is not None else founders.get(row["KayitID"])
        if owner is None:
            events.skip(OWNER_UNKNOWN)
            continue
        if owner not in accounts:
            events.skip(NO_OWNER)
            continue
        try:
            details = json.loads(row["Ayrinti"], parse_constant=_no_constants)
            # 1e400 parses to inf, which jsonb cannot store.
            json.dumps(details, allow_nan=False)
        except (TypeError, ValueError, RecursionError):
            details = _no_constants
        if (not _hex(row["ID"]) or not isinstance(row["KayitID"], str) or row["Aktor"] not in ACTORS
                or not (row["KullaniciID"] is None or type(row["KullaniciID"]) is int)
                or not _text(row["Olay"], 0, 32) or details is _no_constants or row["Olusturma"] is None):
            plan.reject("ajan_olaylari", row["ID"], ["kimlik, aktör, olay, ayrıntı (JSON) ya da zaman geçersiz"])
            continue
        _accept(plan, "ajan_olaylari", row["ID"], {
            "sahip_id": owner, "kayit_turu": kind, "kayit_id": row["KayitID"], "kullanici_id": row["KullaniciID"],
            "aktor": row["Aktor"], "olay": row["Olay"], "ayrinti": details, "olusturma": _moment(row["Olusturma"])})


def _notes(index: _Index, tables: dict[str, list[dict[str, Any]]], plan: Plan) -> None:
    accounts = plan.tables["kullanicilar"].rows
    managers = [user_id for user_id in accounts if index.users[user_id]["Rol"] == "Yonetici"]
    plan.notes.append(f"Roller: Admin → yonetici: {ids(user_id for user_id, values in accounts.items() if values['rol'] == 'yonetici')}"
                      f"; firma yöneticisi (Yonetici) → kullanici: {ids(managers)}.")
    passive = [user_id for user_id, values in accounts.items() if values["durum"] == "pasif"]
    if passive:
        plan.notes.append(f"Pasif taşınan hesaplar: {ids(passive)}.")
    closed = [firm_id for firm_id, firm in index.firms.items() if firm["Durum"] != ACTIVE]
    affected = [user_id for user_id in accounts if index.users[user_id]["FirmaID"] in closed
                and index.user_modules.get(user_id)]
    plan.notes.append(f"SYS_Firmalar: {len(index.firms)} firma, pasif: {ids(closed)}; modül hakları taşınmayan hesaplar: "
                      f"{ids(affected)}.")
    organizations = _rows(tables, "SYS_AjanOrganizasyonlari")
    plan.notes.append(f"SYS_AjanOrganizasyonlari: {len(organizations)} kayıt (Peyrev'de tablo yok); ajan kaydı kapalı "
                      f"firmalar: {ids(index.closed_agents)}.")
    members = _rows(tables, "SYS_AjanOfisUyeleri")
    others = [row for row in members if (office := index.offices.get(row["OfisID"])) is None
              or office["YetkiliID"] != row["KullaniciID"]]
    plan.notes.append(f"SYS_AjanOfisUyeleri: {len(members)} üyelik; sahiplik {len(members) - len(others)}, taşınmayan "
                      f"diğer üyelik {len(others)} (hesaplar: {ids({row['KullaniciID'] for row in others})}; bu hesaplar "
                      "Peyrev'de ofissiz başlar).")
    sessions = _rows(tables, "SYS_RpaOturumlari")
    plan.notes.append(f"SYS_RpaOturumlari: {len(sessions)} oturum (aktif {sum(row['Durum'] == ACTIVE for row in sessions)}) "
                      "taşınmaz; Studio'da yeniden giriş gerekir.")
    codes = _rows(tables, "SYS_RpaSifreKodlari")
    now = plan.now.astimezone(TR).replace(tzinfo=None)
    waiting = {row["KullaniciID"] for row in codes if row["Kullanildi"] is None and row["Gecerlilik"] is not None
               and row["Gecerlilik"] > now and row["KullaniciID"] in accounts}
    plan.notes.append(f"SYS_RpaSifreKodlari: {len(codes)} kod taşınmaz; açık kodu olan taşınan hesaplar: {ids(waiting)}"
                      + (" (Peyrev yönetim panelinden yeni kod üretin)." if waiting else "."))
    plan.notes.append(f"SYS_RpaHesapIzleri: {len(_rows(tables, 'SYS_RpaHesapIzleri'))} iz taşınmaz.")
