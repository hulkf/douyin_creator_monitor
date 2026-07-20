#!/usr/bin/env python3
"""Correct ASR transcripts with a domain glossary and propose new term candidates."""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path


DEFAULT_GLOSSARY_PATH = Path(__file__).resolve().parents[1] / "config" / "domain_glossary.json"


def load_glossary(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise SystemExit(f"Glossary must be a JSON object: {path}")
    return payload


def collect_replacements(glossary: dict, domain_ids: set[str] | None = None) -> dict[str, str]:
    replacements: dict[str, str] = {}
    for domain in glossary.get("domains", []):
        if not isinstance(domain, dict):
            continue
        if domain_ids and domain.get("id") not in domain_ids:
            continue
        for source, target in domain.get("replacements", {}).items():
            if isinstance(source, str) and isinstance(target, str):
                replacements[source] = target
    return replacements


def apply_replacements(text: str, replacements: dict[str, str]) -> tuple[str, list[dict]]:
    changes = []
    corrected = text
    for source in sorted(replacements, key=len, reverse=True):
        target = replacements[source]
        count = corrected.count(source)
        if count:
            corrected = corrected.replace(source, target)
            changes.append({"from": source, "to": target, "count": count})
    return corrected, changes


def collect_hotwords(glossary: dict) -> set[str]:
    terms: set[str] = set()
    for domain in glossary.get("domains", []):
        if not isinstance(domain, dict):
            continue
        for word in domain.get("hotwords", []):
            if isinstance(word, str) and word.strip():
                terms.add(word.strip())
    return terms


def extract_candidates(text: str, hotwords: set[str], max_items: int = 80) -> list[dict]:
    ascii_terms = Counter(re.findall(r"\b[A-Za-z][A-Za-z0-9.+#/-]{1,24}\b", text))
    chinese_terms = Counter(re.findall(r"[\u4e00-\u9fff]{2,8}", text))

    candidates: list[dict] = []
    for term, count in ascii_terms.most_common():
        if term in hotwords:
            continue
        if term.lower() in {"http", "https", "www", "com"}:
            continue
        candidates.append({"term": term, "count": count, "type": "latin_or_mixed"})

    for term, count in chinese_terms.most_common():
        if term in hotwords:
            continue
        if len(term) <= 1:
            continue
        candidates.append({"term": term, "count": count, "type": "chinese"})
        if len(candidates) >= max_items:
            break

    return candidates[:max_items]


def merge_candidates(glossary: dict, candidates: list[dict]) -> dict:
    existing = {
        item.get("term")
        for item in glossary.get("candidate_terms", [])
        if isinstance(item, dict)
    }
    target = glossary.setdefault("candidate_terms", [])
    for item in candidates:
        if item["term"] in existing:
            continue
        target.append(
            {
                "term": item["term"],
                "source": "correct_transcript",
                "status": "pending",
                "count": item["count"],
                "type": item["type"],
            }
        )
        existing.add(item["term"])
    return glossary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", help="Raw transcript text file.")
    parser.add_argument("--output", help="Where to write corrected transcript.")
    parser.add_argument("--report-output", help="Where to write correction report JSON.")
    parser.add_argument("--glossary", default=str(DEFAULT_GLOSSARY_PATH))
    parser.add_argument(
        "--domain",
        action="append",
        help="Limit replacements to a glossary domain id. Can be repeated.",
    )
    parser.add_argument(
        "--update-candidates",
        action="store_true",
        help="Append candidate terms into glossary candidate_terms.",
    )
    args = parser.parse_args()

    input_path = Path(args.input)
    glossary_path = Path(args.glossary)
    text = input_path.read_text(encoding="utf-8-sig")
    glossary = load_glossary(glossary_path)
    replacements = collect_replacements(glossary, set(args.domain) if args.domain else None)
    corrected, changes = apply_replacements(text, replacements)
    candidates = extract_candidates(corrected, collect_hotwords(glossary))

    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(corrected, encoding="utf-8")
    else:
        print(corrected)

    report = {
        "input": str(input_path),
        "glossary": str(glossary_path),
        "changes": changes,
        "candidate_terms": candidates,
    }
    if args.report_output:
        Path(args.report_output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report_output).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.update_candidates:
        merge_candidates(glossary, candidates)
        glossary_path.write_text(json.dumps(glossary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
