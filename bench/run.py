"""Release evidence bench: one command builds, deploys, runs every required check and writes the report.

    python bench/run.py --variant faulty
    python bench/run.py --variant corrected
    python bench/run.py --variant corrected --skip C08        # a removed check makes the run Incomplete
    python bench/run.py --variant corrected --repeat 20       # stability: 20 independent runs
    python bench/run.py --variant faulty --expect Blocked     # CI: exit 0 only if the status is the expected one

Exit code without --expect: 0 Demonstrated checks passed, 1 Blocked, 3 Incomplete.
"""
import argparse, datetime, importlib.metadata, json, os, platform, shutil, subprocess, sys, time
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import ROOT, Chain, RpcProxy, free_port, pad, start_anvil, start_app, stop  # noqa: E402
from checks import CHECKS, Check  # noqa: E402
from status import decide, BLOCKED, INCOMPLETE, PASSED  # noqa: E402

CHAIN_ID = 31337
MANIFEST = json.load(open(os.path.join(ROOT, 'bench', 'required-checks.json')))
FAULTS = json.load(open(os.path.join(ROOT, 'variants', 'faults.json')))
WALLET_JS = open(os.path.join(ROOT, 'bench', 'wallet.js')).read()
PACIFIC = ZoneInfo('America/Los_Angeles')


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

    def page(self, who, **wallet):
        cfg = {'account': self.acc[who], 'chainId': hex(CHAIN_ID), 'homeChainId': hex(CHAIN_ID), 'rpcUrl': self.proxy.url}
        cfg.update(wallet)
        context = self.browser.new_context(viewport={'width': 480, 'height': 900}, device_scale_factor=2)
        context.add_init_script(script='window.__WALLET_CONFIG = ' + json.dumps(cfg) + ';\n' + WALLET_JS)
        self.contexts.append(context)
        return context.new_page()

    def close(self):
        for c in self.contexts:
            try:
                c.close()
            except Exception:
                pass


def run_once(variant, skip, evidence_dir, reports_dir, browser, log_dir):
    started = datetime.datetime.now(datetime.timezone.utc)
    t0 = time.time()
    token_code, grants_code = compile_variant(variant)
    anvil, node_url = start_anvil(free_port(), CHAIN_ID, os.path.join(log_dir, 'anvil.log'))
    proxy = RpcProxy(node_url)
    results = []
    try:
        chain = Chain(node_url)
        a = chain.rpc('eth_accounts')
        acc = {'admin': a[0].lower(), 'user': a[1].lower(), 'reader': a[2].lower(), 'user_low': a[3].lower(), 'outsider': a[4].lower()}
        sel = {k: chain.selector(s) for k, s in {
            'balanceOf': 'balanceOf(address)', 'mint': 'mint(address,uint256)', 'approve': 'approve(address,uint256)',
            'grantAndPay': 'grantAndPay(address,bytes32,uint256)', 'grants': 'grants(uint256)', 'grantCount': 'grantCount()',
            'allowlisted': 'allowlisted(address)', 'setAllowlisted': 'setAllowlisted(address,bool)'}.items()}
        err = {k: chain.selector(k + '()') for k in ('NotAllowlisted', 'NotAdmin')}
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
            if cid in skip:
                results.append({'id': cid, 'title': spec['title'], 'outcome': 'skipped', 'assertions': [], 'evidence': {}})
                continue
            chain.rpc('evm_revert', [snap])
            snap = chain.rpc('evm_snapshot')
            proxy.reset()
            app, app_url = start_app(variant, app_env, os.path.join(log_dir, 'app.log'))
            c = Check(cid)
            ctx = Ctx(variant=variant, chain=chain, proxy=proxy, app_url=app_url, browser=browser, acc=acc,
                      addr={'token': token, 'grants': grants}, sel=sel, err=err, dataset_id=dataset_id,
                      evidence_dir=evidence_dir, reports_dir=reports_dir, check_id=cid)
            ct = time.time()
            try:
                CHECKS[cid](ctx, c)
                failed = [x['name'] for x in c.assertions if not x['ok']]
                outcome = 'failed' if failed else ('passed' if c.assertions else 'error')
                error = None if c.assertions else 'check recorded no assertion'
            except Exception as e:  # a crash or a timeout is never a detection
                failed, outcome, error = [x['name'] for x in c.assertions if not x['ok']], 'error', f'{type(e).__name__}: {str(e)[:300]}'
            finally:
                ctx.close()
                stop(app)
            results.append({'id': cid, 'title': spec['title'], 'outcome': outcome,
                            'failed_assertions': failed if outcome == 'failed' else [], 'error': error,
                            'assertions': c.assertions, 'evidence': c.evidence, 'duration_s': round(time.time() - ct, 2)})
    finally:
        proxy.stop()
        stop(anvil)

    finished = datetime.datetime.now(datetime.timezone.utc)
    required = [c['id'] for c in MANIFEST['checks']]
    verdict = decide(required, results)
    by_id = {r['id']: r for r in results}
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
        'status_rule': 'Blocked if any required check fails on a named assertion; otherwise Incomplete if any required check was skipped, timed out or crashed; otherwise Demonstrated checks passed.',
        'planted_faults': [dict(f, detected=by_id.get(f['detected_by'], {}).get('outcome') == 'failed') for f in FAULTS['faults']],
        'checks': results,
        'not_covered': MANIFEST['not_covered'],
        'decision': {
            'rule_approval': 'Not given. The demo rules marked fictitious were written for this demo; real rules are approved by the product owner and counsel.',
            'technical_verification': verdict['status'],
            'launch_decision': 'Not taken by the bench. A named person signs go or no-go with this report attached.',
        },
    }


