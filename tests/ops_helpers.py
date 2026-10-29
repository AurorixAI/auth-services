"""Helpers of the LOGIC-B (operations) tests: the clock, the data set D0 of the SPEC chains, the fixtures with their ids, the exam-service stub.

Runs only on the disposable ``a2_auth`` database (see ``tests/pg_world.py``).  Every test gets its own organisation and its own ids (a salt),
so tests never see each other's rows; the golden fixtures (``tests/fixtures/logicb``) carry the ids of the generator and ``Ids.remap`` turns
them into the ids of the test.
"""
from __future__ import annotations

import importlib.util
import json
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest
from fastapi.testclient import TestClient

from src.auth import business_clock as bc
from src.auth.membership_periods import open_period
from src.auth.service import BUSINESS_TIMEZONE, AuthService
from src.models.auth import (
    Group,
    GroupAttendance,
    GroupAudit,
    Organization,
    OrganizationMember,
    Student,
    StudentGroup,
    User,
)

from tests.pg_world import make_client, needs_pg, pg_session  # noqa: F401  (fixtures)

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "logicb"
_spec = importlib.util.spec_from_file_location("logicb_gen_fixtures", ROOT / "scripts" / "logicb_gen_fixtures.py")
GEN = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(GEN)

NS = GEN.NS
TASHKENT = BUSINESS_TIMEZONE
REAL_SYNC_CANCELLATION = AuthService._sync_exam_lesson_cancellation   # the real one (a fixture below replaces it for most tests)


# ================================================================================================== the clock
class Clock:
    """``clock.set("2026-10-21 14:00")`` (Tashkent wall time) moves the business clock AND the ``datetime.now`` of the service."""

    def __init__(self, monkeypatch, local: str) -> None:
        from src.auth import payment_status, service

        self.state = {"now": self._parse(local)}
        state = self.state

        class FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                moment = state["now"]
                return moment.astimezone(tz) if tz is not None else moment.replace(tzinfo=None)

            @classmethod
            def utcnow(cls):
                return state["now"].astimezone(timezone.utc).replace(tzinfo=None)

        bc.set_clock(lambda: state["now"])
        monkeypatch.setattr(service, "datetime", FrozenDatetime)
        monkeypatch.setattr(payment_status, "datetime", FrozenDatetime)

    @staticmethod
    def _parse(local: str) -> datetime:
        if len(local) == 10:
            local += " 12:00"
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                return datetime.strptime(local, fmt).replace(tzinfo=TASHKENT)
            except ValueError:
                continue
        raise ValueError(local)

    def set(self, local: str) -> None:
        self.state["now"] = self._parse(local)

    @property
    def utc(self) -> datetime:
        return self.state["now"].astimezone(timezone.utc).replace(tzinfo=None)

    @property
    def today(self) -> date:
        return self.state["now"].date()


@pytest.fixture
def clock(monkeypatch):
    c = Clock(monkeypatch, "2026-10-21 14:00")
    # cancelling a lesson asks exam-service; out of scope here (a test of that overrides it)
    monkeypatch.setattr(AuthService, "_sync_exam_lesson_cancellation", lambda self, **kw: None)
    yield c
    bc.set_clock(None)


