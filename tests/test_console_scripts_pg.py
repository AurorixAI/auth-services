"""The two scripts of the pack: ``create_platform_owner`` (interactive, prints one line) and ``send_mail_previews`` (every letter, sample data)."""
import getpass
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from tests.console_helpers import API, PASSWORD, audit_rows, cw, needs_pg  # noqa: F401  (first: it loads the application)
from src.auth.platform_bootstrap import OwnerError, create_owner  # noqa: E402
from src.models.auth import User  # noqa: E402
from src.models.platform import PlatformStaff  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def run_script(monkeypatch, capsys, email, first, last, passwords):
    from scripts import create_platform_owner as script

    answers = iter([email, first, last])
    secrets = iter(passwords)
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": next(secrets))
    code = script.main()
    out = capsys.readouterr()
    return code, out.out, out.err


@needs_pg
def test_the_script_creates_an_owner_who_can_sign_in_and_prints_one_line(cw, monkeypatch, capsys):
    email = f"boss-{uuid4().hex[:8]}@cn1.test"
    secret = "my-very-long-secret-phrase"
    code, out, err = run_script(monkeypatch, capsys, email.upper(), "Арслан", "Д.", [secret, secret])
    assert code == 0 and out == f"Создан владелец консоли {email}\n" and err == ""
    assert secret not in out + err
    c = cw.new_client()
    r = c.post(f"{API}/auth/login", json={"email": email, "password": secret})
    assert r.status_code == 200 and r.json()["is_owner"] is True and r.json()["must_change_password"] is False
    assert r.json()["role"]["name"] == "Владелец платформы" and len(r.json()["permissions"]) == 17
    cw.db.expire_all()
    user = cw.db.query(User).filter_by(email=email).one()
    assert user.password_hash != secret and (user.first_name, user.last_name) == ("Арслан", "Д.")
    assert audit_rows(cw.db, "console.login.success", actor_user_id=user.id)


@needs_pg
def test_the_script_refuses_a_known_email_a_short_password_and_a_mismatch(cw, monkeypatch, capsys):
    secret = "my-very-long-secret-phrase"
    code, out, err = run_script(monkeypatch, capsys, cw.owner.email, "А", "Б", [secret, secret])
    assert code == 1 and out == "" and "уже используется" in err
    cabinet = cw.make_centre()
    code, out, err = run_script(monkeypatch, capsys, cabinet.owner.email, "А", "Б", [secret, secret])
    assert code == 1 and "уже используется" in err
    code, out, err = run_script(monkeypatch, capsys, f"x-{uuid4().hex[:6]}@cn1.test", "А", "Б", ["short", "short"])
    assert code == 1 and out == "" and "12" in err
    code, out, err = run_script(monkeypatch, capsys, f"y-{uuid4().hex[:6]}@cn1.test", "А", "Б", [secret, secret + "!"])
    assert code == 1 and out == "" and "не совпадают" in err
    code, out, err = run_script(monkeypatch, capsys, "not-an-email", "А", "Б", [secret, secret])
    assert code == 1 and "почту" in err
    assert secret not in out + err


@needs_pg
def test_create_owner_twice_for_one_email_is_a_clear_error_not_a_crash(cw):
    email = f"twice-{uuid4().hex[:8]}@cn1.test"
    create_owner(cw.db, email, PASSWORD)
    with pytest.raises(OwnerError, match="уже используется"):
        create_owner(cw.db, email.upper(), PASSWORD)
    assert cw.db.query(PlatformStaff).join(User, User.id == PlatformStaff.user_id).filter(User.email == email).count() == 1


def test_the_preview_script_saves_every_letter(tmp_path, capsys, monkeypatch):
    from scripts import send_mail_previews as script

    monkeypatch.setattr(sys, "argv", ["send_mail_previews", "--save", str(tmp_path)])
    assert script.main() == 0
    assert "Сохранено писем: 5" in capsys.readouterr().out
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(f"{n}.{ext}" for n in
        ("application_received", "welcome", "application_declined", "new_application", "password_reset") for ext in ("html", "txt"))


def test_the_preview_script_sends_five_marked_letters_to_the_address(monkeypatch, capsys):
    from scripts import send_mail_previews as script
    from src.mail import sender

    sent = []
    monkeypatch.setattr(sender, "deliver", lambda msg, cfg=None: sent.append(msg) or True)
    monkeypatch.setattr(sys, "argv", ["send_mail_previews", "--to", "owner@example.test"])
    assert script.main() == 0 and "Отправлено писем: 5 из 5" in capsys.readouterr().out
    assert len(sent) == 5 and all(m["To"] == "owner@example.test" and m["Subject"].startswith("[пример] ") for m in sent)
    assert all(m.get_content_type() == "multipart/alternative" for m in sent)


def test_the_preview_script_needs_an_address_or_a_folder(monkeypatch):
    from scripts import send_mail_previews as script

    monkeypatch.setattr(sys, "argv", ["send_mail_previews"])
    with pytest.raises(SystemExit) as stop:
        script.main()
    assert stop.value.code == 2
