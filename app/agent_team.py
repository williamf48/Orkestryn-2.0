#!/usr/bin/env python3
"""Three cooperating roles: challenge factory, governance and recovery.

Free scripted role replies by default, real local workers and HTTP. --live uses
three separate Nemotron role clients, at most one request each, with exact
interactive manager consent before a revised reservation can execute.
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import hashlib
import html
import io
import itertools
import json
import os
import platform
import subprocess
import sys
import tempfile
import unittest
import webbrowser
from datetime import datetime, timezone
from getpass import getpass
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import challenge_factory as factory
import plan_revision as revision
import service_agent as service
import simulator as sim
import terminal_guard

ROOT = Path(__file__).resolve().parent
VERSION = "agent-rehearsal-three-agent-team-v3"
ROLES = {"factory": (6, "Scenario factory"), "governance": (9, "Governance"), "recovery": (12, "Recovery")}
ORDER = ["A_READ", "B_READ", "B_LIMIT", "C_READ", "C_WRITE", "A_RESERVE", "A_ROUTE"]
FACTORY_VERSION = "agent-rehearsal-team-factory-proposal-v2"
FACTORY_BOUNDS = {"stable_units":(10,80), "candidate_units":(5,40), "headroom_units":(1,3), "extra_units":(4,20)}
FACTORY_FIELDS = {"schema_version", "reason", *FACTORY_BOUNDS}
FACTORY_SCHEMA = {"type":"object", "additionalProperties":False, "required":sorted(FACTORY_FIELDS),
    "properties":{"schema_version":{"const":FACTORY_VERSION}, "reason":{"type":"string","minLength":1,"maxLength":600},
        **{key:{"type":"integer","minimum":low,"maximum":high} for key,(low,high) in FACTORY_BOUNDS.items()}}}
FACTORY_SYSTEM = """You are the Factory Agent (part 6) of a three-agent team.
Return ONE JSON object only, without Markdown, with exactly these six fields:
schema_version, stable_units, candidate_units, headroom_units, extra_units, reason.
schema_version must be agent-rehearsal-team-factory-proposal-v2.
stable_units: integer 10 through 80. candidate_units: integer 5 through 40.
headroom_units: integer 1 through 3. extra_units: integer 4 through 20.
reason: a short public test intent, 1 through 600 characters, not a claimed result.

Choose the four independent numeric inputs to create a capacity conflict.
Do not calculate or return limits. The coordinator computes the initial limit
as stable_units + candidate_units + extra_units, and the tightened limit as
stable_units + candidate_units + headroom_units. Thus the original request is
too large, while a smaller request may fit. Do not return a parameters wrapper,
scenario, fault, fault_after, order, answer labels, approvals, tools or code.

