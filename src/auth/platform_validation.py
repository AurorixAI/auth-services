"""A small validator for the JSON bodies of the new routes: every problem is collected with a Russian text and reported at once
(``422 validation_error`` + ``fields``). The routes take ``body: dict`` and read it through ``Form`` - no English pydantic messages."""
from __future__ import annotations

import re
import unicodedata
from typing import Any, Dict, Iterable, Optional
from uuid import UUID

from .platform_errors import PlatformError, validation_error

EMAIL_RE = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$")
def strip_format_controls(value: str) -> str:
    """Remove the invisible format characters (Unicode category Cf: bidi overrides, zero-width marks): they hide text and flip its order on screen."""
    return "".join(ch for ch in value if unicodedata.category(ch) != "Cf")


_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")  # control characters (tab and new lines are allowed in long text)


class Form:
    def __init__(self, data: Any) -> None:
        if data is None:
            data = {}
        if not isinstance(data, dict):
            raise PlatformError(422, "Некорректный запрос", "invalid_body")
        self.data = data
        self.errors: Dict[str, str] = {}

    def has(self, key: str) -> bool:
        return key in self.data and self.data[key] is not None

    def fail(self, key: str, message: str) -> None:
        self.errors.setdefault(key, message)

    def text(
        self, key: str, label: str, *, required: bool = True, min_len: int = 1, max_len: int = 200, multiline: bool = False,
        strip: bool = True,
    ) -> Optional[str]:
        value = self.data.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            if required:
                self.fail(key, f"{label}: обязательное поле")
            return None
        if not isinstance(value, str):
            self.fail(key, f"{label}: нужен текст")
            return None
        value = strip_format_controls(value)
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:  # a lone surrogate (JSON «\ud800»): it cannot be written to the database - 422, not 500
            self.fail(key, "Недопустимые символы в тексте")
            return None
        if strip:
            value = value.strip()
        if _CONTROL.search(value) or (not multiline and re.search(r"[\r\n\t]", value)):
            self.fail(key, f"{label}: недопустимые символы")
            return None
        if len(value) < min_len:
            self.fail(key, f"{label}: слишком коротко (минимум {min_len})")
            return None
        if len(value) > max_len:
            self.fail(key, f"{label}: слишком длинно (максимум {max_len})")
            return None
        return value

    def email(self, key: str, label: str = "Почта", *, required: bool = True) -> Optional[str]:
        value = self.text(key, label, required=required, max_len=255)
        if value is None:
            return None
        value = value.lower()
        if not EMAIL_RE.match(value) or ".." in value or len(value.split("@")[0]) > 64:
            self.fail(key, "Укажите корректную почту")
            return None
        return value

    def integer(self, key: str, label: str, *, required: bool = False, minimum: int = 0, maximum: int = 10**9) -> Optional[int]:
        value = self.data.get(key)
        if value is None or value == "":
            if required:
                self.fail(key, f"{label}: обязательное поле")
            return None
        if isinstance(value, bool):
            self.fail(key, f"{label}: нужно целое число")
            return None
        if isinstance(value, str) and re.fullmatch(r"\s*\d{1,12}\s*", value):
            value = int(value)
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        if not isinstance(value, int):
            self.fail(key, f"{label}: нужно целое число")
            return None
        if value < minimum or value > maximum:
            self.fail(key, f"{label}: допустимо от {minimum} до {maximum}")
            return None
        return value

    def boolean(self, key: str, *, default: bool = False) -> bool:
        value = self.data.get(key)
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        self.fail(key, "Нужно значение да/нет")
        return default

    def choice(self, key: str, label: str, allowed: Iterable[str], *, required: bool = True) -> Optional[str]:
        value = self.data.get(key)
        allowed = tuple(allowed)
        if value is None or value == "":
            if required:
                self.fail(key, f"{label}: обязательное поле")
            return None
        if not isinstance(value, str) or value not in allowed:
            self.fail(key, f"{label}: допустимо {', '.join(allowed)}")
            return None
        return value

    def uuid(self, key: str, label: str, *, required: bool = True) -> Optional[UUID]:
        value = self.data.get(key)
        if value is None or value == "":
            if required:
                self.fail(key, f"{label}: обязательное поле")
            return None
        try:
            return UUID(str(value))
        except (ValueError, AttributeError, TypeError):
            self.fail(key, f"{label}: некорректный идентификатор")
            return None

    def str_list(self, key: str, label: str, *, max_items: int = 100, max_len: int = 80) -> Optional[list]:
        value = self.data.get(key)
        if value is None:
            self.fail(key, f"{label}: обязательное поле")
            return None
        if not isinstance(value, list) or len(value) > max_items or any(not isinstance(v, str) or len(v) > max_len for v in value):
            self.fail(key, f"{label}: нужен список")
            return None
        return value

    def done(self) -> None:
        if self.errors:
            raise validation_error(self.errors)


def parse_uuid_or_404(value: str, what: str = "Объект") -> UUID:
    try:
        return UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        raise PlatformError(404, f"{what} не найден", "not_found") from None
