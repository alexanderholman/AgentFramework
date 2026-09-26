# Headless authentication and account actions

AF keeps credentials and account state in the configured SQLite database. Host
administration is a local OS authority boundary; the CLI registration commands
must not be exposed as unauthenticated HTTP endpoints. The core services accept
injected repositories, clocks, password hashers, notification ports, identity
providers, and passkey verifiers.

## Scoped tokens and HTTP

Set `AF_HTTP_AUTH_REQUIRED=true` and `AF_AUTH_TENANT_ID=local` to require bearer
authentication at the HTTP application boundary. `af:read` permits GET requests;
`af:write` permits mutations. Issue a scoped credential without printing its secret:

```bash
af auth issue --subject operator --scope af:read --output operator-token.json
af auth rotate --credential operator-token.json --output rotated-token.json
af auth revoke TOKEN_ID
```

Credential destinations must be new files and are created with mode 0600. Token
secrets are randomly generated, visible only in the returned credential, and
stored as hashes. Expiry, tenant, scope, revocation and atomic rotation are checked
on each authentication. Rotation cannot extend the old credential's expiry or
widen its scopes. Revoking an OAuth client or refresh family also revokes access
tokens descended through the general token-rotation API.

## Accounts without a starter UI

`af accounts ACTION --request request.json --output new-result.json` runs the same
headless application actions used by adapters. Keep request files private: they
may contain passwords, bearer credentials, MFA seeds or recovery codes. All result
files use exclusive mode-0600 creation; stdout contains only completion metadata.

| Action | Exact request fields |
| --- | --- |
| `register` | `address`, `password` |
| `verification-request` | `address` |
| `verify` | `secret` |
| `reset-request` | `address` |
| `reset` | `secret`, `password` |
| `login` | `address`, `password`, `factor` (string or null) |
| `totp-begin` | `principal_secret`, `password` |
| `totp-confirm` | `principal_secret`, `code` |
| `notifications` | none (`{}`) |
| `notification-export` | `message_id` |
| `notification-send` | `message_id`, `consented` (must be true) |
| `passkey-register-begin` | `principal_secret`, `password`, `factor` |
| `passkey-register-complete` | `principal_secret`, `challenge_id`, `credential` (JSON string) |
| `passkey-login-begin` | `address` |
| `passkey-login-complete` | `challenge_id`, `credential_id`, `credential` (JSON string) |

Registration returns a neutral pending response. Verification and reset codes go
to an encrypted durable notification outbox. The explicit local-admin
`notification-export` action writes one message's secret to a private result file
and marks it exported. **Local export proves possession of the local code; it does
not prove ownership of an external email inbox.** This offline path is useful for
headless local administration and reproducible tests. A configured `MailManager`
with an `SMTPChannel` can submit a pending message using `notification-send`.
SMTP acceptance is recorded as submission, not verified delivery. Uncertain sends
are not automatically retried. A fresh verification/reset request creates a fresh
code and invalidates the prior code for that purpose.

Passwords contain 15–128 characters and use fixed scrypt N=2^17, r=8, p=1 with a
random salt. Persistent attempt windows precede password checks. Unknown accounts
use a dummy password hash. Reset codes expire in 15 minutes, are hashed at rest,
and are single-use. Password reset revokes existing sessions and leaves enrolled
MFA in place. Hosts determine registration scopes; clients cannot grant themselves
roles or scopes.

TOTP enrollment requires a current authenticated principal and password. The seed
is encrypted with a separately protected host key; confirming a current code
activates it and returns ten one-time recovery codes. Recovery codes are hashed at
rest. TOTP counters and recovery codes are consumed atomically to prevent replay.
Keep the encryption key available across process restarts and protect it separately
from the database. Losing it makes existing MFA seeds and pending notifications
unreadable.

Passkeys use the optional `identity` extra and the maintained `webauthn` package.
The host pins an exact HTTPS origin and relying-party ID. Registration requires
reauthentication, and verification requires user verification. Challenges expire
in five minutes and are consumed once; assertion counters and token issuance are
updated in one transaction. Software-authenticator tests exercise real P-256
signatures through the library. They do not establish physical-authenticator or
browser interoperability. The framework provides ceremony JSON; the host supplies
its browser/native WebAuthn ceremony surface.

## Optional OAuth grants

`af oauth ACTION --request request.json --output new-result.json` provides local
headless grant and client-management actions. Public-client registration and
service-client registration are host-admin operations. The JSON keys are exact:

| Action | Request fields |
| --- | --- |
| `client-register` | `client_id`, `redirect_uris` (array), `scopes` (array) |
| `client-revoke` | `client_id` |
| `service-register` | `client_id`, `scopes` |
| `service-revoke` | `client_id` |
| `client-credentials` | `client_id`, `secret`, `scopes` |
| `refresh-issue` | `client_id`, `principal_secret`, `scopes`, `consented` (true) |
| `refresh` | `client_id`, `refresh_secret`, `scopes` |
| `refresh-revoke` | `client_id`, `refresh_secret` |
| `device-start` | `client_id`, `scopes`, `verification_uri` |
| `device-approve` | `user_code`, `principal_secret`, `consented` (boolean) |
| `device-poll` | `client_id`, `device_code` |

Refresh tokens rotate atomically within a fixed expiry window; replay revokes the
whole family. Devices use short-lived high-entropy device codes, user approval,
minimum polling intervals and `slow_down` responses. Client credentials never
inherit a human principal. No flow silently broadens scopes.

`OAuthAuthorizationService` additionally implements authorization-code + S256
PKCE: exact registered redirects, tenant/client/state binding, explicit scoped
consent, five-minute codes and atomic single-use exchange. The host supplies its
consent presentation and HTTP routes; this is not a claim of a complete hosted
OAuth/OIDC authorization-server product or certification.

## External identity

`ExternalLoginService` registers explicit provider adapters, persists state and
PKCE binding, consumes state before exchange, checks tenant and account-linking
authority, and maps issuer/subject to local identities. It never links by matching
email or imports remote roles/scopes. The local host assigns access afterward.

`OIDCIdentityProvider` supports the authorization-code flow against explicitly
configured HTTPS endpoints and pinned RSA/P-256 public keys. It verifies the
signature, issuer, audience/authorized party, nonce, time claims and subject;
unknown keys and algorithms fail closed. Remote exchange requires explicit
`allow_remote=True`. There is no automatic discovery, dynamic client registration,
JWKS retrieval or trust-on-first-use. Key rotation is an explicit configuration
update. Signed mock-token tests establish adapter behavior, not live provider
qualification.

References: [RFC 7636](https://www.rfc-editor.org/rfc/rfc7636),
[RFC 9700](https://www.rfc-editor.org/rfc/rfc9700),
[RFC 8628](https://www.rfc-editor.org/rfc/rfc8628),
[OIDC Core](https://openid.net/specs/openid-connect-core-1_0.html),
[OWASP password storage](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html),
[py_webauthn](https://duo-labs.github.io/py_webauthn/).
