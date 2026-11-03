"""M11 Phase 1: the email transport boundary. No transport here is wired to
application submission - these tests exercise `EmailTransport` in isolation."""

from __future__ import annotations

import aiosmtplib.errors as smtp_errors
import pytest

from packages.services.delivery.email import (
    EmailAttachment,
    EmailSendResult,
    EmailSendStatus,
    FakeEmailTransport,
    OutboundEmail,
    SMTPEmailTransport,
    build_email_transport,
)
from packages.shared.config import EmailTransportMode, Settings

pytestmark = [pytest.mark.unit, pytest.mark.safety]


def _message() -> OutboundEmail:
    return OutboundEmail(
        to="hiring@example.invalid",
        subject="Application",
        body="See attached.",
        attachments=(
            EmailAttachment(
                filename="resume.pdf", content=b"%PDF-fake", content_type="application/pdf"
            ),
        ),
    )


# --------------------------------------------------------------- fake transport
async def test_fake_transport_never_performs_network_io(monkeypatch: pytest.MonkeyPatch) -> None:
    """The only network-capable call path in this module is `aiosmtplib.
    send`. Poison it and prove the fake transport never routes through it -
    a precise check that does not also break pytest's own asyncio machinery
    the way patching raw `socket.socket` would."""

    async def _forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("FakeEmailTransport must never touch a real socket")

    monkeypatch.setattr("packages.services.delivery.email.aiosmtplib.send", _forbidden)

    transport = FakeEmailTransport()
    result = await transport.send(_message())
    assert result.status is EmailSendStatus.SENT


async def test_fake_transport_records_every_attempt() -> None:
    transport = FakeEmailTransport()
    message = _message()
    await transport.send(message)
    await transport.send(message)
    assert transport.sent == [message, message]


async def test_fake_transport_default_result_is_sent() -> None:
    result = await FakeEmailTransport().send(_message())
    assert result.sent is True
    assert result.message_id


async def test_fake_transport_scripted_results_are_deterministic() -> None:
    """Controlled simulation of success and failure classes, consumed in
    order - exactly what a future submission-service test needs."""
    scripted = [
        EmailSendResult(EmailSendStatus.TRANSIENT_FAILURE, detail="503"),
        EmailSendResult(EmailSendStatus.REJECTED, smtp_code=550, detail="no such user"),
        EmailSendResult(EmailSendStatus.AUTH_FAILED, detail="bad credentials"),
        EmailSendResult(EmailSendStatus.AMBIGUOUS, detail="timed out"),
    ]
    transport = FakeEmailTransport(responses=list(scripted))
    results = [await transport.send(_message()) for _ in range(len(scripted))]
    assert results == scripted
    # Exhausted: falls back to the default (SENT), not an error.
    assert (await transport.send(_message())).status is EmailSendStatus.SENT


async def test_fake_transport_default_result_is_overridable() -> None:
    transport = FakeEmailTransport(
        default_result=EmailSendResult(EmailSendStatus.REJECTED, smtp_code=550)
    )
    result = await transport.send(_message())
    assert result.status is EmailSendStatus.REJECTED
    assert result.smtp_code == 550


# -------------------------------------------------------------- SMTP transport
def test_smtp_transport_requires_a_host() -> None:
    settings = Settings(smtp_host="")
    with pytest.raises(ValueError, match="SMTP_HOST"):
        SMTPEmailTransport(settings, sender="agent@jobforge.invalid")


def test_smtp_transport_does_not_require_credentials() -> None:
    """MailHog needs none; requiring them unconditionally would break local
    dev. Absence must not raise at construction time."""
    settings = Settings(smtp_host="localhost", smtp_username="", smtp_password="")
    transport = SMTPEmailTransport(settings, sender="agent@jobforge.invalid")
    assert transport is not None