def main():
    ids = [c['id'] for c in MANIFEST['checks']]
    p = argparse.ArgumentParser(description='Run the release evidence bench against the Sample Grants synthetic app.')
    p.add_argument('--variant', required=True, choices=['faulty', 'corrected'])
    p.add_argument('--skip', action='append', default=[], metavar='CHECK_ID', help='leave a required check out of the run (the run then cannot pass)')
    p.add_argument('--out', help='report path (default reports/<variant>.json)')
    p.add_argument('--repeat', type=int, default=1, help='independent runs, each on a fresh chain (writes reports/stability-<variant>.json)')
    p.add_argument('--expect', choices=[BLOCKED, INCOMPLETE, PASSED], help='exit 0 only if the status equals this')
    args = p.parse_args()
    unknown = [s for s in args.skip if s not in ids]
    if unknown:
        p.error(f"unknown check id(s): {', '.join(unknown)}. Known: {', '.join(ids)}")
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
            evidence = os.path.join(reports_dir, 'evidence', args.variant if not args.skip else f'{args.variant}-skip')
            os.makedirs(evidence, exist_ok=True)
            report = run_once(args.variant, set(args.skip), evidence, reports_dir, browser, log_dir)
            out = args.out or os.path.join(reports_dir, f'{args.variant}.json')
            json.dump(report, open(out, 'w'), indent=2)
            print(f"{report['run_id']}: {report['status']} | Client MVP: {report['client_mvp']}")
            c = report['counts']
            print(f"  {c['executed']} of {c['required']} required checks executed, {c['passed']} passed, {c['failed']} failed, {c['errored']} errored, {c['not_executed']} not executed")
            for r in report['checks']:
                tail = ', '.join(r.get('failed_assertions') or []) or (r.get('error') or '')
                print(f"  {r['id']} {r['outcome']:8} {r['title']}{'  <- ' + tail if tail else ''}")
            print(f'  report: {os.path.relpath(out, ROOT)}')
            status = report['status']
        else:
            tmp = os.path.join(reports_dir, '.repeat-evidence')
            os.makedirs(tmp, exist_ok=True)
            runs = []
            for i in range(args.repeat):
                r = run_once(args.variant, set(args.skip), tmp, reports_dir, browser, log_dir)
                runs.append({'run': i + 1, 'run_id': r['run_id'], 'status': r['status'], 'duration_s': r['duration_s'],
                             'outcomes': {x['id']: x['outcome'] for x in r['checks']},
                             'failed_assertions': {x['id']: x['failed_assertions'] for x in r['checks'] if x.get('failed_assertions')},
                             'errors': {x['id']: x['error'] for x in r['checks'] if x.get('error')}})
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
    if args.expect:
        sys.exit(0 if status == args.expect else 1)
    sys.exit({PASSED: 0, BLOCKED: 1}.get(status, 3))


if __name__ == '__main__':
    main()
