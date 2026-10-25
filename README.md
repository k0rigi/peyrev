# Peyrev sunucusu

peyrev.com'un merkezi sunucusu: Peyrev Studio'nun lisans ve hesap işlemleri, bireysel başvurular, destek talepleri,
ajan ofisleri ve onayları, ortak model erişimi, indirme sayfası ve yönetim paneli.

- Python 3.12, Flask 3, Gunicorn, PostgreSQL 16 (psycopg 3).
- Firma kavramı yoktur: her hesap bir kullanıcıdır; modül hakları (MOD_RPA, MOD_AJAN) kullanıcıya bitiş tarihiyle verilir.
- Sırlar (veritabanı şifresi, JWT ve lisans imza anahtarları, bildirim bilgileri) depoya girmez; sunucuda `/etc/peyrev/`
  altındadır ve GitHub Secrets ile yönetilir.

## Geliştirme

```bash
uv venv && uv pip install -e ".[test]"
pytest                      # gömülü PostgreSQL (pgserver) ile; CI'da PEYREV_TEST_DATABASE_URL
```
