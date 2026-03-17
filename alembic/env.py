"""
ALGO V1 - Alembic env.py
Настройка Alembic для автогенерации миграций из SQLAlchemy моделей

Все новые изменения схемы — ТОЛЬКО через:
  alembic revision --autogenerate -m "описание"
  alembic upgrade head
"""

import os
import sys
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool
from alembic import context

# Добавляем корень проекта в sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# Импорт моделей — ВСЕ модули, чтобы Base.metadata содержал полную схему
from src.models import Base  # noqa: F401 — 17 core/presentation/analytics моделей
from src.models.exam import GeneratedExam, ExamSession  # noqa: F401 — exam модели
from src.auth.models import (  # noqa: F401 — auth/B2B модели
    Organization, User, OrganizationMember, Invitation,
    Student, Group, StudentGroup, DiagnosticInvite,
    StudentDiagnosticResult,
)

# this is the Alembic Config object
config = context.config

# Установка DATABASE_URL из переменной окружения
database_url = os.getenv(
    "DATABASE_URL",
    "postgresql://postgres:password@localhost:5432/algo_v1"
)
config.set_main_option("sqlalchemy.url", database_url)

# Настройка логирования
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Метаданные моделей для autogenerate
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
