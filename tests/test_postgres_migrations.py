"""Real Alembic upgrade/rollback on one explicitly disposable PostgreSQL DB."""
import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

URL=os.environ.get('A5_AUTH_MIGRATION_TEST_DATABASE_URL')
pytestmark=pytest.mark.skipif(not URL,reason='Requires isolated a5_auth_migrations PostgreSQL')


def test_empty_upgrade_repeat_and_checkpoint_upgrade_preserve_records(monkeypatch):
    assert URL and URL.endswith('/a5_auth_migrations'), 'Use only the disposable migration database'
    engine=create_engine(URL)
    assert not inspect(engine).get_table_names(), 'The migration fixture must start empty'
    monkeypatch.setenv('DATABASE_URL',URL)
    root=Path(__file__).resolve().parents[1]
    config=Config(str(root/'alembic.ini'))
    config.set_main_option('script_location',str(root/'alembic'))
    command.upgrade(config,'head')
    expected={'organizations','student_groups','group_schedule_overrides','group_schedule_audit',
              'group_schedule_one_off_lessons','group_audit','group_attendance','group_attendance_audit'}
    assert expected <= set(inspect(engine).get_table_names())
    assert {'attendance_excuses','attendance_excuse_files'} <= set(inspect(engine).get_table_names())
    assert 'teacher_id' in {c['name'] for c in inspect(engine).get_columns('group_attendance')}
    assert 'details' in {c['name'] for c in inspect(engine).get_columns('group_attendance_audit')}
    assert {'initial_diagnostic_reviewed_invite_id','initial_diagnostic_reviewed_by',
            'initial_diagnostic_reviewed_at'} <= {c['name'] for c in inspect(engine).get_columns('student_groups')}
    ids={name:uuid4() for name in ['org','group','student','membership']}
    from src.models.auth import Organization,Group,Student,StudentGroup
    with Session(engine) as db:
        db.add(Organization(id=ids['org'],name='Migration sentinel',status='active')); db.flush()
        db.add(Group(id=ids['group'],org_id=ids['org'],name='Migration group',class_level=9))
        db.add(Student(id=ids['student'],org_id=ids['org'],first_name='Synthetic',last_name='Migration',class_level=9,status='active'))
        db.flush()
        db.add(StudentGroup(id=ids['membership'],student_id=ids['student'],group_id=ids['group']))
        db.commit()
    command.upgrade(config,'head')
    command.downgrade(config,'l2m3n4o5p6q7')
    assert 'initial_diagnostic_reviewed_by' not in {c['name'] for c in inspect(engine).get_columns('student_groups')}
    command.upgrade(config,'head')
    with engine.connect() as connection:
        assert connection.execute(text('SELECT name FROM organizations WHERE id=:id'),{'id':ids['org']}).scalar_one()=='Migration sentinel'
        assert connection.execute(text('SELECT count(*) FROM student_groups WHERE id=:id'),{'id':ids['membership']}).scalar_one()==1
        assert connection.execute(text('SELECT version_num FROM alembic_version')).scalar_one()=='s9t0u1v2w3x'
    _check_n13_p1_objects(engine,config,ids)
    # Existing application loggers must survive the in-process Alembic setup.
    import logging
    assert not logging.getLogger('src.api.main_v2').disabled
    engine.dispose()