# ============================================================================================== ids + fixtures
class Ids:
    """Test ids derived from a salt; ``remap`` replaces the ids of the generator inside a fixture."""

    def __init__(self, salt: str) -> None:
        self.salt = salt
        self.ns = uuid.uuid5(NS, "salt:" + salt)

    def __call__(self, name: str) -> uuid.UUID:
        return uuid.uuid5(self.ns, name)

    def mapping(self) -> Dict[str, str]:
        names = ["org", "G1", "G2", "G3", "G5", "member_A", "owner", "admin", "A", "B", "C", "D"] + [f"s{i}" for i in range(1, 7)]
        out = {GEN.IDS[n]: str(self(n)) for n in names}
        out[GEN.uid("group-6D")] = str(self("group-6D"))
        out[GEN.uid("invitation-A")] = str(self("invitation-A"))
        out[GEN.uid("sheet-A-2026-10")] = str(self("sheet-A-2026-10"))
        return out

    def remap(self, obj: Any) -> Any:
        mapping = self.mapping()
        if isinstance(obj, str):
            return mapping.get(obj, obj)
        if isinstance(obj, list):
            return [self.remap(x) for x in obj]
        if isinstance(obj, dict):
            return {self.remap(k): self.remap(v) for k, v in obj.items()}
        return obj


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# ================================================================================================ exam-service
class ExamStub:
    """Replaces ``exam_bridge._post``: ``exams`` = what exam-service reports for a lesson / group; ``.calls`` = what auth sent."""

    def __init__(self, monkeypatch) -> None:
        from src.auth import exam_bridge

        self.bridge = exam_bridge
        self.calls: List[tuple] = []
        self.completion_exams: List[dict] = []     # answer of completion-check
        self.completed = {"released": [], "leftRunning": []}
        self.group_exams: List[dict] = []          # answer of archive-impact
        self.release_conflict = False
        self.unavailable = False
        monkeypatch.setattr(exam_bridge, "_post", self._post)

    def _post(self, path: str, payload: dict) -> dict:
        self.calls.append((path, payload))
        if self.unavailable:
            raise self.bridge.ExamBridgeUnavailable("down")
        if path.endswith("/completion-check"):
            return {"ok": True, "exams": self.completion_exams}
        if path.endswith("/completed"):
            return {"ok": True, **self.completed}
        if path.endswith("/archive-impact"):
            return {"exams": self.group_exams}
        if path.endswith("/archive-release"):
            if self.release_conflict:
                raise self.bridge.ExamBridgeConflict("blocked")
            if getattr(self, "release_unavailable", False):
                raise self.bridge.ExamBridgeUnavailable("down at release")
            return {"ok": True, "releasedExamIds": [e["examId"] for e in self.group_exams if e.get("canRelease", True)]}
        raise AssertionError(path)


@pytest.fixture
def exams(monkeypatch):
    return ExamStub(monkeypatch)


# ======================================================================================= the data set D0 (SPEC 2)
def _user(db, key: str, ids: Ids, role: str, email_salt: str) -> User:
    first, last = GEN.PEOPLE[key]
    user = User(id=ids(key), email=f"{key.lower()}-{email_salt}@algo.test", password_hash="synthetic-only", first_name=first,
                last_name=last, role=role)
    db.add(user)
    db.flush()
    return user


def new_group(db, org, ids: Ids, key: str, name: str, teacher: Optional[User], slots: List[dict], *, max_students: int = 30,
              class_level: int = 9, created: datetime = datetime(2026, 9, 1, 0, 0), audit_at: Optional[datetime] = None) -> Group:
    group = Group(id=ids(key), org_id=org.id, name=name, class_level=class_level, teacher_id=teacher.id if teacher else None,
                  max_students=max_students, schedule=slots, created_at=created, lesson_type="group")
    db.add(group)
    db.flush()
    db.add(GroupAudit(org_id=org.id, group_id=group.id, actor_id=ids("owner"), actor_role="owner", action="created",
                      assigned_teacher_id=teacher.id if teacher else None,
                      snapshot={"name": name, "max_students": max_students, "lesson_type": "group", "status": "active"},
                      created_at=audit_at or datetime(2026, 9, 1, 3, 0)))
    return group


def new_slot(lesson_id: str, dow: int, start: str, end: str, valid_from: str, *, room: Optional[str] = None, fmt: str = "offline") -> dict:
    return {"id": lesson_id, "dayOfWeek": dow, "startTime": start, "endTime": end, "subject": "Математика", "room": room, "format": fmt,
            "color": "blue", "topic": None, "validFrom": valid_from}


def new_student(db, org, ids: Ids, key: str, last: str, first: str, group: Optional[Group], *, display_id: Optional[str] = None,
                class_level: int = 9, phone: Optional[str] = None, joined: datetime = datetime(2026, 9, 1, 5, 0), status: str = "active",
                seq: Optional[int] = None) -> Student:
    student = Student(id=ids(key), org_id=org.id, first_name=first, last_name=last, class_level=class_level, status=status,
                      group_id=group.id if group else None, display_id=display_id, student_phone=phone, sequence_number=seq,
                      created_at=datetime(2026, 9, 1, 3, 0))
    db.add(student)
    db.flush()
    if group is not None:
        membership = StudentGroup(student_id=student.id, group_id=group.id)
        open_period(membership, joined)
        db.add(membership)
        db.flush()
    return student


