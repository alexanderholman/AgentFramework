"""Explicit HTTPS OIDC authorization-code adapter with locally pinned verification keys.

No discovery, dynamic registration, remote JWKS fetch, implicit flow, email matching
or remote role adoption. Key rotation is an explicit host configuration update.
"""

from __future__ import annotations

import hmac
import time
from collections.abc import Callable, Mapping
from math import isfinite
from urllib.parse import urlencode, urlsplit

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric.ec import EllipticCurvePublicKey
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey

from af.framework.authentication import (
    AuthenticationDenied,
    ExternalIdentity,
    identity_value,
    pkce_challenge,
    valid_redirect,
)
from af.infrastructure.provider_http import ProviderFailure, decode_json, object_map

SigningKey = RSAPublicKey | EllipticCurvePublicKey


def _number(value: object) -> float:
    if (
        type(value) not in (int, float)
        or not isinstance(value, (int, float))
        or not isfinite(value)
    ):
        raise AuthenticationDenied()
    return float(value)


def _https_endpoint(value: str) -> None:
    valid_redirect(value)
    parsed = urlsplit(value)
    if parsed.scheme != "https" or parsed.query:
        raise ValueError("OIDC endpoints require fixed HTTPS URLs")


class OIDCIdentityProvider:
    def __init__(
        self,
        *,
        provider_id: str,
        issuer: str,
        client_id: str,
        authorization_endpoint: str,
        token_endpoint: str,
        redirect_uris: tuple[str, ...],
        signing_keys: Mapping[str, SigningKey],
        client_secret: str = "",
        allow_remote: bool = False,
        timeout: float = 30,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        for value in (provider_id, issuer, client_id):
            identity_value(value)
        for endpoint in (issuer, authorization_endpoint, token_endpoint):
            _https_endpoint(endpoint)
        if not 1 <= len(redirect_uris) <= 16 or not 1 <= len(signing_keys) <= 16:
            raise ValueError("OIDC requires registered redirects and pinned signing keys")
        for redirect in redirect_uris:
            valid_redirect(redirect)
        for kid, key in signing_keys.items():
            identity_value(kid)
            if not isinstance(key, (RSAPublicKey, EllipticCurvePublicKey)):
                raise ValueError("OIDC requires asymmetric public keys")
            if isinstance(key, RSAPublicKey) and key.key_size < 2048:
                raise ValueError("OIDC RSA keys require at least 2048 bits")
            if isinstance(key, EllipticCurvePublicKey) and key.curve.name != "secp256r1":
                raise ValueError("OIDC ES256 keys require P-256")
        if (
            type(allow_remote) is not bool
            or type(timeout) not in (int, float)
            or not isfinite(timeout)
            or not 0 < timeout <= 60
        ):
            raise ValueError("invalid OIDC transport policy")
        if type(client_secret) is not str or len(client_secret) > 4096:
            raise ValueError("invalid OIDC client credential")
        self.provider_id, self.issuer, self.redirect_uris = provider_id, issuer, redirect_uris
        self._client_id, self._client_secret = client_id, client_secret
        self._authorization_endpoint, self._token_endpoint = authorization_endpoint, token_endpoint
        self._keys = dict(signing_keys)
        self._allow_remote, self._timeout, self._transport, self._clock = (
            allow_remote,
            timeout,
            transport,
            clock,
        )

    def authorization_url(self, *, state: str, challenge: str, redirect_uri: str) -> str:
        if (
            redirect_uri not in self.redirect_uris
            or not 16 <= len(state) <= 512
            or len(challenge) != 43
        ):
            raise AuthenticationDenied()
        return (
            self._authorization_endpoint
            + "?"
            + urlencode(
                {
                    "response_type": "code",
                    "client_id": self._client_id,
                    "redirect_uri": redirect_uri,
                    "scope": "openid",
                    "state": state,
                    "nonce": challenge,
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                }
            )
        )

    def exchange(self, *, code: str, verifier: str, redirect_uri: str) -> ExternalIdentity:
        if (
            not self._allow_remote
            or redirect_uri not in self.redirect_uris
            or type(code) is not str
            or not 1 <= len(code) <= 4096
        ):
            raise AuthenticationDenied()
        nonce = pkce_challenge(verifier)
        auth = (
            httpx.BasicAuth(self._client_id, self._client_secret) if self._client_secret else None
        )
        failed = False
        identity: ExternalIdentity | None = None
        try:
            with (
                httpx.Client(
                    timeout=self._timeout,
                    transport=self._transport,
                    trust_env=False,
                    follow_redirects=False,
                ) as client,
                client.stream(
                    "POST",
                    self._token_endpoint,
                    auth=auth,
                    data={
                        "grant_type": "authorization_code",
                        "client_id": self._client_id,
                        "code": code,
                        "code_verifier": verifier,
                        "redirect_uri": redirect_uri,
                    },
                    headers={"Accept": "application/json"},
                ) as response,
            ):
                if response.status_code != 200:
                    raise AuthenticationDenied()
                content = bytearray()
                started = time.monotonic()
                for chunk in response.iter_bytes():
                    content.extend(chunk)
                    if len(content) > 65_536 or time.monotonic() - started > self._timeout:
                        raise AuthenticationDenied()
                body = object_map(decode_json(bytes(content)))
            token = body.get("id_token")
            if type(token) is not str or len(token) > 32_768:
                raise AuthenticationDenied()
            identity = self._verify(token, nonce)
        except (
            httpx.HTTPError,
            jwt.PyJWTError,
            ValueError,
            TypeError,
            ProviderFailure,
            AuthenticationDenied,
        ):
            failed = True
        if failed or identity is None:
            raise AuthenticationDenied()
        return identity

    def _verify(self, token: str, nonce: str) -> ExternalIdentity:
        parts = token.split(".")
        if len(parts) != 3:
            raise AuthenticationDenied()
        header = object_map(decode_json(jwt.utils.base64url_decode(parts[0])))
        claims = object_map(decode_json(jwt.utils.base64url_decode(parts[1])))
        if set(header) - {"alg", "typ", "kid"} or header.get("alg") not in {"RS256", "ES256"}:
            raise AuthenticationDenied()
        kid = header.get("kid")
        if type(kid) is not str or kid not in self._keys:
            raise AuthenticationDenied()
        key = self._keys[kid]
        algorithm = "RS256" if isinstance(key, RSAPublicKey) else "ES256"
        jwt.decode(
            token,
            key,
            algorithms=[algorithm],
            audience=self._client_id,
            issuer=self.issuer,
            options={
                "require": ["iss", "aud", "sub", "iat", "exp", "nonce"],
                "verify_exp": False,
                "verify_iat": False,
                "verify_nbf": False,
            },
        )
        now = self._clock()
        issued, expires = _number(claims["iat"]), _number(claims["exp"])
        if not now - 600 <= issued <= now + 30 or not now < expires <= issued + 86_400:
            raise AuthenticationDenied()
        if "nbf" in claims and _number(claims["nbf"]) > now + 30:
            raise AuthenticationDenied()
        audience = claims["aud"]
        if (
            isinstance(audience, list)
            and len(audience) > 1
            and claims.get("azp") != self._client_id
        ) or ("azp" in claims and claims["azp"] != self._client_id):
            raise AuthenticationDenied()
        if type(claims["nonce"]) is not str or not hmac.compare_digest(claims["nonce"], nonce):
            raise AuthenticationDenied()
        subject = claims["sub"]
        if type(subject) is not str:
            raise AuthenticationDenied()
        return ExternalIdentity(self.issuer, subject)
