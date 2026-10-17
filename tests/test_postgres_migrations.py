"""Real Alembic upgrade/rollback on one explicitly disposable PostgreSQL DB."""
import os
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from src.core.db_timezone import utc_connect_args
from sqlalchemy.orm import Session

URL=os.environ.get('A5_AUTH_MIGRATION_TEST_DATABASE_URL')
pytestmark=pytest.mark.skipif(not URL,reason='Requires isolated a5_auth_migrations PostgreSQL')
HEAD='x4y5z6a7b8c'          # student archive (N13)
PREVIOUS='w3x4y5z6a7b'      # lesson type and payroll (N13 P3)


def test_empty_upgrade_repeat_and_checkpoint_upgrade_preserve_records(monkeypatch):
    assert URL and URL.endswith('/a5_auth_migrations'), 'Use only the disposable migration database'
    engine=create_engine(URL, connect_args=utc_connect_args(URL))
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
        assert connection.execute(text('SELECT version_num FROM alembic_version')).scalar_one()==HEAD
    assert 'student_payments' in set(inspect(engine).get_table_names())
    assert {'monthly_fee','monthly_fee_updated_by','monthly_fee_updated_at','transferred_at'} <= {c['name'] for c in inspect(engine).get_columns('student_groups')}
    _check_n13_p1_objects(engine,config,ids)
    _check_n13_p2_objects(engine,config,ids)
    _check_n13_p21_objects(engine,config,ids)
    _check_n13_p22_objects(engine,config,ids)
    _check_n13_p3_objects(engine,config,ids)
    _check_n13_archive_objects(engine,config,ids,monkeypatch)
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
        assert c.execute(text('SELECT version_num FROM alembic_version')).scalar_one()==HEAD
        # Backfill restores the snapshot from the group's current teacher.
        assert c.execute(text('SELECT teacher_id FROM group_attendance WHERE id=:a'),params).scalar_one()==teacher


def _check_n13_p2_objects(engine,config,ids):
    """N13 P2: payments table guards, RESTRICT on memberships, round trip."""
    from datetime import date
    from sqlalchemy.exc import IntegrityError
    from src.models.auth import User
    actor,pay=uuid4(),uuid4()
    with Session(engine) as db:
        db.add(User(id=actor,email=f'{actor}@m.invalid',password_hash='synthetic-only',first_name='P',last_name='P',role='owner'))
        db.commit()
    params={'o':ids['org'],'m':ids['membership'],'s':ids['student'],'g':ids['group'],'u':actor,'p':pay}
    insert=("INSERT INTO student_payments (id,org_id,membership_id,student_id,group_id,amount,paid_on,valid_from,valid_to,lessons_count,created_by{extra}) "
            "VALUES ({pid},:o,:m,:s,:g,{amount},'2026-10-05','2026-10-05',{valid_to},{lessons},:u{extra_v})")
    def row(pid=':p',amount='450000',valid_to="'2026-11-04'",lessons='NULL',extra='',extra_v=''):
        return insert.format(pid=pid,amount=amount,valid_to=valid_to,lessons=lessons,extra=extra,extra_v=extra_v)
    with engine.begin() as c:
        c.execute(text(row()),params)
        # a lessons-mode payment is a different row, not a duplicate
        c.execute(text(row(pid='gen_random_uuid()',valid_to='NULL',lessons='12')),params)
    bad={
        'negative amount':row(pid='gen_random_uuid()',amount='-1',valid_to="'2026-12-04'"),
        'zero lessons':row(pid='gen_random_uuid()',valid_to='NULL',lessons='0',amount='1'),
        'neither period':row(pid='gen_random_uuid()',valid_to='NULL',lessons='NULL',amount='2'),
        'both periods':row(pid='gen_random_uuid()',lessons='12',amount='3'),
        'valid_to before valid_from':row(pid='gen_random_uuid()',valid_to="'2026-10-04'",amount='4'),
        'partial void':row(pid='gen_random_uuid()',amount='5',valid_to="'2026-12-05'",extra=',voided_at',extra_v=',now()'),
        'duplicate live payment':row(pid='gen_random_uuid()'),
        'duplicate live lessons payment':row(pid='gen_random_uuid()',valid_to='NULL',lessons='12'),
    }
    for name,sql in bad.items():
        with pytest.raises(IntegrityError,match='.'):
            with engine.begin() as c:
                c.execute(text(sql),params)
    with engine.begin() as c:
        c.execute(text("UPDATE student_payments SET voided_at=now(),voided_by=:u,void_reason='test' WHERE id=:p"),params)
        # the same payment again is allowed once the first one is voided
        c.execute(text(row(pid='gen_random_uuid()')),params)
        with pytest.raises(IntegrityError):
            with c.begin_nested():
                c.execute(text("UPDATE student_groups SET monthly_fee=-5 WHERE id=:m"),params)
    # RESTRICT: a membership (and so its group/student) with payments cannot be deleted.
    for sql in ("DELETE FROM student_groups WHERE id=:m","DELETE FROM groups WHERE id=:g","DELETE FROM students WHERE id=:s"):
        with pytest.raises(IntegrityError):
            with engine.begin() as c:
                c.execute(text(sql),params)
    with engine.begin() as c:
        c.execute(text("UPDATE student_groups SET monthly_fee=450000 WHERE id=:m"),params)
    # Round trip: downgrade drops payments and the fee columns, keeps memberships.
    command.downgrade(config,'s9t0u1v2w3x')
    assert 'student_payments' not in set(inspect(engine).get_table_names())
    assert not ({'monthly_fee','monthly_fee_updated_by','monthly_fee_updated_at','transferred_at'} & {c['name'] for c in inspect(engine).get_columns('student_groups')})
    with engine.connect() as c:
        assert c.execute(text('SELECT version_num FROM alembic_version')).scalar_one()=='s9t0u1v2w3x'
        assert c.execute(text('SELECT count(*) FROM student_groups WHERE id=:m'),params).scalar_one()==1
    command.upgrade(config,'head')
    command.upgrade(config,'head')  # idempotent re-run
    with engine.connect() as c:
        assert c.execute(text('SELECT count(*) FROM student_payments')).scalar_one()==0
        assert c.execute(text('SELECT monthly_fee FROM student_groups WHERE id=:m'),params).scalar_one() is None
        assert c.execute(text('SELECT version_num FROM alembic_version')).scalar_one()==HEAD


