"""Robot Paneli: Studio'nun durum bildirimi, komutlar, panel ve Hesabım görünümleri, sahibine uyarılar.

Bildirim yalnız akış adlarını, durumları, adım numaralarını ve hata türlerini taşır; hata metni, değişken, dosya adı ya
da ekran görüntüsü gelmez ve sıkı doğrulamadan geçmeyen bildirim hiç yazılmaz. Robot, Studio oturumunun cihaz
kimliğiyle tanınır (`kurulum`, belirteçteki cihazla aynı olmalıdır); hesap başına en çok ROBOTS_PER_ACCOUNT robot
kalır (en uzun süredir görünmeyen silinir).

Komut yaşamı: panel ya da Hesabım yazar (sonuc boş) → robotun bir sonraki bildiriminde yanıtla gider (gonderildi) →
robot sonucunu ondan sonraki bildirimde döner (sonuc, sonuclandi). COMMAND_LIFETIME içinde sonuçlanmayan komut
"suresi_doldu" olur; KEEP_COMMANDS'ten eski komutlar bildirim gelince silinir. Durdur yalnız robotun çalışıyor diye
bildirdiği bir çalışma, yeniden çalıştır yalnız son hatalarında bildirdiği bir akış için yazılır.

Uyarılar destek bildirim kanalıyla (Telegram, e-posta) sahibine gider: art arda FAILURE_STREAK hatalı çalışma (yalnız
son uyarıdan yeni hatalar, robot başına saatte en çok bir) ve sessizlik (son 24 saatte bildirmiş robot SILENCE boyunca
susarsa, robot yeniden bildirene kadar bir kez). Studio kapanırken ya da bildirim kapatılınca "kapaniyor" işareti
gönderir; kapalı robot için sessizlik uyarısı verilmez.
"""

from __future__ import annotations

import hmac
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg import Cursor
from psycopg.types.json import Jsonb

from .. import tokens, users
from ..accounts.rules import stamp
from ..common import LOG, ApiError
from ..db import advisory_lock

PROTOCOL = 1
INTERVAL = 300  # Studio'nun bildirim aralığı (saniye); yanıtla bildirilir
BODY_LIMIT = 32 * 1024
MIN_GAP = timedelta(seconds=20)
MAX_OFFICES, MAX_RUNNING, MAX_FAILURES, MAX_ANSWERS = 50, 10, 5, 50
ROBOTS_PER_ACCOUNT = 20
PENDING_LIMIT = 20
COMMAND_LIFETIME = timedelta(minutes=30)
KEEP_COMMANDS = timedelta(days=30)
RECENT_COMMANDS = 10
GREEN, YELLOW = timedelta(minutes=10), timedelta(minutes=60)
DISK_WARNING = 10  # boş disk yüzdesi bunun altındaysa uyarı
FAILURE_STREAK = 3
ALERT_GAP = timedelta(hours=1)
SILENCE, ACTIVE_WINDOW = timedelta(minutes=15), timedelta(hours=24)
CHECK_GAP = timedelta(minutes=1)  # bildirimlerin arasında yapılan sessizlik denetiminin en sık aralığı

CATEGORIES = {
    "hedef_bulunamadi": "Hedef bulunamadı", "ekran_kilitli": "Ekran kilitli", "baglanti": "Bağlantı hatası",
    "zaman_asimi": "Zaman aşımı", "lisans": "Lisans", "dosya": "Dosya hatası", "yapay_zeka": "Yapay zekâ",
    "onay": "Onay alınamadı", "diger": "Diğer",
}
COMMANDS = {"durdur": "Durdur", "yeniden_calistir": "Yeniden çalıştır"}
RESULTS = {"tamam": "Yapıldı", "bulunamadi": "Robotta bulunamadı", "reddedildi": "Robot reddetti",
           "mesgul": "Robot meşguldü", "suresi_doldu": "Robota ulaşmadı"}
