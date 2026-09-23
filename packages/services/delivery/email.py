"""Email transport boundary (M11 Phase 1, locked design).

`EmailTransport` is the interface any future submission channel that sends
email will call through - `send()` and nothing else. Two implementations
exist:

  FakeEmailTransport    Default. Never opens a socket. Records every
                        attempt and returns a scripted or default result.
  SMTPEmailTransport     Connects with `aiosmtplib`, classifies whatever
                        happens into one of `EmailSendStatus`'s outcomes.

Selection is `EmailTransportMode` (`packages.shared.config`), independent of
`SUBMISSION_MODE`: this module answers "which client", never "is a real
send authorised right now" - that question belongs entirely to
`packages.core.services.submission_gate`, and this module has no opinion on
it, imports nothing from it, and never checks it.

Nothing here is wired to application submission yet - M11 Phase 1 explicitly
builds only this boundary and its two implementations, not a caller. No
retry scheduling exists here either; `EmailSendResult.status` gives a future
submission service (Phase 2/3) enough to classify a failure, but deciding
what to do about it - retry, defer, hand off to a human - is that later
service's job, not this transport's.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from email.message import EmailMessage as MimeMessage
from email.utils import make_msgid
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

import aiosmtplib
from aiosmtplib import errors as smtp_errors

from packages.shared.config import EmailTransportMode

if TYPE_CHECKING:
    from packages.shared.config import Settings


class EmailSendStatus(StrEnum):
    """The result classes a future submission service will branch
    retry/handoff behaviour on (AGENT_DESIGN.md §4.5; the M11 design-
    discovery report's retry matrix). Deliberately mirrors that matrix's
    shape rather than raw SMTP reply codes, so the caller never has to
    re-derive "is this the kind of failure that might succeed if retried"
    from a numeric code itself."""

    SENT = "sent"
    #: Permanent: an invalid recipient, or any 5xx response. Never retry.
    REJECTED = "rejected"
    #: The SMTP credential itself failed. Never retry; a human re-authenticates.
    AUTH_FAILED = "auth_failed"
    #: A connection/network problem, or a 4xx response. Might succeed later.
    TRANSIENT_FAILURE = "transient_failure"
    #: Timed out, or a failure this module cannot confidently classify. No
    #: confirmation either way - never assume success, never blindly retry.
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class EmailAttachment:
    filename: str
    content: bytes
    content_type: str = "application/octet-stream"


@dataclass(frozen=True)
class OutboundEmail:
    to: str
    subject: str
    body: str
    attachments: tuple[EmailAttachment, ...] = ()


@dataclass(frozen=True)
class EmailSendResult:
    status: EmailSendStatus
    #: Our own generated Message-ID, not the server's free-text response -
    #: stable and known before the send is attempted, present on every
    #: outcome that got as far as constructing a message (not only SENT),
    #: so a caller can correlate a rejection with the message that provoked
    #: it.
    message_id: str | None = None
    smtp_code: int | None = None
    detail: str = ""

    @property
    def sent(self) -> bool:
        return self.status is EmailSendStatus.SENT


class EmailTransport(Protocol):
    async def send(self, message: OutboundEmail) -> EmailSendResult: ...


class FakeEmailTransport:
    """Deterministic, offline. Never imports or calls anything network-capable.

    `responses` is consumed in order, one per `send()` call; once exhausted,
    every further call returns `default_result` (`SENT` unless overridden).
    Every attempted message is appended to `self.sent`, in order, regardless
    of whether the scripted result for that attempt was success or failure -
    exactly what a test asserting "no duplicate send" or "the right message
    was attempted" needs to inspect afterwards.
    """

    def __init__(
        self,
        *,
        responses: Sequence[EmailSendResult] | None = None,
        default_result: EmailSendResult | None = None,
    ) -> None:
        self._responses: list[EmailSendResult] = list(responses or ())
        self._default = default_result or EmailSendResult(
            status=EmailSendStatus.SENT, message_id="fake-message-id@jobforge.invalid",
        )
        self.sent: list[OutboundEmail] = []

    async def send(self, message: OutboundEmail) -> EmailSendResult:
        self.sent.append(message)
        if self._responses:
            return self._responses.pop(0)
        return self._default


class SMTPEmailTransport:
    """Real SMTP via `aiosmtplib`. Classifies every failure it can name into
    one of `EmailSendStatus`; it does not decide what a caller should do
    about it, and it performs no retry of its own."""

    def __init__(self, settings: Settings, *, sender: str) -> None:
        if not settings.smtp_host:
            raise ValueError(
                "SMTPEmailTransport requires SMTP_HOST to be configured "
                "(EMAIL_TRANSPORT_MODE=smtp was selected without a host)"
            )
        self._host = settings.smtp_host
        self._port = settings.smtp_port
        self._username = settings.smtp_username or None
        self._password = settings.smtp_password or None
        self._sender = sender

    async def send(self, message: OutboundEmail) -> EmailSendResult:
        message_id = make_msgid(domain="jobforge.invalid")
        mime = MimeMessage()
        mime["Message-ID"] = message_id
        mime["From"] = self._sender
        mime["To"] = message.to
        mime["Subject"] = message.subject
        mime.set_content(message.body)
        for attachment in message.attachments:
            maintype, _, subtype = attachment.content_type.partition("/")
            mime.add_attachment(
                attachment.content,
                maintype=maintype or "application",
                subtype=subtype or "octet-stream",
                filename=attachment.filename,
            )
        try:
            await aiosmtplib.send(
                mime, hostname=self._host, port=self._port,
                username=self._username, password=self._password,
            )
        except smtp_errors.SMTPAuthenticationError as exc:
            return EmailSendResult(
                EmailSendStatus.AUTH_FAILED, message_id=message_id, detail=str(exc)
            )
        # SMTPConnectTimeoutError/SMTPReadTimeoutError are SMTPTimeoutError
        # subclasses; caught before SMTPConnectError below, which one of
        # them also subclasses.
        except smtp_errors.SMTPTimeoutError as exc:
            return EmailSendResult(
                EmailSendStatus.AMBIGUOUS, message_id=message_id, detail=str(exc)
            )
        except smtp_errors.SMTPRecipientsRefused as exc:
            return EmailSendResult(
                EmailSendStatus.REJECTED, message_id=message_id, detail=str(exc)
            )
        except smtp_errors.SMTPResponseException as exc:
            status = (
                EmailSendStatus.REJECTED if exc.code >= 500
                else EmailSendStatus.TRANSIENT_FAILURE
            )
            return EmailSendResult(
                status, message_id=message_id, smtp_code=exc.code, detail=str(exc)
            )
        # SMTPConnectError is raised only from aiosmtplib's own
        # _create_connection() (smtp.py) - strictly before any SMTP command
        # is ever sent, so a retry can never duplicate a send. Provably safe.
        except smtp_errors.SMTPConnectError as exc:
            return EmailSendResult(
                EmailSendStatus.TRANSIENT_FAILURE, message_id=message_id, detail=str(exc)
            )
        # SMTPServerDisconnected is NOT confined to the pre-send phase the
        # way SMTPConnectError is: aiosmtplib's protocol.execute_data_command
        # raises the identical exception class whether the connection was
        # lost before DATA started or after the full message body was
        # already written and only the final "250"/error response was lost
        # (protocol.py's read_response: `if self._response_waiter is None:
        # raise SMTPServerDisconnected`) - the same path a mid-session
        # STARTTLS failure or a reset during the DATA write can also reach.
        # Nothing in the exception distinguishes which happened, so this
        # must be AMBIGUOUS, never auto-retried: a retry here could resubmit
        # a message the remote server already accepted.
        except smtp_errors.SMTPServerDisconnected as exc:
            return EmailSendResult(
                EmailSendStatus.AMBIGUOUS, message_id=message_id, detail=str(exc)
            )
        except smtp_errors.SMTPException as exc:
            return EmailSendResult(
                EmailSendStatus.AMBIGUOUS, message_id=message_id, detail=str(exc)
            )
        except (TimeoutError, OSError) as exc:
            return EmailSendResult(
                EmailSendStatus.AMBIGUOUS, message_id=message_id, detail=str(exc)
            )
        return EmailSendResult(EmailSendStatus.SENT, message_id=message_id)


def build_email_transport(settings: Settings, *, sender: str = "") -> EmailTransport:
    """The one place `EmailTransportMode` is read to construct a transport.

    `sender` is required only for `SMTPEmailTransport` (it has to put
    something in the `From` header); `FakeEmailTransport` ignores it, so a
    caller in FAKE mode never needs to have one configured yet.
    """
    if settings.email_transport_mode is EmailTransportMode.SMTP:
        return SMTPEmailTransport(settings, sender=sender)
    return FakeEmailTransport()
