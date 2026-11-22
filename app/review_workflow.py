#!/usr/bin/env python3
"""Offline operator review for routing/dependency recovery conflicts.

Review decisions are simulated operator C consent, not real authentication.
Every decision forks a saved review run; parent evidence is immutable.
No model calls. Start and inspect cases in the dashboard's Recovery review tab.
"""
from __future__ import annotations

import argparse
import copy
import json
import re
import sqlite3
import sys
import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path

import simulator as sim

VERSION = "agent-rehearsal-review-v1"
MODE = "OFFLINE SIMULATED OPERATOR REVIEW; NO MODEL CALLS"
SCENARIOS = {"routing_conflict", "dependency_conflict"}
DECISIONS = {"retain", "withdraw_route", "release_dependency"}
DEPENDENCY = "C-legitimate-job"
ROOT = Path(__file__).resolve().parent


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def state_digest(engine):
    events = engine.events()
    return sim.digest({"state": engine.state(), "journal_head": events[-1]["sequence"] if events else 0})


class ReviewEngine(sim.Engine):
    def __init__(self, path, create=False):
        super().__init__(path, create=create)
        self.db.execute("""CREATE TABLE IF NOT EXISTS review_receipts (
            decision_key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, result TEXT NOT NULL)""")

    def decide(self, scenario, decision, note, expected_digest, decision_key):
        request = {"scenario": scenario, "decision": decision, "note": note,
                   "expected_digest": expected_digest, "actor": "C", "run_id": "route-C"}
        fingerprint = sim.digest(request)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            sim.require(isinstance(decision_key, str) and bool(decision_key), "INVALID_ARGUMENT", "A decision key is required.")
            prior = self.db.execute("SELECT fingerprint, result FROM review_receipts WHERE decision_key=?", (decision_key,)).fetchone()
            if prior:
                result = ({**json.loads(prior[1]), "replayed": True} if prior[0] == fingerprint else
                          {"status": "BLOCKED", "code": "REVIEW_KEY_CONFLICT", "reason": "This decision key has different content."})
                self._event({"kind": "REVIEW_REPLAY", **request, "result": result, "changes": {}})
                self.db.execute("COMMIT")
                return result
            before = self.state()
            after = copy.deepcopy(before)
            changes = {}
            try:
                sim.require(scenario in SCENARIOS and decision in DECISIONS, "INVALID_ARGUMENT", "Unsupported review decision.")
                sim.require(isinstance(note, str) and bool(note.strip()) and len(note) <= 300,
                            "REVIEW_NOTE_REQUIRED", "Record a reason of 1 to 300 characters.")
                sim.require(expected_digest == state_digest(self), "STALE_REVIEW_STATE",
                            "The state or journal changed after this review was displayed. Inspect the current branch.")
                sim.require("deploy-A" in after["paused_runs"], "RECOVERY_NOT_PAUSED", "Pause the failed workflow before review.")
                reports = [e for e in self.events() if e["kind"] == "RECOVERY_REPORT" and e.get("run_id") == "deploy-A"]
                sim.require(reports and reports[-1]["report"]["status"] == "RECOVERY_REVIEW_REQUIRED",
                            "REVIEW_NOT_REQUIRED", "No unresolved recovery report is available.")
                tool = "set_route" if scenario == "routing_conflict" else "add_dependency"
                plan = self._approval(after, "C", "route-C", "AP-route-C", tool)
                sim.require(plan.get("candidate") == "candidate-v2", "RESOURCE_SCOPE_MISMATCH",
                            "C's approved plan does not cover the reviewed candidate.")
                if scenario == "dependency_conflict":
                    sim.require(plan.get("can_depend"), "ACTOR_PERMISSION_DENIED", "C's plan does not authorize dependency work.")
                candidate = self._candidate(after, "candidate-v2", "deploy-A")
                sim.require(candidate["allocated_units"] > 0, "RESOURCE_MISSING", "No retained candidate allocation exists.")
                if decision == "withdraw_route":
                    sim.require(scenario == "routing_conflict", "REVIEW_SCOPE_MISMATCH", "This review concerns a dependency, not a route.")
                    sim.require(after["route_owner"] == "route-C" and after["routing"]["candidate-v2"] > 0,
                                "ROUTE_OWNER_CONFLICT", "Operator C no longer owns an active candidate route.")
                    sim.require(after["stable_healthy"] and after["stable_allowed"] and after["stable_capacity_sufficient"],
                                "FALLBACK_NOT_SAFE", "Stable fallback must be healthy, allowed and sufficiently sized.")
                    after["routing"] = {"stable-v1": 100, "candidate-v2": 0}
                    after["route_version"] += 1
                    # C owns the explicit withdrawal. Recovery must not overwrite it.
                    after["route_owner"] = "route-C"
                    reason = "Simulated operator C consent withdraws only C's active candidate route."
                elif decision == "release_dependency":
                    sim.require(scenario == "dependency_conflict", "REVIEW_SCOPE_MISMATCH", "This review concerns a route, not a dependency.")
                    owned = {"id": DEPENDENCY, "owner_run": "route-C"}
                    sim.require(owned in candidate["dependencies"], "DEPENDENCY_OWNER_CONFLICT", "The reviewed dependency is not owned by C.")
                    candidate["dependencies"].remove(owned)
                    reason = "Simulated operator C consent releases only the reviewed C-owned dependency."
                else:
                    reason = "Simulated operator C retains the resource; automatic cleanup remains unresolved."
                self.check_invariants(after)
                changes = {k: {"before": before[k], "after": after[k]} for k in after if after[k] != before[k]}
                if changes:
                    self.db.execute("UPDATE service_state SET payload=? WHERE id=1", (sim.canonical(after),))
                result = {"status": "SUCCEEDED", "code": "OK", "reason": reason}
            except sim.Denied as exc:
                result = {"status": "BLOCKED", "code": exc.code, "reason": exc.reason}
                changes = {}
            self._event({"kind": "REVIEW_DECISION", "label": "EXPLICIT SIMULATED OPERATOR C CONSENT",
                         "decision_key": decision_key, **request, "result": result, "changes": changes})
            self.db.execute("INSERT INTO review_receipts VALUES (?,?,?)", (decision_key, fingerprint, sim.canonical(result)))
            self.db.execute("COMMIT")
            return result
        except BaseException:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise


