"""Mutants: each rule must turn false on an app that breaks it.

Every mutant copies the repository into a temporary folder, changes a few lines of the corrected
app (or of the faulty one for the CI case), runs the whole bench and compares the outcome with
what the mutant expects. Most must end Blocked. Two must pass: a correct but slow retry, and one
change that no longer matters since the intent comes from the contract. A retry button that does
nothing must end Incomplete: the bench never turns "nothing happened before the deadline" into a
detection.

    python bench/mutants.py                 # all mutants, writes reports/mutants.json
    python bench/mutants.py m1 m4 --jobs 2  # some of them

Exit code 0 only if every mutant ended as expected.
"""
import argparse, concurrent.futures, datetime, json, os, shutil, subprocess, sys, tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CF, CS, APP = 'variants/corrected/flow.js', 'variants/corrected/SampleGrants.sol', 'app/ui/app.js'
FF, FS = 'variants/faulty/flow.js', 'variants/faulty/SampleGrants.sol'
BLOCKED, INCOMPLETE, PASSED = 'Blocked', 'Incomplete', 'Demonstrated checks passed'
RETRY = "  ui.offerRetry(RETRY_LABEL, () => follow(ui, wallet, hash, opts));\n  return null;"
LOCKED = "    locked: (a) => inFlight.has(a) || Boolean(read(a)?.hash),"
ABANDON = "    abandon(a, rejected) { inFlight.delete(a); if (rejected) write(a, null); },"
ADMIN = "    function setAllowlisted(address account, bool allowed) external onlyAdmin {"

