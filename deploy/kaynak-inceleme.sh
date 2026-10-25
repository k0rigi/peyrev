#!/usr/bin/env bash
# deploy/kaynak-inceleme.sh — taşıma öncesi orkestrai veritabanının (MariaDB) kullanıcı, modül, RPA ve ajan tablolarını
# SALT OKUNUR inceler (Sunucu işleri → kaynak-inceleme): tablo yapıları ve toplu sayılar. Veri satırı, e-posta, şifre özeti
# veya başka kişisel bilgi yazdırılmaz. Bağlantı root'un /root/.my.cnf dosyasıyla kurulur (parola komut satırına girmez).

set -Eeuo pipefail
export LC_ALL=C.UTF-8 LANG=C.UTF-8
DB=orke_OrkestraDB
q() { mysql --defaults-file=/root/.my.cnf --default-character-set=utf8mb4 -N -B -r "$DB" -e "$1"; }
section() { printf '\n========== %s ==========\n' "$*"; }

section "Tablolar"
tables=$(q "SHOW TABLES" | grep -E '^SYS_(Kullanicilar|Firmalar|Moduller|FirmaModulleri|KullaniciModulleri|Rpa[A-Za-z]*|Ajan[A-Za-z]*|Bildirimler)$' || true)
for t in $tables; do
  printf '\n-- %s (%s satır)\n' "$t" "$(q "SELECT COUNT(*) FROM \`$t\`")"
  q "SHOW CREATE TABLE \`$t\`" | cut -f2-
done

section "Toplu sayılar (kişisel veri yok)"
echo "-- modül kodları"; q "SELECT ModulID, ModulKodu FROM SYS_Moduller WHERE ModulKodu IN ('MOD_RPA','MOD_AJAN')"
echo "-- kullanıcı modül satırları (modül, kullanıcı sayısı, bitişi boş olan)"
q "SELECT m.ModulKodu, COUNT(DISTINCT km.KullaniciID), SUM(km.BitisTarihi IS NULL) FROM SYS_KullaniciModulleri km JOIN SYS_Moduller m ON m.ModulID = km.ModulID WHERE m.ModulKodu IN ('MOD_RPA','MOD_AJAN') GROUP BY m.ModulKodu"
echo "-- firma modül satırları (modül, firma sayısı, bitişi boş olan)"
q "SELECT m.ModulKodu, COUNT(DISTINCT fm.FirmaID), SUM(fm.BitisTarihi IS NULL) FROM SYS_FirmaModulleri fm JOIN SYS_Moduller m ON m.ModulID = fm.ModulID WHERE m.ModulKodu IN ('MOD_RPA','MOD_AJAN') GROUP BY m.ModulKodu"
echo "-- RPA ya da AJAN satırı olan kullanıcılar: rol, durum, sayı"
q "SELECT k.Rol, k.Durum, COUNT(*) FROM SYS_Kullanicilar k WHERE EXISTS (SELECT 1 FROM SYS_KullaniciModulleri km JOIN SYS_Moduller m ON m.ModulID = km.ModulID WHERE km.KullaniciID = k.KullaniciID AND m.ModulKodu IN ('MOD_RPA','MOD_AJAN')) GROUP BY k.Rol, k.Durum"
echo "-- tüm kullanıcılar: rol, durum, sayı"; q "SELECT Rol, Durum, COUNT(*) FROM SYS_Kullanicilar GROUP BY Rol, Durum"
echo "-- yinelenen e-posta grupları (büyük/küçük harf duyarsız): tüm kullanıcılarda / RPA-AJAN kullanıcılarında"
q "SELECT COUNT(*) FROM (SELECT LOWER(Eposta) e FROM SYS_Kullanicilar GROUP BY LOWER(Eposta) HAVING COUNT(*) > 1) x"
q "SELECT COUNT(*) FROM (SELECT LOWER(k.Eposta) e FROM SYS_Kullanicilar k WHERE EXISTS (SELECT 1 FROM SYS_KullaniciModulleri km JOIN SYS_Moduller m ON m.ModulID = km.ModulID WHERE km.KullaniciID = k.KullaniciID AND m.ModulKodu IN ('MOD_RPA','MOD_AJAN')) GROUP BY LOWER(k.Eposta) HAVING COUNT(*) > 1) x"
echo "-- RPA-AJAN kullanıcılarında boş/NULL e-posta, 3 karakterden kısa veya 150'den uzun giriş adı"
q "SELECT SUM(k.Eposta IS NULL OR k.Eposta = ''), SUM(CHAR_LENGTH(k.Eposta) < 3), SUM(CHAR_LENGTH(k.Eposta) > 150) FROM SYS_Kullanicilar k WHERE EXISTS (SELECT 1 FROM SYS_KullaniciModulleri km JOIN SYS_Moduller m ON m.ModulID = km.ModulID WHERE km.KullaniciID = k.KullaniciID AND m.ModulKodu IN ('MOD_RPA','MOD_AJAN'))"
echo "-- şifre özeti biçimleri (yalnız önek ve sayı)"
q "SELECT SUBSTRING_INDEX(Sifre, '\$', 1), COUNT(*) FROM SYS_Kullanicilar GROUP BY SUBSTRING_INDEX(Sifre, '\$', 1)" 2>/dev/null || q "SHOW COLUMNS FROM SYS_Kullanicilar" | cut -f1,2
echo "-- en büyük kimlikler"; q "SELECT MAX(KullaniciID) FROM SYS_Kullanicilar"; q "SELECT MAX(FirmaID) FROM SYS_Firmalar"
printf 'GENEL: kaynak incelemesi tamamlandı (%s tablo)\n' "$(printf '%s\n' $tables | grep -c .)"
