"""The token verifier accepts only valid RS256 Identity tokens and rejects all others alike."""

from datetime import timedelta

import pytest
from jwt import PyJWK

from ecommerce_ai.infrastructure.auth import InvalidToken, TokenVerifier
from ecommerce_ai.infrastructure.jwks import KeysUnavailable
from tests.support.tokens import (
    AUDIENCE,
    CUSTOMER_ID,
    ISSUER,
    KID,
    UNKNOWN_KID,
    foreign_key,
    jwks,
    make_token,
    tamper,
)


class FakeKeys:
    """Serves the session key under ``KID``; records every lookup."""

    def __init__(self, *, unavailable: bool = False) -> None:
        self.lookups: list[str] = []
        self._unavailable = unavailable
        self._key = PyJWK(jwks()["keys"][0])

    async def get_key(self, kid: str) -> PyJWK | None:
        self.lookups.append(kid)
        if self._unavailable:
            raise KeysUnavailable("down")
        return self._key if kid == KID else None


def _verifier(keys: FakeKeys | None = None) -> TokenVerifier:
    return TokenVerifier(keys or FakeKeys(), issuer=ISSUER, audience=AUDIENCE, leeway=5)


async def test_should_return_identity_when_token_is_valid() -> None:
    identity = await _verifier().verify(make_token())

    assert identity.subject == "user-1"
    assert identity.roles == frozenset({"CUSTOMER"})
    assert identity.customer_id == CUSTOMER_ID


async def test_should_return_identity_without_customer_id_when_claim_is_absent() -> None:
    identity = await _verifier().verify(make_token(roles=["ADMIN"], omit=["customerId"]))

    assert identity.roles == frozenset({"ADMIN"})
    assert identity.customer_id is None


BAD_TOKENS = {
    "expired": lambda: make_token(expires_in=timedelta(minutes=-10)),
    "wrong issuer": lambda: make_token(iss="http://evil.test"),
    "wrong audience": lambda: make_token(aud="other-service"),
    "unknown kid": lambda: make_token(kid=UNKNOWN_KID),
    "foreign key": lambda: make_token(key=foreign_key()),
    "tampered": lambda: tamper(make_token()),
    "missing sub": lambda: make_token(omit=["sub"]),
    "missing exp": lambda: make_token(omit=["exp"]),
    "missing iat": lambda: make_token(omit=["iat"]),
    "missing roles": lambda: make_token(omit=["roles"]),
    "missing iss": lambda: make_token(omit=["iss"]),
    "missing aud": lambda: make_token(omit=["aud"]),
    "roles not a list": lambda: make_token(roles="CUSTOMER"),
    "non-string role": lambda: make_token(roles=[1]),
    "empty sub": lambda: make_token(sub=""),
    "bad customerId": lambda: make_token(customerId="not-a-uuid"),
    "garbage": lambda: "not.a.jwt",
    "empty": lambda: "",
}


@pytest.mark.parametrize("make", BAD_TOKENS.values(), ids=BAD_TOKENS.keys())
async def test_should_raise_invalid_token_when_token_is_bad(make: object) -> None:
    assert callable(make)

    with pytest.raises(InvalidToken):
        await _verifier().verify(make())


@pytest.mark.parametrize(
    "token",
    [make_token(alg="HS256"), make_token(alg="none"), make_token(kid=None)],
    ids=["HS256", "alg none", "no kid"],
)
async def test_should_reject_before_key_lookup_when_header_is_not_rs256_with_kid(
    token: str,
) -> None:
    keys = FakeKeys()

    with pytest.raises(InvalidToken):
        await _verifier(keys).verify(token)

    assert keys.lookups == []


async def test_should_propagate_keys_unavailable_when_no_key_can_be_obtained() -> None:
    with pytest.raises(KeysUnavailable):
        await _verifier(FakeKeys(unavailable=True)).verify(make_token())
