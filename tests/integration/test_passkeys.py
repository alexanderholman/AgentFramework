"""Actual WebAuthn library validates generated P-256 software-authenticator proofs."""

import base64
import hashlib
import json
from pathlib import Path

import pytest

pytest.importorskip("webauthn")
import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

from af.application.accounts import AccountService
from af.application.authentication import AuthenticationService
from af.application.passkeys import PasskeyService
from af.framework.authentication import AuthenticationDenied
from af.infrastructure.accounts import FernetSecretCipher, ScryptPasswordHasher
from af.infrastructure.passkeys import SQLitePasskeyRepository, WebAuthnVerifier


def enc(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def test_passkey_registration_authentication_and_replay(tmp_path: Path) -> None:
    class Mail:
        code = ""

        def deliver(self, address: str, kind: str, secret: str) -> None:
            self.code = secret

    mail = Mail()
    repo = SQLitePasskeyRepository(tmp_path / "db")
    auth = AuthenticationService(repo)
    accounts = AccountService(
        repo, auth, ScryptPasswordHasher(), FernetSecretCipher.generate(), mail
    )
    accounts.register("alice@example.com", "a sufficiently long password", tenant_id="a")
    accounts.verify(mail.code, tenant_id="a")
    token = accounts.login("alice@example.com", "a sufficiently long password", tenant_id="a")
    principal = auth.authenticate(token.secret, tenant_id="a")
    verifier = WebAuthnVerifier(rp_id="af.example", origin="https://af.example", rp_name="AF")
    service = PasskeyService(repo, auth, accounts, verifier)
    begin = service.begin_registration(principal, "a sufficiently long password", tenant_id="a")
    options = json.loads(begin.options_json)
    private = ec.generate_private_key(ec.SECP256R1())
    numbers = private.public_key().public_numbers()
    key_id = b"x" * 32
    key = cbor2.dumps(
        {1: 2, 3: -7, -1: 1, -2: numbers.x.to_bytes(32, "big"), -3: numbers.y.to_bytes(32, "big")}
    )
    client = json.dumps(
        {
            "type": "webauthn.create",
            "challenge": options["challenge"],
            "origin": "https://af.example",
        }
    ).encode()
    authenticator = (
        hashlib.sha256(b"af.example").digest()
        + b"\x45"
        + (0).to_bytes(4, "big")
        + bytes(16)
        + len(key_id).to_bytes(2, "big")
        + key_id
        + key
    )
    attestation = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": authenticator})
    credential = json.dumps(
        {
            "id": enc(key_id),
            "rawId": enc(key_id),
            "type": "public-key",
            "response": {"clientDataJSON": enc(client), "attestationObject": enc(attestation)},
        }
    )
    service.complete_registration(begin.challenge_id, credential, principal, tenant_id="a")
    with pytest.raises(AuthenticationDenied):
        service.complete_registration(begin.challenge_id, credential, principal, tenant_id="a")
    login = service.begin_authentication("alice@example.com", tenant_id="a")
    client = json.dumps(
        {
            "type": "webauthn.get",
            "challenge": json.loads(login.options_json)["challenge"],
            "origin": "https://af.example",
        }
    ).encode()
    authenticator = hashlib.sha256(b"af.example").digest() + b"\x05" + (1).to_bytes(4, "big")
    signature = private.sign(
        authenticator + hashlib.sha256(client).digest(), ec.ECDSA(hashes.SHA256())
    )
    assertion = json.dumps(
        {
            "id": enc(key_id),
            "rawId": enc(key_id),
            "type": "public-key",
            "response": {
                "clientDataJSON": enc(client),
                "authenticatorData": enc(authenticator),
                "signature": enc(signature),
                "userHandle": None,
            },
        }
    )
    logged_in = service.complete_authentication(
        login.challenge_id, enc(key_id), assertion, tenant_id="a"
    )
    assert auth.authenticate(logged_in.secret, tenant_id="a").subject_id == principal.subject_id
    with pytest.raises(AuthenticationDenied):
        service.complete_authentication(login.challenge_id, enc(key_id), assertion, tenant_id="a")
