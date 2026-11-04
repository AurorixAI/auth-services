"""Money-1 A4 (I-6): the rights on money are ONE - the owner or an administrator the owner allowed (``require_payments_access`` / ``money_rights_flag``).

The guard walks EVERY route of the application and fails when a route that moves money has no such dependency.  R3 #10: it is a REGISTRY with a ban by default - every
non-GET route of ``/org/*`` is classified here (money: ``WHOLE`` / ``CONDITIONAL`` / ``INLINE`` / ``SERVICE_CHECKED`` / the owner-only prefixes / the payment modules, or ``NOT_MONEY``
with the reason); a new route without a record fails the test and makes its author decide.  The code is a second witness: a ``NOT_MONEY`` handler that calls a service
that moves money fails too.  Per LESSONS 94 the test first asserts that the walk FOUND the routes it is about."""
import inspect
import re
from uuid import uuid4

import pytest
from fastapi.routing import APIRoute

from src.api import main_v2
from src.auth import dependencies
from src.auth.one_off_purpose import OneOffPurpose

P = "/api/v1/auth"

# route-level: the whole route moves or shows money (``require_payments_access`` among its dependencies)
WHOLE = {
    # excuses: a decision moves the pack of the student and the pay of the teacher
    ("POST", f"{P}/org/attendance/excuses/{{excuse_id}}/review"), ("POST", f"{P}/org/attendance/excuses/{{excuse_id}}/reset"), ("POST", f"{P}/org/attendance/excuses/review-bulk"),
    # pauses, the type of lessons of a group (the pay of the teacher backwards), the kind of a cancellation (the deduction of the students)
    ("POST", f"{P}/org/memberships/{{membership_id}}/pauses"), ("PATCH", f"{P}/org/pauses/{{pause_id}}"), ("POST", f"{P}/org/pauses/{{pause_id}}/void"),
    ("PUT", f"{P}/org/groups/{{group_id}}/lesson-type"), ("PUT", f"{P}/org/groups/{{group_id}}/attendance/cancellation-kind"),
    # the return of a held lesson, the impact of its correction, the reopening of a closed month
    ("POST", f"{P}/org/groups/{{group_id}}/attendance/return"), ("POST", f"{P}/org/payroll-periods/{{month}}/reopen"),
    # payments (the ones of the earlier packs; the full list is the one below)
    ("POST", f"{P}/org/memberships/{{membership_id}}/payments"), ("POST", f"{P}/org/payments/{{payment_id}}/void"), ("POST", f"{P}/org/payments/{{payment_id}}/replace"),
    ("POST", f"{P}/org/memberships/{{membership_id}}/writeoffs"), ("POST", f"{P}/org/memberships/{{membership_id}}/refunds"),
    ("POST", f"{P}/org/writeoffs/{{writeoff_id}}/void"), ("POST", f"{P}/org/refunds/{{refund_id}}/void"),
    ("PUT", f"{P}/org/groups/{{group_id}}/price"), ("PUT", f"{P}/org/memberships/{{membership_id}}/price-override"), ("PUT", f"{P}/org/memberships/{{membership_id}}/opening-balance"),
    ("POST", f"{P}/org/opening-balances/{{opening_id}}/void"),
    # R3 #1: a day off turns the cancellations «по вине центра» into «праздник» (and back): the deductions of the students move
    ("POST", f"{P}/org/days-off"), ("POST", f"{P}/org/days-off/preview"), ("POST", f"{P}/org/days-off/{{day_off_id}}/apply"), ("POST", f"{P}/org/days-off/{{day_off_id}}/void"),
}

# the part of the route that moves money asks the flag (``money_rights_flag``): the route is used by teachers / administrators for the rest
CONDITIONAL = {
    ("POST", f"{P}/org/groups/{{group_id}}/attendance"),                    # a correction of a held lesson
    ("PUT", f"{P}/org/groups/{{group_id}}"),                                # the type of lessons of the group sets the pay rate of its teacher: a REAL change of ``lesson_type`` needs the right (the rest of the fields: any manager)
    ("POST", f"{P}/org/groups/{{group_id}}/attendance/impact"),             # R3 #24: the money right, or the teacher of THIS lesson without the sums
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
    ("POST", f"{P}/org/students/{{student_id}}/archive"),                   # «В архив» with the settlement of the balance
    ("POST", f"{P}/org/import/preview"), ("POST", f"{P}/org/import/commit"),  # the opening-balance columns are read only with the right
}

# the money right is checked INSIDE the service (the route itself is open to a manager: he sees the queue), like ``INLINE`` - and the test below reads the service source for the refusal;
# the SUMS of the pay of the teacher are the owner's (an administrator reads the number of students, not the amount)
SERVICE_CHECKED = {
    ("POST", f"{P}/org/one-off-lessons/{{lesson_id}}/money-decision"): ("«Оплатить / Не оплачивать» an additional lesson: ``OneOffPurpose.decide`` answers 403 MONEY_RIGHT_REQUIRED to an administrator without the right (#159)", OneOffPurpose.decide),
}

