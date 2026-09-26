"""OIDC verifies signed tokens before exposing an external identity."""

from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from af.framework.authentication import AuthenticationDenied, pkce_challenge
from af.infrastructure.oidc import OIDCIdentityProvider


def test_oidc_code_exchange_binds_nonce_issuer_audience_and_signature() -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    verifier = "a" * 43
    claims = {
        "iss": "https://id.example",
        "aud": "af",
        "sub": "subject",
        "iat": 100,
        "exp": 200,
        "nonce": pkce_challenge(verifier),
    }
    responses = [jwt.encode(claims, key, algorithm="RS256", headers={"kid": "key1"})]
    requests: list[httpx.Request] = []
    provider = OIDCIdentityProvider(
        provider_id="example",
        issuer="https://id.example",
        client_id="af",
        authorization_endpoint="https://id.example/authorize",
        token_endpoint="https://id.example/token",
        redirect_uris=("https://af.example/callback",),
        signing_keys={"key1": key.public_key()},
        allow_remote=True,
        clock=lambda: 150,
        transport=httpx.MockTransport(
            lambda request: (
                requests.append(request) or httpx.Response(200, json={"id_token": responses[0]})
            )
        ),
    )
    url = provider.authorization_url(
        state="s" * 32,
        challenge=pkce_challenge(verifier),
        redirect_uri="https://af.example/callback",
    )
    query = parse_qs(urlsplit(url).query)
    assert query["nonce"] == [pkce_challenge(verifier)]
    assert query["code_challenge_method"] == ["S256"]
    assert (
        provider.exchange(
            code="one-time", verifier=verifier, redirect_uri="https://af.example/callback"
        ).subject
        == "subject"
    )
    assert b"grant_type=authorization_code" in requests[0].content
    for changed in (
        {"nonce": "wrong"},
        {"aud": "other"},
        {"iss": "https://evil.example"},
        {"exp": 149},
        {"iat": 999},
        {"azp": "other"},
        {"aud": ["af", "other"]},
    ):
        responses[0] = jwt.encode(
            {**claims, **changed}, key, algorithm="RS256", headers={"kid": "key1"}
        )
        with pytest.raises(AuthenticationDenied):
            provider.exchange(
                code="code", verifier=verifier, redirect_uri="https://af.example/callback"
            )
    responses[0] = jwt.encode(claims, "a" * 32, algorithm="HS256", headers={"kid": "key1"})
    with pytest.raises(AuthenticationDenied):
        provider.exchange(
            code="code", verifier=verifier, redirect_uri="https://af.example/callback"
        )


def test_oidc_refuses_bad_configuration_unapproved_egress_and_bounded_failures() -> None:
    from cryptography.hazmat.primitives.asymmetric import ec

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    config = dict(
        provider_id="example",
        issuer="https://id.example",
        client_id="af",
        authorization_endpoint="https://id.example/authorize",
        token_endpoint="https://id.example/token",
        redirect_uris=("https://af.example/callback",),
        signing_keys={"key": key.public_key()},
        clock=lambda: 150,
    )
    for override in (
        {"issuer": "http://127.0.0.1"},
        {"redirect_uris": ()},
        {"signing_keys": {}},
        {"signing_keys": {"key": "secret"}},
        {
            "signing_keys": {
                "key": rsa.generate_private_key(public_exponent=65537, key_size=1024).public_key()
            }
        },
        {"signing_keys": {"key": ec.generate_private_key(ec.SECP384R1()).public_key()}},
        {"timeout": float("inf")},
        {"client_secret": "x" * 4097},
    ):
        with pytest.raises(ValueError):
            OIDCIdentityProvider(**(config | override))
    provider = OIDCIdentityProvider(**config)
    with pytest.raises(AuthenticationDenied):
        provider.authorization_url(
            state="bad", challenge="a" * 43, redirect_uri="https://af.example/callback"
        )
    with pytest.raises(AuthenticationDenied):
        provider.exchange(
            code="code", verifier="a" * 43, redirect_uri="https://af.example/callback"
        )
    for response in (
        httpx.Response(302, headers={"Location": "https://other.example"}),
        httpx.Response(200, content=b"x" * 65537),
        httpx.Response(200, json={"id_token": 1}),
        httpx.Response(200, json={"id_token": "not.a.jwt"}),
        httpx.Response(200, json={"id_token": "bad"}),
    ):
        transport = httpx.MockTransport(lambda request, response=response: response)
        provider = OIDCIdentityProvider(
            **config, allow_remote=True, transport=transport, client_secret="private"
        )
        with pytest.raises(AuthenticationDenied):
            provider.exchange(
                code="code", verifier="a" * 43, redirect_uri="https://af.example/callback"
            )


def test_oidc_claim_types_key_binding_and_es256() -> None:
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    claims = {
        "iss": "https://id.example",
        "aud": "af",
        "sub": "subject",
        "iat": 100,
        "exp": 200,
        "nonce": pkce_challenge("a" * 43),
    }
    responses = [""]
    provider = OIDCIdentityProvider(
        provider_id="example",
        issuer="https://id.example",
        client_id="af",
        authorization_endpoint="https://id.example/auth",
        token_endpoint="https://id.example/token",
        redirect_uris=("https://af.example/callback",),
        signing_keys={"key": key.public_key()},
        allow_remote=True,
        clock=lambda: 150,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"id_token": responses[0]})
        ),
    )
    responses[0] = jwt.encode(claims, key, algorithm="ES256", headers={"kid": "key"})
    assert (
        provider.exchange(
            code="code", verifier="a" * 43, redirect_uri="https://af.example/callback"
        ).subject
        == "subject"
    )
    for delta in (
        {"iat": True},
        {"nbf": 999},
        {"nonce": True},
        {"exp": float("inf")},
        {"sub": 123},
    ):
        responses[0] = jwt.encode(claims | delta, key, algorithm="ES256", headers={"kid": "key"})
        with pytest.raises(AuthenticationDenied):
            provider.exchange(
                code="code", verifier="a" * 43, redirect_uri="https://af.example/callback"
            )
    responses[0] = jwt.encode(claims, key, algorithm="ES256", headers={"kid": "other"})
    with pytest.raises(AuthenticationDenied):
        provider.exchange(
            code="code", verifier="a" * 43, redirect_uri="https://af.example/callback"
        )
