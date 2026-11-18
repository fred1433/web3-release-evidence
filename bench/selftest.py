"""Self-test of the status rule (no chain, no browser). Run: python bench/selftest.py"""
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from status import decide, BLOCKED, INCOMPLETE, PASSED

ids = [c['id'] for c in json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'required-checks.json')))['checks']]
ok = lambda: [{'id': i, 'outcome': 'passed'} for i in ids]
cases = []
cases.append(('all required checks passed', ok(), PASSED))
r = ok(); r.pop(7); cases.append(('one check removed from the run', r, INCOMPLETE))
r = ok(); r[3]['outcome'] = 'error'; r[3]['error'] = 'Timeout 10000ms exceeded'; cases.append(('one check timed out', r, INCOMPLETE))
r = ok(); r[5]['outcome'] = 'skipped'; cases.append(('one check skipped', r, INCOMPLETE))
r = ok(); r[9]['outcome'] = 'failed'; cases.append(('one assertion failed', r, BLOCKED))
r = ok(); r[9]['outcome'] = 'failed'; r.pop(0); cases.append(('failure and a missing check', r, BLOCKED))
r = ok(); r.pop(2); r.append({'id': 'X99', 'outcome': 'passed'}); cases.append(('unknown check cannot fill a missing slot', r, INCOMPLETE))
r = ok() + [{'id': ids[0], 'outcome': 'passed'}]; cases.append(('duplicate result does not inflate the count', r, PASSED))
cases.append(('empty manifest cannot pass', [], None))

bad = 0
for name, results, expected in cases:
    got = decide(ids if expected is not None else [], results)
    exp = expected if expected is not None else INCOMPLETE
    flag = 'ok ' if got['status'] == exp else 'BAD'
    bad += got['status'] != exp
    print(f"{flag} {name}: {got['status']} ({got['executed']} of {got['required']} executed)")
sys.exit(1 if bad else 0)
