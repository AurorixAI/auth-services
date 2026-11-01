"""The names a test database may have. A plain run uses ``a2_auth`` / ``a5_auth_migrations``; ``scripts/test_fast.sh`` (pytest-xdist) gives every worker its
own copy of the same database with the suffix ``_gw0``, ``_gw1`` ... (see ``tests/conftest.py``). The suffix is the only difference the guards accept."""
import re


def is_a2_db(url) -> bool:
    return bool(url) and re.search(r"/a2_auth(_gw\d+)?$", url) is not None


def is_migration_db(url) -> bool:
    return bool(url) and re.search(r"/a5_auth_migrations(_gw\d+)?$", url) is not None
