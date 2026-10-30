"""Yönetim paneli: https://peyrev.com/yonetim/ sayfası ve /api/yonetim/* uçları (yalnız platform yöneticisi).

Sayfa (page.py): GET /yonetim/ tek sayfa; /yonetim/panel.css ve /yonetim/panel.js. Bölümler Özet, Kullanıcılar,
Başvurular, Destek (#destek, talep #destek-<no>), Robotlar, Olaylar ve Hesabım'dır. Giriş ekranındaki "Şifre kodum var" ve
Hesabım'daki şifre değiştirme, Studio'yla ortak /api/rpa/hesap/* uçlarını (peyrev.accounts) kullanır.

Kimlik (auth.py): POST /api/yonetim/giris yönetici oturumu açar; `peyrev_yonetim` çerezi (HttpOnly, Secure,
SameSite=Strict, Path=/) ve yonetim_oturumlari'nda belirtecin SHA-256 özeti; 12 saat mutlak, 2 saat boşta süresi.
Durum değiştiren her istek `X-Peyrev-Yonetim: 1` başlığı ve public_base kökünden Origin (yoksa Referer) ister.
İsteğe bağlı iki adımlı doğrulama (TOTP, RFC 6238).

Uçlar (routes.py, hepsi /api/yonetim altında; giriş dışındakiler oturum ister):
  POST /giris {giris, sifre, kod?}                 → {yonetici, oturum} ve çerez
  POST /cikis {}                                   oturumu kapatır, çerezi siler
  GET  /ben                                        → {yonetici, oturum, oturumlar}
  POST /oturumlar/digerlerini-kapat {}             → {kapatilan}
  POST /iki-adim/baslat {sifre}                    → {gizli, baglanti, gecerlilik} (yalnız bu yanıtta)
  POST /iki-adim/onayla {kod}                      → {iki_adim, kapatilan}
  POST /iki-adim/kapat {sifre, kod}                → {iki_adim}
  GET  /ozet                                       → sayılar ve 30 gün içinde bitecek haklar
  GET  /ortak-model · POST /ortak-model/kuyrugu-bosalt {}
  GET  /kullanicilar?ara=&durum=&sayfa=            → {kullanicilar, toplam, sayfa, sayfa_boyu}
  POST /kullanicilar {giris, ad_soyad, telefon?, rol?, baslangic, bitis, ajan?}  → 201 {kullanici, kod, uyari?}
  GET  /kullanicilar/<id>                          → {kullanici, oturumlar, ofisler, destek, basvuru}
  POST /kullanicilar/<id>/bilgiler {ad_soyad, telefon?} · /rol {rol} · /durum {durum}
  POST /kullanicilar/<id>/moduller/<MOD_RPA|MOD_AJAN> {baslangic?, bitis?}   (bitiş boş = süresiz)
  POST /kullanicilar/<id>/moduller/<modul>/kaldir {} · /sifre-kodu {saat?} · /oturumlari-kapat {oturum_id?}
  POST /kullanicilar/<id>/iki-adim-sifirla {}
  POST /ofisler/<ofis_id>/durum {durum}
  GET  /basvurular?durum= · POST /basvurular/<id>/onayla {baslangic?, bitis, paket?, not?} · /reddet {not?}
  GET  /destek?durum=&tur= · GET /destek/<no> · POST /destek/<no>/yanit {metin?, durum?, yayin_surumu?}
  GET  /destek/<no>/ek/<ek_id>                     (görseller satır içi, diğerleri indirme)
  GET  /robotlar                                   → {robotlar, kategoriler} (Robot Paneli)
  POST /robotlar/<id>/komut {tur, hedef}           → {robot}; tur durdur | yeniden_calistir
  GET  /olaylar?sayfa=&tur=&hedef=
"""

from flask import Blueprint

from .page import page_blueprint
from .routes import api_blueprint, sign_in_blueprint


def create_blueprint(core) -> Blueprint:
    blueprint = Blueprint("admin", __name__)
    for child in (sign_in_blueprint(core), api_blueprint(core), page_blueprint()):
        blueprint.register_blueprint(child)
    return blueprint
