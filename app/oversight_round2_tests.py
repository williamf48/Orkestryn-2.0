"""Independent invariants for the second offline oversight experiment."""
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import zipfile

import oversight_pairs as pairs
import oversight_round2 as r
import safety_environment as world
import safety_lab


class RoundTwoTests(unittest.TestCase):
    def test_permission_and_harm_labels_do_not_consult_the_monitor(self):
        cases=safety_lab.dataset()["cases"]
        with mock.patch.object(world,"action_guard",side_effect=AssertionError("reference queried monitor")), mock.patch.object(world,"monitor",side_effect=AssertionError("reference queried monitor")):
            labels={c["id"]:r.legacy_labels(c) for c in cases}
        self.assertEqual({(v["authority"] is not None,v["harm"] is not None) for v in labels.values()},
                         {(True,True),(True,False),(False,True),(False,False)})
        for cid in json.loads(r.SPEC.read_bytes())["legacy"]["focus"]:
            self.assertIsNotNone(labels[cid]["authority"]);self.assertIsNone(labels[cid]["harm"])

    def test_repaired_booking_keeps_its_historical_first_harm(self):
        s=r.initial(True); events=[r.event(),r.event("book"),r.event("restore"),r.event("file")]
        ref=r.desk_replay(s,events)
        self.assertEqual((ref["authority"],ref["harm"],ref["task_complete"]),(None,2,True))
        self.assertFalse(r.final_snapshot_damaged(s,events))
        self.assertTrue(r.final_snapshot_damaged(s,events[:2]))
        self.assertFalse(r.desk_replay(r.initial(False),[r.event()])["task_complete"])

    def test_trees_cannot_inspect_a_withheld_feature(self):
        train=r.train_rows()
        full=r.fit(train,["record","prior_occupancy","requester"])
        notes=r.fit(train,["note"])
        changed=r.features(r.initial(True),r.event("book"))
        self.assertTrue(r.predict(full,changed));self.assertFalse(r.predict(full,{**changed,"prior_occupancy":"unreserved"}))
        self.assertEqual(r.predict(full,changed),r.predict(full,{**changed,"note":"new wording"}))
        for word in ("routine","review"):
            self.assertEqual(r.predict(notes,{**changed,"note":word}),r.predict(notes,{"note":word}))

    def test_pair_bounds_keep_absence_distinct_from_a_zero_score(self):
        items=[{"arm":"standard","match":"a","score":{"unauthorized_approvals":2,"timely_correct":4}},
               {"arm":"changes","match":"a","score":None}]
        res=pairs.analyze(items)
        self.assertEqual(res["endpoints"]["unauthorized_approvals"]["bounds_numerators"],[-2,1])
        self.assertEqual(res["endpoints"]["timely_correct"]["bounds_numerators"],[-4,2])
        self.assertIsNone(res["endpoints"]["timely_correct"]["exact_p_sharp_null"])
        items[1]["score"]=items[0]["score"]
        self.assertEqual(pairs.analyze(items)["endpoints"]["timely_correct"]["exact_p_sharp_null"],1)
        self.assertIsNone(pairs.analyze(items+[items[0]])["endpoints"]["timely_correct"]["exact_p_sharp_null"])
        items[1]["match"]="different";self.assertIsNone(pairs.analyze(items)["endpoints"]["timely_correct"])
        self.assertEqual(pairs.flip_test([1,1,1]),0.25)

    def test_recheck_observes_only_evidence_it_requests(self):
        fresh={"fresh":True,"status":"finished","approval":True}
        for values,expected,total in (([fresh],"release",1),([None,fresh],"release",2),
                                      ([{**fresh,"fresh":False},None],"defer",2),
                                      ([None,{**fresh,"approval":False}],"hold",2)):
            lookup=mock.Mock(side_effect=values)
            self.assertEqual(r.handoff_decision("one_recheck","release",lookup),expected)
            self.assertEqual(lookup.call_count,total)

    def test_locked_split_threshold_and_weighted_transfer(self):
        _,s=r.archive();l=s["legacy"];k=l["locked_transfer"]["either"]["selected_on_development"]
        self.assertTrue(r.meets(l["curves"]["development"][k]["either"]))
        self.assertFalse(r.meets(l["curves"]["heldout"][k]["either"]))
        self.assertEqual(l["locked_transfer"]["either"]["heldout_at_locked_budget"],l["curves"]["heldout"][k]["either"])
        late=s["budget"]["early_budget_on_late"];self.assertEqual((late["tp"],late["positive"]),(12,240))

    def test_all_adverse_and_unknown_rows_are_linked_and_intact(self):
        data,_=r.archive()
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            rows={q["id"]:q for q in map(json.loads,z.read("records.jsonl").splitlines())}
            for filename,field in (("failures.jsonl","failure_reasons"),("indeterminate.jsonl","indeterminate_reasons")):
                idx=list(map(json.loads,z.read(filename).splitlines()))
                self.assertEqual({q["id"] for q in idx},{i for i,x in rows.items() if x[field]})
                for q in idx:
                    row=rows[q["id"]];self.assertEqual(q["record_sha256"],hashlib.sha256(r.canonical(row)).hexdigest())
                    self.assertEqual(q["reasons"],row[field])
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/"evidence.zip";path.write_bytes(b"existing")
            with self.assertRaises(FileExistsError):r.save(path)
            self.assertEqual(path.read_bytes(),b"existing")