async def test_smtp_transport_classifies_authentication_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _raise(*args: object, **kwargs: object) -> None:
        raise smtp_errors.SMTPAuthenticationError(535, "bad credentials")

    monkeypatch.setattr("packages.services.delivery.email.aiosmtplib.send", _raise)
    transport = SMTPEmailTransport(Settings(smtp_host="localhost"), sender="a@b.invalid")
    result = await transport.send(_message())
    assert result.status is EmailSendStatus.AUTH_FAILED
    assert result.message_id  # generated before the send attempt


async def test_smtp_transport_classifies_permanent_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _raise(*args: object, **kwargs: object) -> None:
        raise smtp_errors.SMTPResponseException(550, "no such user")

    monkeypatch.setattr("packages.services.delivery.email.aiosmtplib.send", _raise)
    transport = SMTPEmailTransport(Settings(smtp_host="localhost"), sender="a@b.invalid")
    result = await transport.send(_message())
    assert result.status is EmailSendStatus.REJECTED
    assert result.smtp_code == 550


async def test_smtp_transport_classifies_transient_4xx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _raise(*args: object, **kwargs: object) -> None:
        raise smtp_errors.SMTPResponseException(421, "service not available")

    monkeypatch.setattr("packages.services.delivery.email.aiosmtplib.send", _raise)
    transport = SMTPEmailTransport(Settings(smtp_host="localhost"), sender="a@b.invalid")
    result = await transport.send(_message())
    assert result.status is EmailSendStatus.TRANSIENT_FAILURE
    assert result.smtp_code == 421


async def test_smtp_transport_classifies_recipients_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _raise(*args: object, **kwargs: object) -> None:
        refusal = smtp_errors.SMTPRecipientRefused("hiring@example.invalid", 550, "unknown")
        raise smtp_errors.SMTPRecipientsRefused([refusal])

    monkeypatch.setattr("packages.services.delivery.email.aiosmtplib.send", _raise)
    transport = SMTPEmailTransport(Settings(smtp_host="localhost"), sender="a@b.invalid")
    result = await transport.send(_message())
    assert result.status is EmailSendStatus.REJECTED


async def test_smtp_transport_classifies_connection_failure_as_transient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _raise(*args: object, **kwargs: object) -> None:
        raise smtp_errors.SMTPConnectError("connection refused")

    monkeypatch.setattr("packages.services.delivery.email.aiosmtplib.send", _raise)
    transport = SMTPEmailTransport(Settings(smtp_host="localhost"), sender="a@b.invalid")
    result = await transport.send(_message())
    assert result.status is EmailSendStatus.TRANSIENT_FAILURE


async def test_smtp_transport_classifies_timeout_as_ambiguous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _raise(*args: object, **kwargs: object) -> None:
        raise smtp_errors.SMTPConnectTimeoutError("timed out connecting")

    monkeypatch.setattr("packages.services.delivery.email.aiosmtplib.send", _raise)
    transport = SMTPEmailTransport(Settings(smtp_host="localhost"), sender="a@b.invalid")
    result = await transport.send(_message())
    assert result.status is EmailSendStatus.AMBIGUOUS


async def test_smtp_transport_classifies_read_timeout_after_data_as_ambiguous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A read timeout while waiting for DATA's final response is a
    `SMTPTimeoutError` before it is anything else - the message may already
    have been accepted, so it must never be reported as a plain retryable
    transient failure."""

    async def _raise(*args: object, **kwargs: object) -> None:
        raise smtp_errors.SMTPReadTimeoutError("no response")

    monkeypatch.setattr("packages.services.delivery.email.aiosmtplib.send", _raise)
    transport = SMTPEmailTransport(Settings(smtp_host="localhost"), sender="a@b.invalid")
    result = await transport.send(_message())
    assert result.status is EmailSendStatus.AMBIGUOUS


async def test_smtp_transport_classifies_server_disconnected_as_ambiguous_not_transient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`SMTPServerDisconnected` is not confined to the pre-send phase the
    way `SMTPConnectError` is: aiosmtplib's own `execute_data_command` ->
    `read_response` raises this identical exception whether the connection
    dropped before DATA started or after the full message body was already
    written and only the final response was lost. Since nothing in the
    exception distinguishes which happened, a retry could resubmit a
    message the remote server already accepted - so this must be AMBIGUOUS,
    never TRANSIENT_FAILURE, even though some of its real causes (e.g. a
    STARTTLS failure) are in fact always pre-send."""

    async def _raise(*args: object, **kwargs: object) -> None:
        raise smtp_errors.SMTPServerDisconnected("Connection lost")

    monkeypatch.setattr("packages.services.delivery.email.aiosmtplib.send", _raise)
    transport = SMTPEmailTransport(Settings(smtp_host="localhost"), sender="a@b.invalid")
    result = await transport.send(_message())
    assert result.status is EmailSendStatus.AMBIGUOUS


