"""Limits of the xlsx import (DM-001): what a roster file may weigh and how much of it is read."""
from __future__ import annotations

import zipfile
from io import BytesIO

MAX_IMPORT_BYTES = 5 * 1024 * 1024          # the upload itself (checked from Content-Length before the body is read)
MAX_IMPORT_ROWS = 2000                       # data rows per sheet (blank rows do not count)
MAX_IMPORT_SHEETS = 10                       # sheets in the workbook
MAX_SCANNED_ROWS = 50_000                    # raw rows a sheet may span (formatted blanks included): stops a file that "starts" a million rows away
MAX_UNPACKED_BYTES = 40 * 1024 * 1024        # all parts of the xlsx (a zip) together once unpacked: the zip-bomb guard
MAX_ZIP_ENTRIES = 1000

UNREADABLE = "Не удалось прочитать файл. Загрузите .xlsx по нашему шаблону."
TOO_MANY_ROWS = f"В листе «{{sheet}}» больше {MAX_IMPORT_ROWS} строк. Разбейте файл на несколько и загрузите по очереди."
TOO_MANY_SHEETS = f"В файле больше {MAX_IMPORT_SHEETS} листов. Оставьте только «Группы» и «Ученики»."
TOO_BIG_UNPACKED = "Файл слишком велик после распаковки. Сохраните его заново из Excel без лишних данных и картинок."


class ImportFileRejected(Exception):
    """The file itself cannot be imported (not an xlsx, too heavy, too long): HTTP 422 with this Russian text."""


def check_zip(data: bytes) -> None:
    """Refuse a non-zip, a zip with too many parts or one that unpacks to more than ``MAX_UNPACKED_BYTES`` - before openpyxl sees it.

    The sizes come from the zip directory; reading a part never yields more than its declared size, so the declared sum is the bound.
    """
    try:
        with zipfile.ZipFile(BytesIO(data)) as archive:
            infos = archive.infolist()
    except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError, EOFError, ValueError, NotImplementedError):
        raise ImportFileRejected(UNREADABLE) from None
    if len(infos) > MAX_ZIP_ENTRIES or sum(i.file_size for i in infos) > MAX_UNPACKED_BYTES:
        raise ImportFileRejected(TOO_BIG_UNPACKED)
