"""#124 golden fixtures: the answers of the lesson-sheet endpoints on the data set D2 (ids replaced by names).

    LESSONMOVE_WRITE_FIXTURES=1 pytest tests/test_lesson_move_fixtures_pg.py     # writes tests/fixtures/lessonmove/*.json

Without the variable the live answers are compared with the files."""
import json
import os
import re
from pathlib import Path

from tests.b2_helpers import API, client, needs_pg  # noqa: F401
from tests.test_lesson_move_direct_pg import binding, check, move
from tests.test_ops_b2_decide_pg import one, sync, w  # noqa: F401
from tests.b2_helpers import clock, exams, pg_session  # noqa: F401

pytestmark = needs_pg
FIX = Path(__file__).resolve().parent / "fixtures" / "lessonmove"
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def test_the_endpoints_reproduce_the_golden_fixtures(w, sync, exams):
    names = {str(w.ids(n)): n for n in ("org", "G1", "G2", "G3", "G4", "owner", "admin", "A", "B", "C", "D", "s1", "s2", "s3", "s7")}
    seen = {}

    def norm(node):
        if isinstance(node, dict):
            return {k: norm(v) for k, v in node.items()}
        if isinstance(node, list):
            return [norm(v) for v in node]
        if isinstance(node, str):
            if node in names:
                return names[node]
            if node.startswith("one-off-") and len(node) > 8:
                return "one-off-NEW"
            if UUID_RE.fullmatch(node):
                return seen.setdefault(node, f"<id:{len(seen) + 1}>")
        return node

    out = {}
    out["K01_move_check_busy"] = (check(w, newDate="2026-10-27", startTime="10:00", endTime="11:30"), 200, "MoveCheckOut")
    out["K02_move_check_ok"] = (check(w), 200, "MoveCheckOut")
    out["K03_move_stale_sheet"] = (move(w, expectedStartTime="11:00", expectedEndTime="12:00"), 409, "-")
    out["K04_move_teacher_busy"] = (move(w, newDate="2026-10-27", startTime="10:00", endTime="11:30"), 409, "-")
    out["K05_exam_binding_none"] = (binding(w), 200, "ExamBinding")
    exams.completion_exams = [{"examId": "e1", "title": "Контрольная №3", "status": "lobby"}]
    out["K06_exam_binding_lobby"] = (binding(w), 200, "ExamBinding")
    exams.completion_exams = [{"examId": "e1", "title": "Контрольная №3", "status": "active"}]
    out["K07_exam_binding_active"] = (binding(w), 200, "ExamBinding")
    exams.unavailable = True
    out["K08_exam_binding_unavailable"] = (binding(w), 200, "ExamBinding")
    exams.unavailable = False
    out["K09_move_ok"] = (move(w), 200, "DecideOut")
    out["K10_move_again_other_slot"] = (move(w, newDate="2026-10-30"), 409, "-")
    write = os.environ.get("LESSONMOVE_WRITE_FIXTURES") == "1"
    index = {}
    for stem, (r, status, model) in out.items():
        assert r.status_code == status, (stem, r.text)
        got = norm(r.json())
        got = {"status": status, "body": got} if model == "-" else got
        index[stem + ".json"] = model
        target = FIX / f"{stem}.json"
        if write:
            target.write_text(json.dumps(got, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        else:
            assert json.loads(target.read_text(encoding="utf-8")) == got, stem
    if write:
        (FIX / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    else:
        assert json.loads((FIX / "index.json").read_text(encoding="utf-8")) == index
