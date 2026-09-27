# Web access and API explorers

Report version: `1.0.0`, 2026-09-26. Target: WCAG 2.2 AA. Status: repository
implementation and automated evidence; independent accessibility review and manual
assistive-technology qualification remain pending. This report does not assert conformance.

## Installed surfaces

`af serve` exposes server-rendered Task pages at `/` and `/tasks`, the operations
projection at `/operations`, REST documentation at `/docs`, and the GraphQL explorer
at `/graphql/explorer`. `/graphql` is the JSON endpoint. Download schemas from
`/openapi.json` and `/graphql/schema.graphql`; `/redoc` redirects to the local REST docs.

The explorers use only packaged JavaScript and CSS, with no CDN request, inline
script, dynamic source evaluation, or relaxed content-security policy. The REST
page lists installed JSON operations from the actual OpenAPI schema. Request
fields and response types remain available in the schema; this small explorer
does not claim every Swagger UI feature or XML/SOAP editing.

Choose an operation, fill its named path fields, provide JSON query/body input,
and select **Send request**. The GraphQL page accepts a document, variables and an
optional operation name. No query or mutation runs on page load. Persisted-query
policy rejections are displayed; the explorer never registers or approves a query.
Credentials are optional password inputs, used only on an explicit same-origin
submission, never put in a URL or browser storage. Requests reject redirects.
The host's bearer requirement also applies to loading pages and assets; this is
not a browser login, account-management or OAuth-consent UI.

Response bodies are rendered as text, including error responses and hostile HTML.
ETags are shown for subsequent conditional requests. Bodies have a 1 MiB display
limit. **Cancel waiting** aborts the browser request; it does not promise cancellation
or rollback of a dispatched mutation. The UI reports that uncertainty and never
retries automatically. Clear the credential and displayed result when finished.
Documentation pages use `Cache-Control: no-store`.

## Implemented access behavior

| Area | Current behavior | Evidence boundary |
| --- | --- | --- |
| Structure and navigation | One main landmark, skip link, document language, headings, named navigation and explicit labels | Rendered-HTML assertions; not screen-reader certification |
| Keyboard and forms | Native focusable controls, visible focus outline, associated validation errors, invalid field value retained, status announcements | HTML/DOM behavior tests; physical browser/keyboard walkthrough pending |
| Layout | Relative sizes, wrapping navigation, resizable text areas, logical CSS dimensions, response scrolling | Source inspection; 200%/400% zoom and narrow-screen visual checks pending |
| Language and direction | Independent locale/territory controls, RTL document direction, isolated user text, explicit English for untranslated operations/explorers | Locale integration tests; no certification of translation quality |
| Color and motion | System light/dark colors, system focus highlight, forced-colors support, no required animation, reduced-motion override | CSS inspection; measured contrast in actual browser/OS combinations pending |
| No JavaScript | Task forms and operations remain server-rendered; explorer schemas and REST operation list remain readable | TestClient/HTML evidence; interactive explorers require JavaScript |
| Terminal access | Plain snapshot without input, ANSI or alternate screen; typed headless events and explicit terminal restoration | Separate [TUI evidence](tui.md); physical terminal screen readers pending |

The basic/pretty/experimental HTTP profiles share these semantics. Profile selection
does not authorize additional effects. Generated starter applications own their HTML
and must re-evaluate accessibility after modifying it.

## Reproducible repository checks

```bash
uv run pytest tests/feature/test_web_accessibility.py tests/feature/test_api_explorer.py \
  tests/feature/test_http.py tests/feature/test_locale_http.py
node --test tests/js/test_explorer.cjs
```

These exercise actual task persistence through HTTP, REST-generated and GraphQL-generated
request shapes, escaped schema/output text, explicit dispatch, typed path handling,
redirect refusal, no automatic retry, cancellation uncertainty, one-main/skip-link
markup, invalid-form preservation and malformed CSRF/origin rejection. Node tests use
a deliberate DOM double; they do not substitute for real browser rendering or an
automated accessibility engine.

Before declaring WCAG conformance, record candidate-bound manual results for keyboard-only
CRUD and error recovery, screen-reader names/status/error reading, contrast in light/dark
and forced-colors modes, reduced motion, zoom/reflow, RTL, touch targets, and destructive
action recovery. Include the exact browser, OS, assistive technology, viewport/settings,
tester, findings and remediation evidence. Independent reviewers and affected users must
approve that evidence; a green repository test suite cannot provide that approval.
