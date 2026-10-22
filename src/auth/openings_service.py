"""OPENING-BULK service (skeleton of the frozen contract; the implementation follows)."""
from __future__ import annotations

MAX_IMPORT_BYTES = 5 * 1024 * 1024

from .payments_service import PaymentError


class NotImplementedYet(PaymentError):
    status_code = 501
    code = "NOT_IMPLEMENTED"


class OpeningsService:
    def __init__(self, db) -> None:
        self.db = db

    def _todo(self, *a, **k):
        raise NotImplementedYet("OPENING-BULK: not implemented yet")

    list = summary = preview = apply = set_state = export = import_preview = _todo
