"""The eleven required checks. Each one records named assertions and evidence.

A check never decides the release on its own: it returns its assertions, and status.py
turns the set of results into Blocked / Incomplete / Demonstrated checks passed.
The bench reads the chain directly (ctx.chain); the app only sees the node through the proxy.

Waiting is never a verdict: when something the bench waits for does not happen in time
(a screen state, a transaction in the mempool, the indexer catching up), it raises
BenchTimeout, the check ends in error and the run is Incomplete. Only a named assertion
that fails is a detection.
"""
import os, re, time
from harness import http_get, pad, post_json

FEE = 25 * 10**18
UNIT = 10**18
TERMINAL = ['unknown', 'confirmed', 'reverted', 'error', 'rejected']
OUTAGE_MARGIN_MS = 5000  # how long the outage lasts after the screen reaches a terminal state
CONFIRMED_WORD = re.compile(r'(?<![a-z])(?<!not )confirmed\b', re.I)


class BenchTimeout(Exception):
    """What the bench waited for did not happen in time. Never a detection."""


class BenchError(Exception):
    """The bench could not set up the situation it needs. Never a detection."""


class Check:
    def __init__(self, cid):
        self.id = cid
        self.assertions = []
        self.evidence = {}

    def expect(self, name, ok, observed, expected):
        self.assertions.append({'name': name, 'ok': bool(ok), 'observed': observed, 'expected': expected})
        return ok


def show(v):
    """A value as the report shows it: none / true / false, never a Python repr."""
    if v is None:
        return 'none'
    if isinstance(v, bool):
        return 'true' if v else 'false'
    return str(v)


def now_ms():
    return int(time.time() * 1000)


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


def ready_page(ctx, who='user', **wallet):
    page = ctx.page(who, **wallet)
    open_app(ctx, page)
    connect(page)
    sign_terms(page)
    return page


def force_click(page, selector):
    """Click even if the button is disabled: a stale screen or a script must not get through either."""
    page.evaluate("(s) => { const b = document.querySelector(s); b.disabled = false; b.click(); }", selector)


def states(page):
    return page.evaluate('window.__uiStates')


def wait_state(page, wanted, timeout=8000):
    """Wait until the status history contains one of `wanted`. Raises BenchTimeout otherwise."""
    wanted = [wanted] if isinstance(wanted, str) else wanted
    try:
        page.wait_for_function('(w) => window.__uiStates.some(s => w.includes(s.state))', arg=wanted, timeout=timeout)
    except Exception as e:
        raise BenchTimeout(f"the screen did not show {' or '.join(wanted)} within {timeout / 1000:g} s") from e


def wait_current(page, wanted, timeout=8000):
    """Wait until the status line currently shows `wanted`. Raises BenchTimeout otherwise."""
    try:
        page.wait_for_function('(w) => { const s = window.__uiStates; return s.length && s[s.length - 1].state === w; }', arg=wanted, timeout=timeout)
    except Exception as e:
        raise BenchTimeout(f'the screen did not end on {wanted} within {timeout / 1000:g} s') from e


def wait_pool(ctx, n, timeout=8.0):
    end = time.time() + timeout
    while time.time() < end:
        if ctx.chain.pending_count() >= n:
            return True
        time.sleep(0.05)
    raise BenchTimeout(f'expected {n} pending transaction(s) in the mempool within {timeout:g} s')


def wait_indexed(ctx, block, timeout=6.0):
    end = time.time() + timeout
    while time.time() < end:
        _, st = http_get(ctx.app_url + '/api/state')
        if st['indexedBlock'] >= block:
            return st
        time.sleep(0.1)
    raise BenchTimeout(f'the app indexer did not reach block {block} within {timeout:g} s')


def status_hash(page):
    return page.inner_text('#status-hash').strip()


def claims_confirmed(s):
    return s['state'] == 'confirmed' or bool(CONFIRMED_WORD.search(s.get('text') or ''))


def history(sts, t0):
    return [f"{s['state']} \"{s.get('text', '')}\" at {(s['t'] - t0) / 1000:+.2f}s" for s in sts]


def history_rows(sts, t0):
    return [{'state': s['state'], 'text': s.get('text', ''), 'at_s': round((s['t'] - t0) / 1000, 2)} for s in sts]


def token_balance(ctx, who):
    return ctx.chain.uint(ctx.addr['token'], ctx.sel['balanceOf'] + pad(who))


