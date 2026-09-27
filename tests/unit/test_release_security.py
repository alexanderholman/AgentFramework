from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / "scripts"))


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def lockfile(path):
    path.write_text(
        """version = 1
[[package]]
name = "af-agent-framework"
version = "0.1.0a0"
source = { editable = "." }
[[package]]
name = "example"
version = "1.2.3"
source = { registry = "https://pypi.org/simple" }
wheels = [{url = "https://example.org/example.whl", hash = "sha256:"""
        + "a" * 64
        + """"}]
"""
    )
    return path


def test_sbom_pins_all_locked_packages_and_hashes(tmp_path):
    inventory = load("release_inventory")
    lock = lockfile(tmp_path / "uv.lock")
    bom = inventory.build_sbom(lock)
    assert bom["bomFormat"] == "CycloneDX"
    assert bom["specVersion"] == "1.6"
    assert bom["components"][0]["purl"] == "pkg:pypi/example@1.2.3"
    assert bom["components"][0]["externalReferences"][0]["hashes"][0]["content"] == "a" * 64
    assert bom["metadata"]["properties"][0]["value"] == inventory.sha256_file(lock)
    assert "installed" not in json.dumps(bom)
    assert inventory.audit_requirements(lock) == "example==1.2.3 --hash=sha256:" + "a" * 64 + "\n"


@pytest.mark.parametrize(
    "change", ["missing_hash", "url_credentials", "local_dependency", "duplicate"]
)
def test_inventory_rejects_unpinned_or_ambiguous_dependencies(tmp_path, change):
    inventory = load("release_inventory")
    lock = lockfile(tmp_path / "uv.lock")
    raw = lock.read_text()
    if change == "missing_hash":
        raw = raw.replace("sha256:" + "a" * 64, "sha256:wrong")
    elif change == "url_credentials":
        raw = raw.replace("https://example.org/", "https://user:password@example.org/")
    elif change == "local_dependency":
        raw = raw.replace('registry = "https://pypi.org/simple"', 'path = "../secret"')
    else:
        raw += raw[raw.index('[[package]]\nname = "example"') :]
    lock.write_text(raw)
    with pytest.raises(ValueError):
        inventory.build_sbom(lock)


def test_signing_binds_key_identity_content_expiry_and_public_key(tmp_path):
    signing = load("release_signing")
    private = Ed25519PrivateKey.generate()
    key = tmp_path / "private.pem"
    key.write_bytes(
        private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    key.chmod(0o600)
    public = tmp_path / "public.pem"
    public.write_bytes(
        private.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )
    artifacts = {"wheel": tmp_path / "candidate.whl", "sdist": tmp_path / "candidate.tar.gz"}
    for path in artifacts.values():
        path.write_bytes(b"candidate")
    now = datetime(2026, 1, 1, tzinfo=UTC)
    statement = signing.create_statement("a" * 40, artifacts, {}, now=now, lifetime_hours=24)
    envelope = signing.sign_statement(statement, key)
    signing.verify_statement(envelope, public, artifacts, {}, expected_commit="a" * 40, now=now)
    with pytest.raises(ValueError, match="expiry"):
        signing.verify_statement(
            envelope, public, artifacts, {}, expected_commit="a" * 40, now=now + timedelta(days=2)
        )
    artifacts["wheel"].write_bytes(b"changed")
    with pytest.raises(ValueError, match="digest"):
        signing.verify_statement(envelope, public, artifacts, {}, expected_commit="a" * 40, now=now)
    envelope["statement"]["source_commit"] = "b" * 40
    with pytest.raises(ValueError, match="signature"):
        signing.verify_statement(envelope, public, artifacts, {}, expected_commit="a" * 40, now=now)


def test_signing_rejects_public_or_symlink_keys(tmp_path):
    signing = load("release_signing")
    key = tmp_path / "key.pem"
    key.write_bytes(b"not-a-key")
    key.chmod(0o644)
    with pytest.raises(ValueError, match="private"):
        signing.sign_statement({}, key)
    link = tmp_path / "link.pem"
    link.symlink_to(key)
    with pytest.raises((OSError, ValueError)):
        signing.sign_statement({}, link)


def test_scan_report_never_treats_missing_or_skipped_dependencies_as_clean():
    scan = load("release_security")
    assert scan.audit_status(
        {"dependencies": [{"name": "example", "version": "1", "vulns": []}]}, {("example", "1")}
    ) == ("pass", 0)
    assert scan.audit_status({"dependencies": []}, {("example", "1")})[0] == "error"
    assert (
        scan.audit_status(
            {"dependencies": [{"name": "example", "skip_reason": "bad"}]}, {("example", "1")}
        )[0]
        == "error"
    )
    assert scan.audit_status(
        {"dependencies": [{"name": "example", "version": "1", "vulns": [{"id": "CVE-1"}]}]},
        {("example", "1")},
    ) == ("fail", 1)


def test_source_identity_rejects_dirty_and_untracked(tmp_path):
    inventory = load("release_inventory")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "fixture@example.test"], check=True
    )
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Fixture"], check=True)
    (tmp_path / "source").write_text("a")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "fixture"], check=True)
    assert len(inventory.source_commit(tmp_path)) == 40
    (tmp_path / "untracked").write_text("b")
    with pytest.raises(ValueError, match="clean"):
        inventory.source_commit(tmp_path)


