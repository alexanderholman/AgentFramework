"""Shared headless OAuth command actions with explicit principal authentication."""

from __future__ import annotations

from dataclasses import asdict

from af.application.authentication import AuthenticationService, OAuthAuthorizationService
from af.application.oauth_grants import OAuthGrantService
from af.framework.authentication import AuthenticationDenied, OAuthClient, checked_scopes

_ACTION_FIELDS = {
    "client-register": {"client_id", "redirect_uris", "scopes"},
    "client-revoke": {"client_id"},
    "service-register": {"client_id", "scopes"},
    "service-revoke": {"client_id"},
    "client-credentials": {"client_id", "secret", "scopes"},
    "refresh-issue": {"client_id", "principal_secret", "scopes", "consented"},
    "refresh": {"client_id", "refresh_secret", "scopes"},
    "refresh-revoke": {"client_id", "refresh_secret"},
    "device-start": {"client_id", "scopes", "verification_uri"},
    "device-approve": {"user_code", "principal_secret", "consented"},
    "device-poll": {"client_id", "device_code"},
}


def _text(data: dict[str, object], key: str) -> str:
    value = data[key]
    if not isinstance(value, str):
        raise ValueError("expected text")
    return value


def _strings(data: dict[str, object], key: str) -> tuple[str, ...]:
    value = data[key]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("expected text list")
    return tuple(value)


class OAuthController:
    """Client registration is a host-admin action, never an unauthenticated HTTP endpoint."""

    actions = tuple(_ACTION_FIELDS)

    def __init__(
        self,
        grants: OAuthGrantService,
        authorization: OAuthAuthorizationService,
        authentication: AuthenticationService,
        tenant_id: str,
    ) -> None:
        self._grants = grants
        self._authorization = authorization
        self._authentication = authentication
        self._tenant = tenant_id

    def run(self, action: str, payload: object) -> dict[str, object]:
        if (
            action not in _ACTION_FIELDS
            or not isinstance(payload, dict)
            or set(payload) != _ACTION_FIELDS[action]
        ):
            raise ValueError("invalid OAuth action input")
        data: dict[str, object] = payload
        client = _text(data, "client_id") if "client_id" in data else ""
        scopes = checked_scopes(_strings(data, "scopes")) if "scopes" in data else ()
        if action == "client-register":
            self._authorization.register_client(
                OAuthClient(client, self._tenant, _strings(data, "redirect_uris"), scopes)
            )
        elif action == "client-revoke":
            self._authorization.revoke_client(client, tenant_id=self._tenant)
        elif action == "service-register":
            return asdict(
                self._grants.register_service_client(client, tenant_id=self._tenant, scopes=scopes)
            )
        elif action == "service-revoke":
            self._grants.revoke_service_client(client, tenant_id=self._tenant)
        elif action == "client-credentials":
            return asdict(
                self._grants.client_credentials(
                    client, _text(data, "secret"), tenant_id=self._tenant, scopes=scopes
                )
            )
        elif action == "refresh-issue":
            if data["consented"] is not True:
                raise AuthenticationDenied()
            principal = self._authentication.authenticate(
                _text(data, "principal_secret"), tenant_id=self._tenant
            )
            return asdict(
                self._grants.issue_refresh_pair(
                    principal, tenant_id=self._tenant, client_id=client, scopes=scopes
                )
            )
        elif action == "refresh":
            return asdict(
                self._grants.refresh(
                    _text(data, "refresh_secret"),
                    tenant_id=self._tenant,
                    client_id=client,
                    scopes=scopes,
                )
            )
        elif action == "refresh-revoke":
            self._grants.revoke_refresh(
                _text(data, "refresh_secret"), tenant_id=self._tenant, client_id=client
            )
        elif action == "device-start":
            return asdict(
                self._grants.start_device(
                    client,
                    tenant_id=self._tenant,
                    scopes=scopes,
                    verification_uri=_text(data, "verification_uri"),
                )
            )
        elif action == "device-approve":
            if type(data["consented"]) is not bool:
                raise ValueError("consent must be a boolean")
            principal = self._authentication.authenticate(
                _text(data, "principal_secret"), tenant_id=self._tenant
            )
            self._grants.approve_device(
                _text(data, "user_code"),
                principal,
                tenant_id=self._tenant,
                consented=data["consented"],
            )
        elif action == "device-poll":
            return asdict(
                self._grants.poll_device(
                    _text(data, "device_code"), client_id=client, tenant_id=self._tenant
                )
            )
        return {"completed": True}

    def cancel_output(self, action: str, payload: object, result: dict[str, object]) -> None:
        """Revoke newly issued authority if a transport cannot deliver its private file."""
        if not isinstance(payload, dict):
            return
        if action == "service-register":
            self._grants.revoke_service_client(_text(payload, "client_id"), tenant_id=self._tenant)
        elif action in {"refresh", "refresh-issue"}:
            self._grants.revoke_refresh(
                _text(result, "refresh_secret"),
                tenant_id=self._tenant,
                client_id=_text(payload, "client_id"),
            )
        elif action in {"client-credentials", "device-poll"}:
            self._authentication.revoke(_text(result, "token_id"), tenant_id=self._tenant)