def grant_on_chain(ctx, gid):
    raw = ctx.chain.call(ctx.addr['grants'], ctx.sel['grants'] + pad(gid))[2:]
    w = [raw[i:i + 64] for i in range(0, len(raw), 64)]
    return {'owner': '0x' + w[0][24:], 'reader': '0x' + w[1][24:], 'paid': int(w[3], 16), 'active': int(w[4], 16) == 1}


def grant_count(ctx):
    return ctx.chain.uint(ctx.addr['grants'], ctx.sel['grantCount'])


def allowlisted(ctx, who):
    return ctx.chain.uint(ctx.addr['grants'], ctx.sel['allowlisted'] + pad(who)) == 1


def error_name(ctx, sel):
    names = {v: k + '()' for k, v in ctx.err.items()}
    return names.get(sel, show(sel))


def pay_data(ctx, intent):
    return ctx.sel['grantAndPay'] + pad(ctx.acc['reader']) + pad(ctx.dataset_id) + pad(FEE) + pad(intent)


def is_payment(ctx, tx, who):
    return (tx['from'].lower() == who.lower() and (tx.get('to') or '').lower() == ctx.addr['grants']
            and tx['input'].startswith(ctx.sel['grantAndPay']))


def payments_since(ctx, head0, who):
    """Every payment transaction `who` sent since block head0, mined (with its receipt status) or still waiting."""
    out = []
    for n in range(head0 + 1, ctx.chain.head() + 1):
        for tx in ctx.chain.block(n)['transactions']:
            if is_payment(ctx, tx, who):
                rc = ctx.chain.receipt(tx['hash'])
                out.append({'tx': tx['hash'], 'nonce': int(tx['nonce'], 16), 'block': n, 'status': rc['status'], 'intent': '0x' + tx['input'][-64:]})
    for tx in ctx.chain.pending_txs():
        if is_payment(ctx, tx, who):
            out.append({'tx': tx['hash'], 'nonce': int(tx['nonce'], 16), 'block': None, 'status': 'pending', 'intent': '0x' + tx['input'][-64:]})
    return out


def describe(pays):
    if not pays:
        return 'no payment transaction'
    return f"{len(pays)} payment transaction{'s' if len(pays) > 1 else ''}: " + ', '.join(
        f"nonce {p['nonce']} " + ('waiting in the mempool' if p['block'] is None else f"block {p['block']} status {p['status']}") for p in pays)


def mark(ctx, who):
    if ctx.chain.pending_count():
        raise BenchError('the mempool was not empty at the start of a scenario')
    return {'head': ctx.chain.head(), 'balance': token_balance(ctx, who), 'sends': ctx.proxy.count('eth_sendTransaction')}


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
    c.expect('displayed_balance_matches_chain', shown == chain, f'{shown:g} TEST on screen', f'{chain:g} TEST on chain')
    c.evidence['screenshot'] = shot(ctx, page, 'connected')


def c02(ctx, c):
    page = ready_page(ctx, rejectSend=True)
    user = ctx.acc['user']
    nonce = ctx.chain.nonce(user)
    page.click('[data-testid=pay]')
    wait_state(page, 'rejected', 5000)
    page.wait_for_timeout(500)
    sts = states(page)
    c.expect('ui_says_request_declined', sts[-1]['state'] == 'rejected', [s['state'] for s in sts], 'rejected')
    sent = ctx.proxy.count('eth_sendTransaction')
    c.expect('no_transaction_reached_node', sent == 0 and ctx.chain.pending_count() == 0,
             f'{sent} sent, {ctx.chain.pending_count()} pending', '0 sent, 0 pending')
    c.expect('nonce_unchanged', ctx.chain.nonce(user) == nonce, ctx.chain.nonce(user), nonce)
    c.expect('never_confirmed', not any(claims_confirmed(s) for s in sts), [s['state'] for s in sts], 'no confirmed')
    c.evidence['wallet_answer'] = 'code 4001 (user rejected)'
    c.evidence['screenshot'] = shot(ctx, page, 'declined')


