"""The eleven required checks. Each one records named assertions and evidence.
screen_invariant() is added to every check that opens the app. Every check that opens the app runs
twice: on a still chain (the bench mines only when it decides) and on a moving chain (a block every
second while the user acts; see harness.BlockProducer).

A check never decides the release on its own: it returns its assertions, and status.py
turns the set of results into Blocked / Incomplete / Demonstrated checks passed.
The bench reads the chain directly (ctx.chain); the app only sees the node through the proxy.

Waiting is never a verdict: when something the bench waits for does not happen in time
(a screen state, a button, a banner, a transaction in the mempool, the indexer catching up), it raises
BenchTimeout, the check ends in error and the run is Incomplete. Only a named assertion
that fails is a detection. Every appearance is awaited with a deadline; the only fixed windows are
observation windows (observe), and what follows them only checks that something did NOT happen.
"""
import os, re, time
from harness import http_get, pad, post_json

FEE = 25 * 10**18
UNIT = 10**18
TERMINAL = ['unknown', 'confirmed', 'reverted', 'error', 'rejected']
OUTAGE_MARGIN_MS = 5000  # how long the outage lasts after the screen reaches a terminal state
PENDING_MS = 15000  # how long a payment waits in the mempool before the bench lets it into a block (C07, C08)
APPEAR_MS = 8000  # deadline for anything the bench expects to appear on screen
# The word confirmed, unless the sentence says the opposite or the future: "not confirmed",
# "isn't confirmed", "will be confirmed", "not yet confirmed", "until confirmed". wallet.js uses the same rule.
CONFIRMED_WORD = re.compile(r"(?<![a-z])(?<!not )(?<!n't )(?<!be )(?<!yet )(?<!until )confirmed\b", re.I)


class BenchTimeout(Exception):
    """What the bench waited for did not happen in time. Never a detection."""


class BenchError(Exception):
    """The bench could not set up the situation it needs. Never a detection."""


class Check:
    def __init__(self, cid):
        self.id = cid
        self.assertions = []
        self.evidence = {}
        self.waits_expired = []  # deadlines that passed without stopping the check: the run cannot pass

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


def appear(page, selector, what, timeout=APPEAR_MS, state='visible'):
    """Wait until `selector` reaches `state` (visible, hidden). Past the deadline: BenchTimeout."""
    try:
        page.wait_for_selector(selector, state=state, timeout=timeout)
    except Exception as e:
        raise BenchTimeout(f'{what} within {timeout / 1000:g} s') from e


def wait_disabled(page, selector, what, timeout=APPEAR_MS):
    try:
        page.wait_for_function('(s) => { const b = document.querySelector(s); return b && b.disabled; }', arg=selector, timeout=timeout)
    except Exception as e:
        raise BenchTimeout(f'{what} within {timeout / 1000:g} s') from e


def observe(page, ms):
    """Observation window: lets the page run for `ms` so that a wrong claim or a wrong send has time to
    show. It can only give a fault time to appear; what follows it checks that something did NOT happen."""
    end = time.time() + ms / 1000
    while time.time() < end:
        page.wait_for_timeout(min(200, max(1, int((end - time.time()) * 1000))))


def connect(page):
    page.click('[data-testid=connect]')
    appear(page, '[data-testid=account]:not(:empty)', 'the connected account did not show')
    appear(page, '[data-testid=network]:not(:empty)', 'the network name did not show')


def sign_terms(page):
    page.click('[data-testid=accept]')
    appear(page, '[data-testid=accepted]', 'the signed terms did not show')


def ready_page(ctx, who='user', **wallet):
    page = ctx.page(who, **wallet)
    open_app(ctx, page)
    connect(page)
    sign_terms(page)
    return page


def force_click(page, selector):
    """Click even if the button is disabled: a stale screen or a script must not get through either."""
    gesture = getattr(page, '_bench_gesture', None)
    if gesture:
        gesture()
    page.evaluate("(s) => { const b = document.querySelector(s); b.disabled = false; b.click(); }", selector)


def states(page):
    return page.evaluate('window.__uiStates')


def last_state(page):
    return (states(page) or [{'state': 'idle'}])[-1]['state']


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


END = ('confirmed', 'reverted', 'already-paid', 'error', 'rejected')


def wait_end(page, timeout=10000):
    """Wait until the status line ends a payment: confirmed, failed, already paid, error or declined.
    Which one it is, is for an assertion to judge, not for the wait. Past the deadline: BenchTimeout."""
    try:
        page.wait_for_function('(w) => { const s = window.__uiStates; return s.length && w.includes(s[s.length - 1].state); }', arg=list(END), timeout=timeout)
    except Exception as e:
        raise BenchTimeout(f'the status line did not end the payment within {timeout / 1000:g} s') from e
    return last_state(page)


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


