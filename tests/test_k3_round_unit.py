"""Round after R3 / security review (no database): the log of the mail module keeps no address, the token of step 3 survives odd digits,
the canonical mailbox, the start-up warning about the hostnames of Turnstile."""
import smtplib
from uuid import uuid4

import pytest

from tests.console_helpers import needs_pg  # noqa: F401  (first: the import order of the application)
from src.auth import demo_requests as service
from src.auth.platform_errors import PlatformError
from src.core import startup_guard as guard
from src.mail import letters, sender

ADDRESS = "john.doe@gmail.com"


class FakeSMTP:
    """A server that refuses the recipient and puts the address into the text of its error, as smtplib does."""

    def __init__(self, host, port, timeout=None, **kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def ehlo(self):
        pass

    def starttls(self, context=None):
        pass

    def login(self, user, password):
        pass

    def send_message(self, msg, from_addr=None, to_addrs=None):
        raise smtplib.SMTPRecipientsRefused({to_addrs[0]: (550, f"mailbox {to_addrs[0]} unavailable".encode())})


@pytest.fixture
def smtp(monkeypatch):
    for key, value in (("SMTP_HOST", "smtp.example.test"), ("SMTP_PORT", "587"), ("SMTP_USER", "u"), ("SMTP_PASSWORD", "p")):
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)


def received():
    return letters.render_application_received(contact_name="Иван Секретов", centre_name="Центр Секретов")


def assert_clean(caplog):
    assert ADDRESS not in caplog.text and "john.doe" not in caplog.text and "Секретов" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)                         # a trace of smtplib carries the address in its text


def test_r3_a_failed_letter_logs_a_masked_address_the_kind_and_the_class_of_the_error(smtp, caplog):
    with caplog.at_level("DEBUG"):
        assert sender.send(ADDRESS, received()) is False
    assert "j***@gmail.com" in caplog.text and "SMTPRecipientsRefused" in caplog.text and "application_received" in caplog.text
    assert_clean(caplog)


def test_r3_a_sent_letter_and_an_unconfigured_smtp_log_no_address_either(smtp, monkeypatch, caplog):
    monkeypatch.setattr(smtplib, "SMTP", lambda *a, **k: type("Ok", (FakeSMTP,), {"send_message": lambda self, *a, **k: None})(*a, **k))
    with caplog.at_level("DEBUG"):
        assert sender.send(ADDRESS, received()) is True
    assert "j***@gmail.com" in caplog.text and "application_received" in caplog.text
    assert_clean(caplog)
    caplog.clear()
    monkeypatch.delenv("SMTP_HOST")
    with caplog.at_level("DEBUG"):
        assert sender.send(ADDRESS, received()) is False
    assert "j***@gmail.com" in caplog.text and "not configured" in caplog.text
    assert_clean(caplog)


def test_r3_a_crash_while_building_the_letter_logs_no_address(monkeypatch, caplog):
    def boom(to, rendered, cfg=None):
        raise ValueError(f"bad header for {to}")

    monkeypatch.setattr(sender, "build_message", boom)
    with caplog.at_level("DEBUG"):
        assert sender.send(ADDRESS, received()) is False
    assert "ValueError" in caplog.text
    assert_clean(caplog)


@pytest.mark.parametrize("address, masked", [("a@x.test", "a***@x.test"), ("john.doe@gmail.com", "j***@gmail.com"), ("", "***"), ("no-at-sign", "n***")])
def test_r3_the_mask_keeps_the_first_letter_and_the_domain(address, masked):
    assert sender.mask_address(address) == masked


@pytest.mark.parametrize("digits", ["²", "①", "１２", "٣", "1²", "", "-5", "1" * 13, " 12"])
def test_r4_a_token_with_a_strange_digit_is_403_not_a_crash(digits):
    uid = uuid4()
    token = f"{uid.hex}.{digits}.{'0' * 64}"
    with pytest.raises(PlatformError) as caught:
        service.check_details_token(token, uid)
    assert caught.value.status_code == 403 and caught.value.code == "invalid_details_token"


@pytest.mark.parametrize(
    "email, canonical",
    [
        ("John.Doe@Gmail.com", "johndoe@gmail.com"), ("j.o.h.n+tag@googlemail.com", "john@gmail.com"), ("a.b+c@mail.test", "a.b@mail.test"),
        ("  X@Y.test ", "x@y.test"), ("+only@mail.test", "+only@mail.test"), ("nodomain", "nodomain"),
    ],
)
def test_r6_the_canonical_mailbox(email, canonical):
    assert service.canonical_mailbox(email) == canonical


def test_r9_a_key_without_a_list_of_hostnames_is_a_warning_on_a_deployed_stack(caplog):
    env = {"APP_ENV": "staging", "FRONTEND_URL": "https://app.stage.algolab.me", "SITE_URL": "https://stage.algolab.me", "TURNSTILE_SECRET_KEY": "k"}
    with caplog.at_level("WARNING"):
        guard.check_public_urls(env)
    assert "TURNSTILE_HOSTNAMES не задан" in caplog.text
    caplog.clear()
    with caplog.at_level("WARNING"):
        guard.check_public_urls({**env, "TURNSTILE_HOSTNAMES": "stage.algolab.me"})
    assert caplog.text == ""
