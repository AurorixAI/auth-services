"""Money-1 A4 (I-6): the rights on money are ONE - the owner or an administrator the owner allowed (``require_payments_access`` / ``money_rights_flag``).

The guard walks EVERY route of EVERY ``*router*.py`` of the application and fails when a route that moves money has no such dependency:
the list of the money routes is explicit (below); a route NOT in the list whose handler calls a service that moves money is a failure too ("a new money route without the right").
Per LESSONS 94 the test first asserts that the walk FOUND the routes it is about."""
import inspect
import re
from uuid import uuid4

import pytest
from fastapi.routing import APIRoute

from src.api import main_v2
from src.auth import dependencies

P = "/api/v1/auth"

# route-level: the whole route moves or shows money (``require_payments_access`` among its dependencies)
WHOLE = {
    # excuses: a decision moves the pack of the student and the pay of the teacher
    ("POST", f"{P}/org/attendance/excuses/{{excuse_id}}/review"), ("POST", f"{P}/org/attendance/excuses/{{excuse_id}}/reset"), ("POST", f"{P}/org/attendance/excuses/review-bulk"),
    # pauses, the type of lessons of a group (the pay of the teacher backwards), the kind of a cancellation (the deduction of the students)
    ("POST", f"{P}/org/memberships/{{membership_id}}/pauses"), ("PATCH", f"{P}/org/pauses/{{pause_id}}"), ("POST", f"{P}/org/pauses/{{pause_id}}/void"),
    ("PUT", f"{P}/org/groups/{{group_id}}/lesson-type"), ("PUT", f"{P}/org/groups/{{group_id}}/attendance/cancellation-kind"),
    # the return of a held lesson, the impact of its correction, the reopening of a closed month
    ("POST", f"{P}/org/groups/{{group_id}}/attendance/return"), ("POST", f"{P}/org/groups/{{group_id}}/attendance/impact"), ("POST", f"{P}/org/payroll-periods/{{month}}/reopen"),
    # payments (the ones of the earlier packs; the full list is the one below)
    ("POST", f"{P}/org/memberships/{{membership_id}}/payments"), ("POST", f"{P}/org/payments/{{payment_id}}/void"), ("POST", f"{P}/org/payments/{{payment_id}}/replace"),
    ("POST", f"{P}/org/memberships/{{membership_id}}/writeoffs"), ("POST", f"{P}/org/memberships/{{membership_id}}/refunds"),
    ("POST", f"{P}/org/writeoffs/{{writeoff_id}}/void"), ("POST", f"{P}/org/refunds/{{refund_id}}/void"),
    ("PUT", f"{P}/org/groups/{{group_id}}/price"), ("PUT", f"{P}/org/memberships/{{membership_id}}/price-override"), ("PUT", f"{P}/org/memberships/{{membership_id}}/opening-balance"),
}

# the part of the route that moves money asks the flag (``money_rights_flag``): the route is used by teachers / administrators for the rest
CONDITIONAL = {
    ("POST", f"{P}/org/groups/{{group_id}}/attendance"),                    # a correction of a held lesson
    ("POST", f"{P}/org/groups/{{group_id}}/attendance/reconcile"),          # a lesson formalised backwards, «кто провёл»
    ("PUT", f"{P}/org/groups/{{group_id}}/lesson-teacher"),                 # who taught a held lesson (ЗП-02)
    ("POST", f"{P}/org/groups/{{group_id}}/lessons/restore"),               # the return of «Урока не было» for a lesson that had begun
    ("POST", f"{P}/org/groups/{{group_id}}/students/{{student_id}}"),       # a transfer
    ("POST", f"{P}/org/groups/{{group_id}}/students/bulk"),                 # a bulk transfer
}

# the money check is inside the handler (the answer depends on the student's money): the handler passes ``has_payments_access`` to the service
INLINE = {
    ("DELETE", f"{P}/org/groups/{{group_id}}/students/{{student_id}}"),     # «Убрать из группы» with an advance
    ("POST", f"{P}/org/groups/{{group_id}}/students/{{student_id}}/leave"),  # the same with the settlement of the money
}

# the owner only (the money of the teachers: LESSONS 45) - stricter than the money right
OWNER_ONLY_PREFIXES = (f"{P}/org/payroll/", f"{P}/org/settings/payments")

# a handler that calls one of these moves money: the route must be in a list above (or be owner-only)
MONEY_CALLS = re.compile(
    r"PaymentService|PauseService|LessonTypeService|OpeningService|LeaveService|change_cancellation_kind|return_completed_lesson|reopen_month|correction_impact|"
    r"\.create_writeoff|\.create_refund|\.void_writeoff|\.void_refund|\.set_override|set_group_price|write_override|PayrollPayoutService|PrPayoutService|openings_service|ExcuseService\(db\)\.review|"
    r"\.review\(|\.reset\(|\.review_bulk\(|restore_lesson|\.save_group_attendance|\.reconcile_missed|LessonTeachers\(.*\)\.put|bulk_add_students|add_student_to_group")


