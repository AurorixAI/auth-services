#!/usr/bin/env python3
"""PAY-2 conversion report - a DRY RUN that writes nothing (the whole run is ONE transaction that is rolled back).

    DATABASE_URL=postgresql+psycopg2://user:pass@host/db python scripts/pay2_convert_report.py [--org ID] [--json out.json]
                                                                                       [--limit 20] [--today YYYY-MM-DD]

What it does, on any database (stage copy, stage, production), before the release:

1. **before** - the OLD model: for every tracked membership the status and "paid until" that the old code gives (the explicit
   ``valid_from / valid_to / lessons_count`` of the payments, ``payment_status.compute_status``), read from the legacy columns
   by plain SQL, so it works on a database that is not migrated yet;
2. applies the migration ``e1f2a3b4c5d`` INSIDE the transaction (when the database is not at that revision yet);
3. **after** - the NEW model: ``ledger_service`` + ``billing_ledger.run_ledger`` over the same data (every live payment is now
   just money; the old periods are ignored);
4. prints, per organisation: the money totals (old live payments = new payments - they MUST be equal), the statuses before / after,
   debt and advance after, the sum of the distribution (everything that was paid must sit on a charge or in the advance), and the
   memberships whose picture changes (the debts that the old periods hid); then ROLLS BACK.

Student names are never printed (display id + group only); ``--json`` writes everything for a closer look.  Exit code 1 when a
money total differs or the distribution does not add up.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("APP_ENV", "testing")
os.environ.setdefault("SECRET_KEY", "pay2-report-secret-key-at-least-32-characters")

from src.core.db_timezone import utc_connect_args  # noqa: E402

MIGRATION = ROOT / "alembic" / "versions" / "e1f2a3b4c5d_pay2_balance_ledger.py"
LOGIC_A_MIGRATION = ROOT / "alembic" / "versions" / "f2a3b4c5d6e_logic_a_money.py"   # the tables the current loader reads (pauses, openings)
REVISION = "e1f2a3b4c5d"


def money(value: Optional[int]) -> str:
    return "—" if value is None else f"{value:,}".replace(",", " ")


def load_migration(path: Path = MIGRATION):
    spec = importlib.util.spec_from_file_location("pay2_migration_" + path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


# ------------------------------------------------------------------------------------------------ before (old model)


def old_picture(conn, session: Session, today: date, org_filter: Optional[str]) -> Dict[str, Dict[str, Any]]:
    """The old status of every tracked membership, by the legacy columns (plain SQL: the new columns may not exist yet)."""
    from src.auth import payment_status as ps
    from src.auth import pricing
    from src.auth.payments_service import PaymentService

    args: Dict[str, Any] = {}
    org_sql = ""
    if org_filter:
        org_sql, args["org"] = " AND s.org_id = :org", org_filter
    members = conn.execute(
        text(
            "SELECT sg.id, s.org_id, s.display_id, s.id AS student_id, g.id AS group_id, g.name, sg.joined_at "
            "FROM student_groups sg JOIN students s ON s.id = sg.student_id JOIN groups g ON g.id = sg.group_id "
            "WHERE sg.is_active IS TRUE AND g.status = 'active' AND s.status <> 'archived'" + org_sql
        ),
        args,
    ).fetchall()
    ids = [m.id for m in members]
    pays: Dict[Any, List[Any]] = {i: [] for i in ids}
    if ids:
        rows = conn.execute(
            text(
                "SELECT id, membership_id, amount, paid_on, valid_from, valid_to, lessons_count, carried_from_payment_id, "
                "carried_out_at, created_at FROM student_payments WHERE voided_at IS NULL AND membership_id = ANY(:ids)"
            ),
            {"ids": ids},
        ).fetchall()
        for r in rows:
            pays[r.membership_id].append(r)
    org_settings = {
        r.id: ((r.settings or {}).get("payments") or {}) for r in conn.execute(text("SELECT id, settings FROM organizations"))
    }
    books = pricing.load_books(session, {m.group_id for m in members}, ids)
    # lessons-mode memberships: counted lessons by the old rule (the payments' own valid_from is the start)
    lessons_ids = [m.id for m in members if any(p.lessons_count is not None for p in pays[m.id])]
    dates: Dict[Any, List[date]] = {}
    if lessons_ids:
        from src.models.auth import Group, Student, StudentGroup

        objs = {
            sg.id: (sg, st, gr)
            for sg, st, gr in session.query(StudentGroup, Student, Group)
            .join(Student, Student.id == StudentGroup.student_id)
            .join(Group, Group.id == StudentGroup.group_id)
            .filter(StudentGroup.id.in_(lessons_ids))
        }
        live = {m: [SimpleNamespace(valid_from=p.valid_from, lessons_count=p.lessons_count, carried_out_at=p.carried_out_at)
                    for p in pays[m] if p.lessons_count is not None] for m in lessons_ids}
        by_org: Dict[Any, List[tuple]] = {}
        for m in members:
            if m.id in objs:
                by_org.setdefault(m.org_id, []).append(objs[m.id])
        for org_id, tuples in by_org.items():
            dates.update(PaymentService(session)._counted_lesson_dates(str(org_id), tuples, live))
    out: Dict[str, Dict[str, Any]] = {}
    for m in members:
        block = org_settings.get(m.org_id, {})
        status_settings = ps.StatusSettings(
            expiring_days=block.get("expiring_days", ps.DEFAULT_EXPIRING_DAYS),
            expiring_lessons=block.get("expiring_lessons", ps.DEFAULT_EXPIRING_LESSONS),
        )
        records = [
            ps.PaymentRecord(
                paid_on=p.paid_on, valid_from=p.valid_from or p.paid_on, valid_to=p.valid_to, lessons_count=p.lessons_count,
                created_at=p.created_at or datetime.min, id=p.id, carried_out_at=p.carried_out_at,
                membership_id=p.membership_id, carried_from_id=p.carried_from_payment_id,
            )
            for p in pays[m.id]
            if p.valid_from is not None  # a payment entered after PAY-2 has no period: it is not part of the OLD picture
        ]
        price = books.price(m.id, m.group_id, today)
        result = ps.compute_status(today=today, monthly_fee=price.amount, payments=records,
                                   lesson_dates=dates.get(m.id, ()), settings=status_settings)
        out[str(m.id)] = {
            "org_id": str(m.org_id), "display_id": m.display_id, "group": m.name, "price": price.amount,
            "status": result.status, "mode": result.period_mode,
            "paid_until": result.paid_until.isoformat() if result.paid_until else None,
            "lessons_left": result.lessons_left,
            "money": sum(int(p.amount) for p in pays[m.id]), "payments": len(pays[m.id]),
        }
    return out


# ------------------------------------------------------------------------------------------------ after (new model)


def new_picture(session: Session, today: date, orgs: List[str]) -> Dict[str, Dict[str, Any]]:
    from src.auth import ledger_service as ls

    out: Dict[str, Dict[str, Any]] = {}
    for org_id in orgs:
        for row in ls.LedgerLoader(session, org_id, today).load(scope="tracked"):
            l = row.ledger
            distributed = sum(a.amount for a in l.allocations)
            out[str(row.membership.id)] = {
                "org_id": org_id, "status": l.status.status, "mode": l.mode,
                "paid_until": l.status.paid_until.isoformat() if l.status.paid_until else None,
                "lessons_left": l.status.lessons_left, "charged": l.charged, "paid": l.paid + l.transferred_in,
                "written_off": l.written_off, "debt": l.debt, "past_debt": l.past_debt, "advance": l.advance,
                "net_money": sum(l.net_money.values()), "distributed": distributed,
                "reconciles": l.reconciles, "flags": list(l.flags),
            }
    return out


# ------------------------------------------------------------------------------------------------ the report


def centre_check(session: Session, org_id: str, today: date) -> Dict[str, Any]:
    """The centre-wide books over EVERY membership with money (left, archived, transferred out too): what was paid equals
    what the ledgers distributed plus what went back (refunds, transfers out); every membership distributes exactly its net
    money; transfers out equal transfers in."""
    from src.auth import ledger_service as ls

    entries = ls.LedgerLoader(session, org_id, today).load(scope="all")
    paid = distributed_plus_cut = 0
    out = back = 0
    bad: List[str] = []
    for e in entries:
        l = e.ledger
        ids = {str(p.id) for p in e.payments if p.amount and p.amount > 0}
        paid += sum(int(p.amount) for p in e.payments if p.amount and p.amount > 0)
        net = sum(v for k, v in l.net_money.items() if k in ids)
        cut = sum(v for k, v in l.refunded_from.items() if k in ids) + sum(v for k, v in l.transferred_from.items() if k in ids)
        distributed_plus_cut += net + cut
        out += l.transferred_out
        back += l.transferred_in
        distributed = sum(a.amount for a in l.allocations)
        if distributed != sum(l.net_money.values()) or not l.reconciles:
            bad.append(str(e.membership.id)[:8])
    return {
        "memberships": len(entries), "payments_sum": paid, "net_plus_returned": distributed_plus_cut,
        "payments_ok": paid == distributed_plus_cut, "transfer_out": out, "transfer_in": back,
        "transfers_ok": out == back, "bad_memberships": bad[:20], "distribution_ok": not bad,
    }


def build_report(connection, *, today: date, org: Optional[str] = None, apply_migration: bool = True) -> Dict[str, Any]:
    """Everything the script prints, as data.  ``connection`` is inside a transaction the CALLER rolls back."""
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    session = Session(bind=connection)
    before = old_picture(connection, session, today, org)
    has_kind = connection.execute(text(
        "SELECT 1 FROM information_schema.columns WHERE table_name = 'student_payments' AND column_name = 'kind'")).scalar()
    version = REVISION if has_kind else connection.execute(text("SELECT version_num FROM alembic_version")).scalar()
    migrated_here = False
    if apply_migration and not has_kind:
        module = load_migration()
        with Operations.context(MigrationContext.configure(connection)):
            module.upgrade()
            if not connection.execute(text("SELECT to_regclass('membership_pauses')")).scalar():
                load_migration(LOGIC_A_MIGRATION).upgrade()      # the loader of today reads the LOGIC-A tables too
        migrated_here = True
    session.expire_all()
    with_money = {str(r[0]) for r in connection.execute(text("SELECT DISTINCT org_id FROM student_payments WHERE voided_at IS NULL"))}
    orgs = sorted({v["org_id"] for v in before.values()} | (with_money if not org else {org} & with_money))
    after = new_picture(session, today, orgs)
    totals = {}
    for org_id in orgs:
        old_money = connection.execute(
            text("SELECT coalesce(sum(amount), 0) FROM student_payments WHERE voided_at IS NULL AND org_id = :o"), {"o": org_id}
        ).scalar_one()
        mine_before = {k: v for k, v in before.items() if v["org_id"] == org_id}
        mine_after = {k: v for k, v in after.items() if v["org_id"] == org_id}
        new_money = connection.execute(
            text(
                "SELECT coalesce(sum(amount), 0) FROM student_payments "
                "WHERE voided_at IS NULL AND org_id = :o AND "
                + ("kind = 'payment'" if migrated_here or version == REVISION else "carried_from_payment_id IS NULL")
            ),
            {"o": org_id},
        ).scalar_one()
        name = connection.execute(text("SELECT name FROM organizations WHERE id = :o"), {"o": org_id}).scalar()
        tracked_money = sum(v["money"] for v in mine_before.values())
        totals[org_id] = {
            "name": name, "memberships": len(mine_before), "money_before": int(old_money), "money_after": int(new_money),
            "money_equal": int(old_money) == int(new_money), "tracked_money": tracked_money,
            "tracked_net": sum(v["net_money"] for v in mine_after.values()),
            "status_before": dict(Counter(v["status"] for v in mine_before.values())),
            "status_after": dict(Counter(v["status"] for v in mine_after.values())),
            "debt_total": sum(v["debt"] for v in mine_after.values()),
            "advance_total": sum(v["advance"] for v in mine_after.values()),
            "with_debt": sum(1 for v in mine_after.values() if v["debt"] > 0),
            "with_advance": sum(1 for v in mine_after.values() if v["advance"] > 0),
            "distribution_ok": all(
                v["distributed"] == v["net_money"] and v["reconciles"] for v in mine_after.values()),
            "centre": centre_check(session, org_id, today),
            "flags": dict(Counter(f for v in mine_after.values() for f in v["flags"])),
        }
    rows = []
    for key, b in before.items():
        a = after.get(key, {})
        rows.append({
            "membership_id": key, "org_id": b["org_id"], "display_id": b["display_id"], "group": b["group"],
            "old_status": b["status"], "old_paid_until": b["paid_until"], "old_lessons_left": b["lessons_left"],
            "new_status": a.get("status"), "new_paid_until": a.get("paid_until"), "new_lessons_left": a.get("lessons_left"),
            "money": b["money"], "charged": a.get("charged"), "debt": a.get("debt"), "advance": a.get("advance"),
            "changed": (b["status"], b["paid_until"]) != (a.get("status"), a.get("paid_until")),
        })
    return {
        "as_of": today.isoformat(), "migrated_in_transaction": migrated_here, "version_before": version,
        "orgs": totals, "memberships": rows,
        "ok": all(t["money_equal"] and t["distribution_ok"] and t["centre"]["payments_ok"] and t["centre"]["transfers_ok"]
                  and t["centre"]["distribution_ok"] for t in totals.values()),
    }


def render(report: Dict[str, Any], limit: int) -> str:
    out = [f"PAY-2: отчёт конвертации на {report['as_of']} — ничего не записывается (вся работа в одной транзакции, откат в конце)."]
    out.append("Миграция e1f2a3b4c5d применена внутри транзакции." if report["migrated_in_transaction"]
               else f"Миграция не применялась (версия БД {report['version_before']}).")
    for org_id, t in report["orgs"].items():
        out.append(f"\nЦентр «{t['name']}» ({org_id[:8]})  отслеживаемых членств: {t['memberships']}")
        out.append(f"  деньги (живые оплаты): было {money(t['money_before'])} · стало {money(t['money_after'])} · "
                   f"{'РАВНЫ' if t['money_equal'] else 'НЕ РАВНЫ !!!'}")
        out.append(f"  распределение: оплачено у отслеживаемых {money(t['tracked_net'])} из {money(t['tracked_money'])} "
                   f"(остальное — у ушедших / архивных / перенесено) · сходится: {'да' if t['distribution_ok'] else 'НЕТ !!!'}")
        c = t["centre"]
        out.append(f"  по всему центру ({c['memberships']} членств, с ушедшими и архивными): оплачено {money(c['payments_sum'])} · "
                   f"распределено + возвращено {money(c['net_plus_returned'])} · {'сходится' if c['payments_ok'] else 'НЕ СХОДИТСЯ !!!'}; "
                   f"переводы: ушло {money(c['transfer_out'])} / пришло {money(c['transfer_in'])} · "
                   f"{'сходится' if c['transfers_ok'] else 'НЕ СХОДИТСЯ !!!'}; распределение по членствам: "
                   f"{'ок' if c['distribution_ok'] else 'ОШИБКА ' + ', '.join(c['bad_memberships'])}")
        out.append(f"  статусы до:    {dict(sorted(t['status_before'].items()))}")
        out.append(f"  статусы после: {dict(sorted(t['status_after'].items()))}")
        out.append(f"  долг: {money(t['debt_total'])} у {t['with_debt']} членств · аванс: {money(t['advance_total'])} у {t['with_advance']}"
                   + (f" · флаги: {t['flags']}" if t["flags"] else ""))
    changed = [r for r in report["memberships"] if r["changed"]]
    out.append(f"\nМембершипов, у которых «оплачено до» или статус изменились: {len(changed)} из {len(report['memberships'])}")
    if changed:
        out.append(f"  {'ученик':<12}{'группа':<22}{'было':<28}{'стало':<28}{'долг':>12}{'аванс':>12}")
        changed.sort(key=lambda r: -(r["debt"] or 0))
        for r in changed[:limit]:
            old = f"{r['old_status']} {r['old_paid_until'] or ''}".strip()
            new = f"{r['new_status']} {r['new_paid_until'] or ''}".strip()
            out.append(f"  {str(r['display_id'] or r['membership_id'][:8]):<12}{r['group'][:21]:<22}{old:<28}{new:<28}"
                       f"{money(r['debt']):>12}{money(r['advance']):>12}")
        if len(changed) > limit:
            out.append(f"  … и ещё {len(changed) - limit} (весь список: --json)")
    out.append("\nИТОГ: " + ("деньги равны, распределение сходится." if report["ok"] else "ЕСТЬ РАСХОЖДЕНИЯ — см. выше."))
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    parser.add_argument("--org")
    parser.add_argument("--json", dest="json_path")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--today")
    args = parser.parse_args()
    if not args.database_url:
        parser.error("DATABASE_URL is required")
    today = date.fromisoformat(args.today) if args.today else None
    if today is None:
        from src.auth.payment_status import business_today

        today = business_today()
    engine = create_engine(args.database_url, connect_args=utc_connect_args(args.database_url))
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                report = build_report(connection, today=today, org=args.org)
            finally:
                transaction.rollback()  # never commit: this is a dry run
    finally:
        engine.dispose()
    print(render(report, args.limit))
    if args.json_path:
        Path(args.json_path).write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
