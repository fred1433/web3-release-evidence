"""The eleven required checks. Each one records named assertions and evidence.

A check never decides the release on its own: it returns its assertions, and status.py
turns the set of results into Blocked / Incomplete / Demonstrated checks passed.
The bench reads the chain directly (ctx.chain); the app only sees the node through the proxy.
"""
import json, os, time
from harness import http_get, pad, post_json

FEE = 25 * 10**18
UNIT = 10**18


class Check:
    def __init__(self, cid):
        self.id = cid
        self.assertions = []
        self.evidence = {}

    def expect(self, name, ok, observed, expected):
        self.assertions.append({'name': name, 'ok': bool(ok), 'observed': observed, 'expected': expected})
        return ok


# ---------- interface helpers ----------

def open_app(ctx, page):
    page.goto(ctx.app_url + '/')
    page.wait_for_selector('body[data-ready="1"]', timeout=10000)


def connect(page):
    page.click('[data-testid=connect]')
    page.wait_for_selector('[data-testid=account]:not(:empty)', timeout=5000)


def sign_terms(page):
    page.click('[data-testid=accept]')
    page.wait_for_selector('[data-testid=accepted]:visible', timeout=5000)


def states(page):
    return page.evaluate('window.__uiStates')


def wait_state(page, wanted, timeout=8000):
    """True once the status history contains one of `wanted`; False on timeout (not an error)."""
    wanted = [wanted] if isinstance(wanted, str) else wanted
    try:
        page.wait_for_function('(w) => window.__uiStates.some(s => w.includes(s.state))', arg=wanted, timeout=timeout)
        return True
    except Exception:
        return False


def wait_pool(ctx, n, timeout=8.0):
    end = time.time() + timeout
    while time.time() < end:
        if ctx.chain.pending_count() >= n:
            return True
        time.sleep(0.05)
    raise TimeoutError(f'expected {n} pending transaction(s) in the mempool')


def wait_indexed(ctx, block, timeout=6.0):
    end = time.time() + timeout
    while time.time() < end:
        _, st = http_get(ctx.app_url + '/api/state')
        if st['indexedBlock'] >= block:
            return st
        time.sleep(0.1)
    return http_get(ctx.app_url + '/api/state')[1]


def status_hash(page):
    return page.inner_text('#status-hash').strip()


def history(sts, t0):
    return [f"{s['state']} at {(s['t'] - t0) / 1000:+.2f}s" for s in sts]


def token_balance(ctx, who):
    return ctx.chain.uint(ctx.addr['token'], ctx.sel['balanceOf'] + pad(who))


def grant_on_chain(ctx, gid):
    raw = ctx.chain.call(ctx.addr['grants'], ctx.sel['grants'] + pad(gid))[2:]
    w = [raw[i:i + 64] for i in range(0, len(raw), 64)]
    return {'owner': '0x' + w[0][24:], 'reader': '0x' + w[1][24:], 'paid': int(w[3], 16), 'active': int(w[4], 16) == 1}


def grant_count(ctx):
    return ctx.chain.uint(ctx.addr['grants'], ctx.sel['grantCount'])


def shot(ctx, page, name, selector='#app'):
    path = os.path.join(ctx.evidence_dir, f'{ctx.check_id}-{name}.png')
    page.locator(selector).screenshot(path=path)
    return os.path.relpath(path, ctx.reports_dir)


# ---------- checks ----------

def c01(ctx, c):
    page = ctx.page('user')
    open_app(ctx, page)
    connect(page)
    user = ctx.acc['user']
    c.expect('connected_account_matches', page.inner_text('[data-testid=account]') == user[:6] + '…' + user[-4:],
             page.inner_text('[data-testid=account]'), user[:6] + '…' + user[-4:])
    c.expect('chain_is_local_dev_chain', page.inner_text('[data-testid=network]') == 'Local dev chain',
             page.inner_text('[data-testid=network]'), 'Local dev chain')
    page.wait_for_function("document.querySelector('[data-testid=balance]').textContent !== '-'", timeout=5000)
    shown = float(page.inner_text('[data-testid=balance]').replace(',', ''))
    chain = token_balance(ctx, user) / UNIT
    c.expect('displayed_balance_matches_chain', shown == chain, f'{shown} TEST on screen', f'{chain} TEST on chain')
    c.evidence['screenshot'] = shot(ctx, page, 'connected')


