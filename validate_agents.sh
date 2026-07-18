#!/bin/bash
# Validation script for AgentFramework governance assets.

echo "======================================"
echo "AgentFramework Validation Suite"
echo "======================================"
echo ""

PASSED=0
FAILED=0
TOTAL=0

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

run_test() {
    local test_id=$1
    local test_name=$2
    shift 2

    TOTAL=$((TOTAL + 1))
    echo "Running ${test_id}: ${test_name}"

    if "$@"; then
        echo -e "${GREEN}✓ PASS${NC}: ${test_id}"
        PASSED=$((PASSED + 1))
    else
        echo -e "${RED}✗ FAIL${NC}: ${test_id}"
        FAILED=$((FAILED + 1))
    fi
    echo ""
}

warn_if_agents_dir_missing() {
    local agent_count
    agent_count=$(uv run python3 - <<'PY'
from pathlib import Path

import yaml

data = yaml.safe_load(Path("agents.yaml").read_text(encoding="utf-8")) or {}
agents = data.get("agents", []) if isinstance(data, dict) else []
print(sum(1 for item in agents if isinstance(item, dict) and item.get("id")))
PY
)
    if [ "$agent_count" -gt 0 ] && [ ! -d "agents" ]; then
        echo -e "${YELLOW}Warning: agents.yaml defines $agent_count agent(s) but agents/ directory does not exist.${NC}"
        echo "Create the directory with: mkdir -p agents"
        echo ""
    fi
}

check_agent_files_exist() {
    if [ ! -f "agents.yaml" ]; then
        return 0
    fi

    uv run python3 - <<'PY'
from pathlib import Path

import yaml

data = yaml.safe_load(Path("agents.yaml").read_text(encoding="utf-8")) or {}
scripts = data.get("agents", []) if isinstance(data, dict) else []
missing = []
for item in scripts:
    if not isinstance(item, dict):
        continue
    file_path = item.get("file_path")
    if isinstance(file_path, str) and file_path and not Path(file_path).is_file():
        missing.append(file_path)

if missing:
    for path in missing:
        print(f"Missing file: {path}")
    raise SystemExit(1)
PY
}

check_required_headings() {
    local file=$1
    local purpose inputs outputs behavior constraints

    purpose=$(grep -n '^## Purpose$' "$file" | cut -d: -f1)
    inputs=$(grep -n '^## Inputs$' "$file" | cut -d: -f1)
    outputs=$(grep -n '^## Outputs$' "$file" | cut -d: -f1)
    behavior=$(grep -n '^## Behavior$' "$file" | cut -d: -f1)
    constraints=$(grep -n '^## Constraints$' "$file" | cut -d: -f1)

    if [ -z "$purpose" ] || [ -z "$inputs" ] || [ -z "$outputs" ] || [ -z "$behavior" ] || [ -z "$constraints" ]; then
        return 1
    fi

    [ "$purpose" -lt "$inputs" ] &&
        [ "$inputs" -lt "$outputs" ] &&
        [ "$outputs" -lt "$behavior" ] &&
        [ "$behavior" -lt "$constraints" ]
}

check_h2_heading_format() {
    local file=$1
    grep -Eq '^## (Purpose|Inputs|Outputs|Behavior|Constraints)$' "$file"
}

file_exists() {
    [ -f "$1" ]
}

run_uv_python() {
    uv run "$@"
}

if [ -f "agents.yaml" ]; then
    warn_if_agents_dir_missing
fi

echo "=== SPEC-007: Flexible Directory Structure ==="
if [ -d "agents" ]; then
    run_test "TEST-007-1" \
        "Verify all agent files referenced in agents.yaml exist" \
        check_agent_files_exist
else
    echo "Note: agents/ directory does not exist yet - skipping TEST-007-1"
    echo ""
fi

