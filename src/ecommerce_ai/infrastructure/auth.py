"""Local verification of Identity's RS256 access tokens (constitution II, research 6).

``TokenVerifier.verify`` checks the header first: anything but ``alg: RS256`` with a ``kid`` is
rejected before a key is looked up, so ``none`` and HS* tokens never reach a key. The signature,
``iss``, ``aud``, ``exp`` and ``iat`` are then checked with PyJWT, and ``sub``, ``exp``, ``iat``
and ``roles`` are required. Every reason a token is bad becomes the same ``InvalidToken``, so
callers can answer one identical 401. ``KeysUnavailable`` from the key source passes through
(the caller answers 503).
"""

from typing import Final, Protocol
from uuid import UUID

import jwt
from jwt import PyJWK
from pydantic import ValidationError

from ecommerce_ai.infrastructure.caller import CallerIdentity

ALGORITHM: Final = "RS256"
REQUIRED_CLAIMS: Final = ("sub", "exp", "iat", "roles")


class InvalidToken(Exception):
    """The token is malformed, forged, expired or not meant for this service."""


class SigningKeys(Protocol):
    """Source of Identity's public keys (``JwksCache``)."""

    async def get_key(self, kid: str) -> PyJWK | None:
        """Return the key of ``kid``, ``None`` when unknown; may raise ``KeysUnavailable``."""
        ...


class TokenVerifier:
    """Verifies bearer tokens against Identity's published keys."""

    def __init__(self, keys: SigningKeys, *, issuer: str, audience: str, leeway: float) -> None:
        """Accept tokens from ``issuer`` for ``audience``, allowing ``leeway`` seconds of skew."""
        self._keys = keys
        self._issuer = issuer
        self._audience = audience
        self._leeway = leeway

    async def verify(self, token: str) -> CallerIdentity:
        """Return the caller's identity, or raise ``InvalidToken``."""
        kid = _checked_kid(token)
        key = await self._keys.get_key(kid)
        if key is None:
            raise InvalidToken("unknown signing key")
        try:
            claims = jwt.decode(
                token,
                key.key,
                algorithms=[ALGORITHM],
                issuer=self._issuer,
                audience=self._audience,
                leeway=self._leeway,
                options={"require": list(REQUIRED_CLAIMS)},
            )
        except jwt.PyJWTError as error:
            raise InvalidToken("token rejected") from error
        return _identity(claims)


def _checked_kid(token: str) -> str:
    """Return the header's ``kid`` when the header announces RS256; raise ``InvalidToken``."""
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as error:
        raise InvalidToken("malformed token") from error
    if header.get("alg") != ALGORITHM:
        raise InvalidToken("unexpected algorithm")
    kid = header.get("kid")
    if not isinstance(kid, str) or not kid:
        raise InvalidToken("missing key id")
    return kid


def _identity(claims: dict[str, object]) -> CallerIdentity:
    """Build the identity from verified claims; malformed claim values are ``InvalidToken``."""
    subject, roles, customer_id = claims.get("sub"), claims.get("roles"), claims.get("customerId")
    if not isinstance(subject, str) or not subject:
        raise InvalidToken("invalid subject")
    if not isinstance(roles, list) or not all(isinstance(role, str) for role in roles):
        raise InvalidToken("invalid roles")
    try:
        return CallerIdentity(
            subject=subject,
            roles=frozenset(roles),
            customer_id=None if customer_id is None else UUID(str(customer_id)),
        )
    except (ValueError, ValidationError) as error:
        raise InvalidToken("invalid claims") from error
