---
name: rpa-license-admin
description: Peyrev (MOD_RPA) lisansını bir hesaba vermek, süresini uzatmak veya kaldırmak, Ajanlar hakkını (MOD_AJAN) yönetmek, başvuruları onaylamak ya da "süreniz dolmuştur / lisans tanımlı değil / giriş yapılamıyor" sorunlarını incelemek için kullan. Peyrev yönetim paneli (peyrev.com/yonetim) üzerinden, kullanıcı bazlı hak kuralına göre çalışır; firma kavramı yoktur.
---

> Bu skill Studio deposundan (`/Users/k0rigi/Desktop/Web projeleri/peyrev-studio`) kopyalandı. İçindeki `docs/`, `credentials/`, `scripts/`, `src/peyrev_studio/` yolları o depoya göredir; sunucu yolları (`src/peyrev/…`) bu depoya.

# Peyrev lisans yönetimi

**Kural:** Her hesap bir kişidir; firma yoktur. Kullanıcı Studio'yu ancak hesabında `MOD_RPA` hakkı varsa ve hakkın bitiş tarihi bugünden (Türkiye saati) önce değilse açabilir; bitiş günü dahildir, bitiş boşsa hak süresizdir. `yonetici` rolündeki hesap Studio'yu hak satırı olmadan da açar. **Ajanlar** için hesapta hem `MOD_RPA` hem `MOD_AJAN` satırı gerekir (yöneticide de); ajan erişimi iki haktan erken bitenin son günü kapanır. Studio her açılışta ve 10 dakikada bir peyrev.com'dan onay alır; bir hesap aynı anda tek bilgisayarda çalışır. Ayrıntılar: `docs/lisans.md`.

## Önce sor

Hak vermek, uzatmak veya kaldırmak canlı hesapları değiştirir. Hesabı, hakkı ve bitiş tarihini kullanıcıdan açıkça teyit etmeden işlem yapma.

İşlemler Peyrev yönetim panelinden yapılır: veritabanına elle satır yazılmaz, SQL çalıştırılmaz, sunucuya SSH ile girilmez. Panel yalnız `yonetici` rolündeki etkin hesapla açılır; yöneticinin şifresini veya iki adımlı doğrulama kodunu sohbete yazdırma. Panel işlemini kullanıcı yapar; sen adımları ve sorunun nedenini söylersin.

## Yönetim paneli

