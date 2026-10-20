#!/usr/bin/env python3
"""Dry run of the price migration b8c9d0e1f2a (ROADMAP #105) - READ ONLY, writes nothing.

Run it on PRODUCTION BEFORE the release, against the database the migration will run on:

    DATABASE_URL=postgresql+psycopg2://user:pass@host/db python scripts/group_price_dry_run.py [--json] [--verbose]

It reads ``student_groups.monthly_fee`` and applies the very function the migration uses (``plan_backfill`` of
``alembic/versions/b8c9d0e1f2a_group_prices.py``), so what it prints is what the migration will do:

* per centre and group: the votes of the most frequent fee, the price the group gets or «без цены (меньше 2
  совпадений)» (a price needs at least 2 votes: one student's fee must not become the price of a group; then every
  fee stays an individual price and nobody changes), ties and single-vote groups flagged, how many memberships
  follow the price, how many get an individual price (exception) and how many memberships WITHOUT a fee start to
  follow the price, active / inactive (the only intended change of a student's price: the line to read before the
  release);
* "активные" = active membership of a student who is not archived; the exceptions also cover inactive memberships.

The connection is opened read-only (``SET TRANSACTION READ ONLY``): the script cannot change the database.
Student names are never printed; ``--verbose`` lists the sums of the exceptions per group.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy import create_engine, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.core.db_timezone import utc_connect_args  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "alembic" / "versions" / "b8c9d0e1f2a_group_prices.py"


def load_migration():
    spec = importlib.util.spec_from_file_location("group_prices_migration", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


MIN_VOTES = load_migration().MIN_VOTES


def money(value) -> str:
    return "—" if value is None else f"{value:,}".replace(",", " ")


def collect(url: str):
    migration = load_migration()
    engine = create_engine(url, connect_args=utc_connect_args(url))
    try:
        with engine.connect() as connection:
            connection.execute(text("SET TRANSACTION READ ONLY"))
            assert connection.execute(text("SHOW transaction_read_only")).scalar() == "on"
            rows = migration.fetch_rows(connection)
            target = connection.execute(text("SELECT current_database()")).scalar()
            tables = {
                row[0]
                for row in connection.execute(
                    text("SELECT table_name FROM information_schema.tables WHERE table_name IN ('group_prices')")
                )
            }
            connection.rollback()
    finally:
        engine.dispose()
    return migration.plan_backfill(rows), target, bool(tables)


def report(plans, target: str, applied: bool, verbose: bool) -> str:
    out = [f"Сухой прогон переноса цен (b8c9d0e1f2a) — БД «{target}», только чтение, ничего не записывается."]
    if applied:
        out.append("ВНИМАНИЕ: таблица group_prices уже есть — миграция применена или применялась раньше.")
    total = Counter()
    org = None
    for plan in plans:
        if plan.org_name != org:
            org = plan.org_name
            out.append(f"\nЦентр: {org}")
            out.append(
                f"  {'Группа':<26}{'Цена группы':>33}{'Голосов':>9}{'Следуют':>9}{'Исключения':>12}{'Без цены → цена':>17}  Пометки"
            )
        if plan.price is not None:
            price = money(plan.price)
        elif plan.votes:
            price = "без цены (меньше 2 совпадений)"
        else:
            price = "без цены (сумм нет)"
        exceptions = f"{len(plan.overrides)} ({plan.override_active} акт.)" if plan.overrides else "0"
        null_fee = f"{plan.null_active} / {plan.null_inactive}"
        flags = []
        if plan.tie:
            flags.append("НИЧЬЯ" + (" (выбрана сумма, заданная позже, затем меньшая)" if plan.price is not None else " — без цены"))
        if plan.single_vote:
            flags.append("ОДИН ГОЛОС — суммы остаются индивидуальными")
        out.append(
            f"  {plan.group_name[:25]:<26}{price:>33}{plan.votes:>9}"
            f"{plan.follow_active:>9}{exceptions:>12}{null_fee:>17}  {'; '.join(flags)}"
        )
        if verbose and plan.overrides:
            sums = Counter(fee for _, fee, _ in plan.overrides)
            out.append("      исключения: " + ", ".join(f"{money(fee)} × {n}" for fee, n in sorted(sums.items())))
        total["groups"] += 1
        total["priced"] += plan.price is not None
        total["no_price"] += plan.price is None
        total["single"] += plan.single_vote
        total["tie"] += plan.tie
        total["memberships"] += plan.memberships
        total["active"] += plan.active
        total["follow_active"] += plan.follow_active
        total["follow_inactive"] += plan.follow_inactive
        total["overrides"] += len(plan.overrides)
        total["overrides_active"] += plan.override_active
        total["null_active"] += plan.null_active
        total["null_inactive"] += plan.null_inactive
    out.append(
        "\nГолосов — активных членств с самой частой суммой. Цена группы — только при {min} голосах и больше: иначе группа остаётся без цены,"
        " а каждая сумма — индивидуальной ценой «Цена до введения цены группы» (ничего не меняется).".format(min=MIN_VOTES)
    )
    out.append("Следуют — активных членств, которые берут цену группы (сумма равна ей или пуста).")
    out.append("Исключения — членства (любые, и неактивные) с другой суммой: им записывается индивидуальная цена с 01.01.2000.")
    out.append("Без цены → цена — членства без суммы, которые начнут брать цену группы: активных / неактивных.")
    out.append("НИЧЬЯ — несколько сумм набрали поровну голосов: берётся заданная позже, затем меньшая (если голосов хватает).")
    out.append(
        "\nИтого: групп с членствами {groups}; получат цену {priced} (ничья по голосам: {tie}), без цены {no_price} "
        "(из них с одним голосом: {single}).\n"
        "  членств {memberships} (активных {active}); следуют цене группы: активных {follow_active}, "
        "неактивных {follow_inactive};\n"
        "  индивидуальные цены (исключения): {overrides} (активных {overrides_active});\n"
        "  НАЧНУТ БРАТЬ ЦЕНУ ГРУППЫ (сейчас без цены): активных {null_active}, неактивных {null_inactive}.".format(**total)
    )
    return "\n".join(out)


def as_json(plans, target: str, applied: bool):
    return {
        "database": target,
        "migration_already_applied": applied,
        "groups": [
            {
                "org": p.org_name,
                "group": p.group_name,
                "group_id": str(p.group_id),
                "price": p.price,
                "votes": p.votes,
                "min_votes": MIN_VOTES,
                "no_price_reason": None if p.price is not None else ("less_than_min_votes" if p.votes else "no_fees"),
                "single_vote": p.single_vote,
                "tie": p.tie,
                "memberships": p.memberships,
                "active": p.active,
                "follow_active": p.follow_active,
                "follow_inactive": p.follow_inactive,
                "overrides": len(p.overrides),
                "overrides_active": p.override_active,
                "null_fee_start_following_active": p.null_active,
                "null_fee_start_following_inactive": p.null_inactive,
            }
            for p in plans
        ],
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL"), help="default: $DATABASE_URL")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--verbose", action="store_true", help="list the sums of the exceptions per group")
    args = parser.parse_args(argv)
    if not args.database_url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 2
    host = urlsplit(args.database_url.replace("postgresql+psycopg2://", "postgresql://", 1)).hostname
    print(f"# database host: {host}", file=sys.stderr)
    plans, target, applied = collect(args.database_url)
    if args.json:
        print(json.dumps(as_json(plans, target, applied), ensure_ascii=False, indent=2, default=str))
    else:
        print(report(plans, target, applied, args.verbose))
    return 0


if __name__ == "__main__":
    sys.exit(main())
