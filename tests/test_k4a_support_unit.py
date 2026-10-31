"""K4a «Поддержка»: the letters (no text of the answer, no Reply-To for the author, the words of the author defanged and escaped) and the pure rules - without a database."""
import re
from datetime import datetime

from src.auth import support_rules as rules
from src.mail import letters, render, sender

EVIL = '<img src=x onerror=alert(1)> https://evil.example/x www.evil.example <script>alert(2)</script>'


def team_letter(kind="support_new", text=EVIL, centre="Центр «Альфа»"):
    fn = letters.render_support_new if kind == "support_new" else letters.render_support_author_reply
    return fn(ticket_id="7f0c1e2a-9b1d-4c33-8a55-0d6f3b2a1c9e", number=17, centre_name=centre, author_role="teacher", text=text)


def test_the_letter_to_the_team_has_the_number_the_centre_the_role_300_characters_and_the_link_to_the_card(monkeypatch):
    monkeypatch.setenv("CONSOLE_URL", "https://console.test")
    long = "слово " * 100
    for kind in ("support_new", "support_author_reply"):
        r = team_letter(kind, long)
        assert "№17" in r.subject and "Центр «Альфа»" in r.subject
        for part in (r.text, r.html):
            assert "№17" in part and "Центр «Альфа»" in part and "преподаватель" in part
            assert "https://console.test/support?id=7f0c1e2a-9b1d-4c33-8a55-0d6f3b2a1c9e" in part
        assert r.text.count("слово") <= 51 and "…" in r.text                                       # the first 300 characters only
        assert "Открыть в консоли" in r.html


def test_what_the_author_wrote_is_escaped_in_html_and_defanged_in_both_parts():
    for kind in ("support_new", "support_author_reply"):
        r = team_letter(kind)
        assert "<img src=x" not in r.html and "<script" not in r.html and "&lt;img" in r.html
        assert "https://evil.example" not in r.text and "https://evil.example" not in r.html                   # no mail client makes a link of it
        assert "https[:]//evil" in r.text and "www[.]evil" in r.text


def test_the_letter_to_the_author_says_there_is_an_answer_and_carries_neither_the_answer_nor_a_token(monkeypatch):
    monkeypatch.setenv("FRONTEND_URL", "https://app.test")
    monkeypatch.setenv("MAIL_REPLY_TO", "hello@example.test")
    r = letters.render_support_reply(greeting="Дилноза", number=17, org_role="admin")
    assert r.subject == "Есть ответ на обращение №17 — ALGO"
    for part in (r.text, r.html):
        assert "Дилноза" in part and "№17" in part and "Ответить можно только в кабинете ALGO" in part and "https://app.test/admin/support" in part
        assert "set-password" not in part and "token" not in part.lower()
    assert re.findall(r"https?://[^\s\"'<>]+", r.text) == ["https://app.test/admin/support", "https://algolab.me"]
    msg = sender.build_message("person@example.test", r)
    assert msg["Reply-To"] is None and msg["To"] == "person@example.test"                                      # an answer to this letter would land in the Zoho box
    assert sender.build_message("support@example.test", team_letter())["Reply-To"] == "hello@example.test"      # the letters to the team keep it


def test_the_role_decides_the_cabinet_page_and_an_unknown_one_falls_back_to_the_teachers(monkeypatch):
    monkeypatch.setenv("FRONTEND_URL", "https://app.test")
    assert [letters.cabinet_support_url(r) for r in ("owner", "admin", "teacher", "superadmin")] == [
        "https://app.test/owner/support", "https://app.test/admin/support", "https://app.test/teacher/support", "https://app.test/teacher/support"]


def test_the_three_letters_are_registered_and_have_both_parts():
    for name in ("support_new", "support_author_reply", "support_reply"):
        assert name in render.SUBJECTS and name in render.LETTERS
    assert render.TEMPLATE_OF == {"support_new": "support_team", "support_author_reply": "support_team"}


def test_the_limits_are_the_numbers_of_the_spec():
    assert (rules.TEXT_MIN, rules.TEXT_MAX, rules.MAX_ATTACHMENTS, rules.MAX_ATTACHMENT_BYTES) == (10, 2000, 3, 2 * 1024 * 1024)
    assert (rules.TICKETS_PER_AUTHOR_DAY, rules.TICKETS_PER_ORG_DAY, rules.AUTHOR_REPLIES_PER_TICKET_DAY) == (5, 20, 30)
    assert (rules.WAITING_AUTO_RESOLVE_DAYS, rules.REOPEN_DAYS) == (14, 14)
    assert (rules.TEAM_MAIL_PER_DAY, rules.AUTHOR_MAIL_PER_DAY, rules.TEAM_MAIL_PER_TICKET_SECONDS, rules.AUTHOR_MAIL_PER_TICKET_SECONDS) == (30, 50, 3600, 1800)
    assert (rules.AUTHOR_REPLIES_PER_AUTHOR_DAY, rules.AUTHOR_REPLIES_PER_ORG_DAY) == (30, 100)
    from src.auth import password_reset

    assert len({rules.TEAM_MAIL_KEY, rules.AUTHOR_MAIL_KEY, "rl:forgot_platform_day"}) == 3 and password_reset.PER_PLATFORM_DAY == 150          # three budgets, three counters