def _check_n13_p21_objects(engine,config,ids):
    """N13 P2.1: carried_from_payment_id / carried_out_at, the duplicate guard, idempotent, round trip."""
    from sqlalchemy.exc import IntegrityError
    from src.models.auth import User
    actor,orig,orig2,carry,carry2=uuid4(),uuid4(),uuid4(),uuid4(),uuid4()
    with Session(engine) as db:
        db.add(User(id=actor,email=f'{actor}@m.invalid',password_hash='synthetic-only',first_name='C',last_name='C',role='owner'))
        db.commit()
    params={'o':ids['org'],'m':ids['membership'],'s':ids['student'],'g':ids['group'],'u':actor,'p':orig,'p2':orig2,'c':carry,'c2':carry2}
    insert=("INSERT INTO student_payments (id,org_id,membership_id,student_id,group_id,amount,paid_on,valid_from,valid_to,lessons_count,created_by,carried_from_payment_id) "
            "VALUES ({pid},:o,:m,:s,:g,{amount},'2026-10-05','{vf}',{valid_to},NULL,:u,{src})")
    def row(pid,amount,src,vf='2026-10-05',valid_to="'2026-11-04'"):
        return insert.format(pid=pid,amount=amount,src=src,vf=vf,valid_to=valid_to)
    def cols(): return {c['name'] for c in inspect(engine).get_columns('student_payments')}
    def index_names(): return {ix['name'] for ix in inspect(engine).get_indexes('student_payments')}
    assert {'carried_from_payment_id','carried_out_at'} <= cols()
    assert {'idx_student_payments_carried_from','uq_student_payments_active_dup'} <= index_names()
    with engine.begin() as c:
        c.execute(text(row(':p','450000','NULL')),params)
        c.execute(text(row(':p2','450001','NULL',vf='2026-10-06')),params)
        # a carry-over of the same figures but amount 0 and a later start is not a duplicate of the original
        c.execute(text(row(':c','0',':p',vf='2026-10-20')),params)
        # identical figures from ANOTHER source are a different payment (A -> B -> A -> B within one day)
        c.execute(text(row(':c2','0',':p2',vf='2026-10-20')),params)
        c.execute(text("UPDATE student_payments SET carried_out_at='2026-10-20 10:00:00' WHERE id=:p"),params)
    bad={
        'unknown source payment':row('gen_random_uuid()','0','gen_random_uuid()',vf='2026-10-21'),
        'duplicate carry-over of the same source':row('gen_random_uuid()','0',':p',vf='2026-10-20'),
        'duplicate ordinary payment':row('gen_random_uuid()','450000','NULL'),
    }
    for name,sql in bad.items():
        with pytest.raises(IntegrityError,match='.'):
            with engine.begin() as c:
                c.execute(text(sql),params)
    with engine.connect() as c:
        assert c.execute(text('SELECT carried_from_payment_id FROM student_payments WHERE id=:c'),params).scalar_one()==orig
        assert c.execute(text('SELECT carried_from_payment_id FROM student_payments WHERE id=:p'),params).scalar_one() is None
        assert str(c.execute(text('SELECT carried_out_at FROM student_payments WHERE id=:p'),params).scalar_one())=='2026-10-20 10:00:00'
    carried_out_type=next(col['type'] for col in inspect(engine).get_columns('student_payments') if col['name']=='carried_out_at')
    assert 'TIMESTAMP' in str(carried_out_type).upper()               # the transfer INSTANT, not a day
    # Round trip: the links and stamps are dropped, the rows stay as ordinary zero payments; the
    # pair of identical carry-overs that the old guard would refuse is reduced to one live row.
    command.downgrade(config,'t0u1v2w3x4y')
    assert not ({'carried_from_payment_id','carried_out_at'} & cols())
    assert 'idx_student_payments_carried_from' not in index_names() and 'uq_student_payments_active_dup' in index_names()
    with engine.connect() as c:
        assert c.execute(text('SELECT version_num FROM alembic_version')).scalar_one()=='t0u1v2w3x4y'
        assert c.execute(text('SELECT count(*) FROM student_payments WHERE org_id=:o'),params).scalar_one()==4
        assert c.execute(text('SELECT count(*) FROM student_payments WHERE org_id=:o AND voided_at IS NULL'),params).scalar_one()==3
        reasons=[r for (r,) in c.execute(text("SELECT void_reason FROM student_payments WHERE id IN (:c,:c2) AND voided_at IS NOT NULL"),params)]
        assert len(reasons)==1 and reasons[0].startswith('Откат миграции')
    with pytest.raises(IntegrityError,match='.'):                  # the old guard is back
        with engine.begin() as c:
            c.execute(text("INSERT INTO student_payments (id,org_id,membership_id,student_id,group_id,amount,paid_on,valid_from,valid_to,created_by) "
                           "VALUES (gen_random_uuid(),:o,:m,:s,:g,0,'2026-10-05','2026-10-20','2026-11-04',:u)"),params)
    command.upgrade(config,'head')
    command.upgrade(config,'head')  # idempotent re-run
    # The columns / indexes already exist (baseline/create_all) - the revision must not break.
    command.stamp(config,'t0u1v2w3x4y')
    command.upgrade(config,'head')
    assert {'carried_from_payment_id','carried_out_at'} <= cols()
    with engine.connect() as c:
        assert c.execute(text('SELECT version_num FROM alembic_version')).scalar_one()==HEAD
        assert c.execute(text('SELECT count(*) FROM student_payments WHERE org_id=:o'),params).scalar_one()==4
        assert c.execute(text('SELECT carried_from_payment_id FROM student_payments WHERE id=:c'),params).scalar_one() is None


