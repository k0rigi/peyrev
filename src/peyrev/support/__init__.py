"""Studio öneri ve destek talepleri (/api/rpa/destek/*) ve yönetim panelinin destek işlevleri.

Uçların yolu, gövdesi, sınırları, hata kodları ve yanıt biçimi kurulu Studio sürümleriyle uyumlu kalır:
  POST /api/rpa/destek/talepler                    yeni talep → 201 {talep}
  GET  /api/rpa/destek/talepler                    kullanıcının talepleri → {talepler}
  GET  /api/rpa/destek/ozet                        okunmamış yanıt sayısı ve yayınlanan öneriler → {okunmamis, yayinlanan}
  GET  /api/rpa/destek/talepler/<no>               talep ayrıntısı (kullanıcı gördü) → {talep}
  POST /api/rpa/destek/talepler/<no>/mesaj         kullanıcı yanıtı {metin} → {talep}
  GET  /api/rpa/destek/talepler/<no>/ek/<32 hex>   ekin kendisi (her zaman indirme olarak)
Kimlik: lisans yanıtındaki destek belirteci (Authorization: Bearer); her istekte hesap, şifre izi, Studio oturumu ve
MOD_RPA hakkı veritabanından yeniden denetlenir. Yönetici uçları burada yoktur: panel service.py'deki işlevleri çağırır,
ekleri file_response ile verir.
"""

from __future__ import annotations

import re

from flask import Blueprint, Response, request

from ..common import INVALID_FIELDS, ApiError, json_body, ok, parse_json
from . import notify, service
from .service import answer_ticket, list_tickets, open_ticket, ticket_attachment, unread_count

__all__ = ["answer_ticket", "create_blueprint", "file_response", "list_tickets", "open_ticket", "ticket_attachment",
           "unread_count"]

CREATE_FIELDS = {"tur", "baslik", "metin", "adim", "surum", "platform", "ekler"}
CREATE_REQUIRED = {"tur", "baslik", "metin", "surum", "platform"}


def file_response(found: tuple[str, str, bytes], *, inline: bool = False) -> Response:
    """Ekin yanıtı: biçim kayıttaki gibi, ad güvenli karakterlere indirgenir, tarayıcı türü tahmin etmez.

    Studio ekleri her zaman indirme olarak alır; panel görselleri satır içi (inline=True) gösterebilir.
    """
    name, media, data = found
    response = Response(data, mimetype=media)
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", name)[:80] or "ek"
    response.headers["Content-Disposition"] = f'{"inline" if inline else "attachment"}; filename="{safe}"'
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Content-Security-Policy"] = "default-src 'none'; sandbox"
    return response


def _create_body() -> dict:
    """Yeni talebin gövdesi: json_body gibi sıkı, ama düzey ve değer sayısı sınırı yok. Akış yapısı eki iç içe
    adımlarla 20 düzeyi, büyük bir akışta 10.000 değeri geçebilir; ekin boyutu ayrıca 200 KB ile sınırlıdır."""
    if request.content_length is not None and request.content_length > service.CREATE_BODY_LIMIT:
        raise ApiError("İstek çok büyük.", 413, "GOVDE_SINIRI")
    raw = request.get_data(cache=False)
    if len(raw) > service.CREATE_BODY_LIMIT:
        raise ApiError("İstek çok büyük.", 413, "GOVDE_SINIRI")
    try:
        body = parse_json(raw)
    except (ValueError, UnicodeError, RecursionError):
        raise ApiError(INVALID_FIELDS) from None
    if not isinstance(body, dict) or set(body) - CREATE_FIELDS or CREATE_REQUIRED - set(body):
        raise ApiError(INVALID_FIELDS)
    return body


def create_blueprint(core) -> Blueprint:
    blueprint = Blueprint("support", __name__)

    def studio(work):
        """Kimlik ve iş tek işlemde; dönüş değeri çağırana ulaştığında işlem kaydedilmiştir."""
        with core.db.transaction() as cur:
            return work(cur, service.studio_user(core, cur, request.headers.get("Authorization")))

    # Bodies and identifiers are checked before the database is touched; only a new ticket's body
    # (up to 7 MB with images) is read after the caller has been identified, and the identity is checked again with
    # the write.
    @blueprint.post("/api/rpa/destek/talepler")
    def studio_create():
        studio(lambda cur, user: None)
        fields = service.new_ticket(_create_body())
        ticket, notice = studio(lambda cur, user: service.create(core, cur, user, fields))
        notify.deliver(core, *notice)
        return ok(201, talep=ticket)

    @blueprint.get("/api/rpa/destek/talepler")
    def studio_list():
        return ok(talepler=studio(lambda cur, user: service.mine(cur, user)))

    @blueprint.get("/api/rpa/destek/ozet")
    def studio_overview():
        return ok(**studio(lambda cur, user: service.overview(cur, user)))

    @blueprint.get("/api/rpa/destek/talepler/<int:number>")
    def studio_ticket(number: int):
        number = service.ticket_number(number)
        return ok(talep=studio(lambda cur, user: service.open_mine(core, cur, user, number)))

    @blueprint.post("/api/rpa/destek/talepler/<int:number>/mesaj")
    def studio_reply(number: int):
        number = service.ticket_number(number)
        text = service.reply_text(json_body({"metin"}, {"metin"}, limit=service.BODY_LIMIT).get("metin"))
        ticket, notice = studio(lambda cur, user: service.reply_mine(core, cur, user, number, text))
        notify.deliver(core, *notice)
        return ok(talep=ticket)

    @blueprint.get("/api/rpa/destek/talepler/<int:number>/ek/<identifier>")
    def studio_attachment(number: int, identifier: str):
        number, identifier = service.ticket_number(number), service.attachment_id(identifier)
        return file_response(studio(lambda cur, user: service.attachment(cur, number, identifier, user["id"])))

    return blueprint