def wait_until(page, what, fn, timeout=10.0):
    """Poll fn until it returns something truthy, letting the page run. Past the deadline: BenchTimeout."""
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        page.wait_for_timeout(50)
    raise BenchTimeout(f'{what} within {timeout:g} s')


AT_REST = ('idle', 'unknown', 'confirmed', 'reverted', 'already-paid', 'rejected', 'error')
QUIET_MS = 500
HOLD_MS = 1000


def quiet(pages, cond, ms, what, timeout=10.0):
    """Wait until cond() holds and no status line on `pages` changed for `ms` in a row.
    This is an observation window, not a verdict: it only gives a wrong claim time to show up.
    Past the deadline: BenchTimeout."""
    end = time.time() + timeout
    seen, since = None, None
    while time.time() < end:
        now = sum(len(states(p)) for p in pages)
        if cond() and now == seen:
            if (time.time() - since) * 1000 >= ms:
                return
        else:
            seen, since = now, time.time()
        pages[0].wait_for_timeout(50)
    raise BenchTimeout(f'{what} within {timeout:g} s')


def settle(ctx, pages, s, who, timeout=30.0):
    """End of a pay-again scenario, in two phases.
    1. Nothing is mined yet: the app may send whatever it wants. Done when every payment the app
       sent is on the node, no screen waits for its wallet, and nothing changed for QUIET_MS.
    2. The RPC answers again and the bench mines until every payment the app sent is in a block,
       the mempool is empty and every screen is at rest, for QUIET_MS in a row.
    A payment the app would start later than that is outside the observation window."""
    def sent():  # payments the app sent and the node accepted
        return ctx.proxy.count('eth_sendTransaction') - s['sends'] - sum(1 for x in list(ctx.proxy.refused) if x['t'] >= s['t'])

    def known():
        return len(payments_since(ctx, s['head'], who))

    last = last_state

    try:
        quiet(pages, lambda: known() >= sent() and all(last(p) != 'awaiting-signature' for p in pages), QUIET_MS,
              'the app kept sending or waiting for its wallet', timeout)
    except BenchTimeout as e:
        raise BenchTimeout(f'{e} ({sent()} sent, {known()} on the node, refused by the node: {ctx.proxy.refused[-3:]}, '
                           f'screens: {[last(p) for p in pages]})') from e
    ctx.proxy.restore()

    def mined_and_at_rest():
        if ctx.chain.pending_count():
            ctx.chain.mine()
            return False
        landed = [p for p in payments_since(ctx, s['head'], who) if p['block'] is not None]
        return len(landed) >= sent() and all(last(p) in AT_REST for p in pages)
    quiet(pages, mined_and_at_rest, QUIET_MS, 'the payments and the screen did not settle', timeout)


def screen_invariant(ctx, c):
    """Applied to every check that opens the app (I1). The status line: each moment it claimed
    confirmed must come after the bench mined the block that holds the receipt of the transaction it
    names. The rest of the page (visible text, tab title, alert and confirm dialogs): each moment it
    claimed confirmed must come after the block holding the receipt of a transaction that tab sent."""
    claims = [x for x in ctx.ui_log if x.get('kind', 'status') == 'status' and claims_confirmed(x)]
    page_claims = [x for x in ctx.ui_log if x.get('kind') == 'page' and CONFIRMED_WORD.search(x.get('text') or '')]
    bad = []

    def block_time(h):
        r = ctx.chain.receipt(h) if h and h.startswith('0x') else None
        return r, (ctx.chain.mined_at.get(r['blockHash']) if r else None)

    for x in claims:
        h = x.get('hash') or ''
        r, t_block = block_time(h)
        if r is None:
            bad.append(f'"{x["text"]}" for {h[:10] + "…" if h else "no transaction"}: no receipt on chain')
        elif t_block is None or x['t'] < t_block:
            ahead = f'{(t_block - x["t"]) / 1000:.2f} s before' if t_block else 'before'
            bad.append(f'"{x["text"]}" {ahead} block {int(r["blockNumber"], 16)} was mined (tab {x["tab"]})')
    for x in page_claims:
        sent = [y['hash'] for y in ctx.ui_log if y.get('kind') == 'sent' and y['tab'] == x['tab'] and y['t'] <= x['t']]
        landed = [t for t in (block_time(h)[1] for h in sent) if t is not None and t <= x['t']]
        if not landed:
            bad.append(f'page outside the status line: "{x["text"][:80]}" with no transaction of tab {x["tab"]} in a block yet')
    n = len(claims) + len(page_claims)
    c.expect('no_confirmed_before_receipt_block', not bad,
             bad or f'{n} confirmed moment{"s" if n != 1 else ""} on {len({x["tab"] for x in ctx.ui_log}) or 0} tab(s) '
                    f'({len(claims)} on the status line, {len(page_claims)} elsewhere on the page), each after its block',
             'every confirmed moment, on the status line or anywhere on the page, after the block holding its receipt')


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
                p = {'tx': tx['hash'], 'nonce': int(tx['nonce'], 16), 'block': n, 'status': rc['status'], 'intent': '0x' + tx['input'][-64:]}
                if rc['status'] == '0x0':
                    p['reason'] = revert_reason(ctx, tx, n)
                out.append(p)
    for tx in ctx.chain.pending_txs():
        if is_payment(ctx, tx, who):
            out.append({'tx': tx['hash'], 'nonce': int(tx['nonce'], 16), 'block': None, 'status': 'pending', 'intent': '0x' + tx['input'][-64:]})
    return out