# NOT money: the reason is the proof that somebody thought about it (R3 #10).  A route here whose handler calls a money service fails the test below.
NOT_MONEY = {
    ("POST", f"{P}/org/attendance/excuses/{{excuse_id}}/files"): "a file of a certificate: nobody decides anything",
    ("DELETE", f"{P}/org/attendance/excuses/files/{{file_id}}"): "a file of a certificate",
    ("POST", f"{P}/org/diagnostics"): "diagnostics: tests of the students",
    ("POST", f"{P}/org/diagnostics/quick"): "diagnostics: tests of the students",
    ("POST", f"{P}/org/diagnostics/{{diagnostic_id}}/acknowledge-report"): "a read mark on a report",
    ("POST", f"{P}/org/groups"): "a new group has no lessons held; the type of lessons is the starting value",
    ("POST", f"{P}/org/groups/{{group_id}}/archive"): "refused while students are in the group; the cancelled lessons pay nothing",
    ("POST", f"{P}/org/groups/{{group_id}}/restore"): "the group comes back without lessons held",
    ("POST", f"{P}/org/groups/{{group_id}}/attendance/start"): "a lesson begins: the money appears when it is completed (the marks route is CONDITIONAL)",
    ("POST", f"{P}/org/groups/{{group_id}}/attendance/reconcile/cancel"): "withdraws a request to formalise; no pay arises before «Оформить»",
    ("POST", f"{P}/org/groups/{{group_id}}/lesson-move"): "moves a FUTURE lesson; a held one is protected by the closed-period rule",
    ("POST", f"{P}/org/invitations"): "an invitation to the centre", ("DELETE", f"{P}/org/invitations/{{invite_id}}"): "an invitation",
    ("POST", f"{P}/org/invitations/{{invite_id}}/resend"): "an invitation",
    ("DELETE", f"{P}/org/members/{{member_id}}"): "a staff member: refused while he has groups; the pay of held lessons stays in the sheets",
    ("POST", f"{P}/org/members/{{member_id}}/email-change"): "login data", ("POST", f"{P}/org/members/{{member_id}}/password-link"): "login data",
    ("POST", f"{P}/org/members/{{member_id}}/handover"): "future lessons go to another teacher; the pay arises when a lesson is held",
    ("POST", f"{P}/org/members/{{member_id}}/restore"): "the member comes back", ("PUT", f"{P}/org/members/{{member_id}}/role"): "the role of a member",
    ("POST", f"{P}/org/schedule/lessons"): "the schedule of FUTURE lessons", ("PUT", f"{P}/org/schedule/lessons/{{lesson_id}}"): "the schedule",
    ("DELETE", f"{P}/org/schedule/lessons/{{lesson_id}}"): "the schedule; held lessons are protected by the closed-period rule",
    ("PUT", f"{P}/org/schedule/lessons/{{lesson_id}}/recurrence"): "the schedule", ("POST", f"{P}/org/schedule/lessons/{{lesson_id}}/recurrence/end"): "the schedule",
    ("POST", f"{P}/org/schedule/lessons/{{lesson_id}}/reschedule"): "the schedule", ("POST", f"{P}/org/schedule/occurrences"): "the schedule",
    ("POST", f"{P}/org/students"): "a student card without money (the enrolment and its fee go through the roster routes, CONDITIONAL)",
    ("PUT", f"{P}/org/students/{{student_id}}"): "the card; the archive goes through its own route",
    ("POST", f"{P}/org/students/{{student_id}}/restore"): "the card comes back and the student is re-enrolled; the accruals begin from today by the normal rule, no money is entered",
    ("POST", f"{P}/org/substitutions"): "a request for a substitute for a FUTURE lesson", ("POST", f"{P}/org/substitutions/decide-bulk"): "the same, the decision",
    ("POST", f"{P}/org/substitutions/withdraw"): "the same", ("POST", f"{P}/org/substitutions/{{request_id}}/decide"): "the same",
    ("POST", f"{P}/org/substitutions/{{request_id}}/withdraw"): "the same",
    ("PUT", f"{P}/org"): "the name and the contacts of the centre", ("PUT", f"{P}/me"): "the profile of the person",
}

# the owner only (the money of the teachers: LESSONS 45) - stricter than the money right
OWNER_ONLY_PREFIXES = (f"{P}/org/payroll/", f"{P}/org/settings/payments")

