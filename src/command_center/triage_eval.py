"""Offline triage eval: how many of a fixed set of representative items
land in the right lane.

    uv run python -m command_center.triage_eval            # the configured provider
    uv run python -m command_center.triage_eval --simulated  # no LLM at all

The cases (triage_eval_cases.json) are synthetic — no real mail — and
cover the noise the brief used to get wrong (job alerts, surveys, event
promos, a deadline that had already passed) alongside real work, invites,
calendar events and tasks. Each run goes through exactly the pull's code
path — rules → LLM → post-processing (pipeline._triage) — against a
throwaway database with only the built-in triage rules, so a model or
prompt change can be measured before it reaches the real brief. The
configured provider is called with the synthetic cases only; the real
database is never opened.

--simulated replaces the model with each case's scripted "careless"
answer — what the deterministic layers alone make of it.
"""

import argparse
import json
import re
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterator

from command_center import db, deadlines, pipeline, triage, triage_rules
from command_center.config import TZ
from command_center.sources import RawItem

CASES_PATH = Path(__file__).with_name("triage_eval_cases.json")
_RELATIVE_DATE = re.compile(r"\{today([+-]\d+)\}")


@dataclass
class Result:
    case_id: str
    expected: str
    got: str | None
    decided_by: str  # "rules" (before the model) or "model" (model + post-processing)
    missed_ok: bool = True  # an expected "deadline passed" was actually flagged
    known_gap: bool = False
    accept: tuple[str, ...] = ()  # other lanes that are just as right for this case

    @property
    def ok(self) -> bool:
        # "skip" = hidden from the brief by a rule, which shows up as no item.
        got = "skip" if self.got is None else self.got
        return (got == self.expected or got in self.accept) and self.missed_ok


def _resolve_dates(value, today):
    """{today-2} → the date two days before `today`, anywhere in the case."""
    if isinstance(value, str):
        return _RELATIVE_DATE.sub(lambda m: (today + timedelta(days=int(m.group(1)))).isoformat(), value)
    if isinstance(value, dict):
        return {k: _resolve_dates(v, today) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_dates(v, today) for v in value]
    return value


def load_cases(path: Path = CASES_PATH, today=None) -> list[dict]:
    today = today or datetime.now(TZ).date()
    with open(path, encoding="utf-8") as f:
        return [_resolve_dates(case, today) for case in json.load(f)["cases"]]


def _raw(case: dict) -> RawItem:
    return RawItem(
        source=case["source"], source_id=case["id"], title=case["title"],
        body=case.get("body", ""), metadata=dict(case.get("metadata", {})),
    )


def simulated_model(cases: list[dict]):
    """A stand-in model answering with each case's scripted `simulated`."""
    by_id = {case["id"]: case for case in cases}

    def model(raw_items: list[RawItem]) -> list[dict]:
        answers = []
        for raw in raw_items:
            scripted = by_id[raw.source_id].get("simulated", {})
            answers.append({
                "source": raw.source, "source_id": raw.source_id,
                "lane": scripted.get("lane", "action_items"), "priority": scripted.get("priority", 2),
                "why_it_matters": scripted.get("why_it_matters", ""),
                "suggested_next_step": scripted.get("suggested_next_step", ""),
                "deadline": scripted.get("deadline", ""),
            })
        return triage._map_results(raw_items, answers)

    return model


@contextmanager
def _scratch_db() -> Iterator[None]:
    """A temporary database holding only the built-in rules — the eval
    never sees (or changes) the user's own rules or data."""
    original = db.DB_PATH
    with tempfile.TemporaryDirectory() as tmp:
        db.DB_PATH = Path(tmp) / "eval.db"
        try:
            db.init_db()
            triage_rules.seed_builtin_rules()
            yield
        finally:
            db.DB_PATH = original


def run_eval(cases: list[dict], model=None) -> list[Result]:
    raw_items = [_raw(case) for case in cases]
    with _scratch_db():
        # Everything that didn't go on to the model was decided by a rule —
        # sent to Reading, or hidden (a Skip rule leaves no item at all).
        for_llm = triage_rules.prefilter(raw_items)[0]
        decided_ids = {raw.source_id for raw in raw_items} - {raw.source_id for raw in for_llm}
        items, degraded = pipeline._triage(raw_items, "eval", model=model)
    if degraded:
        raise RuntimeError(f"Triage failed during the eval: {degraded}")

    by_id = {item["source_id"]: item for item in items}
    now = datetime.now(TZ)
    results = []
    for case in cases:
        item = by_id.get(case["id"])
        missed_ok = True
        if case.get("expect_missed"):
            missed_ok = bool(item and deadlines.is_missed(item.get("deadline"), now))
        results.append(Result(
            case_id=case["id"], expected=case["expected"], got=item["lane"] if item else None,
            accept=tuple(case.get("accept", ())),
            decided_by="rules" if case["id"] in decided_ids else "model",
            missed_ok=missed_ok, known_gap=bool(case.get("known_gap")),
        ))
    return results


def accuracy(results: list[Result]) -> float:
    return sum(r.ok for r in results) / len(results) if results else 0.0


def report(results: list[Result]) -> str:
    lines = [f"Triage eval: {sum(r.ok for r in results)}/{len(results)} correct ({accuracy(results):.0%})", ""]
    for r in results:
        mark = "ok  " if r.ok else ("gap " if r.known_gap else "MISS")
        got = r.got or ("hidden" if r.expected == "skip" else "(dropped)")
        extra = "" if r.missed_ok else "  (deadline not flagged as passed)"
        lines.append(f"  {mark} {r.case_id:<28} expected {r.expected:<13} got {got:<13} by {r.decided_by}{extra}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--simulated", action="store_true", help="use each case's scripted answer instead of the LLM")
    args = parser.parse_args(argv)
    cases = load_cases()
    results = run_eval(cases, model=simulated_model(cases) if args.simulated else None)
    print(report(results))
    return 0 if all(r.ok or r.known_gap for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
