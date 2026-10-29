-- Hesap başına ofis sınırı: her ofis bağımsız bir müşteridir. Hesap Studio'da en çok bu kadar ofis kurar (pasifler de
-- sayılır); daha fazlasını yönetim paneli verir ya da panelden müşteri ofisi açılır. Varsayılan 1; mevcut hesaplar
-- bugünkü ofislerini korur (sınır, sahip oldukları ofis sayısından az olmaz).
ALTER TABLE kullanicilar ADD COLUMN ofis_siniri INTEGER NOT NULL DEFAULT 1 CHECK (ofis_siniri BETWEEN 1 AND 50);
UPDATE kullanicilar k SET ofis_siniri = LEAST(50, GREATEST(1, (SELECT count(*) FROM ajan_ofisleri o WHERE o.sahip_id = k.id)));
