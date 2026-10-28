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


def legacy_add(db,model,**values):
    """Insert a row of ``model`` through Core, with the columns given plus the model's own defaults.  The DB of this test sits at many
    revisions: an ORM insert names EVERY mapped column, also the ones of revisions newer than the one the DB is at (the console pack added
    ``users.platform_block*`` and ``organizations.blocked_*``); Core names only what it is given."""
    db.execute(model.__table__.insert().values(**values))
HEAD='p154a1b2c3d4'          # W1 (live day off range, restore audit) on top of pf1a2b3c4d5 (PAY-F) on top of cn1a2b3c4d5 (console pack «Консоль-1»)
LOGICB2_HEAD='lb3c4d5e6f7'  # LOGIC-B2: exam release marker, substitution requests
LOGICB2='lb2b3c4d5e6'       # LOGIC-B (2/2): group archive, member audit, invitations
LOGICB1='lb1a2b3c4d5'       # LOGIC-B (1/2): marks version, lesson teachers, validFrom
LOGICA='f2a3b4c5d6e'        # LOGIC-A: pauses, opening balances, payouts, divisor / lesson-type history
PAY2='e1f2a3b4c5d'          # PAY-2: payment = money (the revision before LOGIC-A)
PAYROLL_RATES='d0e1f2a3b4c' # payroll rates (UX-2): the revision before PAY-2
EXCUSE='c9d0e1f2a3b'        # excuse decision mark (N13 P4, #39): the revision before the payroll rates
PRICES='b8c9d0e1f2a'        # group price + individual prices (N13 P4, #105): the revision before the decision mark
SESSIONS='a7b8c9d0e1f'      # user sessions (N13 tails, #101): the revision before the group price
PERIODS='z6a7b8c9d0e'       # membership periods (N13 tails, #59): the revision before the sessions
INVITES='y5z6a7b8c9d'       # diagnostic invite «cancelled» (N13 tails, #89)
ARCHIVE='x4y5z6a7b8c'       # student archive (N13)
PREVIOUS='w3x4y5z6a7b'      # lesson type and payroll (N13 P3): the revision before the archive


def _group_row(db,id,org_id,name,class_level=9,created_at=None):
    """A group inserted with the oldest columns only: the DB of this test sits at many revisions, and the ORM would
    insert columns of the newer ones (LOGIC-B: ``inactive_intervals``, ``archive_reason``)."""
    db.execute(text("INSERT INTO groups (id,org_id,name,class_level,created_at) VALUES (:i,:o,:n,:c,coalesce(:t,now()))"),
               {'i':id,'o':org_id,'n':name,'c':class_level,'t':created_at})


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
        legacy_add(db,Organization,id=ids['org'],name='Migration sentinel',status='active'); db.flush()
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
    _check_n13_invite_cancelled_objects(engine,config,ids)
    _check_n13_periods_objects(engine,config,ids)
    _check_n13_sessions_objects(engine,config,ids)
    _check_n13_group_price_objects(engine,config,ids)
    _check_n13_excuse_mark_objects(engine,config,ids)
    _check_ux2_payroll_rates_objects(engine,config,ids)
    _check_pay2_objects(engine,config,ids)
    _check_logic_a_objects(engine,config,ids)
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
        legacy_add(db,User,id=teacher,email=f'{teacher}@m.invalid',password_hash='synthetic-only',first_name='T',last_name='T',role='teacher')
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
        legacy_add(db,User,id=actor,email=f'{actor}@m.invalid',password_hash='synthetic-only',first_name='P',last_name='P',role='owner')
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
        # PAY-2: 'neither period' and 'an identical live payment' are legal now (a payment is a sum, no period)
        'both periods':row(pid='gen_random_uuid()',lessons='12',amount='3'),
        'valid_to before valid_from':row(pid='gen_random_uuid()',valid_to="'2026-10-04'",amount='4'),
        'partial void':row(pid='gen_random_uuid()',amount='5',valid_to="'2026-12-05'",extra=',voided_at',extra_v=',now()'),
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
        legacy_add(db,User,id=actor,email=f'{actor}@m.invalid',password_hash='synthetic-only',first_name='C',last_name='C',role='owner')
        db.commit()
    params={'o':ids['org'],'m':ids['membership'],'s':ids['student'],'g':ids['group'],'u':actor,'p':orig,'p2':orig2,'c':carry,'c2':carry2}
    insert=("INSERT INTO student_payments (id,org_id,membership_id,student_id,group_id,amount,paid_on,valid_from,valid_to,lessons_count,created_by,carried_from_payment_id) "
            "VALUES ({pid},:o,:m,:s,:g,{amount},'2026-10-05','{vf}',{valid_to},NULL,:u,{src})")
    def row(pid,amount,src,vf='2026-10-05',valid_to="'2026-11-04'"):
        return insert.format(pid=pid,amount=amount,src=src,vf=vf,valid_to=valid_to)
    def cols(): return {c['name'] for c in inspect(engine).get_columns('student_payments')}
    def index_names(): return {ix['name'] for ix in inspect(engine).get_indexes('student_payments')}
    assert {'carried_from_payment_id','carried_out_at'} <= cols()
    assert 'idx_student_payments_carried_from' in index_names() and 'uq_student_payments_active_dup' not in index_names()   # PAY-2 dropped the old guard
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
        legacy_add(db,User,id=actor,email=f'{actor}@m.invalid',password_hash='synthetic-only',first_name='R',last_name='R',role='owner')
        legacy_add(db,Organization,id=org2,name='Second centre',status='active'); db.flush()
        _group_row(db,group2,org2,'G2',9)
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
        legacy_add(db,User,id=actor,email=f'{actor}@m.invalid',password_hash='synthetic-only',first_name='P',last_name='3',role='owner')
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
        legacy_add(db,User,id=actor,email=f'{actor}@m.invalid',password_hash='synthetic-only',first_name='A',last_name='R',role='owner'); db.commit()
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
        _group_row(db,group2,org,'Archive group 2',9); db.flush()
        for sid,status,primary,name in ((a,'archived',group,'A'),(b,'archived',group2,'B'),(c,'active',group,'C'),(d,'archived',None,'D')):
            db.add(Student(id=sid,org_id=org,first_name=name,last_name='Archive',class_level=9,status=status,group_id=primary))
        db.flush()
        db.commit()
    with engine.begin() as conn:    # raw SQL: the database is at an OLDER head here, the models are not
        conn.execute(text("INSERT INTO student_groups (id,student_id,group_id,is_active,joined_at) VALUES (:i,:s,:g,true,now())"),{'i':ma1,'s':a,'g':group})
        conn.execute(text("INSERT INTO student_groups (id,student_id,group_id,is_active,joined_at) VALUES (:i,:s,:g,true,now())"),{'i':ma2,'s':a,'g':group2})
        conn.execute(text("INSERT INTO student_groups (id,student_id,group_id,is_active,joined_at,left_at) VALUES (:i,:s,:g,false,now(),'2026-09-01 10:00:00')"),{'i':mb,'s':b,'g':group2})
        conn.execute(text("INSERT INTO student_groups (id,student_id,group_id,is_active,joined_at) VALUES (:i,:s,:g,true,now())"),{'i':mc,'s':c,'g':group})
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


def _check_n13_invite_cancelled_objects(engine,config,ids):
    """#89: ``cancelled`` is a valid invite status, with its date and reason; the downgrade maps it to ``expired``."""
    from sqlalchemy.exc import IntegrityError
    from src.models.auth import User
    org,student,creator=ids['org'],ids['student'],uuid4()
    with Session(engine) as db:
        legacy_add(db,User,id=creator,email=f'{creator}@m.invalid',password_hash='synthetic-only',first_name='I',last_name='C',role='owner'); db.commit()
    def cols(): return {c['name']:c for c in inspect(engine).get_columns('diagnostic_invites')}
    def add(status,code,reason=None):
        """Raw SQL: the models describe the NEWEST head, the database may be at an older one."""
        extra=cols().keys() & {'cancelled_at','cancel_reason'}
        names=',cancelled_at,cancel_reason' if extra else ''
        values=",CASE WHEN :st='cancelled' THEN now() END,:r" if extra else ''
        params={'o':org,'s':student,'u':creator,'c':code,'st':status,'r':reason}
        with engine.begin() as conn:
            conn.execute(text(f"INSERT INTO diagnostic_invites (id,org_id,student_id,created_by,code,class_level,status,expires_at{names}) "
                              f"VALUES (gen_random_uuid(),:o,:s,:u,:c,9,:st,now()+interval '1 day'{values})"),params)
    def status_of(code):
        with engine.connect() as conn:
            return conn.execute(text('SELECT status FROM diagnostic_invites WHERE code=:c'),{'c':code}).scalar_one()
    c=cols()
    assert {'cancelled_at','cancel_reason','revived_at'} <= set(c) and c['cancelled_at']['nullable'] and c['cancel_reason']['nullable'] and c['revived_at']['nullable']
    assert str(c['revived_at']['type'])=='TIMESTAMP'
    assert str(c['cancel_reason']['type'])=='VARCHAR(50)' and str(c['cancelled_at']['type'])=='TIMESTAMP'
    for status,code in (('pending','M89PEND'),('in_progress','M89PROG'),('expired','M89EXPI')):
        add(status,code)
    add('cancelled','M89CANC','student_archived')
    with pytest.raises(IntegrityError,match='ck_diagnostic_invites_status'):
        add('bogus','M89BOGU')
    with engine.connect() as conn:
        assert conn.execute(text("SELECT cancel_reason FROM diagnostic_invites WHERE code='M89CANC' AND cancelled_at IS NOT NULL")).scalar_one()=='student_archived'
        assert conn.execute(text("SELECT count(*) FROM diagnostic_invites WHERE code='M89PEND' AND cancelled_at IS NULL AND cancel_reason IS NULL")).scalar_one()==1
    # Roll back one revision: cancelled -> expired, the columns go, the old CHECK is back; nothing else changes.
    command.downgrade(config,ARCHIVE)
    assert not ({'cancelled_at','cancel_reason','revived_at'} & set(cols()))
    assert [status_of(x) for x in ('M89PEND','M89PROG','M89EXPI','M89CANC')]==['pending','in_progress','expired','expired']
    with pytest.raises(IntegrityError,match='ck_diagnostic_invites_status'):
        add('cancelled','M89AGAIN')
    with engine.connect() as conn:
        assert conn.execute(text('SELECT version_num FROM alembic_version')).scalar_one()==ARCHIVE
    # ... and up again: the same data, the new status is accepted again (the date and the reason of the old one are gone).
    # The CHECK of a database made by the SQL scripts carries the PostgreSQL default name: it is found by what it says.
    with engine.begin() as conn:
        conn.execute(text('ALTER TABLE diagnostic_invites RENAME CONSTRAINT ck_diagnostic_invites_status TO diagnostic_invites_status_check'))
    command.upgrade(config,'head')
    with engine.connect() as conn:
        assert conn.execute(text("SELECT conname FROM pg_constraint WHERE conrelid='diagnostic_invites'::regclass AND contype='c' AND pg_get_constraintdef(oid) LIKE '%pending%'")).scalars().all()==['ck_diagnostic_invites_status']
    assert {'cancelled_at','cancel_reason','revived_at'} <= set(cols())
    assert status_of('M89CANC')=='expired'
    add('cancelled','M89AGAIN','student_archived')
    assert status_of('M89AGAIN')=='cancelled'
    # A second run is a no-op, and a database built from the models (columns and CHECK exist already) then stamped is fine.
    command.upgrade(config,'head')
    command.stamp(config,ARCHIVE)
    command.upgrade(config,'head')
    with engine.connect() as conn:
        assert conn.execute(text('SELECT version_num FROM alembic_version')).scalar_one()==HEAD
        assert conn.execute(text("SELECT count(*) FROM pg_constraint WHERE conrelid='diagnostic_invites'::regclass AND contype='c' AND pg_get_constraintdef(oid) LIKE '%pending%'")).scalar_one()==1   # one status CHECK, not two
    add('cancelled','M89LAST','student_archived')


