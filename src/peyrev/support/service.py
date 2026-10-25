"""Destek talepleri: doğrulama, Studio işlemleri ve yönetim panelinin servis işlevleri.

Kullanıcı talebini Studio'dan yazar: yeni adım önerisi, adım iyileştirme, hata bildirimi, soru. İsteğe bağlı ekler
(değerleri Studio'da maskelenmiş akış yapısı, son çalışmanın hata kaydı, en çok üç görsel) talep ve mesajlarla birlikte
veritabanında tutulur; diske hiçbir şey yazılmaz. Kurallar orkestrai'deki rpa_destek ile aynıdır; firma yoktur, talep
kullanıcıya bağlıdır ve herkes yalnız kendi taleplerini görür. Günlük sınırlar Türkiye gününe göre sayılır.

Yönetim paneli bu işlevleri route olarak değil Python'dan, kendi işleminin imleciyle çağırır; panel yönetici oturumunu
doğrular, her işlev yöneticiyi aynı işlemde yeniden denetler (etkin 'yonetici' rolü; değilse 403 YETKI_YOK):
  list_tickets(core, cur, admin_id, *, durum=None, tur=None) -> {"talepler", "okunmamis", "turler", "durumlar"}
  open_ticket(core, cur, admin_id, number) -> talep ayrıntısı; yonetici_gordu şimdi olur
  answer_ticket(core, cur, admin_id, number, *, metin=None, durum=None, yayin_surumu=None) -> talep ayrıntısı
  ticket_attachment(core, cur, admin_id, number, attachment_id) -> (ad, bicim, icerik)
  unread_count(core, cur, admin_id) -> int  (panel rozeti: kullanıcının yazdığı, yöneticinin henüz görmediği talepler)
Yanıt ve durum değişikliği yonetim_olaylari'na yazılır (yanıt metni yazılmaz).
"""

from __future__ import annotations

import base64
import json
import re
import time
import uuid
from datetime import datetime
from typing import Any

from psycopg import Cursor
from psycopg.types.json import Jsonb

from .. import tokens, users
from ..clock import TR
from ..common import ApiError
from ..db import advisory_lock

TYPES = {"adim_onerisi": "Yeni adım önerisi", "adim_iyilestirme": "Adım iyileştirme", "hata": "Hata bildirimi",
         "soru": "Soru", "diger": "Diğer"}
STATES = {"acik": "Açık", "inceleniyor": "İnceleniyor", "yanitlandi": "Yanıtlandı", "planlandi": "Planlandı",
          "yayinlandi": "Yayınlandı", "kapandi": "Kapatıldı"}
TITLE_LIMIT, TEXT_LIMIT = 120, 5000
JSON_ATTACHMENT_LIMIT = 200 * 1024
IMAGE_LIMIT, IMAGES_PER_TICKET = 1536 * 1024, 3
IMAGE_TYPES = {"image/png": b"\x89PNG\r\n\x1a\n", "image/jpeg": b"\xff\xd8\xff"}
# A new ticket may carry three base64 images; a reply is text only.
CREATE_BODY_LIMIT = 7 * 1024 * 1024
BODY_LIMIT = 16 * 1024
TICKETS_PER_DAY, MESSAGES_PER_DAY = 10, 50
LIST_LIMIT = 200
NUMBER_LIMIT = 2**31 - 1  # talep_no is an integer column
STEP_TYPE = re.compile(r"[a-z][a-z0-9_]{0,40}\.[a-z][a-z0-9_]{0,40}")
VERSION = re.compile(r"[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}")
ATTACHMENT_ID = re.compile(r"[0-9a-f]{32}")
PLATFORMS = {"macOS", "Windows", "Linux"}
LICENSE_REQUIRED = "Destek talebi için geçerli Peyrev lisansı gerekli."
ADMIN_ONLY = "Bu işlem yalnız platform yöneticisine açıktır."
TICKET_SELECT = ("SELECT t.*, k.ad_soyad, k.giris FROM destek_talepleri t "
                 "LEFT JOIN kullanicilar k ON k.id = t.kullanici_id")


# ----- biçim ve doğrulama -----------------------------------------------------------------------
def _stamp(value: datetime | None) -> str | None:
    """Studio ve panel Türkiye saatini gösterir."""
    return value.astimezone(TR).strftime("%Y-%m-%d %H:%M") if value is not None else None


