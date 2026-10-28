# Peyrev sunucusu — proje kuralları

Bu depo (`k0rigi/peyrev`, yerel kopya `~/Desktop/peyrev`) peyrev.com'un merkezi sunucusudur: lisans ve hesaplar, şifre
işlemleri, bireysel başvurular, destek talepleri, ajan ofisleri, ortak model erişimi, yönetim paneli (`/yonetim/`),
ana sayfa, eğitim sayfası, güncelleme dosyaları (`/indir/`) ve kişiye özel indirme bağlantıları. Python 3.12, Flask 3, Gunicorn, PostgreSQL 16. Masaüstü uygulaması
**Peyrev Studio** ayrı depodadır: `k0rigi/peyrev-studio`, yerel kopya `/Users/k0rigi/Desktop/Web projeleri/peyrev-studio`
(kendi CLAUDE.md'si geçerlidir). Arayüz, belgeler, commit mesajları ve kullanıcıya dönük tüm metinler Türkçedir.

## Kurallar

1. **Depo herkese açıktır.** Sır (şifre, anahtar, belirteç, sunucu adresi, kısa ömürlü indirme adresi) koda, belgeye,
   commit'e, Actions günlüğüne ve sohbete girmez. Sunucu sırları `/etc/peyrev/` altında `root:peyrev 0640` dosyalardadır
   ve sunucuda üretilir; GitHub Secrets'ta yalnız SSH girişi (`HOST`, `USERNAME`, `PASSWORD`) ve bildirim bilgileri
   (`DESTEK_*`) durur. Sunucu işlerinin çıktısı günlüğe değil şifreli yapıta yazılır (`.github/scripts/uzak.py`);
   günlükte yalnız `GENEL:` satırları görünür. GitHub belirteci komut satırından (argv) geçirilmez.
2. **Sunucu elle güncellenmez.** SSH/SFTP ile dosya yüklenmez, komut çalıştırılmaz. Kod değişikliği commit + push ile
   gider: main'e push'ta **Dağıtım** testleri çalıştırır, sürümü kurar, geçişleri uygular, sağlık denetimi yapar ve
   gerekirse önceki sürüme döner. Kurulum ve bakım işleri **Sunucu işleri** iş akışıyla yapılır.
   Bütün sunucu iş akışları aynı eşzamanlılık grubundadır (`peyrev-sunucu`): bir seferde tek iş başlatılır; ikinci iş
   bekleyen ilkinin yerine geçer. SSH bağlantısı kurulamadı hatası geçicidir: yalnız düşen iş yeniden çalıştırılır.
3. **Diğer siteler bozulmaz.** Sunucu paylaşımlıdır: peyrev.com'un yanında başka müşteri siteleri (hmtgross.com,
   mkticaret.com, altunhukuk.av.tr) ve e-postaları çalışır. Kurulum ve sunucu işleri sitelerin durum kodlarını önce ve
   sonra karşılaştırır. OpenLiteSpeed ayarı ve PostgreSQL dışındaki servisler kullanıcının onayı olmadan değiştirilmez.
   Eski sistem (orkestrai.net) 2026-10-26'da sunucudan tamamen kaldırıldı; Peyrev hiçbir şekilde ona bağlı değildir.
4. **Lisans sözleşmesi bozulmaz.** Firma yoktur, her hesap bir kişidir. Studio'yu açmak için hesapta `MOD_RPA` hakkı
   ve geçmemiş bitiş (gün dahil, Türkiye saati; boş = süresiz) gerekir; `yonetici` rolü satırsız açar; Ajanlar için
   ayrıca `MOD_AJAN`. Studio'nun kullandığı uçların yolu, gövdesi, hata kodları ve imzalı yanıt biçimi (protokol 2)
   kurulu sürümlerle uyumlu kalır; uyumu bozan değişiklik yeni Studio sürümü ve `MINIMUM_VERSION` planı ister.
   Lisans imza anahtarı (`/etc/peyrev/lisans_ed25519`) değişirse kurulu uygulamalar lisans doğrulayamaz.
