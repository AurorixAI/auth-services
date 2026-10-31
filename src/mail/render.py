"""Letter rendering: Jinja2 HTML (autoescaped) + plain text of the same letter. Pure functions: no SMTP, no database."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from src.auth.demo_request_choices import LOCALE_LABELS, NEEDS_LABELS, ORG_TYPE_LABELS, POSITION_LABELS, TOOL_LABELS, label

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
    "staff_invite": "Приглашение в ALGO — {centre_name}",
    "password_changed": "Пароль ALGO изменён",
    "email_change_confirm": "Подтвердите новый адрес почты — ALGO",
    "email_change_notice": "Вашу почту в ALGO меняют",
    "email_changed": "Почта в ALGO изменена",
    "support_new": "[ALGO] Обращение №{number}: {centre_name}",
    "support_author_reply": "[ALGO] Ответ автора по обращению №{number}: {centre_name}",
    "support_reply": "Есть ответ на обращение №{number} — ALGO",
}
# the letters that share a template file
TEMPLATE_OF: Dict[str, str] = {"support_new": "support_team", "support_author_reply": "support_team"}
ROLE_WORDS = {"owner": "владелец", "admin": "администратор", "teacher": "преподаватель"}
LETTERS = tuple(SUBJECTS)
ROLE_TEXT = {"teacher": "преподаватель", "admin": "администратор"}
INITIATOR_TEXT = {"owner": "Владелец", "admin": "Администратор"}

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


_SCHEME = re.compile(r"(?i)\b(https?|ftps?|file)\s*:\s*/{1,2}")
_PSEUDO = re.compile(r"(?i)\b(mailto|tel|javascript|data)\s*:")
_WWW = re.compile(r"(?i)\bwww\.")
_DOT = re.compile(r"(?<=\w)\.(?=[A-Za-z]{2,})")  # a dot between a word and Latin letters is what makes ``x.com`` a link


def _inert(value: str, limit: int) -> str:
    """Text from a stranger for the OWNER's letter: cut short and defanged so that no mail client turns it into a link
    (``https://x`` -> ``https[:]//x``, ``www.x`` -> ``www[.]x``, ``x.com`` -> ``x[.]com``). The console shows the original."""
    text = _clean(value, limit + 1)
    cut = len(text) > limit
    text = text[:limit].rstrip() + ("…" if cut else "")
    text = _SCHEME.sub(lambda m: f"{m.group(1)}[:]//", text)
    text = _PSEUDO.sub(lambda m: f"{m.group(1)}[:]", text)
    text = _WWW.sub("www[.]", text)
    return _DOT.sub("[.]", text)


def _source_text(source: dict | None) -> str:
    if not isinstance(source, dict):
        return ""
    parts = [f"{k}={_inert(v, 80)}" for k, v in source.items() if v and k.startswith("utm_")]
    for key in ("referrer", "landing"):
        if source.get(key):
            parts.append(f"{key}: {_inert(source[key], 120)}")
    return "\n".join(parts)


APPLICANT_WORDS_LETTERS = ("application_received", "welcome", "application_declined")


def _prepare(letter: str, ctx: dict) -> dict:
    out = dict(ctx)
    for key in ("greeting", "centre_name", "city", "reason", "email", "initiator_name", "new_email", "new_email_masked", "support_email"):
        if key in out:
            # the name and the centre came from the applicant's own form: in a letter he or the owner receives they must not turn into links
            inert = letter in APPLICANT_WORDS_LETTERS and key in ("greeting", "centre_name")
            out[key] = _inert(out[key], 500) if inert else _clean(out[key], 500)
    if letter == "application_received":
        out.setdefault("steps", STEPS)
    elif letter == "welcome":
        out["expires_text"] = tashkent_text(out.get("expires_at"))
        out.setdefault("expires_phrase", "72 часа")
        out.setdefault("email", "")
    elif letter == "staff_invite":
        out["expires_text"] = tashkent_text(out.get("expires_at"))
        out.setdefault("expires_phrase", "7 дней")
        out["role_text"] = ROLE_TEXT.get(out.get("org_role"), "сотрудник")
    elif letter == "password_reset":
        out.setdefault("expires_phrase", "1 час")
        out.setdefault("greeting", "")
        out.setdefault("email", "")
        out.setdefault("kind", "console")  # console (the team) / self («Забыли пароль») / staff (the owner or the administrator of the centre)
        out.setdefault("centre_name", "")
        out.setdefault("initiator_name", "")
        out["initiator_text"] = INITIATOR_TEXT.get(out.get("initiator_role"), "Руководитель")
        out["expires_text"] = tashkent_text(out.get("expires_at"))
        out["requested_text"] = tashkent_text(out.get("requested_at"))
    elif letter == "password_changed":
        out.setdefault("greeting", "")
        out["changed_text"] = tashkent_text(out.get("changed_at"))
    elif letter == "email_change_confirm":
        out.setdefault("greeting", "")
        out["expires_text"] = tashkent_text(out.get("expires_at"))
    elif letter in ("email_change_notice", "email_changed"):
        out.setdefault("greeting", "")
    elif letter == "application_declined":
        out.setdefault("reason", "")
    elif letter in ("support_new", "support_author_reply"):
        # the words of the author are cut short and defanged (no mail client makes a link of them); the full text stays in the console
        out["headline"] = "Новое обращение" if letter == "support_new" else "Ответ автора"
        out["rows"] = [
            ("Номер", f"№{out['number']}"),
            ("Центр", out.get("centre_name", "")),
            ("Роль", ROLE_WORDS.get(out.get("author_role"), "")),
            ("Текст" if letter == "support_new" else "Ответ", _inert(out.get("text"), 300)),
        ]
    elif letter == "support_reply":
        out.setdefault("greeting", "")
    elif letter == "new_application":
        r = out["request"]
        branches = r.get("branches_count")
        rows = [
            ("Имя", _inert(r.get("contact_name"), 120), "plain"),
            ("Должность", label(POSITION_LABELS, r.get("position")), "plain"),
            ("Центр", _inert(r.get("centre_name"), 200), "plain"),
            ("Тип центра", label(ORG_TYPE_LABELS, r.get("org_type")), "plain"),
            ("Город", _inert(r.get("city"), 100), "plain"),
            ("Адрес", _inert(r.get("address"), 300), "plain"),
            ("Телефон", _clean(r.get("phone"), 40), "tel"),
            ("Почта", _clean(r.get("email"), 255), "mail"),
            ("Удобное время", _inert(r.get("preferred_contact_time"), 100), "plain"),
            ("Учеников", "" if r.get("students_count") is None else str(r.get("students_count")), "plain"),
            ("Филиалов", "" if branches is None else ("нет" if branches == 0 else str(branches)), "plain"),
            ("Задачи", ", ".join(label(NEEDS_LABELS, code) for code in (r.get("needs") or []) if label(NEEDS_LABELS, code)), "plain"),
            ("Чем пользуются", label(TOOL_LABELS, r.get("current_tool")), "plain"),
            ("Откуда узнали", _inert(r.get("heard_from"), 200), "plain"),
            ("Язык формы", label(LOCALE_LABELS, r.get("locale")), "plain"),
            ("Комментарий", _inert(r.get("comment"), 600), "plain"),
            ("Источник", _source_text(r.get("source")), "plain"),
            ("Согласие на обработку данных", "дано " + tashkent_text(r["consent_at"]) if r.get("consent_at") else "", "plain"),
            ("Получена", tashkent_text(r.get("created_at")) + " (Ташкент)", "plain"),
        ]
        out["rows"] = [(label, value, kind) for label, value, kind in rows if value and value.strip() != "(Ташкент)"]
        out["centre_name"] = _inert(r.get("centre_name"), 200)  # these two go to the Subject too
        out["city"] = _inert(r.get("city"), 100)
        out["is_repeat"] = bool(r.get("is_repeat"))
    return out


def render(letter: str, ctx: dict) -> Rendered:
    if letter not in SUBJECTS:
        raise KeyError(letter)
    data = _prepare(letter, ctx)
    data.setdefault("site_url", "https://algolab.me")
    subject = SUBJECTS[letter].format(**{k: data.get(k, "") for k in ("centre_name", "city", "number")}) if "{" in SUBJECTS[letter] else SUBJECTS[letter]
    data["subject"] = subject
    template = TEMPLATE_OF.get(letter, letter)
    html = _html.get_template(f"{template}.html.j2").render(**data)
    text = re.sub(r"\n{3,}", "\n\n", _text.get_template(f"{template}.txt.j2").render(**data))
    return Rendered(letter=letter, subject=subject, html=html, text=text)
