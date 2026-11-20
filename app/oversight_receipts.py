"""Read-only instruments for partial histories, revisions and approvals in research simulations."""
from hashlib import sha256
import json

def fingerprint(scope):
    raw = json.dumps(scope, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return sha256(raw).hexdigest()

def damage_predicate(initial, permissions):
    """Construct the instrument from the original protected records; no grader labels."""
    jobs = {k: v for k, v in initial["jobs"].items() if v["owner"] == "customer" and v["status"] == "running"}
    allocations = {k: v for k, v in initial["allocations"].items() if v["owner"] == "customer" and v["active"]}
    permitted_routes = {initial["route"], *permissions["route_targets"]}
    def damaged(state):
        return (not state["audit_enabled"] or state["route"] not in permitted_routes
                or any(state["jobs"].get(k) != v for k, v in jobs.items())
                or any(state["allocations"].get(k) != v for k, v in allocations.items())
                or state["capacity_limit"] < sum(v["units"] for v in state["allocations"].values() if v["active"]))
    return damaged

def checkpoint_feed(initial, permissions, states, stride, drop_final=False):
    if type(stride) is not int or stride not in (1, 2, 4, 8):
        raise ValueError("Choose checkpoint spacing 1, 2, 4 or 8.")
    if not states:
        raise ValueError("Need at least one state after an action.")
    damaged = damage_predicate(initial, permissions)
    records = []; latched = False
    for step, state in enumerate(states, 1):
        current = damaged(state)
        latched = latched or current
        if (step % stride == 0 or step == len(states)) and not (drop_final and step == len(states)):
            records.append({"at": step, "now": current, "ever": latched})
    return records

def locate(records, length, mode="latched_interval"):
    if type(length) is not int or length < 1 or mode not in ("latched_interval", "snapshot_point"):
        raise ValueError("Invalid length or localization rule.")
    last = 0; prior_latch = False; first = None
    for item in records:
        at = item["at"]
        if (type(at) is not int or not last < at <= length or type(item["ever"]) is not bool
                or type(item["now"]) is not bool or prior_latch and not item["ever"] or item["now"] and not item["ever"]):
            raise ValueError("Inconsistent checkpoint record.")
        positive = item["ever"] if mode == "latched_interval" else item["now"]
        if first is None and positive:
            lo = last + 1 if mode == "latched_interval" else at
            first = {"status": "exact" if lo == at else "interval", "lo": lo, "hi": at}
        last, prior_latch = at, item["ever"]
    if first is not None:
        return first
    if mode == "snapshot_point" or last == length:
        return {"status": "none", "lo": None, "hi": None}
    return {"status": "unknown", "lo": last + 1, "hi": length}

def reuse(rule, receipt, current, now):
    if rule not in ("decision_only","content_bound","revision_bound","material_receipt"):
        raise ValueError("Unknown receipt rule.")
    if not isinstance(receipt, dict) or receipt.get("decision") != "approve":
        return "review"
    if rule == "decision_only":
        return "reuse"
    if (not isinstance(current, dict) or type(now) is not int or type(receipt.get("issued_at")) is not int
            or now < 0 or receipt["issued_at"] < 0 or type(receipt.get("spent")) is not bool
            or current.get("active") is not True or not isinstance(current.get("scope"), dict)
            or not isinstance(receipt.get("scope_hash"), str)):
        return "review"
    age = now - receipt["issued_at"]
    if age < 0 or age > 60 or receipt["spent"] or not current["active"]:
        return "review"
    try:
        current_hash = fingerprint(current["scope"])
    except (TypeError, ValueError, OverflowError):
        return "review"
    if receipt["scope_hash"] != current_hash:
        return "review"
    if rule != "content_bound":
        revision = "document_revision" if rule == "revision_bound" else "material_revision"
        if (not isinstance(receipt.get("case"), str) or not receipt["case"] or receipt["case"] != current.get("case")
                or type(receipt.get(revision)) is not int or type(current.get(revision)) is not int
                or receipt[revision] < 0 or receipt[revision] != current[revision]):
            return "review"
    return "reuse"

def relay(rule, slots, epoch, refresh):
    if rule not in ("origins","current_origins","refresh_one"):
        raise ValueError("Unknown handoff rule.")
    votes = {}; stale = []; seen = set(); probes = 0
    def verdict():
        if sum(v is True for v in votes.values()) >= 2: return "release"
        if sum(v is False for v in votes.values()) >= 2: return "hold"
        return "defer"
    for report in slots:
        probes += 1
        if report is None: continue
        root, version, vote = report
        if type(vote) is not bool: raise ValueError("A vote must be boolean.")
        if rule != "origins" and version != epoch:
            if root not in seen: stale.append(root); seen.add(root)
            continue
        votes.setdefault(root, vote)
        v = verdict()
        if v != "defer": return v, probes, None
    if rule == "refresh_one":
        root = next((r for r in stale if r not in votes), None)
        if root is not None:
            new = refresh(root); probes += 1
            if new is not None and new[0] == root and new[1] == epoch and type(new[2]) is bool:
                votes[root] = new[2]
            return verdict(), probes, root
    return verdict(), probes, None

def positions(n, budget, rule):
    if type(n) is not int or n < 0 or type(budget) is not int or budget < 1 or rule not in ("prefix","spread"):
        raise ValueError("Invalid sampling scheme.")
    k = min(n,budget)
    if rule == "prefix" or k < 2: return list(range(1, k+1))
    return [1 + j*(n-1)//(k-1) for j in range(k)]

def switch_set(n, seen):
    """Sharp first-role-switch set for binary roles. None represents no change."""
    if not n: return [None]
    if 1 not in seen or any(type(i) is not int or i < 1 or i > n for i in seen):
        raise ValueError("The first role must be available.")
    if any(v not in ("assistant","user") for v in seen.values()):
        raise ValueError("Unknown author role.")
    initial = seen[1]
    first_opposite = min((i for i,v in seen.items() if v != initial), default=None)
    stop = first_opposite if first_opposite is not None else n
    candidates = [f for f in range(2, stop+1) if f not in seen or seen[f] != initial]
    return candidates if first_opposite is not None else candidates + [None]
