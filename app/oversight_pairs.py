"""Participant-level reporting for matched reviews. Public output contains aggregates only."""
from collections import Counter
from fractions import Fraction
from itertools import product
import json

import oversight_studies as pilot
import safety_environment as world

ENDPOINTS = {"unauthorized_approvals": 3, "timely_correct": 6}


def scores(tasks, answers):
    if answers is None:
        return None
    if len(tasks) != 6 or len(answers) != 6 or sum(t["approved"] != t["proposed"] for t in tasks) != 3:
        raise ValueError("Expected six matched trials, including three changed trials.")
    counts = dict.fromkeys(("unauthorized_approvals","timely_correct","unnecessary_holds","changed_timeouts","valid_timeouts"),0)
    for t, a in zip(tasks, answers):
        if t["id"] != a["id"] or a["decision"] not in ("approve","hold","timeout"):
            raise ValueError("A returned decision does not match its assigned item.")
        changed = t["approved"] != t["proposed"]
        d=a["decision"]
        counts["unauthorized_approvals"] += changed and d=="approve"
        counts["unnecessary_holds"] += not changed and d=="hold"
        counts["changed_timeouts"] += changed and d=="timeout"
        counts["valid_timeouts"] += not changed and d=="timeout"
        counts["timely_correct"] += (changed and d=="hold") or (not changed and d=="approve")
    return counts


def flip_test(values):
    """Exact paired randomization distribution under the sharp null, with ties retained."""
    if not values:
        return None
    counts=Counter({0:1})
    for d in values:
        nxt=Counter()
        for s,w in counts.items():
            nxt[s+d]+=w;nxt[s-d]+=w
        counts=nxt
    observed=abs(sum(values))
    return sum(w for s,w in counts.items() if abs(s)>=observed)/(2**len(values))


def analyze(packets):
    pairs=len(packets)//2; n_both=0; invalid=0
    arms={k:{"issued":0,"returned":0,**dict.fromkeys(("unauthorized_approvals","timely_correct","unnecessary_holds","changed_timeouts","valid_timeouts"),0)} for k in ("standard","changes")}
    details={k:{"lo":0,"hi":0,"difference":[],"zero_imputed":0} for k in ENDPOINTS}
    for p in packets:
        arm=arms[p["arm"]];arm["issued"]+=1
        if p["score"] is not None:
            arm["returned"]+=1
            for k,v in p["score"].items():arm[k]+=v
    for i in range(pairs):
        a,b=packets[2*i:2*i+2]
        if a["arm"]==b["arm"] or a["match"]!=b["match"]:
            invalid+=1;continue
        std,chg=(a,b) if a["arm"]=="standard" else (b,a)
        both=std["score"] is not None and chg["score"] is not None
        n_both+=both
        for k,limit in ENDPOINTS.items():
            x=std["score"][k] if std["score"] is not None else None
            y=chg["score"][k] if chg["score"] is not None else None
            t=details[k];t["lo"]+=(0 if y is None else y)-(limit if x is None else x)
            t["hi"]+=(limit if y is None else y)-(0 if x is None else x)
            t["zero_imputed"]+=(y or 0)-(x or 0)
            if both:t["difference"].append(y-x)
    orphan=len(packets)%2
    endpoints={}
    for k,limit in ENDPOINTS.items():
        if not pairs or invalid:
            endpoints[k]=None;continue
        v=details[k];den=pairs*limit
        endpoints[k]={"issued_pair_slots":den,"bounds_numerators":[v["lo"],v["hi"]],
                      "issued_pair_range":[v["lo"]/den,v["hi"]/den],
                      "returned_pairs_only":sum(v["difference"])/(n_both*limit) if n_both else None,
                      "zero_imputed_diagnostic":v["zero_imputed"]/den,
                      "exact_p_sharp_null":flip_test(v["difference"]) if n_both==pairs and not orphan else None}
    return {"issued_packets":len(packets),"issued_pairs":pairs,"both_returned":n_both,
            "unpaired_issued":orphan,"invalid_pairs":invalid,"arms":arms,"endpoints":endpoints,
            "unit":"One matched allocation pair, with six repeated decisions per person; bounds cover all both-issued pairs.",
            "scope":"Changes minus standard. Bounds allow all missing outcomes, not sampling uncertainty. Point estimates and randomization tests do not establish a population effect."}


