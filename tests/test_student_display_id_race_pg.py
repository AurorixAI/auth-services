"""#96: student display-ID allocation under concurrency (disposable PostgreSQL, A2_AUTH_TEST_DATABASE_URL).

Two things can go wrong when students are enrolled at the same moment:

* two *organisations* with equal initials enrol their first student together: both pick the same free prefix
  (``NC``), the second INSERT of ``organizations.student_id_prefix`` hits ``uq_org_student_id_prefix`` and the
  request used to fail with a 500;
* two students of the *same* organisation get the same number (``TE-003`` twice) or an organisation ends up
  with two prefixes.

Every test runs real threads with their own sessions and removes what it created.
"""
import os
import random
import string
import threading
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func
from src.core.db_timezone import utc_connect_args
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("SECRET_KEY", "test-secret-key-must-be-at-least-32-chars-ok")
os.environ.setdefault("JWT_SECRET_KEY", "test-jwt-secret-key-must-be-at-least-32")
os.environ.setdefault("APP_ENV", "testing")
os.environ.setdefault("DATABASE_URL", "postgresql://algo:algo@localhost:5432/algo_test")

from src.auth import student_id_service as sid  # noqa: E402
from src.models.auth import Organization, Student  # noqa: E402

DB_URL = os.getenv("A2_AUTH_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DB_URL, reason="Requires disposable A2 auth PostgreSQL")


@pytest.fixture
def factory():
    assert DB_URL and DB_URL.endswith("/a2_auth"), "Use only the disposable a2_auth database"
    engine = create_engine(DB_URL, pool_size=10, connect_args=utc_connect_args(DB_URL))
    Organization.metadata.create_all(engine)
    made: list = []  # org ids to clean up
    yield sessionmaker(engine), made
    with sessionmaker(engine)() as db:
        for org_id in made:
            db.query(Student).filter(Student.org_id == org_id).delete()
            db.query(Organization).filter(Organization.id == org_id).delete()
        db.commit()
    engine.dispose()


def _letters() -> str:
    """Two random capitals: initials that no other test or earlier run is likely to hold."""
    return "".join(random.choice(string.ascii_uppercase) for _ in range(2))


def _make_org(sessions, made, name, prefix=None):
    with sessions() as db:
        org = Organization(name=name, status="active", student_id_prefix=prefix)
        db.add(org)
        db.commit()
        made.append(org.id)
        return org.id


def _make_student(sessions, org_id, first="Алия"):
    with sessions() as db:
        student = Student(org_id=org_id, first_name=first, last_name=uuid4().hex[:8], class_level=9, status="active")
        db.add(student)
        db.commit()
        return student.id


def _run_threads(jobs):
    """Run every job in its own thread; return (results, errors) in job order."""
    results, errors = [None] * len(jobs), [None] * len(jobs)

    def wrap(i, job):
        try:
            results[i] = job()
        except BaseException as exc:  # noqa: BLE001 - the test reports whatever the request would have died of
            errors[i] = exc

    threads = [threading.Thread(target=wrap, args=(i, job)) for i, job in enumerate(jobs)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not any(t.is_alive() for t in threads), "a thread is stuck: deadlock"
    return results, errors


def test_equal_initials_enrolling_first_students_together_both_succeed(factory, monkeypatch):
    sessions, made = factory
    ab = _letters()
    org_a = _make_org(sessions, made, f"{ab[0]}alpha {ab[1]}beta")
    org_b = _make_org(sessions, made, f"{ab[0]}gamma {ab[1]}delta")
    student_a, student_b = _make_student(sessions, org_a), _make_student(sessions, org_b)

    # Make the race deterministic: each thread waits, once, after it has chosen its prefix, until the other has
    # chosen too, so both see the same free candidate before either one writes it.
    chosen = threading.Barrier(2, timeout=20)
    seen = threading.local()
    real_unique_prefix = sid._unique_prefix

    def unique_prefix(*args, **kwargs):
        prefix = real_unique_prefix(*args, **kwargs)
        if not getattr(seen, "waited", False):
            seen.waited = True
            chosen.wait()
        return prefix

    monkeypatch.setattr(sid, "_unique_prefix", unique_prefix)

    def enrol(org_id, student_id):
        def job():
            with sessions() as db:
                student = db.get(Student, student_id)
                sid.assign_display_id(db, org_id, student)
                db.commit()
                return student.display_id

        return job

    results, errors = _run_threads([enrol(org_a, student_a), enrol(org_b, student_b)])
    assert errors == [None, None], errors
    with sessions() as db:
        prefixes = [db.get(Organization, o).student_id_prefix for o in (org_a, org_b)]
    assert all(prefixes) and prefixes[0] != prefixes[1], prefixes
    assert {p[:2] for p in prefixes} == {ab}, "the loser takes the next candidate: AB, AB1"
    assert sorted(results) == sorted(f"{p}-001" for p in prefixes), results


def test_many_organisations_with_equal_initials_all_get_distinct_prefixes(factory):
    sessions, made = factory
    ab = _letters()
    orgs = [_make_org(sessions, made, f"{ab[0]}one{i} {ab[1]}two{i}") for i in range(6)]
    students = [_make_student(sessions, o) for o in orgs]
    start = threading.Barrier(len(orgs), timeout=20)

    def enrol(org_id, student_id):
        def job():
            with sessions() as db:
                student = db.get(Student, student_id)
                start.wait()
                sid.assign_display_id(db, org_id, student)
                db.commit()
                return student.display_id

        return job

    results, errors = _run_threads([enrol(o, s) for o, s in zip(orgs, students)])
    assert errors == [None] * len(orgs), errors
    with sessions() as db:
        prefixes = [db.get(Organization, o).student_id_prefix for o in orgs]
    assert len(set(prefixes)) == len(orgs), prefixes
    assert all(p.startswith(ab) for p in prefixes), prefixes
    assert sorted(results) == sorted(f"{p}-001" for p in prefixes)


def test_a_lost_prefix_claim_is_retried_in_a_savepoint_that_keeps_the_callers_transaction(factory, monkeypatch):
    """The prefix write runs in a SAVEPOINT: what the caller already wrote in the same transaction survives it."""
    sessions, made = factory
    ab = _letters()
    holder = _make_org(sessions, made, f"{ab[0]}holder {ab[1]}one")
    with sessions() as db:
        sid.assign_display_id(db, holder, db.get(Student, _make_student(sessions, holder)))
        db.commit()
        assert db.get(Organization, holder).student_id_prefix == ab
    late = _make_org(sessions, made, f"{ab[0]}late {ab[1]}two")
    student_id = _make_student(sessions, late)
    real = sid._unique_prefix
    calls = []

    def stale_first(db, base, exclude, rejected=()):
        # The first answer is a candidate somebody else has just taken: the INSERT really hits the unique index.
        calls.append(set(rejected))
        return base if not rejected else real(db, base, exclude, rejected)

    monkeypatch.setattr(sid, "_unique_prefix", stale_first)
    with sessions() as db:
        student = db.get(Student, student_id)
        student.first_name = "Written before the prefix"
        db.flush()
        sid.assign_display_id(db, late, student)
        db.commit()
    assert calls == [set(), {ab}], calls
    with sessions() as db:
        assert db.get(Organization, late).student_id_prefix == f"{ab}1"
        assert db.get(Student, student_id).first_name == "Written before the prefix"
        assert db.get(Student, student_id).display_id == f"{ab}1-001"


def test_students_of_one_organisation_enrolled_together_get_distinct_consecutive_numbers(factory):
    sessions, made = factory
    org_id = _make_org(sessions, made, f"{_letters()[0]}same {_letters()[1]}org", prefix=None)
    count = 8
    students = [_make_student(sessions, org_id, f"S{i}") for i in range(count)]
    start = threading.Barrier(count, timeout=20)

    def enrol(student_id):
        def job():
            with sessions() as db:
                org = db.get(Organization, org_id)  # the org is already in the session, as in a real request
                assert org is not None
                student = db.get(Student, student_id)
                start.wait()
                sid.assign_display_id(db, org_id, student)
                db.commit()
                return student.display_id

        return job

    results, errors = _run_threads([enrol(s) for s in students])
    assert errors == [None] * count, errors
    assert len(set(results)) == count, results
    with sessions() as db:
        prefix = db.get(Organization, org_id).student_id_prefix
        numbers = sorted(n for (n,) in db.query(Student.sequence_number).filter(Student.org_id == org_id))
    assert prefix and all(r.startswith(f"{prefix}-") for r in results), (prefix, results)
    assert numbers == list(range(1, count + 1))


def test_a_session_that_loaded_the_organisation_before_its_prefix_was_assigned_keeps_that_prefix(factory):
    """The lock must re-read the organisation: a stale copy would assign a second, different prefix."""
    sessions, made = factory
    ab = _letters()
    holder = _make_org(sessions, made, f"{ab[0]}holder {ab[1]}one", prefix=ab)  # so the new org gets AB1, not AB
    org_id = _make_org(sessions, made, f"{ab[0]}stale {ab[1]}org")
    first, second = _make_student(sessions, org_id, "A"), _make_student(sessions, org_id, "B")
    with sessions() as late:
        held = late.get(Organization, org_id)  # this request has read the organisation (and still holds it)
        assert held.student_id_prefix is None
        late_student = late.get(Student, second)
        with sessions() as early:  # another request enrols the first student and commits
            sid.assign_display_id(early, org_id, early.get(Student, first))
            early.commit()
        with sessions() as check:
            assert check.get(Organization, org_id).student_id_prefix == f"{ab}1"
        # AB is free again, so a stale organisation would now be given AB: a second prefix for the same school.
        with sessions() as other:
            other.query(Organization).filter(Organization.id == holder).update({"student_id_prefix": None})
            other.commit()
        sid.assign_display_id(late, org_id, late_student)
        late.commit()
        assert late_student.display_id == f"{ab}1-002"
        assert held.student_id_prefix == f"{ab}1"
    with sessions() as db:
        assert db.get(Organization, org_id).student_id_prefix == f"{ab}1"


def test_the_batch_allocator_survives_the_same_prefix_race(factory, monkeypatch):
    sessions, made = factory
    ab = _letters()
    org_a = _make_org(sessions, made, f"{ab[0]}batch {ab[1]}one")
    org_b = _make_org(sessions, made, f"{ab[0]}batch {ab[1]}two")
    chosen = threading.Barrier(2, timeout=20)
    seen = threading.local()
    real_unique_prefix = sid._unique_prefix

    def unique_prefix(*args, **kwargs):
        prefix = real_unique_prefix(*args, **kwargs)
        if not getattr(seen, "waited", False):
            seen.waited = True
            chosen.wait()
        return prefix

    monkeypatch.setattr(sid, "_unique_prefix", unique_prefix)

    def importing(org_id):
        def job():
            with sessions() as db:
                allocator = sid.DisplayIdAllocator(db, org_id)
                students = [Student(org_id=org_id, first_name="I", last_name=str(i), class_level=9) for i in range(3)]
                db.add_all(students)
                for student in students:
                    allocator.allocate(student)
                db.commit()
                return [s.display_id for s in students]

        return job

    results, errors = _run_threads([importing(org_a), importing(org_b)])
    assert errors == [None, None], errors
    with sessions() as db:
        assert db.query(func.count(func.distinct(Organization.student_id_prefix))).filter(
            Organization.id.in_([org_a, org_b])
        ).scalar() == 2
    flat = [d for r in results for d in r]
    assert len(set(flat)) == 6, flat
