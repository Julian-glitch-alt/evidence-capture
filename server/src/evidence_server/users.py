"""Per-adjuster accounts (T47, tasks/t47-design.md).

Pure: no database, no clock, no network. Passwords are stdlib scrypt with a
random 16-byte salt (r=8, p=1, dklen=32, N from SCRYPT_N, default 16384),
stored as `scrypt$N$r$p$salthex$hashhex` so verification always uses the
stored cost. Comparison with hmac.compare_digest.
"""

import hashlib
import hmac
import os
import secrets

OTP_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
OTP_LENGTH = 16
PASSWORD_MIN, PASSWORD_MAX = 12, 128
NAME_MAX = 80
EMAIL_MAX = 254


def _scrypt_n() -> int:
    try:
        return int(os.environ.get("SCRYPT_N", "16384"))
    except ValueError:
        return 16384


def hash_password(password: str) -> str:
    """-> the stored hash of a new password."""
    salt = secrets.token_bytes(16)
    n = _scrypt_n()
    digest = hashlib.scrypt(password.encode(), salt=salt, n=n, r=8, p=1, dklen=32)
    return f"scrypt${n}$8$1${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """-> True when the password matches the stored hash. False for a malformed hash."""
    try:
        parts = (stored or "").split("$")
        if len(parts) != 6 or parts[0] != "scrypt":
            return False
        _, n, r, p, salthex, hashhex = parts
        expected = bytes.fromhex(hashhex)
        digest = hashlib.scrypt(
            password.encode(), salt=bytes.fromhex(salthex),
            n=int(n), r=int(r), p=int(p), dklen=len(expected),
        )
        return hmac.compare_digest(digest, expected)
    except (ValueError, OverflowError):
        return False


def one_time_password() -> str:
    """A 16-character password from the unambiguous alphabet (no 0/O/1/l)."""
    return "".join(secrets.choice(OTP_ALPHABET) for _ in range(OTP_LENGTH))


def check_new_password(password: str) -> str:
    """-> the password, or raises ValueError(plain message) when it is too short or long."""
    if not isinstance(password, str) or not PASSWORD_MIN <= len(password) <= PASSWORD_MAX:
        raise ValueError("The password must be 12 to 128 characters")
    return password


def check_email(email: str) -> str:
    """-> the email trimmed and lowercased, or raises ValueError(plain message)."""
    clean = (email.strip().lower() if isinstance(email, str) else "")
    if len(clean) > EMAIL_MAX or clean.count("@") != 1 or any(not side for side in clean.split("@")):
        raise ValueError("Enter an email address like name@example.com")
    return clean


def check_name(name: str) -> str:
    """-> the name stripped, or raises ValueError(plain message) when blank or too long."""
    clean = (name.strip() if isinstance(name, str) else "")
    if not clean or len(clean) > NAME_MAX:
        raise ValueError("Enter a name of 1 to 80 characters")
    return clean