def init_repository(path):
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    for key, value in [("user.email", "fixture@example.test"), ("user.name", "Fixture")]:
        subprocess.run(["git", "-C", str(path), "config", key, value], check=True)
    return path


def commit_repository(path):
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "fixture"], check=True)


def test_git_snapshot_uses_committed_content_including_export_ignored_files(tmp_path):
    inventory = load("release_inventory")
    root = init_repository(tmp_path / "repo")
    (root / ".gitattributes").write_text("sensitive export-ignore\n")
    (root / "sensitive").write_text("must still be scanned")
    commit_repository(root)
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    inventory.source_snapshot(root, snapshot, inventory.source_commit(root))
    assert (snapshot / "sensitive").read_text() == "must still be scanned"
    (root / "link").symlink_to("/etc/passwd")
    commit_repository(root)
    with pytest.raises(ValueError, match="links"):
        inventory.source_snapshot(root, tmp_path / "snapshot2", inventory.source_commit(root))


def test_json_outputs_are_private_no_clobber_and_duplicate_inputs_fail(tmp_path):
    inventory = load("release_inventory")
    path = tmp_path / "result.json"
    inventory.write_json(path, {"status": "pass"})
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        inventory.write_json(path, {"status": "different"})
    assert inventory.read_json(path) == {"status": "pass"}
    path.write_text('{"status":"pass", "status":"fail"}')
    with pytest.raises(ValueError, match="duplicate"):
        inventory.read_json(path)
    path.write_text('{"bad":NaN}')
    with pytest.raises(ValueError):
        inventory.read_json(path)
    with pytest.raises(ValueError, match="bounded"):
        inventory.read_regular(path, limit=1)


def test_real_json_subprocess_does_not_echo_scanner_secrets(tmp_path, capsys):
    scan = load("release_security")
    code, result = scan.run_json_tool(
        [sys.executable, "-I", "-c", 'import json; print(json.dumps({"ok":True}))'],
        tmp_path,
        tmp_path,
    )
    assert code == 0 and result == {"ok": True}
    with pytest.raises(ValueError):
        scan.run_json_tool(
            [
                sys.executable,
                "-I",
                "-c",
                'import sys; print("PRIVATE", file=sys.stderr); print("invalid")',
            ],
            tmp_path,
            tmp_path,
        )
    assert "PRIVATE" not in capsys.readouterr().out
    assert not (tmp_path / "tool-result.json").exists()


