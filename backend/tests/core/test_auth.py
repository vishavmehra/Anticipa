"""ADR-057: Supabase JWT verification (JWKS/ES256, aud/iss/exp, role only from app_metadata). No network."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import uuid

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from anticipa.config import Settings
from anticipa.core.auth import authenticate_token
from anticipa.core.errors import Forbidden, ProviderUnavailable, Unauthenticated
from anticipa.core.rbac import ROLES, permissions_for

pytestmark = pytest.mark.usefixtures("jwks")


def test_valid_token_yields_user_with_role_from_app_metadata(mint_token, test_settings):
    sub = uuid.uuid4()
    user = authenticate_token(
        mint_token(
            "care_coordinator", sub=sub, email="c@example.test", user_metadata={"display_name": "Casey"}
        ),
        test_settings,
    )
    assert user.id == sub and user.email == "c@example.test" and user.role == "care_coordinator"
    assert user.display_name == "Casey"
    assert user.permissions == permissions_for("care_coordinator")


@pytest.mark.parametrize("role", ROLES)
def test_every_defined_role_is_accepted(mint_token, test_settings, role):
    assert authenticate_token(mint_token(role), test_settings).role == role


def test_role_in_user_metadata_is_never_trusted(mint_token, test_settings):
    token = mint_token(None, app_metadata={}, user_metadata={"anticipa_role": "system_admin"})
    with pytest.raises(Forbidden):
        authenticate_token(token, test_settings)


@pytest.mark.parametrize("claim", ["root", "", None, ["observer"], {"role": "observer"}, 7])
def test_missing_unknown_or_malformed_role_is_forbidden(mint_token, test_settings, claim):
    with pytest.raises(Forbidden):
        authenticate_token(mint_token(None, app_metadata={"anticipa_role": claim}), test_settings)


def test_app_metadata_of_the_wrong_type_is_forbidden(mint_token, test_settings):
    with pytest.raises(Forbidden):
        authenticate_token(mint_token(None, app_metadata="observer"), test_settings)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "overrides",
    [
        {"aud": "anon"},
        {"aud": None},
        {"iss": "https://evil.supabase.co/auth/v1"},
        {"iss": None},
        {"expires_in": -3600},
        {"expires_in": None},
        {"sub": "not-a-uuid"},
    ],
    ids=["wrong-aud", "no-aud", "wrong-iss", "no-iss", "expired", "no-exp", "sub-not-uuid"],
)
def test_invalid_claims_are_rejected(mint_token, test_settings, overrides):
    with pytest.raises(Unauthenticated):
        authenticate_token(mint_token("observer", **overrides), test_settings)


def test_token_signed_by_another_key_is_rejected(mint_token, test_settings):
    attacker = ec.generate_private_key(ec.SECP256R1())
    with pytest.raises(Unauthenticated):
        authenticate_token(
            mint_token("system_admin", key=attacker), test_settings
        )  # same kid, wrong signature


def test_unknown_kid_is_rejected(mint_token, test_settings):
    with pytest.raises(Unauthenticated):
        authenticate_token(mint_token("system_admin", kid="rotated-away"), test_settings)


def test_hs256_signed_with_the_public_key_is_rejected(jwks, test_settings):
    """Algorithm-confusion attack: HS256 with the (public) key bytes as the shared secret (PyJWT refuses to
    sign
    that, so the token is assembled by hand)."""
    public_pem = jwks.private_key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)

    def b64(data: bytes) -> bytes:
        return base64.urlsafe_b64encode(data).rstrip(b"=")

    header = b64(json.dumps({"alg": "HS256", "typ": "JWT", "kid": jwks.kid}).encode())
    payload = b64(
        json.dumps({
            "sub": str(uuid.uuid4()), "aud": "authenticated", "iss": f"{test_settings.supabase_url}/auth/v1",
            "exp": 4_000_000_000, "app_metadata": {"anticipa_role": "system_admin"},
        }).encode()
    )  # fmt: skip
    signature = b64(hmac.new(public_pem, header + b"." + payload, hashlib.sha256).digest())
    with pytest.raises(Unauthenticated):
        authenticate_token((header + b"." + payload + b"." + signature).decode(), test_settings)


def test_unsigned_alg_none_is_rejected(test_settings, jwks):
    token = jwt.encode(
        {"sub": str(uuid.uuid4()), "aud": "authenticated", "app_metadata": {"anticipa_role": "system_admin"}},
        None,
        algorithm="none",
        headers={"kid": jwks.kid},
    )
    with pytest.raises(Unauthenticated):
        authenticate_token(token, test_settings)


@pytest.mark.parametrize("garbage", ["", "abc", "a.b.c", "Bearer x"])
def test_garbage_is_rejected(test_settings, garbage):
    with pytest.raises(Unauthenticated):
        authenticate_token(garbage, test_settings)


def test_unreachable_jwks_is_a_503_not_a_401(mint_token, test_settings, jwks):
    token = mint_token("observer")
    jwks.unreachable = True
    with pytest.raises(ProviderUnavailable):
        authenticate_token(token, test_settings)


def test_auth_provider_not_configured_is_a_503():
    with pytest.raises(ProviderUnavailable):
        authenticate_token("x.y.z", Settings(_env_file=None, supabase_url=None))
