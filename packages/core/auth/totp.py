"""TOTP second factor (SECURITY.md §3.1).

The stored secret is encrypted at rest; this module takes the *decrypted*
secret and never reads the database itself, so the decryption boundary stays
in one place (`packages.core.crypto`).

`valid_window=1` accepts the immediately preceding and following 30-second
step, which absorbs ordinary clock skew between the user's device and the
server. Widening it further would materially enlarge the window an intercepted
code stays usable in.
"""

from __future__ import annotations

#: One step either side of now. 30s per step, so ±30s of tolerated skew.
VALID_WINDOW = 1

#: What the authenticator app displays as the account name.
ISSUER = "JobForge"


def generate_secret() -> str:
    """A fresh base32 TOTP secret, for setup tooling only."""
    import pyotp

    return pyotp.random_base32()


def verify_code(secret: str, code: str) -> bool:
    """True when `code` is valid for `secret` now (±1 step).

    Returns False rather than raising for empty or malformed input: a login
    attempt with a nonsense code is a failed login, not a server error.
    """
    if not secret or not code:
        return False
    code = code.strip().replace(" ", "")
    if not code.isdigit():
        return False
    import pyotp

    try:
        return pyotp.TOTP(secret).verify(code, valid_window=VALID_WINDOW)
    except (ValueError, TypeError):
        return False


def provisioning_uri(secret: str, email: str) -> str:
    """The `otpauth://` URI to enrol an authenticator app.

    Shown once at setup and never stored or logged - it contains the secret
    in full, which is exactly why `provisioning_uri` and `totp_uri` are in the
    logging redaction list.
    """
    import pyotp

    return pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name=ISSUER)
