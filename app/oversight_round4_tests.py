"""Checks of boundary cases and proof claims in round 004, independent of the study counts."""
from itertools import combinations, product
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import copy
import io
import json
import unittest
import zipfile
import oversight_receipts as r
import oversight_round4 as study
import oversight_round2 as grader
import oversight_timing as timing
import safety_environment as world

class RoundFourTests(unittest.TestCase):
    def test_event_interval_contains_a_repaired_event_an_endpoint_cannot_see(self):
        c=next(c for group,c in study.transient_plans() if group=="transient")
        _,hist,_=world.execute(c["initial_state"],c["candidate"]["actions"])
        gold=grader.legacy_labels(c)["harm"]
        rec=r.checkpoint_feed(c["initial_state"],c["permissions"],hist,4)
        self.assertEqual(r.locate(rec,8,"snapshot_point")["status"],"none")
        self.assertEqual(r.locate(rec,8),{"status":"interval","lo":1,"hi":4})
        self.assertEqual(gold,2)
        self.assertEqual(r.locate([],8),{"status":"unknown","lo":1,"hi":8})

    def test_complete_negative_missing_tail_and_inconsistent_latch(self):
        rec=[{"at":4,"now":False,"ever":False}]
        self.assertEqual(r.locate(rec,4)["status"],"none")
        self.assertEqual(r.locate(rec,6),{"status":"unknown","lo":5,"hi":6})
        with self.assertRaises(ValueError): r.locate([{"at":1,"now":False,"ever":True},{"at":2,"now":False,"ever":False}],2)
        with self.assertRaises(ValueError): r.locate(rec*2,8)
        with self.assertRaises(ValueError): r.locate([{"at":1,"now":True,"ever":False}],1)

    def test_bounded_receipts_same_meaning_is_not_same_lifetime(self):
        for edit,wanted in (("none","reuse"),("key_order","reuse"),("note","reuse"),("scope","review"),("edit_restore","review"),("other_case","review"),("revoked","review")):
            receipt,cur,t=study.approval_fixture("approve",edit,60,False)
            self.assertEqual(r.reuse("material_receipt",receipt,cur,t),wanted,edit)
            self.assertEqual(r.reuse("material_receipt",receipt,cur,t+1),"review")
            self.assertEqual(r.reuse("material_receipt",receipt,cur,99),"review")
            receipt["spent"]=True
            self.assertEqual(r.reuse("material_receipt",receipt,cur,t),"review")

    def test_malformed_receipt_clock_and_missing_binding_are_unusable(self):
        rc,cur,t=study.approval_fixture("approve","none",0,False)
        for change in ({"issued_at":float("nan")},{"issued_at":True},{"case":None},{"material_revision":2.0},{"spent":0}):
            with self.subTest(change=change): self.assertEqual(r.reuse("material_receipt",{**rc,**change},cur,t),"review")
        short=dict(rc);del short["scope_hash"]
        self.assertEqual(r.reuse("material_receipt",short,cur,t),"review")
        self.assertEqual(r.reuse("material_receipt",rc,cur,float("nan")),"review")

    def test_refresh_rejects_copied_old_or_unavailable_votes(self):
        slots=[("A",0,True),("A",0,True),("B",1,True)]
        self.assertEqual(r.relay("origins",slots,1,lambda _:None)[0],"release")
        self.assertEqual(r.relay("current_origins",slots,1,lambda _:None)[0],"defer")
        for reply in (None,("A",0,True),("B",1,True),("A",1,False)):
            self.assertEqual(r.relay("refresh_one",slots,1,lambda _:reply)[0],"defer")
        self.assertEqual(r.relay("refresh_one",slots,1,lambda x:(x,1,True)),("release",4,"A"))

    def test_feasible_switch_positions_match_every_binary_completion_through_eight(self):
        for n in range(1,9):
            universe=list(product(("assistant","user"),repeat=n))
            gold={a:next((i for i,v in enumerate(a,1) if v!=a[0]),None) for a in universe}
            for bits in product((False,True),repeat=n-1):
                idx=(0,)+tuple(i for i,on in enumerate(bits,1) if on)
                buckets={}
                for seq in universe: buckets.setdefault(tuple(seq[i] for i in idx),set()).add(gold[seq])
                for pattern,possible in buckets.items():
                    self.assertEqual(set(r.switch_set(n,{i+1:v for i,v in zip(idx,pattern)})),possible,(n,idx,pattern))

    def test_even_samples_do_not_duplicate_positions_or_claim_no_change_after_a_gap(self):
        self.assertEqual(r.positions(10,4,"spread"),[1,4,7,10])
        for n,k in product(range(17),range(1,17)):
            positions=r.positions(n,k,"spread")
            self.assertEqual(len(set(positions)),min(n,k))
        self.assertEqual(r.switch_set(3,{1:"user",3:"user"}),[2,None])
        self.assertEqual(r.switch_set(3,{1:"user",3:"assistant"}),[2,3])

    def test_timing_wire_path_checks_real_candidate_schema(self):
        _,c=next(study.transient_plans());v=world.public_context(c)
        self.assertEqual(timing.wire_decode(world.canonical(v).encode()),v)
        bad=copy.deepcopy(v);del bad["untrusted_candidate"]["actions"][0]["actor"]
        with self.assertRaises(ValueError):timing.wire_decode(world.canonical(bad).encode())

    def test_evidence_indexes_and_existing_file_protection(self):
        raw,summary=study.archive()
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            cases=json.loads(z.read("cases.json"));self.assertEqual(len(cases),72)
            rows={json.loads(v)["id"]:v for v in z.read("observations.jsonl").splitlines(keepends=True)}
            self.assertEqual(len(rows),summary["observations"])
            for name in ("discrepancies","unresolved"):
                idx=[json.loads(x) for x in z.read(name+".jsonl").splitlines()]
                self.assertEqual({r["id"] for r in idx},{i for i,v in rows.items() if json.loads(v)[name]})
                self.assertTrue(all(sha256(rows[r["id"]]).hexdigest()==r["record_sha256"] for r in idx))
        with TemporaryDirectory() as d:
            f=Path(d)/"existing";f.write_bytes(b"keep")
            for fn in (study.save,timing.save):
                with self.assertRaises(FileExistsError):fn(f)
            self.assertEqual(f.read_bytes(),b"keep")