def c02(ctx, c):
    page = ctx.page('user', rejectSend=True)
    open_app(ctx, page)
    connect(page)
    sign_terms(page)
    user = ctx.acc['user']
    nonce = ctx.chain.nonce(user)
    page.click('[data-testid=pay]')
    seen = wait_state(page, 'rejected', 5000)
    page.wait_for_timeout(500)
    sts = states(page)
    c.expect('ui_says_request_declined', seen, [s['state'] for s in sts], 'rejected')
    sent = ctx.proxy.count('eth_sendTransaction')
    c.expect('no_transaction_reached_node', sent == 0 and ctx.chain.pending_count() == 0,
             f'{sent} sent, {ctx.chain.pending_count()} pending', '0 sent, 0 pending')
    c.expect('nonce_unchanged', ctx.chain.nonce(user) == nonce, ctx.chain.nonce(user), nonce)
    c.expect('never_confirmed', not any(s['state'] == 'confirmed' for s in sts), [s['state'] for s in sts], 'no confirmed')
    c.evidence['wallet_answer'] = 'code 4001 (user rejected)'
    c.evidence['screenshot'] = shot(ctx, page, 'declined')


def c03(ctx, c):
    page = ctx.page('user')
    open_app(ctx, page)
    connect(page)
    sign_terms(page)
    c.expect('no_banner_on_right_chain', not page.is_visible('[data-testid=wrong-network]'), page.is_visible('[data-testid=wrong-network]'), False)
    page.evaluate("window.__testWallet.setChain('0x1')")
    page.wait_for_timeout(300)
    c.expect('banner_on_wrong_chain', page.is_visible('[data-testid=wrong-network]'), page.is_visible('[data-testid=wrong-network]'), True)
    c.expect('payment_disabled_on_wrong_chain', page.is_disabled('[data-testid=pay]'), page.is_disabled('[data-testid=pay]'), True)
    c.evidence['screenshot'] = shot(ctx, page, 'wrong-network')
    # A stale screen that still lets the click through must not send either.
    page.evaluate("const b=document.querySelector('[data-testid=pay]'); b.disabled=false; b.click();")
    page.wait_for_timeout(800)
    sent = ctx.proxy.count('eth_sendTransaction')
    c.expect('nothing_sent_on_wrong_chain', sent == 0 and ctx.chain.pending_count() == 0,
             f'{sent} sent, {ctx.chain.pending_count()} pending', '0 sent, 0 pending')
    page.click('[data-testid=switch-network]')
    page.wait_for_timeout(300)
    c.expect('switch_back_clears_banner', not page.is_visible('[data-testid=wrong-network]'),
             page.is_visible('[data-testid=wrong-network]'), False)
    c.evidence['wallet_calls'] = [x['method'] for x in page.evaluate('window.__walletCalls')]


def c04(ctx, c):
    page = ctx.page('user')
    open_app(ctx, page)
    connect(page)
    c.expect('payment_locked_until_terms_signed', page.is_disabled('[data-testid=pay]'), page.is_disabled('[data-testid=pay]'), True)
    sign_terms(page)
    page.click('[data-testid=pay]')
    wait_pool(ctx, 1)
    ctx.chain.mine()
    wait_state(page, 'confirmed', 8000)
    calls = [x['method'] for x in page.evaluate('window.__walletCalls')]
    i_sign = calls.index('personal_sign') if 'personal_sign' in calls else None
    i_send = calls.index('eth_sendTransaction') if 'eth_sendTransaction' in calls else None
    c.expect('terms_signed_before_transaction', i_sign is not None and i_send is not None and i_sign < i_send,
             f'personal_sign at call {i_sign}, eth_sendTransaction at call {i_send}', 'personal_sign first')
    user = ctx.acc['user']
    code, rec = http_get(f"{ctx.app_url}/api/acceptance/{user}")
    c.expect('api_holds_verified_acceptance', code == 200 and rec.get('verified') is True, f'HTTP {code}, verified={rec.get("verified")}', 'HTTP 200, verified=True')
    # A record signed by another key must be refused.
    terms = http_get(ctx.app_url + '/config.json')[1]['terms']
    forged = ctx.chain.rpc('personal_sign', ['0x' + terms.encode().hex(), ctx.acc['outsider']])
    try:
        r = post_json(ctx.app_url + '/api/acceptance', {'account': user, 'message': terms, 'signature': forged})
        forged_code = 201 if r.get('verified') else 0
    except Exception as e:
        forged_code = getattr(e, 'code', 0)
    c.expect('api_refuses_forged_acceptance', forged_code == 400, f'HTTP {forged_code}', 'HTTP 400')
    c.evidence['wallet_calls'] = calls
    c.evidence['acceptance_signature'] = rec.get('signature', '')[:20] + '…'