def c03(ctx, c):
    page = ready_page(ctx)
    c.expect('no_banner_on_right_chain', not page.is_visible('[data-testid=wrong-network]'), page.is_visible('[data-testid=wrong-network]'), False)
    page.evaluate("window.__testWallet.setChain('0x1')")
    page.wait_for_timeout(300)
    c.expect('banner_on_wrong_chain', page.is_visible('[data-testid=wrong-network]'), page.is_visible('[data-testid=wrong-network]'), True)
    c.expect('payment_disabled_on_wrong_chain', page.is_disabled('[data-testid=pay]'), page.is_disabled('[data-testid=pay]'), True)
    c.evidence['screenshot'] = shot(ctx, page, 'wrong-network')
    force_click(page, '[data-testid=pay]')
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
    force_click(page, '[data-testid=pay]')
    page.wait_for_timeout(800)
    sent = ctx.proxy.count('eth_sendTransaction')
    c.expect('nothing_sent_before_terms', sent == 0 and ctx.chain.pending_count() == 0,
             f'{sent} sent, {ctx.chain.pending_count()} pending', '0 sent, 0 pending')
    sign_terms(page)
    page.click('[data-testid=pay]')
    wait_pool(ctx, 1)
    ctx.chain.mine()
    wait_state(page, 'confirmed', 8000)
    calls = [x['method'] for x in page.evaluate('window.__walletCalls')]
    i_sign = calls.index('personal_sign') if 'personal_sign' in calls else None
    i_send = calls.index('eth_sendTransaction') if 'eth_sendTransaction' in calls else None
    c.expect('terms_signed_before_transaction', i_sign is not None and i_send is not None and i_sign < i_send,
             f'personal_sign at call {show(i_sign)}, eth_sendTransaction at call {show(i_send)}', 'personal_sign first')
    user = ctx.acc['user']
    code, rec = http_get(f"{ctx.app_url}/api/acceptance/{user}")
    c.expect('api_holds_verified_acceptance', code == 200 and rec.get('verified') is True,
             f'HTTP {code}, verified {show(rec.get("verified"))}', 'HTTP 200, verified true')
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
    data = pay_data(ctx, ctx.chain.keccak('c05-intent'))
    err = ctx.chain.simulate_error(out, ctx.addr['grants'], data)
    h = ctx.chain.send(out, ctx.addr['grants'], data, gas=300000)
    ctx.chain.mine()
    r = ctx.chain.receipt(h)
    c.expect('direct_call_reverted', r['status'] == '0x0', f"status {r['status']}", 'status 0x0')
    c.expect('revert_reason_is_NotAllowlisted', err == ctx.err['NotAllowlisted'], error_name(ctx, err), 'NotAllowlisted()')
    c.expect('no_grant_created', grant_count(ctx) == 0 and token_balance(ctx, ctx.acc['reader']) == reader_before,
             f'{grant_count(ctx)} grants', '0 grants, reader balance unchanged')
    c.evidence.update({'tx': h, 'block': int(r['blockNumber'], 16), 'caller': 'outsider (not allowlisted, holds TEST, has approved)', 'path': 'eth_sendTransaction straight to the node, no interface'})


def c06(ctx, c):
    low = ctx.acc['user_low']
    page = ready_page(ctx, 'user_low', fixedGas=hex(300000))
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
    if r is None:
        raise BenchError('no receipt for the transaction the screen shows')
    sts = [s['state'] for s in states(page)]
    c.expect('receipt_status_0', r['status'] == '0x0', r['status'], '0x0')
    tok_after = (token_balance(ctx, low), token_balance(ctx, ctx.acc['reader']))
    c.expect('token_balances_unchanged', tok_after == tok_before,
             f'user_low {tok_after[0] / UNIT:g}, reader {tok_after[1] / UNIT:g}', f'user_low {tok_before[0] / UNIT:g}, reader {tok_before[1] / UNIT:g}')
    fee = int(r['gasUsed'], 16) * int(r['effectiveGasPrice'], 16)
    spent = native_before - ctx.chain.native(low)
    c.expect('native_balance_down_by_fee_only', spent == fee, f'{spent} wei', f'{fee} wei (gasUsed x effectiveGasPrice)')
    c.expect('ui_says_failed', sts[-1] == 'reverted', sts, 'reverted')
    c.expect('never_confirmed', 'confirmed' not in sts, sts, 'no confirmed')
    c.evidence.update({'tx': h, 'block': int(r['blockNumber'], 16), 'gas_limit': 300000, 'gas_used': int(r['gasUsed'], 16), 'states_before_block': before_block})
    c.evidence['screenshot'] = shot(ctx, page, 'failed')


