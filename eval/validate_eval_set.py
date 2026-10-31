#!/usr/bin/env python3
"""Validate eval/questions.yaml and report coverage.

  python eval/validate_eval_set.py                  # structure, coverage, TODOs
  python eval/validate_eval_set.py --run-sql        # also execute every gold_sql
  python eval/validate_eval_set.py --run-sql --show # print the result rows too
  python eval/validate_eval_set.py --run-sql --allow-empty
                                                    # CI: queries must run, but may return
                                                    # no rows against the small fixture

--run-sql uses READONLY_DATABASE_URL: gold queries get exactly the same
permissions and timeout as LLM-generated queries, so a gold query that only
works as admin is caught here.

Exit code 1 if there are errors (warnings and TODOs do not fail), so it can
run in CI.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from collections import Counter
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FILE = ROOT / "eval" / "questions.yaml"

ROUTES = {"sql", "docs", "both", "none"}
BEHAVIOURS = {"answer", "clarify", "refuse"}
SPLITS = {"dev", "test"}
DIFFICULTIES = {"easy", "medium", "hard"}
REQUIRED = ["id", "question", "route", "behaviour", "split", "expected_answer", "tags", "difficulty"]

# Statements a gold query must never contain. The read-only role blocks
# these anyway; this check gives a clearer error.
FORBIDDEN_SQL = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|copy|vacuum|call|do)\b", re.I
)


def is_todo(value) -> bool:
    if value is None:
        return False
    if isinstance(value, list):
        return any(is_todo(v) for v in value)
    return str(value).strip().upper().startswith("TODO")


def check_question(q: dict, errors: list[str], warnings: list[str], todos: list[str]) -> None:
    qid = q.get("id", "<missing id>")

    for field in REQUIRED:
        if field not in q:
            errors.append(f"{qid}: missing field '{field}'")

    route, behaviour = q.get("route"), q.get("behaviour")
    if route not in ROUTES:
        errors.append(f"{qid}: route must be one of {sorted(ROUTES)}, got {route!r}")
    if behaviour not in BEHAVIOURS:
        errors.append(f"{qid}: behaviour must be one of {sorted(BEHAVIOURS)}, got {behaviour!r}")
    if q.get("split") not in SPLITS:
        errors.append(f"{qid}: split must be one of {sorted(SPLITS)}")
    if q.get("difficulty") not in DIFFICULTIES:
        errors.append(f"{qid}: difficulty must be one of {sorted(DIFFICULTIES)}")
    if not isinstance(q.get("tags", []), list):
        errors.append(f"{qid}: tags must be a list")

    if route in ROUTES and not str(qid).startswith(f"{route}-"):
        errors.append(f"{qid}: id prefix should be '{route}-' to match route")

    answering = behaviour == "answer"
    if route in ("sql", "both") and answering and not q.get("gold_sql"):
        errors.append(f"{qid}: route '{route}' with behaviour 'answer' needs gold_sql")
    if route in ("docs", "both") and answering and not q.get("gold_doc_ids"):
        errors.append(f"{qid}: route '{route}' with behaviour 'answer' needs gold_doc_ids")
    if route == "none" and behaviour == "answer":
        errors.append(f"{qid}: route 'none' should have behaviour 'refuse' or 'clarify'")

    sql = q.get("gold_sql")
    if sql and FORBIDDEN_SQL.search(sql):
        errors.append(f"{qid}: gold_sql contains a write or DDL keyword")
    if sql and not re.match(r"^\s*(select|with)\b", sql, re.I):
        errors.append(f"{qid}: gold_sql must start with SELECT or WITH")

    for field in ("expected_answer", "gold_doc_ids"):
        if is_todo(q.get(field)):
            todos.append(f"{qid}: {field}")

    if answering and not q.get("must_include") and not is_todo(q.get("expected_answer")):
        warnings.append(f"{qid}: must_include is empty; automatic answer checks will have nothing to match")


def run_gold_sql(
    questions: list[dict], dsn: str, show: bool, allow_empty: bool, errors: list[str], warnings: list[str]
) -> None:
    import psycopg

    print("\nRunning gold SQL as the read-only role")
    with psycopg.connect(dsn, autocommit=True) as conn:
        for q in questions:
            sql = q.get("gold_sql")
            if not sql:
                continue
            try:
                cur = conn.execute(sql)
                rows = cur.fetchall()
                cols = [d.name for d in cur.description]
            except Exception as exc:
                msg = str(exc).strip().splitlines()[0]
                errors.append(f"{q['id']}: gold_sql failed: {msg}")
                print(f"  FAIL {q['id']}: {msg}")
                continue
            status = "OK  " if rows else "EMPTY"
            print(f"  {status} {q['id']}: {len(rows)} row(s)")
            if not rows:
                msg = f"{q['id']}: gold_sql returned no rows; the answer cannot be grounded"
                (warnings if allow_empty else errors).append(msg)
            if show and rows:
                print("       " + " | ".join(cols))
                for r in rows[:5]:
                    print("       " + " | ".join("" if v is None else str(v) for v in r))
                if len(rows) > 5:
                    print(f"       ... {len(rows) - 5} more")


def main(argv: list[str] | None = None) -> int:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")

    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("file", nargs="?", type=Path, default=DEFAULT_FILE)
    p.add_argument("--run-sql", action="store_true", help="execute each gold_sql as the read-only role")
    p.add_argument("--show", action="store_true", help="with --run-sql, print the first rows of each result")
    p.add_argument("--allow-empty", action="store_true",
                   help="with --run-sql, treat empty results as warnings (for the small CI fixture)")
    p.add_argument("--dsn", default=os.getenv("READONLY_DATABASE_URL"))
    args = p.parse_args(argv)

    data = yaml.safe_load(args.file.read_text(encoding="utf-8"))
    questions = data.get("questions") or []
    targets = data.get("targets", {})

    errors: list[str] = []
    warnings: list[str] = []
    todos: list[str] = []

    dupes = [i for i, n in Counter(q.get("id") for q in questions).items() if n > 1]
    for d in dupes:
        errors.append(f"duplicate id: {d}")
    dupe_qs = [t for t, n in Counter(q.get("question", "").strip().lower() for q in questions).items() if n > 1]
    for d in dupe_qs:
        warnings.append(f"duplicate question text: {d!r}")

    for q in questions:
        check_question(q, errors, warnings, todos)

    # ------------------------------------------------------------ coverage
    by_route = Counter(q.get("route") for q in questions)
    by_behaviour = Counter(q.get("behaviour") for q in questions)
    by_split = Counter(q.get("split") for q in questions)
    tags = Counter(t for q in questions for t in q.get("tags", []) or [])

    shown = args.file.relative_to(ROOT) if args.file.is_relative_to(ROOT) else args.file
    print(f"{shown}: {len(questions)} questions\n")
    print("Route coverage (have / target)")
    for route, target in targets.get("route", {}).items():
        have = by_route.get(route, 0)
        bar = "#" * min(have, 30)
        print(f"  {route:<6}{have:>3} / {target:<3} {bar}")
    print("\nBehaviour: " + ", ".join(f"{b} {by_behaviour.get(b, 0)}" for b in sorted(BEHAVIOURS)))

    total = len(questions) or 1
    test_share = by_split.get("test", 0) / total
    print(f"Split: dev {by_split.get('dev', 0)}, test {by_split.get('test', 0)} ({test_share:.0%} test)")
    want = targets.get("test_share")
    if want is not None and abs(test_share - want) > 0.1 and len(questions) >= 20:
        warnings.append(f"test split is {test_share:.0%}, target {want:.0%}")
    print("Top tags: " + ", ".join(f"{t} ({n})" for t, n in tags.most_common(8)))

    if args.run_sql:
        if not args.dsn:
            errors.append("--run-sql needs READONLY_DATABASE_URL in .env or --dsn")
        else:
            run_gold_sql(questions, args.dsn, args.show, args.allow_empty, errors, warnings)

    # ------------------------------------------------------------ report
    if todos:
        print(f"\nTODO ({len(todos)} fields still to fill)")
        for t in todos:
            print(f"  - {t}")
    if warnings:
        print(f"\nWarnings ({len(warnings)})")
        for w in warnings:
            print(f"  - {w}")
    if errors:
        print(f"\nErrors ({len(errors)})")
        for e in errors:
            print(f"  - {e}")
        return 1
    print("\nNo errors.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
