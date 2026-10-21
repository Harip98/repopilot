import logging

from repopilot.cli import configure_logging


def test_http_client_urls_are_not_logged_at_info():
    # A private repo's tarball redirect carries a short-lived token in its query string.
    configure_logging()
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
    assert logging.getLogger("httpcore").getEffectiveLevel() >= logging.WARNING
    assert logging.getLogger("repopilot").getEffectiveLevel() <= logging.INFO