def test_scan_adapters_are_pinned_redacted_and_fail_closed(tmp_path):
    scan = load("release_security")
    lockfile(tmp_path / "uv.lock")
    calls = []

    def runner(argv, working, temporary):
        calls.append(argv)
        if "bandit" in argv:
            return 1, {
                "errors": [],
                "results": [
                    {
                        "test_id": "B123",
                        "issue_severity": "HIGH",
                        "issue_confidence": "HIGH",
                        "filename": "src/a.py",
                        "line_number": 1,
                        "code": "PRIVATE",
                    }
                ],
            }
        if "pip_audit" in argv:
            return 1, {
                "dependencies": [
                    {
                        "name": "example",
                        "version": "1.2.3",
                        "vulns": [
                            {"id": "CVE-FIXTURE", "fix_versions": ["2"], "description": "PRIVATE"}
                        ],
                    }
                ]
            }
        return 0, {
            "version": "1.5.0",
            "results": {
                "src/a.py": [
                    {
                        "hashed_secret": "a" * 40,
                        "line_number": 1,
                        "type": "fixture",
                        "secret": "PRIVATE",
                    }
                ]
            },
        }

    checks = scan.scan_snapshot(
        tmp_path,
        tmp_path,
        allow_advisory_network=True,
        runner=runner,
        version=scan.PINS.__getitem__,
    )
    assert all(result["status"] == "fail" for result in checks.values())
    assert "PRIVATE" not in json.dumps(checks)
    assert "--no-verify" in calls[1]
    assert "--disable-pip" in calls[2] and "--require-hashes" in calls[2]
    calls.clear()
    missing = scan.scan_snapshot(
        tmp_path,
        tmp_path,
        allow_advisory_network=False,
        runner=runner,
        version=lambda name: "wrong",
    )
    assert not calls
    assert missing["pip-audit"]["status"] == "unknown"
    assert missing["bandit"]["status"] == "error"

    def broken(*args):
        raise subprocess.TimeoutExpired("fixture", 1)

    unavailable = scan.scan_snapshot(
        tmp_path,
        tmp_path,
        allow_advisory_network=True,
        runner=broken,
        version=scan.PINS.__getitem__,
    )
    assert all(result["status"] == "error" for result in unavailable.values())


@pytest.mark.parametrize(
    "tool,code,document",
    [
        ("bandit", 0, {"errors": ["skipped file"], "results": []}),
        ("bandit", 1, {"errors": [], "results": []}),
        ("bandit", 0, {"errors": [], "results": [{}]}),
        ("detect-secrets", 0, {"version": "1.5.0", "results": {"a": {}}}),
        ("detect-secrets", 0, {"version": "1.5.0", "results": {"a": [{}]}}),
    ],
)
def test_malformed_scanner_results_never_pass(tool, code, document):
    scan = load("release_security")
    with pytest.raises(ValueError):
        scan._source_results(tool, document, code)


def test_policy_is_exact_key_bound_expiring_and_revocable():
    signing = load("release_signing")
    now = datetime(2026, 1, 1, tzinfo=UTC)
    policy = {
        "schema": "af-signing-key-policy/v1",
        "key_id": "a" * 64,
        "valid_from": (now - timedelta(hours=1)).isoformat(),
        "valid_until": (now + timedelta(hours=1)).isoformat(),
        "revoked": False,
        "owner": "fixture only",
        "review_reference": "fixture not an approval",
    }
    signing.validate_key_policy(policy, "a" * 64, now=now)
    for changed in [
        dict(policy, revoked=True),
        dict(policy, key_id="b" * 64),
        dict(policy, valid_until=now.isoformat()),
        dict(policy, owner=""),
    ]:
        with pytest.raises(ValueError):
            signing.validate_key_policy(changed, "a" * 64, now=now)


