"""Peyrev Dijital İkiz: peyrev.com/ikiz/'deki ayrı servise giriş belirteci (MOD_IKIZ hakkı).

Belirteç = base64url(yük) + "." + base64url(imza); ikisi de dolgusuz ("=" yok). Yük sıkı JSON'dur (ayraçlarda boşluk
yok, UTF-8, ASCII'ye kaçırılmaz): {"aud": "ikiz", "sub": <hesap kimliği>, "ad": <görünen ad>, "iat": <unix>,
"exp": iat + 720, "jti": <rastgele onaltılık>}. İmza, yükün baytları üzerinde Ed25519'dur ve lisans anahtarından ayrı
/etc/peyrev/ikiz_ed25519 ile atılır; ikiz servisi yalnız açık anahtarı (ikiz_ed25519.pub) bilir. Servis imzayı, aud'u
ve süreyi doğrular, jti'yi bir kez kabul eder.

Belirteç iki yoldan verilir: Studio'nun lisans yanıtı (`ikiz_erisim`) ve Hesabım'daki "Dijital İkiz'i aç" bağlantısı
(GET /api/uye/ikiz → /ikiz/giris?b=<belirteç>). Hak her seferinde veritabanından okunur (users.module_state): MOD_IKIZ
satırı ve bitiş günü; yönetici rolü satırsız da açar.
"""

from __future__ import annotations

import base64
import json
import secrets
from datetime import date
from typing import Any

from psycopg import Cursor

from .. import users

AUDIENCE = "ikiz"
LIFETIME_SECONDS = 720


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def name(user: dict[str, Any]) -> str:
    """Görünen ad: ad soyad; yoksa giriş adının @ öncesi."""
    return (user.get("ad_soyad") or "").strip() or str(user["giris"]).split("@", 1)[0]


def allowed(cur: Cursor, user: dict[str, Any], today: date) -> bool:
    return user.get("durum") == "aktif" and users.module_state(cur, user, users.IKIZ, today)[0] == users.ACTIVE


def issue(core, user: dict[str, Any]) -> str:
    """İmzalı, 720 saniye geçerli giriş belirteci (hakkı çağıran denetler)."""
    issued = int(core.clock.now().timestamp())
    payload = {"aud": AUDIENCE, "sub": int(user["id"]), "ad": name(user), "iat": issued,
               "exp": issued + LIFETIME_SECONDS, "jti": secrets.token_hex(16)}
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return _b64(body) + "." + _b64(core.keys.ikiz_sign(body))
