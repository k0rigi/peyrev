"""Topluluk içeriği: konular, yorumlar ve konuya eklenen Studio akışı.

Kategoriler: duyuru (Duyurular ve belgeler; yalnız yönetici açar), soru, akis (akış paylaşımı), oneri. Yazılanlar hemen
yayınlanır; yönetici panelden gizler, sabitler, yoruma kapatır ya da siler. Gizli konu ve yorum üyelere görünmez.
Yazar kendi konusunu ve yorumunu silebilir. Spam'e karşı hesap başına saatte en çok TOPICS_PER_HOUR konu ve
WRITES_PER_HOUR yazı (konu + yorum). Üyeler birbirinin yalnız görünen adını görür (e-posta gösterilmez).

Akış: Studio'nun "Dışa aktar" dosyası (JSON, en çok FLOW_LIMIT bayt; name ve steps alanları) olduğu gibi saklanır ve
indirilir; Studio "İçe aktar" ile açar. Listede adımların başlığı, türü ve derinliği özet olarak gösterilir; komut
çalıştırma, web betiği, veritabanına yazma gibi adımlar "dikkat" olarak işaretlenir.
"""

from __future__ import annotations

import json
import re
from datetime import timedelta
from typing import Any

from psycopg import Cursor

from ..accounts import rules
from ..common import ApiError
from ..uye import display_name

CATEGORIES = {"duyuru": "Duyurular ve belgeler", "soru": "Soru ve yardım", "akis": "Akış paylaşımı",
              "oneri": "Öneri ve fikir"}
ADMIN_CATEGORIES = {"duyuru"}
PAGE_SIZE = 30
TOPICS_PER_HOUR = 5
WRITES_PER_HOUR = 30
FLOW_LIMIT = 1024 * 1024
SUMMARY_LIMIT = 300
RISKY = {"system.command", "web.script", "database.execute", "file.operation", "http.request", "ftp.transfer",
         "mail.send", "notify.send", "clipboard.set", "input.type", "input.hotkey"}
BRANCHES = ("children", "otherwise", "branches", "steps")
ACTION = re.compile(r"[a-z][a-z0-9_]{0,40}(?:\.[a-z0-9_]{1,40}){1,3}")


def _text(value: Any, label: str, minimum: int, maximum: int) -> str:
    """Çok satırlı metin: satır sonları korunur, denetim karakterleri reddedilir; HTML olarak hiç yorumlanmaz."""
    if not isinstance(value, str):
        raise ApiError(f"{label} {minimum}-{maximum} karakter olmalıdır.")
    text = "\n".join(line.rstrip() for line in value.replace("\r\n", "\n").replace("\r", "\n").strip().split("\n"))
    text = re.sub(r"\n{3,}", "\n\n", text)
    if not minimum <= len(text) <= maximum:
        raise ApiError(f"{label} {minimum}-{maximum} karakter olmalıdır.")
    if any(ord(char) < 32 and char not in "\n\t" for char in text):
        raise ApiError(f"{label} geçersiz karakter içeriyor.")
    return text


def flow_summary(data: Any) -> tuple[str, int, list[dict[str, Any]]]:
    """(ad, adım sayısı, özet). Dosya Studio'nun dışa aktardığı akış biçiminde olmalıdır."""
    if not isinstance(data, dict) or not isinstance(data.get("steps"), list) or not data["steps"]:
        raise ApiError("Akış dosyası okunamadı. Peyrev'de akışı açıp \"Dışa aktar\" ile aldığınız .json dosyasını seçin.")
    name = data.get("name")
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 200:
        raise ApiError("Akışın adı okunamadı. Peyrev'de dışa aktardığınız dosyayı seçin.")
    summary: list[dict[str, Any]] = []
    count = 0

    def walk(steps: Any, depth: int) -> None:
        nonlocal count
        if not isinstance(steps, list) or depth > 12:
            return
        for step in steps:
            if not isinstance(step, dict) or not isinstance(step.get("action"), str) or not ACTION.fullmatch(step["action"]):
                raise ApiError("Akış dosyasında okunamayan bir adım var. Peyrev'de dışa aktardığınız dosyayı seçin.")
            count += 1
            if len(summary) < SUMMARY_LIMIT:
                title = step.get("title") if isinstance(step.get("title"), str) else ""
                summary.append({"baslik": " ".join(title.split())[:120], "tur": step["action"], "derinlik": depth,
                                "dikkat": step["action"] in RISKY})
            for key in BRANCHES:
                value = step.get(key)
                if isinstance(value, list):
                    walk(value, depth + 1)
                elif isinstance(value, dict):  # branches keyed by name
                    for branch in value.values():
                        walk(branch, depth + 1)

    walk(data["steps"], 0)
    return " ".join(name.split()), count, summary


def _author(row: dict[str, Any], prefix: str = "yazar_") -> dict[str, Any]:
    user = {"ad_soyad": row[prefix + "ad_soyad"], "giris": row[prefix + "giris"]}
    return {"ad": display_name(user), "yonetici": row[prefix + "rol"] == "yonetici"}