def test_the_png_and_jpeg_signatures_and_nothing_else():
    assert rules.sniff_image(b"\x89PNG\r\n\x1a\n" + b"0") == "image/png" and rules.sniff_image(b"\xff\xd8\xff\xe0" + b"0") == "image/jpeg"
    for junk in (b"", b"%PDF-1.4", b"<svg/>", b"GIF89a", b"RIFF....WEBPVP8 ", b"\x89PNG", b"  \x89PNG\r\n\x1a\n"):
        assert rules.sniff_image(junk) is None, junk


def test_the_download_headers():
    h = rules.download_headers("image/png", 17, 2)
    assert h["Content-Disposition"] == 'attachment; filename="support-17-2.png"' and h["X-Content-Type-Options"] == "nosniff"
    assert h["Cache-Control"] == "no-store" and h["Content-Security-Policy"] == "sandbox"
    assert rules.download_headers("image/jpeg", 1, 1)["Content-Disposition"].endswith('.jpg"')


def test_the_browser_is_cut_to_200_and_has_no_control_characters():
    assert rules.browser_of("A" * 500) == "A" * 200 and rules.browser_of(None) == "" and rules.browser_of("a\x00b\r\nc") == "abc"


# ---- R3-4: the list of the token words is ONE list on three sides ----------------------------------------------------------------------
# Change TOGETHER with algo-front-p154/src/lib/public-pages.ts (TOKEN_SEGMENTS) and the masks of nginx (algo-infrastructure/tests/test_k4a_token_masks.py): LESSONS 77.
TOKEN_WORDS_MASKED_BY_NGINX = {"invite", "set-password", "confirm-email", "d", "e", "diagnostic", "password-links", "email-change", "by-code"}
TOKEN_WORDS_OF_THE_SUPPORT_ONLY = {"session-status", "support-login", "reset"}


def test_the_token_prefixes_are_the_agreed_list_and_each_one_masks_its_next_segment():
    assert rules._TOKEN_PREFIXES == TOKEN_WORDS_MASKED_BY_NGINX | TOKEN_WORDS_OF_THE_SUPPORT_ONLY
    for word in sorted(rules._TOKEN_PREFIXES):
        assert rules.page_template(f"/{word}/onlylowercase") == f"/{word}/[id]", word
        assert rules.page_template(f"/api/v1/auth/{word}/abc/more") == f"/api/v1/auth/{word}/[id]/more", word
    assert rules.page_template("/api/v1/auth/password-links/abc") == "/api/v1/auth/password-links/[id]"
    assert rules.page_template("/api/v1/auth/email-change/abc") == "/api/v1/auth/email-change/[id]"


def test_a_segment_is_a_word_only_as_a_whole_the_trailing_newline_is_not_a_word():
    assert rules.page_template("/teacher/word\n/x") == "/teacher/[id]/x"
    assert rules.page_template("/teacher/word\n\n/a/b") == "/teacher/[id]/a/b"
    assert rules.page_template("/teacher/word") == "/teacher/word"


def test_the_size_of_a_picture_is_read_from_its_header():
    import struct, zlib

    def png(w, h):
        body = struct.pack(">II", w, h) + b"\x08\x02\x00\x00\x00"
        return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + body + struct.pack(">I", zlib.crc32(b"IHDR" + body))

    assert rules.image_size(png(640, 480), "image/png") == (640, 480)
    assert rules.image_size(b"\x89PNG\r\n\x1a\n" + b"\x00" * 30, "image/png") is None
    jpeg = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xc0\x00\x11\x08\x04\x00\x06\x00\x03\x01\x22\x00\x02\x11\x01\x03\x11\x01\xff\xd9"
    assert rules.image_size(jpeg, "image/jpeg") == (1536, 1024)
    assert rules.image_size(jpeg[:14], "image/jpeg") is None and rules.image_size(b"\xff\xd8", "image/jpeg") is None
    assert rules.image_size(b"\xff\xd8" + b"\xff" * 5000, "image/jpeg") is None                       # a header made of padding does not loop forever
