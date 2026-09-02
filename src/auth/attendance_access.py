"""Pure authorization helpers for schedule and attendance routes."""


def is_org_admin(user: dict) -> bool:
    """Whether the authenticated member has organisation-wide access."""
    return user.get("org_role") in {"owner", "admin"} or user.get("role") in {
        "owner",
        "admin",
        "superadmin",
    }
