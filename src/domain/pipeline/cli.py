"""
ALGO V1 — Pipeline CLI
src/pipeline/cli.py

Команды:
  pdf    — полная оцифровка PDF учебника
  json   — импорт задач из JSON
  audit  — аудит покрытия скелета задачами
  status — статистика БД
"""

import argparse
import json
import logging
import os

from src.domain.pipeline.config import CFG

log = logging.getLogger("pipeline")


# ------------------------------------------------------------------
# CLI helpers
# ------------------------------------------------------------------

def audit_skeleton(class_level: int) -> None:
    """Проверяет покрытие скелета задачами."""
    db_url = CFG.get("database_url", os.getenv("DATABASE_URL", ""))
    if not db_url:
        print("DATABASE_URL не задан")
        return

    from sqlalchemy import create_engine, text

    engine = create_engine(db_url)
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT kh.id, kh.name_ru, kh.level, kh.origin "
                "FROM knowledge_hierarchy kh "
                "LEFT JOIN tasks_master tm ON tm.skill_id = kh.id AND tm.is_active = TRUE "
                "WHERE kh.level = 'L4' "
                "  AND kh.class_level_start <= :cl AND kh.class_level_end >= :cl "
                "  AND kh.is_active = TRUE "
                "  AND tm.id IS NULL "
                "ORDER BY kh.sequence_order"
            ),
            {"cl": class_level},
        ).fetchall()

        total = conn.execute(
            text(
                "SELECT COUNT(*) FROM knowledge_hierarchy "
                "WHERE level = 'L4' "
                "  AND class_level_start <= :cl AND class_level_end >= :cl "
                "  AND is_active = TRUE"
            ),
            {"cl": class_level},
        ).scalar()

    covered = total - len(rows)
    pct = covered / total * 100 if total else 0
    print(f"\nАудит скелета для {class_level} класса")
    print(f"  Всего L4-навыков: {total}")
    print(f"  С задачами: {covered} ({pct:.1f}%)")
    print(f"  Без задач: {len(rows)}")

    if rows:
        print("\n  Навыки без задач:")
        for r in rows[:20]:
            badge = f" [{r[3]}]" if r[3] != "base" else ""
            print(f"    {r[0]}: {r[1]}{badge}")
        if len(rows) > 20:
            print(f"    … и ещё {len(rows) - 20}")


def show_status() -> None:
    """Показывает статистику БД."""
    db_url = CFG.get("database_url", os.getenv("DATABASE_URL", ""))
    if not db_url:
        print("DATABASE_URL не задан")
        return

    from sqlalchemy import create_engine, text

    engine = create_engine(db_url)
    with engine.connect() as conn:
        tasks = conn.execute(
            text("SELECT COUNT(*) FROM tasks_master WHERE is_active = TRUE")
        ).scalar()
        skills = conn.execute(
            text(
                "SELECT COUNT(*) FROM knowledge_hierarchy "
                "WHERE level = 'L4' AND is_active = TRUE"
            )
        ).scalar()
        textbooks = conn.execute(text("SELECT COUNT(*) FROM textbooks")).scalar()

        by_source = conn.execute(
            text(
                "SELECT LEFT(id, 6) AS prefix, COUNT(*) "
                "FROM tasks_master WHERE is_active = TRUE "
                "GROUP BY prefix ORDER BY prefix"
            )
        ).fetchall()

        by_difficulty = conn.execute(
            text(
                "SELECT difficulty, COUNT(*) "
                "FROM tasks_master WHERE is_active = TRUE "
                "GROUP BY difficulty ORDER BY difficulty"
            )
        ).fetchall()

    print(f"\nСтатус БД")
    print(f"  Задач: {tasks}")
    print(f"  L4-навыков: {skills}")
    print(f"  Учебников: {textbooks}")
    print(f"\n  По источнику:")
    for prefix, cnt in by_source:
        print(f"    {prefix}*: {cnt}")
    print(f"\n  По сложности:")
    label_map = {"A": "Лёгкий", "B": "Средний", "C": "Сложный"}
    for d, cnt in by_difficulty:
        print(f"    {d} ({label_map.get(d, d)}): {cnt}")


# ------------------------------------------------------------------
# main()
# ------------------------------------------------------------------

def main() -> None:
    """Entry-point для CLI конвейера оцифровки."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    parser = argparse.ArgumentParser(
        description="ALGO V1 — Конвейер оцифровки учебников",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", help="Команда")

    # pdf
    p_pdf = sub.add_parser("pdf", help="Полная оцифровка PDF учебника")
    p_pdf.add_argument("pdf_path", help="Путь к PDF файлу")
    p_pdf.add_argument("--textbook-id", required=True, help="UUID учебника в БД")
    p_pdf.add_argument("--class-level", type=int, default=5, help="Класс (5–11)")

    # json
    p_json = sub.add_parser("json", help="Импорт задач из JSON")
    p_json.add_argument("json_path", help="Путь к JSON файлу")
    p_json.add_argument("--textbook-id", required=True, help="UUID учебника в БД")
    p_json.add_argument("--class-level", type=int, default=5, help="Класс (5–11)")

    # audit
    p_audit = sub.add_parser("audit", help="Аудит покрытия скелета задачами")
    p_audit.add_argument("--class-level", type=int, default=5, help="Класс")

    # status
    sub.add_parser("status", help="Статистика БД")

    args = parser.parse_args()

    if args.command == "pdf":
        from src.domain.pipeline.orchestrator import DigitizationPipeline

        pipeline = DigitizationPipeline(args.textbook_id, args.class_level)
        pipeline.process_pdf(args.pdf_path)

    elif args.command == "json":
        from src.domain.pipeline.orchestrator import DigitizationPipeline

        pipeline = DigitizationPipeline(args.textbook_id, args.class_level)
        pipeline.process_json(args.json_path)

    elif args.command == "audit":
        audit_skeleton(args.class_level)

    elif args.command == "status":
        show_status()

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