def _check_n13_p1_objects(engine,config,ids):
    """N13 P1: constraints work, data survives, downgrade/upgrade round-trip."""
    from sqlalchemy.exc import IntegrityError
    from src.models.auth import User
    teacher,att,exc_id=uuid4(),uuid4(),uuid4()
    with Session(engine) as db:
        db.add(User(id=teacher,email=f'{teacher}@m.invalid',password_hash='synthetic-only',first_name='T',last_name='T',role='teacher'))
        db.commit()
    with engine.begin() as c:
        c.execute(text('UPDATE groups SET teacher_id=:t WHERE id=:g'),{'t':teacher,'g':ids['group']})
        c.execute(text("INSERT INTO group_attendance (id,org_id,group_id,lesson_date,lesson_id,status,records) VALUES (:a,:o,:g,'2026-10-01','m1','completed','{}')"),{'a':att,'o':ids['org'],'g':ids['group']})
        c.execute(text("INSERT INTO attendance_excuses (id,org_id,attendance_id,student_id,reason,reason_by,reason_at) VALUES (:e,:o,:a,:s,'болел',:u,now())"),{'e':exc_id,'o':ids['org'],'a':att,'s':ids['student'],'u':teacher})
    with engine.connect() as c:
        assert c.execute(text('SELECT status FROM attendance_excuses WHERE id=:e'),{'e':exc_id}).scalar_one()=='pending'
    # CHECK / UNIQUE guards
    bad={
        'status':"INSERT INTO attendance_excuses (id,org_id,attendance_id,student_id,reason,reason_by,reason_at,status) VALUES (gen_random_uuid(),:o,:a,gen_random_uuid(),'r',:u,now(),'bogus')",
        'unique':"INSERT INTO attendance_excuses (id,org_id,attendance_id,student_id,reason,reason_by,reason_at) VALUES (gen_random_uuid(),:o,:a,:s,'r',:u,now())",
        'size':"INSERT INTO attendance_excuse_files (id,org_id,excuse_id,content_type,size_bytes,data,uploaded_by,uploaded_at) VALUES (gen_random_uuid(),:o,:e,'image/png',2097153,'\\x00',:u,now())",
        'audit':"INSERT INTO group_attendance_audit (id,attendance_id,org_id,group_id,actor_id,action,after_status,after_records) VALUES (gen_random_uuid(),:a,:o,:g,:u,'nope','completed','{}')",
        'group_audit':"INSERT INTO group_audit (id,org_id,group_id,actor_id,actor_role,action,snapshot) VALUES (gen_random_uuid(),:o,:g,:u,'admin','nope','{}')",
    }
    params={'o':ids['org'],'a':att,'s':ids['student'],'u':teacher,'e':exc_id,'g':ids['group']}
    for name,sql in bad.items():
        with pytest.raises(IntegrityError,match='.'):
            with engine.begin() as c:
                c.execute(text(sql),params)
    with engine.begin() as c:
        for action in ('excuse_reviewed','excuse_reset','excuse_removed','excuse_file_added','excuse_file_removed'):
            c.execute(text("INSERT INTO group_attendance_audit (id,attendance_id,org_id,group_id,actor_id,action,after_status,after_records,details) VALUES (gen_random_uuid(),:a,:o,:g,:u,'"+action+"','completed','{}','[]')"),params)
        c.execute(text("INSERT INTO group_audit (id,org_id,group_id,actor_id,actor_role,action,snapshot) VALUES (gen_random_uuid(),:o,:g,:u,'admin','teacher_changed','{}')"),params)
        # Deleting the lesson removes its excuses (CASCADE).
        c.execute(text("INSERT INTO group_attendance (id,org_id,group_id,lesson_date,lesson_id,status,records) VALUES (:a2,:o,:g,'2026-10-02','m2','completed','{}')"),{**params,'a2':uuid4()})
    # Round trip: downgrade drops the new objects but keeps lessons and audit rows.
    command.downgrade(config,'r8s9t0u1v2w')
    names=set(inspect(engine).get_table_names())
    assert not ({'attendance_excuses','attendance_excuse_files'} & names)
    assert 'teacher_id' not in {c['name'] for c in inspect(engine).get_columns('group_attendance')}
    assert 'details' not in {c['name'] for c in inspect(engine).get_columns('group_attendance_audit')}
    with engine.connect() as c:
        assert c.execute(text('SELECT version_num FROM alembic_version')).scalar_one()=='r8s9t0u1v2w'
        assert c.execute(text('SELECT count(*) FROM group_attendance WHERE id=:a'),params).scalar_one()==1
        assert c.execute(text("SELECT count(*) FROM group_audit WHERE action='teacher_changed'")).scalar_one()==1
    with pytest.raises(IntegrityError):
        with engine.begin() as c:
            c.execute(text(bad['group_audit'].replace("'nope'","'teacher_changed'")),params)
    command.upgrade(config,'head')
    command.upgrade(config,'head')  # idempotent re-run
    # Objects already present (baseline/create_all) must not break the revision.
    command.stamp(config,'r8s9t0u1v2w')
    command.upgrade(config,'head')
    with engine.connect() as c:
        assert c.execute(text('SELECT version_num FROM alembic_version')).scalar_one()=='s9t0u1v2w3x'
        # Backfill restores the snapshot from the group's current teacher.
        assert c.execute(text('SELECT teacher_id FROM group_attendance WHERE id=:a'),params).scalar_one()==teacher
