#!/usr/bin/env python3
"""Validate the static data under data/ (kept in sync with the fetch tool).

Used by CI (see .github/workflows/deploy-pages.yml) and runnable locally:

    python3 tools/check.py

Checks:
  - data/catalog.json exists and lists languages
  - every catalog language has a matching data/<key>.json (or data/all.json)
  - count == len(repos)
  - repos are sorted by stars descending
  - every repo has a summary and a pushed_at
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


def load(path: Path) -> dict:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def main() -> int:
    errors: list[str] = []
    catalog_path = DATA / "catalog.json"
    if not catalog_path.exists():
        print("missing data/catalog.json", file=sys.stderr)
        return 1

    catalog = load(catalog_path)
    langs = catalog.get("languages", [])
    if not langs:
        errors.append("catalog.languages is empty")

    total = 0
    for entry in langs:
        key = entry.get("key")
        label = entry.get("label", "")
        path = DATA / f"{key}.json"
        if not path.exists():
            errors.append(f"catalog lists {label} ({key}) but {path.name} is missing")
            continue
        doc = load(path)
        repos = doc.get("repos", [])
        if doc.get("count") != len(repos):
            errors.append(f"{path.name}: count={doc.get('count')} != repos={len(repos)}")
        if key == "all":
            total = len(repos)

        stars = [r.get("stars", 0) for r in repos]
        if stars != sorted(stars, reverse=True):
            errors.append(f"{path.name}: repos are not sorted by stars descending")

        for repo in repos:
            name = repo.get("full_name", "?")
            if not repo.get("summary"):
                errors.append(f"{path.name}: {name} has no summary")
                break
            if not repo.get("pushed_at"):
                errors.append(f"{path.name}: {name} has no pushed_at")
                break

    print(f"languages: {len(langs)} · all: {total} repos")
    if errors:
        for err in errors:
            print("x", err, file=sys.stderr)
        return 1
    print("data OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