The coordinator fixes the requested conflict family, operator order and fresh
metrics-at-boundary condition. Independent code validates and rehearses the
resulting challenge before starting workers. Governance (part 9) then observes
the real local state. Recovery (part 12) receives confirmed execution, approved
scope and a durable failure pause. You cannot authorize either peer. No retries
or silent replacement of rejected output. The schema in context is authoritative.
"""
PROMPTS = {"factory": FACTORY_SYSTEM, "governance": revision.SYSTEM, "recovery": service.RECOVERY_SYSTEM}


def implementation_digest():
    return sim.digest({"service_controller": service.controller_digest(),
        "agent_team.py": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "terminal_guard.py": hashlib.sha256((ROOT/"terminal_guard.py").read_bytes()).hexdigest()})


def factory_context(scenario):
    return {"schema":copy.deepcopy(FACTORY_SCHEMA), "requested_scenario":scenario,
        "task":"Choose the four independent numeric inputs. The coordinator builds and validates the fixed-profile challenge; do not return its program fields."}


def factory_fixture():
    return {"schema_version":FACTORY_VERSION, "stable_units":30, "candidate_units":10,
        "headroom_units":2, "extra_units":5,
        "reason":"SCRIPTED FACTORY FIXTURE: tighter shared capacity with C's legitimate work present before repair."}


def compile_factory_proposal(value, scenario):
    if scenario not in ("routing", "dependency"):
        raise factory.ProposalError("Unknown team conflict family.")
    if not isinstance(value, dict):
        raise factory.ProposalError("Factory must return one object matching the six-field team contract.")
    if set(value) != FACTORY_FIELDS:
        missing = ", ".join(sorted(FACTORY_FIELDS-set(value))) or "none"
        extra = ", ".join(sorted(str(key) for key in set(value)-FACTORY_FIELDS)) or "none"
        raise factory.ProposalError("Factory contract fields differ: missing ["+missing+"]; unexpected ["+extra+"]. No workers started.")
    if value["schema_version"] != FACTORY_VERSION:
        raise factory.ProposalError("Factory schema_version must be " + FACTORY_VERSION + ".")
    for key,(low,high) in FACTORY_BOUNDS.items():
        if type(value[key]) is not int or not low <= value[key] <= high:
            raise factory.ProposalError("Factory " + key + " must be an integer from " + str(low) + " through " + str(high) + "; booleans are not integers.")
    if not isinstance(value["reason"],str) or not value["reason"].strip() or len(value["reason"]) > 600:
        raise factory.ProposalError("Factory reason must be a nonempty string of at most 600 characters.")
    base = value["stable_units"] + value["candidate_units"]
    challenge = {"schema_version":factory.PROPOSAL_VERSION, "scenario":scenario,
        "parameters":{"stable_units":value["stable_units"], "candidate_units":value["candidate_units"],
            "extra_units":value["extra_units"], "initial_limit":base+value["extra_units"], "new_limit":base+value["headroom_units"]},
        "fault":"metrics_at_limit", "fault_after":5, "order":list(ORDER), "reason":value["reason"]}
    return validate_team_challenge(challenge, scenario)


def validate_team_challenge(value, scenario):
    challenge = factory.validate_proposal(value)
    mismatches = [key for key,expected in {"scenario":scenario,"fault":"metrics_at_limit","fault_after":5,"order":ORDER}.items()
                  if challenge[key] != expected]
    p = challenge["parameters"]
    base = p["stable_units"] + p["candidate_units"]
    if not (1 <= p["new_limit"]-base <= 3):
        mismatches.append("parameters.new_limit (requires 1-3 units of headroom)")
    if not (p["new_limit"] < base+p["extra_units"] <= p["initial_limit"]):
        mismatches.append("parameters.extra_units/initial_limit (original request must fit initially and exceed tightened headroom)")
    if mismatches:
        raise factory.ProposalError("Compiled challenge is outside the team profile: " + "; ".join(mismatches) + ". No workers started.")
    return service.validate_source(challenge)


class RoleClient(factory.NemotronProvider):
    """An independent model client and system instruction for one role only."""
    def __init__(self, role, api_key, max_output_tokens=10000, thinking=True):
        super().__init__(api_key, max_output_tokens, thinking)
        self.system_prompt = PROMPTS[role]
        self.mode = "LIVE NEBIUS NEMOTRON " + role.upper() + " AGENT"


class ScriptedRole:
    live = False
    request_settings = {}

    def __init__(self, role, scenario):
        self.role, self.scenario = role, scenario
        self.system_prompt = PROMPTS[role]
        self.mode = "SCRIPTED " + role.upper() + " RESPONSE; ZERO MODEL CALLS"

    def ask(self, context):
        if self.role == "factory":
            return sim.canonical(factory_fixture()), {"source": self.mode, "finish_reason": "stop", "refused": False}
        return service.OfflineAgent().ask_phase("repair" if self.role == "governance" else "recovery", context)


class Ledger:
    """Append-only-in-session handoffs, persisted before each role is invoked.

    Hash links detect inconsistent edits; they are not signatures or external
    attestation. No model can write to this ledger or approve a revision.
    """
    def __init__(self, path):
        self.path, self.events = Path(path), []
        factory.write_json(self.path, self.events)

    @staticmethod
    def valid(events):
        previous = None
        for index, event in enumerate(events, 1):
            if (not isinstance(event, dict) or event.get("sequence") != index
                    or event.get("previous_sha256") != previous):
                return False
            value = {key: val for key, val in event.items() if key != "sha256"}
            if event.get("sha256") != sim.digest(value):
                return False
            previous = event["sha256"]
        return True

    def verify(self):
        if not self.valid(self.events) or json.loads(self.path.read_text(encoding="utf-8")) != self.events:
            raise ValueError("Coordination handoffs changed; stop for review.")

    def add(self, sender, recipient, kind, payload):
        self.verify()
        event = {"sequence": len(self.events)+1, "sender": sender, "recipient": recipient, "kind": kind,
            "created_at": datetime.now(timezone.utc).isoformat(), "payload": copy.deepcopy(payload),
            "previous_sha256": self.events[-1]["sha256"] if self.events else None}
        event["sha256"] = sim.digest(event)
        self.events.append(event)
        factory.write_json(self.path, self.events)


def save(directory, summary, ledger):
    summary["handoffs"] = copy.deepcopy(ledger.events)
    calls = summary["calls"]
    summary["model_calls"] = sum(c["live"] for c in calls)
    summary["role_requests"] = len(calls)
    usage = [c.get("metadata", {}).get("total_tokens") for c in calls if c["live"]]
    known = [x for x in usage if type(x) is int and x >= 0]
    summary["usage"] = {"reported_tokens": sum(known), "calls_with_usage": len(known),
        "missing_usage_calls": summary["model_calls"]-len(known)}
    factory.write_json(directory/"summary.json", summary)
    esc = lambda value: html.escape(str(value), quote=True)
    cards = []
    for role, (part, label) in ROLES.items():
        attempt = next((c for c in calls if c["role"] == role), None)
        status = attempt["status"] if attempt else "NOT_REQUESTED"
        detail = summary.get("decisions", {}).get(role, {})
        reason = detail.get("reason", "")
        cards.append("<article><small>Coordinating agent</small><h2>" + esc(label) + " agent</h2><p><b>"
            + esc(status.replace("_", " ")) + "</b></p><p>" + esc(reason) + "</p>"
            + ("<a href='roles/" + role + "/request.json'>Input and role instructions</a> · <a href='roles/" + role + "/response.json'>Response</a>"
               if attempt and attempt["status"] == "RECEIVED" else "") + "</article>")
    rows = []
    for event in ledger.events:
        rows.append("<tr><td>" + str(event["sequence"]) + "</td><td>" + esc(event["sender"]) + " → " + esc(event["recipient"])
            + "</td><td>" + esc(event["kind"].replace("_", " ")) + "<details><summary>Recorded handoff</summary><pre>"
            + esc(json.dumps(event["payload"], indent=2)) + "</pre></details></td></tr>")
    outcome = summary.get("outcome", {})
    page = ("<!doctype html><html lang='en'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>Orkestryn · Three-agent team</title>" + factory.STYLE
        + "<style>.cards{grid-template-columns:repeat(auto-fit,minmax(245px,1fr))}.cards h2{margin-top:8px}small{color:#69dccf}</style>"
        + "<h1>Three agents. One shared service.</h1><p>" + esc(summary["mode"]) + "</p><p><b>"
        + esc(summary["status"].replace("_", " ")) + "</b> · " + esc(summary["role_requests"])
        + "/3 role requests · " + esc(summary["model_calls"]) + " paid model calls.</p><div class='cards'>" + "".join(cards) + "</div>"
        + "<p>The factory's validated case creates the service state reviewed by governance. Recovery receives the approved revision, confirmed execution, and durable pause. The coordinator enforces the tools and exact manager consent.</p>"
        + ("<h2>Observed outcome</h2><pre>" + esc(json.dumps(outcome, indent=2)) + "</pre>" if outcome else "")
        + ("<p>" + esc(summary["error"]) + "</p>" if summary.get("error") else "")
        + "<h2>Coordination record</h2><table><thead><tr><th>#</th><th>Handoff</th><th>Decision</th></tr></thead><tbody>"
        + "".join(rows) + "</tbody></table><h2>Checks</h2><pre>" + esc(json.dumps(summary.get("checks", {}), indent=2)) + "</pre><p>"
        + ("<a href='service/index.html'>Execution and recovery evidence</a> · " if (directory/"service"/"index.html").is_file() else "")
        + "<a href='summary.json'>Full team result</a> · <a href='coordination.json'>Handoff records</a></p>"
        + "<p>One bounded scenario per run. Scripted replies do not measure model accuracy. Real local arithmetic workers and HTTP; fixture capacity and identities. Retained work at the policy outcome is captured before separate harness teardown. No persistent supervisor.</p></html>")
    (directory/"index.html").write_text(page, encoding="utf-8")


class Coordinator:
    def __init__(self, directory, providers, scenario, summary, ledger):
        self.directory, self.providers, self.scenario = directory, providers, scenario
        self.summary, self.ledger = summary, ledger
        self.initial_digest = implementation_digest()
        self.validated = False
        self.consented = False

    def verify(self):
        self.ledger.verify()
        if implementation_digest() != self.initial_digest:
            raise ValueError("Team implementation changed during this run; stop for review.")

    def call(self, role, context):
        self.verify()
        expected = list(ROLES)[len(self.summary["calls"])] if len(self.summary["calls"]) < 3 else None
        if role != expected:
            raise factory.GenerationError("Team request order or three-call ceiling violated; no retry.")
        provider = self.providers[role]
        path = self.directory/"roles"/role
        path.mkdir(parents=True, exist_ok=False)
        entry = {"role": role, "live": provider.live, "status": "REQUEST_STARTED", "context_sha256": sim.digest(context)}
        self.summary["calls"].append(entry)
        self.summary["status"] = role.upper() + "_REQUESTED"
        self.ledger.add("coordinator", role, "decision_requested", {"context_sha256": entry["context_sha256"],
            "input": "roles/"+role+"/request.json", "problem": ROLES[role][0]})
        factory.write_json(path/"request.json", {"role": role, "system": PROMPTS[role], "context": context,
            "request_settings": provider.request_settings, "model_requested": factory.inference.MODEL if provider.live else None})
        save(self.directory, self.summary, self.ledger)
        print("Team: " + ROLES[role][1] + " agent", flush=True)
        try:
            content, metadata = provider.ask(copy.deepcopy(context))
            entry.update(status="RECEIVED", metadata=metadata)
            self.summary["status"] = role.upper() + "_REPLY_RECEIVED"
            factory.write_json(path/"response.json", {"visible_content": content[:factory.MAX_RESPONSE_BYTES] if isinstance(content,str) else None,
                "metadata": metadata, "content_truncated_for_storage": isinstance(content,str) and len(content)>factory.MAX_RESPONSE_BYTES})
            if metadata.get("refused") or metadata.get("finish_reason") != "stop":
                raise factory.GenerationError("Unfinished or refused " + role + " reply; no retry or substitute.")
            self.verify()
            self.ledger.add(role, "coordinator", "decision_proposed", {"response_sha256": sim.digest(content),
                "response": "roles/"+role+"/response.json"})
            return content, metadata
        except BaseException as exc:
            entry.update(status="INTERRUPTED" if isinstance(exc, KeyboardInterrupt) else "ERROR", error_type=type(exc).__name__)
            raise
        finally:
            save(self.directory, self.summary, self.ledger)

    def ask_phase(self, phase, context):
        if not self.validated:
            raise factory.GenerationError("No independently validated factory handoff.")
        if phase == "repair":
            role = "governance"
        elif phase == "recovery":
            role = "recovery"
            if (not self.consented or not context.get("workflow_failure_recorded")
                    or "deploy-A" not in context.get("current_state", {}).get("paused_runs", [])
                    or context.get("execution", {}).get("status") != "SUCCEEDED"
                    or not context.get("approved_recovery_scope")):
                raise factory.GenerationError("Recovery requires recorded consent, confirmed execution and durable pause.")
            self.ledger.add("coordinator", "recovery", "approved_execution_handoff", {
                "execution": context["execution"], "grant": context["approved_recovery_scope"],
                "based_on_state": context["based_on_state"], "workflow_durably_paused": True})
        else:
            raise factory.GenerationError("Unknown team phase.")
        content, metadata = self.call(role, context)
        # Parse once here for the coordination record, and again in the existing
        # engine's strict gate before any proposal can bind or action can run.
        decision = (revision.validate_proposal(factory.decode(content)) if role == "governance"
                    else service.parse_recovery(content, context["based_on_state"]))
        self.summary["decisions"][role] = decision
        return content, metadata

    @property
    def live(self):
        return self.providers["governance"].live

    @property
    def mode(self):
        return self.summary["mode"]

    @property
    def request_settings(self):
        return self.providers["governance"].request_settings

    def consent(self, record, report, callback, open_review):
        self.verify()
        self.summary["status"] = "AWAITING_EXACT_MANAGER_CONSENT"
        self.ledger.add("governance", "coordinator", "revision_rehearsed", {"revision_id": record["revision_id"],
            "proposal": record["proposal"], "rehearsal_passed": record["rehearsal"]["passed"]})
        save(self.directory, self.summary, self.ledger)
        if open_review:
            webbrowser.open((self.directory/"index.html").as_uri())
        approved = callback(record, report)
        self.verify()  # includes drift during the human's review
        self.consented = approved is True
        self.ledger.add("manager" if self.live else "scripted_manager_fixture", "coordinator", "exact_consent_recorded", {
            "approved": self.consented, "revision_id": record["revision_id"],
            "additional_units": record["proposal"]["additional_units"], "includes_bounded_recovery": self.consented})
        self.summary["status"] = "EXACT_CONSENT_RECORDED" if self.consented else "CONSENT_DECLINED"
        save(self.directory, self.summary, self.ledger)
        return self.consented


def run_team(output, providers=None, scenario="dependency", consent=None, open_review=False):
    if scenario not in ("routing", "dependency"):
        raise ValueError("Choose routing or dependency.")
    providers = providers or {role: ScriptedRole(role, scenario) for role in ROLES}
    if set(providers) != set(ROLES) or len({p.live for p in providers.values()}) != 1:
        raise ValueError("Supply exactly the three roles, all scripted or all live.")
    if any(getattr(p, "system_prompt", None) != PROMPTS[role] for role,p in providers.items()):
        raise ValueError("Each role must use its registered instructions.")
    directory = service.new_directory(output)
    directory.mkdir(parents=True, exist_ok=False)
    ledger = Ledger(directory/"coordination.json")
    live = providers["factory"].live
    summary = {"version": VERSION, "created_at": datetime.now(timezone.utc).isoformat(),
        "platform": platform.system(), "python_version": platform.python_version(),
        "mode": "LIVE NEBIUS THREE-AGENT TEAM; EXACT TERMINAL CONSENT" if live else "SCRIPTED THREE-AGENT DEMO; REAL WORKERS/HTTP; ZERO MODEL CALLS",
        "scenario": scenario, "status": "STARTING", "calls": [], "decisions": {}, "checks": {},
        "factory_contract_version": FACTORY_VERSION,
        "live_call_ceiling": 3 if live else 0, "role_order": list(ROLES),
        "scope": "One registered known-template scenario, independent validation, sequential role handoffs, no session resume or hidden retry."}
    coordinator = Coordinator(directory, providers, scenario, summary, ledger)
    summary["implementation_digest"] = coordinator.initial_digest
    callback = consent if consent is not None else service.terminal_consent if live else lambda *_:True
    save(directory, summary, ledger)
    try:
        content, _ = coordinator.call("factory", factory_context(scenario))
        proposal = factory.decode(content)
        challenge, baseline = compile_factory_proposal(proposal, scenario)
        summary["decisions"]["factory"] = proposal
        factory.write_json(directory/"factory_proposal.json", proposal)
        factory.write_json(directory/"challenge.json", challenge)
        factory.write_json(directory/"factory_baseline.json", baseline)
        coordinator.validated = True
        ledger.add("coordinator", "governance", "validated_factory_case", {"factory_proposal_sha256": sim.digest(proposal),
            "factory_contract_version": FACTORY_VERSION, "challenge_sha256": sim.digest(challenge),
            "scenario": challenge["scenario"], "independent_baseline_passed": baseline["passed"],
            "delivery": "Validated Factory numbers compiled by the fixed coordinator mapping into a rehearsed challenge. Fresh local runtime delivers current state to governance. Factory narrative and baseline labels are not forwarded."})
        summary["status"] = "FACTORY_VALIDATED"
        save(directory, summary, ledger)
        report = service.run_agent(directory/"service", coordinator, challenge,
            lambda record, current: coordinator.consent(record, current, callback, open_review), review_refresh=True)
        summary["status"] = report["stage"]
        if report.get("error"):
            summary["error"] = report["error"]
        if report.get("approval_probe"):
            certificate = report["approval_probe"]
            summary["approval_revalidation"] = {"certificate_sha256":sim.digest(certificate),
                "metrics":certificate["metrics"], "rehearsal_passed":certificate["rehearsal"]["passed"],
                "scope":"Fresh probe after human review; original state and exact model proposal unchanged."}
        summary["outcome"] = {"stage": report["stage"], "recovery_status": report.get("recovery", {}).get("status"),
            "committed_units": report.get("committed_units"), "capacity_limit": report.get("capacity_limit"),
            "availability": report.get("availability"), "workflow_paused": "deploy-A" in report.get("state", {}).get("paused_runs", []),
            "harness_teardown": report.get("teardown"), "error": report.get("error")}
        summary["checks"]["service_checks_passed"] = report.get("checks_passed") is True
        summary["checks"]["all_workers_stopped_after_outcome"] = report.get("teardown", {}).get("all_workers_stopped") is True
        coordinator.verify()
        ledger.add("coordinator", "reviewer", "outcome_recorded", summary["outcome"])
    except (factory.GenerationError, factory.ProposalError, ValueError, OSError) as exc:
        summary.update(status="STOPPED_FOR_REVIEW", error=type(exc).__name__ + ": " + str(exc))
    except KeyboardInterrupt:
        summary.update(status="INTERRUPTED", error="Interrupted; inspect saved state and the separate harness teardown record.")
    finally:
        try:
            ledger.verify()
            ledger_valid = True
        except (OSError, ValueError):
            ledger_valid = False
        unchanged = implementation_digest() == coordinator.initial_digest
        summary["checks"].update(ledger_consistent=ledger_valid, implementation_unchanged=unchanged,
            role_order_and_ceiling=[c["role"] for c in summary["calls"]] == list(ROLES)[:len(summary["calls"])] and len(summary["calls"])<=3)
        if not ledger_valid or not unchanged:
            summary["status"] = "STOPPED_FOR_REVIEW"
        summary["full_three_role_flow_completed"] = (summary["status"] == "RECOVERED" and len(summary["decisions"])==3
            and all(summary["checks"].values()))
        save(directory, summary, ledger)
    return directory, summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--scenario", choices=("routing", "dependency"), default="dependency")
    parser.add_argument("--max-output-tokens", type=int, default=10000)
    parser.add_argument("--thinking", choices=("on", "off"), default="on")
    parser.add_argument("--output", type=Path, default=ROOT/"agent_team_runs")
    parser.add_argument("--open", action="store_true")
    args = parser.parse_args()
    if not 1024 <= args.max_output_tokens <= 10000:
        parser.error("Use 1024-10000 output tokens per request.")
    providers = None
    if args.live:
        if not terminal_guard.interactive_input():
            parser.error("Live team mode needs an interactive terminal for exact consent.")
        print("LIVE team: at most 3 paid requests, one per role, up to %s output tokens each. No retries." % args.max_output_tokens, flush=True)
        key = (os.environ.get("ORKESTRYN2_NEBIUS_API_KEY") or getpass("Paste the Nebius API key locally: ")).strip()
        if not key:
            parser.error("No API key supplied; no request made.")
        providers = {role:RoleClient(role, key, args.max_output_tokens, args.thinking=="on") for role in ROLES}
    directory, result = run_team(args.output, providers, args.scenario, open_review=args.open)
    print("Team: %s | role requests: %s/3 | model calls: %s" % (result["status"], result["role_requests"], result["model_calls"]))
    if result.get("error"):
        print("Reason: " + result["error"], file=sys.stderr)
    print("Report: " + str(directory/"index.html"))
    if args.open:
        webbrowser.open((directory/"index.html").as_uri())
    return 0 if result["full_three_role_flow_completed"] else 1




if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (factory.GenerationError, ValueError, OSError) as exc:
        print("TEAM STOPPED: " + str(exc), file=sys.stderr)
        raise SystemExit(1)
    except KeyboardInterrupt:
        print("Team interrupted; saved evidence retained.", file=sys.stderr)
        raise SystemExit(130)