TOPIC_COLUMNS = ("k.*, u.ad_soyad AS yazar_ad_soyad, u.giris AS yazar_giris, u.rol AS yazar_rol, "
                 "a.ad AS akis_ad, a.adim_sayisi AS akis_adim, a.boyut AS akis_boyut, a.indirme AS akis_indirme, "
                 "a.ozet AS akis_ozet")
TOPIC_FROM = ("topluluk_konulari k JOIN kullanicilar u ON u.id = k.yazar_id "
              "LEFT JOIN topluluk_akislari a ON a.konu_id = k.id")


def topic_view(row: dict[str, Any], viewer: dict[str, Any] | None = None, *, full: bool = False,
               admin: bool = False) -> dict[str, Any]:
    item = {"id": row["id"], "kategori": row["kategori"], "baslik": row["baslik"], "yazar": _author(row),
            "sabit": row["sabit"], "kilitli": row["kilitli"], "yanit_sayisi": row["yanit_sayisi"],
            "goruntulenme": row["goruntulenme"], "olusturma": rules.stamp(row["olusturma"]),
            "son_hareket": rules.stamp(row["son_hareket"]), "akis": row["akis_ad"] is not None}
    if full:
        item["metin"] = row["metin"]
        item["benim"] = viewer is not None and viewer["id"] == row["yazar_id"]
        if row["akis_ad"] is not None:
            item["akis"] = {"ad": row["akis_ad"], "adim_sayisi": row["akis_adim"], "boyut": row["akis_boyut"],
                            "indirme": row["akis_indirme"], "adimlar": row["akis_ozet"]}
    else:
        item["ozet"] = " ".join(row["metin"].split())[:220]
    if admin:
        item.update(gizli=row["gizli"], yazar_giris=row["yazar_giris"], yazar_id=row["yazar_id"])
    return item


def listing(cur: Cursor, *, kategori: Any = None, ara: Any = None, sayfa: int = 1, admin: bool = False) -> dict[str, Any]:
    if kategori not in (None, "") and kategori not in CATEGORIES:
        raise ApiError("Kategori geçersiz.")
    clauses, values = ([] if admin else ["NOT k.gizli"]), []
    if kategori:
        clauses.append("k.kategori = %s")
        values.append(kategori)
    if isinstance(ara, str) and ara.strip():
        term = "%" + ara.strip()[:100].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        clauses.append("(k.baslik ILIKE %s OR k.metin ILIKE %s)")
        values += [term, term]
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    cur.execute(f"SELECT count(*) AS n FROM topluluk_konulari k {where}", values)
    total = cur.fetchone()["n"]
    cur.execute(f"SELECT {TOPIC_COLUMNS} FROM {TOPIC_FROM} {where} ORDER BY k.sabit DESC, k.son_hareket DESC, k.id DESC "
                "LIMIT %s OFFSET %s", values + [PAGE_SIZE, (sayfa - 1) * PAGE_SIZE])
    return {"konular": [topic_view(row, admin=admin) for row in cur.fetchall()], "toplam": total, "sayfa": sayfa,
            "sayfa_boyu": PAGE_SIZE, "kategoriler": CATEGORIES}


def _limits(cur: Cursor, user_id: int, moment, *, topic: bool) -> None:
    since = moment - timedelta(hours=1)
    cur.execute("SELECT (SELECT count(*) FROM topluluk_konulari WHERE yazar_id = %s AND olusturma > %s) AS konu, "
                "(SELECT count(*) FROM topluluk_yorumlari WHERE yazar_id = %s AND olusturma > %s) AS yorum",
                (user_id, since, user_id, since))
    row = cur.fetchone()
    if (topic and row["konu"] >= TOPICS_PER_HOUR) or row["konu"] + row["yorum"] >= WRITES_PER_HOUR:
        raise ApiError("Çok sık yazıyorsunuz. Bir süre sonra tekrar deneyin.", 429, "COK_SIK")


