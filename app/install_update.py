"""Validate the complete standalone runtime before opening a workspace."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path

REQUIRED = frozenset((
    'LICENSE',
    'agent_team.py',
    'browser_proposals.py',
    'case_workflow.py',
    'challenge_factory.py',
    'change_coordinator.py',
    'conflict_explorer.py',
    'conflict_lab.py',
    'coordination_reference.py',
    'coverage_planner.py',
    'desktop_entry.py',
    'desktop_launcher.py',
    'downloadable_demo.py',
    'durable_runtime.py',
    'hosted_server.py',
    'inference_gateway.py',
    'install_update.py',
    'judge_evaluation.py',
    'judge_relay.py',
    'judge_relay_schema.sql',
    'judge_relay_worker.mjs',
    'judge_setup.py',
    'live_evaluator.py',
    'live_readiness.py',
    'local_service.py',
    'mission_control.py',
    'named_access.py',
    'native_process.py',
    'operations_evidence.py',
    'operations_outcomes.py',
    'operations_proposals.py',
    'operations_server.py',
    'operations_supervisor.py',
    'operations_ui.py',
    'operations_workspace.py',
    'oversight_pairs.py',
    'oversight_participant.html',
    'oversight_receipts.py',
    'oversight_review.py',
    'oversight_round.py',
    'oversight_round2.py',
    'oversight_round3.py',
    'oversight_round4.py',
    'oversight_round_001.json',
    'oversight_round_002.json',
    'oversight_round_003.json',
    'oversight_round_004.json',
    'oversight_shadow.py',
    'oversight_store.py',
    'oversight_studies.py',
    'oversight_timing.py',
    'oversight_ui.html',
    'plan_revision.py',
    'relay_demo.py',
    'release_check.py',
    'requirements-research.txt',
    'restart_checker.py',
    'review_workflow.py',
    'runtime_sessions.py',
    'safety_environment.py',
    'safety_http.py',
    'safety_inspect.py',
    'safety_inspect_check.py',
    'safety_lab.py',
    'safety_live.py',
    'safety_suite_v1.json',
    'safety_ui.py',
    'safety_variants.py',
    'service_agent.py',
    'simulator.py',
    'team_workspace.py',
    'terminal_guard.py',
    'workflow_balanced.json',
    'workflow_bulk.json',
    'workflow_dependency_chain.json',
    'workflow_deployment_heavy.json',
    'workflow_planner.py',
))


def digest(data):
    return hashlib.sha256(data).hexdigest()


def payload(source):
    source = Path(source).resolve()
    manifest_path = source / "bundle_manifest.json"
    if manifest_path.is_symlink():
        raise ValueError("The runtime manifest must be a regular file.")
    raw = manifest_path.read_bytes()
    manifest = json.loads(raw)
    hashes = manifest.get("file_sha256")
    if manifest.get("package_version") != "2.0.1" or not isinstance(hashes, dict) or set(hashes) != REQUIRED:
        raise ValueError("This is not the complete Orkestryn 2.0 runtime.")
    files = {}
    for name in sorted(REQUIRED):
        path = source / name
        if not path.is_file() or path.is_symlink():
            raise ValueError("A required runtime file is missing: " + name)
        data = path.read_bytes()
        if digest(data) != hashes[name]:
            raise ValueError("Runtime integrity check failed: " + name)
        files[name] = data
    files["bundle_manifest.json"] = raw
    return files, manifest["package_version"]