def revert_reason(ctx, tx, block):
    """Name the error a mined transaction reverted with, from the node's trace of that transaction."""
    try:
        out = ctx.chain.rpc('debug_traceTransaction', [tx['hash'], {'tracer': 'callTracer'}]).get('output') or ''
        return error_name(ctx, out[:10]) if len(out) >= 10 else 'reverted'
    except Exception:
        return 'reverted'


def describe(ctx, pays):
    """Every payment transaction, with what it did: a debit, or a revert (no debit) and its reason."""
    if not pays:
        return 'no payment transaction'
    def one(p):
        if p['block'] is None:
            return f"nonce {p['nonce']} waiting in the mempool"
        if p['status'] == '0x1':
            return f"nonce {p['nonce']} block {p['block']} status 0x1 (debit)"
        return f"nonce {p['nonce']} block {p['block']} status 0x0 ({p.get('reason') or 'reverted'}, no debit)"
    return f"{len(pays)} payment transaction{'s' if len(pays) > 1 else ''}: " + ', '.join(one(p) for p in pays)


def mark(ctx, who):
    if ctx.chain.pending_count():
        raise BenchError('the mempool was not empty at the start of a scenario')
    return {'head': ctx.chain.head(), 'balance': token_balance(ctx, who), 'sends': ctx.proxy.count('eth_sendTransaction'), 't': now_ms()}


def next_order(ctx, who):
    """Revoke every active grant of `who`, straight on the contract, so the next scenario is a new order."""
    sent = False
    for gid in range(1, grant_count(ctx) + 1):
        g = grant_on_chain(ctx, gid)
        if g['active'] and g['owner'] == who.lower():
            ctx.chain.send(who, ctx.addr['grants'], ctx.sel['revoke'] + pad(gid), gas=200000)
            sent = True
    if sent:
        ctx.chain.mine()


def shot(ctx, page, name, selector='#app'):
    path = os.path.join(ctx.evidence_dir, f'{ctx.check_id}-{name}.png')
    page.locator(selector).screenshot(path=path)
    return os.path.relpath(path, ctx.reports_dir)


# ---------- checks ----------

PAY, RETRY_BTN, BANNER = '[data-testid=pay]', '[data-testid=retry]', '[data-testid=wrong-network]'


def c01(ctx, c):
    page = ctx.page('user')
    open_app(ctx, page)
    connect(page)
    user = ctx.acc['user']
    c.expect('connected_account_matches', page.inner_text('[data-testid=account]') == user[:6] + '…' + user[-4:],
             page.inner_text('[data-testid=account]'), user[:6] + '…' + user[-4:])
    c.expect('chain_is_local_dev_chain', page.inner_text('[data-testid=network]') == 'Local dev chain',
             page.inner_text('[data-testid=network]'), 'Local dev chain')
    try:
        page.wait_for_function("document.querySelector('[data-testid=balance]').textContent !== '-'", timeout=APPEAR_MS)
    except Exception as e:
        raise BenchTimeout(f'no balance on screen within {APPEAR_MS / 1000:g} s') from e
    shown = float(page.inner_text('[data-testid=balance]').replace(',', ''))
    chain = token_balance(ctx, user) / UNIT
    c.expect('displayed_balance_matches_chain', shown == chain, f'{shown:g} TEST on screen', f'{chain:g} TEST on chain')
    c.evidence['screenshot'] = shot(ctx, page, 'connected')


