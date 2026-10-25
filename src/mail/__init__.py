"""Letters of the platform (pack «Консоль-1»): Jinja2 templates, multipart/alternative, SMTP."""
from .letters import (  # noqa: F401
    render_application_declined,
    render_application_received,
    render_new_application,
    render_password_reset,
    render_welcome,
    send_application_declined,
    send_application_received,
    send_new_application,
    send_password_reset,
    send_welcome,
)
from .render import LETTERS, Rendered  # noqa: F401