def c07(ctx, c):
    user, reader = ctx.acc['user'], ctx.acc['reader']
    page = ready_page(ctx)
    before = (token_balance(ctx, user), token_balance(ctx, reader))
    page.click('[data-testid=pay]')
    wait_pool(ctx, 1)
    page.wait_for_timeout(1000)  # time for the screen to claim anything it wants before the block
    t_mine = ctx.chain.mine()
    wait_state(page, 'confirmed', 8000)
    sts = states(page)
    early = [s for s in sts if claims_confirmed(s) and s['t'] < t_mine]
    c.expect('no_confirmed_before_block', not early, history(sts, t_mine), 'confirmed only after the block (t >= 0)')
    h = status_hash(page)
    r = ctx.chain.receipt(h)
    c.expect('receipt_status_1', r is not None and r['status'] == '0x1', show(r and r['status']), '0x1')
    after = (token_balance(ctx, user), token_balance(ctx, reader))
    c.expect('tokens_moved_once', before[0] - after[0] == FEE and after[1] - before[1] == FEE,
             f'user -{(before[0] - after[0]) / UNIT:g}, reader +{(after[1] - before[1]) / UNIT:g}', 'user -25, reader +25')
    g = grant_on_chain(ctx, 1)
    c.expect('grant_active_on_chain', g['active'] and g['owner'] == user.lower(),
             f"grant #1 {'active' if g['active'] else 'inactive'}, owner {g['owner']}", 'grant #1 active, owned by user')
    blk = int(r['blockNumber'], 16)
    st = wait_indexed(ctx, blk)
    api = [x for x in st['grants'] if x['id'] == '1']
    same = bool(api) and api[0]['active'] and api[0]['owner'] == g['owner'] and api[0]['reader'] == g['reader'] and int(api[0]['paid']) == g['paid']
    c.expect('api_matches_chain', same,
             f"API grant #1 {'active' if api[0]['active'] else 'inactive'}, owner {api[0]['owner']}, paid {int(api[0]['paid']) / UNIT:g} TEST" if api else 'no grant in the API',
             'API grant #1 equals the chain')
    c.evidence.update({'tx': h, 'block': blk, 'ui_history': history(sts, t_mine)})
    c.evidence['screenshot'] = shot(ctx, page, 'confirmed')


def c08(ctx, c):
    """RPC outage on receipts, held until the screen reaches a terminal state plus a margin.
    Then the bench restores the RPC and mines: both versions end on Confirmed, so only the
    full history (state and visible text, timed against the block) can tell them apart."""
    user = ctx.acc['user']
    ctx.proxy.cut('eth_getTransactionReceipt')
    page = ready_page(ctx)
    head0 = ctx.chain.head()
    bal0 = token_balance(ctx, user)
    t_click = now_ms()
    page.click('[data-testid=pay]')
    wait_pool(ctx, 1)
    wait_state(page, TERMINAL, 10000)
    page.wait_for_timeout(OUTAGE_MARGIN_MS)
    # The outage is still in place: write down what each layer says now.
    sts = [s for s in states(page) if s['t'] >= t_click]
    last = sts[-1]
    h = last['hash'] or status_hash(page)
    receipt = ctx.chain.receipt(h) if h else None
    if receipt is not None:
        raise BenchError('a receipt exists before the bench mined: the outage scenario did not hold')
    head = ctx.chain.head()
    pending = ctx.chain.pending_count()
    _, api = http_get(f'{ctx.app_url}/api/grants?owner={user}')
    _, apistate = http_get(f'{ctx.app_url}/api/state')
    title = page.inner_text('#status-title')
    screenshot = shot(ctx, page, 'outage')
    columns = {
        'interface': {'status': title, 'state': last['state'], 'tx': h},
        'api': {'grants_for_user': len(api['grants']), 'indexed_block': apistate['indexedBlock']},
        'chain': {'receipt': None, 'pending_in_mempool': pending, 'latest_block': head,
                  'blocks_mined_since_click': head - head0, 'grant_count': grant_count(ctx),
                  'user_test_balance_unchanged': token_balance(ctx, user) == bal0},
    }
    # End of the outage: answers come back, the block is mined, the user asks again if the screen offers it.
    ctx.proxy.restore()
    t_mine = ctx.chain.mine()
    if page.is_visible('[data-testid=retry]'):
        page.click('[data-testid=retry]')
    wait_current(page, 'confirmed', 10000)
    page.wait_for_timeout(300)
    final = [s for s in states(page) if s['t'] >= t_click]
    receipt_after = ctx.chain.receipt(h)
    early = [s for s in final if claims_confirmed(s) and s['t'] < t_mine]
    before_block = [s for s in final if s['t'] < t_mine]
    c.expect('never_confirmed_without_receipt', not early,
             history(before_block, t_click) + [f'receipt on chain before the block: none', f'blocks mined before the bench mined: {head - head0}'],
             'the word confirmed never shown before the block that holds the receipt')
    c.expect('outage_shown_as_pending_or_unknown', last['state'] in ('submitted', 'unknown') and not claims_confirmed(last),
             f"{last['state']}: \"{last['text']}\"", 'submitted or unknown, without the word confirmed')
    end = final[-1]
    c.expect('ends_confirmed_after_block', end['state'] == 'confirmed' and end['text'] == 'Confirmed' and receipt_after is not None and receipt_after['status'] == '0x1',
             f"{end['state']}: \"{end['text']}\", receipt status {show(receipt_after and receipt_after['status'])}", 'confirmed: "Confirmed", receipt status 0x1')
    columns['interface']['history'] = history_rows(final, t_click)
    columns['interface']['block_mined_at_s'] = round((t_mine - t_click) / 1000, 2)
    c.evidence.update({
        'screenshot': screenshot,
        'columns': columns,
        'outage': 'the proxy on the app path answered HTTP 503 to every eth_getTransactionReceipt; the node still answered the bench',
        'outage_held_s': round((t_mine - t_click) / 1000, 2),
        'cut_requests': sum(1 for e in ctx.proxy.events if e['cut']),
    })