def c02(ctx, c):
    page = ready_page(ctx, rejectSend=True)
    user = ctx.acc['user']
    nonce = ctx.chain.nonce(user)
    page.click(PAY)
    wait_current(page, 'rejected', 5000)
    observe(page, 500)
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
    appear(page, BANNER, 'the wrong-network banner was still shown on the right chain', state='hidden')
    c.expect('no_banner_on_right_chain', True, 'banner hidden', 'banner hidden')
    page.evaluate("window.__testWallet.setChain('0x1')")
    appear(page, BANNER, 'no wrong-network banner after the wallet moved to chain 1')
    c.expect('banner_on_wrong_chain', True, 'banner shown', 'banner shown')
    wait_disabled(page, PAY, 'the Pay button stayed active on the wrong chain')
    c.expect('payment_disabled_on_wrong_chain', True, 'Pay button disabled', 'Pay button disabled')
    c.evidence['screenshot'] = shot(ctx, page, 'wrong-network')
    force_click(page, PAY)
    observe(page, 800)
    sent = ctx.proxy.count('eth_sendTransaction')
    c.expect('nothing_sent_on_wrong_chain', sent == 0 and ctx.chain.pending_count() == 0,
             f'{sent} sent, {ctx.chain.pending_count()} pending', '0 sent, 0 pending')
    page.click('[data-testid=switch-network]')
    appear(page, BANNER, 'the wrong-network banner stayed after switching back', state='hidden')
    c.expect('switch_back_clears_banner', True, 'banner hidden', 'banner hidden')
    c.evidence['wallet_calls'] = [x['method'] for x in page.evaluate('window.__walletCalls')]


def c04(ctx, c):
    page = ctx.page('user')
    open_app(ctx, page)
    connect(page)
    # A lock, not an appearance: it must hold from the moment the account is connected.
    locked = page.is_disabled(PAY)
    c.expect('payment_locked_until_terms_signed', locked, f'Pay button {"disabled" if locked else "active"}', 'Pay button disabled')
    force_click(page, PAY)
    observe(page, 800)
    sent = ctx.proxy.count('eth_sendTransaction')
    c.expect('nothing_sent_before_terms', sent == 0 and ctx.chain.pending_count() == 0,
             f'{sent} sent, {ctx.chain.pending_count()} pending', '0 sent, 0 pending')
    sign_terms(page)
    page.click(PAY)
    wait_pool(ctx, 1)
    ctx.chain.mine()
    ended = wait_end(page)
    c.expect('payment_confirmed', ended == 'confirmed', ended, 'confirmed')
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
    data = pay_data(ctx, ctx.chain.keccak('c05-order'))
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
    page.click(PAY)
    wait_pool(ctx, 1)
    observe(page, 600)
    before_block = [s['state'] for s in states(page)]
    ctx.chain.mine()
    wait_state(page, ['reverted', 'confirmed'], 8000)
    quiet([page], lambda: True, 300, 'the status line kept changing after the block')
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


def early_page_claims(ctx, t):
    return [x for x in ctx.ui_log if x.get('kind') == 'page' and CONFIRMED_WORD.search(x.get('text') or '') and x['t'] < t]


def c07(ctx, c):
    """A healthy RPC, a payment that waits 15 s in the mempool before its block (with a block every second
    around it on the moving chain): nothing on the page may say confirmed before that block."""
    user, reader = ctx.acc['user'], ctx.acc['reader']
    page = ready_page(ctx)
    before = (token_balance(ctx, user), token_balance(ctx, reader))
    page.click(PAY)  # on the moving chain a block comes just before the click, not counted below
    head0 = ctx.chain.head()
    wait_pool(ctx, 1)
    t_pool = now_ms()
    observe(page, PENDING_MS)  # the payment waits; the screen may claim anything it wants before its block
    empty = ctx.chain.head() - head0
    t_mine = ctx.chain.mine()
    wait_end(page)
    sts = states(page)
    early = [s for s in sts if claims_confirmed(s) and s['t'] < t_mine]
    elsewhere = early_page_claims(ctx, t_mine)
    c.expect('no_confirmed_before_block', not early and not elsewhere,
             history(sts, t_mine) + [f'elsewhere on the page at {(x["t"] - t_mine) / 1000:+.2f}s: "{x["text"][:80]}"' for x in elsewhere],
             'confirmed only after the block (t >= 0), on the status line and anywhere else on the page')
    h = status_hash(page)
    r = ctx.chain.receipt(h)
    c.expect('receipt_status_1', r is not None and r['status'] == '0x1', show(r and r['status']), '0x1')
    after = (token_balance(ctx, user), token_balance(ctx, reader))
    c.expect('tokens_moved_once', before[0] - after[0] == FEE and after[1] - before[1] == FEE,
             f'user -{(before[0] - after[0]) / UNIT:g}, reader +{(after[1] - before[1]) / UNIT:g}', 'user -25, reader +25')
    g = grant_on_chain(ctx, 1)
    c.expect('grant_active_on_chain', g['active'] and g['owner'] == user.lower(),
             f"grant #1 {'active' if g['active'] else 'inactive'}, owner {g['owner']}", 'grant #1 active, owned by user')
    blk = int(r['blockNumber'], 16) if r else ctx.chain.head()
    st = wait_indexed(ctx, blk)
    api = [x for x in st['grants'] if x['id'] == '1']
    same = bool(api) and api[0]['active'] and api[0]['owner'] == g['owner'] and api[0]['reader'] == g['reader'] and int(api[0]['paid']) == g['paid']
    c.expect('api_matches_chain', same,
             f"API grant #1 {'active' if api[0]['active'] else 'inactive'}, owner {api[0]['owner']}, paid {int(api[0]['paid']) / UNIT:g} TEST" if api else 'no grant in the API',
             'API grant #1 equals the chain')
    c.evidence.update({'tx': h, 'block': blk, 'ui_history': history(sts, t_mine),
                       'pending_s': round((t_mine - t_pool) / 1000, 2), 'blocks_without_the_payment': empty})
    c.evidence['screenshot'] = shot(ctx, page, 'confirmed')


