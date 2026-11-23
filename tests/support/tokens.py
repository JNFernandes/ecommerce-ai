"""Test signing keys, a JWKS document and a token factory for Identity-style RS256 tokens.

Keys are generated once per test session. ``make_token`` builds valid tokens by default and
every bad-token case the auth tests need: expired, wrong issuer or audience, unknown ``kid``,
HS256, ``alg=none``, missing claims and tampered payloads.
"""

import base64
import json
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from functools import cache

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from ecommerce_ai.infrastructure.settings import Settings

KID = "test-key-1"
"""Key id of the session signing key published in ``jwks()``."""

UNKNOWN_KID = "unknown-key"
"""A key id that ``jwks()`` never publishes."""

CUSTOMER_ID = uuid.UUID("6f1c2b8e-4d3a-4f6e-9a7b-1c2d3e4f5a6b")
"""``customerId`` claim of the default token."""

HS256_SECRET = "a-shared-secret-that-is-long-enough-for-hs256!"  # noqa: S105  # fake, test-only
"""Symmetric secret for the HS256 (wrong algorithm) case."""

ISSUER: str = Settings.model_fields["jwt_issuer"].default
"""Default ``jwt_issuer`` setting (read from the field, not the environment)."""

AUDIENCE: str = Settings.model_fields["jwt_audience"].default
"""Default ``jwt_audience`` setting."""


@cache
def signing_key() -> rsa.RSAPrivateKey:
    """Return the session RSA key whose public half is published in ``jwks()``."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@cache
def foreign_key() -> rsa.RSAPrivateKey:
    """Return an RSA key that Identity never published (signature check must fail)."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def jwks(
    *, kid: str = KID, key: rsa.RSAPrivateKey | None = None
) -> dict[str, list[dict[str, object]]]:
    """Return a JWKS document with one RS256 public key."""
    public = (key or signing_key()).public_key()
    jwk: dict[str, object] = dict(RSAAlgorithm.to_jwk(public, as_dict=True))
    jwk.update({"kid": kid, "alg": "RS256", "use": "sig"})
    return {"keys": [jwk]}


def make_token(
    *,
    kid: str | None = KID,
    alg: str = "RS256",
    key: rsa.RSAPrivateKey | None = None,
    expires_in: timedelta = timedelta(minutes=5),
    omit: Iterable[str] = (),
    **claims: object,
) -> str:
    """Return a signed token; ``claims`` override the defaults and ``omit`` removes claims.

    Defaults: ``sub``, ``roles=["CUSTOMER"]``, ``customerId``, ``iat``, ``exp``, ``iss`` and
    ``aud`` matching the default settings. ``alg`` may be ``RS256``, ``HS256`` or ``none``.
    """
    now = datetime.now(UTC)
    payload: dict[str, object] = {
        "sub": "user-1",
        "roles": ["CUSTOMER"],
        "customerId": str(CUSTOMER_ID),
        "iat": int(now.timestamp()),
        "exp": int((now + expires_in).timestamp()),
        "iss": ISSUER,
        "aud": AUDIENCE,
    }
    payload.update(claims)
    for name in omit:
        payload.pop(name, None)
    headers = {"kid": kid} if kid is not None else None
    if alg == "RS256":
        return jwt.encode(payload, key or signing_key(), algorithm="RS256", headers=headers)
    if alg == "HS256":
        return jwt.encode(payload, HS256_SECRET, algorithm="HS256", headers=headers)
    if alg == "none":
        return jwt.encode(payload, "", algorithm="none", headers=headers)
    msg = f"unsupported test algorithm: {alg}"
    raise ValueError(msg)


def tamper(token: str, **claims: object) -> str:
    """Return ``token`` with its payload changed but the original signature kept."""
    header, payload, signature = token.split(".")
    decoded = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    decoded.update(claims or {"roles": ["ADMIN"]})
    forged = base64.urlsafe_b64encode(json.dumps(decoded).encode()).rstrip(b"=").decode()
    return f"{header}.{forged}.{signature}"