ANSWERS = ("tamam", "bulunamadi", "reddedildi", "mesgul")
COLORS = {"yesil": "10 dakikadan yeni", "sari": "1 saatten yeni", "kirmizi": "1 saatten eski"}

TOP = {"surum", "kurulum", "kapaniyor", "uygulama", "isletim_sistemi", "etiket", "ekran_kilitli", "bos_disk", "sirada",
       "ofisler", "yanitlar"}
STATUS_REQUIRED = {"uygulama", "isletim_sistemi", "etiket", "ofisler"}
OFFICE = {"id", "ad", "telegram", "calisan", "son_24_saat", "ardisik_hata", "son_hatalar"}
RUNNING = {"run_id", "workflow_id", "akis", "baslangic", "adim"}
FAILURE = {"run_id", "workflow_id", "akis", "zaman", "kategori"}
COUNTS = {"basarili", "hatali", "iptal"}
IDENT = re.compile(r"[0-9a-f]{32}")
DEVICE = re.compile(r"[0-9a-f]{32,64}")
VERSION = re.compile(r"\d{1,4}\.\d{1,4}\.\d{1,4}(?:[-+][0-9A-Za-z.]{1,20})?")
INVALID = "Robot durumu geçersiz."
NOT_FOUND = "Robot bulunamadı."
LICENSE_REQUIRED = "Robot Paneli için geçerli Peyrev lisansı gerekli."


# ----- doğrulama ----------------------------------------------------------------------------------
def _invalid() -> ApiError:
    return ApiError(INVALID, 400, "GECERSIZ")


def _object(value: Any, allowed: set[str], required: set[str] = frozenset()) -> dict:
    if not isinstance(value, dict) or set(value) - allowed or required - set(value):
        raise _invalid()
    return value


def _list(value: Any, limit: int) -> list:
    if not isinstance(value, list) or len(value) > limit:
        raise _invalid()
    return value


def _int(value: Any, low: int, high: int, *, nullable: bool = False) -> int | None:
    if value is None and nullable:
        return None
    if type(value) is not int or not low <= value <= high:
        raise _invalid()
    return value


def _bool(value: Any, *, nullable: bool = False) -> bool | None:
    if value is None and nullable:
        return None
    if type(value) is not bool:
        raise _invalid()
    return value


def _text(value: Any, low: int, high: int) -> str:
    """Kırpılmış, boşlukları teke inmiş metin; denetim karakteri reddedilir (ekrana yalnız metin olarak yazılır)."""
    if not isinstance(value, str) or any(ord(char) < 32 or char == "\x7f" for char in value):
        raise _invalid()
    value = " ".join(value.split())
    if not low <= len(value) <= high:
        raise _invalid()
    return value


