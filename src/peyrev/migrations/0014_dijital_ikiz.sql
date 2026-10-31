-- Peyrev Dijital İkiz: üçüncü paket. Yeni modül hakkı MOD_IKIZ (peyrev.com/ikiz/'deki depo dijital ikizi) ve başvuru
-- paketi 'ikiz' (MOD_RPA + MOD_AJAN + MOD_IKIZ birlikte verilir). Var olan satırlar değişmez.
ALTER TABLE kullanici_modulleri DROP CONSTRAINT kullanici_modulleri_modul_check;
ALTER TABLE kullanici_modulleri ADD CONSTRAINT kullanici_modulleri_modul_check
    CHECK (modul IN ('MOD_RPA', 'MOD_AJAN', 'MOD_IKIZ'));
ALTER TABLE basvurular DROP CONSTRAINT basvurular_paket_check;
ALTER TABLE basvurular ADD CONSTRAINT basvurular_paket_check CHECK (paket IN ('rpa', 'rpa_ajan', 'ikiz'));
