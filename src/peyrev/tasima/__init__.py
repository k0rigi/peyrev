"""orkestrai (MariaDB) → Peyrev (PostgreSQL) veri taşıması: `python -m peyrev.tasima --hedef {prova,gercek}
--kaynak-ayar <orkestrai api/db.py>` (deploy/tasima.sh çalıştırır).

source: kaynağın tek tutarlı, salt okunur okunması; plan: dönüşüm kuralları; target: hedefe tek işlemde yazma, kimlik
dizileri ve doğrulama; report: kişisel veri içermeyen rapor.
"""