def roster_of(students: List[Student]) -> List[dict]:
    return [{"student_id": str(s.id), "first_name": s.first_name, "last_name": s.last_name, "full_name": f"{s.last_name} {s.first_name}",
             "class_level": s.class_level, "display_id": s.display_id} for s in students]


def history_row(db, org, group: Group, lesson_id: str, day: str, teacher: Optional[User], students: List[Student], *,
                status: str = "completed", start: Optional[str] = None, end: Optional[str] = None, marked_by: Optional[User] = None,
                version: int = 1, records: Optional[Dict[str, str]] = None) -> GroupAttendance:
    """A finished (or open) lesson of the past, with the roster frozen."""
    d = date.fromisoformat(day)
    started = datetime.combine(d, datetime.strptime(start, "%H:%M").time()) if start else None
    completed = datetime.combine(d, datetime.strptime(end, "%H:%M").time()) if (end and status == "completed") else None
    row = GroupAttendance(org_id=org.id, group_id=group.id, teacher_id=teacher.id if teacher else None, lesson_date=day, lesson_id=lesson_id,
                          status=status, records=records if records is not None else {str(s.id): "present" for s in students},
                          roster_snapshot=roster_of(students), marked_by=(marked_by or teacher).id if (marked_by or teacher) else None,
                          started_at=started, completed_at=completed, version=version,
                          created_at=(started or datetime.combine(d, datetime.min.time())) - timedelta(hours=5))
    db.add(row)
    db.flush()
    return row


def build_org(db, salt: Optional[str] = None) -> SimpleNamespace:
    """An organisation with its staff (owner, admin, teachers A B C D) and a second, foreign organisation; no groups yet."""
    salt = salt or uuid.uuid4().hex[:8]
    ids = Ids(salt)
    org = Organization(id=ids("org"), name="Алго Тест", status="active")
    other = Organization(id=ids("other_org"), name="Чужой центр", status="active")
    db.add_all([org, other])
    db.flush()
    people = {"owner": _user(db, "owner", ids, "owner", salt), "admin": _user(db, "admin", ids, "admin", salt)}
    for key in "ABCD":
        people[key] = _user(db, key, ids, "teacher", salt)
    for key, user in people.items():
        role = "owner" if key == "owner" else "admin" if key == "admin" else "teacher"
        member = OrganizationMember(id=ids("member_" + key), org_id=org.id, user_id=user.id, org_role=role, is_active=True)
        db.add(member)
    db.flush()
    foreign = User(id=ids("foreign_admin"), email=f"foreign-{salt}@algo.test", password_hash="x", first_name="Чужой", last_name="Админ", role="admin")
    db.add(foreign)
    db.flush()
    db.add(OrganizationMember(org_id=other.id, user_id=foreign.id, org_role="admin", is_active=True))
    db.flush()
    w = SimpleNamespace(db=db, ids=ids, salt=salt, org=org, other_org=other, svc=AuthService(db), foreign_admin=foreign, **people)
    w.member = lambda key: db.query(OrganizationMember).filter_by(id=ids("member_" + key)).one()
    return w


