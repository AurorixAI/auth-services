"""The six letters: multipart/alternative with a text part, escaped user input, the links, no foreign domains, mobile / dark markup."""
import re
from datetime import datetime, timedelta

import pytest

from src.mail import letters, render, sender
from src.mail import config as mail_config

EVIL = '<script>alert("x")</script> & <img src=x onerror=alert(1)> "q" \'s\''
FRONT, CONSOLE = "https://app.test", "https://console.test"
ALLOWED = ("https://app.test", "https://console.test", "https://algolab.me", "mailto:", "tel:")


@pytest.fixture(autouse=True)
def mail_env(monkeypatch):
    for key, value in {
        "FRONTEND_URL": FRONT, "CONSOLE_URL": CONSOLE, "SITE_URL": "https://algolab.me", "ADMIN_NOTIFICATION_EMAIL": "owner@example.test",
        "MAIL_REPLY_TO": "hello@example.test", "SMTP_FROM_NAME": "ALGO", "SMTP_FROM": "noreply@algolab.me",
    }.items():
        monkeypatch.setenv(key, value)


def sample_request(**over):
    base = {
        "id": "7f0c1e2a-9b1d-4c33-8a55-0d6f3b2a1c9e", "contact_name": "Иванов Иван Иванович", "centre_name": "Учебный центр «Альфа»", "city": "Ташкент",
        "phone": "+998901234567", "email": "ivan@example.uz", "students_count": 120, "comment": "Хотим попробовать",
        "source": {"utm_source": "telegram"}, "created_at": datetime(2026, 10, 25, 9, 30), "is_repeat": False,
    }
    base.update(over)
    return base


def all_letters(**over):
    now = datetime(2026, 10, 25, 9, 30)
    name = over.get("name", "Иван Иванович")
    centre = over.get("centre", "Учебный центр «Альфа»")
    return [
        letters.render_application_received(contact_name=name, centre_name=centre),
        letters.render_welcome(greeting=name, centre_name=centre, token="TOKEN_abc-123", expires_at=now + timedelta(hours=72), email="ivan@example.uz"),
        letters.render_application_declined(greeting=name, centre_name=centre, reason=over.get("reason", "Причина отказа")),
        letters.render_new_application(sample_request(contact_name=name, centre_name=centre, comment=over.get("comment", "Комментарий"))),
        letters.render_password_reset(greeting=name, token="RESET_abc-123", email="ivan@example.uz"),
        letters.render_staff_invite(centre_name=centre, org_role="teacher", code="CODE_abc123XYZ45", expires_at=now + timedelta(days=7)),
        letters.render_password_changed(greeting=name, changed_at=now),
        letters.render_email_change_confirm(greeting=name, centre_name=centre, token="CONFIRM_abc-123", expires_at=now + timedelta(hours=24)),
        letters.render_email_change_notice(greeting=name, centre_name=centre, new_email_masked="n***@example.uz"),
        letters.render_email_changed(greeting=name, new_email="new@example.uz"),
    ]


def test_there_are_ten_letters_and_each_has_html_and_text():
    rendered = all_letters()
    assert [r.letter for r in rendered] == list(render.LETTERS) == [
        "application_received", "welcome", "application_declined", "new_application", "password_reset", "staff_invite",
        "password_changed", "email_change_confirm", "email_change_notice", "email_changed",
    ]
    for r in rendered:
        assert r.subject and "\n" not in r.subject
        assert r.html.lstrip().lower().startswith("<!doctype html") and "</html>" in r.html
        assert len(r.text) > 200 and "<" not in r.text.replace("<3", "")      # a real text part, not markup
        assert "Команда ALGO" in r.text and "Команда ALGO" in r.html and "algolab.me" in r.html