# id: (rule it breaks, what it does, expected status, [(file, old, new), ...])
MUTANTS = {
    'late_d1': ('D1', 'Confirmed from the transaction lookup after about 3 s of failed receipts.', BLOCKED, [
        (CF, "      failures += 1;\n      if (failures >= 3) return lost(ui, wallet, hash, opts);",
         "      failures += 1;\n      if (failures >= 12) { try { const tx = await wallet.request({ method: 'eth_getTransactionByHash', params: [hash] }); if (tx) { ui.setState('confirmed', hash); return tx; } } catch (_) {} }\n      if (failures >= 20) return lost(ui, wallet, hash, opts);")]),
    'unknown_then_confirmed': ('D1', 'Shows unknown, then switches to Confirmed 3 s later from the transaction lookup.', BLOCKED, [
        (CF, RETRY, "  ui.offerRetry(RETRY_LABEL, () => follow(ui, wallet, hash, opts));\n  setTimeout(async () => { try { const tx = await wallet.request({ method: 'eth_getTransactionByHash', params: [hash] }); if (tx) ui.setState('confirmed', hash); } catch (_) {} }, 3000);\n  return null;")]),
    'label_lies': ('D1', 'The state says unknown, the visible text says Confirmed.', BLOCKED, [
        (APP, "  unknown: 'Status unknown: the network did not answer',", "  unknown: 'Confirmed',")]),
    'm1_confirm_on_retry': ('D1', 'Check status again shows Confirmed as soon as the node knows the transaction, before any block.', BLOCKED, [
        (CF, RETRY, "  ui.offerRetry(RETRY_LABEL, async () => {\n    try {\n      const tx = await wallet.request({ method: 'eth_getTransactionByHash', params: [hash] });\n      if (tx) { ui.setState('confirmed', hash); if (opts.done) opts.done(tx); return tx; }\n    } catch (_) { /* still down */ }\n    return follow(ui, wallet, hash, opts);\n  });\n  return null;")]),
    'weak_d2': ('D2', 'Only adding is guarded: anyone can remove anyone.', BLOCKED, [
        (CS, ADMIN, "    function setAllowlisted(address account, bool allowed) external {\n        if (allowed && msg.sender != admin) revert NotAdmin();")]),
    'allowlisted_admin': ('D2', 'Any allowlisted account can edit the allowlist.', BLOCKED, [
        (CS, ADMIN, "    function setAllowlisted(address account, bool allowed) external {\n        if (msg.sender != admin && !allowlisted[msg.sender]) revert NotAdmin();")]),
    'wrong_error': ('D2', 'Refused, but with another error than NotAdmin.', BLOCKED, [
        (CS, ADMIN, "    function setAllowlisted(address account, bool allowed) external {\n        if (msg.sender != admin) revert NotAllowlisted();")]),
    'no_retry': ('D3', 'No retry button after status unknown.', BLOCKED, [(CF, RETRY, "  return null;")]),
    'retry_noop': ('D3', 'The retry button does nothing. Nothing happening is a passed deadline, not a detection: the run ends Incomplete, which cannot pass either.', INCOMPLETE, [(CF, RETRY, "  ui.offerRetry(RETRY_LABEL, () => {});\n  return null;")]),
    'pay_unlocked': ('D3', 'The Pay button is never locked (the contract still reverts the duplicate).', BLOCKED, [(CF, LOCKED, "    locked: () => false,")]),
    'no_storage': ('D3', 'Nothing survives a reload: the transaction in progress is kept in memory only.', BLOCKED, [
        (CF, "export function createPayments({ wallet, cfg, storage = window.localStorage }) {",
         "export function createPayments({ wallet, cfg, storage = { m: new Map(), getItem(k) { return this.m.get(k) ?? null; }, setItem(k, v) { this.m.set(k, v); }, removeItem(k) { this.m.delete(k); } } }) {")]),
    'contract_no_intent': ('D3', 'The contract no longer refuses a paid intent.', BLOCKED, [
        (CS, "        if (intentPaid[msg.sender][intent]) revert IntentAlreadyPaid();\n", "")]),
    'retry_repays_same_intent': ('D3', 'The retry pays again with the same intent: one debit, one reverted duplicate.', BLOCKED, [
        (CF, LOCKED, "    locked: () => false,"), (CF, RETRY, "  ui.offerRetry(RETRY_LABEL, opts.payAgain);\n  return null;")]),
    'browser_intent': ('D3', 'The intent is made in the browser again instead of being asked from the contract.', BLOCKED, [
        (CF, "      try { return await call(cfg.selectors.intentFor + pad(a) + pad(cfg.reader) + pad(cfg.datasetId)); } catch (e) { inFlight.delete(a); throw e; }",
         "      return '0x' + [...crypto.getRandomValues(new Uint8Array(32))].map((b) => b.toString(16).padStart(2, '0')).join('');")]),
    'm3_new_intent_after_wallet_error': ('D3', 'After any wallet error the app forgets the order and makes a new intent in the browser.', BLOCKED, [
        (CF, ABANDON, "    abandon(a, rejected) { inFlight.delete(a); write(a, null); forgot.add(a); },"),
        (CF, "  const inFlight = new Set();", "  const inFlight = new Set();\n  const forgot = new Set();"),
        (CF, "      inFlight.add(a);\n      try {", "      inFlight.add(a);\n      if (forgot.has(a)) return '0x' + [...crypto.getRandomValues(new Uint8Array(32))].map((b) => b.toString(16).padStart(2, '0')).join('');\n      try {")]),
    'm3_as_written': ('D3', 'The reviewer\'s M3 line as written: the browser record is wiped on any wallet error. The intent no longer lives there, so this changes nothing: the next Pay asks the contract and gets the same intent.', PASSED, [
        (CF, ABANDON, "    abandon(a) { inFlight.delete(a); write(a, null); },")]),
    'm4_slow_retry': ('waits', 'Correct but slow: the retry checks the same transaction after a 2 s backoff.', PASSED, [
        (CF, RETRY, "  ui.offerRetry(RETRY_LABEL, async () => { await sleep(2000); return follow(ui, wallet, hash, opts); });\n  return null;")]),
    'ci_only_d2': ('CI', 'A faulty app that keeps only D2 (D1 and D3 fixed) must fail the CI calibration.', 'calibration fails', None),
}


def make_clone(name, base):
    dst = os.path.join(base, name)
    shutil.copytree(ROOT, dst, ignore=shutil.ignore_patterns('.venv', '.git', 'reports', 'site', 'out', 'cache', '.wrangler', '__pycache__', 'node_modules'))
    os.makedirs(os.path.join(dst, 'reports'), exist_ok=True)
    return dst


