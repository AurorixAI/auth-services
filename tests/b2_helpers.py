"""LOGIC-B2 test helpers: the data set D2 (= D0 of package B + G4 "6-Г" with the teacher C and the student s7; rates for A, B, C - not for D), the ids of the B2
fixtures and ``golden`` (compare an answer with a fixture; the ids the server generates - requests, batches, one-off lessons - are learned from the answer)."""
from __future__ import annotations

import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List

from src.models.auth import GroupAttendance, LessonSubstitutionEvent, LessonSubstitutionRequest, PayrollRate

from tests.ops_helpers import (  # noqa: F401
    GEN, Ids, build_d0, client, clock, code_of, diff, exams, history_row, needs_pg, new_group, new_slot, new_student, pg_session, run_parallel,
)

FIX2 = Path(__file__).resolve().parent / "fixtures" / "logicb2"
API = "/api/v1/auth"
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class Ids2(Ids):
    def mapping(self) -> Dict[str, str]:
        out = super().mapping()
        out[GEN.uid("group-G4")] = str(self("G4"))
        out[GEN.uid("student-7")] = str(self("s7"))
        return out


def fx(name: str) -> Any:
    return json.loads((FIX2 / name).read_text(encoding="utf-8"))


def build_d2(db, salt=None):
    w = build_d0(db, salt)
    w.ids = Ids2(w.salt)
    w.G4 = new_group(db, w.org, w.ids, "G4", "6-Г", w.C, [new_slot("lesson-l5", 4, "16:00", "17:30", "2026-10-15")], class_level=6)
    w.s7 = new_student(db, w.org, w.ids, "s7", "Алиев", "Рустам", w.G4, display_id="AL06-001", class_level=6)
    for user in (w.A, w.B, w.C):
        db.add(PayrollRate(org_id=w.org.id, teacher_id=user.id, lesson_type="group", percent=30, valid_from=date(2026, 9, 1)))
    db.commit()
    return w


def learn(actual: Any, expected: Any, mapping: Dict[str, str], known: set, path: str = "") -> List[str]:
    """Parallel walk: wherever the fixture holds an id the test world does not know (a request, a batch, a one-off lesson) and the answer holds one, remember the pair;
    a pair seen twice must agree.  Returns the problems."""
    out: List[str] = []
    if isinstance(expected, dict) and isinstance(actual, dict):
        for k in expected:
            if k in actual:
                out += learn(actual[k], expected[k], mapping, known, f"{path}/{k}")
    elif isinstance(expected, list) and isinstance(actual, list):
        for i, (a, e) in enumerate(zip(actual, expected)):
            out += learn(a, e, mapping, known, f"{path}[{i}]")
    elif isinstance(expected, str) and isinstance(actual, str):
        free = (UUID_RE.match(expected) and expected not in known) or (expected.startswith("one-off-") and expected != actual)
        if free:
            if mapping.setdefault(expected, actual) != actual:
                out.append(f"{path}: id {expected} is {mapping[expected]} elsewhere but {actual} here")
    return out


def sub(expected: Any, mapping: Dict[str, str]) -> Any:
    if isinstance(expected, str):
        return mapping.get(expected, expected)
    if isinstance(expected, list):
        return [sub(x, mapping) for x in expected]
    if isinstance(expected, dict):
        return {sub(k, mapping): sub(v, mapping) for k, v in expected.items()}
    return expected


class Golden:
    """One per test: ``g(w, actual, "b2c1_create.json")`` -> problems (empty = equal)."""

    def __init__(self) -> None:
        self.mapping: Dict[str, str] = {}

    def __call__(self, w, actual: Any, name: str, select=None) -> List[str]:
        expected = fx(name)
        if select:
            expected = select(expected)
        expected = w.ids.remap(expected)
        known = set(w.ids.mapping().values())
        problems = learn(actual, expected, self.mapping, known)
        return problems + diff(actual, sub(expected, self.mapping))


def requests_of(w, status=None):
    w.db.expire_all()
    q = w.db.query(LessonSubstitutionRequest).filter_by(org_id=w.org.id).order_by(LessonSubstitutionRequest.created_at, LessonSubstitutionRequest.id)
    if status:
        q = q.filter_by(status=status)
    return q.all()


def events_of(w, request_id=None):
    w.db.expire_all()
    q = w.db.query(LessonSubstitutionEvent).filter_by(org_id=w.org.id).order_by(LessonSubstitutionEvent.created_at, LessonSubstitutionEvent.id)
    if request_id:
        q = q.filter_by(request_id=request_id)
    return q.all()


def req(w, who, lessons, reason="sick", comment=None, suggested=None):
    """POST /org/substitutions; ``lessons`` = [(group, lessonId, 'YYYY-MM-DD')]."""
    body = {"lessons": [{"groupId": str(g.id), "lessonId": lid, "lessonDate": d} for g, lid, d in lessons], "reasonCode": reason}
    if comment:
        body["comment"] = comment
    if suggested is not None:
        body["suggestedTeacherId"] = str(getattr(w, suggested).id)
    return client(w, who).post(f"{API}/org/substitutions", json=body)


def decide(w, who, request_id, **body):
    return client(w, who).post(f"{API}/org/substitutions/{request_id}/decide", json=body)


def row_of(w, group, lesson, day):
    w.db.expire_all()
    return w.db.query(GroupAttendance).filter_by(group_id=group.id, lesson_id=lesson, lesson_date=day).first()
