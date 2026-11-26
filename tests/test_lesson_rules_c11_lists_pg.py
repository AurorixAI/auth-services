"""LESSON-RULES С11: the rows of the lists of «Замены» (the types, the window of the entries, the ages of the waiting rows, the entries of the history).

World: ``lesson_rules_helpers.world`` (group «Г» of the teacher A «Иванов И.», Mon / Wed 10:00-11:30, nine students).  Today Thu 26.11.2026 12:00 unless a test sets another time.  The expectation of each
test is in its comment BEFORE the call."""
import pytest

from src.auth import substitution_schemas as S

from tests.lesson_rules_helpers import MON, PAST_MON, WED, accounts, kind_in_db, marks_of, reconcile, save, set_kind, teacher_cancels, world, clock, pg_session, needs_pg, client, API  # noqa: F401
from tests.ops_helpers import grant_money_right

pytestmark = needs_pg
SUB = f"{API}/org/substitutions"


def listing(w, who="owner", **params):
    r = client(w, who).get(SUB, params=params)
    assert r.status_code == 200, r.text
    return r.json()


def _held_late(w, g, people, day="2026-11-25"):
    reconcile(w, g, day)
    save(w, g, people, day, marks=marks_of(people, first_present=2))


def contained(raw, dumped, path="$"):
    """Every key and value of ``raw`` is in ``dumped`` (the parsed model may add defaults, never drop or change a field)."""
    if isinstance(raw, dict):
        assert isinstance(dumped, dict), path
        for k, v in raw.items():
            assert k in dumped, f"{path}.{k} is lost by the model"
            contained(v, dumped[k], f"{path}.{k}")
    elif isinstance(raw, list):
        assert isinstance(dumped, list) and len(raw) == len(dumped), path
        for i, (a, b) in enumerate(zip(raw, dumped)):
            contained(a, b, f"{path}[{i}]")
    else:
        assert raw == dumped, f"{path}: {raw!r} != {dumped!r}"


# ============================================================================================ п.3 the types of the rows
def test_the_rows_of_the_list_are_three_models_told_apart_by_source_and_no_field_is_lost(pg_session, clock):
    """Hand: a pending request (teacher A asks Mon 30.11), a waiting cancellation (Mon 23.11), a lesson completed late (Wed 25.11): the pending list has TWO rows (request + waiting), the history ONE
    (entry of the journal).  Each row says its ``source`` (request / unconfirmed / audit) and is parsed by its OWN model: nothing of the real answer (``source``, ``canDecide``, ``waitingDays``, the words
    of the teacher ``reason``, ``lateDays``...) disappears when the answer is read through the model; the schema of the answer names the three models behind the discriminator ``source``."""
    w, g, people, members = world(pg_session, clock)
    teacher_cancels(w, g, reason="Отключили свет")
    clock.set("2026-11-24 12:00")
    assert client(w, "A").post(SUB, json={"lessons": [{"groupId": str(g.id), "lessonId": MON, "lessonDate": "2026-11-30"}], "reasonCode": "sick"}).status_code == 201
    clock.set("2026-11-26 12:00")
    _held_late(w, g, people)
    pending, decided = listing(w, status="pending"), listing(w, status="decided")
    assert sorted(i.get("source") for i in pending["items"]) == ["request", "unconfirmed"] and [i.get("source") for i in decided["items"]] == ["audit"]
    names = []
    for raw in (pending, decided):
        parsed = S.SubstitutionListOut.model_validate(raw)
        contained(raw, parsed.model_dump(mode="json"))
        names += [type(i).__name__ for i in parsed.items]
    assert sorted(names) == ["AuditEntryRow", "SubstitutionRequestRow", "WaitingCancellationRow"]
    waiting = next(i for i in pending["items"] if i["source"] == "unconfirmed")
    assert (waiting["canDecide"], waiting["waitingDays"], waiting["reason"], waiting["ageDays"]) == (True, 0, "Отключили свет", 3)
    spec = S.SubstitutionListOut.model_json_schema()["properties"]["items"]["items"]
    assert spec["discriminator"]["propertyName"] == "source" and sorted(spec["discriminator"]["mapping"]) == ["audit", "request", "unconfirmed"], spec