def c05(ctx, c):
    out = ctx.acc['outsider']
    reader_before = token_balance(ctx, ctx.acc['reader'])
    data = ctx.sel['grantAndPay'] + pad(ctx.acc['reader']) + pad(ctx.dataset_id) + pad(FEE)
    err = ctx.chain.simulate_error(out, ctx.addr['grants'], data)
    h = ctx.chain.send(out, ctx.addr['grants'], data, gas=300000)
    ctx.chain.mine()
    r = ctx.chain.receipt(h)
    c.expect('direct_call_reverted', r['status'] == '0x0', f"status {r['status']}", 'status 0x0')
    c.expect('revert_reason_is_NotAllowlisted', err == ctx.err['NotAllowlisted'], err, f"{ctx.err['NotAllowlisted']} NotAllowlisted()")
    c.expect('no_grant_created', grant_count(ctx) == 0 and token_balance(ctx, ctx.acc['reader']) == reader_before,
             f'{grant_count(ctx)} grants', '0 grants, reader balance unchanged')
    c.evidence.update({'tx': h, 'block': int(r['blockNumber'], 16), 'caller': 'outsider (not allowlisted, holds TEST, has approved)', 'path': 'eth_sendTransaction straight to the node, no interface'})


def c06(ctx, c):
    low = ctx.acc['user_low']
    page = ctx.page('user_low', fixedGas=hex(300000))
    open_app(ctx, page)
    connect(page)
    sign_terms(page)
    tok_before = (token_balance(ctx, low), token_balance(ctx, ctx.acc['reader']))
    native_before = ctx.chain.native(low)
    page.click('[data-testid=pay]')
    wait_pool(ctx, 1)
    page.wait_for_timeout(600)
    before_block = [s['state'] for s in states(page)]
    ctx.chain.mine()
    wait_state(page, ['reverted', 'confirmed'], 8000)
    page.wait_for_timeout(300)
    h = status_hash(page)
    r = ctx.chain.receipt(h)
    sts = [s['state'] for s in states(page)]
    c.expect('receipt_status_0', r is not None and r['status'] == '0x0', r and r['status'], '0x0')
    tok_after = (token_balance(ctx, low), token_balance(ctx, ctx.acc['reader']))
    c.expect('token_balances_unchanged', tok_after == tok_before, [x / UNIT for x in tok_after], [x / UNIT for x in tok_before])
    fee = int(r['gasUsed'], 16) * int(r['effectiveGasPrice'], 16)
    spent = native_before - ctx.chain.native(low)
    c.expect('native_balance_down_by_fee_only', spent == fee, f'{spent} wei', f'{fee} wei (gasUsed x effectiveGasPrice)')
    c.expect('ui_says_failed', 'reverted' in sts, sts, 'reverted')
    c.expect('never_confirmed', 'confirmed' not in sts, sts, 'no confirmed')
    c.evidence.update({'tx': h, 'block': int(r['blockNumber'], 16), 'gas_limit': 300000, 'gas_used': int(r['gasUsed'], 16), 'states_before_block': before_block})
    c.evidence['screenshot'] = shot(ctx, page, 'failed')


