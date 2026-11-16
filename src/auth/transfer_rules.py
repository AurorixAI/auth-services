"""
ALGO - Money-2 / wave A: the ONE place of the rules of a transfer of a student between groups that are not money maths.

* ``resolve_sources`` (T1): a transfer closes exactly ONE membership - the source the caller named (``from_group_id``); without it only a student of
  ONE active group is moved (that group is the source), a student of several groups is asked «из какой».  The other groups of the student are not touched.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, List, Optional, Sequence
from uuid import UUID

from sqlalchemy.orm import Session

from src.models.auth import Group, StudentGroup

from . import ops_rules


def _names(db: Session, org_id: Any, group_ids: Sequence[Any]) -> Dict[Any, str]:
    if not group_ids:
        return {}
    return {g.id: g.name for g in db.query(Group).filter(Group.org_id == org_id, Group.id.in_(list(group_ids))).all()}


def resolve_sources(db: Session, org_id: Any, target_group_id: Any, mode: str, others: Sequence[StudentGroup], from_group_id: Optional[Any]) -> List[StudentGroup]:
    """The memberships a transfer closes (T1): ``[]`` for a plain adding or a student who is in no other group, else exactly the named source.

    ``others`` are the ACTIVE memberships of the student in groups other than the target (already locked by the caller).  Refusals (coded, nothing is written):
    422 ``SOURCE_ONLY_FOR_TRANSFER`` (a source with ``mode=add``), 422 ``TRANSFER_SOURCE_IS_TARGET``, 422 ``TRANSFER_SOURCE_REQUIRED`` (several active groups and no source: ``groups`` lists them),
    409 ``TRANSFER_SOURCE_GONE`` (the source is not an active group of the student any more - somebody moved or removed him a moment ago)."""
    if mode != "transfer":
        if from_group_id is not None:
            raise ops_rules.unprocessable("SOURCE_ONLY_FOR_TRANSFER", "Группа-источник нужна только при переводе")
        return []
    if from_group_id is not None:
        if str(from_group_id) == str(target_group_id):
            raise ops_rules.unprocessable("TRANSFER_SOURCE_IS_TARGET", "Нельзя перевести ученика в ту же группу, из которой он переводится")
        chosen = [m for m in others if str(m.group_id) == str(from_group_id)]
        if not chosen:
            name = _names(db, org_id, [from_group_id]).get(from_group_id if isinstance(from_group_id, UUID) else UUID(str(from_group_id)))
            where = f"в группе «{name}»" if name else "в выбранной группе"
            raise ops_rules.conflict("TRANSFER_SOURCE_GONE", f"Ученик уже не учится {where}: его только что перевели или убрали. Обновите страницу и проверьте группы ученика")
        return chosen
    if len(others) > 1:
        names = _names(db, org_id, [m.group_id for m in others])
        raise ops_rules.unprocessable(
            "TRANSFER_SOURCE_REQUIRED", "Ученик учится в нескольких группах — выберите, из какой его перевести",
            groups=[{"group_id": str(m.group_id), "group_name": names.get(m.group_id, "")} for m in others])
    return list(others)
