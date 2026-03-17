"""001_initial_schema

Create users table and core schema tables.
This is the initial migration that sets up the base database structure.

Revision ID: 001_initial
Revises: 
Create Date: 2024-01-20
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB


# revision identifiers
revision = '001_initial'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ================================================================
    # USERS TABLE
    # ================================================================
    op.create_table(
        'users',
        sa.Column('id', UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text('gen_random_uuid()')),
        
        # Authentication
        sa.Column('email', sa.String(255), unique=True, nullable=False),
        sa.Column('password_hash', sa.String(255), nullable=False),
        
        # Profile
        sa.Column('first_name', sa.String(100), nullable=False),
        sa.Column('last_name', sa.String(100), nullable=False),
        sa.Column('patronymic', sa.String(100), nullable=True),
        
        # Role
        sa.Column('role', sa.String(20), nullable=False, server_default='student'),
        
        # Student-specific
        sa.Column('class_level', sa.Integer, nullable=True),
        sa.Column('school_name', sa.String(300), nullable=True),
        
        # Teacher-specific
        sa.Column('subjects', JSONB, server_default='[]'),
        
        # Token management
        sa.Column('refresh_token_hash', sa.String(255), nullable=True),
        
        # Status
        sa.Column('is_active', sa.Boolean, server_default=sa.text('true')),
        sa.Column('is_verified', sa.Boolean, server_default=sa.text('false')),
        
        # Metadata
        sa.Column('last_login_at', sa.DateTime, nullable=True),
        sa.Column('login_count', sa.Integer, server_default='0'),
        
        # Settings
        sa.Column('language', sa.String(5), server_default='ru'),
        sa.Column('settings', JSONB, server_default='{}'),
        
        # Timestamps
        sa.Column('created_at', sa.DateTime, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime, server_default=sa.func.now()),
        
        # Constraints
        sa.CheckConstraint(
            "role IN ('student', 'teacher', 'admin', 'methodist')",
            name='ck_users_role'
        ),
        sa.CheckConstraint(
            "class_level IS NULL OR (class_level >= 1 AND class_level <= 11)",
            name='ck_users_class_level'
        ),
    )
    
    # Indexes
    op.create_index('idx_users_email', 'users', ['email'], unique=True)
    op.create_index('idx_users_role', 'users', ['role'])
    op.create_index('idx_users_active', 'users', ['is_active'])

    # ================================================================
    # TRIGGER: auto-update updated_at
    # ================================================================
    op.execute("""
        CREATE OR REPLACE FUNCTION update_updated_at_column()
        RETURNS TRIGGER AS $$
        BEGIN
            NEW.updated_at = now();
            RETURN NEW;
        END;
        $$ language 'plpgsql';
    """)
    
    op.execute("""
        CREATE TRIGGER update_users_updated_at
            BEFORE UPDATE ON users
            FOR EACH ROW
            EXECUTE FUNCTION update_updated_at_column();
    """)

    # ================================================================
    # SEED: Create default admin user
    # ================================================================
    # Password: admin123 (bcrypt hash)
    op.execute("""
        INSERT INTO users (email, password_hash, first_name, last_name, role, is_active, is_verified)
        VALUES (
            'admin@algo.kz',
            '$2b$12$LJ3mLxKx9Q0Mf4YzkKv0oOZT7hEK.7tQd6KV6P3j5fFdZ2eSbqIOe',
            'Admin',
            'ALGO',
            'admin',
            true,
            true
        )
        ON CONFLICT (email) DO NOTHING;
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS update_users_updated_at ON users")
    op.execute("DROP FUNCTION IF EXISTS update_updated_at_column()")
    op.drop_index('idx_users_active', table_name='users')
    op.drop_index('idx_users_role', table_name='users')
    op.drop_index('idx_users_email', table_name='users')
    op.drop_table('users')
