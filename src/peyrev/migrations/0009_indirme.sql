-- Kişiye özel indirme bağlantıları (peyrev.com/api/indir/<anahtar>). Anahtarın yalnız SHA-256 özeti saklanır;
-- hesap başına bir bağlantı açık kalır (yenisi eskisini iptal eder), her indirme sayılır.

CREATE TABLE indirme_baglantilari (
    ozet TEXT PRIMARY KEY CHECK (ozet ~ '^[0-9a-f]{64}$'),
    kullanici_id BIGINT NOT NULL REFERENCES kullanicilar (id) ON DELETE CASCADE,
    olusturan_id BIGINT,
    olusturma TIMESTAMPTZ NOT NULL,
    gecerlilik TIMESTAMPTZ NOT NULL,
    iptal TIMESTAMPTZ,
    kullanim INTEGER NOT NULL DEFAULT 0,
    son_kullanim TIMESTAMPTZ
);
CREATE INDEX indirme_baglantilari_kullanici_idx ON indirme_baglantilari (kullanici_id, olusturma DESC);
