#!/usr/bin/env python3
"""Validate the static data under data/ (kept in sync with the fetch tool).

Used by CI (see .github/workflows/deploy-pages.yml) and runnable locally:

    python3 tools/check.py

Checks:
  - data/catalog.json exists and lists languages
  - every catalog language has a matching data/<key>.json (or data/all.json)
  - that file is an index (no repos) listing data/<key>.<n>.json chunks,
    each chunk present and count == len(chunk repos)
  - sum of chunk counts == index count
  - repos are sorted by stars descending across the chunks
  - every repo has a summary and a pushed_at
  - every repo has kind tags (tags/primary_tag) and domain tags (domains/primary_domain)
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
        if "repos" in doc:
            errors.append(f"{path.name}: still holds repos, expected an index of chunks")
            repos = doc.get("repos", [])
        else:
            repos = []
            for name in doc.get("chunks", []):
                chunk_path = DATA / name
                if not chunk_path.exists():
                    errors.append(f"{path.name}: chunk {name} is missing")
                    continue
                part = load(chunk_path)
                chunk_repos = part.get("repos", [])
                if part.get("count") != len(chunk_repos):
                    errors.append(
                        f"{name}: count={part.get('count')} != repos={len(chunk_repos)}"
                    )
                if len(chunk_repos) > doc.get("chunk_size", 300):
                    errors.append(f"{name}: {len(chunk_repos)} repos exceeds chunk_size")
                repos.extend(chunk_repos)
            if not doc.get("chunks"):
                errors.append(f"{path.name}: index lists no chunks")
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
            if not repo.get("tags") or repo.get("primary_tag") not in repo.get("tags", []):
                errors.append(f"{path.name}: {name} has bad kind tags")
                break
            if not repo.get("domains") or repo.get("primary_domain") not in repo.get("domains", []):
                errors.append(f"{path.name}: {name} has bad domain tags")
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