def _check_n13_p22_objects(engine,config,ids):
    """N13 P2.2: receipt numbers (backfill, unique per organisation), partial_period, cheque files, round trip."""
    from sqlalchemy.exc import IntegrityError
    from src.models.auth import Organization,Group,Student,StudentGroup,User
    actor=uuid4()
    org2,group2,student2,membership2=uuid4(),uuid4(),uuid4(),uuid4()
    with Session(engine) as db:
        db.add(User(id=actor,email=f'{actor}@m.invalid',password_hash='synthetic-only',first_name='R',last_name='R',role='owner'))
        db.add(Organization(id=org2,name='Second centre',status='active')); db.flush()
        db.add(Group(id=group2,org_id=org2,name='G2',class_level=9))
        db.add(Student(id=student2,org_id=org2,first_name='S',last_name='Two',class_level=9,status='active')); db.flush()
        db.add(StudentGroup(id=membership2,student_id=student2,group_id=group2)); db.commit()
    def cols(): return {c['name'] for c in inspect(engine).get_columns('student_payments')}
    def index_names(): return {ix['name'] for ix in inspect(engine).get_indexes('student_payments')}
    assert {'receipt_no','partial_period'} <= cols() and 'uq_student_payments_receipt_no' in index_names()
    assert 'student_payment_files' in set(inspect(engine).get_table_names())
    assert 'idx_student_payment_files_payment' in {ix['name'] for ix in inspect(engine).get_indexes('student_payment_files')}
    # Start from the revision before: no receipts yet, payments of two centres, one voided, one carry-over.
    command.downgrade(config,'t1u2v3w4x5y')
    assert not ({'receipt_no','partial_period'} & cols()) and 'uq_student_payments_receipt_no' not in index_names()
    assert 'student_payment_files' not in set(inspect(engine).get_table_names())
    with engine.begin() as c:
        c.execute(text('DELETE FROM student_payments'))
    pays={k:uuid4() for k in ('a1','a2','a3','a4','carry','b1','b2')}
    org,m,s,g=ids['org'],ids['membership'],ids['student'],ids['group']
    with engine.begin() as c:
        # the created_at order is a3, a1(voided), a2, carry, a4; the ids are random on purpose
        c.execute(text("INSERT INTO student_payments (id,org_id,membership_id,student_id,group_id,amount,paid_on,valid_from,valid_to,created_by,created_at) VALUES (:id,:o,:m,:s,:g,3,'2026-09-01','2026-09-01','2026-09-30',:u,'2026-09-01 08:00:00')"),{'id':pays['a3'],'o':org,'m':m,'s':s,'g':g,'u':actor})
        c.execute(text("INSERT INTO student_payments (id,org_id,membership_id,student_id,group_id,amount,paid_on,valid_from,valid_to,created_by,created_at,voided_at,voided_by,void_reason) VALUES (:id,:o,:m,:s,:g,1,'2026-09-05','2026-09-05','2026-10-04',:u,'2026-09-05 08:00:00',now(),:u,'ошибка')"),{'id':pays['a1'],'o':org,'m':m,'s':s,'g':g,'u':actor})
        c.execute(text("INSERT INTO student_payments (id,org_id,membership_id,student_id,group_id,amount,paid_on,valid_from,valid_to,created_by,created_at) VALUES (:id,:o,:m,:s,:g,2,'2026-10-05','2026-10-05','2026-11-04',:u,'2026-10-05 08:00:00')"),{'id':pays['a2'],'o':org,'m':m,'s':s,'g':g,'u':actor})
        c.execute(text("INSERT INTO student_payments (id,org_id,membership_id,student_id,group_id,amount,paid_on,valid_from,valid_to,created_by,created_at,carried_from_payment_id) VALUES (:id,:o,:m,:s,:g,0,'2026-10-05','2026-10-20','2026-11-04',:u,'2026-10-20 08:00:00',:src)"),{'id':pays['carry'],'o':org,'m':m,'s':s,'g':g,'u':actor,'src':pays['a2']})
        c.execute(text("INSERT INTO student_payments (id,org_id,membership_id,student_id,group_id,amount,paid_on,valid_from,valid_to,created_by,created_at) VALUES (:id,:o,:m,:s,:g,4,'2026-10-21','2026-10-21','2026-11-20',:u,'2026-10-21 08:00:00')"),{'id':pays['a4'],'o':org,'m':m,'s':s,'g':g,'u':actor})
        for key,at,amount in (('b2','2026-10-03 09:00:00',8),('b1','2026-10-01 09:00:00',7)):
            c.execute(text("INSERT INTO student_payments (id,org_id,membership_id,student_id,group_id,amount,paid_on,valid_from,valid_to,created_by,created_at) VALUES (:id,:o,:m,:s,:g,:amount,'2026-10-05','2026-10-05','2026-11-04',:u,:at)"),{'id':pays[key],'o':org2,'m':membership2,'s':student2,'g':group2,'u':actor,'at':at,'amount':amount})
    command.upgrade(config,'head')
    with engine.connect() as c:
        numbers={k:c.execute(text('SELECT receipt_no FROM student_payments WHERE id=:i'),{'i':v}).scalar_one() for k,v in pays.items()}
        partial=c.execute(text('SELECT count(*) FROM student_payments WHERE partial_period')).scalar_one()
        version=c.execute(text('SELECT version_num FROM alembic_version')).scalar_one()
    # by created_at inside each organisation, the voided one included, the carry-over without a number
    assert numbers=={'a3':1,'a1':2,'a2':3,'carry':None,'a4':4,'b1':1,'b2':2}, numbers
    assert partial==0 and version==HEAD
    # new rows: NULL receipt allowed (carry-over), partial_period defaults to false, numbers are unique per centre only
    with engine.begin() as c:
        c.execute(text("INSERT INTO student_payments (id,org_id,membership_id,student_id,group_id,amount,paid_on,valid_from,valid_to,created_by,receipt_no) VALUES (gen_random_uuid(),:o,:m,:s,:g,9,'2026-12-01','2026-12-01','2026-12-31',:u,5)"),{'o':org,'m':m,'s':s,'g':g,'u':actor})
        c.execute(text("INSERT INTO student_payments (id,org_id,membership_id,student_id,group_id,amount,paid_on,valid_from,valid_to,created_by,receipt_no) VALUES (gen_random_uuid(),:o,:m,:s,:g,9,'2026-12-01','2026-12-01','2026-12-31',:u,5)"),{'o':org2,'m':membership2,'s':student2,'g':group2,'u':actor})
        c.execute(text("INSERT INTO student_payments (id,org_id,membership_id,student_id,group_id,amount,paid_on,valid_from,valid_to,created_by) VALUES (gen_random_uuid(),:o,:m,:s,:g,10,'2026-12-01','2026-12-01','2026-12-31',:u)"),{'o':org,'m':m,'s':s,'g':g,'u':actor})
        c.execute(text("INSERT INTO student_payments (id,org_id,membership_id,student_id,group_id,amount,paid_on,valid_from,valid_to,created_by) VALUES (gen_random_uuid(),:o,:m,:s,:g,11,'2026-12-01','2026-12-01','2026-12-31',:u)"),{'o':org,'m':m,'s':s,'g':g,'u':actor})
    with pytest.raises(IntegrityError,match='uq_student_payments_receipt_no'):
        with engine.begin() as c:
            c.execute(text("INSERT INTO student_payments (id,org_id,membership_id,student_id,group_id,amount,paid_on,valid_from,valid_to,created_by,receipt_no) VALUES (gen_random_uuid(),:o,:m,:s,:g,12,'2026-12-01','2026-12-01','2026-12-31',:u,5)"),{'o':org,'m':m,'s':s,'g':g,'u':actor})
    with engine.connect() as c:
        assert c.execute(text('SELECT count(*) FROM student_payments WHERE org_id=:o AND receipt_no IS NULL'),{'o':org}).scalar_one()==3   # the carry-over and two numberless rows
    # cheque files: size check, cascade with the payment, the index on payment_id
    f=uuid4()
    file_sql=("INSERT INTO student_payment_files (id,org_id,payment_id,filename,content_type,size_bytes,data,uploaded_by,uploaded_at) "
              "VALUES ({id},:o,:p,'c.png','image/png',{size},'\\x89504e47',:u,now())")
    with engine.begin() as c:
        c.execute(text(file_sql.format(id=':f',size='4')),{'f':f,'o':org,'p':pays['a4'],'u':actor})
    for size in ('0','2097153'):
        with pytest.raises(IntegrityError,match='.'):
            with engine.begin() as c:
                c.execute(text(file_sql.format(id='gen_random_uuid()',size=size)),{'o':org,'p':pays['a4'],'u':actor})
    with pytest.raises(IntegrityError,match='.'):                  # a file needs a payment
        with engine.begin() as c:
            c.execute(text(file_sql.format(id='gen_random_uuid()',size='4')),{'o':org,'p':uuid4(),'u':actor})
    with engine.begin() as c:
        c.execute(text(file_sql.format(id='gen_random_uuid()',size='4')),{'o':org,'p':pays['a3'],'u':actor})
    # Round trip: the downgrade leaves the payments and removes numbers, the flag and the files without a trace.
    command.downgrade(config,'t1u2v3w4x5y')
    assert not ({'receipt_no','partial_period'} & cols()) and 'uq_student_payments_receipt_no' not in index_names()
    assert 'student_payment_files' not in set(inspect(engine).get_table_names())
    with engine.connect() as c:
        assert c.execute(text('SELECT version_num FROM alembic_version')).scalar_one()=='t1u2v3w4x5y'
        assert c.execute(text('SELECT count(*) FROM student_payments WHERE org_id=:o'),{'o':org}).scalar_one()==8
    command.upgrade(config,'head')
    command.upgrade(config,'head')  # idempotent re-run
    # Everything exists already (a database built from the models): the revision must neither fail nor renumber.
    command.stamp(config,'t1u2v3w4x5y')
    command.upgrade(config,'head')
    with engine.connect() as c:
        assert c.execute(text('SELECT version_num FROM alembic_version')).scalar_one()==HEAD
        again=[n for (n,) in c.execute(text('SELECT receipt_no FROM student_payments WHERE org_id=:o AND receipt_no IS NOT NULL ORDER BY receipt_no'),{'o':org})]
        assert again==sorted(again) and len(again)==len(set(again)) and len(again)==7      # renumbered once by the downgrade/upgrade, consistently; the carry-over has none
        assert c.execute(text('SELECT count(*) FROM student_payment_files')).scalar_one()==0