def _ident(value: Any, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not IDENT.fullmatch(value):
        raise _invalid()
    return value


def _moment(value: Any) -> str:
    """Saat dilimli ISO anı, UTC'ye çevrilmiş olarak."""
    if not isinstance(value, str) or len(value) > 40:
        raise _invalid()
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise _invalid() from None
    if parsed.tzinfo is None:
        raise _invalid()
    return parsed.astimezone(UTC).isoformat()


def _office(value: Any) -> dict[str, Any]:
    item = _object(value, OFFICE, OFFICE)
    counts = _object(item["son_24_saat"], COUNTS, COUNTS)
    running = [_object(entry, RUNNING, RUNNING) for entry in _list(item["calisan"], MAX_RUNNING)]
    failures = [_object(entry, FAILURE, FAILURE) for entry in _list(item["son_hatalar"], MAX_FAILURES)]
    result = {
        "id": _ident(item["id"], nullable=True), "ad": _text(item["ad"], 0, 120), "telegram": _bool(item["telegram"]),
        "calisan": [{"run_id": _ident(entry["run_id"]), "workflow_id": _ident(entry["workflow_id"]),
                     "akis": _text(entry["akis"], 1, 200), "baslangic": _moment(entry["baslangic"]),
                     "adim": _int(entry["adim"], 1, 10000, nullable=True)} for entry in running],
        "son_24_saat": {key: _int(counts[key], 0, 100000) for key in ("basarili", "hatali", "iptal")},
        "ardisik_hata": _int(item["ardisik_hata"], 0, 100000),
        "son_hatalar": [{"run_id": _ident(entry["run_id"]), "workflow_id": _ident(entry["workflow_id"]),
                         "akis": _text(entry["akis"], 1, 200), "zaman": _moment(entry["zaman"]),
                         "kategori": _category(entry["kategori"])} for entry in failures],
    }
    if len({entry["run_id"] for entry in result["calisan"]}) != len(result["calisan"]):
        raise _invalid()
    return result


def _category(value: Any) -> str:
    if not isinstance(value, str) or value not in CATEGORIES:
        raise _invalid()
    return value


def validate(data: dict, device: str) -> tuple[dict | None, list[tuple[int, str]], bool]:
    """(durum ya da kapanışta None, komut yanıtları, kapanıyor mu). `data` json_body'den gelir (bilinmeyen üst alan yok)."""
    _object(data, TOP, {"surum", "kurulum"})
    if type(data["surum"]) is not int or data["surum"] != PROTOCOL:
        raise ApiError("Bu Robot Paneli sürümü desteklenmiyor.", 400, "SURUM")
    if (not isinstance(data["kurulum"], str) or not DEVICE.fullmatch(data["kurulum"])
            or not hmac.compare_digest(data["kurulum"], device)):
        raise ApiError("Bildirim bu Studio oturumunun bilgisayarına ait değil.", 403, "YETKI_YOK")
    closing = _bool(data.get("kapaniyor", False))
    answers: list[tuple[int, str]] = []
    for entry in _list(data.get("yanitlar", []), MAX_ANSWERS):
        entry = _object(entry, {"id", "sonuc"}, {"id", "sonuc"})
        if entry["sonuc"] not in ANSWERS:
            raise _invalid()
        answers.append((_int(entry["id"], 1, 2**62), entry["sonuc"]))
    if closing and not STATUS_REQUIRED & set(data):
        return None, answers, True
    _object(data, TOP, STATUS_REQUIRED | {"surum", "kurulum"})
    version = data["uygulama"]
    if not isinstance(version, str) or len(version) > 20 or not VERSION.fullmatch(version):
        raise _invalid()
    offices = [_office(item) for item in _list(data["ofisler"], MAX_OFFICES)]
    if len({item["id"] for item in offices}) != len(offices):
        raise _invalid()
    status = {
        "uygulama": version, "isletim_sistemi": _text(data["isletim_sistemi"], 1, 40),
        "etiket": _text(data["etiket"], 1, 60),
        "ekran_kilitli": _bool(data.get("ekran_kilitli"), nullable=True),
        "bos_disk": _int(data.get("bos_disk"), 0, 100, nullable=True),
        "sirada": _int(data.get("sirada", 0), 0, 10000),
        "ofisler": offices,
    }
    return status, answers, closing


# ----- Studio bildirimi ---------------------------------------------------------------------------
def studio_user(core, cur: Cursor, authorization: str | None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Robot belirteci (lisans yanıtıyla verilir), açık Studio oturumu ve geçerli Peyrev lisansı."""
    user, claims = tokens.verify(core, cur, "robot", authorization)
    if users.module_state(cur, user, users.RPA, core.clock.today_tr())[0] != users.ACTIVE:
        raise ApiError(LICENSE_REQUIRED, 403, "YETKI_YOK")
    return user, claims


def heartbeat(core, cur: Cursor, user: dict[str, Any], claims: dict[str, Any],
              data: dict) -> tuple[list[dict[str, Any]], tuple[str, str] | None]:
    """(robota giden komutlar, işlem kaydedilince gönderilecek uyarı ya da None)."""
    device = claims["cihaz"]
    status, answers, closing = validate(data, device)
    moment = core.clock.now()
    advisory_lock(cur, f"robot:{user['id']}:{device}")
    cur.execute("SELECT * FROM robotlar WHERE kullanici_id = %s AND kurulum_kimligi = %s FOR UPDATE",
                (user["id"], device))
    robot = cur.fetchone()
    if (robot is not None and not closing and not answers and not robot["kapali"]
            and moment - robot["son_gorulme"] < MIN_GAP):
        raise ApiError("Durum çok sık bildiriliyor; bir sonraki bildirimde yeniden deneyin.", 429, "COK_SIK")
    if robot is None:
        cur.execute("INSERT INTO robotlar (kullanici_id, kurulum_kimligi, son_gorulme, created_at) "
                    "VALUES (%s, %s, %s, %s) RETURNING *", (user["id"], device, moment, moment))
        robot = cur.fetchone()
        # An account keeps its most recently seen computers only.
        cur.execute("DELETE FROM robotlar WHERE kullanici_id = %s AND id NOT IN (SELECT id FROM robotlar WHERE "
                    "kullanici_id = %s ORDER BY son_gorulme DESC, id DESC LIMIT %s)",
                    (user["id"], user["id"], ROBOTS_PER_ACCOUNT))
    if status is not None:
        cur.execute("UPDATE robotlar SET son_gorulme = %s, kapali = %s, etiket = %s, surum = %s, isletim_sistemi = %s, "
                    "durum = %s WHERE id = %s RETURNING *",
                    (moment, closing, status["etiket"], status["uygulama"], status["isletim_sistemi"], Jsonb(status),
                     robot["id"]))
    else:
        cur.execute("UPDATE robotlar SET son_gorulme = %s, kapali = TRUE WHERE id = %s RETURNING *", (moment, robot["id"]))
    robot = cur.fetchone()
    if answers:
        cur.execute("UPDATE robot_komutlari k SET sonuc = a.sonuc, sonuclandi = %s "
                    "FROM unnest(%s::bigint[], %s::text[]) AS a(id, sonuc) "
                    "WHERE k.id = a.id AND k.robot_id = %s AND k.gonderildi IS NOT NULL AND k.sonuc IS NULL",
                    (moment, [item[0] for item in answers], [item[1] for item in answers], robot["id"]))
    cur.execute("UPDATE robot_komutlari SET sonuc = 'suresi_doldu', sonuclandi = %s "
                "WHERE robot_id = %s AND sonuc IS NULL AND olusturuldu < %s", (moment, robot["id"], moment - COMMAND_LIFETIME))
    commands: list[dict[str, Any]] = []
    if not closing:
        cur.execute("UPDATE robot_komutlari SET gonderildi = %s WHERE robot_id = %s AND gonderildi IS NULL "
                    "AND sonuc IS NULL RETURNING id, tur, hedef, ofis_id", (moment, robot["id"]))
        for row in sorted(cur.fetchall(), key=lambda item: item["id"]):
            target = "run_id" if row["tur"] == "durdur" else "workflow_id"
            commands.append({"id": row["id"], "tur": row["tur"], target: row["hedef"], "ofis_id": row["ofis_id"]})
    cur.execute("DELETE FROM robot_komutlari WHERE olusturuldu < %s", (moment - KEEP_COMMANDS,))
    notice = _failure_alert(core, cur, user, robot, status, moment) if status is not None else None
    return commands, notice


def _failure_alert(core, cur: Cursor, user: dict[str, Any], robot: dict[str, Any], status: dict,
                   moment: datetime) -> tuple[str, str] | None:
    """Art arda FAILURE_STREAK hatalı çalışma: yalnız son uyarıdakinden yeni hatalar, robot başına saatte en çok bir."""
    if robot["hata_uyarisi"] is not None and moment - robot["hata_uyarisi"] < ALERT_GAP:
        return None
    mark = robot["hata_uyari_imi"]
    offices, newest = [], None
    for office in status["ofisler"]:
        if office["ardisik_hata"] < FAILURE_STREAK or not office["son_hatalar"]:
            continue
        latest = max(datetime.fromisoformat(item["zaman"]) for item in office["son_hatalar"])
        if mark is not None and latest <= mark:
            continue
        offices.append(office)
        newest = latest if newest is None else max(newest, latest)
    if not offices:
        return None
    cur.execute("UPDATE robotlar SET hata_uyarisi = %s, hata_uyari_imi = %s WHERE id = %s", (moment, newest, robot["id"]))
    lines = [f"Hesap: {user['giris']}" + (f" ({user['ad_soyad']})" if user["ad_soyad"] else ""),
             f"Bilgisayar: {status['etiket']} · Peyrev {status['uygulama']}"]
    for office in offices:
        lines.append(f"\nOfis: {office['ad'] or 'Hesabın ofisi'} · art arda {office['ardisik_hata']} hatalı çalışma")
        lines += [f"- {item['akis']} · {CATEGORIES[item['kategori']]} · {_shown(item['zaman'])}"
                  for item in office["son_hatalar"]]
    lines.append(f"\nRobot Paneli: {core.settings.public_base.rstrip('/')}/yonetim/#robotlar")
    return f"Robot uyarısı: art arda {FAILURE_STREAK} hatalı çalışma · {status['etiket']}", "\n".join(lines)


def _shown(value: str | None) -> str | None:
    return stamp(datetime.fromisoformat(value)) if value else None


# ----- sessizlik denetimi -------------------------------------------------------------------------
def check_silence(core) -> int:
    """Son 24 saatte bildirmiş, kapanmamış robot 15 dakikadır susuyorsa sahibine bir kez bildirir; uyarılan robot sayısı.

    Satırlar uyarı anıyla işaretlenerek seçilir: aynı anda çalışan iki denetim aynı robotu iki kez bildirmez."""
    moment = core.clock.now()
    with core.db.transaction() as cur:
        cur.execute("UPDATE robotlar r SET sessizlik_uyarisi = %s FROM kullanicilar k "
                    "WHERE k.id = r.kullanici_id AND k.durum = 'aktif' AND NOT r.kapali AND r.son_gorulme < %s "
                    "AND r.son_gorulme > %s AND (r.sessizlik_uyarisi IS NULL OR r.sessizlik_uyarisi < r.son_gorulme) "
                    "RETURNING r.id, r.etiket, r.son_gorulme, k.giris, k.ad_soyad",
                    (moment, moment - SILENCE, moment - ACTIVE_WINDOW))
        rows = sorted(cur.fetchall(), key=lambda row: row["id"])
    if rows:
        from ..support import notify

        lines = [f"- {row['etiket'] or 'Adsız bilgisayar'} · {row['giris']}"
                 + (f" ({row['ad_soyad']})" if row["ad_soyad"] else "") + f" · son bildirim {stamp(row['son_gorulme'])}"
                 for row in rows]
        lines.append(f"\nRobot Paneli: {core.settings.public_base.rstrip('/')}/yonetim/#robotlar")
        notify.deliver(core, f"Robot uyarısı: {len(rows)} robot 15 dakikadır bildirim göndermiyor",
                       "Bilgisayar kapanmış, uyku moduna geçmiş ya da internet bağlantısı kesilmiş olabilir.\n\n"
                       + "\n".join(lines))
    return len(rows)


def maybe_check_silence(core) -> None:
    """Bildirimlerin arasında, süreç başına en çok dakikada bir sessizlik denetimi; hatası bildirimi etkilemez."""
    moment = core.clock.now()
    last = core.extras.get("robot_sessizlik_denetimi")
    if last is not None and moment - last < CHECK_GAP:
        return
    core.extras["robot_sessizlik_denetimi"] = moment
    try:
        check_silence(core)
    except Exception as error:  # noqa: BLE001 - the robot's report never fails over this check
        LOG.warning("Robot sessizlik denetimi yapılamadı (%s).", type(error).__name__)


# ----- görünümler ve komutlar ---------------------------------------------------------------------
def _color(age: timedelta) -> str:
    return "yesil" if age < GREEN else "sari" if age < YELLOW else "kirmizi"


def _command_view(row: dict[str, Any], moment: datetime) -> dict[str, Any]:
    result = row["sonuc"]
    if result is None and moment - row["olusturuldu"] >= COMMAND_LIFETIME:
        result = "suresi_doldu"
    waiting = "Robota gönderildi" if row["gonderildi"] else "Robotun bildirimi bekleniyor"
    return {"id": row["id"], "tur": row["tur"], "tur_adi": COMMANDS[row["tur"]], "hedef": row["hedef"],
            "akis": row["akis"], "olusturan": row["olusturan"], "olusturuldu": stamp(row["olusturuldu"]),
            "sonuc": result, "sonuc_adi": RESULTS[result] if result else waiting,
            "sonuclandi": stamp(row["sonuclandi"])}


def robot_view(row: dict[str, Any], commands: list[dict[str, Any]], moment: datetime, *,
               owner: bool = False) -> dict[str, Any]:
    status = row["durum"] or {}
    age = moment - row["son_gorulme"]
    offices = []
    for office in status.get("ofisler", []):
        offices.append({
            "id": office["id"], "ad": office["ad"], "telegram": office["telegram"],
            # A closed Studio runs nothing: its last list is not shown as running.
            "calisan": [] if row["kapali"] else [{**item, "baslangic": _shown(item["baslangic"])}
                                                 for item in office["calisan"]],
            "son_24_saat": office["son_24_saat"], "ardisik_hata": office["ardisik_hata"],
            "son_hatalar": [{**item, "zaman": _shown(item["zaman"]), "kategori_adi": CATEGORIES[item["kategori"]]}
                            for item in office["son_hatalar"]],
        })
    disk = status.get("bos_disk")
    view = {"id": row["id"], "etiket": row["etiket"] or "Adsız bilgisayar", "surum": row["surum"],
            "isletim_sistemi": row["isletim_sistemi"], "son_gorulme": stamp(row["son_gorulme"]),
            "dakika": max(0, int(age.total_seconds() // 60)), "renk": _color(age), "renk_adi": COLORS[_color(age)],
            "kapali": row["kapali"], "ekran_kilitli": status.get("ekran_kilitli"), "bos_disk": disk,
            "disk_uyari": disk is not None and disk < DISK_WARNING, "sirada": status.get("sirada", 0),
            "komut_verilebilir": not row["kapali"] and age < YELLOW,
            "ofisler": offices, "komutlar": [_command_view(item, moment) for item in commands]}
    if not owner:
        view["kullanici"] = {"id": row["kullanici_id"], "giris": row["giris"], "ad_soyad": row["ad_soyad"]}
    return view


def listing(core, cur: Cursor, *, owner: int | None = None) -> list[dict[str, Any]]:
    """Robotlar, en son görüleni üstte; `owner` verilirse yalnız o hesabınkiler (Hesabım)."""
    moment = core.clock.now()
    condition, values = ("WHERE r.kullanici_id = %s", [owner]) if owner is not None else ("", [])
    cur.execute("SELECT r.*, k.giris, k.ad_soyad FROM robotlar r JOIN kullanicilar k ON k.id = r.kullanici_id "
                + condition + " ORDER BY r.son_gorulme DESC, r.id DESC LIMIT 500", values)
    robots = cur.fetchall()
    commands: dict[int, list[dict[str, Any]]] = {}
    if robots:
        cur.execute("SELECT * FROM robot_komutlari WHERE robot_id = ANY(%s) AND olusturuldu > %s ORDER BY id DESC",
                    ([row["id"] for row in robots], moment - timedelta(days=1)))
        for row in cur.fetchall():
            listed = commands.setdefault(row["robot_id"], [])
            if len(listed) < RECENT_COMMANDS:
                listed.append(row)
    return [robot_view(row, commands.get(row["id"], []), moment, owner=owner is not None) for row in robots]


def _target(status: dict, kind: str, target: str) -> tuple[str | None, str] | None:
    """(ofis, akış adı): durdur için çalışan bir çalışma, yeniden çalıştır için son hatalardaki bir akış."""
    for office in status.get("ofisler", []):
        entries = office["calisan"] if kind == "durdur" else office["son_hatalar"]
        key = "run_id" if kind == "durdur" else "workflow_id"
        for item in entries:
            if item[key] == target:
                return office["id"], item["akis"]
    return None


def create_command(core, cur: Cursor, robot_id: int, kind: Any, target: Any, *, actor: dict[str, Any],
                   source: str, owner: int | None = None) -> dict[str, Any]:
    """Komut yazar ve olay kaydına geçirir. `owner` verilirse robot o hesabın olmalıdır (başkasınınki bulunamadı)."""
    if kind not in COMMANDS:
        raise ApiError("Komut geçersiz.")
    if not isinstance(target, str) or not IDENT.fullmatch(target):
        raise ApiError("Komutun hedefi geçersiz.")
    query = "SELECT * FROM robotlar WHERE id = %s" + (" AND kullanici_id = %s" if owner is not None else "")
    cur.execute(query + " FOR UPDATE", (robot_id,) + ((owner,) if owner is not None else ()))
    robot = cur.fetchone()
    if robot is None:
        raise ApiError(NOT_FOUND, 404, "BULUNAMADI")
    moment = core.clock.now()
    if robot["kapali"] or moment - robot["son_gorulme"] >= YELLOW:
        raise ApiError("Robot şu anda bildirim göndermiyor; komut ona ulaşmaz.", 409, "ROBOT_KAPALI")
    found = _target(robot["durum"] or {}, kind, target)
    if found is None:
        raise ApiError("Bu çalışma artık çalışmıyor." if kind == "durdur"
                       else "Bu akış robotun son hatalı çalışmaları arasında yok.", 409, "HEDEF_YOK")
    cur.execute("SELECT count(*) FILTER (WHERE tur = %s AND hedef = %s) AS ayni, count(*) AS hepsi FROM robot_komutlari "
                "WHERE robot_id = %s AND sonuc IS NULL AND olusturuldu > %s",
                (kind, target, robot_id, moment - COMMAND_LIFETIME))
    pending = cur.fetchone()
    if pending["ayni"]:
        raise ApiError("Bu komut zaten robota gönderilmeyi bekliyor.", 409, "BEKLIYOR")
    if pending["hepsi"] >= PENDING_LIMIT:
        raise ApiError("Bu robot için bekleyen komut çok; robot bildirince yeniden deneyin.", 429, "COK_SIK")
    office, name = found
    cur.execute("INSERT INTO robot_komutlari (robot_id, tur, hedef, ofis_id, akis, olusturan, olusturan_id, olusturuldu) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (robot_id, kind, target, office, name, source, actor["id"], moment))
    from ..admin import events

    details = {"komut": kind, "akis": name, "kullanici_id": robot["kullanici_id"]}
    if source == "yonetici":
        events.record(core, cur, actor["id"], "robot_komutu", f"robot:{robot_id}", **details)
    else:
        events.record(core, cur, None, "robot_komutu_uye", f"robot:{robot_id}", **details, kaynak="hesabim")
    cur.execute("SELECT r.*, k.giris, k.ad_soyad FROM robotlar r JOIN kullanicilar k ON k.id = r.kullanici_id "
                "WHERE r.id = %s", (robot_id,))
    row = cur.fetchone()
    cur.execute("SELECT * FROM robot_komutlari WHERE robot_id = %s AND olusturuldu > %s ORDER BY id DESC LIMIT %s",
                (robot_id, moment - timedelta(days=1), RECENT_COMMANDS))
    return robot_view(row, cur.fetchall(), moment, owner=owner is not None)
