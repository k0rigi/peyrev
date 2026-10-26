# Peyrev sunucusu

peyrev.com'un merkezi sunucusu. Peyrev Studio (masaüstü akış stüdyosu) şu işler için buraya bağlanır:

- lisans ve hesap işlemleri, şifre değiştirme ve şifre kodu,
- bireysel başvurular,
- destek talepleri ve bildirimleri,
- ajan ofisleri, görevler, onaylar ve ortak model erişimi,
- imzalı güncelleme dosyaları ve indirme sayfası (`/indir/`).

Yönetim paneli `/yonetim/` adresindedir (aşağıda).

- Python 3.12, Flask 3, Gunicorn, PostgreSQL 16 (psycopg 3).
- Firma kavramı yoktur: her hesap bir kullanıcıdır. Modül hakları (`MOD_RPA`, `MOD_AJAN`) kullanıcıya bitiş tarihiyle
  verilir; `bitis` boşsa süresizdir ve bitiş günü (Türkiye saati) dahildir.
- Sırlar (veritabanı şifresi, JWT ve lisans imza anahtarları, bildirim bilgileri) depoya girmez. Sunucuda
  `/etc/peyrev/` altında `root:peyrev 0640` dosyalarda durur ve GitHub Secrets ile yönetilir.

## Yönetim paneli

`https://peyrev.com/yonetim/`; uçları `/api/yonetim/*` (`src/peyrev/admin/`). Bölümler: Özet, Kullanıcılar (hesap açma,
modül hakları, şifre kodu, Studio oturumları, ajan ofisleri), Başvurular, Destek, Olaylar ve Hesabım. Sayfa da API'den,
satır içi betik ve stil olmadan, sıkı güvenlik başlıklarıyla gelir.

- **Giriş:** yalnız durumu `aktif`, rolü `yonetici` olan hesap girer. Giriş adı ve şifre Studio'yla aynıdır, hatalı
  deneme sınırı da ortaktır (15 dakikada hesap başına 10, adres başına 30). İlk yönetici taşımayla gelen orkestrai
  `Admin` hesabıdır (rolü `yonetici` olur, şifresi aynen taşınır); başka yönetici panelde rol verilerek açılır.
  Yönetici kendi hesabını pasif yapamaz, kendi yönetici rolünü kaldıramaz.
- **İki adımlı doğrulama:** isteğe bağlıdır ve Hesabım'dan açılır (TOTP; Google Authenticator, Microsoft Authenticator
  gibi uygulamalar). Açıkken girişte 6 haneli kod istenir ve her kod bir kez geçer. Kodunu kaybeden yöneticinin
  doğrulamasını başka bir yönetici, kullanıcı ayrıntısından sıfırlar.
- **Oturum:** girişten 12 saat sonra ya da 2 saat işlem yapılmazsa biter; yönetici başına en çok 10 oturum açık kalır.
  Belirteç yalnız `peyrev_yonetim` çerezindedir (HttpOnly, Secure, SameSite=Strict), sunucu özetini saklar. Şifre
  değişince, hesap pasifleşince ya da yönetici rolü kalkınca oturum kapanır.
- Durum değiştiren her istek `X-Peyrev-Yonetim: 1` başlığını ve `https://peyrev.com` kökenini taşır (CSRF). Her
  değişiklik `yonetim_olaylari`'na yazılır ve Olaylar bölümünde görünür; şifre, kod ve yanıt metni yazılmaz.

## Geliştirme

```bash
uv venv && uv pip install -e ".[test,mysql]" ruff
pytest                      # yerelde gömülü PostgreSQL (pgserver), CI'da PEYREV_TEST_DATABASE_URL
ruff check .
```

Veritabanı yapısı `src/peyrev/migrations/NNNN_ad.sql` geçişleriyle kurulur (`python -m peyrev.migrate`). Her geçiş bir kez,
kendi işleminde uygulanır; dosyaların başında orkestrai (MariaDB) tablolarından eşleme yazılıdır.

## Sunucu düzeni

orkestrai.net ile aynı AlmaLinux 8 sunucusunda, ondan bağımsız çalışır:

| Parça | Yer |
| --- | --- |
| API | `peyrev.service` (systemd, `peyrev` hesabı), Gunicorn `127.0.0.1:5101` |
| Kod | `/opt/peyrev/releases/<commit>` (her sürüm kendi sanal ortamıyla), etkin sürüm `/opt/peyrev/current` |
| Python | uv ile `/opt/peyrev/python` (CPython 3.12); sistem Python'una dokunulmaz |
| Veritabanı | PostgreSQL 16 (yalnız localhost): `peyrev` ve taşıma provası için `peyrev_prova` |
| Sırlar | `/etc/peyrev/` (`db.env`, `jwt_secret`, `lisans_ed25519`, isteğe bağlı `destek.env`, `llm.env`) |
| Yedek | Her gece 03:30, `pg_dump` ve sırlar, 14 gün: `/var/backups/peyrev` (`/etc/cron.d/peyrev-yedek`) |
| Site | CyberPanel sitesi `peyrev.com`; OpenLiteSpeed `/api/` ve `/yonetim/` isteklerini API'ye iletir, gerisi statiktir |
| SSL | Let's Encrypt, peyrev.com + www.peyrev.com (acme.sh kendiliğinden yeniler); www ve http, https://peyrev.com'a yönlenir |

## İş akışları

Depo herkese açık olduğundan Actions günlükleri de herkese açıktır. Sunucuya dokunan iş akışları
`.github/scripts/uzak.py` ile çalışır: betiğin bütün çıktısı sahibin açık anahtarıyla şifrelenir
(`.github/rapor-anahtari.pub`) ve yapıt olarak saklanır. Günlükte yalnız `GENEL:` satırları (durum kodları gibi) görünür.
Sunucu SSH anahtarı betiklerde sabittir.