def create_topic(core, cur: Cursor, user: dict[str, Any], data: dict[str, Any]) -> int:
    kategori = data.get("kategori")
    if kategori not in CATEGORIES:
        raise ApiError("Kategori seçin.")
    if kategori in ADMIN_CATEGORIES and user["rol"] != "yonetici":
        raise ApiError("Bu kategoriye yalnız Peyrev ekibi yazar.", 403, "YETKI_YOK")
    title = rules.text(data.get("baslik"), "Başlık", 5, 150)
    body = _text(data.get("metin"), "Metin", 1, 10000)
    flow = data.get("akis")
    stored = None
    if flow is not None:
        raw = json.dumps(flow, ensure_ascii=False)
        if len(raw.encode("utf-8")) > FLOW_LIMIT:
            raise ApiError("Akış dosyası en çok 1 MB olabilir.")
        stored = (raw, *flow_summary(flow))
    moment = core.clock.now()
    _limits(cur, user["id"], moment, topic=True)
    cur.execute("INSERT INTO topluluk_konulari (kategori, baslik, metin, yazar_id, olusturma, son_hareket) "
                "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id", (kategori, title, body, user["id"], moment, moment))
    topic_id = cur.fetchone()["id"]
    if stored:
        raw, name, count, summary = stored
        cur.execute("INSERT INTO topluluk_akislari (konu_id, ad, icerik, boyut, adim_sayisi, ozet) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (topic_id, name, raw, len(raw.encode("utf-8")), count, json.dumps(summary, ensure_ascii=False)))
    return topic_id


def _topic(cur: Cursor, topic_id: int, *, admin: bool = False, lock: bool = False) -> dict[str, Any]:
    cur.execute(f"SELECT {TOPIC_COLUMNS} FROM {TOPIC_FROM} WHERE k.id = %s" + (" FOR UPDATE OF k" if lock else ""),
                (topic_id,))
    row = cur.fetchone()
    if row is None or (row["gizli"] and not admin):
        raise ApiError("Konu bulunamadı.", 404, "BULUNAMADI")
    return row


def comments(cur: Cursor, topic_id: int, viewer: dict[str, Any] | None, *, admin: bool = False) -> list[dict[str, Any]]:
    cur.execute("SELECT y.*, u.ad_soyad AS yazar_ad_soyad, u.giris AS yazar_giris, u.rol AS yazar_rol "
                "FROM topluluk_yorumlari y JOIN kullanicilar u ON u.id = y.yazar_id WHERE y.konu_id = %s"
                + ("" if admin else " AND NOT y.gizli") + " ORDER BY y.olusturma, y.id", (topic_id,))
    result = []
    for row in cur.fetchall():
        item = {"id": row["id"], "metin": row["metin"], "yazar": _author(row), "olusturma": rules.stamp(row["olusturma"]),
                "benim": viewer is not None and viewer["id"] == row["yazar_id"]}
        if admin:
            item.update(gizli=row["gizli"], yazar_giris=row["yazar_giris"])
        result.append(item)
    return result


def read_topic(cur: Cursor, topic_id: int, viewer: dict[str, Any]) -> dict[str, Any]:
    row = _topic(cur, topic_id)
    cur.execute("UPDATE topluluk_konulari SET goruntulenme = goruntulenme + 1 WHERE id = %s", (topic_id,))
    row["goruntulenme"] += 1
    return {"konu": topic_view(row, viewer, full=True), "yorumlar": comments(cur, topic_id, viewer)}


def add_comment(core, cur: Cursor, user: dict[str, Any], topic_id: int, data: dict[str, Any]) -> int:
    row = _topic(cur, topic_id, lock=True)
    if row["kilitli"]:
        raise ApiError("Bu konu yorumlara kapalı.", 409, "KILITLI")
    text = _text(data.get("metin"), "Yorum", 1, 5000)
    moment = core.clock.now()
    _limits(cur, user["id"], moment, topic=False)
    cur.execute("INSERT INTO topluluk_yorumlari (konu_id, yazar_id, metin, olusturma) VALUES (%s, %s, %s, %s) RETURNING id",
                (topic_id, user["id"], text, moment))
    comment_id = cur.fetchone()["id"]
    cur.execute("UPDATE topluluk_konulari SET yanit_sayisi = yanit_sayisi + 1, son_hareket = %s WHERE id = %s",
                (moment, topic_id))
    return comment_id


def flow_file(cur: Cursor, topic_id: int) -> tuple[str, str]:
    _topic(cur, topic_id)
    cur.execute("UPDATE topluluk_akislari SET indirme = indirme + 1 WHERE konu_id = %s RETURNING ad, icerik", (topic_id,))
    row = cur.fetchone()
    if row is None:
        raise ApiError("Bu konuda akış yok.", 404, "BULUNAMADI")
    return row["ad"], row["icerik"]


def delete_own_topic(cur: Cursor, user: dict[str, Any], topic_id: int) -> None:
    row = _topic(cur, topic_id, lock=True)
    if row["yazar_id"] != user["id"]:
        raise ApiError("Yalnız kendi konunuzu silebilirsiniz.", 403, "YETKI_YOK")
    cur.execute("DELETE FROM topluluk_konulari WHERE id = %s", (topic_id,))


def delete_comment(cur: Cursor, comment_id: int, *, user: dict[str, Any] | None = None) -> int:
    """Yorumu siler (user verilirse yalnız kendi yorumu); konunun yanıt sayısı düşer. Konu kimliğini döndürür."""
    cur.execute("SELECT * FROM topluluk_yorumlari WHERE id = %s FOR UPDATE", (comment_id,))
    row = cur.fetchone()
    if row is None or (user is not None and row["gizli"]):
        raise ApiError("Yorum bulunamadı.", 404, "BULUNAMADI")
    if user is not None and row["yazar_id"] != user["id"]:
        raise ApiError("Yalnız kendi yorumunuzu silebilirsiniz.", 403, "YETKI_YOK")
    cur.execute("DELETE FROM topluluk_yorumlari WHERE id = %s", (comment_id,))
    cur.execute("UPDATE topluluk_konulari SET yanit_sayisi = greatest(yanit_sayisi - 1, 0) WHERE id = %s",
                (row["konu_id"],))
    return row["konu_id"]

