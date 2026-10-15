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
        assert connection.execute(text('SELECT version_num FROM alembic_version')).scalar_one()=='u1v2w3x4y5z'
    assert 'student_payments' in set(inspect(engine).get_table_names())
    assert {'monthly_fee','monthly_fee_updated_by','monthly_fee_updated_at','transferred_at'} <= {c['name'] for c in inspect(engine).get_columns('student_groups')}
    _check_n13_p1_objects(engine,config,ids)
    _check_n13_p2_objects(engine,config,ids)
    _check_n13_p21_objects(engine,config,ids)
    _check_n13_p22_objects(engine,config,ids)
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
        assert c.execute(text('SELECT version_num FROM alembic_version')).scalar_one()=='u1v2w3x4y5z'
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
        assert c.execute(text('SELECT version_num FROM alembic_version')).scalar_one()=='u1v2w3x4y5z'


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
        assert c.execute(text('SELECT version_num FROM alembic_version')).scalar_one()=='u1v2w3x4y5z'
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
    assert partial==0 and version=='u1v2w3x4y5z'
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
        assert c.execute(text('SELECT version_num FROM alembic_version')).scalar_one()=='u1v2w3x4y5z'
        again=[n for (n,) in c.execute(text('SELECT receipt_no FROM student_payments WHERE org_id=:o AND receipt_no IS NOT NULL ORDER BY receipt_no'),{'o':org})]
        assert again==sorted(again) and len(again)==len(set(again)) and len(again)==7      # renumbered once by the downgrade/upgrade, consistently; the carry-over has none
        assert c.execute(text('SELECT count(*) FROM student_payment_files')).scalar_one()==0

