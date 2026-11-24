#!/usr/bin/env bash
# deploy/ikiz-okuma.sh — Dijital İkiz veritabanları için SALT OKUNUR kullanıcı (Sunucu işleri → ikiz-okuma): sahibin
# pgAdmin gibi bir araçla ikiz verisine bakabilmesi için. `ikiz_okuma` rolü açılır (ya da şifresi yenilenir); yalnız
# ikizlerin veritabanlarına (ikiz ve ikiz_<firma>; sahibi aynı adlı rol) bağlanır ve yalnız okur: SELECT dışında hiçbir
# yetkisi yoktur, sonradan geçişlerle gelen tablolar da okunur (varsayılan yetki). peyrev ve sunucudaki diğer
# veritabanlarına bağlanamaz. Her çalıştırmada yeni şifre sunucuda üretilir ve yalnız şifreli rapora yazılır; sunucuda
# saklanmaz. Uygulamalara ve servislere dokunulmaz.

set -Eeuo pipefail
export LC_ALL=C.UTF-8 LANG=C.UTF-8
ROL=ikiz_okuma
genel() { printf 'GENEL: %s\n' "$*"; }
psql_su() { runuser -u postgres -- psql -X -q -v ON_ERROR_STOP=1 "$@"; }

SIFRE=$(openssl rand -base64 64 | tr -dc 'A-Za-z0-9' | head -c 32)
[ "${#SIFRE}" -eq 32 ] || { echo "Şifre üretilemedi"; exit 1; }

# Şifre komut satırına (ps) düşmez: SQL standart girdiden gider. Şifre yalnız harf ve rakamdır (tırnak sorunu yok).
psql_su -d postgres <<SQL
SELECT 'CREATE ROLE $ROL LOGIN' WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '$ROL') \gexec
ALTER ROLE $ROL WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 5
  PASSWORD '$SIFRE';
ALTER ROLE $ROL SET default_transaction_read_only = on;
ALTER ROLE $ROL SET statement_timeout = '120s';
SQL

mapfile -t VTLER < <(psql_su -d postgres -At -c "SELECT datname FROM pg_database WHERE (datname = 'ikiz'
  OR datname LIKE 'ikiz\_%') AND pg_get_userbyid(datdba) = datname AND datallowconn ORDER BY 1")
[ "${#VTLER[@]}" -gt 0 ] || { echo "İkiz veritabanı bulunamadı"; exit 1; }

for vt in "${VTLER[@]}"; do
  echo "== $vt"
  psql_su -d "$vt" -v vt="$vt" -v rol="$ROL" <<'SQL'
SELECT format('GRANT CONNECT ON DATABASE %I TO %I', :'vt', :'rol') \gexec
-- Sahibin şemaları ve public: kullanım, bugünkü tablolar ve sonradan açılacak tablolar için yalnız SELECT.
SELECT format('GRANT USAGE ON SCHEMA %I TO %I', nspname, :'rol')
  FROM pg_namespace WHERE nspname = 'public' OR pg_get_userbyid(nspowner) = :'vt' \gexec
SELECT format('GRANT SELECT ON ALL TABLES IN SCHEMA %I TO %I', nspname, :'rol')
  FROM pg_namespace WHERE nspname = 'public' OR pg_get_userbyid(nspowner) = :'vt' \gexec
SELECT format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA %I GRANT SELECT ON TABLES TO %I', :'vt', nspname, :'rol')
  FROM pg_namespace WHERE nspname = 'public' OR pg_get_userbyid(nspowner) = :'vt' \gexec
SELECT count(*) AS okunan_tablo FROM information_schema.role_table_grants
 WHERE grantee = :'rol' AND privilege_type = 'SELECT';
SELECT count(*) AS secim_disi_yetki FROM information_schema.role_table_grants
 WHERE grantee = :'rol' AND privilege_type <> 'SELECT';
SQL
done

# Denetim: rol yalnız ikizlere bağlanabilir, hiçbir şeyin sahibi değildir, süper kullanıcı değildir.
echo "== bağlanabildiği veritabanları"
psql_su -d postgres -At -c "SELECT datname FROM pg_database WHERE has_database_privilege('$ROL', datname, 'CONNECT')
  ORDER BY 1"
# postgres, template0 ve template1 PostgreSQL'in veri taşımayan sistem veritabanlarıdır; her rol varsayılan olarak bağlanır.
DIGER=$(psql_su -d postgres -At -c "SELECT count(*) FROM pg_database WHERE has_database_privilege('$ROL', datname,
  'CONNECT') AND datname NOT IN ('postgres', 'template0', 'template1')
  AND NOT (datname = 'ikiz' OR (datname LIKE 'ikiz\_%' AND pg_get_userbyid(datdba) = datname))")
echo "== pg_hba (yerel TCP)"
psql_su -d postgres -At -c "SELECT line_number, type, database, user_name, address, auth_method FROM pg_hba_file_rules
  ORDER BY line_number" || true
PORT=$(psql_su -d postgres -At -c "SHOW port")

# Gerçek giriş denemesi (TCP, şifreyle): okur, yazamaz.
ILK=${VTLER[-1]}
GIRIS=basarisiz
YAZMA=reddedildi
if PGPASSWORD=$SIFRE psql -X -h 127.0.0.1 -p "$PORT" -U "$ROL" -d "$ILK" -At \
    -c "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'" </dev/null; then
  GIRIS=tamam
  if PGPASSWORD=$SIFRE psql -X -h 127.0.0.1 -p "$PORT" -U "$ROL" -d "$ILK" -At -v ON_ERROR_STOP=1 \
      -c "BEGIN; SET TRANSACTION READ WRITE; CREATE TABLE okuma_denemesi (x int); ROLLBACK;" </dev/null 2>/dev/null; then
    YAZMA=YAPILABILDI
  fi
fi

echo
echo "IKIZ_OKUMA_KULLANICI: $ROL"
echo "IKIZ_OKUMA_SIFRESI: $SIFRE"
echo "IKIZ_OKUMA_PORT: $PORT (sunucunun içinde; pgAdmin'de SSH tüneliyle localhost)"
echo "IKIZ_OKUMA_VERITABANLARI: ${VTLER[*]}"
genel "salt okunur kullanıcı hazır: ${#VTLER[@]} veritabanı, giriş $GIRIS, yazma $YAZMA, başka veritabanı $DIGER"
[ "$GIRIS" = tamam ] && [ "$YAZMA" = reddedildi ] && [ "$DIGER" = 0 ]
