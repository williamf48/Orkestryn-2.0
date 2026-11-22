#!/usr/bin/env python3
"""Reconnect to leased local arithmetic workers and resolve real owner review.

One supervisor owns the runtime and the SQLite coordinator. Short-lived CLI
clients can disconnect without stopping it. A supervisor crash is deliberately
not rebound to another runtime: last evidence stays unknown until inspected.
No model calls, cloud resources or production identities are introduced here.
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import hashlib
import hmac
import html
import http.client
import io
import json
import os
import platform
import re
import secrets
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
import webbrowser
from datetime import datetime, timezone
from http.server import HTTPServer
from pathlib import Path
from unittest import mock

import local_service as local
import native_process
import restart_checker as restart
import service_agent as service
import simulator as sim
import team_workspace as workspace
import terminal_guard

VERSION = "agent-rehearsal-runtime-sessions-v1"
MODE = "SCRIPTED LOCAL RUNTIME CONTINUATION; REAL WORKERS/HTTP; ZERO MODEL CALLS"
ROOT = Path(__file__).resolve().parent
FOLDER = "runtime_session_runs"
DEMO_FOLDER = "runtime_session_demo_runs"
SCENARIOS = {"routing", "dependency"}
SOURCES = {"EXPLICIT TERMINAL FIXTURE CONSENT", "SCRIPTED LOCAL LAB FIXTURE"}
DEPENDENCY = "C-legitimate-job"
MAX_ACTIVE = 2


def now():
    return datetime.now(timezone.utc).isoformat()


def implementation_digest():
    return sim.digest({"version": VERSION, "runtime_sessions.py": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                       "core_controller": service.controller_digest(), "native_process": hashlib.sha256((ROOT/"native_process.py").read_bytes()).hexdigest()})


def component(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,159}", value):
        raise ValueError("Use a registered local session identifier.")
    return value


def directory_for(output, session_id=None):
    root = Path(output).absolute()
    if root.resolve() != root:
        raise ValueError("Session folders must not use symbolic links.")
    directory = root / component(session_id) if session_id is not None else root
    if directory.resolve() != directory:
        raise ValueError("Session evidence must stay in its own folder.")
    return directory


def read_file(directory, name):
    path = Path(directory) / name
    if path.resolve() != path or not path.is_file():
        raise ValueError("Session evidence is missing or linked outside its folder.")
    return workspace.read_json(path)


def protection(state):
    return {key: copy.deepcopy(state[key]) for key in ("stable_units", "capacity_limit", "budget_version")} | {
        "B_plan": copy.deepcopy(state["plans"]["cost-B"]), "B_approval": copy.deepcopy(state["approvals"]["AP-cost-B"])}


def decision_phrase(session_id, decision, digest):
    labels = {"retain": "RETAIN C WORK", "withdraw_route": "WITHDRAW C ROUTE", "release_dependency": "RELEASE C DEPENDENCY"}
    if decision not in labels:
        raise ValueError("Choose a bounded C decision.")
    return labels[decision] + " " + component(session_id) + " " + digest[:12]


def stop_phrase(session_id):
    return "STOP LOCAL SESSION " + component(session_id)


def snapshot_digest(state, events, actual):
    return sim.digest({"state": state, "journal_head": events[-1]["sequence"] if events else 0,
                       "runtime": service.runtime_view(actual)})


def saved_session(directory):
    """Read only public evidence. Never read the local access capability here."""
    directory = Path(directory).absolute()
    data = read_file(directory, "summary.json")
    if (data.get("version") != VERSION or data.get("session_id") != directory.name
            or data.get("mode") != MODE or data.get("model_calls") != 0):
        raise ValueError("Unsupported session evidence.")
    snapshot = data.get("policy_snapshot")
    if snapshot:
        workspace.validate_snapshot(snapshot)
        expected = snapshot_digest(snapshot["state"], snapshot["events"], data["actual_at_outcome"])
        if expected != data.get("current_digest"):
            raise ValueError("The session state, journal or runtime snapshot changed.")
    result = copy.deepcopy(data)
    result["historical_controller"] = data["implementation_digest"] != implementation_digest()
    result["heartbeat_stale"] = data["lifecycle"] != "STOPPED" and time.time() - data["heartbeat_unix"] > 10
    result["liveness_scope"] = "Saved observation only; use the terminal status command to contact this supervisor."
    return result


def create_session(output, scenario, ttl):
    if scenario not in SCENARIOS or type(ttl) is not int or not 1 <= ttl <= 3600:
        raise ValueError("Choose routing/dependency and a bounded lease of at most one hour.")
    root = directory_for(output)
    root.mkdir(parents=True, exist_ok=True)
    registry = root / ".launch-registry.sqlite3"
    if registry.resolve() != registry:
        raise ValueError("The session launch registry cannot be a symbolic link.")
    db = sqlite3.connect(registry, timeout=5)
    try:
        db.execute("CREATE TABLE IF NOT EXISTS launch_guard (id INTEGER PRIMARY KEY)")
        db.execute("BEGIN IMMEDIATE")
        return create_under_lock(root, scenario, ttl)
    finally:
        db.commit()
        db.close()


def create_under_lock(root, scenario, ttl):
    active = []
    for path in root.iterdir():
        if path.is_dir() and not path.name.startswith("."):
            try:
                spec = read_file(path, "session.json")
                summary = read_file(path, "summary.json") if (path / "summary.json").exists() else {}
                failure = read_file(path, "launch_failure.json") if (path / "launch_failure.json").exists() else {}
                ended_start = failure.get("supervisor_exit_confirmed") is True or failure.get("supervisor_started") is False
                if summary.get("lifecycle") != "STOPPED" and not ended_start and spec["expires_unix"] > time.time():
                    active.append(path.name)
            except (ValueError, OSError, KeyError):
                continue
    if len(active) >= MAX_ACTIVE:
        raise ValueError("Two unexpired sessions already exist. Inspect or stop them first: " + ", ".join(active))
    session_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    directory = directory_for(root, session_id)
    directory.mkdir(exist_ok=False)
    spec = {"version": VERSION, "session_id": session_id, "scenario": scenario, "created_at": now(),
            "lease_seconds": ttl, "expires_unix": time.time() + ttl, "implementation_digest": implementation_digest()}
    workspace.write_json(directory / "session.json", spec)
    return directory


class SessionHost:
    """Only this supervisor thread writes policy; the runtime owns all workers."""
    def __init__(self, directory):
        self.directory = Path(directory).absolute()
        self.spec = read_file(self.directory, "session.json")
        if (self.spec.get("version") != VERSION or self.spec.get("session_id") != self.directory.name
                or self.spec.get("scenario") not in SCENARIOS or self.spec.get("implementation_digest") != implementation_digest()
                or type(self.spec.get("lease_seconds")) is not int or not 1 <= self.spec["lease_seconds"] <= 3600):
            raise ValueError("Session source or registration changed before startup.")
        self.deadline = time.monotonic() + max(0, min(self.spec["lease_seconds"], self.spec["expires_unix"] - time.time()))
        if self.expired():
            raise ValueError("The session lease expired before startup.")
        claim = self.directory / ".supervisor-claim"
        with claim.open("x", encoding="utf-8") as stream:
            stream.write(str(os.getpid()))
        if (self.directory / "coordinator.sqlite3").exists():
            raise ValueError("A previous runtime cannot be replaced or silently rebound.")
        self.runtime = self.engine = self.server = None
        self.stopping = False
        self.teardown = None
        self.stop_reason = None
        self.token = secrets.token_urlsafe(32)
        try:
            challenge, baseline = service.validate_source(service.fixture_challenge(self.spec["scenario"]))
            workspace.write_json(self.directory / "challenge.json", challenge)
            workspace.write_json(self.directory / "source_baseline.json", baseline)
            self.runtime = local.LocalRuntime()
            self.engine = local.LocalEngine(self.directory / "coordinator.sqlite3", local.RuntimeClient(self.runtime))
            self.engine.db.execute("CREATE TABLE session_reviews (decision_key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, request TEXT NOT NULL, phase TEXT NOT NULL, result TEXT NOT NULL)")
            program, trace = service.setup_local(self.engine, self.runtime, challenge)
            workspace.write_json(self.directory / "local_program.json", program)
            workspace.write_json(self.directory / "setup_trace.json", trace)
            self.initial_protection = protection(self.engine.state())
            # The session starts at the same paused recovery boundary as the
            # completed demos. Here the physical workers remain under a lease.
            self.engine.recover()
            self.publish()
        except BaseException:
            self.close("STARTUP_FAILED")
            raise

    def expired(self):
        return time.monotonic() >= self.deadline

    def guard(self):
        sim.require(not self.expired() and not self.stopping, "SESSION_EXPIRED", "No new operation after the lease or shutdown.")
        sim.require(implementation_digest() == self.spec["implementation_digest"], "SESSION_SOURCE_CHANGED", "Session/controller source changed; inspect evidence and stop this session.")
        actual = self.engine.client.state()
        state = self.engine.state()
        sim.require(actual["runtime_id"] == self.runtime.identity, "RUNTIME_IDENTITY_CHANGED", "Never bind old commitments to a replacement runtime.")
        sim.require(protection(state) == self.initial_protection, "PROTECTED_WORK_CHANGED", "B's approved work changed; stop for review.")
        sim.require(actual["candidate_percent"] == state["routing"]["candidate-v2"] and actual["route_owner"] == state["route_owner"],
                    "RUNTIME_STATE_DRIFT", "Physical routing and confirmed policy disagree.")
        return state, actual

    def choices(self):
        state = self.engine.state()
        if self.engine.pending():
            return []
        candidate = state.get("deployments", {}).get("candidate-v2", {})
        if "deploy-A" not in state["paused_runs"] or candidate.get("allocated_units", 0) == 0:
            return []
        scenario = self.spec["scenario"]
        if scenario == "routing" and state["route_owner"] == "route-C" and state["routing"]["candidate-v2"] > 0:
            return ["retain", "withdraw_route"]
        if scenario == "dependency" and {"id": DEPENDENCY, "owner_run": "route-C"} in candidate.get("dependencies", []):
            return ["retain", "release_dependency"]
        return []

    def describe(self):
        e = self.engine
        snapshot = workspace.snapshot_from_db(self.directory / "coordinator.sqlite3")
        actual = self.runtime.snapshot()
        reviews = [{"decision_key": key, "phase": phase, "result": workspace.decode(raw)}
                   for key, phase, raw in e.db.execute("SELECT decision_key,phase,result FROM session_reviews ORDER BY rowid")]
        recovery = next((event["report"] for event in reversed(snapshot["events"]) if event.get("kind") == "RECOVERY_REPORT"), None)
        options = self.choices()
        pending = [r["decision_key"] for r in reviews if r["phase"] in {"OWNER_PENDING", "RECOVERY_PENDING"}]
        status = "RECONCILIATION_REQUIRED" if pending or e.pending() else "AWAITING_OWNER_REVIEW" if options else "RECOVERED_SERVICE_ACTIVE" if recovery and recovery["status"] == "VERIFIED_RECOVERED" else "REVIEW_REQUIRED"
        matches = actual["candidate_percent"] == snapshot["state"]["routing"]["candidate-v2"] and actual["route_owner"] == snapshot["state"]["route_owner"]
        unchanged = implementation_digest() == self.spec["implementation_digest"]
        if not unchanged:
            status = "SESSION_SOURCE_CHANGED"
        elif not matches and not pending and not e.pending():
            status = "RUNTIME_STATE_DRIFT"
        return {"version": VERSION, "session_id": self.directory.name, "mode": MODE, "model_calls": 0,
            "scenario": self.spec["scenario"], "created_at": self.spec["created_at"], "expires_unix": self.spec["expires_unix"],
            "lease_seconds": self.spec["lease_seconds"], "heartbeat_at": now(), "heartbeat_unix": time.time(),
            "supervisor_pid": os.getpid(), "runtime_id": self.runtime.identity,
            "implementation_digest": self.spec["implementation_digest"], "source_unchanged": unchanged,
            "lifecycle": "STOPPED" if self.teardown is not None else "RUNNING", "status": status,
            "policy_snapshot": snapshot, "actual_at_outcome": actual,
            "worker_endpoints": {name: worker.port for name, worker in self.runtime.workers.items()},
            "physical_route_matches_policy": matches,
            "current_digest": snapshot_digest(snapshot["state"], snapshot["events"], actual),
            "available_decisions": [] if self.teardown is not None or pending else options,
            "recovery": recovery, "owner_reviews": reviews, "pending_reviews": pending,
            "metrics": workspace.metrics(snapshot["state"]), "teardown": self.teardown,
            "scope": "Real local arithmetic workers/HTTP; fixture capacity and C authority. A coordinator client can reconnect while this supervisor lives. This is not supervisor-crash recovery, model inference or production owner authentication."}

    def publish(self):
        result = self.describe()
        workspace.write_json(self.directory / "summary.json", result)
        return result

    def save_review(self, key, phase, result):
        with self.engine.db:
            self.engine.db.execute("UPDATE session_reviews SET phase=?,result=? WHERE decision_key=?", (phase, sim.canonical(result), key))
            self.engine._event({"kind": "RUNTIME_OWNER_REVIEW_RESULT", "decision_key": key, "phase": phase, "result": result, "changes": {}})
        self.publish()
        return result

    def finish_review(self, key, owner_result):
        if owner_result.get("status") != "SUCCEEDED":
            result = {"session_id": self.directory.name, **owner_result, "decision_key": key}
            phase = "OWNER_PENDING" if self.engine.pending() else "FINISHED"
            return self.save_review(key, phase, result)
        self.guard()
        recovery = self.engine.recover()
        phase = "RECOVERY_PENDING" if self.engine.pending() else "FINISHED"
        result = {"session_id": self.directory.name, "decision_key": key,
            "status": "OUTCOME_UNKNOWN" if phase == "RECOVERY_PENDING" else "SUCCEEDED",
            "code": "RECONCILIATION_REQUIRED" if phase == "RECOVERY_PENDING" else "OWNER_DECISION_RECORDED",
            "owner_result": owner_result, "recovery": recovery}
        return self.save_review(key, phase, result)

    def review(self, request):
        required = {"session_id", "decision_key", "decision", "note", "expected_digest", "consent", "consent_source"}
        if not isinstance(request, dict) or set(request) != required or request["session_id"] != self.directory.name:
            raise ValueError("Use an exact registered owner-review request.")
        key = component(request["decision_key"])
        fingerprint = sim.digest(request)
        prior = self.engine.db.execute("SELECT fingerprint,phase,result FROM session_reviews WHERE decision_key=?", (key,)).fetchone()
        if prior:
            sim.require(prior[0] == fingerprint, "REVIEW_KEY_CONFLICT", "This review key already has different content.")
            return {**workspace.decode(prior[2]), "replayed": True}
        state, actual = self.guard()
        sim.require(not self.engine.pending(), "RECONCILIATION_REQUIRED", "Reconcile the existing effect before a new decision.")
        sim.require(request["expected_digest"] == snapshot_digest(state, self.engine.events(), actual), "STALE_REVIEW_STATE", "Inspect the current session and obtain new consent.")
        sim.require(request["decision"] in self.choices(), "REVIEW_SCOPE_MISMATCH", "This C decision is not available for the current retained work.")
        sim.require(isinstance(request["note"], str) and 0 < len(request["note"].strip()) <= 300, "REVIEW_NOTE_REQUIRED", "Record a reason of 1 to 300 characters.")
        sim.require(request["consent_source"] in SOURCES and request["consent"] == decision_phrase(self.directory.name, request["decision"], request["expected_digest"]),
                    "EXACT_OWNER_CONSENT_REQUIRED", "The exact displayed C decision and snapshot require separate consent.")
        tool = "set_route" if self.spec["scenario"] == "routing" else "add_dependency"
        plan = self.engine._approval(state, "C", "route-C", "AP-route-C", tool)
        sim.require(plan.get("candidate") == "candidate-v2" and (tool != "add_dependency" or plan.get("can_depend")),
                    "APPROVAL_SCOPE_MISMATCH", "C's approval must cover this exact work.")
        with self.engine.db:
            self.engine.db.execute("INSERT INTO session_reviews VALUES(?,?,?,'OWNER_PENDING',?)", (key, fingerprint, sim.canonical(request), sim.canonical({"status": "OUTCOME_UNKNOWN", "code": "OWNER_INTENT_RECORDED"})))
            self.engine._event({"kind": "RUNTIME_OWNER_REVIEW_INTENT", "decision_key": key, "request": request, "changes": {}})
        try:
            if request["decision"] == "retain":
                result = {"session_id": self.directory.name, "decision_key": key, "status": "SUCCEEDED", "code": "RETAINED_NO_EFFECTS"}
                return self.save_review(key, "FINISHED", result)
            if request["decision"] == "withdraw_route":
                # Review latency is not treated as fresh measurements. Measure
                # after consent, then check that only observations changed.
                metrics = self.engine.measure()
                current, physical = self.guard()
                before_without_metrics, after_without_metrics = copy.deepcopy(state), copy.deepcopy(current)
                for value in (before_without_metrics, after_without_metrics):
                    value["deployments"]["candidate-v2"].pop("metrics", None)
                sim.require(before_without_metrics == after_without_metrics and service.runtime_view(actual) == service.runtime_view(physical),
                            "STALE_REVIEW_STATE", "Work changed during the post-consent probe; new consent is required.")
                sim.require(metrics["correctness"] == 100 and metrics["error_rate"] == 0, "FRESH_PROBE_FAILED", "Keep C's route when the probe fails.")
                owner_result = sim.action(self.engine, "set_route", {"candidate_id": "candidate-v2", "candidate_percent": 0,
                    "route_version": current["route_version"]}, "owner-" + key, actor="C", run="route-C")
            else:
                with self.engine.db:
                    current, _ = self.guard()
                    after = copy.deepcopy(current)
                    owned = {"id": DEPENDENCY, "owner_run": "route-C"}
                    sim.require(owned in after["deployments"]["candidate-v2"]["dependencies"], "DEPENDENCY_OWNER_CONFLICT", "Release only the exact C-owned dependency.")
                    after["deployments"]["candidate-v2"]["dependencies"].remove(owned)
                    self.engine.record_state(current, after, {"kind": "RUNTIME_OWNER_DEPENDENCY_RELEASE", "decision_key": key,
                        "actor": "C", "run_id": "route-C", "approval_id": "AP-route-C", "dependency": owned,
                        "note": "Exact local fixture consent; all other dependencies remain."})
                owner_result = {"status": "SUCCEEDED", "code": "EXACT_C_DEPENDENCY_RELEASED"}
            return self.finish_review(key, owner_result)
        except (sim.Denied, ValueError, OSError, http.client.HTTPException) as exc:
            result = {"session_id": self.directory.name, "decision_key": key, "status": "OUTCOME_UNKNOWN" if self.engine.pending() else "BLOCKED",
                      "code": getattr(exc, "code", type(exc).__name__), "reason": str(exc)}
            return self.save_review(key, "OWNER_PENDING" if self.engine.pending() else "FINISHED", result)

    def resume(self, key):
        component(key)
        row = self.engine.db.execute("SELECT phase,result FROM session_reviews WHERE decision_key=?", (key,)).fetchone()
        sim.require(row is not None, "REVIEW_MISSING", "No saved owner intent has this key.")
        if row[0] == "FINISHED":
            return {**workspace.decode(row[1]), "replayed": True}
        # A pending external route can legitimately differ from confirmed
        # policy. Reconcile its exact recorded receipt before the drift guard.
        sim.require(not self.expired() and implementation_digest() == self.spec["implementation_digest"], "SESSION_SOURCE_OR_LEASE_CHANGED", "Stop for review.")
        result = self.engine.reconcile()
        if result["status"] != "SUCCEEDED":
            return {"session_id": self.directory.name, "decision_key": key, **result}
        self.guard()
        owner = result if row[0] == "OWNER_PENDING" else workspace.decode(row[1]).get("owner_result", {"status": "SUCCEEDED"})
        return self.finish_review(key, owner)

    def probe(self):
        self.guard()
        samples = [self.engine.client.infer() for _ in range(10)]
        return {"session_id": self.directory.name, "requests": 10, "successful": sum(s.get("ok") is True for s in samples),
                "samples": samples, "scope": "Ten measured local arithmetic requests, not model inference."}

    def close(self, reason):
        if self.teardown is not None:
            return self.teardown
        self.stop_reason = reason
        before = None
        capture_error = None
        try:
            if self.engine is not None and hasattr(self, "initial_protection"):
                before = self.publish()
        except Exception as exc:
            capture_error = type(exc).__name__
        try:
            cleanup = self.runtime.close() if self.runtime is not None else []
            self.teardown = {"reason": reason, "workers": cleanup, "all_workers_stopped": bool(cleanup) and all(x["stopped"] for x in cleanup),
                "captured_at": now(), "capture_error": capture_error,
                "scope": "Explicit session or lease teardown; never counted as policy cleanup or released commitments."}
            workspace.write_json(self.directory / "teardown.json", self.teardown)
            if self.engine is not None and before is not None:
                # Preserve the policy outcome's physical snapshot before
                # teardown; a separate record proves subsequent owned exits.
                before.update(lifecycle="STOPPED", available_decisions=[], teardown=self.teardown, heartbeat_at=now(), heartbeat_unix=time.time())
                workspace.write_json(self.directory / "summary.json", before)
        finally:
            if self.engine is not None:
                self.engine.close()
        return self.teardown


def serve(directory):
    host = SessionHost(directory)
    server = None
    class Handler(local.JsonHandler):
        def authorized(self):
            valid = (self.headers.get("Host") == "127.0.0.1:%s" % server.server_port and not self.headers.get("Origin")
                     and hmac.compare_digest(self.headers.get("X-Rehearsal-Token", ""), host.token))
            if not valid:
                self.send_json({"error": "Local session capability and terminal client required"}, 403)
            return valid

        def do_GET(self):
            if not self.authorized():
                return
            if self.path != "/status":
                return self.send_json({"error": "Unknown session endpoint"}, 404)
            self.send_json(host.publish())

        def do_POST(self):
            if not self.authorized():
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 4096 or self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                    raise ValueError("Use a small exact session request.")
                self.connection.settimeout(3)
                request = workspace.decode(self.rfile.read(size).decode("utf-8"))
                if not isinstance(request, dict) or request.get("session_id") != host.directory.name:
                    raise ValueError("Wrong session identity.")
                if self.path == "/review":
                    result = host.review(request)
                elif self.path == "/resume" and set(request) == {"session_id", "decision_key"}:
                    result = host.resume(request["decision_key"])
                elif self.path == "/probe" and set(request) == {"session_id"}:
                    result = host.probe()
                elif self.path == "/stop" and set(request) == {"session_id", "consent"} and request["consent"] == stop_phrase(host.directory.name):
                    host.stopping = True
                    result = {"session_id": host.directory.name, "status": "STOPPING", "code": "TEARDOWN_REQUESTED"}
                else:
                    raise ValueError("Unsupported local session operation or consent.")
                self.send_json(result)
            except sim.Denied as exc:
                self.send_json({"session_id": host.directory.name, "status": "BLOCKED", "code": exc.code, "reason": exc.reason})
            except (OSError, ValueError, KeyError, TypeError, http.client.HTTPException) as exc:
                self.send_json({"error": str(exc)}, 400)

        def do_PUT(self):
            self.send_json({"error": "Unsupported method"}, 405)

        do_DELETE = do_PATCH = do_PUT
    reason = "SUPERVISOR_ERROR"
    try:
        server = HTTPServer(("127.0.0.1", 0), Handler)
        server.timeout = 0.25
        access = {"version": VERSION, "session_id": host.directory.name, "pid": os.getpid(),
                  "runtime_id": host.runtime.identity, "port": server.server_port, "token": host.token, "os_identity": native_process.os_process(os.getpid())["identity"]}
        workspace.write_json(host.directory / ".access.json", access)
        (host.directory / ".access.json").chmod(0o600)
        host.publish()
        last = time.monotonic()
        while not host.stopping and not host.expired():
            server.handle_request()
            if time.monotonic() - last >= 1:
                host.publish()
                last = time.monotonic()
        reason = "EXPLICIT_SESSION_SHUTDOWN" if host.stopping else "LEASE_EXPIRED"
    finally:
        if server is not None:
            server.server_close()
        host.close(reason)


class SessionClient:
    def __init__(self, directory):
        self.directory = Path(directory).absolute()
        access = read_file(self.directory, ".access.json")
        if (set(access) not in ({"version", "session_id", "pid", "runtime_id", "port", "token"},{"version", "session_id", "pid", "runtime_id", "port", "token", "os_identity"})
                or access["version"] != VERSION or access["session_id"] != self.directory.name
                or type(access["pid"]) is not int or access["pid"] <= 0
                or type(access["port"]) is not int or not 1 <= access["port"] <= 65535
                or not isinstance(access["runtime_id"], str) or not re.fullmatch(r"[0-9a-f]{32}", access["runtime_id"])
                or not isinstance(access["token"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", access["token"])):
            raise ValueError("Invalid private session capability.")
        self.access = access

    def status(self):
        result = local.http_json(self.access["port"], "/status", token=self.access["token"], timeout=5)
        if (result.get("session_id") != self.directory.name or result.get("runtime_id") != self.access["runtime_id"]
                or result.get("supervisor_pid") != self.access["pid"] or result.get("version") != VERSION):
            raise ValueError("The supervisor identity changed; no operation was sent.")
        return result

    def request(self, path, request):
        self.status()  # Bind to this exact still-running supervisor first.
        result = local.http_json(self.access["port"], path, request, self.access["token"], timeout=35)
        if result.get("session_id") != self.directory.name:
            raise ValueError("Operation returned a different session identity.")
        return result


def launch_flags():
    return {"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP} if platform.system() == "Windows" else {"start_new_session": True}


def launch_session(output, scenario="routing", ttl=900):
    if type(ttl) is not int or not 60 <= ttl <= 3600:
        raise ValueError("Use a lease of 60 to 3600 seconds.")
    directory = create_session(output, scenario, ttl)
    try:
        with (directory / "supervisor.log").open("w", encoding="utf-8") as stream:
            process = subprocess.Popen([restart.worker_interpreter(), "-u", str(Path(__file__).resolve()), "--serve", str(directory)],
                cwd=ROOT, env=restart.child_env(), stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT,
                close_fds=True, shell=False, **launch_flags())
    except (OSError, RuntimeError):
        workspace.write_json(directory / "launch_failure.json", {"supervisor_started": False, "captured_at": now()})
        raise
    try:
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise ValueError("Supervisor startup failed. Inspect its saved supervisor.log; no replacement runtime was assumed.")
            if (directory / ".access.json").exists():
                result = SessionClient(directory).status()
                if result["supervisor_pid"] != process.pid:
                    raise ValueError("Supervisor process ownership mismatch.")
                # Keep and reap the child while it runs, without making its
                # lifetime depend on the short-lived launcher/client process.
                threading.Thread(target=process.wait, name="local-session-reaper", daemon=True).start()
                return directory, result
            time.sleep(0.05)
        raise ValueError("Supervisor readiness timed out; no success was assumed.")
    except BaseException:
        # Only terminate the child created by this invocation, never a PID
        # loaded from saved evidence. Its owned workers stop on parent EOF.
        if process.poll() is None:
            process.terminate()
        exit_code = process.wait(timeout=8)
        workspace.write_json(directory / "launch_failure.json", {"supervisor_started": True,
            "supervisor_exit_confirmed": True, "supervisor_exit_code": exit_code if type(exit_code) is int else None,
            "captured_at": now(), "scope": "Only this launcher's owned supervisor exit is confirmed here. Worker exits require their separate teardown evidence."})
        raise


def request_for(directory, decision, note="C records its bounded local decision.", consent_source="SCRIPTED LOCAL LAB FIXTURE"):
    description = SessionClient(directory).status()
    digest = description["current_digest"]
    return {"session_id": Path(directory).name, "decision_key": "owner-" + uuid.uuid4().hex,
        "decision": decision, "note": note, "expected_digest": digest,
        "consent": decision_phrase(Path(directory).name, decision, digest), "consent_source": consent_source}


def stop_owned_session(directory):
    """Internal harness cleanup for a session this caller explicitly created."""
    client=SessionClient(directory)
    result = client.request("/stop", {"session_id": Path(directory).name, "consent": stop_phrase(Path(directory).name)})
    if result.get("status") != "STOPPING":
        raise ValueError("Session shutdown was not acknowledged.")
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        data = saved_session(directory)
        if data["lifecycle"] == "STOPPED":
            if not data["teardown"]["all_workers_stopped"]:
                raise ValueError("Some owned process exits are unconfirmed.")
            access=client.access
            if not native_process.wait_for_exit(access['pid'],access.get('os_identity'),15):
                raise ValueError('Session workers stopped, but supervisor exit/file release is unconfirmed.')
            return data
        time.sleep(0.05)
    raise ValueError("Shutdown outcome is not yet confirmed; inspect saved evidence.")


def demo(output, session_output=None):
    directory = service.new_directory(Path(output))
    directory.mkdir(parents=True, exist_ok=False)
    rows = []
    for scenario in ("routing", "dependency"):
        session, first = launch_session(session_output or directory / "sessions", scenario, 120)
        try:
            client = SessionClient(session)
            # A new Python process, not the original launcher, reconnects.
            result = subprocess.run([restart.worker_interpreter(), "-B", str(Path(__file__).resolve()), "status", "--output", str(session.parent), "--session", session.name],
                capture_output=True, text=True, encoding="utf-8", env=restart.child_env(), cwd=ROOT, timeout=15)
            if result.returncode:
                raise ValueError("Separate-process reconnect failed: " + result.stderr)
            reconnected = workspace.decode(result.stdout)
            initial_pids = {k: (v["pid"], v["identity"]) for k, v in first["actual_at_outcome"]["workers"].items()}
            retained = client.request("/review", request_for(session, "retain"))
            after_retain = client.status()
            key = "withdraw_route" if scenario == "routing" else "release_dependency"
            released = client.request("/review", request_for(session, key))
            outcome = client.status()
            probe = client.request("/probe", {"session_id": session.name})
            checks = {
                "new_process_reconnect_same_supervisor": reconnected["supervisor_pid"] == first["supervisor_pid"],
                "reconnect_same_worker_identities": initial_pids == {k: (v["pid"], v["identity"]) for k, v in reconnected["actual_at_outcome"]["workers"].items()},
                "retain_preserves_policy": first["policy_snapshot"]["state"] == after_retain["policy_snapshot"]["state"],
                "retain_keeps_candidate_alive": after_retain["actual_at_outcome"]["workers"]["candidate-v2"]["alive"],
                "owner_consent_recorded": retained["status"] == released["status"] == "SUCCEEDED",
                "recovery_complete": outcome["recovery"]["status"] == "VERIFIED_RECOVERED",
                "real_candidate_exit_confirmed": outcome["actual_at_outcome"]["workers"]["candidate-v2"]["alive"] is False and outcome["actual_at_outcome"]["workers"]["candidate-v2"]["exit_code"] is not None,
                "capacity_released_after_exit": outcome["metrics"]["committed"] == 30,
                "B_protected": protection(first["policy_snapshot"]["state"]) == protection(outcome["policy_snapshot"]["state"]),
                "A_stays_paused": "deploy-A" in outcome["policy_snapshot"]["state"]["paused_runs"],
                "stable_still_serves": probe["successful"] == 10 and outcome["actual_at_outcome"]["workers"]["stable-v1"]["alive"],
                "physical_route_matches": outcome["actual_at_outcome"]["candidate_percent"] == outcome["policy_snapshot"]["state"]["routing"]["candidate-v2"] == 0,
            }
            if not all(checks.values()):
                raise ValueError("Persistent session walkthrough failed: " + sim.canonical(checks))
            rows.append({"scenario": scenario, "session_id": session.name, "checks": checks,
                         "before_units": first["metrics"]["committed"], "after_units": outcome["metrics"]["committed"], "model_calls": 0})
        finally:
            stop_owned_session(session)
    record = {"version": VERSION, "status": "PASSED", "mode": MODE, "cases": rows, "cases_passed": 2,
              "checks_passed": sum(len(row["checks"]) for row in rows), "model_calls": 0,
              "scope": "Actual detached supervisors, separate-process clients and local workers/HTTP. Scripted fixture consent; no models or production authentication."}
    workspace.write_json(directory / "summary.json", record)
    (directory / "index.html").write_text("<!doctype html><meta charset='utf-8'><title>Runtime sessions</title><h1>Persistent local session walkthrough</h1><p>2/2 scripted cases; real workers and HTTP; zero model calls.</p><pre>" + html.escape(json.dumps(record, indent=2)) + "</pre>", encoding="utf-8")
    return directory, record




def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", nargs="?", choices=("start", "status", "review", "resume", "probe", "stop", "demo"), default="demo")
    parser.add_argument("--serve", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--output", type=Path, default=ROOT / FOLDER)
    parser.add_argument("--demo-output", type=Path, default=ROOT / DEMO_FOLDER)
    parser.add_argument("--scenario", choices=sorted(SCENARIOS), default="routing")
    parser.add_argument("--ttl", type=int, default=900)
    parser.add_argument("--session")
    parser.add_argument("--decision", choices=("retain", "withdraw_route", "release_dependency"))
    parser.add_argument("--request", dest="request_key")
    parser.add_argument("--note", default="C records its bounded local decision.")
    parser.add_argument("--open", action="store_true")
    args = parser.parse_args(argv)
    if args.serve is not None:
        serve(args.serve)
        return 0
    if args.action == "demo":
        directory, report = demo(args.demo_output)
        print("Persistent sessions: 2/2 walkthroughs | %s checks | 0 model calls" % report["checks_passed"])
        print("Evidence: " + str(directory))
        if args.open:
            webbrowser.open((directory / "index.html").as_uri())
        return 0
    if args.action == "start":
        directory, result = launch_session(args.output, args.scenario, args.ttl)
        print("Session: " + directory.name)
        print("Evidence: " + str(directory))
        print("Workers stay active for up to %s seconds. Reconnect using runtime-sessions status --session %s" % (args.ttl, directory.name))
        return 0
    if not args.session:
        parser.error("Use --session with the identifier printed at startup.")
    if args.action in {"review", "stop"} and not terminal_guard.interactive_input():
        parser.error("Owner consent and shutdown require an interactive terminal; no capability or worker was accessed.")
    directory = directory_for(args.output, args.session)
    if args.action == "status":
        try:
            result = SessionClient(directory).status()
            result["connection"] = "CONTACTED_SAME_SUPERVISOR"
        except (OSError, ValueError, http.client.HTTPException):
            result = saved_session(directory)
            result["connection"] = "SAVED_STOPPED_RECORD" if result["lifecycle"] == "STOPPED" else "UNREACHABLE_LAST_EVIDENCE_ONLY"
        print(json.dumps(result, indent=2))
        return 0 if result["connection"] != "UNREACHABLE_LAST_EVIDENCE_ONLY" else 2
    client = SessionClient(directory)
    if args.action == "review":
        if args.decision is None:
            parser.error("Select --decision retain, withdraw_route or release_dependency.")
        request = request_for(directory, args.decision, args.note, "EXPLICIT TERMINAL FIXTURE CONSENT")
        current = client.status()
        if args.decision not in current["available_decisions"]:
            parser.error("This owner decision is not currently available. Inspect the saved session first.")
        print("SESSION: " + directory.name + " | " + current["scenario"] + " | committed " + str(current["metrics"]["committed"]))
        print("Decision: " + args.decision + " | reason: " + args.note)
        print("This changes the still-running local service using C's fixture authority; it starts no model request.")
        print("Type exactly: " + request["consent"])
        if input("> ").strip() != request["consent"]:
            print("Consent declined; no owner operation was sent.")
            return 1
        workspace.write_json(directory / ("client-request-" + request["decision_key"] + ".json"), request)
        try:
            result = client.request("/review", request)
        except (OSError, http.client.HTTPException):
            print("Reply unavailable; do not assume success. Inspect status, then resume --request " + request["decision_key"])
            return 2
    elif args.action == "stop":
        client.status()
        print("This tears down every worker in this local session. It does not count as policy recovery.")
        print("Type exactly: " + stop_phrase(directory.name))
        if input("> ").strip() != stop_phrase(directory.name):
            print("Shutdown declined; the lease remains active.")
            return 1
        result = stop_owned_session(directory)["teardown"]
    elif args.action == "resume":
        if not args.request_key:
            parser.error("Use --request with the existing owner intent key; no new consent or action is invented.")
        result = client.request("/resume", {"session_id": directory.name, "decision_key": args.request_key})
    else:
        result = client.request("/probe", {"session_id": directory.name})
    print(json.dumps(result, indent=2))
    return 0 if result.get("status", "SUCCEEDED") == "SUCCEEDED" else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, sim.Denied, http.client.HTTPException) as exc:
        print("RUNTIME SESSION STOPPED: " + str(exc), file=sys.stderr)
        raise SystemExit(1)
    except (KeyboardInterrupt, EOFError):
        print("Client interrupted. Inspect session status and any saved owner intent before retrying; no successful outcome is assumed.", file=sys.stderr)
        raise SystemExit(2)
