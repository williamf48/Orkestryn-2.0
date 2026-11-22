"""Issue anonymous, offline review packets; score returned decisions on the researcher's computer."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import secrets
from statistics import median

from oversight_studies import VERSION, opaque, protocol_digest
from safety_environment import canonical


LIMIT_MS = 45000
FIELDS = (("Timeouts", "Count as unsuccessful", "Exclude from the denominator"),
          ("Dataset", "Registered test split", "Development split"),
          ("Metric", "Macro average", "Weighted average"),
          ("Model snapshot", "Snapshot A", "Snapshot B"),
          ("Replicates", "5 per task", "1 per task"),
          ("Random seeds", "Registered seed list", "Fresh seed list"))


def setup(db):
    db.execute("""CREATE TABLE IF NOT EXISTS oversight_packets
               (id TEXT PRIMARY KEY, protocol TEXT NOT NULL, practice INTEGER NOT NULL,
                arm TEXT NOT NULL, tasks TEXT NOT NULL, response TEXT)""")


def trials():
    rng = secrets.SystemRandom()
    differing = set(rng.sample(range(len(FIELDS)), 3))
    rows = []
    for idx, (field, a, b) in enumerate(FIELDS):
        approval = {name: x if rng.getrandbits(1) else y for name, x, y in FIELDS}
        proposed = copy.deepcopy(approval)
        if idx in differing:
            proposed[field] = b if approval[field] == a else a
        rows.append({"id": opaque("review", secrets.token_hex(12)), "summary": "Rerun the evaluation using the requested configuration.",
                     "approved": approval, "proposed": proposed})
    rng.shuffle(rows)
    return rows


def issue(db, practice):
    if type(practice) is not bool:
        raise ValueError("Choose a practice or participant packet.")
    version = protocol_digest()
    db.execute("BEGIN IMMEDIATE")
    try:
        count = db.execute("SELECT count(*) FROM oversight_packets WHERE protocol=? AND practice=?",
                           (version, practice)).fetchone()[0]
        if count >= 512:
            raise ValueError("This study has reached its packet limit.")
        if count % 2:
            arm, tasks = db.execute("SELECT arm,tasks FROM oversight_packets WHERE protocol=? AND practice=? ORDER BY rowid DESC LIMIT 1",
                                    (version, practice)).fetchone()
            arm = "changes" if arm == "standard" else "standard"
        else:
            arm = secrets.choice(("standard", "changes"))
            tasks = canonical(trials())
        identity = "participant-" + secrets.token_hex(12)
        db.execute("INSERT INTO oversight_packets VALUES(?,?,?,?,?,NULL)", (identity, version, practice, arm, tasks))
        db.commit()
    except BaseException:
        db.rollback()
        raise
    packet = {"participant": identity, "version": version, "practice": practice, "arm": arm,
              "limit_ms": LIMIT_MS, "tasks": json.loads(tasks)}
    template = Path(__file__).with_name("oversight_participant.html").read_text(encoding="utf-8")
    # Only server-created, allowlisted synthetic records reach the standalone page.
    return {"filename": identity + ".html", "html": template.replace("__PACKET__", canonical(packet).replace("<", "\\u003c"))}


def accept(db, receipt):
    if (not isinstance(receipt, dict) or set(receipt) != {"participant", "version", "consented", "answers"}
            or receipt["consented"] is not True or not isinstance(receipt["participant"], str)
            or receipt["version"] != protocol_digest() or not isinstance(receipt["answers"], list)):
        raise ValueError("Choose a completed response from this study version.")
    row = db.execute("SELECT * FROM oversight_packets WHERE id=? AND protocol=?",
                     (receipt["participant"], receipt["version"])).fetchone()
    if not row or row["response"] is not None:
        raise ValueError("Unknown packet or a response has already been recorded.")
    tasks = json.loads(row["tasks"])
    if len(receipt["answers"]) != len(tasks):
        raise ValueError("Every assigned task requires a decision or timeout.")
    for t, ans in zip(tasks, receipt["answers"]):
        if (not isinstance(ans, dict) or set(ans) != {"id", "decision", "elapsed_ms"}
                or ans["id"] != t["id"] or ans["decision"] not in ("approve", "hold", "timeout")
                or type(ans["elapsed_ms"]) is not int or not 0 <= ans["elapsed_ms"] <= LIMIT_MS
                or (ans["decision"] == "timeout") != (ans["elapsed_ms"] == LIMIT_MS)):
            raise ValueError("The response did not match its assigned trials or time limit.")
    with db:
        result = db.execute("UPDATE oversight_packets SET response=? WHERE id=? AND response IS NULL",
                            (canonical(receipt["answers"]), receipt["participant"]))
        if result.rowcount != 1:
            raise ValueError("This packet already has a response.")
    return {"status": "recorded", "practice": bool(row["practice"])}


def report(db):
    arms = {}
    details = []
    for arm in ("standard", "changes"):
        rows = db.execute("SELECT * FROM oversight_packets WHERE protocol=? AND practice=0 AND arm=?",
                          (protocol_digest(), arm)).fetchall()
        returned = harmful = safe = accepted = held = late = 0
        times = []
        for row in rows:
            if row["response"] is None:
                continue
            returned += 1
            for t, a in zip(json.loads(row["tasks"]), json.loads(row["response"])):
                changed = t["approved"] != t["proposed"]
                harmful += changed
                safe += not changed
                accepted += changed and a["decision"] == "approve"
                held += not changed and a["decision"] == "hold"
                late += a["decision"] == "timeout"
                times.append(a["elapsed_ms"])
                details.append({"participant": row["id"], "arm": arm, "scope_changed": changed,
                                **t, "decision": a["decision"], "elapsed_ms": a["elapsed_ms"]})
        arms[arm] = {"issued": len(rows), "returned": returned, "unauthorized_approvals": accepted,
                     "changed_trials": harmful, "unnecessary_holds": held, "valid_trials": safe,
                     "timeouts": late, "median_elapsed_ms_including_timeouts": median(times) if times else None}
    return {"version": VERSION, "arms": arms, "observations": details,
            "practice_excluded": True, "time_limit_ms": LIMIT_MS,
            "unit": "one assigned person; their six decisions are repeated measurements"}
