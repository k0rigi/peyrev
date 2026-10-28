# Peyrev sunucusu

[peyrev.com](https://peyrev.com)'un sunucu tarafı: tanıtım sitesi, hesap ve lisans hizmeti, destek ve yönetim araçları.
Peyrev masaüstü uygulaması ayrı bir depodadır.

- Python 3.12, Flask 3, PostgreSQL 16, Gunicorn
- Statik site `web/` klasöründedir; uygulama `src/peyrev/` altındadır.

## Geliştirme

```bash
python -m pip install -e ".[test]" ruff
python -m pytest            # testler gömülü bir PostgreSQL ile çalışır
python -m ruff check .
python tools/site.py        # sayfaların ortak parçalarını ve stil/betik damgalarını günceller
```

Sırlar (şifreler, anahtarlar, sunucu bilgileri) bu depoda bulunmaz ve bulunmamalıdır.

## Güvenlik

Bir güvenlik açığı bulduysanız lütfen herkese açık bir kayıt açmak yerine info@peyrev.com adresine yazın.

## Lisans

MIT, ayrıntı için `LICENSE`.
