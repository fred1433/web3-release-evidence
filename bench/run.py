"""Release evidence bench: one command builds, deploys, runs every required check and writes the report.

    python bench/run.py --variant faulty
    python bench/run.py --variant corrected
    python bench/run.py --variant corrected --skip C08        # a removed check makes the run Incomplete
    python bench/run.py --variant corrected --repeat 10       # stability: 10 independent runs
    python bench/run.py --variant corrected --regime moving   # one regime only: the run cannot pass
    python bench/run.py --variant faulty --expect Blocked --expect-detected D1,D2,D3
                                     # CI: exit 0 only if the status is Blocked AND exactly these planted
                                     # faults were detected, each on its critical assertion, with no error

Every check that opens the app runs in two regimes: "still" (the bench mines only when it decides)
and "moving" (a block every second while the user acts). Both are required.

Exit code without --expect: 0 Demonstrated checks passed, 1 Blocked, 3 Incomplete.
"""
import argparse, datetime, importlib.metadata, json, os, platform, shutil, subprocess, sys, time
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import ROOT, BlockProducer, Chain, RpcProxy, free_port, pad, start_anvil, start_app, stop  # noqa: E402
from checks import CHECKS, BenchTimeout, Check, screen_invariant  # noqa: E402
from status import decide, BLOCKED, INCOMPLETE, PASSED  # noqa: E402

CHAIN_ID = 31337
MANIFEST = json.load(open(os.path.join(ROOT, 'bench', 'required-checks.json')))
FAULTS = json.load(open(os.path.join(ROOT, 'variants', 'faults.json')))
WALLET_JS = open(os.path.join(ROOT, 'bench', 'wallet.js')).read()
PACIFIC = ZoneInfo('America/Los_Angeles')
REGIMES = MANIFEST['regimes']
BLOCK_INTERVAL_S = 1.0


def regimes_of(spec):
    return spec.get('regimes', [r['id'] for r in REGIMES])


def key(cid, regime):
    return f'{cid}/{regime}'


def required_keys():
    return [key(c['id'], r) for c in MANIFEST['checks'] for r in regimes_of(c)]


