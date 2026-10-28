"""Panelden kişiye özel indirme bağlantısı: oluşturur ve isterse hesabın e-posta adresine gönderir.

Ödeme onayında (başvuru onaylanınca) kendiliğinden, hesap sayfasında elle çağrılır. Bağlantı yalnız bu dönüşte görünür
(saklanan özettir); olay kaydına bağlantı değil, geçerlilik ve e-posta sonucu yazılır.
"""

from __future__ import annotations

from typing import Any

from .. import eposta, indirme, users
from ..accounts import rules
from ..common import ApiError
from . import events
from .service import require_admin

SUBJECT = "Peyrev indirme bağlantınız"


def _letter(user: dict[str, Any], link: str, expires: str, ends_on: str | None, welcome: bool) -> str:
    password = ("başvuruda belirlediğiniz şifre" if user["kaynak"] == "basvuru"
                else "size iletilen şifre (şifre kodu verildiyse giriş ekranındaki \"Şifre kodum var\" ile kendiniz "
                     "belirlersiniz)")
    lines = [f"Merhaba {user['ad_soyad'] or user['giris']},", ""]
    lines.append("Ödemeniz onaylandı ve Peyrev hesabınız açıldı." if welcome
                 else "Peyrev'i indirmeniz için kişisel bağlantınız aşağıdadır.")
    if ends_on:
        lines.append(f"Lisansınız {ends_on} tarihine kadar geçerlidir.")
    lines += ["", f"İndirme bağlantınız ({expires} tarihine kadar geçerli):", link, "",
              "Bağlantıdan Windows ya da macOS kurulumunu seçin. Kurduktan sonra Peyrev'e şu bilgilerle girersiniz:",
              f"Kullanıcı adı: {user['giris']}", f"Şifre: {password}", "",
              "Kurulu Peyrev yeni sürümleri kendiliğinden indirir. Bağlantı size özeldir; lütfen paylaşmayın.", "",
              ("Sorularınız için bu e-postayı yanıtlayabilir ya da Peyrev'deki \"Öneri ve destek\" düğmesini "
               "kullanabilirsiniz."), "", "Peyrev", "https://peyrev.com"]
    return "\n".join(lines)


def send(core, admin_id: int, user_id: int, *, mail: Any, welcome: bool = False) -> dict[str, Any]:
    """{"baglanti", "gecerlilik", "eposta": gonderildi | gonderilemedi | ayar_yok | adres_yok | istenmedi, "alici"}."""
    if type(mail) is not bool:
        raise ApiError("E-posta seçimi geçersiz.")
    with core.db.transaction() as cur:
        require_admin(cur, admin_id)
        user = users.get(cur, user_id)
        if user is None:
            raise ApiError("Hesap bulunamadı.", 404, "BULUNAMADI")
        if user["durum"] != "aktif":
            raise ApiError("Pasif hesaba indirme bağlantısı gönderilmez; önce hesabı etkinleştirin.", 409, "HESAP_PASIF")
        token, expires = indirme.issue(core, cur, user_id, admin_id)
        today = core.clock.today_tr()
        rpa = users.modules(cur, user_id).get(users.RPA)
    link = indirme.address(core, token)
    expiry = rules.stamp(expires)
    recipient = user["giris"] if rules.EMAIL.fullmatch(user["giris"] or "") else None
    if not mail:
        result = "istenmedi"
    elif recipient is None:
        result = "adres_yok"
    else:
        ends_on = rpa["bitis"].strftime("%d.%m.%Y") if rpa and rpa["bitis"] and rpa["bitis"] >= today else None
        result = eposta.send(core, recipient, SUBJECT, _letter(user, link, indirme.tr_day(expires), ends_on, welcome))
    with core.db.transaction() as cur:
        events.record(core, cur, admin_id, "indirme_baglantisi", f"kullanici:{user_id}", gecerlilik=expires.isoformat(),
                      eposta=result)
    return {"kullanici_id": user_id, "baglanti": link, "gecerlilik": expiry, "eposta": result, "alici": recipient}
