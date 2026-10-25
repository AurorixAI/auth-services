"""LOGIC-B migrations lb1a2b3c4d5 / lb2b3c4d5e6 on stage-like data (own disposable database, derived from the migration one).

The data of the old schema (slots without validFrom, legacy slot keys, the placeholder room, an archived group, pending invitations
without e-mail and duplicates, students with a status that contradicts their group) is written with raw SQL at the PAY-2 revision,
then the chain is upgraded, repeated, stamped back, downgraded and upgraded again.
Matrix rows covered here: [O15.R9] (new / diagnosed students of a live group become active), [O7.R9] (existing rows are at version 1),
[O11.R9] (the placeholder room becomes null), [O1.R9] (validFrom backfill), [O4.R9] (inactive interval of archived groups), [O10.R7] (pending invitations without e-mail expire).
"""
import json
import os
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

from src.core.db_timezone import utc_connect_args

BASE_URL = os.environ.get("A5_AUTH_MIGRATION_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not BASE_URL, reason="Requires isolated a5_auth_migrations PostgreSQL")

PAY2 = "e1f2a3b4c5d"
B1 = "lb1a2b3c4d5"
B2 = "lb2b3c4d5e6"
B3 = "lb3c4d5e6f7"
HEAD = "pf1a2b3c4d5"   # the head of the chain (PAY-F, on top of the console pack cn1a2b3c4d5 and the IA fix pack sa1b2c3d4e5); B3 is the LOGIC-B2 revision before it
DB_NAME = "a5_auth_logb_migrations"


@pytest.fixture
def engine(monkeypatch):
    base = make_url(BASE_URL)
    admin = create_engine(base.set(database="postgres"), isolation_level="AUTOCOMMIT", connect_args=utc_connect_args(BASE_URL))
    with admin.connect() as c:
        c.execute(text(f"DROP DATABASE IF EXISTS {DB_NAME} WITH (FORCE)"))
        c.execute(text(f"CREATE DATABASE {DB_NAME}"))
    admin.dispose()
    url = base.set(database=DB_NAME).render_as_string(hide_password=False)
    monkeypatch.setenv("DATABASE_URL", url)
    eng = create_engine(url, connect_args=utc_connect_args(url))
    yield eng, url
    eng.dispose()


def _config():
    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    return config


def _seed(engine):
    """Stage-like data at the PAY-2 revision."""
    ids = {k: uuid4() for k in ("org", "org2", "owner", "teacher", "g_slots", "g_legacy", "g_archived", "g_empty", "s_new", "s_diag",
                                "s_arch", "s_none", "m_new", "m_diag", "m_arch", "a_1", "a_2", "a_3", "one_off", "ovr", "audit_ovr")}
    slots = [
        {"id": "slot-a", "dayOfWeek": 1, "startTime": "10:00", "endTime": "11:30", "subject": "Математика", "room": "Кабинет 204"},
        {"id": "slot-b", "dayOfWeek": 3, "startTime": "16:00", "endTime": "17:30", "room": "12"},        # first row 14.10
        {"id": "slot-c", "dayOfWeek": 5, "startTime": "09:00", "endTime": "10:00", "validFrom": "2026-09-02"},   # kept as it is
        {"dayOfWeek": 2, "startTime": "10:00", "endTime": "11:00"},                                         # legacy: no id, no room
    ]
    legacy = [{"day_of_week": 4, "start_time": "08:00", "end_time": "09:00"}]
    gid_legacy = ids["g_legacy"]
    legacy_slot_id = f"lesson-{gid_legacy}-4-08:00"
    with engine.begin() as c:
        c.execute(text("INSERT INTO organizations (id,name,status,org_type) VALUES (:o,'Org','active','center'),(:o2,'Org 2','active','center')"), {"o": ids["org"], "o2": ids["org2"]})
        c.execute(text("INSERT INTO users (id,email,password_hash,first_name,last_name,role,must_change_password) VALUES (:u,'o@m.invalid','x','O','W','owner',false)"), {"u": ids["owner"]})
        for key, name, sched, status in (("g_slots", "G slots", slots, "active"), ("g_legacy", "G legacy", legacy, "active"),
                                          ("g_archived", "G archived", [], "archived"), ("g_empty", "G empty", None, "active")):
            c.execute(text("INSERT INTO groups (id,org_id,name,class_level,schedule,status,created_at,updated_at) "
                           "VALUES (:i,:o,:n,9,CAST(:s AS jsonb),:st,'2026-09-01 08:00','2026-10-05 07:30:15')"),
                      {"i": ids[key], "o": ids["org"], "n": name, "s": json.dumps(sched) if sched is not None else None, "st": status})
        # lesson rows: slot-a first on 12.10 (later 19.10), slot-b first on 14.10, the legacy slot first on 15.10
        for lesson_id, day, group in (("slot-a", "2026-10-19", "g_slots"), ("slot-a", "2026-10-12", "g_slots"), ("slot-b", "2026-10-14", "g_slots"),
                                      (legacy_slot_id, "2026-10-15", "g_legacy")):
            c.execute(text("INSERT INTO group_attendance (id,org_id,group_id,lesson_date,lesson_id,status,records) "
                           "VALUES (gen_random_uuid(),:o,:g,:d,:l,'completed','{}')"), {"o": ids["org"], "g": ids[group], "d": day, "l": lesson_id})
        c.execute(text("INSERT INTO group_schedule_one_off_lessons (id,org_id,group_id,lesson_date,payload,created_by) VALUES ('one-off-1',:o,:g,'2026-10-30',"
                       "'{\"room\": \"Кабинет 204\", \"startTime\": \"10:00\"}',:u),('one-off-2',:o,:g,'2026-10-31','{\"room\": \"12\"}',:u)"),
                  {"o": ids["org"], "g": ids["g_slots"], "u": ids["owner"]})
        c.execute(text("INSERT INTO group_schedule_overrides (id,org_id,group_id,lesson_id,scope,effective_date,payload,reason,changed_by) VALUES "
                       "(:i,:o,:g,'slot-a','occurrence','2026-10-26','{\"room\": \"Кабинет 204\"}','r',:u)"),
                  {"i": ids["ovr"], "o": ids["org"], "g": ids["g_slots"], "u": ids["owner"]})
        c.execute(text("INSERT INTO group_schedule_audit (id,override_id,org_id,group_id,lesson_id,scope,effective_date,actor_id,after_payload,reason) VALUES "
                       "(:i,:ov,:o,:g,'slot-a','occurrence','2026-10-26',:u,'{\"room\": \"Кабинет 204\"}','r')"),
                  {"i": ids["audit_ovr"], "ov": ids["ovr"], "o": ids["org"], "g": ids["g_slots"], "u": ids["owner"]})
        # students: new / diagnosed with an active membership of a live group; one in an archived group; one without a group
        for key, status in (("s_new", "new"), ("s_diag", "diagnosed"), ("s_arch", "new"), ("s_none", "new")):
            c.execute(text("INSERT INTO students (id,org_id,first_name,last_name,class_level,status) VALUES (:i,:o,'S',:n,9,:st)"),
                      {"i": ids[key], "o": ids["org"], "n": key, "st": status})
        for m, s, g in (("m_new", "s_new", "g_slots"), ("m_diag", "s_diag", "g_slots"), ("m_arch", "s_arch", "g_archived")):
            c.execute(text("INSERT INTO student_groups (id,student_id,group_id,is_active,joined_at) VALUES (:m,:s,:g,true,'2026-09-02 05:00')"),
                      {"m": ids[m], "s": ids[s], "g": ids[g]})
        # invitations: a pending one without e-mail, two pending duplicates (the newer stays), a pending one of another case
        ins = ("INSERT INTO invitations (id,org_id,invited_by,email,org_role,code,status,expires_at,created_at) "
               "VALUES (:i,:o,:u,:e,'teacher',:c,'pending','2026-12-01 00:00',:t)")
        for key, email, code, created in (("a_1", None, "CODE0001", "2026-10-01 00:00"), ("a_2", "T@x.uz", "CODE0002", "2026-10-02 00:00"),
                                          ("a_3", "t@x.uz", "CODE0003", "2026-10-03 00:00")):
            c.execute(text(ins), {"i": ids[key], "o": ids["org"], "u": ids["owner"], "e": email, "c": code, "t": created})
        # an audit row of the old kind
        c.execute(text("INSERT INTO group_audit (id,org_id,group_id,actor_id,actor_role,action,snapshot) VALUES (gen_random_uuid(),:o,:g,:u,'owner','updated','{}')"),
                  {"o": ids["org"], "g": ids["g_slots"], "u": ids["owner"]})
    return ids, legacy_slot_id


def test_logicb_migrations_on_stage_like_data(engine):
    eng, url = engine
    config = _config()
    # The first revision of the chain creates the tables from the CURRENT models (so an empty database already has the LOGIC-B
    # objects): go to the head and back to get the schema of the previous release, then write the stage-like data.
    command.upgrade(config, "head")
    command.downgrade(config, PAY2)
    assert "version" not in {c["name"] for c in inspect(eng).get_columns("group_attendance")}
    ids, legacy_slot_id = _seed(eng)

    command.upgrade(config, "head")
    with eng.connect() as c:
        assert c.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == HEAD
        # O7: every existing row is at version 1
        assert {r[0] for r in c.execute(text("SELECT version FROM group_attendance")).fetchall()} == {1}
        # O1: validFrom of every slot
        schedule = c.execute(text("SELECT schedule FROM groups WHERE id=:g"), {"g": ids["g_slots"]}).scalar_one()
        by_id = {s.get("id"): s for s in schedule}
        # the backfill does NOT change history: a legacy slot is valid from the day the GROUP was created (01.09 13:00 Tashkent), whatever its first row is
        assert by_id["slot-a"]["validFrom"] == "2026-09-01"          # first row 12.10: not used any more
        assert by_id["slot-b"]["validFrom"] == "2026-09-01"
        assert by_id["slot-c"]["validFrom"] == "2026-09-02"          # already there: kept (the seed gives it 2026-09-02)
        no_id = [s for s in schedule if "id" not in s][0]
        assert no_id["validFrom"] == "2026-09-01"                    # no rows at all: still the creation day (its past lessons stay "missed", not erased)
        legacy = c.execute(text("SELECT schedule FROM groups WHERE id=:g"), {"g": ids["g_legacy"]}).scalar_one()
        assert legacy[0]["validFrom"] == "2026-09-01"
        assert c.execute(text("SELECT schedule FROM groups WHERE id=:g"), {"g": ids["g_empty"]}).scalar_one() is None
        # O11: the placeholder room is gone, a real one stays, the immutable audit is untouched
        assert by_id["slot-a"]["room"] is None and by_id["slot-b"]["room"] == "12"
        rooms = dict(c.execute(text("SELECT id, payload->'room' FROM group_schedule_one_off_lessons")).fetchall())
        assert rooms == {"one-off-1": None, "one-off-2": "12"}
        assert c.execute(text("SELECT payload->'room' FROM group_schedule_overrides WHERE id=:i"), {"i": ids["ovr"]}).scalar_one() is None
        assert c.execute(text("SELECT after_payload->>'room' FROM group_schedule_audit WHERE id=:i"), {"i": ids["audit_ovr"]}).scalar_one() == "Кабинет 204"
        # O4: archived group -> an open interval from its updated_at; the others have none
        assert c.execute(text("SELECT inactive_intervals FROM groups WHERE id=:g"), {"g": ids["g_archived"]}).scalar_one() == [{"from": "2026-10-05T07:30:15", "to": None}]
        assert c.execute(text("SELECT inactive_intervals FROM groups WHERE id=:g"), {"g": ids["g_slots"]}).scalar_one() == []
        # O10: no e-mail -> expired; of the duplicates only the newest stays pending
        status = {r[0]: r[1] for r in c.execute(text("SELECT code, status FROM invitations")).fetchall()}
        assert status == {"CODE0001": "expired", "CODE0002": "cancelled", "CODE0003": "pending"}
        # O15: new / diagnosed students of a live group are active; archived group / no group: untouched
        st = {r[0]: r[1] for r in c.execute(text("SELECT last_name, status FROM students")).fetchall()}
        assert st == {"s_new": "active", "s_diag": "active", "s_arch": "new", "s_none": "new"}
        # O13: the time conventions are documented on the columns
        comments = dict(c.execute(text(
            "SELECT a.attname, col_description(a.attrelid, a.attnum) FROM pg_attribute a WHERE a.attrelid='group_attendance'::regclass "
            "AND a.attname IN ('started_at','completed_at','created_at','updated_at')")).fetchall())
        assert comments == {"started_at": "business time Asia/Tashkent, naive", "completed_at": "business time Asia/Tashkent, naive",
                            "created_at": "UTC naive", "updated_at": "UTC naive"}
    tables = set(inspect(eng).get_table_names())
    assert {"lesson_teacher_assignments", "member_audit"} <= tables
    assert "exam_release_pending" in {c["name"] for c in inspect(eng).get_columns("groups")}
    idx = {i["name"] for i in inspect(eng).get_indexes("lesson_teacher_assignments")}
    assert {"uq_lesson_teacher_assignment_active", "idx_lesson_teacher_assignment_teacher"} <= idx
    assert "uq_invitations_pending_email" in {i["name"] for i in inspect(eng).get_indexes("invitations")}

    # the new guards
    from sqlalchemy.exc import IntegrityError

    def run(sql, **params):
        with eng.begin() as c:
            c.execute(text(sql), params)

    run("INSERT INTO group_audit (id,org_id,group_id,actor_id,actor_role,action,snapshot) VALUES (gen_random_uuid(),:o,:g,:u,'owner','group_archived','{}')",
        o=ids["org"], g=ids["g_slots"], u=ids["owner"])
    with pytest.raises(IntegrityError):
        run("INSERT INTO group_audit (id,org_id,group_id,actor_id,actor_role,action,snapshot) VALUES (gen_random_uuid(),:o,:g,:u,'owner','nonsense','{}')",
            o=ids["org"], g=ids["g_slots"], u=ids["owner"])
    att = uuid4()
    run("INSERT INTO group_attendance (id,org_id,group_id,lesson_date,lesson_id,status,records) VALUES (:i,:o,:g,'2026-10-26','x','in_progress','{}')",
        i=att, o=ids["org"], g=ids["g_slots"])
    run("INSERT INTO group_attendance_audit (id,attendance_id,org_id,group_id,actor_id,action,after_status,after_records) "
        "VALUES (gen_random_uuid(),:a,:o,:g,:u,'teacher_changed','in_progress','{}')", a=att, o=ids["org"], g=ids["g_slots"], u=ids["owner"])
    asg = ("INSERT INTO lesson_teacher_assignments (id,org_id,group_id,lesson_id,lesson_date,teacher_id,reason,set_by,cancelled_at) "
           "VALUES (gen_random_uuid(),:o,:g,'slot-a','2026-10-26',:u,'болезнь',:u,:c)")
    run(asg, o=ids["org"], g=ids["g_slots"], u=ids["owner"], c=None)
    run(asg, o=ids["org"], g=ids["g_slots"], u=ids["owner"], c=datetime(2026, 10, 21))       # a cancelled one next to the active one: fine
    with pytest.raises(IntegrityError):                                                        # a second active one: not
        run(asg, o=ids["org"], g=ids["g_slots"], u=ids["owner"], c=None)
    with pytest.raises(IntegrityError):                                                        # one pending per e-mail and centre
        run("INSERT INTO invitations (id,org_id,invited_by,email,org_role,code,status,expires_at) VALUES (gen_random_uuid(),:o,:u,'T@X.UZ','teacher','CODE0009','pending','2026-12-01')",
            o=ids["org"], u=ids["owner"])
    run("INSERT INTO invitations (id,org_id,invited_by,email,org_role,code,status,expires_at) VALUES (gen_random_uuid(),:o,:u,'T@X.UZ','teacher','CODE0010','cancelled','2026-12-01')",
        o=ids["org"], u=ids["owner"])
    run("INSERT INTO invitations (id,org_id,invited_by,email,org_role,code,status,expires_at) VALUES (gen_random_uuid(),:o,:u,'t@x.uz','teacher','CODE0011','pending','2026-12-01')",
        o=ids["org2"], u=ids["owner"])                                                        # another centre: fine

    # LOGIC-B2 (lb3c4d5e6f7): the request / event tables, the CHECKs, the partial unique index of INV-B2-1
    assert {"lesson_substitution_requests", "lesson_substitution_events"} <= tables
    req_idx = {i["name"] for i in inspect(eng).get_indexes("lesson_substitution_requests")}
    assert "uq_lsr_pending_lesson" in req_idx
    lsr = ("INSERT INTO lesson_substitution_requests (id,org_id,group_id,lesson_id,lesson_date,start_time,end_time,starts_at,requester_id,origin,batch_id,reason_code,status,decided_at,created_at,updated_at) "
           "VALUES (:i,:o,:g,'slot-a','2026-10-26','10:00','11:30','2026-10-26 05:00',:u,'teacher',gen_random_uuid(),'sick',:s,:d,now(),now())")
    pending_id = uuid4()
    run(lsr, i=pending_id, o=ids["org"], g=ids["g_slots"], u=ids["owner"], s="pending", d=None)
    with pytest.raises(IntegrityError):                                                        # a second pending request of the lesson
        run(lsr, i=uuid4(), o=ids["org"], g=ids["g_slots"], u=ids["owner"], s="pending", d=None)
    run(lsr, i=uuid4(), o=ids["org"], g=ids["g_slots"], u=ids["owner"], s="withdrawn", d=datetime(2026, 10, 21))   # a terminal one next to it: fine
    with pytest.raises(IntegrityError):                                                        # pending with a decision time / terminal without it
        run(lsr, i=uuid4(), o=ids["org"], g=ids["g_slots"], u=ids["owner"], s="pending", d=datetime(2026, 10, 21))
    with pytest.raises(IntegrityError):
        run(lsr, i=uuid4(), o=ids["org"], g=ids["g_slots"], u=ids["owner"], s="rejected", d=None)
    with pytest.raises(IntegrityError):
        run(lsr, i=uuid4(), o=ids["org"], g=ids["g_slots"], u=ids["owner"], s="nonsense", d=datetime(2026, 10, 21))
    ev = "INSERT INTO lesson_substitution_events (id,request_id,org_id,actor_id,action,snapshot,created_at) VALUES (gen_random_uuid(),:r,:o,NULL,:a,'{}',now())"
    run(ev, r=pending_id, o=ids["org"], a="requested")
    with pytest.raises(IntegrityError):
        run(ev, r=pending_id, o=ids["org"], a="nonsense")

    # idempotent: a second upgrade, and the revisions again after a stamp - nothing changes
    snapshot = _data_snapshot(eng)
    command.upgrade(config, "head")
    command.stamp(config, PAY2)
    command.upgrade(config, "head")
    assert _data_snapshot(eng) == snapshot

    # downgrade: the objects go, the audit rows of the new actions go with them, the old CHECKs are back; then up again
    command.downgrade(config, PAY2)
    cols = {c["name"] for c in inspect(eng).get_columns("group_attendance")}
    assert "version" not in cols
    assert not ({"lesson_teacher_assignments", "member_audit"} & set(inspect(eng).get_table_names()))
    assert not ({"inactive_intervals", "archive_reason", "exam_release_pending"} & {c["name"] for c in inspect(eng).get_columns("groups")})
    assert not ({"lesson_substitution_requests", "lesson_substitution_events"} & set(inspect(eng).get_table_names()))      # the requests are lost by the rollback (documented)
    with eng.connect() as c:
        assert c.execute(text("SELECT count(*) FROM group_audit WHERE action IN ('group_archived','group_restored','lesson_teacher_set','lesson_teacher_cleared')")).scalar_one() == 0
        assert c.execute(text("SELECT count(*) FROM group_attendance_audit WHERE action='teacher_changed'")).scalar_one() == 0
        assert c.execute(text("SELECT count(*) FROM group_audit WHERE action='updated'")).scalar_one() == 1      # the old audit is kept
        assert c.execute(text("SELECT col_description('group_attendance'::regclass, a.attnum) FROM pg_attribute a WHERE a.attrelid='group_attendance'::regclass AND a.attname='started_at'")).scalar_one() is None
    with pytest.raises(IntegrityError):
        run("INSERT INTO group_audit (id,org_id,group_id,actor_id,actor_role,action,snapshot) VALUES (gen_random_uuid(),:o,:g,:u,'owner','group_archived','{}')",
            o=ids["org"], g=ids["g_slots"], u=ids["owner"])
    command.downgrade(config, PAY2)  # repeating is harmless
    command.upgrade(config, "head")
    with eng.connect() as c:
        assert c.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == HEAD
        # a slot keeps the validFrom the first upgrade wrote (the data steps are not reverted)
        schedule = c.execute(text("SELECT schedule FROM groups WHERE id=:g"), {"g": ids["g_slots"]}).scalar_one()
        assert {s.get("id"): s["validFrom"] for s in schedule if "id" in s}["slot-a"] == "2026-09-01"
        # the archived group got its interval again (the column was dropped and re-created)
        assert c.execute(text("SELECT inactive_intervals FROM groups WHERE id=:g"), {"g": ids["g_archived"]}).scalar_one() == [{"from": "2026-10-05T07:30:15", "to": None}]


def _data_snapshot(eng):
    with eng.connect() as c:
        return (
            [tuple(r) for r in c.execute(text("SELECT id, schedule::text, inactive_intervals::text, status FROM groups ORDER BY id")).fetchall()],
            [tuple(r) for r in c.execute(text("SELECT code, status FROM invitations ORDER BY code")).fetchall()],
            [tuple(r) for r in c.execute(text("SELECT id, status FROM students ORDER BY id")).fetchall()],
            [tuple(r) for r in c.execute(text("SELECT id, payload::text FROM group_schedule_one_off_lessons ORDER BY id")).fetchall()],
        )
