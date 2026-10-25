"""Letter rendering: Jinja2 HTML (autoescaped) + plain text of the same letter. Pure functions: no SMTP, no database."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

TEMPLATES = Path(__file__).parent / "templates"
TASHKENT = timezone(timedelta(hours=5))
MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря")

_html = Environment(
    loader=FileSystemLoader(str(TEMPLATES)), autoescape=select_autoescape(["j2"], default=True), undefined=StrictUndefined,
    trim_blocks=True, lstrip_blocks=True,
)
_text = Environment(
    loader=FileSystemLoader(str(TEMPLATES)), autoescape=False, undefined=StrictUndefined, keep_trailing_newline=True,
)

SUBJECTS: Dict[str, str] = {
    "application_received": "Заявка получена — ALGO",
    "welcome": "Добро пожаловать в ALGO — задайте пароль",
    "application_declined": "Ответ по вашей заявке — ALGO",
    "new_application": "[ALGO] Новая заявка: {centre_name}, {city}",
    "password_reset": "Сброс пароля ALGO",
}
LETTERS = tuple(SUBJECTS)

STEPS = (
    ("1", "Знакомство", "Короткий звонок или сообщение: узнаем, как устроен ваш центр и что для вас важно."),
    ("2", "Демо", "Покажем ALGO на примере вашего центра — без обязательств."),
    ("3", "Доступ", "Если мы подходим друг другу, откроем доступ и пришлём персональное приглашение со ссылкой."),
)


@dataclass(frozen=True)
class Rendered:
    letter: str
    subject: str
    html: str
    text: str


def tashkent_text(moment: datetime | None) -> str:
    """A naive-UTC moment as «28 октября, 14:30» in Tashkent time."""
    if moment is None:
        return ""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    local = moment.astimezone(TASHKENT)
    return f"{local.day} {MONTHS[local.month - 1]}, {local:%H:%M}"


def _clean(value: Any, limit: int = 2000) -> str:
    """Text that came from a user: one string, no control characters (the HTML layer escapes it, the text layer must not carry them)."""
    text = "" if value is None else str(value)
    text = "".join(ch for ch in text if ch in "\n\t" or (ord(ch) >= 32 and ord(ch) != 127))
    return text[:limit]


def _source_text(source: dict | None) -> str:
    if not isinstance(source, dict):
        return ""
    parts = [f"{k}={_clean(v, 120)}" for k, v in source.items() if v and k.startswith("utm_")]
    for key in ("referrer", "landing"):
        if source.get(key):
            parts.append(f"{key}: {_clean(source[key], 160)}")
    return "\n".join(parts)


def _prepare(letter: str, ctx: dict) -> dict:
    out = dict(ctx)
    for key in ("greeting", "centre_name", "city", "reason", "email"):
        if key in out:
            out[key] = _clean(out[key], 500)
    if letter == "application_received":
        out.setdefault("steps", STEPS)
    elif letter == "welcome":
        out["expires_text"] = tashkent_text(out.get("expires_at"))
        out.setdefault("expires_phrase", "72 часа")
        out.setdefault("email", "")
    elif letter == "password_reset":
        out.setdefault("expires_phrase", "1 час")
        out.setdefault("greeting", "")
        out.setdefault("email", "")
    elif letter == "application_declined":
        out.setdefault("reason", "")
    elif letter == "new_application":
        r = out["request"]
        rows = [
            ("Имя", _clean(r.get("contact_name"), 200), "plain"),
            ("Центр", _clean(r.get("centre_name"), 300), "plain"),
            ("Город", _clean(r.get("city"), 200), "plain"),
            ("Телефон", _clean(r.get("phone"), 40), "tel"),
            ("Почта", _clean(r.get("email"), 255), "mail"),
            ("Учеников", "" if r.get("students_count") is None else str(r.get("students_count")), "plain"),
            ("Комментарий", _clean(r.get("comment"), 2000), "plain"),
            ("Источник", _source_text(r.get("source")), "plain"),
            ("Получена", tashkent_text(r.get("created_at")) + " (Ташкент)", "plain"),
        ]
        out["rows"] = [(label, value, kind) for label, value, kind in rows if value and value.strip() != "(Ташкент)"]
        out["centre_name"] = _clean(r.get("centre_name"), 300)
        out["city"] = _clean(r.get("city"), 200)
        out["is_repeat"] = bool(r.get("is_repeat"))
    return out


def render(letter: str, ctx: dict) -> Rendered:
    if letter not in SUBJECTS:
        raise KeyError(letter)
    data = _prepare(letter, ctx)
    data.setdefault("site_url", "https://algolab.me")
    subject = SUBJECTS[letter].format(**{k: data.get(k, "") for k in ("centre_name", "city")}) if "{" in SUBJECTS[letter] else SUBJECTS[letter]
    data["subject"] = subject
    html = _html.get_template(f"{letter}.html.j2").render(**data)
    text = re.sub(r"\n{3,}", "\n\n", _text.get_template(f"{letter}.txt.j2").render(**data))
    return Rendered(letter=letter, subject=subject, html=html, text=text)