5. **Veriyi panel değiştirir.** Hesap, hak, başvuru ve destek değişiklikleri yönetim panelinden yapılır ve
   `yonetim_olaylari`'na yazılır; veritabanına elle satır yazılmaz. Eski sistemden taşıma 2026-10-26'da bir kez yapıldı;
   taşınan hesapların kaynağı `tasima`'dır ve eski biçimdeki şifre özetleri doğrulanmaya devam eder.
6. **Her değişikliğin testi vardır.** Testler gerçek PostgreSQL ile çalışır (yerelde pgserver). Güvenlik ve lisans
   davranışı değişince testi, düzeltme geri alınırsa başarısız olacak biçimde yazılır. Kullanıcıya dönük değişiklikte
   `README.md` güncellenir.
7. **Sayfalar:** panel ve site telefon, tablet ve bilgisayarda düzgün görünür; sahte veri gösterilmez; satır içi betik
   ve stil yoktur (sıkı CSP); kullanıcı verisi DOM'a yalnız metin olarak yazılır.
8. **GitHub Actions maliyeti:** bu depo açıktır ve Actions'ı ücretsizdir. Studio deposu 2026-10-28'den beri gizlidir
   (kaynak kodu kopyalanmasın diye): onun dakikaları aylık 2.000 dakikalık kotadan düşer (macOS 10×, Windows 2×; harcama
   limiti 0 $), push'ta CI çalışmaz, derleme yaklaşık 150 kota dakikasıdır. Bu depo gizli yapılacaksa ya da ücret
   doğuracak bir adım çıkarsa önce kullanıcıya bildir.
9. **Kural önerileri:** tekrar edecek, faydalı bir kural fark edersen önce kullanıcıya sor; onaylarsa bu listeye kısa
   ve gerekçeli ekle.

## Komutlar

Proje iCloud ile eşitlenen Masaüstü'ndedir; sanal ortam iCloud dışındadır:

```bash
PY="$HOME/.venvs/peyrev/bin/python"
"$PY" -m pytest -p no:cacheprovider          # tüm testler (gömülü PostgreSQL)
"$PY" -m ruff check .
node --check src/peyrev/admin/static/panel.js && node --check web/assets/site.js
```

Ortam yoksa: `"/Users/k0rigi/Desktop/Web projeleri/peyrev-studio/.bootstrap/bin/uv" venv -p 3.12 ~/.venvs/peyrev`, sonra
`pyproject.toml`'daki bağımlılıklar ile `test` eki ve `ruff` kurulur. iCloud yavaşsa çalışma için
`/private/tmp` altında bir klon kullanılabilir (push oradan, sonra burada `git pull --ff-only`).

