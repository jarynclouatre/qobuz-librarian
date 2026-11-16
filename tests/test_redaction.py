"""Credential redaction tests."""
from qobuz_librarian import redaction


def test_redact_masks_a_header_tuple_repr():
    # A dumped list of request headers uses a comma, not = or :, between the
    # name and the value: [('X-User-Auth-Token', 'abc123'), ...].
    text = "headers=[('X-User-Auth-Token', 'deadbeefcafefeed1234'), ('Accept', 'json')]"
    cleaned = redaction.redact(text)
    assert "deadbeefcafefeed1234" not in cleaned
    assert "[redacted]" in cleaned


def test_redact_leaves_a_comma_after_a_setting_name_alone():
    text = "Set QOBUZ_USER_AUTH_TOKEN, or open the Settings page"
    assert redaction.redact(text) == text