# a handler that calls one of these moves money: the route must be in a list above (or be owner-only)
MONEY_CALLS = re.compile(
    r"PaymentService|PauseService|LessonTypeService|OpeningService|LeaveService|change_cancellation_kind|return_completed_lesson|reopen_month|correction_impact|"
    r"\.create_writeoff|\.create_refund|\.void_writeoff|\.void_refund|\.set_override|set_group_price|write_override|PayrollPayoutService|PrPayoutService|openings_service|ExcuseService\(db\)\.review|"
    r"\.review\(|\.reset\(|\.review_bulk\(|restore_lesson|\.save_group_attendance|\.reconcile_missed|LessonTeachers\(.*\)\.put|bulk_add_students|add_student_to_group|"
    r"DaysOffService|_service\(db\)\.(create|apply|void)|cancel_lesson|import_ops\.commit|StudentArchiveService\(db\)\.archive|OneOffPurpose|_purposes\(db\)\.decide")


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


def _is_money_class(key, deps):
    module = ROUTES[key][1].endpoint.__module__.rsplit(".", 1)[-1]
    return (key in WHOLE or key in CONDITIONAL or key in INLINE or key in SERVICE_CHECKED or key[1].startswith(OWNER_ONLY_PREFIXES) or module in PAYMENT_MODULES)


PAYMENT_MODULES = ("payments_router", "payments_pay2_router", "openings_router", "payments_income_router")


def test_every_non_get_route_of_the_centre_is_classified_and_the_registry_has_no_dead_or_double_entries():
    """R3 #10 (the ban by default): a new route of ``/org/*`` without a class - money or NOT money with a reason - fails here."""
    org = [k for k in ROUTES if k[0] != "GET" and k[1].startswith(f"{P}/org")]
    assert len(org) >= 85, len(org)
    unclassified = [k for k in sorted(org) if not _is_money_class(k, ROUTES[k][0]) and k not in NOT_MONEY]
    assert not unclassified, f"routes with no class in tests/test_money1_rights_guard.py (money or NOT_MONEY with a reason): {unclassified}"
    dead = [k for k in sorted(NOT_MONEY) + sorted(SERVICE_CHECKED) if k not in ROUTES]
    assert not dead, f"records of routes that do not exist (renamed?): {dead}"
    classes = [WHOLE, CONDITIONAL, INLINE, set(SERVICE_CHECKED), set(NOT_MONEY)]
    double = [k for k in sorted(set().union(*classes)) if sum(k in c for c in classes) > 1]
    assert not double, f"routes in two classes: {double}"
    assert all(reason.strip() for reason in NOT_MONEY.values()) and all(reason.strip() for reason, _ in SERVICE_CHECKED.values())


def test_a_route_whose_right_is_checked_in_the_service_has_the_refusal_in_the_service():
    """``SERVICE_CHECKED`` is not an exemption: the service the route calls must answer 403 ``MONEY_RIGHT_REQUIRED`` itself (LESSONS 94: the walk found the records it is about)."""
    assert SERVICE_CHECKED
    for key, (_reason, service) in SERVICE_CHECKED.items():
        assert "MONEY_RIGHT_REQUIRED" in inspect.getsource(service), key


def test_a_not_money_route_does_not_call_a_money_service_and_has_no_money_dependency():
    """The code is the second witness of the registry: a NOT_MONEY handler whose source names a service that moves money is a wrong record."""
    wrong = []
    for key in sorted(NOT_MONEY):
        deps, route = ROUTES[key]
        if dependencies.require_payments_access in deps or dependencies.money_rights_flag in deps or MONEY_CALLS.search(inspect.getsource(route.endpoint)):
            wrong.append(key)
    assert not wrong, f"NOT_MONEY records that touch money: {wrong}"


def test_a_route_with_the_money_dependency_is_a_money_record_and_the_services_are_only_behind_it():
    """The other direction + the whole application (not only ``/org/*``): the handler of ANY non-GET route that calls a money service has the right (or is a record)."""
    scanned, bad = 0, []
    for key, (deps, route) in sorted(ROUTES.items()):
        if key[0] == "GET":
            continue
        scanned += 1
        if dependencies.require_payments_access in deps and not (key in WHOLE or key[1].startswith(OWNER_ONLY_PREFIXES) or route.endpoint.__module__.rsplit(".", 1)[-1] in PAYMENT_MODULES
                                                                  or key in (CONDITIONAL | INLINE)):
            bad.append((key, "has the money dependency but is not in a money class"))
        module = route.endpoint.__module__.rsplit(".", 1)[-1]
        if module in PAYMENT_MODULES or key[1].startswith(OWNER_ONLY_PREFIXES) or dependencies.require_payments_access in deps or key in (WHOLE | CONDITIONAL | INLINE | set(SERVICE_CHECKED)):
            continue
        if MONEY_CALLS.search(inspect.getsource(route.endpoint)):
            bad.append((key, "calls a money service and is in no money class"))
    assert scanned >= 120, scanned
    assert not bad, f"{bad}"


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
