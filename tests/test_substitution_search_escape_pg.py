"""MN-09 (wave E): `%` and `_` in the search of the journal «Замены» are ordinary characters, not masks (every other search of the service escapes them)."""
import pytest

from tests.test_ops_b2_decide_pg import sync, w  # noqa: F401  (fixtures)
from tests.b2_helpers import client, clock, needs_pg, pg_session  # noqa: F401  (fixtures)
from tests.test_p12_direct_substitution_pg import journal, put

pytestmark = needs_pg


@pytest.mark.parametrize("mask", ["%", "_", "%%", "I_", "a%", "\\"])
def test_a_wildcard_character_in_the_search_matches_nothing(w, sync, mask):
    assert put(w, "admin", "D").status_code == 200
    assert len(journal(w)) == 1
    assert journal(w, search=mask) == []


def test_the_search_still_finds_by_a_part_of_the_name_and_ignores_case(w, sync):
    assert put(w, "admin", "D").status_code == 200
    part = w.D.last_name[1:4].upper()
    found = journal(w, search=f"  {part}  ")
    assert len(found) == 1 and found[0]["decision"]["substitute"]["id"] == str(w.D.id)