def c07(ctx, c):
    user, reader = ctx.acc['user'], ctx.acc['reader']
    page = ctx.page('user')
    open_app(ctx, page)
    connect(page)
    sign_terms(page)
    before = (token_balance(ctx, user), token_balance(ctx, reader))
    page.click('[data-testid=pay]')
    wait_pool(ctx, 1)
    page.wait_for_timeout(1000)  # time for the screen to claim anything it wants before the block
    t_mine = ctx.chain.mine()
    wait_state(page, 'confirmed', 8000)
    sts = states(page)
    early = [s for s in sts if s['state'] == 'confirmed' and s['t'] < t_mine]
    c.expect('no_confirmed_before_block', not early and any(s['state'] == 'confirmed' for s in sts),
             history(sts, t_mine), 'confirmed only after the block (t >= 0)')
    h = status_hash(page)
    r = ctx.chain.receipt(h)
    c.expect('receipt_status_1', r is not None and r['status'] == '0x1', r and r['status'], '0x1')
    after = (token_balance(ctx, user), token_balance(ctx, reader))
    c.expect('tokens_moved_once', before[0] - after[0] == FEE and after[1] - before[1] == FEE,
             f'user -{(before[0] - after[0]) / UNIT}, reader +{(after[1] - before[1]) / UNIT}', 'user -25, reader +25')
    g = grant_on_chain(ctx, 1)
    c.expect('grant_active_on_chain', g['active'] and g['owner'] == user.lower(), g, 'grant #1 active, owned by user')
    blk = int(r['blockNumber'], 16)
    st = wait_indexed(ctx, blk)
    api = [x for x in st['grants'] if x['id'] == '1']
    same = bool(api) and api[0]['active'] and api[0]['owner'] == g['owner'] and api[0]['reader'] == g['reader'] and int(api[0]['paid']) == g['paid']
    c.expect('api_matches_chain', same, api[0] if api else 'no grant in API', 'API grant #1 equals the chain')
    c.evidence.update({'tx': h, 'block': blk, 'ui_history': history(sts, t_mine)})
    c.evidence['screenshot'] = shot(ctx, page, 'confirmed')


def c08(ctx, c):
    user = ctx.acc['user']
    ctx.proxy.cut('eth_getTransactionReceipt')
    page = ctx.page('user')
    open_app(ctx, page)
    connect(page)
    sign_terms(page)
    head0 = ctx.chain.head()
    bal0 = token_balance(ctx, user)
    t_sent = int(time.time() * 1000)
    page.click('[data-testid=pay]')
    wait_pool(ctx, 1)
    page.wait_for_timeout(2500)  # several receipt polls, all cut by the proxy
    sts = states(page)
    h = status_hash(page)
    receipt = ctx.chain.receipt(h)
    head = ctx.chain.head()
    pending = ctx.chain.pending_count()
    _, api = http_get(f'{ctx.app_url}/api/grants?owner={user}')
    _, apistate = http_get(f'{ctx.app_url}/api/state')
    shown = [s for s in sts if s['state'] == 'confirmed']
    c.expect('never_confirmed_without_receipt', not shown,
             history(sts, t_sent) + [f'receipt on chain: {receipt}', f'blocks mined since the click: {head - head0}'],
             'no confirmed while the receipt does not exist')
    final = sts[-1]['state'] if sts else None
    c.expect('outage_shown_as_pending_or_unknown', final in ('submitted', 'unknown'), final, 'submitted or unknown')
    title = page.inner_text('#status-title')
    c.evidence.update({
        'screenshot': shot(ctx, page, 'outage'),
        'columns': {
            'interface': {'status': title, 'state': final, 'history': history(sts, t_sent), 'tx': h},
            'api': {'grants_for_user': len(api['grants']), 'indexed_block': apistate['indexedBlock']},
            'chain': {'receipt': receipt, 'pending_in_mempool': pending, 'latest_block': head,
                      'blocks_mined_since_click': head - head0, 'grant_count': grant_count(ctx),
                      'user_test_balance_unchanged': token_balance(ctx, user) == bal0},
        },
        'outage': 'proxy answered HTTP 503 to every eth_getTransactionReceipt; the bench read the node directly',
        'cut_requests': sum(1 for e in ctx.proxy.events if e['cut']),
    })


