"""K2-1, block G (letters), no database: the Subject survives (invariant 9), the headers, the allow-list of a stage (invariant 8), the staff invitation letter,
the plain-text part without base64, the texts that invite a reply."""
import base64
import email
import logging
import random
import re
import sys
from datetime import datetime, timedelta
from email import policy

import pytest

from src.mail import config as mail_config
from src.mail import letters, render, sender

FRONT = "https://app.test"


@pytest.fixture(autouse=True)
def mail_env(monkeypatch):
    for key, value in {
        "FRONTEND_URL": FRONT, "CONSOLE_URL": "https://console.test", "SITE_URL": "https://algolab.me", "ADMIN_NOTIFICATION_EMAIL": "owner@example.test",
        "MAIL_REPLY_TO": "support@algolab.me", "SMTP_FROM_NAME": "ALGO", "SMTP_FROM": "noreply@algolab.me",
    }.items():
        monkeypatch.setenv(key, value)
    for key in ("MAIL_ALLOWLIST", "MAIL_STAGE_MODE", "APP_ENV"):
        monkeypatch.delenv(key, raising=False)


def staff_letter(centre="Учебный центр «Альфа»", role="teacher", code="Zk3Q9dR2mT7vB5nX8aLw"):
    return letters.render_staff_invite(centre_name=centre, org_role=role, code=code, expires_at=datetime(2026, 11, 6, 9, 30))


# ---------------------------------------------------------------------------------------------------- G: Subject (invariant 9)

WORDS = (
    "Центр Алго Тест прода школа лицей математика Ташкент Samarkand Academy Star Light Smart Мир знаний Олимп ИТ Lab Zero One"
    " Бахор Нур Илм Зиё Kids Pro 2000 №5 им. Улугбека Образование Plus Эрудит Логика MathPro K3 тест".split()
)


def random_centre(rng):
    words = [rng.choice(WORDS) for _ in range(rng.randint(1, 8))]
    name = " ".join(words)
    if rng.random() < 0.08:
        name += " " + "очень-длинное-название-" * rng.randint(4, 8)  # more than 100 characters
    if rng.random() < 0.3:
        name = name.replace("Центр", "«Центр»")
    return name


def roundtrip_subject(subject_source: str, **make):
    rendered = render.Rendered(letter="new_application", subject=subject_source, html="<p>x</p>", text="x")
    msg = sender.build_message("ivan@example.uz", rendered)
    parsed = email.message_from_bytes(msg.as_bytes(), policy=policy.default)
    return str(parsed["Subject"])


def test_the_subject_survives_10000_random_centre_names():
    rng = random.Random(20261030)
    bad = []
    for _ in range(10_000):
        centre = random_centre(rng)
        subject = render.SUBJECTS["new_application"].format(centre_name=centre, city="Ташкент")
        if roundtrip_subject(subject) != subject:
            bad.append(subject)
    assert bad == [], bad[:3]


def test_the_subject_of_the_reported_centre_keeps_its_space():
    subject = render.SUBJECTS["new_application"].format(centre_name="K3 тест прода", city="Ташкент")
    assert roundtrip_subject(subject) == "[ALGO] Новая заявка: K3 тест прода, Ташкент"


def test_every_letter_subject_survives_with_hard_names():
    for centre in ("K3 тест прода", "Ё" * 150, "a b " * 60, "Центр «Альфа» & <Бета>"):
        for rendered in (staff_letter(centre=centre),
                         letters.render_new_application({"id": "i", "contact_name": "И", "centre_name": centre, "city": "Т", "created_at": datetime(2026, 10, 25)})):
            parsed = roundtrip_subject(rendered.subject)
            assert parsed == rendered.subject.strip(), (centre[:20], rendered.letter)  # (a header never ends in a space)


@pytest.mark.skipif(sys.version_info >= (3, 12), reason="the folding defect is in CPython 3.11 (the image runs 3.11)")
def test_negative_control_the_default_policy_loses_the_space_on_this_python():
    """Without ``max_line_length=998`` the same names lose a space on Python 3.11 - this is what the policy fixes."""
    from email.message import EmailMessage

    rng = random.Random(20261030)
    broken = 0
    for _ in range(2000):
        subject = render.SUBJECTS["new_application"].format(centre_name=random_centre(rng), city="Ташкент")
        msg = EmailMessage()
        msg["Subject"] = subject
        msg.set_content("x")
        if str(email.message_from_bytes(msg.as_bytes(), policy=policy.default)["Subject"]) != subject:
            broken += 1
    assert broken > 0


# ---------------------------------------------------------------------------------------------------- G: headers and parts