def scenario_setup(path, scenario):
    if scenario == "routing_conflict":
        sim.run_scenario(scenario, path)
        return
    engine = sim.Engine(path)
    try:
        sim.start_canary(engine)
        sim.cost_change(engine)
        result = sim.action(engine, "add_dependency", {"candidate_id": "candidate-v2", "dependency_id": DEPENDENCY},
                            "C-dependency", actor="C", run="route-C")
        if result["status"] != "SUCCEEDED":
            raise RuntimeError("The reviewed dependency fixture was not admitted.")
        engine.fixture_event("CANDIDATE_FAILURE")
        engine.recover()
    finally:
        engine.close()


def build_report(engine, scenario, parent=None, decision=None):
    state, events = engine.state(), engine.events()
    recovery = next(e["report"] for e in reversed(events) if e["kind"] == "RECOVERY_REPORT")
    candidate = state["deployments"]["candidate-v2"]
    unresolved = recovery["status"] == "RECOVERY_REVIEW_REQUIRED" and candidate["allocated_units"] > 0
    allowed = ["retain", "withdraw_route" if scenario == "routing_conflict" else "release_dependency"] if unresolved else []
    expected_owner = "route-C" if scenario == "routing_conflict" else "deploy-A"
    units = state["stable_units"] + sum(d["allocated_units"] for d in state["deployments"].values())
    units += sum(r["units"] for r in state["reservations"].values() if r["status"] == "PENDING")
    assertions = [{"assertion": name, "passed": bool(passed)} for name, passed in [
        ("B's capacity limit and version remain intact", state["capacity_limit"] == 70 and state["budget_version"] == 2),
        ("A remains paused for recovery", "deploy-A" in state["paused_runs"]),
        ("Stable allocation remains 40 units", state["stable_units"] == 40),
        ("Commitments remain within the current limit", units <= state["capacity_limit"]),
        ("Traffic weights total 100 percent", sum(state["routing"].values()) == 100),
        ("The current route owner is preserved", state["route_owner"] == expected_owner),
        ("Any released candidate has a confirmed stop", candidate["allocated_units"] > 0 or candidate["status"] == "STOPPED"),
    ]]
    return {"version": VERSION, "mode": MODE, "scenario": scenario, "model_calls": 0,
            "task_outcome": "REVIEW_DECISION_RECORDED" if decision else "AWAITING_OPERATOR_REVIEW",
            "recovery_outcome": recovery["status"], "recovery": recovery, "initial_state": sim.initial_state(),
            "final_state": state, "events": events, "assertions": assertions,
            "review": {"state_digest": state_digest(engine), "available_decisions": allowed,
                       "operator": "SIMULATED C / route-C", "source_run": parent,
                       "decision": decision, "dependency_id": DEPENDENCY if scenario == "dependency_conflict" else None,
                       "scope": "Local fixture consent, not real manager identity verification or production authorization."}}