def stamp(dt):
    return {'utc': dt.astimezone(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC'),
            'pacific': dt.astimezone(PACIFIC).strftime('%Y-%m-%d %H:%M:%S %Z')}


def sh(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT).stdout.strip()
    except FileNotFoundError:
        return None


def require_tools():
    missing = [t for t in ('anvil', 'forge', 'node') if shutil.which(t) is None]
    if missing:
        sys.exit(f"Missing tool(s): {', '.join(missing)}. Install Foundry (https://getfoundry.sh) and Node 20 or newer.")


def compile_variant(variant):
    out = os.path.join('out', variant)
    r = subprocess.run(['forge', 'build', 'contracts', f'variants/{variant}', '--out', out, '--force'],
                       capture_output=True, text=True, cwd=ROOT)
    if r.returncode != 0:
        sys.exit('Compilation failed:\n' + r.stdout[-2000:] + r.stderr[-2000:])
    load = lambda f, c: json.load(open(os.path.join(ROOT, out, f, f'{c}.json')))['bytecode']['object']
    return load('TestToken.sol', 'TestToken'), load('SampleGrants.sol', 'SampleGrants')


class Ctx:
    def __init__(self, **kw):
        self.__dict__.update(kw)
        self.contexts = []
        self.ui_log = []  # every change of the status line, on every tab, sent by the test wallet as it happens

    def blocks_pass(self, n=1):
        """Moving chain: wait until n more blocks have come. Still chain: nothing passes."""
        if self.producer is None:
            return
        try:
            self.producer.wait_blocks(n)
        except TimeoutError as e:
            raise BenchTimeout(str(e)) from e

    def page(self, who, **wallet):
        cfg = {'account': self.acc[who], 'chainId': hex(CHAIN_ID), 'homeChainId': hex(CHAIN_ID), 'rpcUrl': self.proxy.url}
        cfg.update(wallet)
        context = self.browser.new_context(viewport={'width': 480, 'height': 900}, device_scale_factor=2)
        tab = len(self.contexts) + 1
        context.expose_binding('__benchRecord', lambda source, entry: self.ui_log.append(dict(entry, tab=tab)))
        context.add_init_script(script='window.__WALLET_CONFIG = ' + json.dumps(cfg) + ';\n' + WALLET_JS)
        self.contexts.append(context)
        page = context.new_page()
        if self.producer is not None:
            # Moving chain: a block arrives just before every user gesture, on top of one every second.
            def before(fn):
                def wrapped(*a, **kw):
                    self.producer.block_now(gesture=True)
                    return fn(*a, **kw)
                return wrapped
            for name in ('goto', 'reload', 'click', 'dblclick'):
                setattr(page, name, before(getattr(page, name)))
            page._bench_gesture = lambda: self.producer.block_now(gesture=True)
        return page

    def close(self):
        for c in self.contexts:
            try:
                c.close()
            except Exception:
                pass


def run_once(variant, skip, evidence_dir, reports_dir, browser, log_dir, only_regime=None):
    started = datetime.datetime.now(datetime.timezone.utc)
    t0 = time.time()
    token_code, grants_code = compile_variant(variant)
    anvil, node_url = start_anvil(free_port(), CHAIN_ID, os.path.join(log_dir, 'anvil.log'))
    chain = Chain(node_url)
    proxy = RpcProxy(node_url, chain.lock)
    results = []
    try:
        a = chain.rpc('eth_accounts')
        acc = {'admin': a[0].lower(), 'user': a[1].lower(), 'reader': a[2].lower(), 'user_low': a[3].lower(), 'outsider': a[4].lower()}
        sel = {k: chain.selector(s) for k, s in {
            'balanceOf': 'balanceOf(address)', 'mint': 'mint(address,uint256)', 'approve': 'approve(address,uint256)',
            'grants': 'grants(uint256)', 'grantCount': 'grantCount()',
            'allowlisted': 'allowlisted(address)', 'setAllowlisted': 'setAllowlisted(address,bool)', 'revoke': 'revoke(uint256)'}.items()}
        sel['grantAndPay'] = chain.selector('grantAndPay(address,bytes32,uint256,bytes32)')
        err = {k: chain.selector(k + '()') for k in ('NotAllowlisted', 'NotAdmin', 'OrderAlreadyPaid', 'NotCurrentOrder')}
        # Deploy and seed the fixture state, then snapshot it.
        token = chain.send_and_mine(acc['admin'], None, token_code)['contractAddress'].lower()
        grants = chain.send_and_mine(acc['admin'], None, grants_code + pad(token))['contractAddress'].lower()
        for who in ('user', 'outsider'):
            chain.send_and_mine(acc['admin'], token, sel['mint'] + pad(acc[who]) + pad(1000 * 10**18))
        for who in ('user', 'user_low'):
            chain.send_and_mine(acc['admin'], grants, sel['setAllowlisted'] + pad(acc[who]) + pad(1))
        for who in ('user', 'user_low', 'outsider'):
            chain.send_and_mine(acc[who], token, sel['approve'] + pad(grants) + 'f' * 64)
        setup_block = chain.head()
        snap = chain.rpc('evm_snapshot')
        app_env = {'RPC_URL': proxy.url, 'GRANTS_ADDRESS': grants, 'TOKEN_ADDRESS': token, 'ADMIN_ADDRESS': acc['admin'],
                   'READER_ADDRESS': acc['reader'], 'CHAIN_ID': hex(CHAIN_ID)}
        dataset_id = chain.keccak('sensor-log-S07')

        for spec in MANIFEST['checks']:
            cid = spec['id']
            for regime in regimes_of(spec):
                k = key(cid, regime)
                base = {'id': cid, 'regime': regime, 'key': k, 'title': spec['title']}
                if cid in skip or k in skip or (only_regime and regime != only_regime):
                    results.append(dict(base, outcome='skipped', assertions=[], evidence={}))
                    continue
                chain.rpc('evm_revert', [snap])
                snap = chain.rpc('evm_snapshot')
                chain.forget_blocks()
                proxy.reset()
                app, app_url = start_app(variant, app_env, os.path.join(log_dir, 'app.log'))
                c = Check(cid)
                producer = BlockProducer(chain, BLOCK_INTERVAL_S) if regime == 'moving' else None
                ctx = Ctx(variant=variant, chain=chain, proxy=proxy, app_url=app_url, browser=browser, acc=acc,
                          addr={'token': token, 'grants': grants}, sel=sel, err=err, dataset_id=dataset_id,
                          evidence_dir=evidence_dir, reports_dir=reports_dir, check_id=f'{cid}-{regime}', regime=regime, producer=producer)
                ct = time.time()
                head_start = chain.head()
                try:
                    if producer:
                        producer.start()
                    CHECKS[cid](ctx, c)
                    error = None if c.assertions else 'check recorded no assertion'
                except Exception as e:  # a crash or a timeout is never a detection
                    error = f'{type(e).__name__}: {str(e)[:300]}'
                finally:
                    if producer:
                        producer.stop()
                    ctx.close()
                    stop(app)
                if not error and c.waits_expired:
                    error = 'BenchTimeout: ' + '; '.join(c.waits_expired)[:300]
                if ctx.contexts:  # the check watched the screen: the confirmed-after-block invariant applies
                    try:
                        screen_invariant(ctx, c)
                    except Exception as e:
                        error = error or f'{type(e).__name__} in the screen invariant: {str(e)[:300]}'
                failed = [x['name'] for x in c.assertions if not x['ok']]
                # Only a named assertion that failed is a detection; an error with no failed assertion is not.
                outcome = 'failed' if failed else ('error' if error else 'passed')
                results.append(dict(base, outcome=outcome, failed_assertions=failed if outcome == 'failed' else [], error=error,
                                    waits_expired=c.waits_expired, assertions=c.assertions, evidence=c.evidence,
                                    blocks=chain.head() - head_start, empty_blocks=producer.count if producer else 0,
                                    blocks_before_gestures=producer.at_gesture if producer else 0,
                                    duration_s=round(time.time() - ct, 2)))
    finally:
        proxy.stop()
        stop(anvil)

    finished = datetime.datetime.now(datetime.timezone.utc)
    required = required_keys()
    verdict = decide(required, [dict(r, id=r['key']) for r in results])
    for r in results:
        spec = next(s for s in MANIFEST['checks'] if s['id'] == r['id'])
        r['trace'] = {k: spec[k] for k in ('rule', 'allowed', 'forbidden', 'criterion', 'layer', 'method')}
    dirty = sh(['git', 'status', '--porcelain', '--', '.', ':!reports', ':!site'])
    return {
        'schema': 'release-evidence/1',
        'kind': 'Recorded test run',
        'app': 'Sample Grants (synthetic application)',
        'variant': variant,
        'run_id': f"{variant}-{started.strftime('%Y%m%dT%H%M%SZ')}",
        'commit': sh(['git', 'rev-parse', 'HEAD']),
        'worktree_clean': dirty == '',
        'chain_id': CHAIN_ID,
        'setup_block': setup_block,
        'started': stamp(started),
        'finished': stamp(finished),
        'duration_s': round(time.time() - t0, 1),
        'versions': {
            'anvil': (sh(['anvil', '--version']) or '').splitlines()[0],
            'forge': (sh(['forge', '--version']) or '').splitlines()[0],
            'solc': '0.8.28',
            'node': sh(['node', '--version']),
            'python': platform.python_version(),
            'playwright': importlib.metadata.version('playwright'),
            'chromium': browser.version,
        },
        'status': verdict['status'],
        'client_mvp': 'not assessed',
        'counts': {k: verdict[k] for k in ('required', 'executed', 'passed')} | {
            'failed': len(verdict['failed']), 'errored': len(verdict['errored']), 'not_executed': len(verdict['not_executed'])},
        'failed': verdict['failed'],
        'errored': verdict['errored'],
        'not_executed': verdict['not_executed'],
        'regimes': REGIMES,
        'invariants': MANIFEST['invariants'],
        'status_rule': 'Blocked if any required check fails on a named assertion; otherwise Incomplete if any required check was skipped, timed out or crashed; otherwise Demonstrated checks passed.',
        'planted_faults': [dict(f, detected=detected(f, results), detected_in=detected_in(f, results)) for f in FAULTS['faults']],
        'checks': results,
        'not_covered': MANIFEST['not_covered'],
        'decision': 'The bench fills in the technical result only. The rules marked fictitious were written for this demo and approved by no one; go or no-go is a person\'s decision, taken with this report in hand.',
    }


def detected_in(fault, results):
    """The regimes in which the planted fault's own critical assertion failed."""
    return [r['regime'] for r in results if r['id'] == fault['detected_by'] and r.get('outcome') == 'failed'
            and fault['critical'] in (r.get('failed_assertions') or [])]


def detected(fault, results):
    """A planted fault counts as detected only if its own critical assertion failed."""
    return bool(detected_in(fault, results))


def calibration(report, expected):
    """Problems with a faulty run that must show exactly the expected detections, or [] if none."""
    problems = []
    found = sorted(f['id'] for f in report['planted_faults'] if f['detected'])
    if found != sorted(expected):
        problems.append(f"planted faults detected: {', '.join(found) or 'none'}; expected: {', '.join(sorted(expected)) or 'none'}")
    wanted_checks = sorted(f['detected_by'] for f in report['planted_faults'] if f['id'] in expected)
    failed_checks = sorted({k.split('/')[0] for k in report['failed']})
    if failed_checks != wanted_checks:
        problems.append(f"failed checks: {', '.join(failed_checks) or 'none'}; expected exactly: {', '.join(wanted_checks) or 'none'}")
    errors = [r['key'] for r in report['checks'] if r.get('error') and not (r['outcome'] == 'failed' and r['error'].startswith('BenchTimeout: ') and r.get('waits_expired'))]
    if errors or report['not_executed']:
        problems.append(f"checks with an error: {', '.join(errors) or 'none'}; not executed: {', '.join(report['not_executed']) or 'none'}")
    return problems


def main():
    ids = [c['id'] for c in MANIFEST['checks']]
    p = argparse.ArgumentParser(description='Run the release evidence bench against the Sample Grants synthetic app.')
    p.add_argument('--variant', required=True, choices=['faulty', 'corrected'])
    p.add_argument('--skip', action='append', default=[], metavar='CHECK_ID', help='leave a required check out of the run, in both regimes (C08) or in one (C08/moving); the run then cannot pass')
    p.add_argument('--regime', choices=[r['id'] for r in REGIMES], help='run one regime only; the other is left out, so the run cannot pass')
    p.add_argument('--out', help='report path (default reports/<variant>.json)')
    p.add_argument('--repeat', type=int, default=1, help='independent runs, each on a fresh chain (writes reports/stability-<variant>.json)')
    p.add_argument('--expect', choices=[BLOCKED, INCOMPLETE, PASSED], help='exit 0 only if the status equals this')
    p.add_argument('--expect-detected', metavar='IDS', help='comma-separated planted fault ids (or "none"): exit 0 only if exactly these were detected on their critical assertions, the failed checks are exactly theirs, and no check errored or was left out')
    args = p.parse_args()
    fault_ids = [f['id'] for f in FAULTS['faults']]
    expected = None
    if args.expect_detected is not None:
        expected = [] if args.expect_detected.strip().lower() == 'none' else [x.strip() for x in args.expect_detected.split(',') if x.strip()]
        bad = [x for x in expected if x not in fault_ids]
        if bad or (not expected and args.expect_detected.strip().lower() != 'none'):
            p.error(f"--expect-detected: unknown fault id(s): {', '.join(bad) or '(empty list)'}. Known: {', '.join(fault_ids)}, or none")
        if args.repeat != 1:
            p.error('--expect-detected works on a single run, not with --repeat')
    unknown = [s for s in args.skip if s not in ids and s not in required_keys()]
    if unknown:
        p.error(f"unknown check id(s): {', '.join(unknown)}. Known: {', '.join(ids)}, or one regime of a check such as C08/moving")
    if args.repeat < 1:
        p.error('--repeat must be 1 or more')
    require_tools()

    from playwright.sync_api import sync_playwright
    reports_dir = os.path.join(ROOT, 'reports')
    log_dir = os.path.join(ROOT, 'reports', 'logs')
    os.makedirs(log_dir, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        if args.repeat == 1:
            evidence = os.path.join(reports_dir, 'evidence', args.variant if not (args.skip or args.regime) else f'{args.variant}-partial')
            os.makedirs(evidence, exist_ok=True)
            report = run_once(args.variant, set(args.skip), evidence, reports_dir, browser, log_dir, args.regime)
            out = args.out or os.path.join(reports_dir, f'{args.variant}.json')
            json.dump(report, open(out, 'w'), indent=2)
            print(f"{report['run_id']}: {report['status']} | Client MVP: {report['client_mvp']}")
            c = report['counts']
            print(f"  {c['executed']} of {c['required']} required checks executed, {c['passed']} passed, {c['failed']} failed, {c['errored']} errored, {c['not_executed']} not executed")
            for r in report['checks']:
                tail = ', '.join(r.get('failed_assertions') or []) or (r.get('error') or '')
                print(f"  {r['id']} {r['regime']:6} {r['outcome']:8} {r['title']}{'  <- ' + tail if tail else ''}")
            print(f'  report: {os.path.relpath(out, ROOT)}')
            status = report['status']
            if expected is not None:
                problems = calibration(report, expected)
                for f in report['planted_faults']:
                    print(f"  {f['id']} {'detected' if f['detected'] else 'not detected':13} by {f['detected_by']} on {f['critical']}" + (f" ({', '.join(f['detected_in'])} chain)" if f['detected'] else ''))
                print('  calibration: ' + ('exactly the expected detections' if not problems else 'FAILED, ' + '; '.join(problems)))
        else:
            tmp = os.path.join(reports_dir, '.repeat-evidence')
            os.makedirs(tmp, exist_ok=True)
            runs = []
            for i in range(args.repeat):
                r = run_once(args.variant, set(args.skip), tmp, reports_dir, browser, log_dir, args.regime)
                runs.append({'run': i + 1, 'run_id': r['run_id'], 'status': r['status'], 'duration_s': r['duration_s'],
                             'outcomes': {x['key']: x['outcome'] for x in r['checks']},
                             'failed_assertions': {x['key']: x['failed_assertions'] for x in r['checks'] if x.get('failed_assertions')},
                             'errors': {x['key']: x['error'] for x in r['checks'] if x.get('error')},
                             'planted_faults_detected': [f['id'] for f in r['planted_faults'] if f['detected']]})
                print(f"run {i + 1:2}/{args.repeat}: {r['status']} in {r['duration_s']}s", flush=True)
            sig = lambda x: (x['status'], json.dumps(x['outcomes'], sort_keys=True), json.dumps(x['failed_assertions'], sort_keys=True))
            summary = {'variant': args.variant, 'runs': args.repeat, 'commit': r['commit'], 'versions': r['versions'],
                       'identical': len({sig(x) for x in runs}) == 1, 'statuses': {s: sum(1 for x in runs if x['status'] == s) for s in {x['status'] for x in runs}},
                       'finished': stamp(datetime.datetime.now(datetime.timezone.utc)), 'detail': runs}
            out = os.path.join(reports_dir, f'stability-{args.variant}.json')
            json.dump(summary, open(out, 'w'), indent=2)
            print(f"{args.repeat} runs, identical outcome every time: {summary['identical']} ({summary['statuses']}) -> {os.path.relpath(out, ROOT)}")
            status = runs[-1]['status'] if summary['identical'] else 'unstable'
        browser.close()
    if args.expect or expected is not None:
        ok = (not args.expect or status == args.expect) and (expected is None or not problems)
        sys.exit(0 if ok else 1)
    sys.exit({PASSED: 0, BLOCKED: 1}.get(status, 3))


if __name__ == '__main__':
    main()
