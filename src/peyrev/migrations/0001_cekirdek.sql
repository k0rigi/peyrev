-- Çekirdek: kullanıcılar, modül hakları, Studio oturumları ve yönetim olayları. Firma kavramı yoktur.

CREATE SEQUENCE IF NOT EXISTS kullanici_id_seq START 1;
-- Studio yerel ajan verisini lisanstaki firma_id ile bölümler: taşınan kullanıcıda eski FirmaID, yenide bu diziden.
CREATE SEQUENCE IF NOT EXISTS alan_id_seq START 1000000;

CREATE TABLE kullanicilar (
    id BIGINT PRIMARY KEY DEFAULT nextval('kullanici_id_seq'),
    giris TEXT NOT NULL CHECK (length(giris) BETWEEN 3 AND 150),
    ad_soyad TEXT NOT NULL DEFAULT '' CHECK (length(ad_soyad) <= 120),
    telefon TEXT CHECK (telefon IS NULL OR length(telefon) <= 32),
    sifre_hash TEXT NOT NULL DEFAULT '',
    rol TEXT NOT NULL DEFAULT 'kullanici' CHECK (rol IN ('yonetici', 'kullanici')),
    durum TEXT NOT NULL DEFAULT 'aktif' CHECK (durum IN ('aktif', 'pasif')),
    alan_id INTEGER NOT NULL DEFAULT nextval('alan_id_seq') CHECK (alan_id >= 1),
    kaynak TEXT NOT NULL DEFAULT 'yonetim' CHECK (kaynak IN ('orkestrai', 'basvuru', 'yonetim')),
    totp_gizli TEXT,
    olusturma TIMESTAMPTZ NOT NULL DEFAULT now(),
    guncelleme TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER SEQUENCE kullanici_id_seq OWNED BY kullanicilar.id;
CREATE UNIQUE INDEX kullanicilar_giris_uq ON kullanicilar (lower(giris));
CREATE INDEX kullanicilar_kisa_idx ON kullanicilar (lower(split_part(giris, '@', 1)));

CREATE TABLE kullanici_modulleri (
    kullanici_id BIGINT NOT NULL REFERENCES kullanicilar (id) ON DELETE CASCADE,
    modul TEXT NOT NULL CHECK (modul IN ('MOD_RPA', 'MOD_AJAN')),
    baslangic DATE,
    bitis DATE,                          -- NULL: süresiz; bitiş günü dahildir (Europe/Istanbul)
    guncelleme TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (kullanici_id, modul)
);

-- Studio oturumları: hesap başına aynı anda tek bilgisayar.
CREATE TABLE oturumlar (
    id TEXT PRIMARY KEY CHECK (id ~ '^[0-9a-f]{32}$'),
    kullanici_id BIGINT NOT NULL REFERENCES kullanicilar (id) ON DELETE CASCADE,
    cihaz TEXT NOT NULL,
    sira INTEGER NOT NULL DEFAULT 0,
    ornek TEXT,
    tekrar INTEGER NOT NULL DEFAULT 0,
    tekrar_var BOOLEAN NOT NULL DEFAULT false,
    surum TEXT NOT NULL DEFAULT '',
    ip TEXT NOT NULL DEFAULT '',
    olusturma TIMESTAMPTZ NOT NULL DEFAULT now(),
    son_gorulme TIMESTAMPTZ NOT NULL DEFAULT now(),
    durum TEXT NOT NULL DEFAULT 'aktif' CHECK (durum IN ('aktif', 'kapandi')),
    kapanma_nedeni TEXT
);
CREATE INDEX oturumlar_kullanici_idx ON oturumlar (kullanici_id, durum);
CREATE INDEX oturumlar_kapali_idx ON oturumlar (durum, son_gorulme);

-- Yönetim paneli ve taşıma işlemlerinin denetim kaydı (kim, ne zaman, neyi değiştirdi; sır içermez).
CREATE TABLE yonetim_olaylari (
    id BIGSERIAL PRIMARY KEY,
    yonetici_id BIGINT,
    tur TEXT NOT NULL,
    hedef TEXT,
    ayrinti JSONB NOT NULL DEFAULT '{}'::jsonb,
    zaman TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX yonetim_olaylari_zaman_idx ON yonetim_olaylari (zaman DESC);