def save_report(directory, report):
    write_json(directory / "report.json", report)
    write_json(directory / "summary.json", {k: report[k] for k in
               ("version", "mode", "scenario", "task_outcome", "recovery_outcome", "model_calls", "review")})


def new_directory(output):
    directory = output / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8])
    directory.mkdir(parents=True, exist_ok=False)
    return directory


def create_case(output, scenario):
    if scenario not in SCENARIOS:
        raise ValueError("Unsupported review scenario.")
    directory = new_directory(output)
    scenario_setup(directory / "state.sqlite3", scenario)
    engine = ReviewEngine(directory / "state.sqlite3")
    try:
        report = build_report(engine, scenario)
        save_report(directory, report)
        return directory, report
    finally:
        engine.close()


def source_directory(output, run):
    if not isinstance(run, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", run):
        raise ValueError("Invalid source review identifier.")
    base = output.resolve()
    directory = (base / run).resolve()
    if directory.parent != base:
        raise ValueError("Source review is outside the review folder.")
    for filename in ("report.json", "state.sqlite3"):
        path = directory / filename
        if not path.is_file() or path.resolve().parent != directory:
            raise ValueError("Source review evidence is missing or outside its run folder.")
    return directory


def resolve_case(output, source_run, expected_digest, decision, note):
    source = source_directory(output, source_run)
    report_path = source / "report.json"
    if report_path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("The source review report is too large.")
    original = json.loads(report_path.read_text(encoding="utf-8"))
    if original.get("version") != VERSION or original.get("scenario") not in SCENARIOS:
        raise ValueError("Unsupported source review format.")
    directory = new_directory(output)
    source_db = sqlite3.connect((source / "state.sqlite3").as_uri() + "?mode=ro", uri=True)
    destination = sqlite3.connect(directory / "state.sqlite3")
    try:
        source_db.backup(destination)
    finally:
        destination.close()
        source_db.close()
    engine = ReviewEngine(directory / "state.sqlite3")
    try:
        if engine.state() != original["final_state"] or engine.events() != original["events"]:
            raise ValueError("Source database and saved journal disagree; inspect the source evidence.")
        decision_key = "review-" + sim.digest({"parent": source_run, "digest": expected_digest,
                                             "decision": decision, "note": note})[:24]
        result = engine.decide(original["scenario"], decision, note, expected_digest, decision_key)
        if result["status"] == "SUCCEEDED" and decision != "retain":
            engine.recover()  # Every cleanup gate is checked again against current state.
        report = build_report(engine, original["scenario"], parent=source_run,
                              decision={"choice": decision, "note": note, "result": result})
        save_report(directory, report)
        return directory, report
    finally:
        engine.close()




def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "review_runs")
    parser.add_argument("--scenario", choices=sorted(SCENARIOS), default="routing_conflict")
    parser.add_argument("--source-run")
    parser.add_argument("--expected-digest")
    parser.add_argument("--decision", choices=sorted(DECISIONS))
    parser.add_argument("--note")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(ReviewTests))
        return 0 if result.wasSuccessful() else 1
    if args.source_run:
        if args.expected_digest is None or args.decision is None or args.note is None:
            parser.error("A source review requires the displayed digest, a decision and a reason.")
        directory, report = resolve_case(args.output, args.source_run, args.expected_digest, args.decision, args.note)
    else:
        directory, report = create_case(args.output, args.scenario)
    print("ORKESTRYN - SIMULATED OPERATOR REVIEW")
    print("No model calls. Decisions fork saved local simulation evidence.")
    print("Recovery: " + report["recovery_outcome"])
    print("Decision: " + (report["review"]["decision"]["result"]["status"] if report["review"]["decision"] else "AWAITING REVIEW"))
    print("Evidence: " + str(directory.resolve()))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        print("REVIEW WORKFLOW FAILED: " + str(exc), file=sys.stderr)
        raise SystemExit(1)