Adres: `https://peyrev.com/yonetim/` (API'si `https://peyrev.com/api/yonetim/…`). Her değişiklik **Olaylar** bölümüne yazılır.

| Bölüm | Ne yapılır |
| --- | --- |
| **Özet** | Hesap, başvuru ve destek sayıları; 30 gün içinde bitecek haklar; ortak modelin durumu |
| **Kullanıcılar** | Hesap arama, **Yeni kullanıcı**. Hesabın ayrıntısı: rol, **Hesabı pasif yap / etkinleştir**, iki adımlı doğrulamayı sıfırlama, **Modül hakları**, **Şifre kodu**, **Studio oturumları**, **Ajan ofisleri** |
| **Başvurular** | Bireysel başvurular: **Ödeme alındı, hesabı aç** veya **Reddet** |
| **Destek** | Öneri ve destek talepleri (`docs/destek.md`) |
| **Olaylar** | Paneldeki her değişikliğin kaydı: kim, ne zaman, hangi hesapta ne yaptı |
| **Hesabım** | Yöneticinin kendi şifresi ve isteğe bağlı iki adımlı doğrulaması |

## Sık işler

- **Hak vermek veya uzatmak:** Kullanıcılar → hesap → **Modül hakları**: `MOD_RPA` (ve gerekirse `MOD_AJAN`) için başlangıç ve bitiş. Bitiş boş bırakılırsa süresizdir; geçmiş bir gün seçilemez, erişimi bugün bitirmek için bugünü seçin.
- **Kullanımı hemen kesmek:** `MOD_RPA` hakkını kaldırın (**Hakkı kaldır**) veya hesabı pasif yapın. Açık Studio en geç 10 dakika içinde kilitlenir ve çalışan akış bir sonraki adımda durur; pasif hesabın oturumu da geçersizleşir. Yalnız **Studio oturumları → Oturumu kapat** kişiyi giriş ekranına döndürür; hakkı duruyorsa yeniden girebilir.
- **Yeni hesap:** Kullanıcılar → **Yeni kullanıcı**: giriş adı (kullanıcı adı veya e-posta), ad soyad, isteğe bağlı telefon, rol, lisans dönemi (bugünü kapsamalı) ve isteğe bağlı Ajanlar. Aynı giriş adı başka hesapta varsa ya da kullanıcı adı başka bir hesabın e-postasının `@` öncesiyle aynıysa hesap açılmaz. Hesabın şifresi yoktur: 7 gün geçerli, tek kullanımlık **şifre kodu** bir kez gösterilir. Kodu kişiye güvenli bir kanaldan iletin; sohbette veya dosyada bırakmayın. Kişi Studio giriş ekranında **Şifre kodum var** ile kullanıcı adını, kodu ve kendi şifresini yazar. Süresiz hak, hesap açıldıktan sonra Modül hakları'ndan verilir.
- **Başvuru:** kişi `https://peyrev.com/#basvuru` formunu doldurur; başvuru lisans vermez. Ödeme görülünce Başvurular → **Ödeme alındı, hesabı aç** (bitiş, gerekirse başlangıç ve paket); kişi başvuruda belirlediği şifreyle girer. Bu e-postayla zaten hesap varsa onay yapılmaz. Onaylanmayan veya reddedilen başvurular 90 gün sonra silinir.
- **Unutulan şifre:** hesap → **Şifre kodu üret** (24 saat, 3 gün veya 7 gün geçerli); yeni kod öncekini geçersiz kılar. 15 dakikada hesap başına 10, adres başına 30 hatalı deneme kabul edilir.
- **Rol:** `yonetici` paneli kullanır ve Studio'yu `MOD_RPA` satırı olmadan açar; Ajanlar için onun da iki satırı olmalıdır. Yönetici kendi hesabını pasif yapamaz ve kendi yönetici rolünü kaldıramaz.
- **Ajan ofisleri:** kişi ofislerini Studio'da kendisi kurar (hesap başına en fazla 50); panelde işlem gerekmez. Hesap → **Ajan ofisleri → Ofisi pasif yap** ofisi kapatır (sahibi ve Studio erişemez, kayıtlar korunur, ofis sınırda sayılmaya devam eder); **Ofisi etkinleştir** geri açar. Ofislerin üyesi yoktur; riskli ajan işlerini hesabın sahibi onaylar.

Hak değişikliği açık uygulamaya en geç 10 dakika içinde (arka plan doğrulaması) ya da kişi **Lisansı şimdi doğrula** / **Yeniden kontrol et** dediğinde yansır. Kapalı uygulama bir sonraki açılışta onay alamaz ve açılmaz. İnterneti kesilmiş açık bir uygulama en fazla 60 dakika daha çalışır.

## Oturumlar (tek bilgisayar kuralı)

Her giriş sunucuda bir oturum açar ve hesabın başka bilgisayardaki oturumunu kapatır. Hesabın ayrıntısındaki **Studio oturumları** açık oturumları gösterir ve kapatır. Kapanma nedenleri:

| Neden | Anlamı |
| --- | --- |
| `BASKA_CIHAZ` | Hesap başka bir bilgisayarda açıldı |
| `YENI_GIRIS` | Aynı bilgisayarda yeniden giriş yapıldı |
| `CIKIS` | Kişi Studio'da **Oturumu kapat** dedi |
| `KOPYA` | Aynı oturum iki yerde birden kullanıldı |
| `YONETICI` | Oturumu yönetici kapattı |

Aynı hesapta sık `BASKA_CIHAZ` / `KOPYA` görülmesi hesabın paylaşıldığını gösterir; kullanıcıya her kişi için ayrı hesap gerektiğini bildir. Kapanmış oturumlar 90 gün sonra silinir.

Eş zamanlı bilgisayar sayısı, doğrulama aralığı, bağlantısız çalışma süresi ve lisans verilen en düşük uygulama sürümü `k0rigi/peyrev` deposundadır: `src/peyrev/accounts/sessions.py` (`CONCURRENT_SESSIONS = 1`), `src/peyrev/accounts/licensing.py` (`REFRESH_SECONDS = 600`, `TOLERANCE_SECONDS = 3600`, `MINIMUM_VERSION`, `PROTOCOL = 2`), hatalı deneme sınırları `src/peyrev/accounts/rules.py`. Değişiklik commit + push ile gider (**Dağıtım** iş akışı); değiştirmeden önce kullanıcıya sor.

## Sorun inceleme

| Studio'daki ileti | Olası neden | Ne yapılır |
| --- | --- | --- |
| Kullanıcı adı veya şifre hatalı. | Yanlış şifre, hesap yok ya da pasif. | Panelde hesabı ara; pasifse etkinleştir; şifre unutulduysa şifre kodu üret. |
| Çok fazla hatalı deneme. 15 dakika sonra tekrar deneyin. | 15 dakikada hesap başına 10 veya adres başına 30 hatalı deneme. | 15 dakika beklenir. |
| Bu kullanıcı adı birden fazla hesapla eşleşiyor. E-posta adresinizin tamamını yazın. | Kısa ad birden fazla hesabın `@` öncesiyle eşleşiyor. | Kişi e-postanın tamamını yazar. |
| Başvurunuz alındı; ödemeniz onaylandığında hesabınız açılır… | Bekleyen bir başvurunun e-posta ve şifresiyle giriş denendi. | Ödeme alındıysa Başvurular'dan hesabı açın. |
| Bu kullanıcı için Peyrev lisansı tanımlı değil. | Hesapta `MOD_RPA` hakkı yok. | Modül hakları'ndan `MOD_RPA` verin. |
| Peyrev kullanım süreniz … tarihinde dolmuştur. | `MOD_RPA` bitişi geçmiş. | Bitişi ileri alın veya boşaltın; kişi **Yeniden kontrol et** der. |
| Bu Peyrev sürümü artık desteklenmiyor… | Studio sürümü `MINIMUM_VERSION` değerinden eski. | Güncelleme arka planda iner, yeniden açılışta kurulur; olmazsa `https://peyrev.com/indir/`. |
| Peyrev'e geçildi. Hesabınızla bir kez yeniden giriş yapın… | 0.9.x'ten 1.0.0'a güncellenen kurulumun ilk açılışı; hata değildir. | Aynı kullanıcı adı ve şifreyle girilir; şifre hatırlanmıyorsa şifre kodu. |
| Lisans doğrulanamadı. Peyrev'i açmak için internet bağlantısı ve peyrev.com erişimi gerekir… / peyrev.com lisans sunucusuna ulaşılamadı… | Bilgisayar `https://peyrev.com` adresine (443) ulaşamıyor (internet, proxy, güvenlik duvarı) ya da sunucu yanıt vermiyor. | Aşağıdaki sağlık denetimi; kurum ağında peyrev.com'a izin. Açılışta onay şarttır; açık uygulama bağlantısız en fazla 60 dakika çalışır. |
| Lisans sunucusunun yanıtı doğrulanamadı. Uygulamayı güncelleyin. | Sunucunun lisans anahtarı Studio'ya gömülü anahtarla uyuşmuyor. | Aşağıdaki anahtar denetimi. |
| Bu hesap başka bir bilgisayarda açıldığı için bu bilgisayardaki oturum kapatıldı… | `BASKA_CIHAZ` | Hesap paylaşılıyorsa ayrı hesap gerekir. |
| Bu oturum aynı anda birden fazla yerde kullanıldığı için kapatıldı… | `KOPYA` | Yeniden giriş yeterlidir; sık tekrarlanıyorsa çalışma alanının başka bilgisayara kopyalanıp kopyalanmadığına bakın. |
| Oturumunuz yönetici tarafından kapatıldı… | `YONETICI` | Hakkı duruyorsa kişi yeniden girer. |
| Oturumun süresi doldu. Yeniden giriş yapın. | Şifre değişti, hesap pasifleşti ya da oturum doğrulanamadı. | Yeniden giriş. |
| Ajanlar menüsü görünmüyor | `MOD_AJAN` yok veya bitmiş ya da `MOD_RPA` satırı yok (yöneticide de gerekir). | Modül hakları'ndan ikisini verin; kişi **Lisansı şimdi doğrula** der. |
| Seçili ofise erişiminiz kalmadı… | Ofis pasif yapıldı veya silindi. | Panelden ofisi etkinleştirin ya da kişi başka ofis seçer. |

Canlı uçları dışarıdan denetlemek için (şifre gerektirmez):

```bash
curl -s https://peyrev.com/api/saglik                # {"success": true, "durum": "ok", "surum": ...}
curl -s https://peyrev.com/api/rpa/lisans/anahtar    # anahtar, protokol, asgari_surum
```

`anahtar` uygulamadaki `licensing.LICENSE_PUBLIC_KEY` ile aynı olmalıdır; `protokol` şu an `2`, `asgari_surum` `1.0.0`. Anahtar farklıysa sunucudaki `/etc/peyrev/lisans_ed25519` değişmiş demektir ve kurulu uygulamalar lisans doğrulayamaz. Anahtar dağıtımlarla değişmez ve gece yedeğindedir: önce yedekten geri yüklemeyi kullanıcıyla konuş; olmazsa `LICENSE_PUBLIC_KEY` güncellenip yeni sürüm yayımlanır (`rpa-release`). Güncelleme düzeneği lisanstan bağımsız çalıştığı için kullanıcılar yeni sürümü alabilir.

Uygulama tarafında çalışma alanındaki `data/license.json` yalnız kurulum kimliğini ve kurulumun giriş yaptığı sunucuyu (`"sunucu": "peyrev.com"`) içerir; kullanıcı adı, oturum belirteci, lisans ve şifre yoktur. Dosya uygulamayı açmaz: onay her açılışta sunucudan alınır ve yalnız bellekte tutulur. İşaret yoksa (1.0.0'dan önceki bir sürümden kalan çalışma alanı) giriş ekranı Peyrev'e geçildiğini yazar.

## Eski sürümler

0.9.53 ve önceki sürümler eski sunucuya bağlanır ve peyrev.com'dan lisans alamaz; hesaplar şifreleri ve haklarıyla peyrev.com'a taşındı (taşınan hakkın bitişi, eski firma ve hesap bitişlerinden erken olanıdır). Peyrev hesapları yalnız panelden değiştirilir. Eski sürüm kullanan biri sorun yaşıyorsa çözüm güncel sürümü [peyrev.com/indir](https://peyrev.com/indir/) adresinden kurmaktır; akışları ve ayarları yerinde kalır.

1.1.0 ve sonrası lisansta ürün olarak `peyrev` bekler (sunucu bu sürümlere `peyrev` verir). Cihaz kimliği de buna göre türediğinden 1.0.x'ten güncellenen bir kurulumun ilk girişi yeni bir bilgisayar oturumu sayılır; tek bilgisayar kuralı gereği önceki oturum kapanır, kullanıcı için ek bir adım yoktur.