def c09(ctx, c):
    """One payment intent, one payment: retry after status unknown, then three other ways to pay again
    (the main button during unknown, a reload, a double click). Each must leave one payment on chain."""
    user = ctx.acc['user']
    cut = ('eth_getTransactionReceipt', 'eth_getTransactionByHash')

    # 1. Status unknown, then the retry button.
    ctx.proxy.cut(*cut)
    page = ready_page(ctx)
    s0 = mark(ctx, user)
    t0 = now_ms()
    page.click('[data-testid=pay]')
    wait_pool(ctx, 1)
    wait_state(page, 'unknown', 8000)
    first = states(page)[-1]
    c.expect('outage_shown_as_unknown', first['state'] == 'unknown' and not claims_confirmed(first),
             f"{first['state']}: \"{first['text']}\"", 'unknown, without the word confirmed')
    ctx.proxy.restore()
    retry = page.locator('[data-testid=retry]')
    label = retry.inner_text() if retry.is_visible() else None
    c.expect('retry_offered', label is not None, f'"{label}"' if label else 'no retry button on screen', 'a retry button on screen')
    t_retry = now_ms()
    if label is not None:
        retry.click()
    page.wait_for_timeout(1500)
    asked = ctx.proxy.lookups_since(t_retry, first['hash']) if first['hash'] else []
    c.expect('retry_checks_same_transaction', len(asked) > 0,
             f"{len(asked)} receipt or transaction lookup{'s' if len(asked) != 1 else ''} for the first transaction in the 1.5 s after the retry",
             'at least 1 lookup for the first transaction')
    after_retry = payments_since(ctx, s0['head'], user)
    sends = ctx.proxy.count('eth_sendTransaction') - s0['sends']
    c.expect('retry_sends_no_new_payment', len(after_retry) == 1 and sends == 1,
             f'{sends} eth_sendTransaction from the app; ' + describe(after_retry), '1 eth_sendTransaction; 1 payment transaction')
    ctx.chain.mine()
    if label is not None:
        wait_current(page, 'confirmed', 10000)
        page.wait_for_timeout(300)
    end = states(page)[-1]
    c.expect('screen_ends_confirmed', end['state'] == 'confirmed' and end['text'] == 'Confirmed' and end['hash'] == first['hash'],
             f"{end['state']}: \"{end['text']}\" for {'the first transaction' if end['hash'] == first['hash'] else 'another transaction'}",
             'confirmed: "Confirmed" for the first transaction')
    pays = payments_since(ctx, s0['head'], user)
    c.expect('one_payment_on_chain', len(pays) == 1 and pays[0]['status'] == '0x1', describe(pays), '1 payment transaction, status 0x1')
    debit = s0['balance'] - token_balance(ctx, user)
    c.expect('test_debited_once', debit == FEE, f'-{debit / UNIT:g} TEST', '-25 TEST')
    c.evidence.update({'retry_button': label, 'payments': pays, 'ui_history': history(states(page), t0),
                       'screenshot': shot(ctx, page, 'after-retry')})
    intent = pays[0]['intent'] if pays else None
    page.context.close()

    # 2. Status unknown, then the main Pay button instead of the retry.
    ctx.proxy.cut(*cut)
    page = ready_page(ctx)
    s1 = mark(ctx, user)
    page.click('[data-testid=pay]')
    wait_pool(ctx, 1)
    wait_state(page, 'unknown', 8000)
    disabled = page.is_disabled('[data-testid=pay]')
    c.expect('pay_locked_while_unknown', disabled, f'Pay button {"disabled" if disabled else "active"}', 'Pay button disabled')
    force_click(page, '[data-testid=pay]')
    page.wait_for_timeout(1500)
    ctx.proxy.restore()
    ctx.chain.mine()
    page.wait_for_timeout(800)
    pays = payments_since(ctx, s1['head'], user)
    c.expect('pay_during_unknown_pays_once', len(pays) == 1 and s1['balance'] - token_balance(ctx, user) == FEE,
             describe(pays) + f", -{(s1['balance'] - token_balance(ctx, user)) / UNIT:g} TEST", '1 payment transaction, -25 TEST')
    c.evidence['pay_during_unknown'] = pays
    page.context.close()

    # 3. Status unknown, the user reloads the page, then pays.
    ctx.proxy.cut(*cut)
    page = ready_page(ctx)
    s2 = mark(ctx, user)
    page.click('[data-testid=pay]')
    wait_pool(ctx, 1)
    wait_state(page, 'unknown', 8000)
    pending_hash = states(page)[-1]['hash']
    page.reload()
    page.wait_for_selector('body[data-ready="1"]', timeout=10000)
    connect(page)
    page.wait_for_timeout(1500)
    resumed = [s for s in states(page) if s['hash'] and s['hash'] == pending_hash]
    c.expect('reload_resumes_same_transaction', bool(resumed),
             f"after reload the status line follows {'the pending transaction' if resumed else 'no transaction'}: " + ', '.join(s['state'] for s in states(page)) if states(page) else 'after reload the status line follows no transaction',
             'the pending transaction, followed again')
    sign_terms(page)
    force_click(page, '[data-testid=pay]')
    page.wait_for_timeout(1500)
    ctx.proxy.restore()
    ctx.chain.mine()
    page.wait_for_timeout(800)
    pays = payments_since(ctx, s2['head'], user)
    c.expect('reload_then_pay_pays_once', len(pays) == 1 and s2['balance'] - token_balance(ctx, user) == FEE,
             describe(pays) + f", -{(s2['balance'] - token_balance(ctx, user)) / UNIT:g} TEST", '1 payment transaction, -25 TEST')
    c.evidence['reload_then_pay'] = pays
    page.context.close()

    # 4. No outage at all: a double click on Pay.
    page = ready_page(ctx)
    s3 = mark(ctx, user)
    page.dblclick('[data-testid=pay]')
    wait_pool(ctx, 1)
    page.wait_for_timeout(1000)
    ctx.chain.mine()
    wait_state(page, 'confirmed', 8000)
    page.wait_for_timeout(800)
    pays = payments_since(ctx, s3['head'], user)
    c.expect('double_click_pays_once', len(pays) == 1 and s3['balance'] - token_balance(ctx, user) == FEE,
             describe(pays) + f", -{(s3['balance'] - token_balance(ctx, user)) / UNIT:g} TEST", '1 payment transaction, -25 TEST')
    c.evidence['double_click'] = pays
    page.context.close()

    # 5. The contract itself: the intent of the first payment, sent again straight to the node.
    if intent is None:
        raise BenchError('no payment from scenario 1 to replay')
    data = pay_data(ctx, intent)
    err = ctx.chain.simulate_error(user, ctx.addr['grants'], data)
    h = ctx.chain.send(user, ctx.addr['grants'], data, gas=300000)
    ctx.chain.mine()
    r = ctx.chain.receipt(h)
    c.expect('contract_refuses_same_intent', r['status'] == '0x0' and err == ctx.err['IntentAlreadyPaid'],
             f"status {r['status']}, error {error_name(ctx, err)}", 'status 0x0, IntentAlreadyPaid()')
    c.evidence['same_intent_replay'] = {'tx': h, 'block': int(r['blockNumber'], 16), 'intent': intent,
                                        'path': 'eth_sendTransaction straight to the node, no interface'}