def test_the_message_is_multipart_alternative_with_the_brand_headers():
    for r in all_letters():
        msg = sender.build_message("person@example.test", r)
        assert msg.get_content_type() == "multipart/alternative"
        kinds = [p.get_content_type() for p in msg.iter_parts()]
        assert kinds == ["text/plain", "text/html"]                           # the richest part last
        assert msg["From"] == "ALGO <noreply@algolab.me>" and msg["Reply-To"] == "hello@example.test" and msg["To"] == "person@example.test"
        assert msg["Message-ID"].endswith("@algolab.me>") and msg["Date"] and msg["Auto-Submitted"] == "auto-generated"
        assert msg.get_body(preferencelist=("plain",)).get_content().strip() == r.text.strip()
        assert msg.get_body(preferencelist=("html",)).get_content().strip() == r.html.strip()
        assert msg.get_content_charset() in (None, "utf-8") and sender.build_message("a@b.test", r).as_bytes()


def test_no_reply_to_when_none_is_configured(monkeypatch):
    monkeypatch.setenv("MAIL_REPLY_TO", "")
    assert sender.build_message("a@b.test", all_letters()[0])["Reply-To"] is None


def test_user_input_is_escaped_in_html_and_kept_verbatim_in_text():
    for r in all_letters(name=EVIL, centre=EVIL, reason=EVIL, comment=EVIL):
        assert "<script" not in r.html and "<img src=x" not in r.html and "onerror=alert(1)>" not in r.html, r.letter
        assert "&lt;script&gt;" in r.html, r.letter                                # shown as text, not run
    received = all_letters(name=EVIL, centre=EVIL)[0]
    assert "<script>" in received.text                                             # plain text is not markup: shown as typed


def test_a_header_injection_in_the_subject_or_the_address_cannot_start_a_header():
    import email
    import email.policy

    r = letters.render_new_application(sample_request(centre_name="Центр\r\nBcc: evil@example.test", city="Т\nX-Evil: 1"))
    msg = sender.build_message("a@b.test\r\nBcc: evil@example.test", r)
    parsed = email.message_from_bytes(msg.as_bytes(), policy=email.policy.default)
    assert parsed["Bcc"] is None and parsed["X-Evil"] is None
    assert "\n" not in parsed["Subject"] and "\n" not in parsed["To"]


def test_the_links_are_right():
    rendered = {r.letter: r for r in all_letters()}
    w = rendered["welcome"]
    assert f"{FRONT}/set-password/TOKEN_abc-123" in w.text and f'href="{FRONT}/set-password/TOKEN_abc-123"' in w.html
    assert "Задать пароль и войти" in w.html and "72 часа" in w.html and "ответьте на это письмо" in w.html
    assert "28 октября, 14:30" in w.text                                     # 72 h from 25.10 09:30 UTC, Tashkent time
    p = rendered["password_reset"]
    assert f'href="{FRONT}/set-password/RESET_abc-123"' in p.html and "1 час" in p.text and "Задать новый пароль" in p.html
    n = rendered["new_application"]
    assert f'href="{CONSOLE}/requests?id=7f0c1e2a-9b1d-4c33-8a55-0d6f3b2a1c9e"' in n.html and "Открыть в консоли" in n.html
    assert 'href="mailto:ivan@example.uz"' in n.html and 'href="tel:+998901234567"' in n.html
    assert rendered["application_received"].html.count("Что будет дальше") == 1 and "1–2 рабочих дней" in rendered["application_received"].text
    for r in rendered.values():
        assert 'href="https://algolab.me"' in r.html


def test_every_url_in_every_letter_is_ours():
    for r in all_letters():
        for url in re.findall(r"""(?:href|src)=["']([^"']+)["']""", r.html):
            assert url.startswith(ALLOWED), (r.letter, url)
        for url in re.findall(r"https?://[^\s\"'<>]+", r.html + r.text):
            assert url.startswith(("https://app.test", "https://console.test", "https://algolab.me", "http://www.w3.org")), (r.letter, url)
        assert "<img" not in r.html and "@import" not in r.html and "<link" not in r.html      # nothing is fetched from anywhere