def _later(first: datetime | None, second: datetime | None) -> bool:
    """İlk an ikinciden sonraysa True; ikincisi yoksa hiç görülmemiş sayılır."""
    return first is not None and (second is None or first > second)


def _ordered_id() -> str:
    """Zamana göre sıralanan kimlik: aynı anda yazılan mesajlar ve ekler de yazıldığı sırayla gösterilir."""
    return f"{time.time_ns() // 1000:014x}{uuid.uuid4().hex[:18]}"


def _text(value: Any, label: str, limit: int, *, lines: bool = False, required: bool = True) -> str | None:
    if value is None or value == "":
        if required:
            raise ApiError(label + " gerekli.")
        return None
    if not isinstance(value, str):
        raise ApiError(label + " geçersiz.")
    value = value.replace("\r\n", "\n").strip()
    if not value and required:
        raise ApiError(label + " gerekli.")
    if len(value) > limit:
        raise ApiError(f"{label} en fazla {limit} karakter olabilir.")
    allowed = "\n\t" if lines else ""
    # Lone surrogates cannot be stored as PostgreSQL text: they are refused like control characters.
    if any((ord(char) < 32 and char not in allowed) or char == "\x7f" or "\ud800" <= char <= "\udfff"
           for char in value):
        raise ApiError(label + " geçersiz karakter içeriyor.")
    if not lines and ("<" in value or ">" in value):
        raise ApiError(label + " < veya > içeremez.")
    return value