def c10(ctx, c):
    user, low, out, admin = ctx.acc['user'], ctx.acc['user_low'], ctx.acc['outsider'], ctx.acc['admin']
    page = ctx.page('user')
    open_app(ctx, page)
    connect(page)
    visible = page.is_visible('[data-testid=admin-panel]')
    c.expect('admin_control_hidden_for_user', not visible, f'admin panel {"shown" if visible else "hidden"}', 'admin panel hidden')

    def attempt(frm, target, allowed):
        data = ctx.sel['setAllowlisted'] + pad(target) + pad(1 if allowed else 0)
        err = ctx.chain.simulate_error(frm, ctx.addr['grants'], data)
        h = ctx.chain.send(frm, ctx.addr['grants'], data, gas=100000)
        ctx.chain.mine()
        return h, ctx.chain.receipt(h), err

    # An ordinary account that is itself allowlisted tries both directions, straight on the contract.
    h_add, r_add, e_add = attempt(user, out, True)
    c.expect('ordinary_account_cannot_add', r_add['status'] == '0x0' and e_add == ctx.err['NotAdmin'],
             f"status {r_add['status']}, error {error_name(ctx, e_add)}", 'status 0x0, NotAdmin()')
    h_rm, r_rm, e_rm = attempt(user, low, False)
    c.expect('ordinary_account_cannot_remove', r_rm['status'] == '0x0' and e_rm == ctx.err['NotAdmin'],
             f"status {r_rm['status']}, error {error_name(ctx, e_rm)}", 'status 0x0, NotAdmin()')
    a_out, a_low = allowlisted(ctx, out), allowlisted(ctx, low)
    c.expect('allowlist_unchanged', not a_out and a_low,
             f'outsider allowlisted {show(a_out)}, user_low allowlisted {show(a_low)}', 'outsider allowlisted false, user_low allowlisted true')
    # The admin path still works: the fix must not lock the admin out.
    apage = ctx.page('admin')
    open_app(ctx, apage)
    connect(apage)
    shown = apage.is_visible('[data-testid=admin-panel]')
    _, r_a1, _ = attempt(admin, out, True)
    added = allowlisted(ctx, out)
    _, r_a2, _ = attempt(admin, out, False)
    removed = not allowlisted(ctx, out)
    c.expect('admin_can_edit_allowlist', shown and r_a1['status'] == '0x1' and added and r_a2['status'] == '0x1' and removed,
             f"admin panel {'shown' if shown else 'hidden'}; add status {r_a1['status']}, remove status {r_a2['status']}",
             'admin panel shown; add status 0x1, remove status 0x1')
    c.evidence.update({'tx': h_add, 'block': int(r_add['blockNumber'], 16), 'remove_tx': h_rm,
                       'caller': 'user: allowlisted, not the admin', 'calls': ['setAllowlisted(outsider, true)', 'setAllowlisted(user_low, false)'],
                       'path': 'eth_sendTransaction straight to the node, no interface'})


