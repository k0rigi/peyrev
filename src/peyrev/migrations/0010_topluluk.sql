-- Topluluk (peyrev.com/topluluk/): yalnız aboneliği devam eden hesaplara açık konular, yorumlar ve paylaşılan akışlar.
-- Üye oturumunun belirteci yalnız çerezde durur; burada SHA-256 özeti saklanır. Hesap silinince yazdıkları da gider.

CREATE TABLE uye_oturumlari (
    id TEXT PRIMARY KEY CHECK (id ~ '^[0-9a-f]{32}$'),
    kullanici_id BIGINT NOT NULL REFERENCES kullanicilar (id) ON DELETE CASCADE,
    belirtec_ozeti TEXT NOT NULL UNIQUE CHECK (belirtec_ozeti ~ '^[0-9a-f]{64}$'),
    sifre_izi TEXT NOT NULL,
    ip TEXT NOT NULL DEFAULT '',
    tarayici TEXT NOT NULL DEFAULT '',
    olusturma TIMESTAMPTZ NOT NULL,
    son_gorulme TIMESTAMPTZ NOT NULL,
    bitis TIMESTAMPTZ NOT NULL
);
CREATE INDEX uye_oturumlari_kullanici_idx ON uye_oturumlari (kullanici_id, son_gorulme DESC);

CREATE TABLE topluluk_konulari (
    id BIGSERIAL PRIMARY KEY,
    kategori TEXT NOT NULL CHECK (kategori IN ('duyuru', 'soru', 'akis', 'oneri')),
    baslik TEXT NOT NULL CHECK (char_length(baslik) BETWEEN 5 AND 150),
    metin TEXT NOT NULL CHECK (char_length(metin) BETWEEN 1 AND 10000),
    yazar_id BIGINT NOT NULL REFERENCES kullanicilar (id) ON DELETE CASCADE,
    sabit BOOLEAN NOT NULL DEFAULT FALSE,
    kilitli BOOLEAN NOT NULL DEFAULT FALSE,
    gizli BOOLEAN NOT NULL DEFAULT FALSE,
    yanit_sayisi INTEGER NOT NULL DEFAULT 0,
    goruntulenme INTEGER NOT NULL DEFAULT 0,
    olusturma TIMESTAMPTZ NOT NULL,
    son_hareket TIMESTAMPTZ NOT NULL
);
CREATE INDEX topluluk_konulari_liste_idx ON topluluk_konulari (gizli, sabit DESC, son_hareket DESC, id DESC);
CREATE INDEX topluluk_konulari_yazar_idx ON topluluk_konulari (yazar_id, olusturma DESC);

CREATE TABLE topluluk_yorumlari (
    id BIGSERIAL PRIMARY KEY,
    konu_id BIGINT NOT NULL REFERENCES topluluk_konulari (id) ON DELETE CASCADE,
    yazar_id BIGINT NOT NULL REFERENCES kullanicilar (id) ON DELETE CASCADE,
    metin TEXT NOT NULL CHECK (char_length(metin) BETWEEN 1 AND 5000),
    gizli BOOLEAN NOT NULL DEFAULT FALSE,
    olusturma TIMESTAMPTZ NOT NULL
);
CREATE INDEX topluluk_yorumlari_konu_idx ON topluluk_yorumlari (konu_id, olusturma, id);
CREATE INDEX topluluk_yorumlari_yazar_idx ON topluluk_yorumlari (yazar_id, olusturma DESC);

-- Konuya eklenen Studio akışı (dışa aktarılan JSON, olduğu gibi); adımlar listede özet olarak gösterilir.
CREATE TABLE topluluk_akislari (
    konu_id BIGINT PRIMARY KEY REFERENCES topluluk_konulari (id) ON DELETE CASCADE,
    ad TEXT NOT NULL CHECK (char_length(ad) BETWEEN 1 AND 200),
    icerik TEXT NOT NULL,
    boyut INTEGER NOT NULL,
    adim_sayisi INTEGER NOT NULL,
    ozet JSONB NOT NULL,
    indirme INTEGER NOT NULL DEFAULT 0
);
