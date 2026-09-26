"""Shared local account actions; credentials are supplied by private transport inputs."""

from __future__ import annotations

from dataclasses import asdict

from af.application.accounts import AccountService
from af.application.authentication import AuthenticationService
from af.application.passkeys import PasskeyService
from af.framework.accounts import AccountNotificationStore
from af.framework.authentication import AuthenticationDenied
from af.framework.context import Principal

_FIELDS = {
    "register": {"address", "password"},
    "verification-request": {"address"},
    "verify": {"secret"},
    "reset-request": {"address"},
    "reset": {"secret", "password"},
    "login": {"address", "password", "factor"},
    "totp-begin": {"principal_secret", "password"},
    "totp-confirm": {"principal_secret", "code"},
    "notifications": set(),
    "notification-export": {"message_id"},
    "notification-send": {"message_id", "consented"},
    "passkey-register-begin": {"principal_secret", "password", "factor"},
    "passkey-register-complete": {"principal_secret", "challenge_id", "credential"},
    "passkey-login-begin": {"address"},
    "passkey-login-complete": {"challenge_id", "credential_id", "credential"},
}


def _text(data: dict[str, object], key: str) -> str:
    value = data[key]
    if type(value) is not str or not isinstance(value, str):
        raise ValueError("expected text")
    return value


def _factor(data: dict[str, object]) -> str | None:
    return None if data["factor"] is None else _text(data, "factor")


class AccountController:
    actions = tuple(_FIELDS)

    def __init__(
        self,
        accounts: AccountService,
        authentication: AuthenticationService,
        notifications: AccountNotificationStore,
        *,
        tenant_id: str,
        passkeys: PasskeyService | None = None,
    ) -> None:
        self._accounts, self._authentication, self._notifications = (
            accounts,
            authentication,
            notifications,
        )
        self._tenant, self._passkeys = tenant_id, passkeys

    def _principal(self, data: dict[str, object]) -> Principal:
        return self._authentication.authenticate(
            _text(data, "principal_secret"), tenant_id=self._tenant
        )

    def run(self, action: str, payload: object) -> dict[str, object]:
        if (
            action not in _FIELDS
            or not isinstance(payload, dict)
            or set(payload) != _FIELDS[action]
        ):
            raise ValueError("invalid account action input")
        data: dict[str, object] = payload
        if action == "register":
            return asdict(
                self._accounts.register(
                    _text(data, "address"), _text(data, "password"), tenant_id=self._tenant
                )
            )
        if action == "verification-request":
            return asdict(
                self._accounts.request_verification(_text(data, "address"), tenant_id=self._tenant)
            )
        if action == "verify":
            return asdict(self._accounts.verify(_text(data, "secret"), tenant_id=self._tenant))
        if action == "reset-request":
            return asdict(
                self._accounts.request_reset(_text(data, "address"), tenant_id=self._tenant)
            )
        if action == "reset":
            return asdict(
                self._accounts.reset_password(
                    _text(data, "secret"), _text(data, "password"), tenant_id=self._tenant
                )
            )
        if action == "login":
            return asdict(
                self._accounts.login(
                    _text(data, "address"),
                    _text(data, "password"),
                    tenant_id=self._tenant,
                    factor=_factor(data),
                )
            )
        if action == "totp-begin":
            return {
                "totp_secret": self._accounts.begin_totp(
                    self._principal(data), _text(data, "password"), tenant_id=self._tenant
                )
            }
        if action == "totp-confirm":
            return {
                "recovery_codes": self._accounts.confirm_totp(
                    self._principal(data), _text(data, "code"), tenant_id=self._tenant
                )
            }
        if action == "notifications":
            return {"notifications": [asdict(item) for item in self._notifications.pending()]}
        if action == "notification-export":
            return asdict(self._notifications.export(_text(data, "message_id")))
        if action == "notification-send":
            if data["consented"] is not True:
                raise AuthenticationDenied()
            return {"submission_status": self._notifications.dispatch(_text(data, "message_id"))}
        passkeys = self._passkeys
        if passkeys is None:
            raise AuthenticationDenied()
        if action == "passkey-register-begin":
            return asdict(
                passkeys.begin_registration(
                    self._principal(data),
                    _text(data, "password"),
                    tenant_id=self._tenant,
                    factor=_factor(data),
                )
            )
        if action == "passkey-register-complete":
            passkeys.complete_registration(
                _text(data, "challenge_id"),
                _text(data, "credential"),
                self._principal(data),
                tenant_id=self._tenant,
            )
            return {"registered": True}
        if action == "passkey-login-begin":
            return asdict(
                passkeys.begin_authentication(_text(data, "address"), tenant_id=self._tenant)
            )
        return asdict(
            passkeys.complete_authentication(
                _text(data, "challenge_id"),
                _text(data, "credential_id"),
                _text(data, "credential"),
                tenant_id=self._tenant,
            )
        )

    def cancel_output(self, action: str, result: dict[str, object]) -> None:
        if action in {"login", "passkey-login-complete"}:
            self._authentication.revoke(_text(result, "token_id"), tenant_id=self._tenant)
