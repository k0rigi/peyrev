"""Şifre işlemleri (oturumsuz, Studio'nun hesap servisi): /api/rpa/hesap/sifre ve /api/rpa/hesap/sifre-yenile.

Studio hesap adresini lisans adresinden türetir (…/rpa/lisans → …/rpa/hesap). Her etkin hesapta çalışır; lisans aranmaz.
Yeni şifre hesabın şifre izini değiştirir: Studio yenileme, ajan ve destek belirteçleri bir sonraki kullanımda geçersizdir
(oturum satırı kapatılmaz). Hatalı denemeler girişteki ad kuralıyla hesap ve adres başına sayılır
(rules.password_attempts_left); yanlış mevcut şifrede, ad bulunsun ya da bulunmasın, girişteki kadar özet denetlenir.
"""

from __future__ import annotations

import hmac
from typing import Any

from flask import Blueprint
from psycopg import Cursor
from werkzeug.security import generate_password_hash

from .. import users
from ..common import ApiError, client_ip, json_body, ok
from . import rules


def _change(core, cur: Cursor, login: str, current: str, new: Any, address: str) -> ApiError | None:
    moment = core.clock.now()
    candidates = users.find_by_login(cur, login)
    rules.password_attempts_left(cur, login, address, moment, candidates)
    matched = [user for user in candidates if rules.matches(user["sifre_hash"], current)]
    if len(matched) > 1:
        raise ApiError(rules.AMBIGUOUS, 409, "BELIRSIZ")
    if not matched:
        rules.equalize([user["sifre_hash"] for user in candidates], current)
        rules.password_failed(cur, rules.failed_name(login, candidates), address, moment)
        return ApiError("Kullanıcı adı veya mevcut şifre hatalı.", 401, "HATALI_GIRIS")
    user = matched[0]
    # Checked only now: a wrong current password with a weak new one answers 401, not 400.
    password = rules.password(new, user["giris"])
    if rules.matches(user["sifre_hash"], password):
        raise ApiError("Yeni şifre mevcut şifreyle aynı olamaz.")
    cur.execute("UPDATE kullanicilar SET sifre_hash = %s, guncelleme = %s WHERE id = %s",
                (generate_password_hash(password), moment, user["id"]))
    return None


def _reset(core, cur: Cursor, login: str, code: str, password: str, address: str) -> ApiError | None:
    moment = core.clock.now()
    found = users.find_by_login(cur, login)
    rules.password_attempts_left(cur, login, address, moment, found)
    # Only a single account can take a code: no account or several accounts count as a wrong code (401, not 409).
    # The code query runs either way (for no account when the name is not a single account's).
    cur.execute("SELECT id, kod_ozeti FROM sifre_kodlari WHERE kullanici_id = %s AND kullanildi IS NULL "
                "AND gecerlilik > %s FOR UPDATE", (found[0]["id"] if len(found) == 1 else 0, moment))
    matched = next((row for row in cur.fetchall()
                    if hmac.compare_digest(row["kod_ozeti"], rules.code_digest(code, row["id"]))), None)
    if matched is None:
        rules.password_failed(cur, rules.failed_name(login, found), address, moment)
        return ApiError("Kullanıcı adı veya şifre kodu hatalı ya da kodun süresi dolmuş.", 401, "KOD_HATALI")
    user_id = found[0]["id"]
    cur.execute("UPDATE kullanicilar SET sifre_hash = %s, guncelleme = %s WHERE id = %s",
                (generate_password_hash(password), moment, user_id))
    # Every open code of the account is spent, not only the one used.
    cur.execute("UPDATE sifre_kodlari SET kullanildi = %s WHERE kullanici_id = %s AND kullanildi IS NULL",
                (moment, user_id))
    return None


def create_blueprint(core) -> Blueprint:
    blueprint = Blueprint("hesap", __name__, url_prefix="/api/rpa/hesap")

    # A refusal that wrote failed-attempt traces is returned, not raised, so the transaction keeps them.
    @blueprint.post("/sifre")
    def change():
        data = json_body({"kullanici", "sifre", "yeni_sifre"}, {"kullanici", "sifre", "yeni_sifre"})
        login, current = data["kullanici"], data["sifre"]
        if (not isinstance(login, str) or not login.strip() or len(login) > 150
                or not isinstance(current, str) or not current or len(current) > 256):
            raise ApiError("Kullanıcı adı ve mevcut şifre gereklidir.")
        with core.db.transaction() as cur:
            refusal = _change(core, cur, login.strip(), current, data["yeni_sifre"], client_ip())
        if refusal is not None:
            raise refusal
        return ok(message="Şifreniz değiştirildi.")

    @blueprint.post("/sifre-yenile")
    def reset():
        # Studio 0.9.35 sent "eposta"; later versions send "kullanici" (a user name or an e-mail address).
        data = json_body({"eposta", "kullanici", "kod", "yeni_sifre"}, {"kod", "yeni_sifre"})
        login = data.get("kullanici", data.get("eposta"))
        if not isinstance(login, str) or not login.strip() or len(login) > 150:
            raise ApiError("Kullanıcı adınızı veya e-posta adresinizi yazın.")
        login = login.strip()
        password = rules.password(data["yeni_sifre"], login)
        code = data["kod"]
        if not isinstance(code, str) or not 8 <= len(code) <= 32:
            raise ApiError("Şifre kodunu yazın.")
        with core.db.transaction() as cur:
            refusal = _reset(core, cur, login, code, password, client_ip())
        if refusal is not None:
            raise refusal
        return ok(message="Şifreniz yenilendi. Yeni şifrenizle giriş yapabilirsiniz.")

    return blueprint