| İş akışı | Ne yapar |
| --- | --- |
| Testler | Çekme isteklerinde ve main dışındaki dallarda testler (PostgreSQL 16) |
| Dağıtım | main'e her push'ta testler, ardından dağıtım (`deploy/dagitim.sh`): sürüm klasörü, özet denetimli bağımlılıklar, geçişler, yeniden başlatma, sağlık denetimi ve gerekirse önceki sürüme dönüş. `DAGITIM_ACIK=1` depo değişkeniyle açılır |
| Sunucu işleri | `inceleme` ve `kaynak-inceleme` (salt okunur), `kurulum-deneme` / `kurulum` (`deploy/kurulum.sh`), `bakim-ac` / `bakim-kapat` (orkestrai'de taşıma bakımı), `tasima-prova` / `tasima-gercek` |
| Studio yayını | Studio paketlerini `/indir/` altına doğrulayarak yayımlar (`inspect`, `publish`, `prune`) |
| Destek bildirim ayarları | Telegram ve e-posta bilgilerini Secrets'tan `/etc/peyrev/destek.env` dosyasına yazar ve deneme bildirimi gönderir |

Her sunucu işi önce ve sonra orkestrai.net'in açılış sayfasını ve `/api/rpa/lisans/anahtar` ucunu denetler. Kurulum, aynı
sunucudaki diğer sitelerin durum kodlarını da karşılaştırır ve fark görürse o adımı geri alır.

## Taşıma (orkestrai → Peyrev)

`tasima-prova` ve `tasima-gercek` (`deploy/tasima.sh`) şunu `peyrev` hesabıyla çalıştırır:

```bash
python -m peyrev.tasima --hedef {prova,gercek} --kaynak-ayar /home/orkestrai.net/public_html/api/db.py
```

- Kaynak bağlantısı `db.py`'deki `DB_CONFIG`'ten okunur; dosya çalıştırılmaz (yalnız sabitler ve
  `os.getenv("AD", "varsayılan")`). MariaDB tek tutarlı, salt okunur işlemde okunur (REPEATABLE READ,
  `START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY`).
- Hedef `/etc/peyrev/db.env`'dendir. `prova`: `PROVA_DATABASE_URL`, public şeması silinip geçişlerle baştan kurulur;
  doğrulama tutmasa da yazılanlar incelemek için kalır. Adı gerçek veritabanınınkiyle aynıysa prova çalışmaz.
  `gercek`: `DATABASE_URL`; yalnız `kaynak='orkestrai'` hesapları ve kayıtları eklenir ya da kaynakla eşitlenir
  (değeri değişmeyen satır yeniden yazılmaz, tekrar çalıştırmak bir şey değiştirmez). Peyrev'de doğan satırlara
  dokunulmaz. Okuma, yazma ve doğrulama tek işlemdir; bir sorun ya da uyuşmazlıkta hepsi geri alınır, kimlik dizileri
  de ilerletilmez.
- Çıkış: `0` bütün denetimler tuttu, `1` sorun, uyuşmazlık ya da hata, `2` komut satırı hatası.

| Hedef | Kaynak | Kural |
| --- | --- | --- |
| `kullanicilar` | `SYS_Kullanicilar` | MOD_RPA ya da MOD_AJAN satırı olan ya da rolü Admin olan hesaplar. Kimlik, giriş adı (Eposta) ve Werkzeug şifre özeti aynen; Admin → `yonetici`, diğerleri (firma yöneticisi Yonetici dahil) → `kullanici`; `alan_id` = FirmaID |
| `kullanici_modulleri` | `SYS_KullaniciModulleri` | Hesabın ve firmasının aynı modül satırı gerekir; bitiş ikisinin erkeni (boş: süresiz), başlangıç firmanınki. Pasif firmanın hiçbir hakkı, ajan kaydı kapalı firmanın MOD_AJAN'ı taşınmaz |
| `basvurular` | `SYS_RpaBasvurulari` | Hepsi; aynı e-postalı bekleyenlerden en eskisi. Karar verilmiş başvurunun şifre özeti boş yazılır |
| `destek_*` | `SYS_RpaTalep*` | Hesabı taşınanların talepleri numarasıyla; mesaj ve ekler (baytı baytına) talepleriyle |
| `ajan_ofisleri` | `SYS_AjanOfisleri` | Sahibi YetkiliID; sıra sahibin üyelik zamanından. Diğer üyelikler taşınmaz |
| `ajan_gorevleri`, `ajan_onaylari`, `ajan_olaylari` | `SYS_AjanEkip*` | Sahibi ofisin yetkilisi (ofis yoksa oluşturan / onaycı); onaylardan yalnız `rejected` ve `consumed`, olaylardan yalnız görev, onay ve ofis olayları |

Oturumlar, şifre kodları, hesap izleri, ajan organizasyonları ve bekleyen onaylar taşınmaz. DATETIME değerleri Türkiye
saati sayılır. Sahibi taşınmayan kayıt atlanır; atlanan her satırın nedeni raporda sayılır.

Rapor yalnız tablo adı, sayı ve kimlik numarası içerir (e-posta, ad, metin yoktur):

- her hedef tablo için `kaynak (uygun) / hedef / atlanan (neden: sayı; …)` satırı (hedef: uygun satırlardan hedefte
  birebir aynı olanlar) ve hedef tabloların toplam satırları,
- denetimler: şifre özetlerinin SHA-256 toplamı (kaynak ve hedef), ofis kimlikleri ve talep numaraları, Peyrev'de doğan
  satırların değişmediği, kimlik dizilerinin sonraki değerleri,
- her hesap için modül başına beklenen ve hedefteki durum ile bitiş günü,
- notlar (rol eşlemesi, pasif firmalar, taşınmayan tablolar, Werkzeug biçiminde olmayan özetler, açık şifre kodu olan
  hesaplar), sorunlar ve `SONUÇ:` satırı.

Operatör notları:

- Önce prova çalıştırılıp rapor okunur; gerçek taşıma orkestrai'de bakım açıkken yapılır (betik önce yedek alır).
- Gerçek taşımadan önce Peyrev'de hesap açılmamalıdır: yeni hesaplar 1'den numaralanır ve orkestrai kimlikleriyle
  çakışır. Çakışan hesap, giriş adı, talep numarası ya da ofis adı sorun olarak yazılır ve taşıma yapılmaz; Peyrev
  tarafı düzeltilip yeniden çalıştırılır.
- Gerçek taşımayı yeniden çalıştırmak taşınmış hesapları kaynakla eşitler: Peyrev'de bu hesaplarda yapılan
  değişiklikler (şifre, modül süresi, ofis adı, talep durumu) kaynağın değerine döner. Kaynakta silinen hesap pasif
  olur, kayıtları kalır.
- Taşınan herkes Studio'da yeniden giriş yapar; açık şifre kodu olan hesaplara (raporda kimlikleriyle) yönetim
  panelinden yeni kod üretilir.

Şifreli bir raporu açmak için (yalnız özel anahtarın sahibi):

```bash
python tools/rapor-coz.py <run_id> --anahtar "<Studio deposu>/credentials/peyrev-rapor-x25519.pem"
```