async def test_smtp_transport_still_classifies_pure_connect_error_as_transient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unlike `SMTPServerDisconnected`, `SMTPConnectError` is raised only
    from aiosmtplib's `_create_connection`, strictly before any SMTP command
    is ever sent - provably safe to auto-retry. This is the control case
    proving the fix above did not overcorrect into treating every
    connection-shaped failure as ambiguous."""

    async def _raise(*args: object, **kwargs: object) -> None:
        raise smtp_errors.SMTPConnectError("connection refused")

    monkeypatch.setattr("packages.services.delivery.email.aiosmtplib.send", _raise)
    transport = SMTPEmailTransport(Settings(smtp_host="localhost"), sender="a@b.invalid")
    result = await transport.send(_message())
    assert result.status is EmailSendStatus.TRANSIENT_FAILURE


async def test_smtp_transport_classifies_unexpected_os_error_as_ambiguous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _raise(*args: object, **kwargs: object) -> None:
        raise ConnectionResetError("connection reset by peer")

    monkeypatch.setattr("packages.services.delivery.email.aiosmtplib.send", _raise)
    transport = SMTPEmailTransport(Settings(smtp_host="localhost"), sender="a@b.invalid")
    result = await transport.send(_message())
    assert result.status is EmailSendStatus.AMBIGUOUS


async def test_smtp_transport_success(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_send(*args: object, **kwargs: object) -> tuple[dict, str]:
        return {}, "250 OK"

    monkeypatch.setattr("packages.services.delivery.email.aiosmtplib.send", _fake_send)
    transport = SMTPEmailTransport(Settings(smtp_host="localhost"), sender="a@b.invalid")
    result = await transport.send(_message())
    assert result.status is EmailSendStatus.SENT
    assert result.sent is True
    assert result.message_id


# --------------------------------------------------------------------- factory
def test_build_email_transport_defaults_to_fake() -> None:
    transport = build_email_transport(Settings())
    assert isinstance(transport, FakeEmailTransport)


def test_build_email_transport_selects_smtp_when_configured() -> None:
    settings = Settings(email_transport_mode=EmailTransportMode.SMTP, smtp_host="localhost")
    transport = build_email_transport(settings, sender="a@b.invalid")
    assert isinstance(transport, SMTPEmailTransport)


def test_smtp_credentials_are_not_exposed_by_default_repr() -> None:
    """The transport necessarily *holds* the credential to authenticate with
    it - that is not exposure. Exposure is a repr, a log line, or a config
    dump printing it, and the plain default object repr carries no
    attributes at all."""
    settings = Settings(
        smtp_host="localhost",
        smtp_password="s3cr3t-smtp-pw",  # pragma: allowlist secret
    )
    transport = SMTPEmailTransport(settings, sender="a@b.invalid")
    assert "s3cr3t-smtp-pw" not in repr(transport)


async def test_smtp_failure_detail_never_echoes_the_configured_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _raise(*args: object, **kwargs: object) -> None:
        raise smtp_errors.SMTPAuthenticationError(535, "bad credentials")

    monkeypatch.setattr("packages.services.delivery.email.aiosmtplib.send", _raise)
    settings = Settings(
        smtp_host="localhost",
        smtp_password="s3cr3t-smtp-pw",  # pragma: allowlist secret
    )
    transport = SMTPEmailTransport(settings, sender="a@b.invalid")
    result = await transport.send(_message())
    assert "s3cr3t-smtp-pw" not in result.detail