def c11(ctx, c):
    reader = ctx.acc['reader']
    page = ready_page(ctx)
    page.click('[data-testid=pay]')
    wait_pool(ctx, 1)
    ctx.chain.mine()
    wait_state(page, 'confirmed', 8000)
    wait_indexed(ctx, ctx.chain.head())
    # The reader's identity is simulated: it is declared in a request header, there is no reader sign-in.
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
    c.expect('grant_inactive_on_chain', not g['active'], f"grant #1 {'active' if g['active'] else 'inactive'}", 'grant #1 inactive')
    st = wait_indexed(ctx, blk)
    code2, _ = http_get(ctx.app_url + '/api/data/1', {'x-reader-address': reader})
    c.expect('api_refuses_after_revoke', code2 == 403, f'HTTP {code2} at indexed block {st["indexedBlock"]}', f'HTTP 403 once block {blk} is indexed')
    c.evidence.update({'revoke_block': blk, 'api_before': code1, 'api_after': code2,
                       'reader_identity': 'simulated: declared in the x-reader-address request header, no reader sign-in'})


CHECKS = {'C01': c01, 'C02': c02, 'C03': c03, 'C04': c04, 'C05': c05, 'C06': c06,
          'C07': c07, 'C08': c08, 'C09': c09, 'C10': c10, 'C11': c11}
