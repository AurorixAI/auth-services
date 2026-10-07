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
        assert connection.execute(text('SELECT version_num FROM alembic_version')).scalar_one()=='r8s9t0u1v2w'
    # Existing application loggers must survive the in-process Alembic setup.
    import logging
    assert not logging.getLogger('src.api.main_v2').disabled
    engine.dispose()
