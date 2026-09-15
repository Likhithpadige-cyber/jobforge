"""Schemas for the authentication endpoints.

Nothing here ever carries `password_hash`, `totp_secret_enc`, the session id
or the TOTP secret. `UserRead` is the whole of what the frontend learns about
the signed-in human.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class LoginRequest(StrictModel):
    """Credentials for a login attempt.

    `totp_code` is optional *in the schema* so a local-development client may
    omit it. Whether it is actually required is decided by the server from
    `settings.totp_required`, never by the client: outside local/test a
    request without a code is rejected exactly as a wrong code would be.
    """

    email: EmailStr
    password: Annotated[str, Field(min_length=1, max_length=1024)]
    totp_code: Annotated[str | None, Field(min_length=6, max_length=10)] = None


class UserRead(BaseModel):
    """The minimal identity the frontend needs. No secrets, ever."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: str
    role: str
    last_login_at: datetime | None = None


class LoginResponse(BaseModel):
    """The CSRF token is returned so a fresh client can send it immediately.

    It is not a secret in the same sense as the session id: it is deliberately
    readable by the frontend, and useless without the HttpOnly session cookie.
    """

    user: UserRead
    csrf_token: str


class LogoutResponse(BaseModel):
    revoked: bool


class AuthConfigRead(BaseModel):
    """What the sign-in form needs before anyone has authenticated.

    Carries no secret and no user data - only whether this deployment demands
    a second factor, so the form can render the right fields instead of
    guessing or failing the first attempt.
    """

    totp_required: bool