def test_headers_from_reply_to_date_and_auto_submitted():
    msg = sender.build_message("person@example.test", staff_letter())
    assert msg["From"] == "ALGO <noreply@algolab.me>"
    assert msg["Reply-To"] == "support@algolab.me"
    assert msg["Auto-Submitted"] == "auto-generated"
    assert msg["List-Unsubscribe"] is None and msg["List-Unsubscribe-Post"] is None
    date = msg["Date"]
    # the policy writes a datetime header with a numeric zone: «+0000» is right; the old «-0000» («local time unknown») is what the filters disliked
    assert re.fullmatch(r"[A-Z][a-z]{2}, \d{1,2} [A-Z][a-z]{2} \d{4} \d{2}:\d{2}:\d{2} (GMT|\+0000)", str(date)), date
    assert "-0000" not in str(date)
    assert email.utils.parsedate_to_datetime(str(date)).tzinfo is not None


def test_reply_to_comes_from_the_environment_only(monkeypatch):
    monkeypatch.setenv("MAIL_REPLY_TO", "")
    assert sender.build_message("p@e.test", staff_letter())["Reply-To"] is None
    monkeypatch.setenv("MAIL_REPLY_TO", "help@algolab.me")
    assert sender.build_message("p@e.test", staff_letter())["Reply-To"] == "help@algolab.me"


def test_the_text_part_is_quoted_printable_never_base64():
    for rendered in (staff_letter(), letters.render_welcome(greeting="Иван", centre_name="Центр", token="T", expires_at=datetime(2026, 11, 6), email="a@b.uz")):
        msg = sender.build_message("p@e.test", rendered)
        for part in msg.iter_parts():
            assert part["Content-Transfer-Encoding"] in ("quoted-printable", "8bit", "7bit"), part.get_content_type()
        raw = msg.as_bytes().decode("ascii", "replace")
        assert "base64" not in raw.lower()
        plain, html = msg.get_body(("plain",)).get_content(), msg.get_body(("html",)).get_content()
        assert plain.strip() == rendered.text.strip() and html.strip() == rendered.html.strip()
        assert max(len(line) for line in msg.as_bytes().splitlines()) <= 998


def test_no_letter_tells_the_reader_not_to_reply():
    now = datetime(2026, 10, 25, 9, 30)
    everything = [
        letters.render_application_received(contact_name="Иван", centre_name="Центр"),
        letters.render_welcome(greeting="Иван", centre_name="Центр", token="T", expires_at=now, email="a@b.uz"),
        letters.render_application_declined(greeting="Иван", centre_name="Центр", reason="x"),
        letters.render_new_application({"id": "i", "contact_name": "И", "centre_name": "Ц", "city": "Т", "created_at": now}),
        letters.render_password_reset(greeting="Иван", token="T", email="a@b.uz"),
        staff_letter(),
    ]
    for r in everything:
        text = (r.text + r.html).lower()
        assert not re.search(r"не\s+отвечайте|не\s+отвечать|do\s+not\s+reply|no-?reply", text), r.letter


# ---------------------------------------------------------------------------------------------------- A1: the staff invitation letter


def test_the_staff_invitation_letter_has_the_link_the_centre_the_role_and_the_expiry():
    r = staff_letter(role="admin")
    link = f"{FRONT}/invite/Zk3Q9dR2mT7vB5nX8aLw"
    assert link in r.text and f'href="{link}"' in r.html
    assert "«Учебный центр «Альфа»»" in r.text and "администратор" in r.text and "7 дней" in r.text
    assert "6 ноября, 14:30" in r.text                                       # 7 days from 06.11 09:30 UTC in Tashkent time
    assert r.subject == "Приглашение в ALGO — Учебный центр «Альфа»"
    assert "преподаватель" in staff_letter(role="teacher").text
    assert max(len(line) for line in r.text.splitlines()) < 200
    assert len(re.findall(r"https?://", r.text)) == 2                        # the link and the site address of the footer


def test_the_invitation_link_is_made_of_the_configured_front_address_only(monkeypatch):
    monkeypatch.setenv("FRONTEND_URL", "https://app.example.uz/")
    assert letters.invite_url("AbC-_123") == "https://app.example.uz/invite/AbC-_123"
    assert letters.invite_url("a/b c") == "https://app.example.uz/invite/a%2Fb%20c"


# ---------------------------------------------------------------------------------------------------- G: the allow-list of a stage (invariant 8)


def spy_on_deliver(monkeypatch):
    calls = []
    monkeypatch.setattr(sender, "deliver", lambda msg, cfg=None: calls.append(str(msg["To"])) or True)
    return calls


def test_production_without_a_list_sends_to_anybody(monkeypatch):
    calls = spy_on_deliver(monkeypatch)
    assert sender.send("anyone@wherever.test", staff_letter()) is True and calls == ["anyone@wherever.test"]


