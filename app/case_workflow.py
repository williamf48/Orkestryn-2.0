#!/usr/bin/env python3
"""Connect validated challenges to explicit revision review and bounded recovery.

Default: eight free scripted integration cases. --live: a bounded Nebius
campaign that stops for human review. Infrastructure and authority are simulated.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import html
import json
import os
import platform
import sys
import tempfile
import unittest
from unittest import mock
import webbrowser
from collections import Counter
from datetime import datetime, timezone
from getpass import getpass
from pathlib import Path

import challenge_factory as factory
import conflict_explorer as explorer
import conflict_lab as base
import live_evaluator as inference
import plan_revision as revision
import simulator as sim
import coverage_planner as coverage

VERSION = "agent-rehearsal-case-workflow-v1"
ROOT = Path(__file__).resolve().parent
SYSTEM = factory.SYSTEM + """

This campaign connects each challenge to revision at the original PRE-RECOVERY
decision boundary, after all seven operator steps and the injected fault.
Aim for varied cases where A's extra reservation is BLOCKED by B's changed
budget, while C has valid routing or a dependency. Include both: (1) healthy,
unpaused cases with positive but smaller remaining headroom, suitable for a
partial reservation; (2) paused, unhealthy, stale or zero-headroom cases where
revision must defer. stop_timeout, requests_in_flight and metrics_at_limit can
leave an otherwise healthy candidate. Use legal schedules and different bounded
parameters. Do not remove faults or supply revision/approval fields. Successful
original reservations are retained as not applicable, never replaced twice.
When coverage_target is supplied, aim for that requested outcome. Use the
recent observed coverage feedback to correct missed targets within the fixed
schema. An accepted case can still miss the target; do not claim success in reason.
"""


class NemotronChallenges(factory.NemotronProvider):
    system_prompt = SYSTEM


def file_digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class CallBudget:
    """One shared ceiling across generation and revision; record before dispatch."""
    def __init__(self, directory, limit):
        if type(limit) is not int or not 1 <= limit <= 10:
            raise ValueError("Use an integer total call limit from 1 to 10.")
        self.path, self.limit, self.rows = Path(directory) / "calls.json", limit, []
        self.save()

    @property
    def remaining(self):
        return self.limit - len(self.rows)

    def save(self):
        factory.write_json(self.path, {"limit": self.limit, "calls": self.rows})

    def wrap(self, provider, stage):
        budget = self

        class BoundedProvider:
            live = provider.live
            mode = provider.mode
            request_settings = provider.request_settings
            system_prompt = getattr(provider, "system_prompt", factory.SYSTEM)

            def ask(self, context):
                if not budget.remaining:
                    raise factory.GenerationError("The shared campaign call limit has been reached.")
                row = {"call": len(budget.rows) + 1, "stage": stage, "status": "REQUEST_STARTED",
                       "context_sha256": sim.digest(context), "request_settings": self.request_settings}
                budget.rows.append(row)
                budget.save()
                try:
                    content, metadata = provider.ask(context)
                    row.update(status="RECEIVED", metadata=metadata)
                    return content, metadata
                except BaseException as exc:
                    row["status"] = "INTERRUPTED" if isinstance(exc, KeyboardInterrupt) else "ERROR"
                    raise
                finally:
                    budget.save()
        return BoundedProvider()


def read_corpus(path):
    path = Path(path).resolve()
    if path.is_dir():
        path = path / "accepted_proposals.jsonl"
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024 * 1024:
        raise ValueError("Use a regular challenge JSON or accepted_proposals.jsonl file up to 1 MiB.")
    raw = path.read_bytes()
    if path.suffix.lower() == ".jsonl":
        lines = [line for line in raw.decode("utf-8").splitlines() if line.strip()]
        if len(lines) > 100:
            raise ValueError("The input corpus is limited to 100 proposals.")
        proposals = [factory.validate_proposal(factory.decode(line)) for line in lines]
    else:
        proposals = [factory.load_proposal(path)]
    if not proposals:
        raise ValueError("This source has no accepted challenge proposals.")
    return path, raw, proposals


def prepare_case(directory, proposal):
    """Recompute baseline labels, then replay exactly to the same decision state."""
    proposal = factory.validate_proposal(proposal)
    baseline = factory.rehearse(proposal)
    if not baseline["passed"]:
        raise ValueError("Independent baseline checks failed; no revision session was created.")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    factory.write_json(directory / "challenge.json", proposal)
    factory.write_json(directory / "baseline.json", baseline)
    e = revision.RevisionEngine(directory / "coordinator.sqlite3")
    try:
        spec = factory.executable(proposal)
        explorer.seed(e, spec["parameters"])
        trace = explorer.execute(e, spec)
        checks = {"independent_baseline_passed": bool(baseline["passed"]),
                  "exact_decision_state": e.state() == baseline["decision_state"],
                  "exact_event_prefix": e.events() == baseline["parent_events"],
                  "exact_operator_trace": trace == baseline["trace"], **explorer.audit(e)}
        handoff = {"boundary": "AFTER_ALL_OPERATOR_STEPS_AND_FAULT_BEFORE_RECOVERY", "checks": checks,
                   "passed": all(checks.values()), "state_sha256": sim.digest(e.state()),
                   "event_prefix_sha256": sim.digest(e.events()), "event_prefix_length": len(e.events())}
        factory.write_json(directory / "handoff.json", handoff)
        if not handoff["passed"]:
            raise ValueError("Case replay did not preserve the original state and journal; revision stopped.")
        meta = {"version": revision.VERSION, "scenario": "generated_" + proposal["scenario"],
                "scripted_demo": False, "model_calls": 0, "proposal_attempts": [],
                "mode": "VALIDATED CASE REPLAY; NO REVISION PROPOSAL YET", "created_at": datetime.now(timezone.utc).isoformat(),
                "source_case": {"case_id": baseline["case_id"], "program_digest": factory.identifiers(proposal)[0],
                                "proposal_sha256": file_digest(directory / "challenge.json"),
                                "baseline_sha256": file_digest(directory / "baseline.json"),
                                "handoff_sha256": file_digest(directory / "handoff.json")}}
        factory.write_json(directory / "session.json", meta)
        report = revision.save_report(directory, e, meta)
        return report, handoff
    finally:
        e.close()


def finish_scripted_case(directory):
    """Only the explicitly labelled offline demo calls this fixture manager."""
    meta = revision.session_metadata(directory)
    if meta.get("model_calls"):
        raise ValueError("Scripted consent cannot be used for a live proposal session.")
    e = revision.RevisionEngine(directory / "coordinator.sqlite3", create=False)
    try:
        checks = {}
        if meta.get("proposal_id"):
            record = e.proposal_record(meta["proposal_id"])
            checks["proposal_accepted_or_deferred"] = record["status"] in ("AWAITING_APPROVAL", "DEFERRED")
            if record["status"] == "AWAITING_APPROVAL":
                approved = e.approve(record["revision_id"], "Offline integration fixture consents to this exact reservation and bounded recovery.",
                                     "scripted-consent", source="SCRIPTED OFFLINE DEMO MANAGER FIXTURE")
                before = base.commitments(e.state())
                executed = e.execute_revision(record["revision_id"])
                after = base.commitments(e.state())
                replay = e.execute_revision(record["revision_id"])
                checks.update(fresh_consent_recorded=approved["status"] == "SUCCEEDED",
                              exact_reservation=executed["status"] == "SUCCEEDED" and after == before + record["proposal"]["additional_units"],
                              execution_idempotent=bool(replay.get("replayed")) and base.commitments(e.state()) == after)
        recovery = revision.recovery_check(e)
        checks.update({"recovery_" + key: value for key, value in recovery["checks"].items()})
        meta.update(scripted_demo=True, demo_recovery=recovery, demo_checks=checks)
        factory.write_json(directory / "session.json", meta)
        revision.save_report(directory, e, meta)
        return checks
    finally:
        e.close()


def refresh_campaign(directory):
    """Refresh from saved databases; no model requests or service actions."""
    directory = Path(directory).resolve()
    config = revision.read_json(directory / "campaign.json")
    if config.get("version") != VERSION:
        raise ValueError("Use a saved Case Workflow campaign folder.")
    rows, session_calls, session_tokens = [], 0, []
    for item in config["cases"]:
        name = item["directory"]
        if not isinstance(name, str) or Path(name).name != name or not name.startswith("case-") or "\\" in name:
            raise ValueError("Invalid case directory in the campaign record.")
        path = directory / name
        if path.is_symlink():
            raise ValueError("Case folders must not be symbolic links.")
        meta = revision.read_json(path / "session.json")
        session_calls += meta.get("model_calls", 0)
        session_tokens.extend(a.get("generation", {}).get("metadata", {}).get("total_tokens") for a in meta.get("proposal_attempts", []) if a.get("generation"))
        if not meta.get("scripted_demo"):
            revision.session_metadata(path)
        e = revision.RevisionEngine(path / "coordinator.sqlite3", create=False)
        try:
            report = revision.save_report(path, e, meta)
            handoff = revision.read_json(path / "handoff.json")
            baseline = revision.read_json(path / "baseline.json")
            prefix_valid = sim.digest(e.events()[:handoff["event_prefix_length"]]) == handoff["event_prefix_sha256"]
            statuses = [r["status"] for r in report["revisions"]]
            if "deploy-A" in e.state()["paused_runs"] and any(event["kind"] == "RECOVERY_REPORT" for event in e.events()):
                stage = "RECOVERED"
            else:
                stage = statuses[-1] if statuses else item["stage"]
            checks = {**handoff["checks"], "source_event_prefix_preserved": prefix_valid,
                      **report["journal_audit"], **meta.get("demo_checks", {})}
            latest = report["revisions"][-1] if report["revisions"] else None
            rows.append({**item, "stage": stage, "proposal_status": statuses[-1] if statuses else None,
                         "revision_units": latest["proposal"]["additional_units"] if latest else None,
                         "revision_rehearsal_passed": latest["rehearsal"]["passed"] if latest else None,
                         "reference_admission": latest["rehearsal"]["reference_admission"] if latest else None,
                         "before_units": base.commitments(baseline["decision_state"]), "current_units": report["committed_units"],
                         "capacity_limit": report["state"]["capacity_limit"], "route_owner": report["state"]["route_owner"],
                         "model_calls": meta["model_calls"], "checks": checks, "checks_passed": all(checks.values()),
                         "coverage_observation": coverage.classify(baseline), "report": name + "/index.html"})
        finally:
            e.close()
    ledger = revision.read_json(directory / "calls.json")
    generation_rows = [row for row in ledger["calls"] if row["stage"] == "challenge_generation"]
    tokens = [row.get("metadata", {}).get("total_tokens") for row in generation_rows] + session_tokens
    known = [value for value in tokens if type(value) is int and value >= 0]
    total_calls = len(generation_rows) + session_calls
    stages = dict(Counter(row["stage"] for row in rows))
    summary = {**config, "rows": rows, "counts_by_stage": stages, "cases_total": len(rows),
               "handoffs_verified": sum(all(row["checks"].values()) for row in rows),
               "checks_passed": sum(sum(row["checks"].values()) for row in rows),
               "checks_total": sum(len(row["checks"]) for row in rows),
               "model_calls": len(ledger["calls"]), "calls_by_stage": dict(Counter(row["stage"] for row in ledger["calls"])),
               "followup_model_calls": max(0, total_calls - len(ledger["calls"])), "total_recorded_model_calls": total_calls,
               "usage": {"known_total_tokens": sum(known), "calls_with_usage": len(known), "missing_usage_calls": max(0, total_calls - len(known))},
               "refreshed_at": datetime.now(timezone.utc).isoformat(),
               "scope": "Development cases, simulated infrastructure and local manager fixtures. Baseline recovery and revised recovery are distinct branches. Not held-out accuracy or production execution."}
    factory.write_json(directory / "summary.json", summary)
    (directory / "index.html").write_text(render_report(summary, directory), encoding="utf-8")
    return summary


def render_report(summary, directory):
    esc = lambda value: html.escape(str(value), quote=True)
    parts = ["<!doctype html><html lang='en'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Orkestryn · Connected case lab</title>", factory.STYLE,
             "<h1>Connected case lab</h1><p>" + esc(summary["mode"]) + "</p><p>" + esc(summary["scope"]) + "</p>",
             "<div class='cards'><div class='card'><strong class='big'>" + esc(summary["cases_total"]) + "</strong>Validated cases</div><div class='card'><strong class='big'>" + esc(summary["handoffs_verified"]) + "</strong>Case checks pass</div><div class='card'><strong class='big'>" + esc(summary["model_calls"]) + " / " + esc(summary["call_limit"]) + "</strong>Campaign calls</div><div class='card'><strong class='big'>" + esc(summary["counts_by_stage"].get("RECOVERED", 0)) + "</strong>Recovered sessions</div></div>",
             "<h2>One case across three problems</h2><p>6: propose and independently validate a challenge. 9: retain its state, rehearse a revision and require fresh consent for the exact action. 12: recover eligible resources while preserving other operators' work.</p>",
             "<p>Campaign status: " + esc(summary["status"]) + ". " + esc(summary.get("stop_reason") or "") + "</p>",
             "<p>Additional calls from later, separately requested session proposals: " + esc(summary["followup_model_calls"]) + ". The campaign ceiling applies to the original command only. Total recorded calls including follow-ups: " + esc(summary["total_recorded_model_calls"]) + ".</p>",
             "<p>Reported tokens: " + esc(summary["usage"]["known_total_tokens"]) + "; calls reporting usage: " + esc(summary["usage"]["calls_with_usage"]) + "; missing usage: " + esc(summary["usage"]["missing_usage_calls"]) + ". No billed cost estimate.</p>",
             "<p><a href='" + esc(summary["factory_report"]) + "'>Challenge Factory evidence</a> · <a href='summary.json'>Campaign data</a> · <a href='calls.json'>Call ledger</a></p>",
             "<h2>Review each case</h2><p>Live and imported cases stop for your separate approval. Open a case for its exact commands. Deferral is a valid proposal outcome. NOT_APPLICABLE means the original reservation already succeeded. READY_FOR_PROPOSAL means no revision model call was made. A passing handoff does not imply an approved or executed revision.</p>",
             "<div class='scroll'><table><tr><th>Case / fault</th><th>Original request</th><th>Current stage</th><th>Revision</th><th>Capacity before → now / limit</th><th>Checks</th></tr>"]
    if summary.get("generation", {}).get("coverage"):
        parts.insert(-2, coverage.report_fragment(summary["generation"]["coverage"]))
    for row in summary["rows"]:
        parts.append("<tr><td><a href='" + esc(row["report"]) + "'>" + esc(row["directory"]) + "</a><br>" + esc(row["scenario"]) + " / " + esc(row["fault"]) + "</td><td>" + esc(row["original_units"]) + " units / " + esc(row["original_result"]) + "</td><td>" + esc(row["stage"]) + "</td><td>" + esc(row["revision_units"] if row["revision_units"] is not None else "—") + "<br>" + esc(row["proposal_status"] or "No proposal") + "</td><td>" + esc(row["before_units"]) + " → " + esc(row["current_units"]) + " / " + esc(row["capacity_limit"]) + "</td><td>" + ("PASS" if row["checks_passed"] else "FAIL") + "</td></tr>")
    quote = lambda value: "'" + str(value).replace("'", "''") + "'"
    command = ".\\.venv\\Scripts\\python.exe project.py case-workflow --run " + quote(directory) + " --inspect --open"
    parts += ["</table></div><h2>Refresh after review or recovery</h2><p>Case reports update when their commands run. Refresh this campaign to read the latest saved states; this makes no model calls.</p><pre>" + esc(command) + "</pre>",
              "<details><summary>Campaign limits and provenance</summary><pre>" + esc(json.dumps({key: summary[key] for key in ("call_limit", "source", "generation", "calls_by_stage", "counts_by_stage")}, indent=2)) + "</pre></details><footer>" + esc(VERSION) + "</footer></html>"]
    return "".join(parts)


def run_campaign(output, generation_provider=None, revision_provider=None, source=None, cases=5, calls=10, scripted=False, progress=True, coverage_mode="balanced"):
    policy = coverage.CoveragePlan(coverage_mode)
    live = generation_provider is not None and generation_provider.live or revision_provider is not None and revision_provider.live
    if type(cases) is not int or not 1 <= cases <= (5 if live else 8):
        raise ValueError("Use 1–5 cases for a live campaign or 1–8 offline cases.")
    if scripted and live:
        raise ValueError("Live campaigns always require separate explicit manager consent.")
    if type(calls) is not int or not 1 <= calls <= 10:
        raise ValueError("Use 1–10 total calls for generation and revision combined.")
    captured = read_corpus(source) if source is not None else None
    if source is None and generation_provider is None:
        raise ValueError("Choose a challenge source or a generation provider.")
    directory = revision.new_directory(output)
    budget = CallBudget(directory, calls)
    origin = {"kind": "MODEL_GENERATION" if live and source is None else "OFFLINE_FIXTURES"}
    if captured:
        path, raw, proposals = captured
        (directory / "source").mkdir()
        (directory / "source" / path.name).write_bytes(raw)
        origin = {"kind": "IMPORTED_CHALLENGES", "path": str(path), "sha256": hashlib.sha256(raw).hexdigest(),
                  "available_proposals": len(proposals), "selected_proposals": min(cases, len(proposals)),
                  "unselected_proposals": max(0, len(proposals) - cases)}
        generation_provider = factory.FixtureProvider([base.encode(p) for p in proposals[:cases]], "OFFLINE REVALIDATION OF IMPORTED CHALLENGES")
        cases = min(cases, len(proposals))
    generation_calls = min(cases, budget.remaining) if generation_provider.live else cases
    generation = budget.wrap(generation_provider, "challenge_generation") if generation_provider.live else generation_provider
    if not generation_provider.live:
        policy = coverage.CoveragePlan("off")
    factory_directory, generated = factory.run_batch(generation, directory / "factory", generation_calls, progress=progress, policy=policy)
    config = {"version": VERSION, "mode": "OFFLINE SCRIPTED CONNECTED CASE DEMO" if scripted else "LIVE NEBIUS CAMPAIGN; EXPLICIT MANAGER REVIEW REQUIRED" if live else "OFFLINE IMPORT; EXPLICIT MANAGER REVIEW REQUIRED",
              "created_at": datetime.now(timezone.utc).isoformat(), "platform": platform.system(),
              "call_limit": calls, "source": origin, "generation": generated, "cases": [],
              "factory_report": str((factory_directory / "index.html").relative_to(directory)).replace(os.sep, "/"),
              "status": generated["run_status"], "stop_reason": generated["stop_reason"]}
    factory.write_json(directory / "campaign.json", config)
    accepted = [factory.validate_proposal(factory.decode(line)) for line in (factory_directory / "accepted_proposals.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    allow_requests = generated["run_status"] == "COMPLETED"
    try:
        for index, proposal in enumerate(accepted, 1):
            name = "case-%03d" % index
            case_directory = directory / name
            report, _ = prepare_case(case_directory, proposal)
            original = report["original_request"]
            stage = "NOT_APPLICABLE" if original["result"]["status"] == "SUCCEEDED" else "READY_FOR_PROPOSAL"
            item = {"directory": name, "scenario": proposal["scenario"], "fault": proposal["fault"],
                    "original_units": original["args"]["units"], "original_result": original["result"]["code"], "stage": stage}
            config["cases"].append(item)
            factory.write_json(directory / "campaign.json", config)
            if stage == "READY_FOR_PROPOSAL" and allow_requests:
                provider = None
                if live:
                    if revision_provider is None or not budget.remaining:
                        item["reason"] = "Shared call budget exhausted or no revision provider; no fallback proposal was invented."
                        continue
                    provider = budget.wrap(revision_provider, "plan_revision")
                _, report = revision.create_session(directory, "generated", provider=provider, existing_directory=case_directory)
                item["stage"] = report["meta"].get("proposal_status", report["meta"].get("generation", {}).get("status", "FAILED"))
                failure = report["meta"].get("generation", {}).get("status")
                if failure in ("API_ERROR", "REFUSED", "INCOMPLETE_RESPONSE", "INTERRUPTED", "INTERNAL_ERROR"):
                    allow_requests = False
                    config.update(status="STOPPED", stop_reason="Revision request stopped the paid campaign: " + failure + ". No automatic retry.")
            if scripted:
                finish_scripted_case(case_directory)
            factory.write_json(directory / "campaign.json", config)
            if progress:
                print("Connected case %s/%s: %s" % (index, len(accepted), item["stage"]), flush=True)
    except BaseException:
        config.update(status="STOPPED", stop_reason="Campaign interrupted; retained cases and call ledger can be inspected. No automatic resume.")
        raise
    finally:
        if captured:
            config["source"]["original_file_unchanged"] = captured[0].is_file() and file_digest(captured[0]) == origin["sha256"]
        config["calls_unused"] = budget.remaining
        factory.write_json(directory / "campaign.json", config)
        summary = refresh_campaign(directory)
    return directory, summary


def demo_proposals():
    order = ["A_READ", "B_READ", "B_LIMIT", "C_READ", "C_WRITE", "A_RESERVE", "A_ROUTE"]
    rows = [(35, 15, 16, 80, 55, "routing", "stop_timeout"),
            (27, 13, 12, 65, 46, "dependency", "requests_in_flight"),
            (20, 10, 8, 55, 34, "routing", "metrics_at_limit"),
            (30, 12, 15, 70, 50, "routing", "workflow_failure"),
            (22, 8, 10, 55, 35, "dependency", "candidate_failure"),
            (18, 7, 6, 45, 30, "routing", "metrics_expired"),
            (40, 20, 20, 100, 60, "dependency", "stop_timeout"),
            (15, 5, 4, 50, 40, "routing", "stop_timeout")]
    proposals = []
    for values in rows:
        p = {"schema_version": factory.PROPOSAL_VERSION, "reason": "Offline integration fixture for exact case continuity and bounded revision.",
             "parameters": dict(zip(("stable_units", "candidate_units", "extra_units", "initial_limit", "new_limit"), values[:5])),
             "scenario": values[5], "fault": values[6], "fault_after": 7, "order": order[:]}
        proposals.append(p)
    proposals[-1]["order"] = ["A_READ", "A_RESERVE", "A_ROUTE", "B_READ", "B_LIMIT", "C_READ", "C_WRITE"]
    return proposals


def run_demo(output, progress=True):
    provider = factory.FixtureProvider([base.encode(p) for p in demo_proposals()])
    return run_campaign(output, generation_provider=provider, cases=8, scripted=True, progress=progress)




def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--from-factory", type=Path, help="A factory run folder, accepted_proposals.jsonl, or one challenge JSON.")
    parser.add_argument("--cases", type=int, default=5, help="Maximum generated attempts or imported proposals (1–5 live).")
    parser.add_argument("--calls", type=int, default=10, help="Total paid call ceiling across generation and revision (1–10).")
    parser.add_argument("--max-output-tokens", type=int, default=10000)
    parser.add_argument("--thinking", choices=("on", "off"), default="on")
    parser.add_argument("--coverage", choices=("balanced", "repair", "off"), default="balanced", help="Target fresh generated cases; imports remain observation-only.")
    parser.add_argument("--run", type=Path)
    parser.add_argument("--inspect", action="store_true")
    parser.add_argument("--output", type=Path, default=ROOT / "case_runs")
    parser.add_argument("--open", action="store_true")
    args = parser.parse_args()
    if args.run or args.inspect:
        if not (args.run and args.inspect) or args.live or args.from_factory:
            parser.error("Refresh with --run FOLDER --inspect, without --live or --from-factory.")
        directory, summary = args.run.resolve(), refresh_campaign(args.run)
    elif args.live or args.from_factory:
        if not 1 <= args.cases <= 5 or not 1 <= args.calls <= 10 or not 1024 <= args.max_output_tokens <= 10000:
            parser.error("Use 1–5 cases, 1–10 total calls and 1024–10000 output tokens per call.")
        if args.from_factory:
            read_corpus(args.from_factory)
        generation = proposer = None
        if args.live:
            print("LIVE NEBIUS CAMPAIGN: at most %s TOTAL requests; output cap %s each. No automatic retries or manager approval." % (args.calls, args.max_output_tokens), flush=True)
            key = (os.environ.get("ORKESTRYN2_NEBIUS_API_KEY") or getpass("Paste your Nebius API key locally: ")).strip()
            if not key:
                raise factory.GenerationError("No key supplied; no requests made.")
            generation = NemotronChallenges(key, args.max_output_tokens, args.thinking == "on") if not args.from_factory else None
            proposer = revision.NemotronRevision(key, args.max_output_tokens, args.thinking == "on")
        directory, summary = run_campaign(args.output, generation, proposer, args.from_factory, args.cases, args.calls, coverage_mode=args.coverage)
    else:
        directory, summary = run_demo(args.output)
    print("Cases: %s | case checks passing: %s | checks: %s/%s | model calls: %s/%s" % (summary["cases_total"], summary["handoffs_verified"], summary["checks_passed"], summary["checks_total"], summary["model_calls"], summary["call_limit"]))
    print("Report: " + str(directory / "index.html"))
    if args.open:
        webbrowser.open((directory / "index.html").as_uri())
    return 0 if summary["status"] == "COMPLETED" and summary["checks_passed"] == summary["checks_total"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, factory.GenerationError) as exc:
        print("CASE WORKFLOW FAILED: " + str(exc), file=sys.stderr)
        raise SystemExit(1)
