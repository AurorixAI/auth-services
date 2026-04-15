#!/bin/bash
set -e

echo "============================================================"
echo "ALGO V1 - SEEDING SKELETON DATA"
echo "============================================================"

# Load Grade 5 Skills
echo "Loading Grade 5 skills and tasks..."
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" -f /docker-entrypoint-initdb.d/skeletons/grade_05.sql

# Load Grade 6 Skills
echo "Loading Grade 6 skills and tasks..."
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" -f /docker-entrypoint-initdb.d/skeletons/grade_06.sql

# Fix missing is_active defaults (fallback for Alembic created schemas)
echo "Fixing is_active columns for skills and tasks..."
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    UPDATE knowledge_hierarchy SET is_active = true WHERE is_active IS NULL;
    UPDATE tasks_master SET is_active = true WHERE is_active IS NULL;
EOSQL

echo "============================================================"
echo "SEEDING COMPLETE"
echo "============================================================"
