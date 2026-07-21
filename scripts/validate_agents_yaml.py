#!/usr/bin/env python3

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import yaml


def parse_agents_yaml(path: str) -> tuple[list[dict[str, Any]], list[str]]:
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ValueError(f"failed to read {path}: {error.strerror}") from error
    except yaml.YAMLError as error:
        raise ValueError(f"failed to parse {path}: {error}") from error
    if not isinstance(data, dict):
        return [], []

    agents = data.get("agents", [])
    allowed_tags = data.get("allowed_tags", [])
    normalized_agents = [item for item in agents if isinstance(item, dict)]
    normalized_tags = [tag for tag in allowed_tags if isinstance(tag, str)]
    return normalized_agents, normalized_tags


def check_tags_present(path: str) -> int:
    agents, _ = parse_agents_yaml(path)
    missing = [agent.get("id", "<unknown>") for agent in agents if not agent.get("tags")]
    if missing:
        sys.stderr.write("Agents missing tags: " + ", ".join(str(item) for item in missing) + "\n")
        return 1
    return 0


def check_unique_ids(path: str) -> int:
    agents, _ = parse_agents_yaml(path)
    ids = [agent.get("id") for agent in agents if isinstance(agent.get("id"), str)]
    duplicates = sorted({agent_id for agent_id in ids if ids.count(agent_id) > 1})
    if duplicates:
        sys.stderr.write("Duplicate agent IDs: " + ", ".join(duplicates) + "\n")
        return 1
    return 0


def check_tags_allowed(path: str) -> int:
    agents, allowed_tags = parse_agents_yaml(path)
    if not allowed_tags:
        return 0
    allowed = set(allowed_tags)
    invalid: list[str] = []
    for agent in agents:
        tags = agent.get("tags", [])
        if not isinstance(tags, list):
            invalid.append(str(tags))
            continue
        for tag in tags:
            if tag not in allowed:
                invalid.append(str(tag))
    if invalid:
        sys.stderr.write("Tags not in allowed_tags: " + ", ".join(sorted(set(invalid))) + "\n")
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate agents.yaml fields.")
    parser.add_argument(
        "--file",
        default="agents.yaml",
        help="Path to agents.yaml (default: agents.yaml)",
    )
    parser.add_argument("--check-tags-present", action="store_true")
    parser.add_argument("--check-unique-ids", action="store_true")
    parser.add_argument("--check-tags-allowed", action="store_true")

    args = parser.parse_args()

    try:
        if args.check_tags_present:
            return check_tags_present(args.file)
        if args.check_unique_ids:
            return check_unique_ids(args.file)
        if args.check_tags_allowed:
            return check_tags_allowed(args.file)
    except ValueError as error:
        sys.stderr.write(f"{error}\n")
        return 1

    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
