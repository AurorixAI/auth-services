"""Pure authorization helpers for schedule and attendance routes."""


def is_org_manager(user: dict) -> bool:
    """Owner/admin *of this organisation*, from the live membership role only (W1 S1: the one test of an administrator's right).

    The global ``users.role`` does not count: a teacher of this centre who owns or administers another one is a teacher here - he cannot
    open the schedule of every group, correct completed lessons or read an administrator's review notes.
    """
    return user.get("org_role") in {"owner", "admin"}