echo "=== SPEC-002: Agent File Format ==="
if [ -d "agents" ] && [ "$(find agents/ -name '*.md' -type f 2>/dev/null | wc -l)" -gt 0 ]; then
    while IFS= read -r file; do
        [ -f "$file" ] || continue
        run_test "TEST-002-1" \
            "Verify required headings in $(basename "$file")" \
            check_required_headings "$file"
    done < <(find agents/ -name '*.md' -type f | sort)

    while IFS= read -r file; do
        [ -f "$file" ] || continue
        run_test "TEST-002-2" \
            "Verify H2 heading format in $(basename "$file")" \
            check_h2_heading_format "$file"
    done < <(find agents/ -name '*.md' -type f | sort)
else
    echo "Note: No agent markdown files found yet - skipping TEST-002-1 and TEST-002-2"
    echo ""
fi

echo "=== SPEC-003: Tags and Metadata ==="
if [ -f "agents.yaml" ]; then
    run_test "TEST-003-1" \
        "Verify each agent has at least one tag" \
        run_uv_python python3 scripts/validate_agents_yaml.py --check-tags-present --file agents.yaml

    run_test "TEST-003-2" \
        "Verify agent IDs are unique in agents.yaml" \
        run_uv_python python3 scripts/validate_agents_yaml.py --check-unique-ids --file agents.yaml

    if grep -q 'allowed_tags:' agents.yaml; then
        run_test "TEST-003-3" \
            "Verify all tags are from allowed list" \
            run_uv_python python3 scripts/validate_agents_yaml.py --check-tags-allowed --file agents.yaml
    else
        echo "Note: No allowed_tags defined in agents.yaml - skipping TEST-003-3"
        echo ""
    fi
else
    echo "Error: agents.yaml not found"
    FAILED=$((FAILED + 3))
    TOTAL=$((TOTAL + 3))
    echo ""
fi

echo "=== SPEC-004: Append-Only Files ==="
run_test "TEST-004-1" \
    "Verify append-only files are pure appends relative to the resolved base revision" \
    run_uv_python python3 scripts/check_append_only.py --root .

echo "=== SPEC-005: No Fabrication ==="
echo "Note: TEST-005-1 (Citation verification) requires manual review"
echo ""

echo "=== SPEC-006: Markdown Output ==="
run_test "TEST-006-1" \
    "Verify key documentation files use .md extension" \
    file_exists agents.md
run_test "TEST-006-2" "Verify specs.md exists" file_exists specs.md
run_test "TEST-006-3" "Verify agent_runs.md exists" file_exists agent_runs.md
run_test "TEST-006-4" "Verify decisions.md exists" file_exists decisions.md

echo "=== SPEC-008: Script-First Automation ==="
if [ -f "scripts/check_repeatable_script_refs.py" ]; then
    run_test "TEST-008-1" \
        "Verify repeatable task docs include script references" \
        run_uv_python python3 scripts/check_repeatable_script_refs.py --root .
else
    echo "Note: scripts/check_repeatable_script_refs.py not found - skipping TEST-008-1"
    echo ""
fi

if [ -f "scripts/validate_registry.py" ]; then
    run_test "TEST-008-2" \
        "Validate scripts registry schema" \
        run_uv_python python3 scripts/validate_registry.py --registry scripts/registry.yaml
else
    echo "Note: scripts/validate_registry.py not found - skipping TEST-008-2"
    echo ""
fi

if [ -f "scripts/check_dry_run_conventions.py" ]; then
    run_test "TEST-008-3" \
        "Verify execute-intent scripts use dry-run gating" \
        run_uv_python python3 scripts/check_dry_run_conventions.py --registry scripts/registry.yaml
else
    echo "Note: scripts/check_dry_run_conventions.py not found - skipping TEST-008-3"
    echo ""
fi

echo "======================================"
echo "Validation Summary"
echo "======================================"

if [ "$TOTAL" -eq 0 ]; then
    echo -e "${YELLOW}Warning: No tests were executed!${NC}"
    exit 1
fi

echo "Total tests run: $TOTAL"
echo -e "${GREEN}Passed: $PASSED${NC}"
if [ "$FAILED" -gt 0 ]; then
    echo -e "${RED}Failed: $FAILED${NC}"
    echo ""
    echo "Please fix the failing tests before proceeding."
    exit 1
fi

echo -e "${GREEN}All tests passed!${NC}"
exit 0