def test_inventory_cli_preview_then_exclusive_write(tmp_path, capsys):
    inventory = load("release_inventory")
    lock = lockfile(tmp_path / "uv.lock")
    out = tmp_path / "sbom.json"
    args = ["--lock", str(lock), "--output", str(out)]
    assert inventory.main(args) == 0 and not out.exists()
    assert inventory.main([*args, "--execute"]) == 0
    assert inventory.main([*args, "--execute"]) == 2
    assert inventory.read_json(out)["components"][0]["version"] == "1.2.3"
    assert "certifies_production" in capsys.readouterr().out


def test_security_cli_records_unknown_and_blocks_without_network(tmp_path, monkeypatch, capsys):
    scan = load("release_security")
    root = init_repository(tmp_path / "repo")
    lockfile(root / "uv.lock")
    commit_repository(root)
    output = tmp_path / "scan.json"
    monkeypatch.setattr(
        scan,
        "scan_snapshot",
        lambda *a, **kw: {
            "bandit": {"status": "pass"},
            "detect-secrets": {"status": "pass"},
            "pip-audit": {"status": "unknown"},
        },
    )
    args = ["--root", str(root), "--output", str(output)]
    assert scan.main(args) == 0 and not output.exists()
    assert scan.main([*args, "--execute"]) == 1
    report = json.loads(output.read_text())
    assert report["status"] == "blocked" and report["certifies_production"] is False
    assert scan.main(["--root", str(root), "--output", str(root / "bad"), "--execute"]) == 2
    assert "UNKNOWN-SECRETS" not in capsys.readouterr().out


def test_installed_scanners_have_real_offline_safe_and_finding_paths(tmp_path):
    scan = load("release_security")
    try:
        installed = {
            key: scan.importlib.metadata.version(key) for key in ("bandit", "detect-secrets")
        }
    except scan.importlib.metadata.PackageNotFoundError:
        pytest.skip("optional security extra not installed")
    if installed != {key: scan.PINS[key] for key in installed}:
        pytest.skip("optional security tools differ from pins")
    source = tmp_path / "source"
    source.mkdir()
    (source / "src").mkdir()
    (source / "scripts").mkdir()
    (source / "src" / "safe.py").write_text("def answer():\n    return 42\n")
    good = scan.scan_snapshot(source, tmp_path, allow_advisory_network=False)
    assert good["bandit"]["status"] == "pass" and good["detect-secrets"]["status"] == "pass"
    # Artificial fixture marker, never a real credential. Build fragments so the
    # repository source itself does not look like a private key.
    marker = "-----BEGIN " + "RSA PRIVATE KEY-----"
    (source / "fixture.txt").write_text(marker + "\nfixture-only\n")
    (source / "src" / "unsafe.py").write_text("import pickle\npickle.loads(input())\n")
    bad = scan.scan_snapshot(source, tmp_path, allow_advisory_network=False)
    assert bad["detect-secrets"]["status"] == "fail"
    assert marker not in json.dumps(bad)


