"""Keep capability redaction compatible with real access-log formatters."""
import logging
import httpx
import pytest
from uvicorn.logging import AccessFormatter
from src.core.middleware import DiagnosticCapabilityLogFilter


@pytest.mark.parametrize("url", [
    "/api/v1/auth/diagnostic/SECRET-CAPABILITY",
    "/api/v1/internal/diagnostic/SECRET-CAPABILITY/complete",
    "/api/v1/diagnostic/sessions/by-invite?invite_code=SECRET-CAPABILITY",
    "/api/v1/diagnostic/sessions/session-id/report?invite_code=SECRET-CAPABILITY",
])
def test_uvicorn_access_formatter_still_formats_filtered_arguments(url):
    record = logging.LogRecord("uvicorn.access", logging.INFO, "", 0,
        '%s - "%s %s HTTP/%s" %d', ("127.0.0.1:1234", "GET", url, "1.1", 200), None)
    DiagnosticCapabilityLogFilter().filter(record)
    formatted = AccessFormatter('%(client_addr)s - "%(request_line)s" %(status_code)s', use_colors=False).format(record)
    assert "SECRET-CAPABILITY" not in formatted
    assert "[redacted]" in formatted and "200" in formatted and "GET" in formatted
    assert len(record.args) == 5


def test_httpx_url_object_and_status_keep_printf_types():
    record = logging.LogRecord("httpx", logging.INFO, "", 0, "HTTP Request: %s %s %d",
        ("GET", httpx.URL("http://auth/api/v1/auth/diagnostic/SECRET-CAPABILITY"), 200), None)
    DiagnosticCapabilityLogFilter().filter(record)
    assert "SECRET-CAPABILITY" not in record.getMessage()
    assert record.args[-1] == 200 and "[redacted]" in record.getMessage()


def test_mapping_format_arguments_remain_valid():
    record = logging.LogRecord("httpx", logging.INFO, "", 0, "%(url)s %(status)d",
        ({"url": "/api/v1/auth/diagnostic/SECRET-CAPABILITY", "status": 503},), None)
    DiagnosticCapabilityLogFilter().filter(record)
    assert "SECRET-CAPABILITY" not in record.getMessage()
    assert "[redacted]" in record.getMessage() and "503" in record.getMessage()
