"""Limits of the xlsx import (DM-001): what a roster file may weigh and how much of it is read."""
from __future__ import annotations

import struct
import zipfile
import zlib
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


_CHUNK = 64 * 1024
_LOCAL_HEADER = struct.Struct("<4s5H3L2H")  # signature, version, flags, method, time, date, crc, csize, usize, name length, extra length


def check_zip(data: bytes) -> None:
    """Refuse a non-zip, a zip with too many parts or one that REALLY unpacks to more than ``MAX_UNPACKED_BYTES`` - before openpyxl sees it.

    The sizes in the zip directory are the sender's claim and are not trusted: every part is inflated here in 64 KB steps and the bytes
    that come out are counted; the count is abandoned the moment the total passes the limit, and a part whose real size differs from the
    declared one (or whose CRC is wrong) is a broken file.  Never more than one chunk is held in memory.
    """
    try:
        with zipfile.ZipFile(BytesIO(data)) as archive:
            infos = archive.infolist()
        if len(infos) > MAX_ZIP_ENTRIES:
            raise ImportFileRejected(TOO_BIG_UNPACKED)
        total = 0
        for info in infos:
            if info.is_dir():
                continue
            total = _inflate_and_count(data, info, total)
    except ImportFileRejected:
        raise
    except (zipfile.BadZipFile, zipfile.LargeZipFile, zlib.error, struct.error, OSError, EOFError, ValueError, NotImplementedError):
        raise ImportFileRejected(UNREADABLE) from None


def _chunks_of(data: bytes, info: zipfile.ZipInfo):
    """The REAL content of one part, at most ``_CHUNK`` bytes at a time (never trusting the declared sizes)."""
    start = info.header_offset
    head = _LOCAL_HEADER.unpack(data[start:start + _LOCAL_HEADER.size])
    if head[0] != b"PK\x03\x04":
        raise ImportFileRejected(UNREADABLE)
    position = start + _LOCAL_HEADER.size + head[-2] + head[-1]
    end = position + info.compress_size
    if end > len(data) or info.flag_bits & 0x1 or info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
        raise ImportFileRejected(UNREADABLE)           # truncated, encrypted, or a method an xlsx never uses
    if info.compress_type == zipfile.ZIP_STORED:
        while position < end:
            yield data[position:min(position + _CHUNK, end)]
            position = min(position + _CHUNK, end)
        return
    inflater = zlib.decompressobj(-15)
    while position < end:
        pending = data[position:min(position + _CHUNK, end)]
        position += len(pending)
        while pending:
            out = inflater.decompress(pending, _CHUNK)                # at most one chunk of output per step
            pending = inflater.unconsumed_tail
            if out:
                yield out
            elif pending:
                raise ImportFileRejected(UNREADABLE)
    while not inflater.eof:                                           # output the inflater still holds after the last input
        out = inflater.decompress(b"", _CHUNK)
        if not out:
            raise ImportFileRejected(UNREADABLE)                      # the stream ended early
        yield out


def _inflate_and_count(data: bytes, info: zipfile.ZipInfo, total: int) -> int:
    """*total* plus the real unpacked size of one part; raises past the limit and for a part that is not what the directory says."""
    counted, crc = 0, 0
    for chunk in _chunks_of(data, info):
        counted += len(chunk)
        total += len(chunk)
        if total > MAX_UNPACKED_BYTES:
            raise ImportFileRejected(TOO_BIG_UNPACKED)
        crc = zlib.crc32(chunk, crc)
    if counted != info.file_size or (crc & 0xFFFFFFFF) != info.CRC:
        raise ImportFileRejected(UNREADABLE)
    return total