def test_the_markup_is_email_safe_600px_mobile_and_dark():
    for r in all_letters():
        h = r.html
        assert 'width="600"' in h and "max-width:600px" in h and 'role="presentation"' in h
        assert 'name="viewport"' in h and "max-width: 620px" in h and "prefers-color-scheme: dark" in h and 'name="color-scheme"' in h
        assert "<table" in h and "display:flex" not in h and "display: flex" not in h and "grid" not in h
        assert "style=" in h and "Inter" in h and "#2563eb" in h                 # brand blue and font, inline
        assert 'lang="ru"' in h and "<title>" in h
    cta = [r for r in all_letters() if r.letter in ("welcome", "password_reset", "new_application")]
    assert all('class="btn-link"' in r.html and "border-radius:14px" in r.html for r in cta)
    assert all('class="btn-link"' not in r.html for r in all_letters() if r.letter in ("application_received", "application_declined"))


def test_the_decline_letter_shows_the_reason_only_when_there_is_one():
    with_reason = letters.render_application_declined(greeting="Иван", centre_name="Альфа", reason="Пока только от 50 учеников")
    without = letters.render_application_declined(greeting="Иван", centre_name="Альфа", reason=None)
    assert "Пока только от 50 учеников" in with_reason.html and "Комментарий команды" in with_reason.html and "Комментарий команды" in with_reason.text
    assert "Комментарий команды" not in without.html and "Комментарий команды" not in without.text


def test_the_owner_notification_has_every_field_and_marks_a_repeat():
    n = letters.render_new_application(sample_request(is_repeat=True, comment=None, students_count=None))
    for token in ("Иванов Иван Иванович", "Учебный центр", "Ташкент", "+998901234567", "ivan@example.uz", "telegram", "Получена"):
        assert token in n.html and token in n.text, token
    assert "Повторная заявка" in n.html and "ПОВТОРНАЯ ЗАЯВКА" in n.text and "Учеников" not in n.text and "Комментарий" not in n.text
    assert n.subject == "[ALGO] Новая заявка: Учебный центр «Альфа», Ташкент"


def test_a_greeting_is_optional():
    r = letters.render_password_reset(greeting="", token="t", email="")
    assert "Здравствуйте!" in r.text and "Здравствуйте, " not in r.text


def test_deliver_without_smtp_settings_sends_nothing_and_never_raises(monkeypatch, caplog):
    for key in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD"):
        monkeypatch.delenv(key, raising=False)
    r = all_letters()[1]
    with caplog.at_level("INFO"):
        assert sender.send("a@b.test", r) is False
    assert "TOKEN_abc-123" not in caplog.text                                   # a log line never carries the link


def test_a_broken_smtp_server_is_a_false_and_a_log_line(monkeypatch, caplog):
    import smtplib

    class Boom:
        def __init__(self, *a, **k):
            raise OSError("connection refused")

    monkeypatch.setattr(smtplib, "SMTP", Boom)
    monkeypatch.setenv("SMTP_HOST", "smtp.example.test")
    monkeypatch.setenv("SMTP_USER", "u")
    monkeypatch.setenv("SMTP_PASSWORD", "p")
    with caplog.at_level("ERROR"):
        assert sender.send("a@b.test", all_letters()[0]) is False
    assert "Failed to send a letter" in caplog.text


def test_the_owner_address_comes_from_the_environment_and_is_optional(monkeypatch):
    sent = []
    monkeypatch.setattr(sender, "send", lambda to, r: sent.append((to, r.letter)) or True)
    assert letters.send_new_application(sample_request()) is True and sent == [("owner@example.test", "new_application")]
    monkeypatch.setenv("ADMIN_NOTIFICATION_EMAIL", "")
    assert letters.send_new_application(sample_request()) is False and len(sent) == 1
    assert mail_config.load().from_name == "ALGO" and mail_config.load().from_address == "noreply@algolab.me"