def c08(ctx, c):
    """RPC outage on receipts, held until the screen reaches a terminal state plus a margin, and at least
    15 s after the payment reached the mempool. Then the bench restores the RPC and mines: both versions
    end on Confirmed, so only the full history (state and visible text, timed against the block) can
    tell them apart."""
    user = ctx.acc['user']
    ctx.proxy.cut('eth_getTransactionReceipt')
    page = ready_page(ctx)
    bal0 = token_balance(ctx, user)
    t_click = now_ms()
    page.click(PAY)  # on the moving chain a block comes just before the click, not counted below
    head0 = ctx.chain.head()
    wait_pool(ctx, 1)
    t_pool = now_ms()
    wait_state(page, TERMINAL, 10000)
    observe(page, max(OUTAGE_MARGIN_MS, t_pool + PENDING_MS - now_ms()))
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
    how = wait_until(page, 'after the block the screen neither ended the payment nor offered to check again',
                     lambda: 'ended' if last_state(page) in END else ('retry' if page.locator(RETRY_BTN).is_visible() else None))
    if how == 'retry':
        page.click(RETRY_BTN)
    wait_end(page)
    quiet([page], lambda: True, 300, 'the status line kept changing after Confirmed')
    final = [s for s in states(page) if s['t'] >= t_click]
    receipt_after = ctx.chain.receipt(h)
    early = [s for s in final if claims_confirmed(s) and s['t'] < t_mine]
    before_block = [s for s in final if s['t'] < t_mine]
    c.expect('never_confirmed_without_receipt', not early,
             history(before_block, t_click) + [f'receipt on chain before the block: none', f'blocks mined before the bench let the payment in: {head - head0}'],
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
        'pending_s': round((t_mine - t_pool) / 1000, 2),
        'cut_requests': sum(1 for e in ctx.proxy.events if e['cut']),
    })


EARLY, LATE = 'while the first payment waits', 'after the first payment landed'


