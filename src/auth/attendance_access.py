"""Pure authorization helpers for schedule and attendance routes."""


def is_org_admin(user: dict) -> bool:
    """Whether the authenticated member has organisation-wide access."""
    return user.get("org_role") in {"owner", "admin"} or user.get("role") in {
        "owner",
        "admin",
        "superadmin",
    }


def is_org_manager(user: dict) -> bool:
    """Owner/admin *of this organisation*, from the live membership role only.

    Stricter than ``is_org_admin``: the global ``users.role`` does not count,
    so a teacher who is an "admin" elsewhere cannot correct completed lessons
    or read an administrator's review notes here.
    """
    return user.get("org_role") in {"owner", "admin"}
