"""D3 (e2e K3): the access log of uvicorn never carries the personal data of a query string (the console searches requests by e-mail:
``GET /api/v1/platform/demo-requests?q=lead%40mail.com``).  The value of ``q`` / ``email`` / ``phone`` / ``token`` and of ANY parameter that looks like an
e-mail address or a phone number becomes ``[redacted]``; the path and the other parameters stay."""
import logging

import pytest
from uvicorn.logging import AccessFormatter

from tests.console_helpers import needs_pg  # noqa: F401  (first: the import order of the application; it also installs the logging of the service)
from src.core.middleware import DiagnosticCapabilityLogFilter

PATH = "/api/v1/platform/demo-requests"


def access(caplog, target):
    """What uvicorn writes for one request, through the logger the service configured."""
    with caplog.at_level(logging.INFO, logger="uvicorn.access"):
        logging.getLogger("uvicorn.access").info('%s - "%s %s HTTP/%s" %d', "10.0.0.5:4242", "GET", target, "1.1", 200)
    return caplog.text


def test_d3_the_example_of_the_task(caplog):
    text = access(caplog, f"{PATH}?q=a%2Bb%40x.com&status=new")
    assert f"{PATH}?q=[redacted]&status=new" in text and "x.com" not in text and "a%2Bb" not in text and "200" in text


def test_d3_the_filter_is_installed_on_uvicorn_access_and_on_the_handler_of_the_root_logger():
    from src.core.middleware import configure_logging

    root = logging.getLogger()
    saved = root.handlers[:]                                                                   # configure_logging replaces the handlers of the root logger: put them back
    try:
        configure_logging(is_development=False)
        assert any(isinstance(f, DiagnosticCapabilityLogFilter) for f in logging.getLogger("uvicorn.access").filters)
        assert any(isinstance(f, DiagnosticCapabilityLogFilter) for h in root.handlers for f in h.filters)
    finally:
        root.handlers[:] = saved


@pytest.mark.parametrize(
    "query, kept",
    [
        ("q=lead%40mail.com&status=new", "q=[redacted]&status=new"),
        ("status=new&email=lead%40mail.com&page=2", "status=new&email=[redacted]&page=2"),
        ("PHONE=%2B998901234567&sort=created_at", "PHONE=[redacted]&sort=created_at"),
        ("token=abc123&repeat=true", "token=[redacted]&repeat=true"),
        ("q=%D0%98%D0%B2%D0%B0%D0%BD", "q=[redacted]"),                                          # a name searched for: the key decides, whatever the value
        ("q=", "q=[redacted]"),
        ("note=write%20to%20lead%40mail.com%20today&status=new", "note=[redacted]&status=new"),      # an e-mail in any parameter
        ("owner_email=a%40b.uz", "owner_email=[redacted]"),
        ("contact=%2B998%2090%20123-45-67", "contact=[redacted]"),                                    # a phone in any parameter
        ("contact=90+123+45+67", "contact=[redacted]"),
        ("contact=998901234567&x=1", "contact=[redacted]&x=1"),
    ],
)
def test_d3_personal_values_are_redacted_and_the_rest_stays(caplog, query, kept):
    text = access(caplog, f"{PATH}?{query}")
    assert f"{PATH}?{kept}" in text
    for secret in ("mail.com", "998901234567", "90+123", "%D0%98", "abc123", "a%40b.uz", "123-45-67"):
        assert secret not in text


@pytest.mark.parametrize("query", ["status=new,contacted&repeat=false", "date_from=2026-10-01&date_to=2026-10-29", "page=2&page_size=50&sort=created_at&order=desc",
                                   "source=telegram", "city=%D0%A2%D0%B0%D1%88%D0%BA%D0%B5%D0%BD%D1%82", "date_from=2026-10-01T10:00:00"])
def test_d3_ordinary_parameters_are_not_touched(caplog, query):
    assert f"{PATH}?{query}" in access(caplog, f"{PATH}?{query}")


def test_d3_the_path_is_not_touched_and_the_old_redactions_stay(caplog):
    text = access(caplog, "/api/v1/diagnostic/sessions/by-invite?invite_code=SECRET-CAPABILITY&q=a%40b.com")
    assert "SECRET-CAPABILITY" not in text and "invite_code=[redacted]" in text and "q=[redacted]" in text and "/api/v1/diagnostic/sessions/by-invite" in text


def test_d3_the_uvicorn_formatter_still_formats_the_filtered_arguments():
    record = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d', ("127.0.0.1:1", "GET", f"{PATH}?q=a%40b.com&status=new", "1.1", 200), None)
    DiagnosticCapabilityLogFilter().filter(record)
    formatted = AccessFormatter('%(client_addr)s - "%(request_line)s" %(status_code)s', use_colors=False).format(record)
    assert "a%40b.com" not in formatted and "status=new" in formatted and "200" in formatted and len(record.args) == 5


def test_d3_an_address_given_as_the_message_is_redacted_and_a_development_mail_body_is_not_touched():
    record = logging.LogRecord("app", logging.INFO, "", 0, "GET /x?email=a@b.com&y=1 done", None, None)
    DiagnosticCapabilityLogFilter().filter(record)
    assert record.getMessage() == "GET /x?email=[redacted]&y=1 done"
    body = "SMTP not configured\n  Body:\n<p>activate?token=SECRETTOKEN&email=a@b.com</p>"     # the development log of a letter is the developer's own convenience
    record = logging.LogRecord("src.core.email", logging.INFO, "", 0, "%s", (body,), None)
    DiagnosticCapabilityLogFilter().filter(record)
    assert record.getMessage() == body
