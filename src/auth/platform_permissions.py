"""Permissions of the platform console, as DATA: the catalogue, the template roles, and the lookups the checks use.

A role is a row of ``platform_roles`` with a JSON list of the keys below. The owner of the platform (``platform_staff.is_owner``)
may do everything whatever his role says. The keys marked ``active=False`` are reserved for the later packs («Войти как»,
content, finance, AI costs, analytics): a role may already carry them, nothing opens behind them yet.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List

TEAM_MANAGE = "team.manage"


@dataclass(frozen=True)
class Permission:
    key: str
    label: str
    group: str
    active: bool = True


CATALOG: tuple[Permission, ...] = (
    Permission("requests.view", "Заявки: просмотр", "Заявки"),
    Permission("requests.manage", "Заявки: обработка (статусы, заметки, одобрить, отказать)", "Заявки"),
    Permission("orgs.view", "Центры: просмотр", "Центры"),
    Permission("orgs.manage", "Центры: изменения (лимиты)", "Центры"),
    Permission("orgs.block", "Центры: блокировка", "Центры"),
    Permission("users.view", "Пользователи: просмотр", "Пользователи"),
    Permission("users.manage", "Пользователи: приглашение и сброс пароля", "Пользователи"),
    Permission("users.block", "Пользователи: блокировка", "Пользователи"),
    Permission("audit.view", "Журнал платформы", "Журнал"),
    Permission(TEAM_MANAGE, "Команда и роли (только владелец)", "Команда"),
    Permission("support.manage", "Поддержка: обращения", "Поддержка"),
    Permission("impersonate.view", "«Войти как»: просмотр", "Поддержка", active=False),
    Permission("impersonate.write", "«Войти как»: с изменениями", "Поддержка", active=False),
    Permission("content.view", "Контент", "Контент", active=False),
    Permission("finance.view", "Финансы", "Финансы", active=False),
    Permission("ai_costs.view", "Расходы на ИИ", "Аналитика", active=False),
    Permission("analytics.view", "Аналитика", "Аналитика", active=False),
)

ALL_KEYS: tuple[str, ...] = tuple(p.key for p in CATALOG)
_KEYSET = frozenset(ALL_KEYS)

OWNER_ROLE_NAME = "Владелец платформы"

# (name, permissions, is_system) - the same list the migration seeds.
TEMPLATE_ROLES: tuple[tuple[str, tuple[str, ...], bool], ...] = (
    (OWNER_ROLE_NAME, ALL_KEYS, True),
    ("Продажи", ("requests.view", "requests.manage", "orgs.view"), False),
    ("Поддержка", ("requests.view", "orgs.view", "users.view", "users.manage", "support.manage", "impersonate.view"), False),
    ("Контент", ("content.view",), False),
    ("Финансы", ("finance.view", "orgs.view"), False),
)


def is_known(key: str) -> bool:
    return key in _KEYSET


def unknown_keys(keys: Iterable[str]) -> List[str]:
    return [k for k in keys if k not in _KEYSET]


def normalise(keys: Iterable[str]) -> List[str]:
    """Catalogue order, no duplicates."""
    wanted = set(keys)
    return [k for k in ALL_KEYS if k in wanted]


def effective_permissions(is_owner: bool, role_permissions: Iterable[str] | None) -> frozenset[str]:
    """What a staff member may do: everything for the owner, else the keys of his role that the catalogue knows."""
    if is_owner:
        return _KEYSET
    return frozenset(k for k in (role_permissions or ()) if k in _KEYSET and k != TEAM_MANAGE)


def ensure_template_roles(db) -> None:
    """Create the template roles that are missing (the migration seeds them; a database built from the models does not have them)."""
    from src.models.platform import PlatformRole

    have = {name for (name,) in db.query(PlatformRole.name).filter(PlatformRole.is_template.is_(True)).all()}
    for name, keys, system in TEMPLATE_ROLES:
        if name not in have and not db.query(PlatformRole.id).filter(PlatformRole.name == name).first():
            db.add(PlatformRole(name=name, permissions=list(keys), is_template=True, is_system=system))
    db.flush()