def c09(ctx, c):
    """One order, at most one debit. Every way to pay again, twice: while the first payment still waits
    in the mempool, and after it landed in a block. After status unknown: the retry, the main Pay button,
    a reload, a second device; without any outage: a double click, and a wallet that broadcasts then
    answers an error. Each must move 25 TEST once; after the first payment landed, a Pay must send
    nothing at all. On the moving chain, blocks pass while the order is unpaid. A duplicate transaction
    the contract reverts is listed, not counted as a debit. Last, the contract itself refuses a second
    payment of a paid order, and an id that is not the open order."""
    user = ctx.acc['user']
    cut = ('eth_getTransactionReceipt', 'eth_getTransactionByHash')
    one_debit = 'one debit: -25 TEST'
    scenarios = []
    c.evidence['scenarios'] = scenarios

    def debit_line(s):
        pays = payments_since(ctx, s['head'], user)
        debit = s['balance'] - token_balance(ctx, user)
        return pays, debit, describe(ctx, pays) + f'; -{debit / UNIT:g} TEST'

    def note(name, late, s, pays, line, **extra):
        scenarios.append(dict({'scenario': name, 'ordering': LATE if late else EARLY, 'blocks': ctx.chain.head() - s['head'],
                               'payments': pays, 'result': line}, **extra))

    def screen(page, s):
        return history(states(page), s['t'])

    def sends_since(n0):
        return ctx.proxy.count('eth_sendTransaction') - n0

    def land(late, n=1):
        """Blocks pass while the first payment waits (moving chain). Late ordering: it lands now."""
        ctx.blocks_pass(n)
        return ctx.chain.mine() if late else None

    def unknown_page():
        ctx.proxy.cut(*cut)
        page = ready_page(ctx)
        s = mark(ctx, user)
        page.click(PAY)
        wait_pool(ctx, 1)
        wait_state(page, 'unknown', 8000)
        return page, s

    def nothing_sent(name, n0):
        n = sends_since(n0)
        c.expect(name, n == 0, f'{n} eth_sendTransaction from the app after the first payment landed', '0: the order is paid')

    first_order = None
    for late in (False, True):
        sfx = '_after_it_landed' if late else ''

        # 1. Status unknown, then the retry button.
        page, s = unknown_page()
        first = states(page)[-1]
        c.expect('outage_shown_as_unknown' + sfx, first['state'] == 'unknown' and not claims_confirmed(first),
                 f"{first['state']}: \"{first['text']}\"", 'unknown, without the word confirmed')
        t_land = land(late)
        ctx.proxy.restore()
        appear(page, RETRY_BTN, 'no retry button on screen after status unknown')
        label = page.inner_text(RETRY_BTN)
        c.expect('retry_offered' + sfx, True, f'"{label}"', 'a retry button on screen')
        t_retry = now_ms()
        page.click(RETRY_BTN)
        # Wait for the app's answer to the retry, however long it takes within the deadline:
        # a lookup of the first transaction, or a new payment.
        wait_until(page, 'the app answered the retry with neither a lookup of the first transaction nor a new payment',
                   lambda: ctx.proxy.lookups_since(t_retry, first['hash']) or sends_since(s['sends']) > 1)
        # Give the screen time to claim anything it wants before the bench mines.
        quiet([page], lambda: True, HOLD_MS, 'the status line kept changing after the retry')
        asked = ctx.proxy.lookups_since(t_retry, first['hash']) if first['hash'] else []
        sends = sends_since(s['sends'])
        if sends > 1:
            wait_until(page, 'a payment the app sent did not reach the node', lambda: len(payments_since(ctx, s['head'], user)) >= sends)
        c.expect('retry_checks_same_transaction' + sfx, len(asked) > 0,
                 f"{len(asked)} receipt or transaction lookup{'s' if len(asked) != 1 else ''} for the first transaction after the retry"
                 + (f', then {sends - 1} new payment' if sends > 1 else ''),
                 'at least 1 lookup for the first transaction')
        after_retry = payments_since(ctx, s['head'], user)
        c.expect('retry_sends_no_new_payment' + sfx, len(after_retry) == 1 and sends == 1,
                 f'{sends} eth_sendTransaction from the app; ' + describe(ctx, after_retry), '1 eth_sendTransaction; 1 payment transaction')
        t_mine = ctx.chain.mine() if ctx.chain.pending_count() else t_land
        wait_end(page)
        end = states(page)[-1]
        c.expect('screen_ends_confirmed' + sfx, end['state'] == 'confirmed' and end['text'] == 'Confirmed' and end['hash'] == first['hash'],
                 f"{end['state']}: \"{end['text']}\" for {'the first transaction' if end['hash'] == first['hash'] else 'another transaction'}",
                 'confirmed: "Confirmed" for the first transaction')
        settle(ctx, [page], s, user)
        pays, debit, line = debit_line(s)
        c.expect('retry_debits_once' + sfx, debit == FEE, line, one_debit)
        note('retry after status unknown', late, s, pays, line, retry_button=label, ui_history=history(states(page), s['t']),
             block_mined_at_s=round(((t_mine or now_ms()) - s['t']) / 1000, 2))
        if not late:
            c.evidence.update({'retry_button': label, 'payments': pays, 'ui_history': history(states(page), s['t']),
                               'retry_block_mined_at_s': round((t_mine - s['t']) / 1000, 2), 'screenshot': shot(ctx, page, 'after-retry')})
            first_order = next((p['intent'] for p in pays if p['status'] == '0x1'), None)
        page.context.close()
        next_order(ctx, user)

        # 2. Status unknown, then the main Pay button instead of the retry.
        page, s = unknown_page()
        # A lock, not an appearance: it must hold the moment the screen says unknown.
        disabled = page.is_disabled(PAY)
        c.expect('pay_locked_while_unknown' + sfx, disabled, f'Pay button {"disabled" if disabled else "active"}', 'Pay button disabled')
        land(late)
        n0 = ctx.proxy.count('eth_sendTransaction')
        force_click(page, PAY)
        settle(ctx, [page], s, user)
        if late:
            nothing_sent('pay_during_unknown_sends_nothing_once_paid', n0)
        pays, debit, line = debit_line(s)
        c.expect('pay_during_unknown_debits_once' + sfx, debit == FEE, line, one_debit)
        note('Pay during unknown', late, s, pays, line, ui_history=screen(page, s))
        page.context.close()
        next_order(ctx, user)

        # 3. Status unknown, the user reloads the page, then pays.
        page, s = unknown_page()
        pending_hash = states(page)[-1]['hash']
        land(late)
        page.reload()
        page.wait_for_selector('body[data-ready="1"]', timeout=10000)
        connect(page)
        sign_terms(page)
        # Wait until the app follows the pending transaction again, says the order is paid (only once it
        # is), or leaves the Pay button open.
        after = wait_until(page, 'after the reload the app neither followed the pending transaction, nor said Already paid, nor opened the Pay button',
                           lambda: 'resumed' if any(x['hash'] == pending_hash for x in states(page))
                           else ('already paid' if late and last_state(page) == 'already-paid'
                                 else ('open' if not page.is_disabled(PAY) else None)))
        c.expect('reload_resumes_same_transaction' + sfx, after in ('resumed', 'already paid'),
                 'after reload the status line ' + {'resumed': 'follows the pending transaction', 'already paid': 'says Already paid',
                                                     'open': 'follows no transaction, and the Pay button is open'}[after]
                 + ': ' + (', '.join(x['state'] for x in states(page)) or 'no state'),
                 'the pending transaction, followed again' + (', or Already paid' if late else ''))
        n0 = ctx.proxy.count('eth_sendTransaction')
        force_click(page, PAY)
        settle(ctx, [page], s, user)
        if late:
            nothing_sent('reload_then_pay_sends_nothing_once_paid', n0)
        pays, debit, line = debit_line(s)
        c.expect('reload_then_pay_debits_once' + sfx, debit == FEE, line, one_debit)
        note('reload, then Pay', late, s, pays, line, ui_history=screen(page, s))
        page.context.close()
        next_order(ctx, user)

        # 4. Status unknown on one device, the same account pays from another (a fresh browser: empty
        # storage). On the moving chain, blocks pass between the two devices while the order is unpaid.
        page, s = unknown_page()
        land(late, 2)
        other = ready_page(ctx)
        n0 = ctx.proxy.count('eth_sendTransaction')
        force_click(other, PAY)
        settle(ctx, [page, other], s, user)
        if late:
            nothing_sent('second_device_sends_nothing_once_paid', n0)
        pays, debit, line = debit_line(s)
        c.expect('second_device_debits_once' + sfx, debit == FEE, line, one_debit)
        note('Pay from a second device', late, s, pays, line, ui_history=screen(page, s), second_device_history=screen(other, s))
        page.context.close()
        other.context.close()
        next_order(ctx, user)

        # 5. No outage: a double click on Pay (no time between the two clicks, so once).
        if not late:
            page = ready_page(ctx)
            s = mark(ctx, user)
            page.dblclick(PAY)
            wait_pool(ctx, 1)
            settle(ctx, [page], s, user)
            pays, debit, line = debit_line(s)
            c.expect('double_click_debits_once', debit == FEE, line, one_debit)
            note('double click', late, s, pays, line, ui_history=screen(page, s))
            page.context.close()
            next_order(ctx, user)

        # 6. No outage: the wallet broadcasts the payment, then answers an error (a timeout). The user pays again.
        page = ready_page(ctx, failAfterSend=True)
        s = mark(ctx, user)
        page.click(PAY)
        wait_pool(ctx, 1)
        wait_state(page, 'error', 8000)
        after_error = states(page)[-1]['text']
        land(late)
        if late:
            # Once the payment is in a block, the screen must say so instead of "Something went wrong".
            try:
                wait_current(page, 'already-paid', APPEAR_MS)
            except BenchTimeout as e:
                c.waits_expired.append(f'scenario 6, {LATE}: {e}')
        before_pay = states(page)[-1]['text']
        n0 = ctx.proxy.count('eth_sendTransaction')
        force_click(page, PAY)
        settle(ctx, [page], s, user)
        if late:
            nothing_sent('wallet_error_then_pay_sends_nothing_once_paid', n0)
        pays, debit, line = debit_line(s)
        screens = f'; screen after the wallet error: "{after_error}"' + (f', once the payment landed: "{before_pay}"' if late else '') + f', at the end: "{states(page)[-1]["text"]}"'
        c.expect('wallet_error_then_pay_debits_once' + sfx, debit == FEE, line + screens, one_debit)
        note('Pay again after a wallet error', late, s, pays, line + screens, ui_history=screen(page, s))
        page.context.close()
        next_order(ctx, user)

    # 7. The contract itself, straight to the node: the paid order of scenario 1 sent again, then an id
    # that is not the open order (what an app making its own ids would send). Neither may debit.
    if first_order is None:
        raise BenchError('no payment from scenario 1 to replay')
    s = mark(ctx, user)
    out = {}
    for name, order, wanted in (('same_order', first_order, 'OrderAlreadyPaid'), ('other_id', ctx.chain.keccak('an order id made in a browser'), 'NotCurrentOrder')):
        b0 = token_balance(ctx, user)
        data = pay_data(ctx, order)
        err = ctx.chain.simulate_error(user, ctx.addr['grants'], data)
        h = ctx.chain.send(user, ctx.addr['grants'], data, gas=300000)
        ctx.chain.mine()
        r = ctx.chain.receipt(h)
        out[name] = {'tx': h, 'block': int(r['blockNumber'], 16), 'order': order, 'status': r['status'], 'error': error_name(ctx, err),
                     'debit_test': (b0 - token_balance(ctx, user)) / UNIT, 'wanted': wanted, 'err_sel': err}
    a, b = out['same_order'], out['other_id']
    c.expect('contract_refuses_same_order', a['status'] == '0x0' and a['err_sel'] == ctx.err['OrderAlreadyPaid'] and a['debit_test'] == 0,
             f"status {a['status']}, error {a['error']}, -{a['debit_test']:g} TEST", 'status 0x0, OrderAlreadyPaid(), -0 TEST')
    c.expect('contract_refuses_other_order_id', b['status'] == '0x0' and b['err_sel'] == ctx.err['NotCurrentOrder'] and b['debit_test'] == 0,
             f"status {b['status']}, error {b['error']}, -{b['debit_test']:g} TEST", 'status 0x0, NotCurrentOrder(), -0 TEST')
    for v in out.values():
        v.pop('err_sel')
    c.evidence['contract_replays'] = dict(out, path='eth_sendTransaction straight to the node, no interface')