def walk():
    """{(METHOD, full path): (set of dependency callables, route)} of every APIRoute of the application."""
    found = {}

    def calls(dependant):
        out = set()
        for sub in dependant.dependencies:
            if sub.call is not None:
                out.add(sub.call)
            out |= calls(sub)
        return out

    def go(routes, prefix, inherited):
        for route in routes:
            if hasattr(route, "original_router"):
                ctx = route.include_context
                go(route.original_router.routes, prefix + ctx.prefix, inherited | {d.dependency for d in ctx.dependencies})
            elif isinstance(route, APIRoute):
                for method in route.methods:
                    found[(method, prefix + route.path)] = (calls(route.dependant) | inherited, route)

    go(main_v2.app.routes, "", set())
    return found


ROUTES = walk()


def test_the_walk_found_the_routes_this_guard_is_about():
    """LESSONS 94: a guard that found nothing proves nothing."""
    assert len(ROUTES) >= 200
    missing = [r for r in sorted(WHOLE | CONDITIONAL | INLINE) if r not in ROUTES]
    assert not missing, f"listed routes that do not exist (renamed? the guard must follow): {missing}"
    assert dependencies.require_payments_access and dependencies.money_rights_flag


def test_every_whole_money_route_has_the_one_dependency():
    bad = [r for r in sorted(WHOLE) if dependencies.require_payments_access not in ROUTES[r][0]]
    assert not bad, f"money routes without require_payments_access: {bad}"


def test_every_conditional_money_route_asks_the_flag():
    bad = [r for r in sorted(CONDITIONAL) if dependencies.money_rights_flag not in ROUTES[r][0]]
    assert not bad, f"routes whose money part does not ask money_rights_flag: {bad}"


def test_the_inline_routes_pass_the_right_to_the_service():
    for key in INLINE:
        source = inspect.getsource(ROUTES[key][1].endpoint)
        assert "has_payments_access(user, db)" in source, key


def test_every_payment_module_route_is_behind_the_money_right_except_the_named_ones():
    """All of the payments / openings / income routers: ``require_payments_access`` (or the owner-only settings) - a new one without it fails here."""
    modules = ("payments_router", "payments_pay2_router", "openings_router", "payments_income_router")
    seen = [(k, v) for k, v in ROUTES.items() if v[1].endpoint.__module__.rsplit(".", 1)[-1] in modules]
    assert len(seen) >= 40, len(seen)
    owner_only = {dependencies.require_payments_settings, dependencies.require_org_owner}
    bad = [k for k, (deps, _) in seen if dependencies.require_payments_access not in deps and not (deps & owner_only)]
    assert not bad, f"payment routes without the money right: {bad}"


def test_a_route_that_calls_a_money_service_is_in_one_of_the_lists():
    """The walk over ALL the routers: a handler whose source calls a service that moves money must be a listed money route (or owner-only)."""
    listed = WHOLE | CONDITIONAL | INLINE
    unlisted = []
    scanned = 0
    for key, (deps, route) in sorted(ROUTES.items()):
        module = route.endpoint.__module__.rsplit(".", 1)[-1]
        if "router" not in module or module.startswith("platform") or module in ("payments_router", "payments_pay2_router", "openings_router", "payments_income_router"):
            continue
        if key[0] == "GET":
            continue                                  # a read moves nothing
        scanned += 1
        if key[1].startswith(OWNER_ONLY_PREFIXES) or dependencies.require_payments_access in deps or key in listed:
            continue
        if MONEY_CALLS.search(inspect.getsource(route.endpoint)):
            unlisted.append((key, module))
    assert scanned >= 60, scanned
    assert not unlisted, f"routes that call a service that moves money but are in no list of tests/test_money1_rights_guard.py: {unlisted}"


# ---------------------------------------------------------------------------------------------------- the rights in action (a few routes, end to end)
from tests.ops_helpers import grant_money_right  # noqa: E402
from tests.pg_world import make_client, needs_pg, pg_session, world  # noqa: E402,F401  (fixtures)


@needs_pg
def test_an_administrator_without_the_right_is_refused_on_the_money_routes_and_with_it_he_is_not(world):
    m = world.db.query(__import__("src.models.auth", fromlist=["StudentGroup"]).StudentGroup).filter_by(student_id=world.students[0].id).one()
    admin = make_client(world, user=world.admin)
    from tests.pg_world import business_today

    pause = {"first_day": business_today().isoformat(), "last_day": business_today().isoformat()}
    assert admin.post(f"{P}/org/memberships/{m.id}/pauses", json=pause).status_code == 403
    assert admin.put(f"{P}/org/groups/{world.group.id}/lesson-type", json={"lesson_type": "pair"}).status_code == 403
    assert admin.post(f"{P}/org/attendance/excuses/review-bulk", json={"ids": [str(uuid4())], "decision": "confirmed"}).status_code == 403
    grant_money_right(world)
    assert admin.post(f"{P}/org/attendance/excuses/review-bulk", json={"ids": [str(uuid4())], "decision": "confirmed"}).status_code == 200
    assert make_client(world, user=world.teacher).post(f"{P}/org/attendance/excuses/review-bulk", json={"ids": [str(uuid4())], "decision": "confirmed"}).status_code == 403