def _attachments(values: Any) -> list[tuple[str, str, str, bytes]]:
    """Denetlenmiş ekler: (tür, ad, biçim, içerik). Studio'dan gelen hiçbir şeye güvenilmez."""
    if values is None:
        return []
    if not isinstance(values, list) or len(values) > IMAGES_PER_TICKET + 2:
        raise ApiError("Ekler geçersiz.")
    rows: list[tuple[str, str, str, bytes]] = []
    kinds: set[str] = set()
    images = 0
    for item in values:
        if not isinstance(item, dict) or item.get("tur") not in ("akis", "hata_kaydi", "gorsel"):
            raise ApiError("Ekler geçersiz.")
        kind = item["tur"]
        if kind == "gorsel":
            if set(item) - {"tur", "ad", "bicim", "veri"}:
                raise ApiError("Görsel eki geçersiz.")
            images += 1
            if images > IMAGES_PER_TICKET:
                raise ApiError(f"En fazla {IMAGES_PER_TICKET} görsel eklenebilir.")
            media = item.get("bicim")
            if not isinstance(media, str) or media not in IMAGE_TYPES or not isinstance(item.get("veri"), str):
                raise ApiError("Yalnız PNG veya JPEG görsel eklenebilir.")
            if len(item["veri"]) > IMAGE_LIMIT * 4 // 3 + 8:
                raise ApiError("Görsel en fazla 1,5 MB olabilir.")
            try:
                data = base64.b64decode(item["veri"].encode("ascii"), validate=True)
            except (ValueError, UnicodeError):
                raise ApiError("Görsel okunamadı.") from None
            if not data or len(data) > IMAGE_LIMIT:
                raise ApiError("Görsel en fazla 1,5 MB olabilir.")
            if not data.startswith(IMAGE_TYPES[media]):
                raise ApiError("Görsel dosyası bildirilen türde değil.")
            name = _text(item.get("ad"), "Görsel adı", 80, required=False) or "gorsel"
            rows.append(("gorsel", name, media, data))
            continue
        if kind in kinds or set(item) - {"tur", "icerik"} or not isinstance(item.get("icerik"), (dict, list)):
            raise ApiError("Ekler geçersiz.")
        kinds.add(kind)
        try:
            data = json.dumps(item["icerik"], ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8")
        except (ValueError, UnicodeError, RecursionError):
            raise ApiError("Ekler geçersiz.") from None
        if len(data) > JSON_ATTACHMENT_LIMIT:
            raise ApiError("Akış ve hata kaydı ekleri en fazla 200 KB olabilir.")
        rows.append((kind, "akis.json" if kind == "akis" else "hata-kaydi.json", "application/json", data))
    return rows


def ticket_number(value: Any) -> int:
    if type(value) is not int or not 1 <= value <= NUMBER_LIMIT:
        raise ApiError("Talep bulunamadı.", 404, "BULUNAMADI")
    return value


def attachment_id(value: Any) -> str:
    if not isinstance(value, str) or not ATTACHMENT_ID.fullmatch(value):
        raise ApiError("Ek bulunamadı.", 404, "BULUNAMADI")
    return value


def new_ticket(body: dict[str, Any]) -> dict[str, Any]:
    """Studio'nun gönderdiği talebi veritabanına gitmeden denetler; kaydedilecek alanları döndürür."""
    kind = body.get("tur")
    if not isinstance(kind, str) or kind not in TYPES:
        raise ApiError("Talep türü geçersiz.")
    title = _text(body.get("baslik"), "Başlık", TITLE_LIMIT)
    text = _text(body.get("metin"), "Açıklama", TEXT_LIMIT, lines=True)
    step = body.get("adim")
    if step not in (None, "") and (not isinstance(step, str) or not STEP_TYPE.fullmatch(step)):
        raise ApiError("İlgili adım geçersiz.")
    version, platform = body.get("surum"), body.get("platform")
    if (not isinstance(version, str) or not VERSION.fullmatch(version) or not isinstance(platform, str)
            or platform not in PLATFORMS):
        raise ApiError("Studio sürümü veya platform bilgisi geçersiz.")
    return {"tur": kind, "baslik": title, "metin": text, "adim": step or None, "surum": version, "platform": platform,
            "ekler": _attachments(body.get("ekler"))}


def reply_text(value: Any) -> str:
    return _text(value, "Yanıt", TEXT_LIMIT, lines=True)


# ----- ortak --------------------------------------------------------------------------------------
def studio_user(core, cur: Cursor, authorization: str | None) -> dict[str, Any]:
    """Destek belirteci, açık Studio oturumu ve geçerli Peyrev lisansı (yönetici MOD_RPA satırı olmadan da geçer)."""
    user, _claims = tokens.verify(core, cur, "destek", authorization)
    if users.module_state(cur, user, users.RPA, core.clock.today_tr())[0] != users.ACTIVE:
        raise ApiError(LICENSE_REQUIRED, 403, "YETKI_YOK")
    return user


def _admin(cur: Cursor, admin_id: Any) -> dict[str, Any]:
    admin = users.get(cur, admin_id) if type(admin_id) is int and admin_id > 0 else None
    if admin is None or admin["rol"] != "yonetici" or admin["durum"] != "aktif":
        raise ApiError(ADMIN_ONLY, 403, "YETKI_YOK")
    return admin


def _ticket(cur: Cursor, number: int, owner: int | None = None, *, lock: bool = False) -> dict[str, Any]:
    query, parameters = TICKET_SELECT + " WHERE t.talep_no = %s", [number]
    if owner is not None:
        query += " AND t.kullanici_id = %s"
        parameters.append(owner)
    cur.execute(query + (" FOR UPDATE OF t" if lock else ""), parameters)
    ticket = cur.fetchone()
    if ticket is None:
        raise ApiError("Talep bulunamadı.", 404, "BULUNAMADI")
    return ticket


def _summary(row: dict[str, Any], viewer: str) -> dict[str, Any]:
    seen = row["kullanici_gordu"] if viewer == "kullanici" else row["yonetici_gordu"]
    other = "yonetici" if viewer == "kullanici" else "kullanici"
    result = {"no": row["talep_no"], "tur": row["tur"], "tur_adi": TYPES.get(row["tur"], row["tur"]),
              "baslik": row["baslik"], "durum": row["durum"], "durum_adi": STATES.get(row["durum"], row["durum"]),
              "adim": row["adim_turu"], "surum": row["surum"], "platform": row["platform"],
              "yayin_surumu": row["yayin_surumu"], "olusturma": _stamp(row["olusturma"]),
              "guncelleme": _stamp(row["guncelleme"]),
              "okunmamis": row["son_yazan"] == other and _later(row["guncelleme"], seen)}
    if viewer == "yonetici":
        login = row.get("giris") or ""
        result.update(kullanici=row.get("ad_soyad") or "", kullanici_id=row["kullanici_id"], giris=login,
                      eposta=login if "@" in login else "")
    return result


def _detail(cur: Cursor, ticket: dict[str, Any], viewer: str) -> dict[str, Any]:
    cur.execute("SELECT id, yazan, metin, olusturma FROM destek_mesajlari WHERE talep_no = %s ORDER BY olusturma, id",
                (ticket["talep_no"],))
    messages = [{"id": row["id"], "yazan": row["yazan"], "metin": row["metin"], "zaman": _stamp(row["olusturma"])}
                for row in cur.fetchall()]
    cur.execute("SELECT id, tur, ad, bicim, boyut FROM destek_ekleri WHERE talep_no = %s ORDER BY olusturma, id",
                (ticket["talep_no"],))
    attachments = [{"id": row["id"], "tur": row["tur"], "ad": row["ad"], "bicim": row["bicim"], "boyut": row["boyut"]}
                   for row in cur.fetchall()]
    result = _summary(ticket, viewer)
    result.update(mesajlar=messages, ekler=attachments)
    return result


def _today(core, cur: Cursor, statement: str, user_id: int) -> int:
    """Bugün (Europe/Istanbul) yazılanların sayısı."""
    cur.execute(statement, (user_id, datetime.combine(core.clock.today_tr(), datetime.min.time(), TR)))
    return int(cur.fetchone()["adet"])


def _add_message(cur: Cursor, number: int, author_id: int, author: str, text: str, moment: datetime) -> None:
    cur.execute("INSERT INTO destek_mesajlari (id, talep_no, yazan_id, yazan, metin, olusturma) "
                "VALUES (%s, %s, %s, %s, %s, %s)", (_ordered_id(), number, author_id, author, text, moment))


def admin_link(core, number: int) -> str:
    return f"{core.settings.public_base.rstrip('/')}/yonetim/#destek-{number}"


def _notice(core, subject: str, user: dict[str, Any], ticket: dict[str, Any], text: str) -> tuple[str, str]:
    """Yöneticiye giden düz metin bildirim: kimden, ne, hangi Studio; panelde talebin bağlantısı."""
    lines = [subject, "",
             f"Kimden: {user.get('ad_soyad') or ''} ({user.get('giris') or ''})",
             f"Tür: {TYPES.get(ticket['tur'], ticket['tur'])}",
             f"Başlık: {ticket['baslik']}",
             f"Studio: {ticket['surum']} · {ticket['platform']}", "",
             text[:1500], "", f"Yanıtlamak için: {admin_link(core, ticket['talep_no'])}"]
    return subject, "\n".join(lines)


# ----- Studio -------------------------------------------------------------------------------------
def create(core, cur: Cursor, user: dict[str, Any], fields: dict[str, Any]) -> tuple[dict[str, Any], tuple[str, str]]:
    """Yeni talep: (Studio'ya dönen ayrıntı, işlem kaydedilince yöneticiye gidecek bildirim)."""
    # Two requests of one person cannot pass the daily limit together.
    advisory_lock(cur, f"destek:{user['id']}")
    if _today(core, cur, "SELECT count(*) AS adet FROM destek_talepleri WHERE kullanici_id = %s AND olusturma >= %s",
              user["id"]) >= TICKETS_PER_DAY:
        raise ApiError("Bugün için talep sınırına ulaştınız. Yarın tekrar yazabilir veya açık talebinize "
                       "yanıt ekleyebilirsiniz.", 429, "SINIR")
    moment = core.clock.now()
    cur.execute("INSERT INTO destek_talepleri (kullanici_id, tur, baslik, adim_turu, surum, platform, olusturma, "
                "guncelleme, kullanici_gordu) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING talep_no",
                (user["id"], fields["tur"], fields["baslik"], fields["adim"], fields["surum"], fields["platform"],
                 moment, moment, moment))
    number = cur.fetchone()["talep_no"]
    _add_message(cur, number, user["id"], "kullanici", fields["metin"], moment)
    for kind, name, media, data in fields["ekler"]:
        cur.execute("INSERT INTO destek_ekleri (id, talep_no, tur, ad, bicim, boyut, icerik, olusturma) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                    (_ordered_id(), number, kind, name, media, len(data), data, moment))
    ticket = _ticket(cur, number, user["id"])
    notice = _notice(core, f"Yeni destek talebi #{number} · {TYPES[fields['tur']]}", user, ticket, fields["metin"])
    return _detail(cur, ticket, "kullanici"), notice


def mine(cur: Cursor, user: dict[str, Any]) -> list[dict[str, Any]]:
    cur.execute("SELECT * FROM destek_talepleri WHERE kullanici_id = %s ORDER BY guncelleme DESC, talep_no DESC "
                "LIMIT %s", (user["id"], LIST_LIMIT))
    return [_summary(row, "kullanici") for row in cur.fetchall()]


def open_mine(core, cur: Cursor, user: dict[str, Any], number: int) -> dict[str, Any]:
    ticket = _ticket(cur, number, user["id"])
    moment = core.clock.now()
    cur.execute("UPDATE destek_talepleri SET kullanici_gordu = %s WHERE talep_no = %s", (moment, number))
    ticket["kullanici_gordu"] = moment
    return _detail(cur, ticket, "kullanici")


def reply_mine(core, cur: Cursor, user: dict[str, Any], number: int,
               text: str) -> tuple[dict[str, Any], tuple[str, str]]:
    advisory_lock(cur, f"destek:{user['id']}")
    ticket = _ticket(cur, number, user["id"], lock=True)
    if ticket["durum"] == "kapandi":
        raise ApiError("Bu talep kapatıldı. Yeni bir talep açabilirsiniz.", 409, "KAPALI")
    if _today(core, cur, "SELECT count(*) AS adet FROM destek_mesajlari WHERE yazan_id = %s AND olusturma >= %s",
              user["id"]) >= MESSAGES_PER_DAY:
        raise ApiError("Bugün için mesaj sınırına ulaştınız.", 429, "SINIR")
    moment = core.clock.now()
    _add_message(cur, number, user["id"], "kullanici", text, moment)
    # A user's answer reopens a ticket that was waiting for them.
    state = "acik" if ticket["durum"] == "yanitlandi" else ticket["durum"]
    cur.execute("UPDATE destek_talepleri SET son_yazan = 'kullanici', durum = %s, guncelleme = %s, "
                "kullanici_gordu = %s WHERE talep_no = %s", (state, moment, moment, number))
    ticket = _ticket(cur, number, user["id"])
    notice = _notice(core, f"Destek talebine yanıt #{number}", user, ticket, text)
    return _detail(cur, ticket, "kullanici"), notice


def overview(cur: Cursor, user: dict[str, Any]) -> dict[str, Any]:
    """Studio rozeti: okunmamış yanıt sayısı ve yayınlanan öneriler."""
    rows = mine(cur, user)
    return {"okunmamis": sum(1 for row in rows if row["okunmamis"]),
            "yayinlanan": [{"no": row["no"], "baslik": row["baslik"], "surum": row["yayin_surumu"]}
                           for row in rows if row["durum"] == "yayinlandi" and row["yayin_surumu"]]}


def attachment(cur: Cursor, number: int, identifier: str, owner: int | None = None) -> tuple[str, str, bytes]:
    """(ad, biçim, içerik); `owner` verilirse yalnız o kullanıcının talebinden."""
    _ticket(cur, number, owner)
    cur.execute("SELECT ad, bicim, icerik FROM destek_ekleri WHERE id = %s AND talep_no = %s", (identifier, number))
    row = cur.fetchone()
    if row is None:
        raise ApiError("Ek bulunamadı.", 404, "BULUNAMADI")
    return row["ad"], row["bicim"], bytes(row["icerik"])


# ----- yönetici -----------------------------------------------------------------------------------
def _unread(cur: Cursor) -> int:
    cur.execute("SELECT count(*) AS adet FROM destek_talepleri WHERE son_yazan = 'kullanici' "
                "AND (yonetici_gordu IS NULL OR guncelleme > yonetici_gordu)")
    return int(cur.fetchone()["adet"])


def _state(state: Any, release: Any, current: str) -> tuple[str, str | None]:
    if state in (None, ""):
        return current, None
    if not isinstance(state, str) or state not in STATES:
        raise ApiError("Durum geçersiz.")
    if state == "yayinlandi":
        if not isinstance(release, str) or not VERSION.fullmatch(release):
            raise ApiError("Yayınlandı durumu için sürüm numarası yazın (ör. 0.9.53).")
        return state, release
    return state, None


def list_tickets(core, cur: Cursor, admin_id: int, *, durum: str | None = None,
                 tur: str | None = None) -> dict[str, Any]:
    """Bütün talepler (son güncellenen önce, en çok 200), isteğe bağlı durum ve tür süzgeciyle.

    `okunmamis` süzgeçten bağımsız rozet sayısıdır; `turler` ve `durumlar` süzgeç seçeneklerinin adlarıdır.
    """
    _admin(cur, admin_id)
    if durum not in (None, "") and (not isinstance(durum, str) or durum not in STATES):
        raise ApiError("Durum süzgeci geçersiz.")
    if tur not in (None, "") and (not isinstance(tur, str) or tur not in TYPES):
        raise ApiError("Tür süzgeci geçersiz.")
    query, parameters = TICKET_SELECT + " WHERE true", []
    if durum:
        query += " AND t.durum = %s"
        parameters.append(durum)
    if tur:
        query += " AND t.tur = %s"
        parameters.append(tur)
    cur.execute(query + " ORDER BY t.guncelleme DESC, t.talep_no DESC LIMIT %s", [*parameters, LIST_LIMIT])
    tickets = [_summary(row, "yonetici") for row in cur.fetchall()]
    return {"talepler": tickets, "okunmamis": _unread(cur), "turler": dict(TYPES), "durumlar": dict(STATES)}


def open_ticket(core, cur: Cursor, admin_id: int, number: int) -> dict[str, Any]:
    """Talebin mesajları ve ekleri; yönetici gördü olarak işaretlenir (rozetten düşer)."""
    _admin(cur, admin_id)
    number = ticket_number(number)
    ticket = _ticket(cur, number)
    moment = core.clock.now()
    cur.execute("UPDATE destek_talepleri SET yonetici_gordu = %s WHERE talep_no = %s", (moment, number))
    ticket["yonetici_gordu"] = moment
    return _detail(cur, ticket, "yonetici")


def answer_ticket(core, cur: Cursor, admin_id: int, number: int, *, metin: str | None = None,
                  durum: str | None = None, yayin_surumu: str | None = None) -> dict[str, Any]:
    """Yanıt yazar ve/veya durumu değiştirir; kullanıcı talebi Studio'da okunmamış görür.

    Yanıt yazılıp durum verilmezse talep "yanıtlandı" olur; ne yanıt ne değişiklik varsa 400. "Yayınlandı" için
    yayın sürümü (X.Y.Z) zorunludur ve Studio öneriyi o sürümün notlarında gösterir; başka durumda sürüm silinir.
    """
    admin = _admin(cur, admin_id)
    number = ticket_number(number)
    text = _text(metin, "Yanıt", TEXT_LIMIT, lines=True, required=False)
    ticket = _ticket(cur, number, lock=True)
    state, release = _state(durum, yayin_surumu, ticket["durum"] if not text else "yanitlandi")
    moment = core.clock.now()
    if text:
        _add_message(cur, number, admin["id"], "yonetici", text, moment)
    elif state == ticket["durum"] and release in (None, ticket["yayin_surumu"]):
        raise ApiError("Yanıt yazın veya durumu değiştirin.")
    cur.execute("UPDATE destek_talepleri SET durum = %s, yayin_surumu = %s, guncelleme = %s, yonetici_gordu = %s, "
                "son_yazan = 'yonetici' WHERE talep_no = %s", (state, release, moment, moment, number))
    details = {"onceki_durum": ticket["durum"], "durum": state, "yanit_uzunlugu": len(text) if text else 0}
    if release:
        details["yayin_surumu"] = release
    cur.execute("INSERT INTO yonetim_olaylari (yonetici_id, tur, hedef, ayrinti, zaman) VALUES (%s, %s, %s, %s, %s)",
                (admin["id"], "destek_yanit" if text else "destek_durum", f"talep:{number}", Jsonb(details), moment))
    return _detail(cur, _ticket(cur, number), "yonetici")


def ticket_attachment(core, cur: Cursor, admin_id: int, number: int, identifier: str) -> tuple[str, str, bytes]:
    """(ad, biçim, içerik); panel görselleri satır içi, akış ve hata kaydını indirme olarak verir (file_response)."""
    _admin(cur, admin_id)
    return attachment(cur, ticket_number(number), attachment_id(identifier))


def unread_count(core, cur: Cursor, admin_id: int) -> int:
    _admin(cur, admin_id)
    return _unread(cur)
