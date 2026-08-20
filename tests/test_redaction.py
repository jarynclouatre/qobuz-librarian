"""Credential redaction tests."""
from qobuz_librarian import redaction


def test_redact_masks_a_header_tuple_repr():
    # A dumped list of request headers uses a comma, not = or :, between the
    # name and the value: [('X-User-Auth-Token', 'abc123'), ...]. This shape
    # reached the job log unmasked before the comma separator was added.
    text = "headers=[('X-User-Auth-Token', 'deadbeefcafefeed1234'), ('Accept', 'json')]"
    cleaned = redaction.redact(text)
    assert "deadbeefcafefeed1234" not in cleaned
    assert "[redacted]" in cleaned
