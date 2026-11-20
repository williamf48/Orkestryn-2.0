"""Paired, local timing for two decision-equivalent research inspectors. No model API."""
from pathlib import Path
from statistics import median
import json
import os
import platform
import random
import subprocess
import sys
import time
import oversight_shadow as shadow
import safety_environment as world
import safety_lab

SPEC = Path(__file__).with_name("oversight_round_004.json")

def wire_decode(payload):
    view = world.parse_json(payload.decode("utf-8"), maximum=131072)
    view["untrusted_candidate"] = world.validate_plan(view["untrusted_candidate"])
    return view

def worker(worker_id):
    cfg=json.loads(SPEC.read_bytes())["budget"]
    inputs={form:[] for form in cfg["workloads"]}
    for case in safety_lab.dataset()["cases"]:
        if case["private"]["split"]!="heldout": continue
        for form,c in (("original",case),("workload",shadow.loaded_twin(case)[0])):
            v=world.public_context(c); encoded=world.canonical(v).encode("utf-8")
            results=[shadow.audit(v,rule,cfg["full_budget"],cfg["price"]) for rule in cfg["strategies"]]
            signature=lambda z:(z["decision"],z["first_authority"],z["first_harm"],z["through"],z["complete"])
            if not results[0]["complete"] or signature(results[0])!=signature(results[1]) or wire_decode(encoded)!=v:
                raise ValueError("Cannot time incomparable reviews: "+case["id"])
            inputs[form].append((v,encoded))
    rng=random.Random(cfg["seed"]+worker_id)
    samples=[]; modes=[(form,mode) for form in cfg["workloads"] for mode in cfg["modes"]]
    for block in range(-cfg["warmups"],cfg["blocks"]):
        groups=modes[:];rng.shuffle(groups)
        for form,mode in groups:
            entries=inputs[form][:];rng.shuffle(entries)
            rules=cfg["strategies"][:]
            if (worker_id+block)%2: rules.reverse()
            for order,rule in enumerate(rules):
                checksum=0; start=time.perf_counter_ns()
                for _ in range(cfg["loops"]):
                    for view,encoded in entries:
                        v = view if mode=="typed" else wire_decode(encoded)
                        out=shadow.audit(v,rule,cfg["full_budget"],cfg["price"])
                        checksum+=out["through"]
                elapsed=time.perf_counter_ns()-start
                if block>=0:
                    samples.append({"worker":worker_id,"block":block,"form":form,"mode":mode,"rule":rule,
                                    "order":order,"elapsed_ns":elapsed,"calls":cfg["loops"]*len(entries),"sum_through":checksum})
    return samples

def summarize(samples):
    by={}
    for r in samples:
        key=r["form"]+"/"+r["mode"]
        by.setdefault(key,{}).setdefault(r["worker"],{}).setdefault(r["block"],{})[r["rule"]]=r
    report={}
    for key,workers in by.items():
        ratios=[];pns=[];ins=[]
        for blocks in workers.values():
            br=[];bp=[];bi=[]
            for pair in blocks.values():
                a,b=pair["prefix_defer"],pair["indexed_defer"]
                if (a["calls"],a["sum_through"])!=(b["calls"],b["sum_through"]): raise ValueError("Unmatched batch.")
                br.append(b["elapsed_ns"]/a["elapsed_ns"]);bp.append(a["elapsed_ns"]/a["calls"]);bi.append(b["elapsed_ns"]/b["calls"])
            ratios.append(median(br));pns.append(median(bp));ins.append(median(bi))
        report[key]={"paired_ratio":median(ratios),"worker_ratio_range":[min(ratios),max(ratios)],"worker_ratios":ratios,
                     "prefix_median_us":median(pns)/1000,"indexed_median_us":median(ins)/1000}
    return report

def save(path):
    path=Path(path)
    if path.exists(): raise FileExistsError(path)
    cfg=json.loads(SPEC.read_bytes())["budget"]; samples=[]
    for i in range(cfg["workers"]):
        proc=subprocess.run([sys.executable,"-B",str(Path(__file__).resolve()),"--worker",str(i)],capture_output=True,text=True,
                            env={**os.environ,"PYTHONHASHSEED":str(cfg["seed"]+i)},check=True)
        samples.extend(json.loads(proc.stdout))
    report={"round":"004","python":platform.python_version(),"implementation":platform.python_implementation(),"platform":platform.system()+"/"+platform.machine(),
            "clock":time.get_clock_info("perf_counter").implementation,"cfg":cfg,"samples":samples,"cells":summarize(samples),
            "timed_reviews":sum(r["calls"] for r in samples),"scope":"Host-specific, warmed, sequential wall times. No live inference or UI/network costs."}
    with path.open("x",encoding="utf-8") as f: json.dump(report,f,sort_keys=True,indent=2);f.write("\n")
    return {"timed_reviews":report["timed_reviews"],"batches":len(samples),"file":str(path),"cells":report["cells"]}

if __name__=="__main__":
    if len(sys.argv)==3 and sys.argv[1]=="--worker": print(json.dumps(worker(int(sys.argv[2]))))
    elif len(sys.argv)==2: print(json.dumps(save(sys.argv[1])))
    else: raise SystemExit("Provide a new JSON output filename.")