def c09(ctx, c):
    user = ctx.acc['user']
    ctx.proxy.cut('eth_getTransactionReceipt', 'eth_getTransactionByHash')
    page = ctx.page('user')
    open_app(ctx, page)
    connect(page)
    sign_terms(page)
    head0 = ctx.chain.head()
    bal0 = token_balance(ctx, user)
    t0 = int(time.time() * 1000)
    page.click('[data-testid=pay]')
    wait_pool(ctx, 1)
    unknown = wait_state(page, 'unknown', 6000)
    c.expect('outage_shown_as_unknown', unknown, [s['state'] for s in states(page)], 'unknown')
    ctx.proxy.restore()
    retry_label = page.inner_text('[data-testid=retry]') if page.is_visible('[data-testid=retry]') else None
    if retry_label:
        page.click('[data-testid=retry]')
    page.wait_for_timeout(1500)
    ctx.chain.mine()
    wait_state(page, 'confirmed', 8000)
    page.wait_for_timeout(300)
    payments = []
    for n in range(head0 + 1, ctx.chain.head() + 1):
        for tx in ctx.chain.block(n)['transactions']:
            if tx['from'].lower() == user.lower() and (tx['to'] or '').lower() == ctx.addr['grants'] and tx['input'].startswith(ctx.sel['grantAndPay']):
                rc = ctx.chain.receipt(tx['hash'])
                payments.append({'tx': tx['hash'], 'nonce': int(tx['nonce'], 16), 'block': n, 'status': rc['status']})
    mined_ok = [p for p in payments if p['status'] == '0x1']
    c.expect('one_payment_mined_per_intent', len(mined_ok) == 1, f'{len(mined_ok)} payment(s) mined: ' + ', '.join(f"nonce {p['nonce']}" for p in mined_ok), '1 payment')
    debit = bal0 - token_balance(ctx, user)
    c.expect('test_debited_once', debit == FEE, f'-{debit / UNIT} TEST', '-25 TEST')
    c.evidence.update({'retry_button': retry_label, 'payments': payments, 'ui_history': history(states(page), t0),
                       'screenshot': shot(ctx, page, 'after-retry')})


def c10(ctx, c):
    page = ctx.page('user')
    open_app(ctx, page)
    connect(page)
    c.expect('admin_control_hidden_for_user', not page.is_visible('[data-testid=admin-panel]'), page.is_visible('[data-testid=admin-panel]'), False)
    out = ctx.acc['outsider']
    data = ctx.sel['setAllowlisted'] + pad(out) + pad(1)
    err = ctx.chain.simulate_error(out, ctx.addr['grants'], data)
    h = ctx.chain.send(out, ctx.addr['grants'], data, gas=100000)
    ctx.chain.mine()
    r = ctx.chain.receipt(h)
    now = ctx.chain.uint(ctx.addr['grants'], ctx.sel['allowlisted'] + pad(out))
    c.expect('direct_admin_call_rejected', r['status'] == '0x0', f"status {r['status']}, simulated error {err}", f"status 0x0, {ctx.err['NotAdmin']} NotAdmin()")
    c.expect('allowlist_unchanged', now == 0, f'allowlisted(outsider) = {bool(now)}', 'False')
    c.evidence.update({'tx': h, 'block': int(r['blockNumber'], 16), 'caller': 'ordinary account, not the admin', 'call': 'setAllowlisted(self, true)', 'path': 'eth_sendTransaction straight to the node, no interface'})


def c11(ctx, c):
    user, reader = ctx.acc['user'], ctx.acc['reader']
    page = ctx.page('user')
    open_app(ctx, page)
    connect(page)
    sign_terms(page)
    page.click('[data-testid=pay]')
    wait_pool(ctx, 1)
    ctx.chain.mine()
    wait_state(page, 'confirmed', 8000)
    st = wait_indexed(ctx, ctx.chain.head())
    code1, _ = http_get(ctx.app_url + '/api/data/1', {'x-reader-address': reader})
    c.expect('api_serves_reader_while_active', code1 == 200, f'HTTP {code1}', 'HTTP 200')
    code_other, _ = http_get(ctx.app_url + '/api/data/1', {'x-reader-address': ctx.acc['outsider']})
    c.expect('api_refuses_other_reader', code_other == 403, f'HTTP {code_other}', 'HTTP 403')
    page.wait_for_selector('[data-testid=revoke-1]', timeout=5000)
    page.click('[data-testid=revoke-1]')
    wait_pool(ctx, 1)
    ctx.chain.mine()
    blk = ctx.chain.head()
    g = grant_on_chain(ctx, 1)
    c.expect('grant_inactive_on_chain', not g['active'], g['active'], False)
    st = wait_indexed(ctx, blk)
    code2, body2 = http_get(ctx.app_url + '/api/data/1', {'x-reader-address': reader})
    c.expect('api_refuses_after_revoke', code2 == 403, f'HTTP {code2} at indexed block {st["indexedBlock"]}', f'HTTP 403 once block {blk} is indexed')
    c.evidence.update({'revoke_block': blk, 'api_before': code1, 'api_after': code2})


CHECKS = {'C01': c01, 'C02': c02, 'C03': c03, 'C04': c04, 'C05': c05, 'C06': c06,
          'C07': c07, 'C08': c08, 'C09': c09, 'C10': c10, 'C11': c11}