def _check_n13_p3_objects(engine,config,ids):
    """N13 P3: groups.lesson_type (default, CHECK) and the payroll tables (constraints, cascade); round trip."""
    from sqlalchemy.exc import IntegrityError
    from src.models.auth import User
    actor=uuid4()
    org,group=ids['org'],ids['group']
    with Session(engine) as db:
        db.add(User(id=actor,email=f'{actor}@m.invalid',password_hash='synthetic-only',first_name='P',last_name='3',role='owner'))
        db.commit()
    def group_cols(): return {c['name'] for c in inspect(engine).get_columns('groups')}
    def tables(): return set(inspect(engine).get_table_names())
    assert 'lesson_type' in group_cols() and {'payroll_sheets','payroll_sheet_revisions'} <= tables()
    with engine.connect() as c:
        assert c.execute(text('SELECT lesson_type FROM groups WHERE id=:g'),{'g':group}).scalar_one()=='group'   # existing groups
        assert c.execute(text("SELECT count(*) FROM groups WHERE lesson_type<>'group'")).scalar_one()==0
    for kind in ('mini_group','pair','individual'):
        with engine.begin() as c:
            c.execute(text('UPDATE groups SET lesson_type=:k WHERE id=:g'),{'k':kind,'g':group})
    with pytest.raises(IntegrityError,match='ck_groups_lesson_type'):
        with engine.begin() as c:
            c.execute(text("UPDATE groups SET lesson_type='bogus' WHERE id=:g"),{'g':group})
    with pytest.raises(IntegrityError,match='.'):
        with engine.begin() as c:
            c.execute(text('UPDATE groups SET lesson_type=NULL WHERE id=:g'),{'g':group})
    with engine.begin() as c:
        c.execute(text("UPDATE groups SET lesson_type='pair' WHERE id=:g"),{'g':group})
    sheet,revision=uuid4(),uuid4()
    insert=("INSERT INTO payroll_sheets (id,org_id,teacher_id,period_month,source_fingerprint,created_by,updated_by{extra}) "
            "VALUES ({id},:o,:t,{month},'abc',:t,:t{values})")
    def add_sheet(id_=':s',month="'2026-09-01'",extra='',values=''):
        with engine.begin() as c:
            c.execute(text(insert.format(id=id_,month=month,extra=extra,values=values)),{'s':sheet,'o':org,'t':actor})
    add_sheet()
    with engine.connect() as c:
        row=c.execute(text('SELECT revision,total_amount,divisor_default,lines::text,percent_inputs::text FROM payroll_sheets WHERE id=:s'),{'s':sheet}).one()
        assert tuple(row)==(1,0,12,'[]','{}')                                  # the defaults
    for what,call in {
        'one sheet per teacher and month':lambda: add_sheet('gen_random_uuid()'),
        'first day of the month':lambda: add_sheet('gen_random_uuid()',"'2026-10-15'"),
        'total >= 0':lambda: add_sheet('gen_random_uuid()',"'2026-08-01'",',total_amount',',-1'),
        'revision >= 1':lambda: add_sheet('gen_random_uuid()',"'2026-08-01'",',revision',',0'),
        'divisor 1..31':lambda: add_sheet('gen_random_uuid()',"'2026-08-01'",',divisor_default',',32'),
    }.items():
        with pytest.raises(IntegrityError,match='.'):
            call()
    rev=("INSERT INTO payroll_sheet_revisions (id,sheet_id,revision,changed_by) VALUES ({id},:s,{n},:t)")
    with engine.begin() as c:
        c.execute(text(rev.format(id=':r',n=1)),{'r':revision,'s':sheet,'t':actor})
    with pytest.raises(IntegrityError,match='uq_payroll_sheet_revision'):
        with engine.begin() as c:
            c.execute(text(rev.format(id='gen_random_uuid()',n=1)),{'s':sheet,'t':actor})
    with pytest.raises(IntegrityError,match='.'):                              # a revision needs its sheet
        with engine.begin() as c:
            c.execute(text(rev.format(id='gen_random_uuid()',n=2)),{'s':uuid4(),'t':actor})
    with engine.begin() as c:                                                  # the history goes with the sheet
        c.execute(text('DELETE FROM payroll_sheets WHERE id=:s'),{'s':sheet})
    with engine.connect() as c:
        assert c.execute(text('SELECT count(*) FROM payroll_sheet_revisions WHERE sheet_id=:s'),{'s':sheet}).scalar_one()==0
    add_sheet()
    with engine.begin() as c:
        c.execute(text(rev.format(id=':r',n=1)),{'r':revision,'s':sheet,'t':actor})
    # Round trip: the downgrade removes the tables and the column and leaves the groups themselves.
    command.downgrade(config,'u1v2w3x4y5z')
    assert 'lesson_type' not in group_cols() and not ({'payroll_sheets','payroll_sheet_revisions'} & tables())
    with engine.connect() as c:
        assert c.execute(text('SELECT version_num FROM alembic_version')).scalar_one()=='u1v2w3x4y5z'
        assert c.execute(text('SELECT count(*) FROM groups WHERE id=:g'),{'g':group}).scalar_one()==1
    command.upgrade(config,'head')
    command.upgrade(config,'head')  # idempotent re-run
    assert 'lesson_type' in group_cols() and {'payroll_sheets','payroll_sheet_revisions'} <= tables()
    with engine.connect() as c:
        assert c.execute(text('SELECT lesson_type FROM groups WHERE id=:g'),{'g':group}).scalar_one()=='group'
        assert c.execute(text('SELECT count(*) FROM payroll_sheets')).scalar_one()==0          # the sheets were dropped by the rollback
    # Everything exists already (a database built from the models): the revision must not fail.
    add_sheet()
    command.stamp(config,'u1v2w3x4y5z')
    command.upgrade(config,'head')
    with engine.connect() as c:
        assert c.execute(text('SELECT version_num FROM alembic_version')).scalar_one()==HEAD
        assert c.execute(text('SELECT count(*) FROM payroll_sheets')).scalar_one()==1           # and it kept what was there