def _check_n13_periods_objects(engine,config,ids):
    """#59: ``student_group_periods`` - the table, its constraints, the backfill (incl. the rebuild from the archive
    journal), idempotence, and the downgrade that keeps every membership row."""
    import json
    from sqlalchemy.exc import IntegrityError
    from src.models.auth import Group,Student,User
    org=ids['org']
    actor=uuid4()
    with Session(engine) as db:
        legacy_add(db,User,id=actor,email=f'{actor}@m.invalid',password_hash='synthetic-only',first_name='P',last_name='A',role='owner'); db.commit()
    # --- the table as the head leaves it
    assert 'student_group_periods' in inspect(engine).get_table_names()
    c={x['name']:x for x in inspect(engine).get_columns('student_group_periods')}
    assert set(c)=={'id','membership_id','joined_at','left_at','created_at'}
    assert not c['joined_at']['nullable'] and c['left_at']['nullable'] and not c['membership_id']['nullable']
    assert {i['name'] for i in inspect(engine).get_indexes('student_group_periods')} >= {'ix_student_group_periods_membership','uq_student_group_periods_open'}
    # the membership of the first check got its period from the backfill (or the model) - exactly one, and open
    with engine.connect() as conn:
        assert conn.execute(text('SELECT count(*) FROM student_group_periods WHERE membership_id=:m AND left_at IS NULL'),{'m':ids['membership']}).scalar_one()==1
    # --- go back to the revision before the table and build the data a backfill must understand (raw SQL: the models are newer)
    command.downgrade(config,INVITES)
    assert 'student_group_periods' not in inspect(engine).get_table_names()
    g1,g2=uuid4(),uuid4()
    students={n:uuid4() for n in range(1,11)}
    with Session(engine) as db:
        _group_row(db,g1,org,'Periods 1',9,datetime(2026,7,1,9,0,0))
        _group_row(db,g2,org,'Periods 2',9,datetime(2026,7,1,9,0,0))
        for n,sid in students.items():
            db.add(Student(id=sid,org_id=org,first_name=f'P{n}',last_name='Periods',class_level=9,status='active',created_at=datetime(2026,8,1,10,0,0)))
        db.commit()
    mem={n:uuid4() for n in range(1,11)}
    def membership(n,group,active,joined,left):
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO student_groups (id,student_id,group_id,is_active,joined_at,left_at) VALUES (:i,:s,:g,:a,:j,:l)"),
                         {'i':mem[n],'s':students[n],'g':group,'a':active,'j':joined,'l':left})
    def archived_row(n,joined_at,archived_at,membership_id=None,student_id=None):
        snap={'membership_id':str(membership_id or mem[n]),'student_id':str(student_id or students[n]),'joined_at':joined_at,'archived_at':archived_at}
        snap={k:v for k,v in snap.items() if v is not None}
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO group_audit (id,org_id,group_id,actor_id,actor_role,action,snapshot) VALUES (gen_random_uuid(),:o,:g,:a,'admin','student_archived',CAST(:s AS jsonb))"),
                         {'o':org,'g':g1,'a':actor,'s':json.dumps(snap)})
    membership(1,g1,True ,datetime(2026,9,1,8,0),None)                       # active
    membership(2,g1,False,datetime(2026,9,1,8,0),datetime(2026,9,30,12,0))   # closed
    membership(3,g1,True ,None,None)                                         # no joined_at: falls back to the student's created_at
    membership(4,g1,False,datetime(2026,9,5,8,0),None)                       # inactive without left_at: a zero-length period
    membership(5,g1,True ,datetime(2026,11,1,9,0),None)                      # archived in October, brought back in November
    archived_row(5,'2026-09-02T08:00:00','2026-10-10T12:30:00.123456')
    membership(6,g2,True ,datetime(2026,12,1,9,0),None)                      # archived twice, brought back twice
    archived_row(6,'2026-09-02T08:00:00','2026-10-10T12:00:00')
    archived_row(6,'2026-10-20T09:00:00','2026-11-15T10:00:00')
    membership(7,g2,False,datetime(2026,9,2,8,0),datetime(2026,10,10,12,0))  # still archived: the journal row IS the current period
    archived_row(7,'2026-09-02T08:00:00','2026-10-10T12:00:00')
    membership(8,g2,True ,datetime(2026,11,1,9,0),None)                      # journal rows that must NOT be believed
    archived_row(8,'yesterday','2026-10-10T12:00:00')                        # not a moment
    archived_row(8,'2026-10-01T08:00:00',None)                               # no end
    archived_row(8,'2026-10-25T00:00:00','2026-11-20T00:00:00')              # ends after the current period starts
    archived_row(8,'2026-10-12T10:00:00','2026-10-12T09:00:00')              # ends before it starts
    archived_row(8,'2026-09-02T08:00:00','2026-10-10T12:00:00',membership_id=uuid4())              # a membership that is gone
    archived_row(8,'2026-09-02T08:00:00','2026-10-10T12:00:00',student_id=students[1])             # another student's membership
    membership(9,g2,False,datetime(2026,9,1,8,0),datetime(2026,10,31,12,0)) # a journal row that overlaps the existing period
    archived_row(9,'2026-09-15T08:00:00','2026-10-01T08:00:00')
    membership(10,g2,True,datetime(2026,11,1,9,0),None)                      # snapshots that contradict each other
    archived_row(10,'2026-09-02T08:00:00','2026-10-10T12:00:00')
    archived_row(10,'2026-09-20T08:00:00','2026-10-15T12:00:00')              # starts inside the first one: dropped
    archived_row(10,'2026-10-10T12:00:00','2026-10-20T12:00:00')              # starts exactly when the first ended: kept
    def periods():
        with engine.connect() as conn:
            rows=conn.execute(text('SELECT membership_id,joined_at,left_at FROM student_group_periods ORDER BY membership_id,joined_at')).all()
        out={}
        for m,j,l in rows: out.setdefault(m,[]).append((j,l))
        return out
    def memberships_snapshot():
        with engine.connect() as conn:
            return conn.execute(text('SELECT id,student_id,group_id,is_active,joined_at,left_at FROM student_groups ORDER BY id')).all()
    rows_before=memberships_snapshot()
    command.upgrade(config,'head')
    got=periods()
    dt=datetime
    assert got[mem[1]]==[(dt(2026,9,1,8,0),None)]
    assert got[mem[2]]==[(dt(2026,9,1,8,0),dt(2026,9,30,12,0))]
    assert got[mem[3]]==[(dt(2026,8,1,10,0),None)]                           # student.created_at
    assert got[mem[4]]==[(dt(2026,9,5,8,0),dt(2026,9,5,8,0))]                # zero-length: covers no instant
    assert got[mem[5]]==[(dt(2026,9,2,8,0),dt(2026,10,10,12,30,0,123456)),(dt(2026,11,1,9,0),None)]   # rebuilt from the journal
    assert got[mem[6]]==[(dt(2026,9,2,8,0),dt(2026,10,10,12,0)),(dt(2026,10,20,9,0),dt(2026,11,15,10,0)),(dt(2026,12,1,9,0),None)]
    assert got[mem[7]]==[(dt(2026,9,2,8,0),dt(2026,10,10,12,0))]             # no duplicate of the current period
    assert got[mem[8]]==[(dt(2026,11,1,9,0),None)]                           # nothing invented
    assert got[mem[9]]==[(dt(2026,9,1,8,0),dt(2026,10,31,12,0))]
    assert got[mem[10]]==[(dt(2026,9,2,8,0),dt(2026,10,10,12,0)),(dt(2026,10,10,12,0),dt(2026,10,20,12,0)),(dt(2026,11,1,9,0),None)]   # the earliest of two overlapping snapshots, not both
    with engine.connect() as conn:                                           # no membership of this block has overlapping periods
        assert conn.execute(text("SELECT count(*) FROM student_group_periods a JOIN student_group_periods b ON a.membership_id=b.membership_id AND a.id<>b.id AND a.joined_at < COALESCE(b.left_at,'infinity') AND b.joined_at < COALESCE(a.left_at,'infinity') WHERE a.membership_id = ANY(:ids)"),{'ids':list(mem.values())}).scalar_one()==0
    assert memberships_snapshot()==rows_before                               # the membership rows are not touched
    with engine.connect() as conn:                                           # one open period iff active (for every membership of this block)
        assert conn.execute(text("SELECT count(*) FROM student_groups sg WHERE sg.id = ANY(:ids) AND (sg.is_active IS TRUE) <> EXISTS (SELECT 1 FROM student_group_periods p WHERE p.membership_id=sg.id AND p.left_at IS NULL)"),{'ids':list(mem.values())}).scalar_one()==0
    # --- idempotent: the same revision again (stamped back) changes nothing
    command.stamp(config,INVITES)
    command.upgrade(config,'head')
    assert periods()==got
    # --- the constraints
    with pytest.raises(IntegrityError,match='uq_student_group_periods_open'):
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO student_group_periods (id,membership_id,joined_at) VALUES (gen_random_uuid(),:m,'2026-12-24 08:00:00')"),{'m':mem[1]})
    with pytest.raises(IntegrityError,match='ck_student_group_periods_order'):
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO student_group_periods (id,membership_id,joined_at,left_at) VALUES (gen_random_uuid(),:m,'2026-12-24 08:00:00','2026-12-23 08:00:00')"),{'m':mem[2]})
    # a payment keeps the membership row (RESTRICT) - the periods hang on the row and go with it (CASCADE)
    with engine.begin() as conn:
        conn.execute(text('DELETE FROM student_groups WHERE id=:m'),{'m':mem[9]})
    assert mem[9] not in periods() and mem[8] in periods()
    # --- the downgrade drops the table only; every membership row stays as it was
    rows_before=memberships_snapshot()
    command.downgrade(config,INVITES)
    assert 'student_group_periods' not in inspect(engine).get_table_names()
    assert memberships_snapshot()==rows_before
    command.upgrade(config,'head')
    assert {k:v for k,v in periods().items() if k in mem.values()}=={k:v for k,v in got.items() if k in mem.values() and k!=mem[9]}   # the same periods come back, the journal still holds the old ones
    with engine.connect() as conn:
        assert conn.execute(text('SELECT version_num FROM alembic_version')).scalar_one()==HEAD
    # --- a database whose table and indexes exist already, stamped back: the revision does not fail and adds nothing
    command.stamp(config,INVITES)
    command.upgrade(config,'head')
    assert {k:v for k,v in periods().items() if k in mem.values()}=={k:v for k,v in got.items() if k in mem.values() and k!=mem[9]}