def build_d0(db, salt: Optional[str] = None, *, with_history: bool = True) -> SimpleNamespace:
    """The data set D0 of the SPEC (today = Wed 21.10.2026 14:00 Tashkent)."""
    w = build_org(db, salt)
    ids, org = w.ids, w.org
    w.G1 = new_group(db, org, ids, "G1", "9-А", w.A, [new_slot("lesson-l1", 1, "10:00", "11:30", "2026-10-12"),
                                                      new_slot("lesson-l2", 3, "16:00", "17:30", "2026-10-14")], max_students=3)
    w.G2 = new_group(db, org, ids, "G2", "7-Б", w.A, [new_slot("lesson-l3", 2, "10:00", "11:00", "2026-10-13")])
    w.G3 = new_group(db, org, ids, "G3", "8-В", w.B, [new_slot("lesson-l4", 1, "10:00", "11:30", "2026-10-19")], class_level=8)
    w.s1 = new_student(db, org, ids, "s1", "Каримов", "Алишер", w.G1, display_id="AL09-001")
    w.s2 = new_student(db, org, ids, "s2", "Юсупов", "Бек", w.G1, display_id="AL09-002")
    w.s3 = new_student(db, org, ids, "s3", "Орлова", "Дана", w.G1, display_id="AL09-003")
    w.s4 = new_student(db, org, ids, "s4", "Ахмедов", "Тимур", w.G2, display_id="AL09-005")
    w.s5 = new_student(db, org, ids, "s5", "Рашидова", "Малика", w.G2, display_id="AL09-006")
    w.s6 = new_student(db, org, ids, "s6", "Каримов", "Азиз", w.G3, display_id="AL09-004", class_level=8, phone="+998901112200")
    if with_history:
        g1 = [w.s1, w.s2, w.s3]
        for lid, day, start, end in (("lesson-l1", "2026-10-12", "10:00", "11:20"), ("lesson-l1", "2026-10-19", "10:00", "11:25"),
                                     ("lesson-l2", "2026-10-14", "16:00", "17:25")):
            history_row(db, org, w.G1, lid, day, w.A, g1, start=start, end=end)
        history_row(db, org, w.G2, "lesson-l3", "2026-10-13", w.A, [w.s4, w.s5], start="10:00", end="10:55")
    db.commit()
    return w


def client(w, who: str = "admin", *, org: str = "own"):
    """HTTP client whose identity is the staff member ``who`` of the organisation."""
    user = getattr(w, who)
    role = "owner" if who == "owner" else "admin" if who == "admin" else "teacher"
    return make_client(w, user=user, org=org, org_role=role, role=role)


def ids_of(items: List[dict], key: str = "id") -> List[Any]:
    return [i[key] for i in items]


# ================================================================================================ concurrency
def run_parallel(w, *calls):
    """Run ``calls`` (each ``fn(svc)`` with a service on its OWN session and connection) in threads; returns ``[(result, error), ...]``.
    The caller has committed its data first: the threads see it."""
    import threading

    from sqlalchemy.orm import sessionmaker

    engine = w.db.get_bind()
    Session = sessionmaker(engine, autoflush=False)
    results: List[Any] = [None] * len(calls)
    barrier = threading.Barrier(len(calls))

    def worker(index, fn):
        session = Session()
        try:
            barrier.wait(timeout=10)
            results[index] = (fn(AuthService(session)), None)
        except BaseException as exc:  # noqa: BLE001 - the test inspects the error
            session.rollback()
            results[index] = (None, exc)
        finally:
            session.close()

    threads = [threading.Thread(target=worker, args=(i, fn)) for i, fn in enumerate(calls)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    return results


def code_of(exc: BaseException) -> Optional[str]:
    detail = getattr(exc, "detail", None)
    return detail.get("code") if isinstance(detail, dict) else None


def diff(a: Any, b: Any, path: str = "") -> List[str]:
    """The paths where two JSON values differ (readable assertion output)."""
    if type(a) != type(b):
        return [f"{path}: {a!r} != {b!r}"]
    if isinstance(a, dict):
        out = []
        for k in sorted(set(a) | set(b), key=str):
            if k not in a or k not in b:
                out.append(f"{path}/{k}: {'missing in actual' if k not in a else 'missing in expected'}")
            else:
                out += diff(a[k], b[k], f"{path}/{k}")
        return out
    if isinstance(a, list):
        if len(a) != len(b):
            return [f"{path}: length {len(a)} != {len(b)}: {a!r} != {b!r}"]
        return [d for i, (x, y) in enumerate(zip(a, b)) for d in diff(x, y, f"{path}[{i}]")]
    return [] if a == b else [f"{path}: {a!r} != {b!r}"]


def grant_money_right(w, on: bool = True) -> None:
    """#159: the owner lets (or no longer lets) the admins of the centre decide the money of the teachers (``organizations.settings.payments.admin_can_manage``)."""
    from src.models.auth import Organization

    org = w.db.query(Organization).filter_by(id=w.org.id).one()
    settings = dict(org.settings or {})
    settings["payments"] = {**(settings.get("payments") or {}), "admin_can_manage": on}
    org.settings = settings
    w.db.commit()
