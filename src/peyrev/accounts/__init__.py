"""Hesap ve lisans modülü. Uçlar (yollar ve sözleşme orkestrai.net ile aynı; Studio yalnız hostu değiştirir):

  GET  /api/rpa/lisans/anahtar        açık anahtar, protokol, asgari sürüm                  → licensing
  POST /api/rpa/lisans/giris          kullanıcı adı + şifre → imzalı lisans ve oturum       → licensing
  POST /api/rpa/lisans/yenile         yenileme belirteci → taze lisans (sıra bir artar)     → licensing
  POST /api/rpa/lisans/cikis          bu bilgisayarın oturumunu kapatır                     → licensing
  POST /api/rpa/hesap/sifre           mevcut şifreyle yeni şifre                            → passwords
  POST /api/rpa/hesap/sifre-yenile    tek kullanımlık şifre koduyla yeni şifre              → passwords
  POST /api/rpa/basvuru               bireysel başvuru                                      → applications

Yönetim paneli uç değil, servis işlevleri kullanır: peyrev.accounts.admin.
"""

from flask import Blueprint

from . import applications, licensing, passwords


def create_blueprint(core) -> Blueprint:
    blueprint = Blueprint("accounts", __name__)
    for child in (licensing.create_blueprint(core), passwords.create_blueprint(core), applications.create_blueprint(core)):
        blueprint.register_blueprint(child)
    return blueprint