def _check_n13_sessions_objects(engine,config,ids):
    """#101: ``user_sessions`` + ``user_session_tokens`` - the tables, the backfill of the legacy ``users.refresh_token_hash``
    (a legacy token keeps working through the new code), idempotence, and the downgrade that makes the legacy column
    true again."""
    from datetime import timedelta
    from sqlalchemy.exc import IntegrityError
    from src.auth.service import AuthService,create_refresh_token,hash_token
    from src.auth.user_sessions import MAX_ACTIVE_SESSIONS,SUPERSEDED_RETENTION_DAYS
    from src.models.auth import User
    # --- the tables as the head leaves them
    names=set(inspect(engine).get_table_names())
    assert {'user_sessions','user_session_tokens'} <= names
    c={x['name']:x for x in inspect(engine).get_columns('user_sessions')}
    assert set(c)=={'id','user_id','created_at','last_used_at','expires_at','revoked_at','revoke_reason','user_agent','ip'}
    assert all(not c[n]['nullable'] for n in ('user_id','created_at','last_used_at','expires_at'))
    assert all(c[n]['nullable'] for n in ('revoked_at','revoke_reason','user_agent','ip'))
    assert c['revoke_reason']['type'].length==40 and c['user_agent']['type'].length==200 and c['ip']['type'].length==64
    t={x['name']:x for x in inspect(engine).get_columns('user_session_tokens')}
    assert set(t)=={'token_hash','session_id','issued_at','superseded_at'}
    assert not t['session_id']['nullable'] and not t['issued_at']['nullable'] and t['superseded_at']['nullable']
    assert inspect(engine).get_pk_constraint('user_session_tokens')['constrained_columns']==['token_hash']
    assert {i['name'] for i in inspect(engine).get_indexes('user_sessions')} >= {'ix_user_sessions_user_active','ix_user_sessions_expires_at'}
    assert {i['name'] for i in inspect(engine).get_indexes('user_session_tokens')} >= {'ix_user_session_tokens_session','ix_user_session_tokens_superseded'}
    with engine.connect() as conn:   # the partial predicates
        defs=dict(conn.execute(text("SELECT indexname,indexdef FROM pg_indexes WHERE tablename IN ('user_sessions','user_session_tokens')")).all())
    assert 'revoked_at IS NULL' in defs['ix_user_sessions_user_active'] and 'superseded_at IS NOT NULL' in defs['ix_user_session_tokens_superseded']
    assert MAX_ACTIVE_SESSIONS==20 and SUPERSEDED_RETENTION_DAYS==7
    def user(n,token_hash=None):
        uid=uuid4()
        with Session(engine) as db:
            legacy_add(db,User,id=uid,email=f'sess-{n}-{uid}@m.invalid',password_hash='synthetic-only',first_name='S',last_name=str(n),role='teacher',is_active=True,refresh_token_hash=token_hash); db.commit()
        return uid
    def sessions_of(uid):
        with engine.connect() as conn:
            return conn.execute(text('SELECT id,created_at,last_used_at,expires_at,revoked_at,revoke_reason,user_agent,ip FROM user_sessions WHERE user_id=:u ORDER BY created_at,id'),{'u':uid}).all()
    def tokens_of(uid):
        with engine.connect() as conn:
            return conn.execute(text('SELECT t.token_hash,t.issued_at,t.superseded_at FROM user_session_tokens t JOIN user_sessions s ON s.id=t.session_id WHERE s.user_id=:u ORDER BY t.issued_at,t.token_hash'),{'u':uid}).all()
    def live_of(uid): return {r.token_hash for r in tokens_of(uid) if r.superseded_at is None}
    def count(table):
        with engine.connect() as conn: return conn.execute(text(f'SELECT count(*) FROM {table}')).scalar_one()
    def insert_session(uid,last_used='2026-10-01 10:00:00',revoked=False):
        sid=uuid4()
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO user_sessions (id,user_id,created_at,last_used_at,expires_at,revoked_at,revoke_reason) VALUES (:i,:u,CAST(:l AS timestamp),CAST(:l AS timestamp),'2099-01-01 00:00:00',CASE WHEN :r THEN CAST(:l AS timestamp) END,CASE WHEN :r THEN 'logout' END)"),{'i':sid,'u':uid,'l':last_used,'r':revoked})
        return sid
    def insert_token(sid,token_hash,issued='2026-10-01 10:00:00',superseded=None):
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO user_session_tokens (token_hash,session_id,issued_at,superseded_at) VALUES (:h,:s,CAST(:i AS timestamp),CAST(:x AS timestamp))"),{'h':token_hash,'s':sid,'i':issued,'x':superseded})
    # the token hash is the primary key, a token cannot outlive its session, a session cannot outlive its user (CASCADE)
    victim=user('cascade'); vs=insert_session(victim); insert_token(vs,'dup-hash'); insert_token(vs,'dup-hash-2','2026-10-01 11:00:00','2026-10-01 12:00:00')
    other_session=insert_session(user('dup'))
    with pytest.raises(IntegrityError,match='user_session_tokens_pkey'):
        insert_token(other_session,'dup-hash')
    with pytest.raises(IntegrityError,match='user_session_tokens_session_id_fkey'):
        insert_token(uuid4(),'orphan-hash')
    with engine.begin() as conn:
        conn.execute(text('DELETE FROM users WHERE id=:u'),{'u':victim})
    assert sessions_of(victim)==[] and tokens_of(victim)==[]
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM user_session_tokens WHERE token_hash IN ('dup-hash','dup-hash-2')")).scalar_one()==0
    # --- the state BEFORE the revision: one refresh token per user, in the users row (raw SQL: the models are newer)
    command.downgrade(config,PERIODS)
    assert not {'user_sessions','user_session_tokens'} & set(inspect(engine).get_table_names())
    u_legacy=user('legacy'); u_other=user('other'); u_none=user('none'); u_empty=user('empty','')
    real_token=create_refresh_token(str(u_legacy)); other_token=create_refresh_token(str(u_other))
    with engine.begin() as conn:
        conn.execute(text('UPDATE users SET refresh_token_hash=:h WHERE id=:u'),{'h':hash_token(real_token),'u':u_legacy})
        conn.execute(text('UPDATE users SET refresh_token_hash=:h WHERE id=:u'),{'h':hash_token(other_token),'u':u_other})
    command.upgrade(config,'head')
    # --- the backfill: ONE session with ONE live token per user that had a token, nothing for the others
    for uid,token in ((u_legacy,real_token),(u_other,other_token)):
        (row,)=sessions_of(uid)
        assert row.revoked_at is None and row.revoke_reason is None and row.user_agent is None and row.ip is None
        assert row.created_at==row.last_used_at and row.expires_at-row.created_at==timedelta(days=30)
        assert abs((row.created_at-datetime.utcnow()).total_seconds())<600   # naive UTC "now"
        (tok,)=tokens_of(uid)
        assert tok.token_hash==hash_token(token) and tok.superseded_at is None and tok.issued_at==row.created_at
    assert sessions_of(u_none)==[] and sessions_of(u_empty)==[] and tokens_of(u_none)==[]
    with engine.connect() as conn:   # the legacy column is kept as it was
        assert conn.execute(text('SELECT refresh_token_hash FROM users WHERE id=:u'),{'u':u_legacy}).scalar_one()==hash_token(real_token)
    # --- a token issued BEFORE the migration works after it, and rotates only its own session
    with Session(engine) as db:
        pair=AuthService(db).refresh_tokens(real_token)
    assert pair.refresh_token!=real_token
    assert live_of(u_legacy)=={hash_token(pair.refresh_token)} and len(tokens_of(u_legacy))==2   # the old one is superseded, kept for the replay check
    assert live_of(u_other)=={hash_token(other_token)} and len(tokens_of(u_other))==1               # the other user's session did not move
    with Session(engine) as db:
        pair=AuthService(db).refresh_tokens(pair.refresh_token)    # and the new token keeps rotating
    # --- idempotent: the same revision again (stamped back) adds nothing - the legacy column still holds the hash of the
    #     FIRST token, which has been superseded since, and it must not come back to life as a session of its own
    before=(count('user_sessions'),count('user_session_tokens'))
    command.stamp(config,PERIODS)
    command.upgrade(config,'head')
    assert (count('user_sessions'),count('user_session_tokens'))==before
    assert live_of(u_legacy)=={hash_token(pair.refresh_token)}
    # --- the downgrade makes the legacy column true again: per user the newest LIVE token of the live session used last;
    #     a stale hash never revives a signed-out or superseded token; users without a live session get NULL
    u_multi,u_dead=user('multi'),user('dead',hash_token('stale-legacy-hash'))
    s_old=insert_session(u_multi,'2026-10-01 10:00:00'); insert_token(s_old,'multi-old')
    s_new=insert_session(u_multi,'2026-10-02 10:00:00')
    insert_token(s_new,'multi-newest-superseded','2026-10-02 10:00:00','2026-10-02 10:05:00'); insert_token(s_new,'multi-sibling-a','2026-10-02 10:05:00'); insert_token(s_new,'multi-newest','2026-10-02 10:06:00')
    s_rev=insert_session(u_multi,'2026-10-03 10:00:00',revoked=True); insert_token(s_rev,'multi-revoked')
    s_dead=insert_session(u_dead,'2026-10-01 10:00:00',revoked=True); insert_token(s_dead,'dead-revoked')
    with engine.begin() as conn:
        conn.execute(text("UPDATE users SET refresh_token_hash='stale-legacy-hash' WHERE id=:u"),{'u':u_multi})
    live_other=next(iter(live_of(u_other))); live_legacy=next(iter(live_of(u_legacy)))
    command.downgrade(config,PERIODS)
    assert not {'user_sessions','user_session_tokens'} & set(inspect(engine).get_table_names())
    with engine.connect() as conn:
        legacy=dict(conn.execute(text('SELECT id,refresh_token_hash FROM users WHERE id=ANY(:ids)'),{'ids':[u_multi,u_dead,u_legacy,u_other,u_none,u_empty]}).all())
    assert legacy[u_multi]=='multi-newest' and legacy[u_dead] is None and legacy[u_none] is None and legacy[u_empty] is None
    assert legacy[u_other]==live_other and legacy[u_legacy]==live_legacy
    command.upgrade(config,'head')
    assert live_of(u_multi)=={'multi-newest'} and len(sessions_of(u_multi))==1 and sessions_of(u_dead)==[]
    assert live_of(u_legacy)=={live_legacy}
    with engine.connect() as conn:
        assert conn.execute(text('SELECT version_num FROM alembic_version')).scalar_one()==HEAD