def test_signing_cli_complete_fixture_and_denials(tmp_path, capsys):
    signing = load("release_signing")
    inventory = load("release_inventory")
    root = init_repository(tmp_path / "repo")
    lockfile(root / "uv.lock")
    (root / "pyproject.toml").write_text('[project]\nname="fixture"\n')
    commit_repository(root)
    commit = inventory.source_commit(root)
    private = Ed25519PrivateKey.generate()
    key = tmp_path / "private.pem"
    public = tmp_path / "public.pem"
    key.write_bytes(
        private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    key.chmod(0o600)
    public.write_bytes(
        private.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )
    now = datetime.now(UTC)
    policy = {
        "schema": "af-signing-key-policy/v1",
        "key_id": signing.hashlib.sha256(private.public_key().public_bytes_raw()).hexdigest(),
        "valid_from": (now - timedelta(days=1)).isoformat(),
        "valid_until": (now + timedelta(days=1)).isoformat(),
        "revoked": False,
        "owner": "test fixture",
        "review_reference": "not an actual release approval",
    }
    scan = {
        "schema": "af-security-scan/v1",
        "source_commit": commit,
        "source_tree": "clean",
        "lock_sha256": inventory.sha256_file(root / "uv.lock"),
        "observed_at": now.isoformat(),
        "status": "pass",
        "tools": signing.PINS,
        "certifies_production": False,
        "checks": {key: {"status": "pass"} for key in signing.PINS},
    }
    files = {
        name: tmp_path / (name + ".json")
        for name in ["sdist", "wheel", "sbom", "scan", "profile", "configuration", "key-policy"]
    }
    for name, path in files.items():
        if name == "sbom":
            value = inventory.build_sbom(root / "uv.lock")
        elif name == "scan":
            value = scan
        elif name == "key-policy":
            value = policy
        else:
            value = {"fixture": True}
        inventory.write_json(path, value)
    base = ["--root", str(root), "--envelope", str(tmp_path / "envelope.json")]
    for name, path in files.items():
        base.extend(["--" + name, str(path)])
    sign = ["sign", *base, "--key", str(key)]
    assert signing.main(sign) == 0 and not (tmp_path / "envelope.json").exists()
    assert signing.main([*sign, "--execute"]) == 0
    verify = ["verify", *base, "--key", str(public)]
    assert signing.main(verify) == 0
    # Required pinned scan freshness and status cannot be copied from old evidence.
    with pytest.raises(ValueError, match="passing"):
        signing.validate_scan(dict(scan, status="blocked"), commit, scan["lock_sha256"])
    with pytest.raises(ValueError, match="fresh"):
        signing.validate_scan(
            dict(scan, observed_at=(now - timedelta(days=2)).isoformat()),
            commit,
            scan["lock_sha256"],
        )
    policy["revoked"] = True
    files["key-policy"].write_text(json.dumps(policy))
    assert signing.main(verify) == 2
    assert "BEGIN" not in capsys.readouterr().out


@pytest.mark.parametrize(
    "kind",
    ["name", "version", "no_artifacts", "no_project", "registry", "format", "duplicate_project"],
)
def test_inventory_rejects_malformed_lock_shapes(tmp_path, kind):
    inventory = load("release_inventory")
    lock = lockfile(tmp_path / "uv.lock")
    raw = lock.read_text()
    if kind == "name":
        raw = raw.replace('name = "example"', 'name = "../secret"')
    elif kind == "version":
        raw = raw.replace('version = "1.2.3"', 'version = "1; bad"')
    elif kind == "no_artifacts":
        raw = raw[: raw.index("wheels =")]
    elif kind == "no_project":
        raw = raw.replace(
            'source = { editable = "." }', 'source = { registry = "https://pypi.org/simple" }'
        )
    elif kind == "registry":
        raw = raw.replace("https://pypi.org/simple", "http://pypi.org/simple")
    elif kind == "format":
        raw = raw.replace("version = 1\n", "version = 4\n")
    else:
        raw = (
            raw.replace('[[package]]\nname = "example"', '[[package]]\nname = "af-agent-framework"')
            .replace('version = "1.2.3"', 'version = "2.0.0"')
            .replace(
                'source = { registry = "https://pypi.org/simple" }', 'source = { editable = "." }'
            )
        )
    lock.write_text(raw)
    with pytest.raises(ValueError):
        inventory.build_sbom(lock)


def test_output_fsync_failure_removes_only_owned_file(tmp_path, monkeypatch):
    inventory = load("release_inventory")
    target = tmp_path / "output"

    def fail(descriptor):
        raise OSError("disk full")

    monkeypatch.setattr(inventory.os, "fsync", fail)
    with pytest.raises(OSError):
        inventory.write_json(target, {"valid": True})
    assert not target.exists()

    def replace(descriptor):
        target.unlink()
        target.write_text("replacement")
        raise OSError("disk full")

    monkeypatch.setattr(inventory.os, "fsync", replace)
    with pytest.raises(OSError):
        inventory.write_json(target, {"valid": True})
    assert target.read_text() == "replacement"


def test_signature_denies_wrong_identity_schema_or_material_set(tmp_path):
    signing = load("release_signing")
    key = Ed25519PrivateKey.generate()
    other = Ed25519PrivateKey.generate()
    private = tmp_path / "key"
    private.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    private.chmod(0o600)
    public = tmp_path / "pub"
    public.write_bytes(
        key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )
    artifact = tmp_path / "wheel"
    artifact.write_bytes(b"data")
    files = {"wheel": artifact}
    for commit, lifetime, observed in [
        ("short", 24, datetime.now(UTC)),
        ("a" * 40, 0, datetime.now(UTC)),
        ("a" * 40, 24, datetime(2026, 1, 1)),
    ]:
        with pytest.raises(ValueError):
            signing.create_statement(commit, files, {}, now=observed, lifetime_hours=lifetime)
    with pytest.raises(ValueError):
        signing.file_identities({})
    with pytest.raises(ValueError):
        signing.file_identities({"one": artifact, "two": artifact})
    statement = signing.create_statement("a" * 40, files, {})
    envelope = signing.sign_statement(statement, private)
    with pytest.raises(ValueError, match="source"):
        signing.verify_statement(envelope, public, files, {}, expected_commit="b" * 40)
    with pytest.raises(ValueError, match="envelope"):
        signing.verify_statement(
            dict(envelope, extra=True), public, files, {}, expected_commit="a" * 40
        )
    altered = signing.sign_statement(dict(statement, certifies_production=True), private)
    with pytest.raises(ValueError, match="statement"):
        signing.verify_statement(altered, public, files, {}, expected_commit="a" * 40)
    public.write_bytes(
        other.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )
    with pytest.raises(ValueError, match="key"):
        signing.verify_statement(envelope, public, files, {}, expected_commit="a" * 40)


@pytest.mark.parametrize("change", ["none", "source", "expired", "extra", "duplicate"])
def test_secret_review_is_exact_expiring_and_retains_findings(tmp_path, change):
    import hashlib

    security = load("release_security")
    source = tmp_path / "fixture.py"
    source.write_text("synthetic fixture content\n")
    (tmp_path / "release").mkdir()
    now = datetime(2026, 9, 26, tzinfo=UTC)
    entry = {
        "filename": "fixture.py",
        "type": "Synthetic",
        "fingerprint": list(bytes.fromhex("a" * 40)),
        "line_sha256": list(hashlib.sha256(source.read_bytes().splitlines()[0]).digest()),
        "reason": "Synthetic non-production fixture reviewed explicitly.",
        "reviewer": "test-reviewer",
        "reviewed_at": (now - timedelta(hours=1)).isoformat(),
        "expires_at": (now + timedelta(days=30)).isoformat(),
    }
    if change == "source":
        source.write_text("changed unreviewed content\n")
    if change == "expired":
        entry["expires_at"] = (now - timedelta(minutes=1)).isoformat()
    if change == "extra":
        entry["filename"] = "unmatched.py"
    reviews = [entry, entry] if change == "duplicate" else [entry]
    (tmp_path / "release" / "secret-reviews.json").write_text(
        json.dumps(
            {
                "schema": "af-secret-reviews/v1",
                "reviews": reviews,
            }
        )
    )
    result = {
        "status": "fail",
        "count": 1,
        "findings": [
            {
                "filename": "fixture.py",
                "type": "Synthetic",
                "fingerprint": "a" * 40,
                "line_number": 1,
            }
        ],
    }
    if change != "none":
        with pytest.raises(ValueError):
            security.reviewed_secret_candidates(tmp_path, result, now=now)
    else:
        reviewed = security.reviewed_secret_candidates(tmp_path, result, now=now)
        assert reviewed["status"] == "pass"
        assert reviewed["count"] == reviewed["reviewed"] == 1
        assert reviewed["unreviewed"] == 0
        assert reviewed["findings"][0]["review"]["reviewer"] == "test-reviewer"