def _check_n13_archive_objects(engine,config,ids,monkeypatch):
    """«Ученик ушёл из центра»: the stamp columns, the audit actions, and the one-off clean-up of archived students."""
    from sqlalchemy.exc import IntegrityError
    from src.models.auth import Group,GroupAudit,Student,StudentGroup,User
    actor,group2=uuid4(),uuid4()
    org,group=ids['org'],ids['group']
    a,b,c,d=(uuid4() for _ in range(4))             # archived with two active memberships / archived with a closed one / ACTIVE (control) / archived, nothing
    ma1,ma2,mb,mc=(uuid4() for _ in range(4))
    pay=uuid4()
    def student_cols(): return {col['name']:col for col in inspect(engine).get_columns('students')}
    def audit_insert(action):
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO group_audit (id,org_id,group_id,actor_id,actor_role,action,snapshot) VALUES (gen_random_uuid(),:o,:g,:a,'admin',:x,'{}'::jsonb)"),
                         {'o':org,'g':group,'a':actor,'x':action})
    with Session(engine) as db:
        db.add(User(id=actor,email=f'{actor}@m.invalid',password_hash='synthetic-only',first_name='A',last_name='R',role='owner')); db.commit()
    cols=student_cols()
    assert {'archived_at','archive_reason'} <= set(cols) and cols['archived_at']['nullable'] and cols['archive_reason']['nullable']
    assert str(cols['archive_reason']['type'])=='VARCHAR(300)'
    for action in ('student_archived','student_restored','updated'):
        audit_insert(action)
    with pytest.raises(IntegrityError,match='ck_group_audit_action'):
        audit_insert('student_deleted')
    # Roll back to the previous head: the old CHECK is back (new rows only), the columns and the rows stay.
    command.downgrade(config,PREVIOUS)
    with engine.connect() as conn:
        assert conn.execute(text('SELECT version_num FROM alembic_version')).scalar_one()==PREVIOUS
        assert conn.execute(text("SELECT count(*) FROM group_audit WHERE action IN ('student_archived','student_restored')")).scalar_one()==2
    assert {'archived_at','archive_reason'} <= set(student_cols())
    with pytest.raises(IntegrityError,match='ck_group_audit_action'):
        audit_insert('student_archived')
    audit_insert('teacher_changed')
    # The data as the generic PUT left it: archived students that still sit in groups.
    with Session(engine) as db:
        db.add(Group(id=group2,org_id=org,name='Archive group 2',class_level=9)); db.flush()
        for sid,status,primary,name in ((a,'archived',group,'A'),(b,'archived',group2,'B'),(c,'active',group,'C'),(d,'archived',None,'D')):
            db.add(Student(id=sid,org_id=org,first_name=name,last_name='Archive',class_level=9,status=status,group_id=primary))
        db.flush()
        db.add(StudentGroup(id=ma1,student_id=a,group_id=group,is_active=True))
        db.add(StudentGroup(id=ma2,student_id=a,group_id=group2,is_active=True))
        db.add(StudentGroup(id=mb,student_id=b,group_id=group2,is_active=False,left_at=datetime(2026,9,1,10,0,0)))
        db.add(StudentGroup(id=mc,student_id=c,group_id=group,is_active=True))
        db.commit()
    with engine.begin() as conn:
        conn.execute(text("UPDATE students SET updated_at='2026-09-02 08:00:00' WHERE id IN (:a,:b,:d)"),{'a':a,'b':b,'d':d})
        conn.execute(text("INSERT INTO student_payments (id,org_id,membership_id,student_id,group_id,amount,paid_on,valid_from,valid_to,created_by,receipt_no) VALUES (:p,:o,:m,:s,:g,450000,'2026-09-01','2026-09-01','2026-09-30',:u,777001)"),
                     {'p':pay,'o':org,'m':ma1,'s':a,'g':group,'u':actor})
    def snapshot():
        with engine.connect() as conn:
            members={r.id:(r.is_active,r.left_at) for r in conn.execute(text('SELECT id,is_active,left_at FROM student_groups WHERE id IN (:a,:b,:c,:d)'),{'a':ma1,'b':ma2,'c':mb,'d':mc})}
            students={r.id:(r.status,r.group_id,r.updated_at,r.archived_at,r.archive_reason) for r in conn.execute(text('SELECT id,status,group_id,updated_at,archived_at,archive_reason FROM students WHERE id IN (:a,:b,:c,:d)'),{'a':a,'b':b,'c':c,'d':d})}
            paid=conn.execute(text('SELECT count(*) FROM student_payments WHERE id=:p AND membership_id=:m AND amount=450000'),{'p':pay,'m':ma1}).scalar_one()
            total=conn.execute(text('SELECT (SELECT count(*) FROM students),(SELECT count(*) FROM student_groups)')).one()
        return members,students,paid,tuple(total)
    dirty=snapshot()
    assert dirty[0][ma1]==(True,None) and dirty[0][ma2]==(True,None) and dirty[1][a][1]==group and dirty[1][b][1]==group2
    command.upgrade(config,'head')
    clean=snapshot()
    members,students,paid,total=clean
    assert members[ma1][0] is False and members[ma1][1] is not None and members[ma2][0] is False and members[ma2][1]==members[ma1][1]   # closed, one instant
    assert members[mb]==(False,datetime(2026,9,1,10,0,0))                      # an already closed membership keeps its day
    assert members[mc]==(True,None)                                            # the active student is untouched ...
    assert students[c][:2]==('active',group) and students[a][:2]==('archived',None) and students[b][:2]==('archived',None) and students[d][:2]==('archived',None)
    assert [students[x][2] for x in (a,b,d)]==[datetime(2026,9,2,8,0,0)]*3     # updated_at: the only date of an old archiving
    legacy=datetime(2026,9,2,8,0,0)                                            # the server is UTC: updated_at as it is
    assert [students[x][3:] for x in (a,b,c,d)]==[(legacy,None),(legacy,None),(None,None),(legacy,None)]   # archived ones are stamped, no reason invented, the active one untouched
    assert paid==1 and total==dirty[3]                                         # payments, students and memberships are all still there
    with engine.connect() as conn:
        assert conn.execute(text('SELECT version_num FROM alembic_version')).scalar_one()==HEAD
        assert conn.execute(text("SELECT count(*) FROM group_audit WHERE action IN ('student_archived','student_restored')")).scalar_one()==2   # the old rows stayed through the round trip
    audit_insert('student_archived'); audit_insert('student_restored')         # the CHECK accepts the new actions again
    with pytest.raises(IntegrityError,match='ck_group_audit_action'):
        audit_insert('student_deleted')
    # Second run is a no-op ...
    command.upgrade(config,'head')
    assert snapshot()==clean
    # ... and up / down / up on dirty data again: an archived student with an active membership is cleaned once more.
    command.downgrade(config,PREVIOUS)
    assert snapshot()==clean                                                   # the downgrade touches no data
    with engine.begin() as conn:
        conn.execute(text('UPDATE student_groups SET is_active=true,left_at=NULL WHERE id=:m'),{'m':ma2})
        conn.execute(text('UPDATE students SET group_id=:g WHERE id=:s'),{'g':group2,'s':a})
    command.upgrade(config,'head')
    members,students,paid,total=snapshot()
    assert members[ma2][0] is False and members[ma2][1] is not None and members[ma2][1]>members[ma1][1]   # closed now
    assert members[ma1]==clean[0][ma1] and students[a][:2]==('archived',None) and students[c][:2]==('active',group) and paid==1
    # The stamp converts updated_at (written by the database in ITS time zone) to naive UTC: a server in another zone.
    command.downgrade(config,PREVIOUS)
    with engine.begin() as conn:
        conn.execute(text('UPDATE students SET archived_at=NULL WHERE id IN (:a,:d)'),{'a':a,'d':d})
    # Alembic pins its session to UTC (#49), so a UTC+5 session no longer happens on its own: switch the pin off to keep
    # proving that the stamp is right for rows written by a server in another zone.
    monkeypatch.setenv('PGOPTIONS','-c timezone=Asia/Tashkent')                 # the migration's own connection is a UTC+5 session
    with monkeypatch.context() as unpinned:
        unpinned.setattr('src.core.db_timezone.utc_connect_args',lambda url,connect_args=None: dict(connect_args or {}))
        try:
            command.upgrade(config,'head')
        finally:
            monkeypatch.delenv('PGOPTIONS')
    students=snapshot()[1]
    assert students[a][3]==datetime(2026,9,2,3,0,0) and students[d][3]==datetime(2026,9,2,3,0,0)   # 08:00 Tashkent = 03:00 UTC
    assert students[b][3]==legacy and students[c][3] is None                   # an existing stamp is never rewritten
    # #49: with the pin on, the same UTC+5 environment cannot move the migration out of UTC - the session is UTC, so the
    # stored 08:00 is already UTC (the stamp must not shift by the environment's zone).
    command.downgrade(config,PREVIOUS)
    with engine.begin() as conn:
        conn.execute(text('UPDATE students SET archived_at=NULL WHERE id IN (:a,:d)'),{'a':a,'d':d})
    monkeypatch.setenv('PGOPTIONS','-c timezone=Asia/Tashkent')
    try:
        command.upgrade(config,'head')
    finally:
        monkeypatch.delenv('PGOPTIONS')
    students=snapshot()[1]
    assert students[a][3]==datetime(2026,9,2,8,0,0) and students[d][3]==datetime(2026,9,2,8,0,0)
    assert students[b][3]==legacy and students[c][3] is None
    # A database built from the models (columns and CHECK exist already), then stamped: the revision must not fail.
    command.stamp(config,PREVIOUS)
    command.upgrade(config,'head')
    with engine.connect() as conn:
        assert conn.execute(text('SELECT version_num FROM alembic_version')).scalar_one()==HEAD
    assert str(student_cols()['archive_reason']['type'])=='VARCHAR(300)'
    # Real data written through the model with the new columns.
    with Session(engine) as db:
        db.add(Student(id=uuid4(),org_id=org,first_name='Z',last_name='Stamp',class_level=9,status='archived',
                       archived_at=datetime(2026,10,16,9,0,0),archive_reason='Переехал'))
        db.commit()
