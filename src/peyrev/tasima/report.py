"""Taşıma raporu (stdout, şifreli yapıta gider): tablo adları, sayılar, kimlik numaraları, özetler ve sabit metinler.

E-posta, ad, başlık, mesaj gibi satır içerikleri yazılmaz: plan ve hedef, kayıtları yalnız kimlik numarasıyla anar.
"""

from __future__ import annotations

from ..clock import TR
from .plan import SOURCES, TARGETS
from .target import GERCEK, Outcome, state_text


def render(outcome: Outcome) -> str:
    moment = outcome.now.astimezone(TR)
    lines = [f"Peyrev taşıması (orkestrai → Peyrev): kip {outcome.mode}, hedef veritabanı {outcome.database or '-'}",
             f"Taşıma anı {moment:%Y-%m-%d %H:%M:%S} (Türkiye saati); lisans günü {moment.date().isoformat()}"]
    plan = outcome.plan
    if plan is not None:
        lines += ["", "Tablolar: kaynak (uygun) / hedef / atlanan (neden)"]
        for table in TARGETS:
            rows = plan.tables[table]
            matched = outcome.matched.get(table)
            line = (f"  {table} ← {SOURCES[table]}: {rows.source} ({len(rows.rows)}) / "
                    f"{'-' if matched is None else matched} / {sum(rows.skipped.values())}")
            reasons = sorted(rows.skipped.items(), key=lambda item: (-item[1], item[0]))
            lines.append(line + (" (" + "; ".join(f"{reason}: {count}" for reason, count in reasons) + ")"
                                 if reasons else ""))
        lines.append("  kaynak: okunan satır; uygun: yazılacak satır; hedef: uygun satırlardan hedefte birebir aynı olan.")
        if outcome.totals:
            lines.append("  Hedef tablolardaki bütün satırlar: "
                         + ", ".join(f"{table} {outcome.totals[table]}" for table in TARGETS))
    if outcome.verified:
        source_total, target_total, count = outcome.passwords
        same = "uyuşuyor" if source_total == target_total else "UYUŞMUYOR"
        others = "değişmedi" if not outcome.disturbed else "DEĞİŞTİ (" + ", ".join(outcome.disturbed) + ")"
        sequences = "; ".join(f"{item.name} sonraki {item.next_value}" + (" (ilerletildi)" if item.advanced else "")
                              for item in outcome.sequences)
        passwords = f"kaynak {source_total}, hedef {target_total} ({same})"
        lines += ["", "Denetimler",
                  f"  Şifre özetlerinin SHA-256 toplamı ({count} hesap): {passwords}",
                  f"  Ofis kimlikleri: uygun {outcome.offices[0]} ofisin {outcome.offices[1]} tanesi hedefte aynı kimlikle",
                  f"  Talep numaraları: uygun {outcome.tickets[0]} talebin {outcome.tickets[1]} tanesi hedefte aynı numarayla",
                  f"  Peyrev'de doğan satırlar: {others}",
                  f"  Kimlik dizileri: {sequences}"]
        lines += ["", "Modül hakları (hesap kimliği: modül beklenen → hedefte)"]
        by_user: dict[int, list[str]] = {}
        for item in outcome.licenses:
            by_user.setdefault(item.user_id, []).append(
                f"{item.module} {state_text(item.expected)} → {state_text(item.actual)}"
                + ("" if item.actual == item.expected else " UYUŞMUYOR"))
        lines += [f"  {user_id}: " + "; ".join(parts) for user_id, parts in by_user.items()] or ["  (hesap yok)"]
    if plan is not None and plan.notes:
        lines += ["", "Notlar"] + [f"  - {note}" for note in plan.notes]
    problems = (plan.problems if plan is not None else []) + outcome.problems
    lines += ["", f"Sorunlar ({len(problems)})" if problems else "Sorunlar: yok"]
    lines += [f"  - {problem}" for problem in problems]
    if outcome.error:
        lines += ["", f"Hata: {outcome.error}"]
    lines += ["", _verdict(outcome, len(problems))]
    return "\n".join(lines) + "\n"


def _verdict(outcome: Outcome, problems: int) -> str:
    if outcome.ok:
        return f"SONUÇ: başarılı ({outcome.mode}); bütün tablolarda sayılar ve denetimler uyuşuyor, veri yazıldı."
    reason = "hata" if outcome.error else f"{problems} sorun"
    if outcome.committed:
        tail = "yazılanlar incelemek için prova veritabanında bırakıldı."
    elif outcome.mode == GERCEK:
        tail = "gerçek veritabanında hiçbir değişiklik kalmadı (işlem geri alındı)."
    else:
        tail = "prova veritabanına veri yazılmadı."
    return f"SONUÇ: BAŞARISIZ ({outcome.mode}, {reason}); {tail}"