def _check_n13_group_price_objects(engine,config,ids):
    """#105: ``group_prices`` + ``membership_price_overrides`` - the backfill on MIXED data (the most frequent fee, a tie by
    the later date, then the smaller sum, NULL fees, inactive and archived memberships, free), the constraints, the
    downgrade that refills ``monthly_fee`` with today's effective price, the round trip and idempotence."""
    from datetime import date,datetime
    from sqlalchemy.exc import IntegrityError
    from src.models.auth import Group,Organization,Student,User
    org,owner=uuid4(),uuid4()
    with Session(engine) as db:
        legacy_add(db,Organization,id=org,name='Price migration org',status='active'); db.flush()
        legacy_add(db,User,id=owner,email=f'{owner}@m.invalid',password_hash='synthetic-only',first_name='P',last_name='P',role='owner'); db.commit()
    groups={n:uuid4() for n in ('majority','tie_date','tie_sum','all_null','inactive_only','free','empty','single','pairs')}
    with Session(engine) as db:
        for name,gid in groups.items():
            _group_row(db,gid,org,name,9)
        db.commit()
    # --- the revision before the group price: no tables, and the audit CHECK has no price actions
    def audit(action):
        with engine.begin() as c:
            c.execute(text("INSERT INTO group_audit (id,org_id,group_id,actor_id,actor_role,action,snapshot,created_at) VALUES (gen_random_uuid(),:o,:g,:u,'owner',:a,CAST('{}' AS jsonb),now())"),{'o':org,'g':groups['majority'],'u':owner,'a':action})
    def audit_actions():
        with engine.connect() as c:
            return sorted(r[0] for r in c.execute(text("SELECT action FROM group_audit WHERE org_id=:o"),{'o':org}))
    def audit_check():
        with engine.connect() as c:
            return c.execute(text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname='ck_group_audit_action' AND conrelid='group_audit'::regclass")).scalar_one()
    price_actions=('group_price_set','membership_price_set','group_price_cancelled','membership_price_cancelled')
    command.downgrade(config,SESSIONS)
    assert not {'group_prices','membership_price_overrides'} & set(inspect(engine).get_table_names())
    assert not any(a in audit_check() for a in price_actions) and 'student_restored' in audit_check()
    with pytest.raises(IntegrityError,match='ck_group_audit_action'): audit('group_price_set')
    def member(group,fee,*,active=True,status='active',updated=None):
        sid,mid=uuid4(),uuid4()
        with Session(engine) as db:
            db.add(Student(id=sid,org_id=org,first_name='S',last_name=group,class_level=9,status=status)); db.commit()
        with engine.begin() as c:
            c.execute(text("INSERT INTO student_groups (id,student_id,group_id,is_active,joined_at,left_at,monthly_fee,monthly_fee_updated_at) VALUES (:m,:s,:g,:a,'2026-09-01 08:00:00',CASE WHEN :a THEN NULL ELSE TIMESTAMP '2026-09-20 08:00:00' END,:f,:u)"),{'m':mid,'s':sid,'g':groups[group],'a':active,'f':fee,'u':updated})
        return mid
    old,new=datetime(2026,1,1),datetime(2026,6,1)
    m={}
    m['maj1']=member('majority',500000,updated=old); m['maj2']=member('majority',500000,updated=new); m['maj3']=member('majority',450000,updated=new)
    m['maj_null']=member('majority',None); m['maj_old']=member('majority',300000,active=False)               # an inactive fee never votes ...
    m['maj_inactive_same']=member('majority',500000,active=False)                                           # ... but one equal to the price follows it
    m['maj_archived']=member('majority',999000,status='archived')                                            # active membership of an archived student: no vote, an exception
    m['td_a']=member('tie_date',400000,updated=old); m['td_a2']=member('tie_date',400000,updated=old)       # a 2 + 2 tie: the later one wins
    m['td_b']=member('tie_date',500000,updated=new); m['td_b2']=member('tie_date',500000,updated=new)
    m['ts_a']=member('tie_sum',500000,updated=old); m['ts_a2']=member('tie_sum',500000,updated=old)         # same date: the smaller sum
    m['ts_b']=member('tie_sum',400000,updated=old); m['ts_b2']=member('tie_sum',400000,updated=old)
    m['sg1']=member('single',350000,updated=new); m['sg_null']=member('single',None)                         # ONE fee: no price, the NULL one stays without
    m['pr_a']=member('pairs',300000,updated=old); m['pr_b']=member('pairs',400000,updated=old); m['pr_c']=member('pairs',500000,updated=new)   # 1 + 1 + 1: no price
    m['an1']=member('all_null',None); m['an2']=member('all_null',None)
    m['io1']=member('inactive_only',350000,active=False); m['io2']=member('inactive_only',None,active=False)
    m['fr1']=member('free',0,updated=new); m['fr2']=member('free',0,updated=old); m['fr3']=member('free',300000,updated=new)
    # --- upgrade: the backfill
    command.upgrade(config,'head')
    assert {'group_prices','membership_price_overrides'} <= set(inspect(engine).get_table_names())
    def prices():
        with engine.connect() as c:
            return {r.name:(r.amount,r.valid_from,r.note,r.created_by) for r in c.execute(text("SELECT g.name,p.amount,p.valid_from,p.note,p.created_by FROM group_prices p JOIN groups g ON g.id=p.group_id WHERE g.org_id=:o"),{'o':org})}
    def overrides():
        with engine.connect() as c:
            return {r.membership_id:(r.amount,r.valid_from,r.reason) for r in c.execute(text("SELECT o.membership_id,o.amount,o.valid_from,o.reason FROM membership_price_overrides o JOIN student_groups sg ON sg.id=o.membership_id JOIN groups g ON g.id=sg.group_id WHERE g.org_id=:o AND COALESCE(o.reason,'')<>'left_group'"),{'o':org})}
    epoch=date(2000,1,1)
    got=prices()
    assert {k:v[0] for k,v in got.items()}=={'majority':500000,'tie_date':500000,'tie_sum':400000,'free':0}      # no price for all_null / inactive_only / empty / single / pairs (less than 2 votes)
    assert all(v[1]==epoch and v[3] is None for v in got.values())
    ex=overrides()
    reason='Цена до введения цены группы'
    assert ex=={m['maj3']:(450000,epoch,reason),m['maj_old']:(300000,epoch,reason),m['maj_archived']:(999000,epoch,reason),
                m['td_a']:(400000,epoch,reason),m['td_a2']:(400000,epoch,reason),m['ts_a']:(500000,epoch,reason),m['ts_a2']:(500000,epoch,reason),
                m['io1']:(350000,epoch,reason),m['fr3']:(300000,epoch,reason),
                m['sg1']:(350000,epoch,reason),m['pr_a']:(300000,epoch,reason),m['pr_b']:(400000,epoch,reason),m['pr_c']:(500000,epoch,reason)}   # a lone fee stays an exception: no amount changes
    # the effective price of every membership that HAD a fee is what it was; the NULL ones follow the group price (or stay empty)
    def effective():
        with engine.connect() as c:
            return {r.id:r.price for r in c.execute(text("""
                SELECT sg.id, COALESCE((SELECT o.amount FROM membership_price_overrides o WHERE o.membership_id=sg.id AND o.valid_from<=CURRENT_DATE AND COALESCE(o.reason,'')<>'left_group' ORDER BY o.valid_from DESC LIMIT 1),
                                       (SELECT p.amount FROM group_prices p WHERE p.group_id=sg.group_id AND p.valid_from<=CURRENT_DATE ORDER BY p.valid_from DESC LIMIT 1)) AS price
                  FROM student_groups sg JOIN groups g ON g.id=sg.group_id WHERE g.org_id=:o"""),{'o':org})}
    eff=effective()
    with engine.connect() as c:
        fees={r.id:r.monthly_fee for r in c.execute(text("SELECT sg.id,sg.monthly_fee FROM student_groups sg JOIN groups g ON g.id=sg.group_id WHERE g.org_id=:o"),{'o':org})}
    for mid,fee in fees.items():
        if fee is not None: assert eff[mid]==fee,(mid,fee,eff[mid])
    assert eff[m['sg1']]==350000 and eff[m['sg_null']] is None and eff[m['pr_a']]==300000 and eff[m['pr_c']]==500000   # nobody silently starts paying
    assert eff[m['maj_null']]==500000 and eff[m['an1']] is None and eff[m['io2']] is None       # NULL follows the price of the group (the intended change)
    assert all(fees[x] is not None or x in (m['maj_null'],m['an1'],m['an2'],m['io2'],m['sg_null']) for x in fees)   # the old column is untouched by the upgrade
    # --- constraints
    gid=groups['majority']
    for sql,params,pattern in (
        ("INSERT INTO group_prices (id,group_id,amount,valid_from,created_at) VALUES (gen_random_uuid(),:g,-1,'2026-10-01',now())",{'g':gid},'ck_group_prices_amount'),
        ("INSERT INTO group_prices (id,group_id,amount,valid_from,created_at) VALUES (gen_random_uuid(),:g,1,'2000-01-01',now())",{'g':gid},'uq_group_prices_group_from'),
        ("INSERT INTO membership_price_overrides (id,membership_id,amount,valid_from,reason,created_at) VALUES (gen_random_uuid(),:m,1,'2026-10-01',NULL,now())",{'m':m['maj1']},'ck_membership_price_overrides_reason'),
        ("INSERT INTO membership_price_overrides (id,membership_id,amount,valid_from,reason,created_at) VALUES (gen_random_uuid(),:m,1,'2026-10-01','   ',now())",{'m':m['maj1']},'ck_membership_price_overrides_reason'),
        ("INSERT INTO membership_price_overrides (id,membership_id,amount,valid_from,reason,created_at) VALUES (gen_random_uuid(),:m,-5,'2026-10-01','x',now())",{'m':m['maj1']},'ck_membership_price_overrides_amount'),
        ("INSERT INTO membership_price_overrides (id,membership_id,amount,valid_from,reason,created_at) VALUES (gen_random_uuid(),:m,1,'2000-01-01','dup',now())",{'m':m['maj3']},'uq_membership_price_overrides_from'),
    ):
        with pytest.raises(IntegrityError,match=pattern):
            with engine.begin() as c: c.execute(text(sql),params)
    with engine.begin() as c:       # "the group price again" needs no reason; a NULL amount is allowed
        c.execute(text("INSERT INTO membership_price_overrides (id,membership_id,amount,valid_from,reason,created_at) VALUES (gen_random_uuid(),:m,NULL,'2026-10-01',NULL,now())"),{'m':m['maj3']})
    # --- audit trail: the CHECK of group_audit.action takes the two price actions, keeps the old ones and refuses the rest
    assert all(a in audit_check() for a in price_actions) and 'student_archived' in audit_check()
    for action in ('created','updated','teacher_changed','student_archived','student_restored',*price_actions): audit(action)
    with pytest.raises(IntegrityError,match='ck_group_audit_action'): audit('price_set')
    # --- post-release changes: a new group price (today), an individual price from today for a follower
    with engine.begin() as c:
        c.execute(text("INSERT INTO group_prices (id,group_id,amount,valid_from,note,created_by,created_at) VALUES (gen_random_uuid(),:g,600000,'2026-10-01','повышение',:u,now())"),{'g':gid,'u':owner})
        c.execute(text("INSERT INTO membership_price_overrides (id,membership_id,amount,valid_from,reason,created_by,created_at) VALUES (gen_random_uuid(),:m,550000,'2026-10-01','скидка',:u,now())"),{'m':m['maj2'],'u':owner})
        c.execute(text("INSERT INTO group_prices (id,group_id,amount,valid_from,created_at) VALUES (gen_random_uuid(),:g,999,'2999-01-01',now())"),{'g':groups['tie_date']})   # a price of the far future: not today's
    # --- downgrade: monthly_fee = the effective price TODAY, NULL when there is none; the tables are gone
    command.downgrade(config,SESSIONS)
    assert not {'group_prices','membership_price_overrides'} & set(inspect(engine).get_table_names())
    # the trail of the prices went with them, the other audit rows stay, the old list of actions is back (and holds)
    assert audit_actions()==sorted(['created','updated','teacher_changed','student_archived','student_restored'])
    assert not any(a in audit_check() for a in price_actions) and all(a in audit_check() for a in ('created','updated','teacher_changed','student_archived','student_restored'))
    for action in price_actions:
        with pytest.raises(IntegrityError,match='ck_group_audit_action'): audit(action)
    audit('student_archived')                                                                  # an old action is still accepted
    with engine.connect() as c:
        back={r.id:r.monthly_fee for r in c.execute(text("SELECT sg.id,sg.monthly_fee FROM student_groups sg JOIN groups g ON g.id=sg.group_id WHERE g.org_id=:o"),{'o':org})}
        assert c.execute(text('SELECT version_num FROM alembic_version')).scalar_one()==SESSIONS
    assert back[m['maj1']]==600000 and back[m['maj2']]==550000                         # the new group price / the new individual price
    assert back[m['maj3']]==600000                                                     # "the group price again" from 2026-10-01 ... (the fixed date is in the past)
    assert back[m['maj_null']]==600000 and back[m['maj_old']]==300000                  # a follower / an old exception
    assert back[m['td_a']]==400000 and back[m['td_b']]==500000                         # the far-future price is not today's
    assert back[m['an1']] is None and back[m['io2']] is None and back[m['io1']]==350000 and back[m['fr1']]==0
    # --- upgrade again: the same effective prices come back (nothing was lost but the history of changes)
    command.upgrade(config,'head')
    assert audit_actions()==sorted(['created','updated','teacher_changed','student_archived','student_archived','student_restored'])   # no price row comes back
    for action in price_actions: audit(action)                                                  # the CHECK takes them again
    assert all(a in audit_check() for a in price_actions)
    again=effective()
    assert {k:v for k,v in again.items() if back[k] is not None}=={k:v for k,v in back.items() if v is not None}
    assert prices()['majority'][0]==600000 and overrides()[m['maj2']][0]==550000
    # --- idempotent: the revision again (stamped back) with the tables in place adds nothing
    with engine.connect() as c: before=(c.execute(text('SELECT count(*) FROM group_prices')).scalar_one(),c.execute(text('SELECT count(*) FROM membership_price_overrides')).scalar_one())
    command.stamp(config,SESSIONS)
    command.upgrade(config,'head')
    with engine.connect() as c:
        assert (c.execute(text('SELECT count(*) FROM group_prices')).scalar_one(),c.execute(text('SELECT count(*) FROM membership_price_overrides')).scalar_one())==before
        assert c.execute(text('SELECT version_num FROM alembic_version')).scalar_one()==HEAD
    # --- cascades: a group takes its prices along, a membership its exceptions
    with engine.begin() as c:
        c.execute(text('DELETE FROM student_groups WHERE id=:m'),{'m':m['maj2']})
    assert m['maj2'] not in overrides()
    # --- an empty database upgrades and downgrades (no rows, no failure)
    command.downgrade(config,SESSIONS); command.upgrade(config,'head')


def _check_n13_excuse_mark_objects(engine,config,ids):
    """#39: ``attendance_excuses.decided_after_deadline`` / ``deadline_at_decision`` - nullable, the decisions made before them stay
    NULL ("unknown", never "on time"), an upgrade run again (or stamped back) keeps the values, the downgrade drops the two
    columns and keeps every decision, the round trip works."""
    from datetime import date
    from src.models.auth import User
    user,att=uuid4(),uuid4()
    with Session(engine) as db:
        legacy_add(db,User,id=user,email=f'{user}@m.invalid',password_hash='synthetic-only',first_name='M',last_name='M',role='admin'); db.commit()
    mark={'decided_after_deadline','deadline_at_decision'}
    def columns():
        return {c['name']:c for c in inspect(engine).get_columns('attendance_excuses')}
    def rows():
        with engine.connect() as c:
            return {r.status:(r.decided_after_deadline,r.deadline_at_decision) for r in c.execute(text("SELECT status,decided_after_deadline,deadline_at_decision FROM attendance_excuses WHERE attendance_id=:a"),{'a':att})}
    def decisions():
        with engine.connect() as c:
            return sorted(r.status for r in c.execute(text("SELECT status FROM attendance_excuses WHERE attendance_id=:a"),{'a':att}))
    # --- the revision before the mark: no columns; decisions of every kind exist (the data of the release before)
    command.downgrade(config,PRICES)
    assert not mark & set(columns())
    with engine.begin() as c:
        c.execute(text("INSERT INTO group_attendance (id,org_id,group_id,lesson_date,lesson_id,status,records) VALUES (:a,:o,:g,'2026-10-05','mark1','completed','{}')"),{'a':att,'o':ids['org'],'g':ids['group']})
        for status in ('pending','confirmed','rejected'):
            c.execute(text("INSERT INTO attendance_excuses (id,org_id,attendance_id,student_id,reason,reason_by,reason_at,status,reviewed_by,reviewed_at) VALUES (gen_random_uuid(),:o,:a,gen_random_uuid(),'болел',:u,now(),:s,CASE WHEN :s='pending' THEN NULL ELSE CAST(:u AS uuid) END,CASE WHEN :s='pending' THEN NULL ELSE now() END)"),{'o':ids['org'],'a':att,'u':user,'s':status})
    with engine.connect() as c:
        assert c.execute(text('SELECT version_num FROM alembic_version')).scalar_one()==PRICES
    # --- upgrade: two nullable columns of the right types, every historical row NULL (a decided one is "unknown", not False)
    command.upgrade(config,'head')
    got=columns()
    assert mark <= set(got) and all(got[n]['nullable'] for n in mark)
    assert str(got['decided_after_deadline']['type']).upper()=='BOOLEAN' and str(got['deadline_at_decision']['type']).upper()=='DATE'
    assert all(c.get('default') is None for c in (got['decided_after_deadline'],got['deadline_at_decision']))
    assert rows()=={'pending':(None,None),'confirmed':(None,None),'rejected':(None,None)}
    with engine.connect() as c:
        assert c.execute(text('SELECT version_num FROM alembic_version')).scalar_one()==HEAD
    # --- the marks the code writes after the release
    with engine.begin() as c:
        c.execute(text("UPDATE attendance_excuses SET decided_after_deadline=true,deadline_at_decision=:d WHERE attendance_id=:a AND status='confirmed'"),{'d':date(2026,10,1),'a':att})
        c.execute(text("UPDATE attendance_excuses SET decided_after_deadline=false,deadline_at_decision=NULL WHERE attendance_id=:a AND status='rejected'"),{'a':att})
    marked={'pending':(None,None),'confirmed':(True,date(2026,10,1)),'rejected':(False,None)}
    assert rows()==marked
    # --- idempotent: upgrade again, and the revision again with the columns in place (a database built by create_all and stamped back)
    command.upgrade(config,'head')
    assert rows()==marked
    command.stamp(config,PRICES)
    command.upgrade(config,'head')
    assert rows()==marked
    with engine.connect() as c:
        assert c.execute(text('SELECT version_num FROM alembic_version')).scalar_one()==HEAD
    # --- downgrade: the columns go, the decisions (status, reviewer, time) stay
    command.downgrade(config,PRICES)
    assert not mark & set(columns())
    assert decisions()==['confirmed','pending','rejected']
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM attendance_excuses WHERE attendance_id=:a AND (status='pending')=(reviewed_by IS NULL) AND (status='pending')=(reviewed_at IS NULL)"),{'a':att}).scalar_one()==3
        assert c.execute(text('SELECT version_num FROM alembic_version')).scalar_one()==PRICES
    command.downgrade(config,PRICES)           # nothing left to drop: repeating is harmless
    # --- and up again: the marks were not kept, every row is "unknown" once more
    command.upgrade(config,'head')
    assert rows()=={'pending':(None,None),'confirmed':(None,None),'rejected':(None,None)}
    with engine.begin() as c:
        c.execute(text('DELETE FROM group_attendance WHERE id=:a'),{'a':att})      # CASCADE: leave no excuse behind
    with engine.connect() as c:
        assert c.execute(text('SELECT count(*) FROM attendance_excuses WHERE attendance_id=:a'),{'a':att}).scalar_one()==0


def _check_ux2_payroll_rates_objects(engine,config,ids):
    """UX-2: ``payroll_rates`` - created empty on a database without sheets; the percents of the saved sheets are moved (one row
    per teacher and kind, and one more for every month in which the percent CHANGED, so every saved month keeps its rate); a
    value that is not a percent is skipped; another centre's rows stay its own; idempotent (also stamped back); the downgrade
    drops the table and leaves the sheets."""
    import json
    from datetime import date
    from decimal import Decimal
    from src.models.auth import User,Organization
    org2=uuid4()
    teachers={n:uuid4() for n in 'ABCD'}
    with Session(engine) as db:
        legacy_add(db,Organization,id=org2,name='Migration second',status='active'); db.flush()
        for name,uid in teachers.items():
            legacy_add(db,User,id=uid,email=f'{uid}@m.invalid',password_hash='synthetic-only',first_name=name,last_name='T',role='teacher')
        db.commit()
    def tables():
        return set(inspect(engine).get_table_names())
    def rates():
        with engine.connect() as c:
            return sorted((r.teacher_id,r.lesson_type,r.valid_from,r.percent,r.note,r.created_by,r.org_id) for r in c.execute(text('SELECT org_id,teacher_id,lesson_type,valid_from,percent,note,created_by FROM payroll_rates')))
    def sheet(org,teacher,month,inputs):
        with engine.begin() as c:
            c.execute(text("INSERT INTO payroll_sheets (id,org_id,teacher_id,period_month,percent_inputs,source_fingerprint,created_by,updated_by) "
                           "VALUES (:i,:o,:t,:m,CAST(:p AS jsonb),'fp',:t,:t)"),{'i':uuid4(),'o':org,'t':teacher,'m':month,'p':json.dumps(inputs)})
    # --- a database without sheets: the table is created, empty
    command.downgrade(config,EXCUSE)
    assert 'payroll_rates' not in tables()
    with engine.begin() as c:
        c.execute(text('DELETE FROM payroll_sheets'))
    command.upgrade(config,'head')
    assert 'payroll_rates' in tables() and rates()==[]
    with engine.connect() as c:
        assert c.execute(text('SELECT version_num FROM alembic_version')).scalar_one()==HEAD
    cols={c['name']:c for c in inspect(engine).get_columns('payroll_rates')}
    assert set(cols)=={'id','org_id','teacher_id','lesson_type','percent','valid_from','note','created_by','created_at'}
    assert not cols['org_id']['nullable'] and not cols['percent']['nullable'] and cols['created_by']['nullable'] and cols['note']['nullable']
    assert str(cols['percent']['type']).upper().startswith('NUMERIC(5, 2)')
    assert any(i['column_names']==['org_id','teacher_id','lesson_type','valid_from'] for i in inspect(engine).get_indexes('payroll_rates'))
    from sqlalchemy.exc import IntegrityError
    for bad in ("'pair',101,'2026-09-01'","'pair',50,'2026-09-15'","'weird',50,'2026-09-01'"):     # the CHECKs: percent, the 1st of a month, the kind
        with pytest.raises(IntegrityError):
            with engine.begin() as c:
                c.execute(text(f"INSERT INTO payroll_rates (id,org_id,teacher_id,lesson_type,percent,valid_from) VALUES (gen_random_uuid(),:o,:t,{bad})"),{'o':ids['org'],'t':teachers['A']})
    assert rates()==[]
    # --- the revision before: sheets with one and with changing percents
    command.downgrade(config,EXCUSE)
    a,b,c_,d=teachers['A'],teachers['B'],teachers['C'],teachers['D']
    sheet(ids['org'],a,date(2026,8,1),{'group':'50'})
    sheet(ids['org'],a,date(2026,9,1),{'group':'50','individual':'40'})       # group unchanged: no new row
    sheet(ids['org'],a,date(2026,10,1),{'group':'45','individual':'40'})      # group changed in October
    sheet(ids['org'],a,date(2026,12,1),{'group':'50'})                         # changed back: a row again (a skipped month keeps 45)
    sheet(ids['org'],b,date(2026,9,1),{'group':'62.5'})                         # one sheet, one percent
    sheet(ids['org'],c_,date(2026,9,1),{'group':'abc','pair':'101','weird':'50','individual':'30'})   # only the valid one
    sheet(org2,d,date(2026,9,1),{'group':'30'})
    command.upgrade(config,'head')
    note='перенесено из ведомостей'
    expected=sorted([
        (a,'group',date(2026,8,1),Decimal('50'),note,None,ids['org']),
        (a,'group',date(2026,10,1),Decimal('45'),note,None,ids['org']),
        (a,'group',date(2026,12,1),Decimal('50'),note,None,ids['org']),
        (a,'individual',date(2026,9,1),Decimal('40'),note,None,ids['org']),
        (b,'group',date(2026,9,1),Decimal('62.5'),note,None,ids['org']),
        (c_,'individual',date(2026,9,1),Decimal('30'),note,None,ids['org']),
        (d,'group',date(2026,9,1),Decimal('30'),note,None,org2),
    ])
    assert rates()==expected
    # created_at is naive UTC like the app's own timestamps: a same-month PUT right after the release always has a later one
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM payroll_rates WHERE abs(extract(epoch FROM (created_at - timezone('utc', now()))))<120")).scalar_one()==len(expected)
    # every saved month keeps its own rate: the lookup of the code (the last row not after the month) gives the percent of each sheet
    def rate_of(teacher,kind,month):
        with engine.connect() as c:
            return c.execute(text("SELECT percent FROM payroll_rates WHERE teacher_id=:t AND lesson_type=:k AND valid_from<=:m ORDER BY valid_from DESC,created_at DESC LIMIT 1"),{'t':teacher,'k':kind,'m':month}).scalar()
    assert [rate_of(a,'group',date(2026,m,1)) for m in (8,9,10,11,12)]==[Decimal('50'),Decimal('50'),Decimal('45'),Decimal('45'),Decimal('50')]
    assert rate_of(a,'group',date(2026,7,1)) is None and rate_of(b,'group',date(2026,9,1))==Decimal('62.5')
    # --- idempotent: upgrade again, and the revision again with the table in place (a database built by create_all and stamped back)
    with engine.begin() as c:
        c.execute(text("INSERT INTO payroll_rates (id,org_id,teacher_id,lesson_type,percent,valid_from,created_by) VALUES (gen_random_uuid(),:o,:t,'group',55,'2027-01-01',:t)"),{'o':ids['org'],'t':a})
    command.upgrade(config,'head')
    assert len(rates())==len(expected)+1
    command.stamp(config,EXCUSE)
    command.upgrade(config,'head')
    assert len(rates())==len(expected)+1
    with engine.connect() as c:
        assert c.execute(text('SELECT version_num FROM alembic_version')).scalar_one()==HEAD
    # --- downgrade: the table goes, the sheets stay; repeated is harmless; up again moves the sheets once more
    command.downgrade(config,EXCUSE)
    assert 'payroll_rates' not in tables()
    command.downgrade(config,EXCUSE)
    with engine.connect() as c:
        assert c.execute(text('SELECT count(*) FROM payroll_sheets WHERE teacher_id=:t'),{'t':a}).scalar_one()==4
        assert c.execute(text('SELECT version_num FROM alembic_version')).scalar_one()==EXCUSE
    command.upgrade(config,'head')
    assert rates()==expected
    with engine.begin() as c:        # leave nothing behind for the checks that may follow
        c.execute(text('DELETE FROM payroll_rates'))
        c.execute(text('DELETE FROM payroll_sheets'))


def _check_pay2_objects(engine,config,ids):
    """PAY-2: payments become money.  Old-model data (a payment with a period, a carry-over copy made by a transfer, a voided
    payment, a lessons package) is converted: nothing is lost, the carry-over copy becomes ``legacy_carry`` and a
    ``membership_transfers`` link, the accounting start is set; the new guards work; the downgrade gives every payment a period."""
    import json
    from datetime import date,datetime
    from sqlalchemy.exc import IntegrityError
    from src.models.auth import User,Organization,Group,StudentGroup
    user,org2,group_b,member_b=uuid4(),uuid4(),uuid4(),uuid4()
    with Session(engine) as db:
        legacy_add(db,User,id=user,email=f'{user}@m.invalid',password_hash='synthetic-only',first_name='P',last_name='A',role='owner')
        legacy_add(db,Organization,id=org2,name='PAY-2 second centre',status='active',settings={'payments':{'period_mode':'lessons','lessons_per_payment':8}}); db.flush()
        _group_row(db,group_b,ids['org'],'PAY-2 group B',9); db.flush()
        db.add(StudentGroup(id=member_b,student_id=ids['student'],group_id=group_b)); db.commit()
    with engine.begin() as c:        # the earlier checks leave payments behind: start from none
        c.execute(text('DELETE FROM student_payment_files'))
        c.execute(text('UPDATE student_payments SET carried_from_payment_id=NULL, replaces_payment_id=NULL'))
        c.execute(text('DELETE FROM student_payments'))
    command.downgrade(config,PAYROLL_RATES)
    cols={c['name'] for c in inspect(engine).get_columns('student_payments')}
    assert not ({'method','kind','idempotency_key'} & cols) and not ({'student_writeoffs','student_refunds','membership_transfers','payment_receipt_snapshots'} & set(inspect(engine).get_table_names()))
    orig,carry,voided,pkg,cal2=uuid4(),uuid4(),uuid4(),uuid4(),uuid4()
    moved=datetime(2026,10,11,5,0,0)
    ins=("INSERT INTO student_payments (id,org_id,membership_id,student_id,group_id,amount,paid_on,valid_from,valid_to,lessons_count,"
         "carried_from_payment_id,carried_out_at,created_by,voided_at,voided_by,void_reason,receipt_no) "
         "VALUES (:id,:o,:m,:s,:g,:amount,:paid,:vf,:vt,:lc,:cf,:co,:u,:va,:vb,:vr,:rn)")
    def pay(**kw):
        base={'o':ids['org'],'m':ids['membership'],'s':ids['student'],'g':ids['group'],'amount':0,'paid':date(2026,9,20),'vf':date(2026,9,15),
              'vt':date(2026,10,14),'lc':None,'cf':None,'co':None,'u':user,'va':None,'vb':None,'vr':None,'rn':None}
        base.update(kw)
        with engine.begin() as c:
            c.execute(text(ins),base)
    pay(id=orig,amount=500000,co=moved,rn=1)                                                    # moved with the student on 11.10
    pay(id=carry,m=member_b,g=group_b,amount=0,paid=date(2026,10,11),vf=date(2026,10,12),vt=date(2026,11,14),cf=orig)   # its copy in B
    pay(id=voided,amount=100000,paid=date(2026,9,1),vf=date(2026,9,1),vt=date(2026,9,30),va=datetime(2026,9,2),vb=user,vr='ошибка',rn=2)
    pay(id=pkg,amount=600000,paid=date(2026,9,25),vf=date(2026,9,25),vt=None,lc=12,rn=3)
    pay(id=cal2,amount=250000,paid=date(2026,10,5),vf=date(2026,10,5),vt=date(2026,11,4),rn=4)  # the same membership, a second period
    with engine.begin() as c:        # the first upgrade of this chain stored the date of that day: forget it
        c.execute(text("UPDATE organizations SET settings = settings #- '{payments,accounting_start_date}'"))
    with engine.connect() as c:
        money_before=c.execute(text('SELECT coalesce(sum(amount),0) FROM student_payments WHERE voided_at IS NULL AND org_id=:o'),{'o':ids['org']}).scalar_one()
    assert money_before==1350000
    # --- the dry run of the conversion report on this stage-like data: the migration runs INSIDE its transaction, nothing is kept
    import importlib.util,sys
    spec=importlib.util.spec_from_file_location('pay2_convert_report',str(Path(__file__).resolve().parents[1]/'scripts'/'pay2_convert_report.py'))
    report_module=importlib.util.module_from_spec(spec); spec.loader.exec_module(report_module)
    with engine.begin() as c:
        c.execute(text("INSERT INTO group_prices (id,group_id,amount,valid_from,created_at) VALUES (gen_random_uuid(),:g,500000,'2000-01-01',now())"),{'g':ids['group']})
    with engine.connect() as c:
        tx=c.begin()
        try:
            # the ORM of today also reads the LOGIC-B columns, which a database at PAYROLL_RATES has not got yet (rolled back below)
            c.execute(text("ALTER TABLE groups ADD COLUMN IF NOT EXISTS inactive_intervals jsonb NOT NULL DEFAULT '[]'::jsonb, ADD COLUMN IF NOT EXISTS archive_reason varchar(300), ADD COLUMN IF NOT EXISTS exam_release_pending boolean NOT NULL DEFAULT false"))
            c.execute(text("ALTER TABLE group_attendance ADD COLUMN IF NOT EXISTS version integer NOT NULL DEFAULT 1"))
            # ... and the columns of #154 (purpose / money / make-up of the one-off lessons, ``made_up_by`` of the attendance row)
            c.execute(text("ALTER TABLE group_schedule_one_off_lessons ADD COLUMN IF NOT EXISTS purpose varchar(10), ADD COLUMN IF NOT EXISTS money varchar(10), ADD COLUMN IF NOT EXISTS made_up_lesson_id varchar(100), ADD COLUMN IF NOT EXISTS made_up_date varchar(10)"))
            c.execute(text("ALTER TABLE group_attendance ADD COLUMN IF NOT EXISTS made_up_by varchar(100)"))
            # ... and the columns of the console pack (users.platform_block*, organizations.blocked_*)
            c.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS platform_blocked_at timestamp, ADD COLUMN IF NOT EXISTS platform_block_reason text, ADD COLUMN IF NOT EXISTS platform_block_was_active boolean"))
            c.execute(text("ALTER TABLE organizations ADD COLUMN IF NOT EXISTS blocked_at timestamp, ADD COLUMN IF NOT EXISTS blocked_reason text"))
            rep=report_module.build_report(c,today=date(2026,10,21))
        finally:
            tx.rollback()
    assert rep['migrated_in_transaction'] and rep['ok'], rep
    t=rep['orgs'][str(ids['org'])]
    assert t['money_before']==t['money_after']==money_before and t['money_equal'] and t['distribution_ok']
    assert t['centre']['payments_ok'] and t['centre']['transfers_ok'] and t['centre']['distribution_ok'] and t['centre']['payments_sum']==money_before
    mine={r['membership_id']:r for r in rep['memberships']}
    first=mine[str(ids['membership'])]
    assert (first['old_status'],first['old_paid_until'])==('active','2026-11-04')       # old: the periods said paid until 04.11
    assert 'student_payments' in inspect(engine).get_table_names() and 'method' not in {c['name'] for c in inspect(engine).get_columns('student_payments')}  # rolled back
    text_out=report_module.render(rep,5)
    assert 'РАВНЫ' in text_out and 'ИТОГ: деньги равны' in text_out
    command.upgrade(config,'head')
    with engine.connect() as c:
        assert c.execute(text('SELECT version_num FROM alembic_version')).scalar_one()==HEAD
        assert c.execute(text('SELECT coalesce(sum(amount),0) FROM student_payments WHERE voided_at IS NULL AND org_id=:o'),{'o':ids['org']}).scalar_one()==money_before
        kinds=dict(c.execute(text('SELECT id,kind FROM student_payments')).fetchall())
        assert kinds[carry]=='legacy_carry' and {kinds[orig],kinds[voided],kinds[pkg],kinds[cal2]}=={'payment'}
        assert {r[0] for r in c.execute(text('SELECT DISTINCT method FROM student_payments')).fetchall()}=={'unknown'}
        links=c.execute(text('SELECT from_membership_id,to_membership_id,at,student_id,created_by FROM membership_transfers')).fetchall()
        assert [(l[0],l[1],l[2],l[3],l[4]) for l in links]==[(ids['membership'],member_b,moved,ids['student'],None)]
        settings={r[0]:r[1] for r in c.execute(text('SELECT id,settings FROM organizations')).fetchall()}
        assert settings[ids['org']]['payments']['accounting_start_date']=='2026-09-15'        # the earliest live payment day (the voided 01.09 is ignored)
        assert settings[org2]['payments']['accounting_start_date']==str(c.execute(text("SELECT (now() AT TIME ZONE 'Asia/Tashkent')::date")).scalar_one())
        assert settings[org2]['payments']['period_mode']=='lessons'                              # the rest of the settings is kept
    cols={c['name']:c for c in inspect(engine).get_columns('student_payments')}
    assert cols['valid_from']['nullable'] and not cols['method']['nullable'] and not cols['kind']['nullable']
    idx={i['name'] for i in inspect(engine).get_indexes('student_payments')}
    assert {'ix_sp_org_paid_on','ix_sp_membership_live','uq_student_payments_idempotency'}<=idx
    assert not ({'uq_student_payments_active_dup','idx_student_payments_org_valid_to'}&idx)
    assert {'student_writeoffs','student_refunds','membership_transfers','payment_receipt_snapshots'}<=set(inspect(engine).get_table_names())
    # --- the new rules: a payment without a period; two equal payments on one day are legal; the idempotency key is unique
    new=dict(o=ids['org'],m=ids['membership'],s=ids['student'],g=ids['group'],u=user,paid=date(2026,10,21))
    sql=("INSERT INTO student_payments (id,org_id,membership_id,student_id,group_id,amount,paid_on,created_by,method,idempotency_key) "
         "VALUES (:i,:o,:m,:s,:g,:a,:paid,:u,:method,:key)")
    key=uuid4()
    with engine.begin() as c:
        c.execute(text(sql),{**new,'i':uuid4(),'a':300000,'method':'cash','key':key})
        c.execute(text(sql),{**new,'i':uuid4(),'a':300000,'method':'cash','key':None})
        c.execute(text(sql),{**new,'i':uuid4(),'a':300000,'method':'cash','key':None})
    for bad in ({'method':'bitcoin','key':None},{'method':'cash','key':key}):
        with pytest.raises(IntegrityError):
            with engine.begin() as c:
                c.execute(text(sql),{**new,'i':uuid4(),'a':1,**bad})
    # --- write-offs / refunds / transfers: the guards
    wo=("INSERT INTO student_writeoffs (id,org_id,membership_id,student_id,group_id,amount,reason,effective_on,created_by) "
        "VALUES (gen_random_uuid(),:o,:m,:s,:g,:a,:r,'2026-10-21',:u)")
    rf=("INSERT INTO student_refunds (id,org_id,membership_id,student_id,group_id,amount,method,effective_on,created_by) "
        "VALUES (gen_random_uuid(),:o,:m,:s,:g,:a,:method,'2026-10-21',:u)")
    with engine.begin() as c:
        c.execute(text(wo),{**new,'a':40000,'r':'болезнь'})
        c.execute(text(rf),{**new,'a':10000,'method':'cash'})
    for stmt,extra in ((wo,{'a':0,'r':'болезнь'}),(wo,{'a':1,'r':' ab '}),(rf,{'a':1,'method':'unknown'}),(rf,{'a':-5,'method':'cash'})):
        with pytest.raises(IntegrityError):
            with engine.begin() as c:
                c.execute(text(stmt),{**new,**extra})
    with pytest.raises(IntegrityError):
        with engine.begin() as c:
            c.execute(text("INSERT INTO membership_transfers (id,org_id,student_id,from_membership_id,to_membership_id,at) VALUES (gen_random_uuid(),:o,:s,:m,:m,now())"),new)
    with engine.begin() as c:
        for action in ('payment_writeoff','payment_writeoff_void','payment_refund','payment_refund_void'):
            c.execute(text("INSERT INTO group_audit (id,org_id,group_id,actor_id,actor_role,action,snapshot) VALUES (gen_random_uuid(),:o,:g,:u,'owner','"+action+"','{}')"),new)
    # --- idempotent: again, and the revision again after a stamp
    command.upgrade(config,'head')
    command.stamp(config,PAYROLL_RATES)
    command.upgrade(config,'head')
    with engine.connect() as c:
        assert c.execute(text('SELECT count(*) FROM membership_transfers')).scalar_one()==1
        assert c.execute(text('SELECT version_num FROM alembic_version')).scalar_one()==HEAD
    # --- downgrade: every payment has a period again, equal payments of one day are voided but one, the new tables go
    command.downgrade(config,PAYROLL_RATES)
    with engine.connect() as c:
        assert not ({'student_writeoffs','student_refunds','membership_transfers','payment_receipt_snapshots'}&set(inspect(engine).get_table_names()))
        assert c.execute(text('SELECT count(*) FROM student_payments WHERE valid_from IS NULL')).scalar_one()==0
        live=c.execute(text("SELECT amount,paid_on,valid_from,valid_to,lessons_count FROM student_payments WHERE paid_on='2026-10-21' AND voided_at IS NULL")).fetchall()
        assert live==[(300000,date(2026,10,21),date(2026,10,21),date(2026,11,20),None)]
        assert c.execute(text("SELECT count(*) FROM student_payments WHERE void_reason LIKE 'downgrade PAY-2%'")).scalar_one()==2
        assert c.execute(text("SELECT count(*) FROM group_audit WHERE action LIKE 'payment_%'")).scalar_one()==0
    with pytest.raises(IntegrityError):
        with engine.begin() as c:
            c.execute(text("INSERT INTO group_audit (id,org_id,group_id,actor_id,actor_role,action,snapshot) VALUES (gen_random_uuid(),:o,:g,:u,'owner','payment_refund','{}')"),new)
    command.upgrade(config,'head')
    with engine.begin() as c:        # leave nothing behind for the checks that may follow
        c.execute(text('DELETE FROM payment_receipt_snapshots'))
        c.execute(text('DELETE FROM student_refunds')); c.execute(text('DELETE FROM student_writeoffs')); c.execute(text('DELETE FROM membership_transfers'))
        c.execute(text('UPDATE student_payments SET carried_from_payment_id=NULL, replaces_payment_id=NULL'))
        c.execute(text('DELETE FROM student_payments'))



def _check_logic_a_objects(engine,config,ids):
    """LOGIC-A on a stage-like dataset: the five tables, the seeds (divisor per organisation, lesson type per group), the F13 markers of the
    closed memberships that had an individual price with a sum, money untouched, the new CHECKs, idempotent, rollback."""
    from datetime import date,datetime
    from sqlalchemy.exc import IntegrityError
    from src.models.auth import User,Organization,Group,Student,StudentGroup
    command.downgrade(config,PAY2)
    names={'student_pauses','membership_pauses','membership_opening_balances','group_lesson_type_history','payroll_divisors','payroll_payouts'}
    assert not (names & set(inspect(engine).get_table_names()))
    # M2: a value another revision put into a CHECK (pack B's group_archived) must survive the upgrade and the downgrade of this one
    def check_def(table,name):
        with engine.connect() as c:
            return c.execute(text("SELECT pg_get_constraintdef(c.oid) FROM pg_constraint c JOIN pg_class t ON t.oid=c.conrelid WHERE t.relname=:t AND c.conname=:n"),{'t':table,'n':name}).scalar_one()
    with engine.begin() as c:
        c.execute(text("ALTER TABLE group_audit DROP CONSTRAINT ck_group_audit_action"))
        c.execute(text("ALTER TABLE group_audit ADD CONSTRAINT ck_group_audit_action CHECK (action IN ('created','updated','teacher_changed','student_archived','student_restored','group_price_set','membership_price_set','group_price_cancelled','membership_price_cancelled','payment_writeoff','payment_writeoff_void','payment_refund','payment_refund_void','group_archived'))"))
    user,org_b,grp_pair,grp_b=uuid4(),uuid4(),uuid4(),uuid4()
    studs={k:uuid4() for k in ('closed_price','closed_plain','closed_null','active_price','closed_returned','closed_future')}
    mems={k:uuid4() for k in studs}
    with Session(engine) as db:
        legacy_add(db,User,id=user,email=f'{user}@m.invalid',password_hash='synthetic-only',first_name='L',last_name='A',role='owner')
        legacy_add(db,Organization,id=org_b,name='LOGIC-A second centre',status='active',settings={'payroll':{'divisor':13}}); db.flush()
        # raw SQL: the schema is the one BEFORE LOGIC-A and before pack B (the ORM group has columns of both, e.g. inactive_intervals)
        for gid,gname,glt in ((grp_pair,'Pair group','pair'),(grp_b,'Plain group','group')):
            db.execute(text("INSERT INTO groups (id,org_id,name,class_level,lesson_type,created_at,updated_at) VALUES (:i,:o,:n,9,:t,now(),now())"),
                       {'i':gid,'o':org_b,'n':gname,'t':glt})
        db.flush()
        for k,sid in studs.items():
            db.add(Student(id=sid,org_id=org_b,first_name=k,last_name='Logic',class_level=9,status='active')); db.flush()
            db.add(StudentGroup(id=mems[k],student_id=sid,group_id=grp_pair,is_active=k.startswith('active') or k=='closed_returned',
                                joined_at=datetime(2026,9,1,5),left_at=None if k.startswith('active') or k=='closed_returned' else datetime(2026,10,10,4,0)))
        db.commit()
    with engine.begin() as c:
        for k,mid in mems.items():
            c.execute(text("DELETE FROM student_group_periods WHERE membership_id=:m"),{'m':mid})
            left=None if k.startswith('active') or k=='closed_returned' else datetime(2026,10,10,4,0)
            c.execute(text("INSERT INTO student_group_periods (id,membership_id,joined_at,left_at) VALUES (gen_random_uuid(),:m,'2026-09-01 05:00',:l)"),{'m':mid,'l':left})
        ov="INSERT INTO membership_price_overrides (id,membership_id,amount,valid_from,reason,created_at) VALUES (gen_random_uuid(),:m,:a,:d,:r,now())"
        c.execute(text(ov),{'m':mems['closed_price'],'a':400000,'d':date(2026,9,1),'r':'скидка'})
        c.execute(text(ov),{'m':mems['active_price'],'a':400000,'d':date(2026,9,1),'r':'скидка'})
        c.execute(text(ov),{'m':mems['closed_returned'],'a':400000,'d':date(2026,9,1),'r':'скидка'})
        c.execute(text(ov),{'m':mems['closed_null'],'a':400000,'d':date(2026,9,1),'r':'скидка'})
        c.execute(text(ov),{'m':mems['closed_null'],'a':None,'d':date(2026,10,1),'r':None})          # the group price again before he left
        c.execute(text(ov),{'m':mems['closed_future'],'a':400000,'d':date(2026,9,1),'r':'скидка'})
        c.execute(text(ov),{'m':mems['closed_future'],'a':300000,'d':date(2026,10,11),'r':'новая'})   # a row of the day after he left: not overwritten
        c.execute(text("INSERT INTO student_payments (id,org_id,membership_id,student_id,group_id,amount,paid_on,created_by,method) "
                       "VALUES (gen_random_uuid(),:o,:m,:s,:g,250000,'2026-10-01',:u,'cash')"),
                  {'o':org_b,'m':mems['closed_price'],'s':studs['closed_price'],'g':grp_pair,'u':user})
        before_money=c.execute(text('SELECT count(*),COALESCE(sum(amount),0) FROM student_payments')).fetchone()
        before_overrides=c.execute(text('SELECT count(*) FROM membership_price_overrides')).scalar_one()
        before_mine=c.execute(text('SELECT count(*) FROM membership_price_overrides WHERE membership_id = ANY(:ids)'),{'ids':list(mems.values())}).scalar_one()
    command.upgrade(config,'head')
    assert names-{'student_pauses'} <= set(inspect(engine).get_table_names())
    for table,name,mine in (('group_audit','ck_group_audit_action',('student_left_group','group_lesson_type_set')),
                            ('group_attendance_audit','ck_group_attendance_audit_action',('excuse_closed_pause',)),('attendance_excuses','ck_attendance_excuse_status',('not_required',))):
        assert all(f"'{v}'" in check_def(table,name) for v in mine),(table,check_def(table,name))
    assert "'group_archived'" in check_def('group_audit','ck_group_audit_action') and "'excuse_file_removed'" in check_def('group_attendance_audit','ck_group_attendance_audit_action')
    with engine.connect() as c:
        assert c.execute(text('SELECT version_num FROM alembic_version')).scalar_one()==HEAD
        assert tuple(c.execute(text('SELECT count(*),COALESCE(sum(amount),0) FROM student_payments')).fetchone())==tuple(before_money)   # money untouched
        mine=list(mems.values())
        markers={r[0]:(r[1],r[2]) for r in c.execute(text("SELECT membership_id,valid_from,created_by FROM membership_price_overrides WHERE reason='left_group' AND amount IS NULL AND membership_id = ANY(:ids)"),{'ids':mine}).fetchall()}
        assert markers=={mems['closed_price']:(date(2026,10,11),None)}      # left 10.10 (09:00 Tashkent): the price ends on 11.10; the others are not touched
        assert c.execute(text('SELECT count(*) FROM membership_price_overrides WHERE membership_id = ANY(:ids)'),{'ids':mine}).scalar_one()==before_mine+1
        div={r[0]:(r[1],r[2]) for r in c.execute(text('SELECT org_id,divisor,valid_from FROM payroll_divisors')).fetchall()}
        assert div[org_b]==(13,date(2020,1,1)) and div[ids['org']][0]==12 and len(div)==c.execute(text('SELECT count(*) FROM organizations')).scalar_one()
        lt={r[0]:r[1] for r in c.execute(text('SELECT group_id,lesson_type FROM group_lesson_type_history')).fetchall()}
        assert lt[grp_pair]=='pair' and lt[grp_b]=='group' and len(lt)==c.execute(text('SELECT count(*) FROM groups')).scalar_one()
    # --- the guards of the new tables
    new=dict(o=org_b,m=mems['active_price'],s=studs['active_price'],g=grp_pair,u=user)
    pause="INSERT INTO membership_pauses (id,org_id,membership_id,student_id,group_id,first_day,last_day,created_by) VALUES (gen_random_uuid(),:o,:m,:s,:g,:a,:b,:u)"
    with engine.begin() as c:
        c.execute(text(pause),{**new,'a':date(2026,10,11),'b':date(2026,10,20)})
    for a,b in ((date(2026,10,5),date(2026,10,1)),(date(2026,1,1),date(2027,6,1))):                  # reversed / longer than 366 days
        with pytest.raises(IntegrityError):
            with engine.begin() as c:
                c.execute(text(pause),{**new,'a':a,'b':b})
    with engine.connect() as c:
        exclusion=c.execute(text("SELECT count(*) FROM pg_constraint WHERE conname='ex_membership_pauses_no_overlap'")).scalar_one()
    if exclusion:
        with pytest.raises(IntegrityError):
            with engine.begin() as c:
                c.execute(text(pause),{**new,'a':date(2026,10,20),'b':date(2026,10,25)})              # overlaps a live pause
    with engine.begin() as c:                                    # a voided pause does not block; void is all-or-none
        c.execute(text("UPDATE membership_pauses SET voided_at=now(),voided_by=:u,void_reason='x'"),new)
        c.execute(text(pause),{**new,'a':date(2026,10,15),'b':date(2026,10,18)})
    with pytest.raises(IntegrityError):
        with engine.begin() as c:
            c.execute(text("UPDATE membership_pauses SET voided_at=now() WHERE voided_at IS NULL"))
    ob="INSERT INTO membership_opening_balances (id,org_id,membership_id,student_id,group_id,paid_until,debt_amount,debt_on,source,created_by) VALUES (gen_random_uuid(),:o,:m,:s,:g,:pu,:da,:do,:src,:u)"
    with engine.begin() as c:
        c.execute(text(ob),{**new,'pu':date(2026,10,31),'da':250000,'do':date(2026,9,30),'src':'manual'})
    for bad in ({'pu':None,'da':None,'do':None,'src':'manual'},{'pu':None,'da':0,'do':date(2026,9,30),'src':'manual'},{'pu':None,'da':5,'do':None,'src':'manual'},
                {'pu':date(2026,10,1),'da':None,'do':None,'src':'ftp'},{'pu':date(2026,10,1),'da':None,'do':None,'src':'manual'}):   # the last: a second LIVE one
        with pytest.raises(IntegrityError):
            with engine.begin() as c:
                c.execute(text(ob),{**new,**bad})
    with pytest.raises(IntegrityError):
        with engine.begin() as c:
            c.execute(text("INSERT INTO payroll_divisors (id,org_id,valid_from,divisor) VALUES (gen_random_uuid(),:o,'2026-10-15',12)"),new)
    with pytest.raises(IntegrityError):
        with engine.begin() as c:
            c.execute(text("INSERT INTO payroll_divisors (id,org_id,valid_from,divisor) VALUES (gen_random_uuid(),:o,'2026-10-01',32)"),new)
    with engine.begin() as c:
        for action in ('membership_pause_set','membership_pause_void','opening_balance_set','opening_balance_void','group_lesson_type_set','student_left_group'):
            c.execute(text("INSERT INTO group_audit (id,org_id,group_id,actor_id,actor_role,action,snapshot) VALUES (gen_random_uuid(),:o,:g,:u,'owner','"+action+"','{}')"),new)
    # --- idempotent: the revision again after a stamp changes nothing (no second seed, no second marker)
    with engine.connect() as c:
        counts=tuple(c.execute(text('SELECT (SELECT count(*) FROM payroll_divisors),(SELECT count(*) FROM group_lesson_type_history),'
                                    "(SELECT count(*) FROM membership_price_overrides WHERE reason='left_group')")).fetchone())
    command.stamp(config,PAY2)
    command.upgrade(config,'head')
    with engine.connect() as c:
        assert tuple(c.execute(text('SELECT (SELECT count(*) FROM payroll_divisors),(SELECT count(*) FROM group_lesson_type_history),'
                                    "(SELECT count(*) FROM membership_price_overrides WHERE reason='left_group')")).fetchone())==counts
        assert c.execute(text('SELECT count(*) FROM membership_pauses')).scalar_one()==2           # data kept
    # --- rollback: tables, markers and the audit rows of the new actions go; the old CHECK is back; PAY-2 data is untouched
    command.downgrade(config,PAY2)
    with engine.connect() as c:
        assert not (names & set(inspect(engine).get_table_names()))
        assert c.execute(text("SELECT count(*) FROM membership_price_overrides WHERE reason='left_group'")).scalar_one()==0
        assert c.execute(text("SELECT count(*) FROM group_audit WHERE action IN ('membership_pause_set','student_left_group')")).scalar_one()==0
        assert tuple(c.execute(text('SELECT count(*),COALESCE(sum(amount),0) FROM student_payments')).fetchone())==tuple(before_money)
        assert c.execute(text('SELECT count(*) FROM membership_price_overrides')).scalar_one()==before_overrides
        assert c.execute(text('SELECT version_num FROM alembic_version')).scalar_one()==PAY2
    # the chain is linear now (LOGIC-A -> LOGIC-B): a rollback to PAY-2 removes the values of both packs; that A's rollback alone keeps B's values is tested
    # in tests/test_integration_ab_pg.py::test_the_linear_chain_composes_the_check_constraints_of_a_and_b
    assert "'group_archived'" not in check_def('group_audit','ck_group_audit_action') and "'student_left_group'" not in check_def('group_audit','ck_group_audit_action')
    assert "'created'" in check_def('group_audit','ck_group_audit_action')
    assert "'not_required'" not in check_def('attendance_excuses','ck_attendance_excuse_status') and "'excuse_closed_pause'" not in check_def('group_attendance_audit','ck_group_attendance_audit_action')
    assert "'excuse_file_removed'" in check_def('group_attendance_audit','ck_group_attendance_audit_action')
    with pytest.raises(IntegrityError):
        with engine.begin() as c:
            c.execute(text("INSERT INTO group_audit (id,org_id,group_id,actor_id,actor_role,action,snapshot) VALUES (gen_random_uuid(),:o,:g,:u,'owner','student_left_group','{}')"),new)
    command.upgrade(config,'head')
    with engine.begin() as c:        # leave nothing behind for the checks that may follow
        c.execute(text('DELETE FROM student_payments WHERE org_id=:o'),{'o':org_b})
        c.execute(text('DELETE FROM membership_pauses')); c.execute(text('DELETE FROM membership_opening_balances'))
