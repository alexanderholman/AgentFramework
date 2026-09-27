# AF Constitution 1.0.0

The versioned baseline is `af.framework.constitution.CONSTITUTION`. Its canonical
representation has a SHA-256 digest. Host configuration can add restrictions; it
cannot override the baseline with an allow flag.

Precedence is Constitution → stricter host policy → human consent and peer lease →
provider, procedure, locale and peer configuration. A signed artifact, model
proposal, advertised capability or caller-supplied confirmation is not authority.

The principles are accessibility, inclusion, diversity, equity and equality,
privacy, consent, human agency, non-discrimination and failure closed. Presenting
a principle here is not a claim of independent ethical, security or accessibility
certification. The finite enforcement below is the implemented private v0.1 scope.

## Executable boundaries

- The ordinary Task pipeline checks every routed descriptor, including deterministic
  routes, before execution or cache reuse. It permits read-only, no-risk procedures
  with no confirmation requirement. Consequential procedures require a separate
  authorized service; the Task pipeline has no grant consumer for those effects.
- `HostRestrictions` can deny installed procedure IDs or lower cost/latency limits.
  It cannot grant a denied effect. Current-time observations remain available and
  are not cached as pure results.
- Classifiers receive task text without arbitrary metadata attributes. Policy,
  selection and routing run with principal claims and presentation locale removed.
  Opaque identity/tenant boundaries, trusted roles/scopes, cancellation and deadlines
  remain intact. Protected attributes are not accepted as resource-ranking signals.
- User-requested presentation accommodations are separate boolean preferences.
  Locale, territory, text size or motion preferences do not grant authority or
  alter formulas. Explicitly supplied text and typed operands remain task data:
  a request to count a word is valid regardless of the word's subject.
- Remote decisions, peer leases, human decision responses and local engineering
  retain their own exact consent, policy, identity, expiry and replay checks.
  Disabling a provider does not weaken deterministic execution or these checks.

Python extensions are trusted installed code. Projection removes ambient policy
signals at AF's extension boundaries; it is not a sandbox preventing malicious
Python from reading host resources. Extensions must not infer protected attributes
from task text or opaque identifiers and use them to select privileges or resources.

Executable evidence: `tests/unit/test_constitution.py`,
`tests/feature/test_constitution_journey.py`, pipeline selector/policy tests, and
the separate consent, peer, locale and authentication regressions.
