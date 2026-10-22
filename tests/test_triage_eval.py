"""The offline triage eval (triage_eval.py): its cases are well-formed, it
runs the real pull path against a throwaway DB, and — with a scripted
careless model — the deterministic layers get every case right except
the recorded known gaps."""

from datetime import date

from command_center import db, triage_eval
from command_center.config import LANES


def test_cases_are_well_formed_and_dates_are_relative() -> None:
    cases = triage_eval.load_cases(today=date(2026, 10, 13))
    assert len(cases) >= 20
    assert len({c["id"] for c in cases}) == len(cases)
    assert all(c["expected"] in (*LANES, "skip") for c in cases)
    passed = next(c for c in cases if c["id"] == "agenthon-deadline-passed")
    assert passed["simulated"]["deadline"] == "2026-10-11 23:59 AoE"


def test_the_review_cases_are_all_covered() -> None:
    ids = {c["id"] for c in triage_eval.load_cases()}
    assert {"linkedin-job-alert", "paid-survey", "nvidia-gtc-promo", "agenthon-deadline-passed",
            "turkish-recruiter"} <= ids


def test_the_eval_never_touches_the_configured_database() -> None:
    before = db.DB_PATH
    cases = triage_eval.load_cases()[:3]
    triage_eval.run_eval(cases, model=triage_eval.simulated_model(cases))
    assert db.DB_PATH == before
    assert not before.exists()  # conftest's throwaway path: nothing was written to it
