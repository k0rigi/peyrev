-- Hesaplar ve lisans (peyrev.accounts): bireysel başvurular, şifre kodları ve deneme izleri.
-- Studio oturumları çekirdekteki `oturumlar` tablosundadır (0001); onu bu modül yazar.
--
-- MySQL (orkestrai) → PostgreSQL eşlemesi; taşıma aracı bu bloğu kullanır (KAYNAK.Kolon → hedef.kolon).
-- Saat dilimsiz DATETIME değerleri Türkiye saatidir: `deger AT TIME ZONE 'Europe/Istanbul'` ile timestamptz olur.
--
-- SYS_RpaBasvurulari → basvurular (bütün satırlar taşınır)
--   BasvuruID → id                    aynen (32 küçük onaltılık); yönetim işlemleri bu kimliği kullanır
--   AdSoyad → ad_soyad · Eposta → eposta (harfler korunur) · Telefon → telefon
--   SifreHash → sifre_hash            Werkzeug özeti aynen; onaylanmış ve reddedilmiş başvuruda ''
--   Paket → paket · Durum → durum · IP → ip · AydinlatmaSurumu → aydinlatma_surumu
--   Olusturma → olusturma · KararZamani → karar_zamani
--   KararVerenID → karar_veren_id     eski yöneticinin KullaniciID'si (hesabı taşınmasa da kalır)
--   KullaniciID → kullanici_id        hesabı taşınmayan onaylı başvuruda NULL
--   YoneticiNotu → yonetici_notu
--   FirmaID → (düşer)
--   Aynı e-postayla birden çok 'bekliyor' satırı varsa yalnız en eskisi taşınır (basvurular_bekleyen_uq).
-- SYS_RpaSifreKodlari → sifre_kodlari (taşınmaz: kodlar 24 saat / 7 gün geçerlidir; geçişte yeni kod üretilir)
--   KodID → id (özetin tuzu, aynen) · KullaniciID → kullanici_id · KodOzeti → kod_ozeti
--   OlusturanID → olusturan_id (0 → NULL) · Olusturma → olusturma · Gecerlilik → gecerlilik · Kullanildi → kullanildi
-- SYS_RpaHesapIzleri → hesap_izleri (taşınmaz)
--   IzID → (yeni dizi) · Tur → tur · Anahtar → anahtar · Zaman → zaman
-- SYS_GirisDenemeleri → giris_denemeleri (taşınmaz; web girişiyle paylaşılan sayaç yalnız Studio girişini sayar)
--   ID → id · Eposta → giris (yazılan giriş adı) · IP → ip · Zaman → zaman
-- SYS_RpaOturumlari → oturumlar (0001; taşınmaz: eski oturumlar eski anahtarla imzalı belirteçlere bağlıdır)
--   OturumID → id · KullaniciID → kullanici_id · Cihaz → cihaz · Sira → sira · Ornek → ornek · Tekrar → tekrar
--   TekrarVar → tekrar_var · Surum → surum · IP → ip · Olusturma → olusturma · SonGorulme → son_gorulme
--   Durum ('Aktif' | 'Kapandi') → durum ('aktif' | 'kapandi') · KapanmaNedeni → kapanma_nedeni · FirmaID → (düşer)
-- SYS_Bildirimler (yeni başvuru zili) → (yok: yeni başvuru core.notifier ile bildirilir)

CREATE TABLE basvurular (
    id TEXT PRIMARY KEY CHECK (id ~ '^[0-9a-f]{32}$'),
    ad_soyad TEXT NOT NULL CHECK (length(ad_soyad) <= 120),
    eposta TEXT NOT NULL CHECK (length(eposta) <= 150),
    telefon TEXT CHECK (telefon IS NULL OR length(telefon) <= 32),
    sifre_hash TEXT NOT NULL DEFAULT '',
    paket TEXT NOT NULL CHECK (paket IN ('rpa', 'rpa_ajan')),
    durum TEXT NOT NULL DEFAULT 'bekliyor' CHECK (durum IN ('bekliyor', 'onaylandi', 'reddedildi')),
    ip TEXT NOT NULL DEFAULT '',
    aydinlatma_surumu TEXT NOT NULL,
    olusturma TIMESTAMPTZ NOT NULL,
    karar_zamani TIMESTAMPTZ,
    karar_veren_id BIGINT,
    -- Onaylı başvuru hesabın denetim izidir: bir hesabın tek onaylı başvurusu olur.
    kullanici_id BIGINT UNIQUE REFERENCES kullanicilar (id) ON DELETE SET NULL,
    yonetici_notu TEXT CHECK (yonetici_notu IS NULL OR length(yonetici_notu) <= 300)
);
-- Aynı e-postayla tek bekleyen başvuru: eş zamanlı ikinci başvuru satır açmaz, ilk başvurunun şifresi geçerli kalır.
CREATE UNIQUE INDEX basvurular_bekleyen_uq ON basvurular (lower(eposta)) WHERE durum = 'bekliyor';
CREATE INDEX basvurular_bekleyen_kisa_idx ON basvurular (lower(split_part(eposta, '@', 1))) WHERE durum = 'bekliyor';
CREATE INDEX basvurular_durum_idx ON basvurular (durum, olusturma);

-- Tek kullanımlık şifre kodları: düz kod saklanmaz, yalnız kaydın kimliğiyle tuzlanmış özeti.
CREATE TABLE sifre_kodlari (
    id TEXT PRIMARY KEY CHECK (id ~ '^[0-9a-f]{32}$'),
    kullanici_id BIGINT NOT NULL REFERENCES kullanicilar (id) ON DELETE CASCADE,
    kod_ozeti TEXT NOT NULL CHECK (kod_ozeti ~ '^[0-9a-f]{64}$'),
    olusturan_id BIGINT,                 -- kodu üreten yönetici
    olusturma TIMESTAMPTZ NOT NULL,
    gecerlilik TIMESTAMPTZ NOT NULL,     -- son geçerlilik anı (hariç)
    kullanildi TIMESTAMPTZ               -- kullanıldı ya da yeni kodla geçersizleşti
);
CREATE INDEX sifre_kodlari_acik_idx ON sifre_kodlari (kullanici_id, gecerlilik) WHERE kullanildi IS NULL;

-- Hesap uçlarının deneme izleri: tur 'sifre' (anahtar 'hesap:<giriş>' | 'adres:<ip>') ve 'basvuru' ('adres:<ip>').
CREATE TABLE hesap_izleri (
    id BIGSERIAL PRIMARY KEY,
    tur TEXT NOT NULL,
    anahtar TEXT NOT NULL CHECK (length(anahtar) <= 170),
    zaman TIMESTAMPTZ NOT NULL
);
CREATE INDEX hesap_izleri_anahtar_idx ON hesap_izleri (tur, anahtar, zaman);
CREATE INDEX hesap_izleri_zaman_idx ON hesap_izleri (zaman);

-- Studio lisans girişinin hatalı denemeleri (yazılan giriş adı ve adres başına sayılır; 1 gün saklanır).
CREATE TABLE giris_denemeleri (
    id BIGSERIAL PRIMARY KEY,
    giris TEXT NOT NULL CHECK (length(giris) <= 150),
    ip TEXT NOT NULL CHECK (length(ip) <= 64),
    zaman TIMESTAMPTZ NOT NULL
);
CREATE INDEX giris_denemeleri_giris_idx ON giris_denemeleri (lower(giris), zaman);
CREATE INDEX giris_denemeleri_ip_idx ON giris_denemeleri (ip, zaman);
CREATE INDEX giris_denemeleri_zaman_idx ON giris_denemeleri (zaman);
