#!/usr/bin/env python3
"""Write the golden examples of the OPENING-BULK contract (``tests/fixtures/openings``): one centre, accounting start 01.11.2026, today 22.10.2026.

    python scripts/openings_gen_fixtures.py          # rewrites the files
    python scripts/openings_gen_fixtures.py --check  # exit 1 when a file differs

Every example is built through the response models (so it is valid by construction); ``tests/test_openings_contract.py`` re-validates them and
``tests/test_opening_bulk_pg.py`` checks that the live answers have exactly the same keys and types.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("APP_ENV", "testing")
os.environ.setdefault("SECRET_KEY", "ci-tests-only-secret-at-least-32-characters")
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg2://x:x@localhost/x")

OUT = ROOT / "tests" / "fixtures" / "openings"
D = date
START, TODAY = D(2026, 11, 1), D(2026, 10, 22)
EXCEL_COLUMNS = [
    # the file is read BY THE HEADER TEXT, never by the position: the columns may move, ``when`` says when a column exists at all
    {"header": "Фамилия*", "kind": "input-keep", "note": "как в импорте; для сопоставления по имени"},
    {"header": "Имя*", "kind": "input-keep"},
    {"header": "Класс (1-11)*", "kind": "input-keep"},
    {"header": "Телефон", "kind": "reference", "note": "серый, при загрузке не читается"},
    {"header": "Группа*", "kind": "input-keep"},
    {"header": "Цена в месяц", "kind": "reference", "note": "«Цена пакета» в центре с режимом уроков; серый, не читается"},
    {"header": "Оплачено до (дата)", "kind": "input", "note": "настоящая дата DD.MM.YYYY; текст дд.мм.гггг / гггг-мм-дд тоже читается"},
    {"header": "Долг, сум", "kind": "input", "note": "целое число, формат #,##0"},
    {"header": "Долг на дату", "kind": "input", "note": "пусто = день перед началом учёта"},
    {"header": "Без остатка (да)", "kind": "input", "note": "«да» = проверено, долга на начало нет (пишется «оплачено до» = день перед началом учёта); вместе со значениями «Оплачено до» / долга — ошибка строки"},
    {"header": "Осталось уроков (пакет)", "kind": "input", "when": "только центр в режиме уроков", "note": "вместо «Оплачено до»"},
    {"header": "Комментарий", "kind": "reference", "when": "только если у кого-то из учеников есть комментарий"},
    {"header": "Статус", "kind": "reference", "when": "только если у кого-то из строк есть пометка (ушёл / перевод / бесплатно / пауза / есть оплаты)"},
    {"header": "ID ученика (не менять)", "kind": "key", "hidden": True, "note": "скрытая колонка в конце"},
    {"header": "ID членства (не менять)", "kind": "key", "hidden": True, "note": "скрытая колонка в конце; главный ключ сопоставления"},
]


def build() -> dict:
    from src.auth.openings_schemas import (
        OpeningBatchApplied, OpeningBatchImpact, OpeningBatchItem, OpeningBatchPreview, OpeningBatchSummary, OpeningFresh,
        OpeningGroupBlock, OpeningGroupRef, OpeningItemResult, OpeningItemState, OpeningLessons, OpeningMembershipRow, OpeningsError,
        OpeningsImportFile, OpeningsImportPreview, OpeningsImportRow, OpeningsList, OpeningsSummary, OpeningStudentRef, OpeningTotals,
        OpeningValue, OpeningWarning,
    )
    from src.auth.payments_schemas import PriceOut

    def row(n, student, name, gid, gname, *, mode="calendar", price=600_000, source="group", enrolled=D(2025, 9, 1), opening=None,
            money=False, missing=None, editable=True, why=None, left=None, only_debt=False, paused=False, display=None):
        mid = f"00000000-0000-4000-8000-0000000001{n:02d}"
        return OpeningMembershipRow(
            membership_id=mid,
            student=OpeningStudentRef(id=f"00000000-0000-4000-8000-0000000002{n:02d}", name=name, display_id=display or f"A-10{n:02d}", class_level=9),
            group=OpeningGroupRef(id=gid, name=gname), mode=mode,
            price=PriceOut(amount=price, source=source, valid_from=D(2026, 1, 1), group_amount=600_000 if source == "override" else None,
                           reason="скидка" if source == "override" else None),
            enrolled_on=enrolled, status="left" if left else "active", left_on=left, paused=paused, opening=opening,
            version=f"00000000-0000-4000-8000-0000000003{n:02d}" if opening else "-", has_money_records=money,
            needs_review=False, missing=(opening is None and not money and enrolled < START and not left) if missing is None else missing,
            floor=max(enrolled, D(2026, 10, 31)), only_debt=only_debt, editable=editable, reason_not_editable=why)

    g1, g2 = "00000000-0000-4000-8000-0000000000a1", "00000000-0000-4000-8000-0000000000a2"
    rows1 = [
        row(1, "s1", "Каримова Алия", g1, "Математика 9А", opening=OpeningValue(paid_until=D(2026, 11, 30))),
        row(2, "s2", "Юсупов Бек", g1, "Математика 9А", opening=OpeningValue(debt_amount=550_000, debt_on=D(2026, 10, 31))),
        row(3, "s3", "Орлова Дана", g1, "Математика 9А"),
        row(4, "s4", "Сидоров Тимур", g1, "Математика 9А", source="override", price=450_000, money=True),
        row(5, "s5", "Назарова Лола", g1, "Математика 9А", paused=True),
    ]
    rows2 = [
        row(6, "s6", "Ахмедов Руслан", g2, "Физика 10Б", mode="lessons", price=1_200_000, enrolled=D(2026, 9, 7)),
        row(7, "s7", "Ким Софья", g2, "Физика 10Б", enrolled=D(2026, 10, 5), editable=False, why="transfer"),
        row(8, "s8", "Рахимов Джамшид", g2, "Физика 10Б", price=0, editable=False, why="free"),
        row(9, "s9", "Исмаилова Зарина", g2, "Физика 10Б", enrolled=D(2026, 11, 3), missing=False),
    ]
    totals = OpeningTotals(memberships=9, with_opening=2, missing=6, left_available=1, shown=9, needs_review=0)
    lst = OpeningsList(
        as_of=TODAY, accounting_start=START, state="pending", period_mode="calendar", paid_until_max=D(2027, 10, 23), totals=totals,
        groups=[OpeningGroupBlock(id=g1, name="Математика 9А", teacher_name="Иванова А.", total=5, with_opening=2, missing=2, memberships=rows1),
                OpeningGroupBlock(id=g2, name="Физика 10Б", teacher_name="Петров П.", total=4, with_opening=0, missing=1, memberships=rows2)])
    left = row(10, "s10", "Мирзаев Одил", g2, "Физика 10Б", enrolled=D(2025, 9, 1), left=D(2026, 6, 30), only_debt=True,
               opening=OpeningValue(debt_amount=300_000, debt_on=D(2026, 6, 30)), missing=False)
    lst_left = OpeningsList(
        as_of=TODAY, accounting_start=START, state="pending", period_mode="calendar", paid_until_max=D(2027, 10, 23),
        totals=OpeningTotals(memberships=9, with_opening=2, missing=6, left_available=1, shown=1),
        groups=[OpeningGroupBlock(id=g2, name="Физика 10Б", teacher_name="Петров П.", total=1, with_opening=1, missing=0, memberships=[left])])
    summary = OpeningsSummary(accounting_start=START, state="pending", totals=OpeningTotals(memberships=9, with_opening=2, missing=6, left_available=1, shown=0))

    def st(debt=0, advance=0, paid=None, status="none", opening=None, left=None):
        return OpeningItemState(opening=opening, debt=debt, advance=advance, paid_until=paid, status=status, lessons_left=left)

    ok_paid = OpeningItemResult(
        membership_id=rows1[2].membership_id, student_name="Орлова Дана", group_name="Математика 9А", status="ok", kind="paid_until", changed=True,
        target=OpeningValue(paid_until=D(2026, 11, 30)), before=st(), after=st(paid=D(2026, 11, 30), status="active", opening=OpeningValue(paid_until=D(2026, 11, 30))))
    ok_debt = OpeningItemResult(
        membership_id=rows1[4].membership_id, student_name="Назарова Лола", group_name="Математика 9А", status="ok", kind="debt", changed=True,
        target=OpeningValue(debt_amount=550_000, debt_on=D(2026, 10, 31)), before=st(),
        after=st(debt=550_000, status="expired", opening=OpeningValue(debt_amount=550_000, debt_on=D(2026, 10, 31))))
    normalised = OpeningItemResult(
        membership_id=rows2[0].membership_id, student_name="Ахмедов Руслан", group_name="Физика 10Б", status="ok", kind="paid_until", changed=True,
        target=OpeningValue(paid_until=D(2026, 10, 31)), before=st(),
        after=st(paid=D(2026, 10, 31), status="active", opening=OpeningValue(paid_until=D(2026, 10, 31))),
        warnings=[OpeningWarning(code="PAID_UNTIL_AT_START", message="«Оплачено до 31.10»: на начало учёта долга нет (строка отмечена как проверенная)")])
    summary_ok = OpeningBatchSummary(items=3, changed=3, paid_until=2, debt=1, cleared=0, unchanged=0, errors=0, debt_total_added=550_000)
    impact = OpeningBatchImpact(memberships_changed=3, memberships_with_new_debt=1, memberships_with_changed_advance=0, debt_total_before=0,
                                debt_total_after=550_000, debt_delta=550_000, debtors_before=0, debtors_after=1, advance_delta=0, refundable_delta=0,
                                indirect_changed=0)
    preview = OpeningBatchPreview(ok=True, digest="d1" * 32, accounting_start=START, summary=summary_ok, impact=impact, needs_reason=[],
                                  items=[ok_paid, ok_debt, normalised])
    bad = OpeningItemResult(
        membership_id=rows1[3].membership_id, student_name="Сидоров Тимур", group_name="Математика 9А", status="error", kind="error",
        code="OPENING_RANGE", message="Оплачено до — не раньше 31.10.2026: за более ранние дни внесите как долг", has_money_records=True)
    money_row = OpeningItemResult(
        membership_id=rows1[0].membership_id, student_name="Каримова Алия", group_name="Математика 9А", status="ok", kind="paid_until", changed=True,
        target=OpeningValue(paid_until=D(2026, 12, 15)), before=st(paid=D(2026, 11, 30), status="active", opening=OpeningValue(paid_until=D(2026, 11, 30))),
        after=st(paid=D(2026, 12, 15), status="active", opening=OpeningValue(paid_until=D(2026, 12, 15))), has_money_records=True, needs_reason=True,
        warnings=[OpeningWarning(code="HAS_MONEY", message="У ученика уже есть оплаты: нужна причина")])
    preview_bad = OpeningBatchPreview(
        ok=False, digest="e2" * 32, accounting_start=START,
        summary=OpeningBatchSummary(items=2, changed=1, paid_until=1, debt=0, cleared=0, unchanged=0, errors=1, debt_total_added=0),
        impact=OpeningBatchImpact(memberships_changed=1), needs_reason=[money_row.membership_id], items=[bad, money_row])
    lessons_item = OpeningItemResult(
        membership_id=rows2[0].membership_id, student_name="Ахмедов Руслан", group_name="Физика 10Б", status="ok", kind="paid_until", changed=True,
        target=OpeningValue(paid_until=D(2026, 11, 14)), lessons=OpeningLessons(lessons_left=8, paid_until=D(2026, 11, 14), text="осталось 8 уроков → оплачено до 14.11"),
        before=st(), after=st(paid=D(2026, 11, 14), status="active", left=8, opening=OpeningValue(paid_until=D(2026, 11, 14))))
    preview_lessons = OpeningBatchPreview(
        ok=True, digest="f3" * 32, accounting_start=START,
        summary=OpeningBatchSummary(items=1, changed=1, paid_until=1), impact=OpeningBatchImpact(memberships_changed=1), needs_reason=[], items=[lessons_item])
    applied = OpeningBatchApplied(batch_id="00000000-0000-4000-8000-0000000004b1", replayed=False, summary=summary_ok, impact=impact, changed=2, state="pending")
    replayed = applied.model_copy(update={"replayed": True})
    imp_rows = [
        OpeningsImportRow(row=2, membership_id=rows1[2].membership_id, matched_by="membership_id", student_name="Орлова Дана", group_name="Математика 9А", status="ok", kind="paid_until"),
        OpeningsImportRow(row=3, membership_id=rows1[4].membership_id, matched_by="student_group", student_name="Назарова Лола", group_name="Математика 9А", status="ok", kind="debt"),
        OpeningsImportRow(row=4, membership_id=rows2[0].membership_id, matched_by="name", student_name="Ахмедов Руслан", group_name="Физика 10Б", status="ok", kind="paid_until"),
        OpeningsImportRow(row=5, status="error", code="UNKNOWN", message="Ученика нет в центре: добавьте его через импорт учеников"),
        OpeningsImportRow(row=6, membership_id=rows1[1].membership_id, matched_by="membership_id", student_name="Юсупов Бек", group_name="Математика 9А", status="error",
                          code="ID_MISMATCH", message="ID членства принадлежит другому ученику: проверьте, не сдвинулись ли колонки"),
        OpeningsImportRow(row=7, membership_id=rows1[0].membership_id, matched_by="membership_id", student_name="Каримова Алия", group_name="Математика 9А", status="error",
                          code="BAD_DATE", message="Оплачено до: «31.13.2026» — не дата"),
    ]
    import_preview = OpeningsImportPreview(
        **preview.model_dump(), file=OpeningsImportFile(rows_read=6, matched=5, changed=3, unchanged=0, errors=3, unknown=1, missing_in_file=4),
        rows=imp_rows, batch_items=[
            OpeningBatchItem(membership_id=rows1[2].membership_id, version="-", paid_until=D(2026, 11, 30)),
            OpeningBatchItem(membership_id=rows1[4].membership_id, version="-", debt_amount=550_000, debt_on=D(2026, 10, 31)),
            OpeningBatchItem(membership_id=rows2[0].membership_id, version="-", paid_until=D(2026, 10, 31))])

    def err(status, code, message, **extra):
        body = {"detail": {"code": code, "message": message, **extra}}      # exactly what the server sends (PaymentError.detail)
        OpeningsError.model_validate(body)
        return {"status": status, "body": body}

    fresh = OpeningFresh(membership_id=rows1[2].membership_id, version="00000000-0000-4000-8000-0000000003ff", opening=OpeningValue(paid_until=D(2026, 11, 30)))
    errors = {
        "STALE_ITEMS": err(409, "STALE_ITEMS", "Кто-то уже изменил эти строки — обновите таблицу", membership_ids=[rows1[2].membership_id], fresh=[fresh.model_dump(mode="json")]),
        "PREVIEW_STALE": err(409, "PREVIEW_STALE", "Данные изменились после проверки — проверьте ещё раз", preview=preview.model_dump(mode="json")),
        "IDEMPOTENCY_MISMATCH": err(409, "IDEMPOTENCY_MISMATCH", "Этот ключ уже использован для другого пакета"),
        "CONCURRENT_EDIT": err(409, "CONCURRENT_EDIT", "Другой пользователь меняет эти данные — повторите через секунду"),
        "BATCH_INVALID": err(422, "BATCH_INVALID", "В пакете есть строки с ошибкой — ничего не записано", items=[bad.model_dump(mode="json")]),
        "REASON_REQUIRED": err(422, "REASON_REQUIRED", "У учеников уже есть оплаты: укажите причину изменения остатка (от 3 до 500 символов)", membership_ids=[money_row.membership_id]),
        "TOO_MANY_ITEMS": err(422, "TOO_MANY_ITEMS", "Не больше 2000 строк за один раз — сохраните по группам"),
        "TOO_MANY_ROWS": err(422, "TOO_MANY_ROWS", "Слишком много учеников — выберите группу"),
        "TOO_MANY_CHANGES": err(422, "TOO_MANY_CHANGES", "В файле больше 2000 изменений — загрузите файл по группам"),
        "FILE_TOO_LARGE": err(422, "FILE_TOO_LARGE", "Файл слишком большой (макс. 5 МБ)"),
        "FILE_UNREADABLE": err(422, "FILE_UNREADABLE", "Не удалось прочитать файл. Загрузите .xlsx, скачанный из ALGO"),
        "SHEET_MISSING": err(422, "SHEET_MISSING", "В файле нет листа «Остатки». Скачайте актуальный файл"),
        "NOT_FOUND": err(404, "NOT_FOUND", "Членство не найдено", membership_ids=["00000000-0000-4000-8000-0000000001ff"]),
    }
    dump = lambda m: m.model_dump(mode="json")
    return {
        "O01_list_pending": dump(lst), "O02_list_include_left": dump(lst_left), "O03_summary_pending": dump(summary),
        "O04_preview_ok": dump(preview), "O05_preview_errors_and_reason": dump(preview_bad), "O06_preview_lessons": dump(preview_lessons),
        "O07_applied": dump(applied), "O08_applied_replayed": dump(replayed), "O09_import_preview": dump(import_preview),
        "errors": errors,
        "excel": {"sheet": "Остатки", "instruction_sheet": "Инструкция", "header_row": 5, "title_block_rows": 3, "total_row": "Итого", "sheet_order": ["Инструкция", "Остатки"], "columns": EXCEL_COLUMNS,
                  "limits": {"max_bytes": 5 * 1024 * 1024, "max_rows": 5000, "max_changes": 2000, "max_export_rows": 20000},
                  "rules": ["заголовок таблицы ищется среди первых 30 строк по названиям колонок (после блока заголовка файла это строка 5; в файле старого вида — строка 1); колонки читаются по названию, не по позиции",
                            "сопоставление: ID членства -> ID ученика + группа -> фамилия + имя + класс + группа (ровно одно совпадение)",
                            "K найден, но фамилия не совпала -> ID_MISMATCH",
                            "одно членство в двух строках -> DUPLICATE (обе не применяются)",
                            "пустые «Оплачено до» / «Долг» / «Долг на дату» у строки с остатком -> убрать остаток; пустые у строки без остатка -> без изменений (остаётся «не заполнено»)",
                            "«Без остатка (да)» -> оплачено до = день перед началом учёта (проверено, долга нет); у ушедшего / перевода / бесплатного — убрать остаток; у пришедшего в группу ПОСЛЕ начала учёта — без изменений (остаток, если он есть, убирается; OPENING_RANGE не возникает)",
                            "формулы не исполняются (data_only); ячейка-формула без значения -> FORMULA",
                            "при выгрузке текст, начинающийся с = + - @ TAB CR, хранится как текст",
                            "строка «Итого» внизу (без ID, в первой колонке «Итого») при загрузке пропускается"]},
    }


INDEX = {
    "O01_list_pending": "OpeningsList", "O02_list_include_left": "OpeningsList", "O03_summary_pending": "OpeningsSummary",
    "O04_preview_ok": "OpeningBatchPreview", "O05_preview_errors_and_reason": "OpeningBatchPreview", "O06_preview_lessons": "OpeningBatchPreview",
    "O07_applied": "OpeningBatchApplied", "O08_applied_replayed": "OpeningBatchApplied", "O09_import_preview": "OpeningsImportPreview",
}


def files() -> dict:
    data = build()
    out = {f"{stem}.json": value for stem, value in data.items()}
    out["index.json"] = {"models": INDEX}
    return {name: json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n" for name, value in out.items()}


def main() -> int:
    wanted = files()
    if "--check" in sys.argv:
        return 0 if all((OUT / n).exists() and (OUT / n).read_text(encoding="utf-8") == t for n, t in wanted.items()) else 1
    OUT.mkdir(parents=True, exist_ok=True)
    for name, text in wanted.items():
        (OUT / name).write_text(text, encoding="utf-8")
    print(f"{len(wanted)} files -> {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