def apply(dst, name):
    changes = MUTANTS[name][3]
    if changes is None:  # ci_only_d2: the corrected app with onlyAdmin removed, in place of the faulty one
        shutil.copy(os.path.join(dst, CF), os.path.join(dst, FF))
        s = open(os.path.join(dst, CS)).read()
        assert s.count(ADMIN) == 1
        open(os.path.join(dst, FS), 'w').write(s.replace(ADMIN, ADMIN.replace(' onlyAdmin', '')))
        return
    for f, old, new in changes:
        path = os.path.join(dst, f)
        s = open(path).read()
        if s.count(old) != 1:
            raise SystemExit(f'{name}: the line to change is not in {f} exactly once: {old[:70]!r}')
        open(path, 'w').write(s.replace(old, new))


def run_one(name, base):
    rule, what, expected, _ = MUTANTS[name]
    dst = make_clone(name, base)
    apply(dst, name)
    if name == 'ci_only_d2':
        args = ['--variant', 'faulty', '--expect', BLOCKED, '--expect-detected', 'D1,D2,D3']
    else:
        args = ['--variant', 'corrected']
    out = os.path.join(dst, 'reports', 'run.json')
    r = subprocess.run([sys.executable, os.path.join(dst, 'bench', 'run.py')] + args + ['--out', out], capture_output=True, text=True)
    rep = json.load(open(out)) if os.path.exists(out) else {}
    status = rep.get('status', 'no report')
    failed = {c['id']: c['failed_assertions'] for c in rep.get('checks', []) if c.get('failed_assertions')}
    errors = {c['id']: c['error'] for c in rep.get('checks', []) if c.get('error')}
    calib = next((l.split('calibration: ', 1)[1] for l in r.stdout.splitlines() if 'calibration: ' in l), None)
    if name == 'ci_only_d2':
        ok = r.returncode != 0 and calib is not None and calib.startswith('FAILED')
        outcome = f'exit {r.returncode}, calibration {calib}'
    else:
        ok = status == expected
        outcome = status
    return {'id': name, 'breaks': rule, 'what': what, 'expected': expected, 'outcome': outcome, 'as_expected': ok,
            'exit': r.returncode, 'failed_assertions': failed, 'errors': errors, 'duration_s': rep.get('duration_s')}


def main():
    p = argparse.ArgumentParser(description='Run the bench against mutants of the app.')
    p.add_argument('names', nargs='*', help='mutant ids (default: all). Known: ' + ', '.join(MUTANTS))
    p.add_argument('--jobs', type=int, default=1, help='mutants run at the same time (default 1)')
    p.add_argument('--out', help='report path (default reports/mutants.json when every mutant runs)')
    args = p.parse_args()
    unknown = [n for n in args.names if n not in MUTANTS]
    if unknown:
        p.error(f"unknown mutant id(s): {', '.join(unknown)}. Known: {', '.join(MUTANTS)}")
    if args.jobs < 1:
        p.error('--jobs must be 1 or more')
    names = args.names or list(MUTANTS)
    results = {}
    with tempfile.TemporaryDirectory(prefix='mutants-') as base:
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as ex:
            futs = {ex.submit(run_one, n, base): n for n in names}
            for f in concurrent.futures.as_completed(futs):
                x = f.result()
                results[x['id']] = x
                where = '; '.join(f"{k}: {', '.join(v)}" for k, v in x['failed_assertions'].items()) or '; '.join(f'{k}: {v}' for k, v in x['errors'].items())
                print(f"{'ok  ' if x['as_expected'] else 'BAD '} {x['id']:34} {x['outcome']:28} expected {x['expected']}" + (f'  <- {where[:300]}' if where else ''), flush=True)
    ordered = [results[n] for n in names]
    good = sum(1 for x in ordered if x['as_expected'])
    print(f'{good} of {len(ordered)} mutants ended as expected')
    out = args.out or (os.path.join(ROOT, 'reports', 'mutants.json') if not args.names else None)
    if out:
        commit = subprocess.run(['git', 'rev-parse', 'HEAD'], capture_output=True, text=True, cwd=ROOT).stdout.strip() or None
        json.dump({'finished': datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC'), 'commit': commit,
                   'as_expected': good, 'total': len(ordered), 'mutants': ordered}, open(out, 'w'), indent=2)
        print('report: ' + os.path.relpath(out, ROOT))
    sys.exit(0 if good == len(ordered) else 1)


if __name__ == '__main__':
    main()
