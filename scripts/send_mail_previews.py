#!/usr/bin/env python3
"""Send every letter of the platform with sample data, to look at them in a real mailbox:

    python -m scripts.send_mail_previews --to owner@example.com        # needs SMTP_* in the environment
    python -m scripts.send_mail_previews --save /tmp/letters            # no sending: writes <letter>.html / .txt

Nothing real is touched: the names, the centre and the links are made up (the links open nothing). One letter is sent per kind, subjects get
a «[пример]» mark so that they are never mistaken for the real ones.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def samples() -> list:
    from src.mail import letters

    now = datetime.utcnow().replace(microsecond=0)
    request = {
        "id": "00000000-0000-4000-8000-000000000000", "contact_name": "Иванов Иван Иванович", "centre_name": "Учебный центр «Альфа»",
        "city": "Ташкент", "phone": "+998901234567", "email": "ivan@example.uz", "students_count": 120,
        "comment": "Хотим попробовать ALGO для двух филиалов.\nУдобно созвониться после 15:00.",
        "source": {"utm_source": "telegram", "utm_medium": "post", "utm_campaign": "october"}, "created_at": now, "is_repeat": False,
    }
    return [
        letters.render_application_received(contact_name="Иван Иванович", centre_name="Учебный центр «Альфа»"),
        letters.render_welcome(
            greeting="Иван Иванович", centre_name="Учебный центр «Альфа»", token="PREVIEW-TOKEN-DOES-NOT-OPEN-ANYTHING",
            expires_at=now + timedelta(hours=72), email="ivan@example.uz",
        ),
        letters.render_application_declined(
            greeting="Иван Иванович", centre_name="Учебный центр «Альфа»",
            reason="Сейчас мы запускаем платформу только в учебных центрах с группами от 50 учеников.",
        ),
        letters.render_new_application(request),
        letters.render_password_reset(greeting="Иван Иванович", token="PREVIEW-TOKEN-DOES-NOT-OPEN-ANYTHING", email="ivan@example.uz"),
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description="Send (or save) sample letters of the ALGO platform")
    parser.add_argument("--to", help="the address that receives all sample letters")
    parser.add_argument("--save", help="a folder to write the letters into instead of sending")
    args = parser.parse_args()
    if not args.to and not args.save:
        parser.error("give --to <address> or --save <folder>")

    from dataclasses import replace

    from src.mail import sender

    rendered = [replace(r, subject=f"[пример] {r.subject}") for r in samples()]
    if args.save:
        folder = Path(args.save)
        folder.mkdir(parents=True, exist_ok=True)
        for r in rendered:
            (folder / f"{r.letter}.html").write_text(r.html, encoding="utf-8")
            (folder / f"{r.letter}.txt").write_text(r.text, encoding="utf-8")
        print(f"Сохранено писем: {len(rendered)} в {folder}")
        return 0
    sent = sum(1 for r in rendered if sender.send(args.to, r))
    print(f"Отправлено писем: {sent} из {len(rendered)} на {args.to}")
    return 0 if sent == len(rendered) else 1


if __name__ == "__main__":
    raise SystemExit(main())
