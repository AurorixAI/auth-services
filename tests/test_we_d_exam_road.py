"""Wave E, E-D (SV-14): ONE road from auth to exam-service.  The three calls ``AuthService`` makes from the cancellation of a lesson and the stop / change of a weekly
lesson went through three copies of the transport in ``service.py`` (own token text, own 409 handling - the impact call had none - own status text); they now use
``exam_bridge._post`` and ONE mapping of its failures (``exam_bridge.refusal``).  Without a database: the transport is exercised with a fake ``httpx.post``."""
import httpx
import pytest

from src.auth import exam_bridge, exam_release
from src.auth.service import AuthService

REAL_POST = exam_bridge._post      # tests/conftest.py replaces ``_post`` and the two schedule calls of AuthService for every test; these are the real ones
REAL = {name: getattr(AuthService, name) for name in ("_sync_exam_lesson_cancellation", "_fetch_exam_recurring_end_impact", "_release_exam_recurring_end")}

CALLS = {
    "cancellation": (lambda svc: svc._sync_exam_lesson_cancellation(phase="cancellation-check", org_id="o", group_id="g", lesson_id="l", lesson_date="2026-11-04", reason=None),
                     exam_bridge.CANCELLATION),
    "impact": (lambda svc: svc._fetch_exam_recurring_end_impact(org_id="o", group_id="g", lesson_id="l", effective_from="2026-11-04"), exam_bridge.RECURRING_IMPACT),
    "release": (lambda svc: svc._release_exam_recurring_end(org_id="o", group_id="g", group_name="G", lesson_id="l", effective_from="2026-11-04", actor_id="a", reason="r"),
                exam_bridge.RECURRING_RELEASE),
}


class Answer:
    def __init__(self, status, body=None, text=""):
        self.status_code, self._body, self.text = status, body, text

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


@pytest.fixture
def road(monkeypatch):
    sent = []
    state = {"answer": Answer(200, {"exams": [], "released_exam_ids": []})}
    monkeypatch.setattr(exam_bridge, "_post", REAL_POST)
    for name, real in REAL.items():
        monkeypatch.setattr(AuthService, name, real)
    monkeypatch.setattr(exam_bridge, "get_settings", lambda: type("S", (), {"internal_service_token": "t", "exam_service_url": "http://exam/"})())

    def post(url, json=None, headers=None, timeout=None):
        sent.append((url, json, headers, timeout))
        if isinstance(state["answer"], Exception):
            raise state["answer"]
        return state["answer"]

    monkeypatch.setattr(exam_bridge.httpx, "post", post)
    svc = AuthService.__new__(AuthService)
    return type("Road", (), {"svc": svc, "sent": sent, "state": state})


def outcome(road, which):
    call, _ = CALLS[which]
    try:
        return call(road.svc)
    except Exception as exc:   # noqa: BLE001 - the kind of the exception is what is compared
        return exc


@pytest.mark.parametrize("which", CALLS)
def test_a_good_answer_goes_through_the_one_transport_with_the_token_and_the_timeout(road, which):
    assert outcome(road, which) in (None, {"exams": [], "released_exam_ids": []}, [])
    url, body, headers, timeout = road.sent[0]
    assert url.startswith("http://exam/internal/lesson-bindings/") and headers == {"X-Internal-Service-Token": "t"} and timeout == exam_bridge.TIMEOUT
    assert body["org_id"] == "o" and body["group_id"] == "g" and body["lesson_id"] == "l"


@pytest.mark.parametrize("which", CALLS)
def test_conflict_is_a_value_error_with_its_reason_or_the_text_of_the_call(road, which):
    texts = CALLS[which][1]
    road.state["answer"] = Answer(409, {"detail": "Экзамен «Контрольная» уже идёт"})
    got = outcome(road, which)
    assert type(got) is ValueError and str(got) == "Экзамен «Контрольная» уже идёт"          # the impact call used to have no 409 branch at all
    road.state["answer"] = Answer(409, {"detail": {"code": "X", "message": "Есть попытки"}})
    assert str(outcome(road, which)) == "Есть попытки"                                          # a coded detail was printed as a dict
    road.state["answer"] = Answer(409, ValueError("no json"))
    assert str(outcome(road, which)) == texts.refused
    road.state["answer"] = Answer(409, {"detail": None})
    assert str(outcome(road, which)) == texts.refused


@pytest.mark.parametrize("which", CALLS)
def test_no_answer_and_no_token_are_one_runtime_error_with_the_text_of_the_call(road, which, monkeypatch):
    texts = CALLS[which][1]
    road.state["answer"] = httpx.ConnectError("down")
    got = outcome(road, which)
    assert type(got) is RuntimeError and str(got) == texts.down
    monkeypatch.setattr(exam_bridge, "get_settings", lambda: type("S", (), {"internal_service_token": "", "exam_service_url": "http://exam"})())
    road.sent.clear()
    got = outcome(road, which)
    assert type(got) is RuntimeError and str(got) == texts.down and road.sent == []         # no token: nothing is sent; the owner reads the same text as for «down»


@pytest.mark.parametrize("which", CALLS)
@pytest.mark.parametrize("status", [400, 404, 422, 500, 503])
def test_another_status_is_an_exam_answer_error_with_the_status(road, which, status):
    road.state["answer"] = Answer(status, None, "boom")
    got = outcome(road, which)
    assert isinstance(got, exam_release.ExamAnswerError) and isinstance(got, RuntimeError)
    assert got.status == status and str(got) == CALLS[which][1].failed


@pytest.mark.parametrize("which", CALLS)
def test_an_answer_that_is_not_json_is_a_runtime_error(road, which):
    road.state["answer"] = Answer(200, ValueError("not json"))
    got = outcome(road, which)
    assert type(got) is RuntimeError and str(got) == exam_bridge.BAD_ANSWER


def test_the_release_must_confirm_the_exams_it_returned(road):
    road.state["answer"] = Answer(200, {"ok": True})
    got = outcome(road, "release")
    assert type(got) is RuntimeError and str(got) == "Сервис экзаменов не подтвердил возврат экзаменов"
    road.state["answer"] = Answer(200, {"released_exam_ids": [1, "b"]})
    assert outcome(road, "release") == ["1", "b"]
    road.state["answer"] = Answer(200, [1])
    assert str(outcome(road, "impact")) == exam_bridge.BAD_ANSWER


def test_the_bridge_callers_still_see_the_two_classes_they_catch():
    assert issubclass(exam_bridge.ExamBridgeUnavailable, exam_bridge.ExamBridgeError) and issubclass(exam_bridge.ExamBridgeConflict, exam_bridge.ExamBridgeError)
    assert issubclass(exam_bridge.ExamBridgeBadAnswer, exam_bridge.ExamBridgeUnavailable)      # the fail-open callers (completion-check) treat it as «down»
    assert exam_release.ExamAnswerError is exam_bridge.ExamAnswerError


def test_service_py_has_no_transport_of_its_own():
    import pathlib

    text = (pathlib.Path(__file__).resolve().parents[1] / "src" / "auth" / "service.py").read_bytes().decode("utf-8")
    assert "httpx" not in text and "/internal/lesson-bindings" not in text and "exam_service_url" not in text