`gh` CLI kurulu değildir. GitHub API'si git kimlik bilgisiyle çağrılır; belirteç curl'e argv ile değil
`curl -K <(printf 'header = "Authorization: Bearer %s"\n' "$TOKEN")` ile verilir (ayrıntı: `rpa-release` skill'i).
Sunucu işi başlatmak: `POST /repos/k0rigi/peyrev/actions/workflows/sunucu.yml/dispatches` `{"ref":"main","inputs":{"islem":"…"}}`;
şifreli raporu açmak: `python tools/rapor-coz.py <run_id> --anahtar "<Studio deposu>/credentials/peyrev-rapor-x25519.pem"`.

## Yapı

| Yol | Görev |
| --- | --- |
| `src/peyrev/app.py`, `common.py`, `config.py`, `core.py`, `db.py` | Uygulama fabrikası, ortak hata biçimi ve güvenlik başlıkları, kanonik adres (www → https://peyrev.com), HSTS, ayarlar, veritabanı havuzu |
| `src/peyrev/accounts/` | Studio lisansı (`licensing.py`, protokol 2), oturumlar, şifre işlemleri, başvurular ve uzatma istekleri, deneme sınırları ve sahte özetle süre eşitleme (`rules.py`) |
| `src/peyrev/indirme.py`, `admin/indirme.py`, `eposta.py` | Kişiye özel indirme bağlantıları (`/api/indir/<anahtar>`), ödeme onayında müşteriye e-posta |
| `src/peyrev/topluluk/`, `admin/topluluk.py` | Üyelere özel topluluk: abonelik denetimi, konular, yorumlar, akış paylaşımı ve panelden moderasyon |
| `src/peyrev/uye.py` | Site hesabı oturumu (`peyrev_hesap`, `/api/uye/*`): Hesabım ve topluluk girişi, kendi indirme bağlantısı, uzatma isteği |
| `src/peyrev/iletisim.py`, `ziyaret.py`, `admin/iletisim.py` | İletişim formu, çerezsiz ziyaret sayımı ve paneldeki İletişim / Ziyaretler bölümleri |
| `src/peyrev/egitim.py`, `admin/egitim.py` | Ücretsiz eğitim videoları: herkese açık liste ve panelden yönetim (YouTube kimliği saklanır) |
| `src/peyrev/support/` | Destek talepleri ve bildirimler (Telegram, e-posta; `/etc/peyrev/destek.env`) |
| `src/peyrev/agents/`, `src/peyrev/llm/` | Ajan ofisleri, görevler, onaylar; ortak model kuyruğu (sunucuda kapalı: işlemcide AVX yok) |
| `src/peyrev/admin/` | Yönetim paneli: kimlik, iki adımlı doğrulama, CSRF, uçlar, sayfa (`static/panel.js`, `panel.css`) |
| `src/peyrev/migrations/` | Veritabanı geçişleri (`python -m peyrev.migrate`) |
| `web/` | Statik site (ortak üst menü, alt bilgi ve damgalar `tools/site.py`): ana sayfa, fiyatlar ve başvuru formu, `cozumler/`, `sablonlar/` (Studio'nun dışa aktarma biçiminde örnek akışlar), `yenilikler/`, `iletisim/`, `hesabim/`, `yasal/`, `egitim/` (videolar `/api/egitim`'den), `topluluk/` (`/api/topluluk/*`), `assets/ekran/` (Studio'dan alınmış ekran görüntüleri), `.htaccess` (www ve http yönlendirmesi) |
| `deploy/` | Sunucuda çalışan betikler (kurulum, dağıtım, destek ayarları, deneme hesabı), özetli bağımlılıklar ve `/indir/` bilgi sayfası (`deploy/indir/`) |
| `.github/workflows/` | Testler, Dağıtım, Sunucu işleri, Studio yayını, Destek bildirim ayarları |
| `tools/rapor-coz.py` | Şifreli sunucu raporlarını açar |

## Studio belgeleri

Masaüstü uygulamasının ayrıntılı belgeleri Studio deposundadır (`/Users/k0rigi/Desktop/Web projeleri/peyrev-studio/docs/`);
sunucuyu etkileyen bir iş yaparken ilgili olanı oradan oku, buraya kopyalama (iki kopya birbirinden ayrılır):

| Belge | Konu |
| --- | --- |
| `docs/lisans.md` | Studio'nun girişi, lisans uçları, cihaz ve oturum kuralları (bu depodaki `accounts/` ile birlikte) |
| `docs/guncelleme-mimarisi.md` | İmzalı güncelleme, iki bildirim (`guncelleme.manifest`, `stable.manifest`), Peyrev adına geçiş (1.1.0) |
| `docs/destek.md` | Öneri ve destek talepleri (bu depodaki `support/` ile birlikte) |
| `docs/ajanlar.md`, `docs/ofis-modeli.md`, `docs/kurumsal-entegrasyon.md` | Ajanlar, ofisler, ortak model ve kurumsal bağlantılar (bu depodaki `agents/`, `llm/` ile birlikte) |
| `docs/mimari.md`, `docs/masaustu-dagitim.md`, `docs/kurulum.md` | Uygulamanın yapısı, paketleme ve kurulum |
| `docs/adimlar.md`, `docs/akislar.md`, `docs/zamanlayici.md`, `docs/web-otomasyonu.md`, `docs/veritabani.md` | Kullanıcının akış adımları ve özellikleri |

## Skill'ler

- `rpa-license-admin` — panelde hak verme, uzatma, başvuru ve lisans sorunlarını inceleme.
- `rpa-release` — Studio'nun yeni sürümünü derleyip peyrev.com/indir'e yayınlama (Studio deposunda çalışır).

Studio'ya yeni adım eklemek gibi masaüstü uygulaması işleri Studio deposunda, onun `rpa-new-step` skill'iyle yapılır.