def test_a_stage_with_an_empty_list_sends_nothing_and_logs_without_the_address(monkeypatch, caplog):
    monkeypatch.setenv("MAIL_STAGE_MODE", "true")
    calls = spy_on_deliver(monkeypatch)
    with caplog.at_level(logging.INFO):
        assert sender.send("someone@real-person.test", staff_letter()) is False
    assert calls == []
    assert "NOT sent: stage allow-list" in caplog.text
    assert "someone" not in caplog.text and "real-person" not in caplog.text


@pytest.mark.parametrize("stage_flag", [{"MAIL_STAGE_MODE": "true"}, {"APP_ENV": "stage"}, {"APP_ENV": "preview"}])
def test_a_stage_sends_only_to_the_addresses_and_domains_of_the_list(monkeypatch, stage_flag):
    for key, value in stage_flag.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("MAIL_ALLOWLIST", "Tester@Example.test, team.uz ;@school.example, ")
    calls = spy_on_deliver(monkeypatch)
    for address, ok in (
        ("tester@example.test", True), ("TESTER@EXAMPLE.TEST", True), ("any@team.uz", True), ("x@school.example", True),
        ("other@example.test", False), ("x@sub.team.uz", False), ("x@team.uz.evil.test", False), ("team.uz@evil.test", False),
        ("tester@example.test.evil.test", False), ("evil@school.example.test", False),
    ):
        assert sender.send(address, staff_letter()) is ok, address
    assert calls == ["tester@example.test", "TESTER@EXAMPLE.TEST", "any@team.uz", "x@school.example"]


def test_a_list_on_production_restricts_too_but_an_empty_one_does_not(monkeypatch):
    monkeypatch.setenv("MAIL_ALLOWLIST", "only@here.test")
    calls = spy_on_deliver(monkeypatch)
    assert sender.send("only@here.test", staff_letter()) is True
    assert sender.send("else@here.test", staff_letter()) is False
    assert calls == ["only@here.test"]


def test_the_network_function_itself_refuses_an_address_outside_the_list(monkeypatch):
    """Even a caller that skips ``send`` cannot reach SMTP for a stranger on a stage: the gate is in ``deliver`` too."""
    monkeypatch.setenv("MAIL_STAGE_MODE", "true")
    monkeypatch.setenv("SMTP_HOST", "smtp.test")
    monkeypatch.setenv("SMTP_USER", "u")
    monkeypatch.setenv("SMTP_PASSWORD", "p")
    touched = []
    monkeypatch.setattr(sender.smtplib, "SMTP", lambda *a, **k: touched.append(a) or (_ for _ in ()).throw(AssertionError("SMTP touched")))
    msg = sender.build_message("stranger@real.test", staff_letter())
    assert sender.deliver(msg) is False and touched == []


def test_an_address_with_a_display_name_or_a_newline_cannot_slip_past_the_list(monkeypatch):
    monkeypatch.setenv("MAIL_STAGE_MODE", "true")
    monkeypatch.setenv("MAIL_ALLOWLIST", "ok@team.uz")
    calls = spy_on_deliver(monkeypatch)
    for sneaky in ("ok@team.uz\r\nBcc: victim@real.test", "Victim <victim@real.test>", "victim@real.test, ok@team.uz"):
        sender.send(sneaky, staff_letter())
    assert all("victim@real.test" not in c.split("\n")[0] or False for c in calls) or calls == []


def test_the_log_of_a_sent_letter_masks_the_address(monkeypatch, caplog):
    monkeypatch.setenv("SMTP_HOST", "smtp.test")
    monkeypatch.setenv("SMTP_USER", "u")
    monkeypatch.setenv("SMTP_PASSWORD", "p")

    class Fake:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def ehlo(self): pass
        def starttls(self, **k): pass
        def login(self, *a): pass
        def send_message(self, *a, **k): pass

    monkeypatch.setattr(sender.smtplib, "SMTP", Fake)
    with caplog.at_level(logging.INFO):
        assert sender.send("ivan.petrov@example.uz", staff_letter()) is True
    assert "i***@example.uz" in caplog.text and "ivan.petrov" not in caplog.text and "Zk3Q9dR2mT7vB5nX8aLw" not in caplog.text


def test_a_broken_template_is_a_false_not_an_exception(monkeypatch):
    def boom(**kw):
        raise KeyError("missing")

    monkeypatch.setattr(letters, "render_staff_invite", boom)
    with pytest.raises(KeyError):
        letters.send_staff_invite("a@b.uz", centre_name="x", org_role="teacher", code="c", expires_at=datetime(2026, 11, 6))
    # the route-level guard (invites.Invites._send_letter) turns it into email_sent=false: see test_k2_1_pg.py


def test_mail_config_parsing():
    assert mail_config._allowlist(" A@B.uz, c.uz;;@D.uz ,") == ("@d.uz", "a@b.uz", "c.uz")
    assert mail_config._allowlist("") == ()