def c10(ctx, c):
    user, low, out, admin = ctx.acc['user'], ctx.acc['user_low'], ctx.acc['outsider'], ctx.acc['admin']
    page = ctx.page('user')
    open_app(ctx, page)
    connect(page)
    appear(page, '[data-testid=admin-panel]', 'the admin panel was shown to an ordinary user', state='hidden')
    c.expect('admin_control_hidden_for_user', True, 'admin panel hidden', 'admin panel hidden')

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
    appear(apage, '[data-testid=admin-panel]', 'no admin panel for the admin wallet')
    _, r_a1, _ = attempt(admin, out, True)
    added = allowlisted(ctx, out)
    _, r_a2, _ = attempt(admin, out, False)
    removed = not allowlisted(ctx, out)
    c.expect('admin_can_edit_allowlist', r_a1['status'] == '0x1' and added and r_a2['status'] == '0x1' and removed,
             f"admin panel shown; add status {r_a1['status']}, remove status {r_a2['status']}",
             'admin panel shown; add status 0x1, remove status 0x1')
    c.evidence.update({'tx': h_add, 'block': int(r_add['blockNumber'], 16), 'remove_tx': h_rm,
                       'caller': 'user: allowlisted, not the admin', 'calls': ['setAllowlisted(outsider, true)', 'setAllowlisted(user_low, false)'],
                       'path': 'eth_sendTransaction straight to the node, no interface'})


def c11(ctx, c):
    reader = ctx.acc['reader']
    page = ready_page(ctx)
    page.click(PAY)
    wait_pool(ctx, 1)
    ctx.chain.mine()
    ended = wait_end(page)
    c.expect('payment_confirmed', ended == 'confirmed', ended, 'confirmed')
    wait_indexed(ctx, ctx.chain.head())
    # The reader's identity is simulated: it is declared in a request header, there is no reader sign-in.
    code1, _ = http_get(ctx.app_url + '/api/data/1', {'x-reader-address': reader})
    c.expect('api_serves_reader_while_active', code1 == 200, f'HTTP {code1}', 'HTTP 200')
    code_other, _ = http_get(ctx.app_url + '/api/data/1', {'x-reader-address': ctx.acc['outsider']})
    c.expect('api_refuses_other_reader', code_other == 403, f'HTTP {code_other}', 'HTTP 403')
    appear(page, '[data-testid=revoke-1]', 'no Revoke button for grant #1')
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