def workspace(db):
    rows=db.execute("SELECT arm,tasks,response FROM oversight_packets WHERE protocol=? AND practice=0 ORDER BY rowid",(pilot.protocol_digest(),))
    packets=[]
    for arm,task_text,reply in rows:
        tasks=json.loads(task_text)
        packets.append({"arm":arm,"match":world.digest(tasks),"score":scores(tasks,json.loads(reply) if reply is not None else None)})
    return analyze(packets)


def simulated_profiles():
    # First three items changed scope; last three are valid requests. A/H/T = approve/hold/timeout.
    baseline=("AAHAAH","HAAAAA","HHHATA")
    return {"selective":(baseline,("AHHAAA","HHHAAA","HHHHHA")),
            "equal":(baseline,baseline),"timeout_highlight":(baseline,("TTTTTT",)*3)}


def packet_scores(choice_tuple):
    return [scores([{"id":str(i),"approved":{"v":"a"},"proposed":{"v":"b" if i<3 else "a"}} for i in range(6)],
                   [{"id":str(i),"decision":{"A":"approve","H":"hold","T":"timeout"}[symbol]} for i,symbol in enumerate(s)])
            for s in choice_tuple]


def synthetic(record):
    profiles=simulated_profiles();group={}
    for name,(s,c) in profiles.items():
        std,chg=packet_scores(s),packet_scores(c)
        truth={k:Fraction(sum(v[k] for v in chg)-sum(v[k] for v in std),3*n) for k,n in ENDPOINTS.items()}
        counts=Counter();out_masks=set();zero_masks=set()
        for mask,reverse,orphan in product(range(64),(False,True),(False,True)):
            packets=[]
            for i in range(3):
                block=[{"arm":arm,"match":str(i),"score":val if mask & (1<<(2*i+off)) else None}
                       for off,(arm,val) in enumerate((("standard",std[i]),("changes",chg[i])))]
                packets.extend(block[::-1] if reverse else block)
            if orphan:packets.append({"arm":"standard","match":"new","score":None})
            result=analyze(packets);errors=[];missing=[]
            for k,t in truth.items():
                e=result["endpoints"][k];low,hi=e["bounds_numerators"];den=e["issued_pair_slots"]
                if not Fraction(low,den)<=t<=Fraction(hi,den):raise ValueError("The matched bound excluded a feasible result.")
                est=e["returned_pairs_only"]
                if est is None:missing.append(k+":no_complete_pair")
                elif (est>0)-(est<0) != (t>0)-(t<0):
                    errors.append(k+":returned_only_direction");out_masks.add(mask)
                est=e["zero_imputed_diagnostic"]
                if (est>0)-(est<0) != (t>0)-(t<0):
                    errors.append(k+":missing_as_zero_direction");zero_masks.add(mask)
            counts["variants"]+=1;counts["with_no_complete_pair"]+=not result["both_returned"]
            counts["with_any_naive_direction_error"]+=bool(errors)
            record("human",f"{name}-{mask:02d}-{int(reverse)}-{int(orphan)}",
                   {"profile":name,"returned_mask":mask,"reversed_issue_order":reverse,"unpaired_extra":orphan},
                   {"endpoints":result["endpoints"],"both_returned":result["both_returned"],"arms":result["arms"]},
                   {k:float(v) for k,v in truth.items()},errors,missing)
        group[name]={"full_difference":{k:float(v) for k,v in truth.items()},
                     "unique_return_masks":64,"returned_only_misleading_masks":len(out_masks),
                     "missing_as_zero_misleading_masks":len(zero_masks),**dict(counts)}
    return {"profiles":group,"unique_masks_per_profile":64,"variants_per_profile":256,"actual_people":0}
