"""Ajan merkezi (/api/studio/ekip): hesabın ofisleri, kendine verdiği insan görevleri ve tek kullanımlık onaylar.

- entitlement: lisans yanıtındaki `ajan` uzantısı ve ajan uçlarının kimlik doğrulaması (lisans ve ortak model modülleri
  de kullanır).
- records: ofis, görev ve onay kuralları; her değişiklik hesap satırı kilidiyle sıraya girer.
- routes: Studio uçları. admin: yönetim panelinin çağırdığı ofis işlemleri. metrics: insan görevi performansı.
"""

from .routes import create_blueprint

__all__ = ["create_blueprint"]
