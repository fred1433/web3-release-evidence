import { follow, createPayments } from '/flow.js';

const cfg = await (await fetch('/config.json')).json();
const $ = (id) => document.getElementById(id);
const wallet = window.ethereum;
let account = null;
let chainOk = false;
let accepted = false;
const payments = createPayments();

const TITLES = {
  idle: 'No payment yet',
  'awaiting-signature': 'Waiting for your wallet',
  rejected: 'You declined the request in your wallet. Nothing was sent.',
  submitted: 'Pending: waiting for the network to include it',
  confirmed: 'Confirmed',
  reverted: 'Failed: the contract rejected the payment',
  unknown: 'Status unknown: the network did not answer',
  error: 'Something went wrong',
};
const ui = {
  setState(state, detail = '') {
    $('status').dataset.state = state;
    $('status-title').textContent = TITLES[state] || state;
    $('status-hash').textContent = detail && detail.startsWith('0x') ? detail : '';
    if (state !== 'unknown') $('retry').hidden = true;
    refreshButtons();
  },
  offerRetry(label, action) {
    const b = $('retry');
    b.textContent = label;
    b.hidden = false;
    b.onclick = () => { b.hidden = true; action(); };
  },
};

const pad = (hex) => hex.replace(/^0x/, '').padStart(64, '0');
const toHex = (n) => '0x' + BigInt(n).toString(16);
const units = (wei) => (Number(BigInt(wei) / 10n ** 15n) / 1000).toLocaleString('en-US');

$('reader').textContent = cfg.reader.slice(0, 6) + '…' + cfg.reader.slice(-4);
$('dataset').textContent = cfg.datasetName;
$('terms-text').textContent = cfg.terms;

function refreshButtons() {
  $('accept').disabled = !(account && chainOk) || accepted;
  $('pay').disabled = !(account && chainOk && accepted) || payments.locked(account);
  $('wrong-net').hidden = !account || chainOk;
}

async function onChain(chainId) {
  chainOk = chainId === cfg.chainId;
  $('network').textContent = chainOk ? 'Local dev chain' : `Chain ${parseInt(chainId, 16)}`;
  refreshButtons();
}

async function refresh() {
  if (!account) return;
  try {
    const bal = await wallet.request({ method: 'eth_call', params: [{ to: cfg.token, data: cfg.selectors.balanceOf + pad(account) }, 'latest'] });
    $('balance').textContent = units(bal);
  } catch (_) { /* balance stays as last read */ }
  try {
    const r = await (await fetch('/api/grants?owner=' + account)).json();
    const list = $('grants');
    list.innerHTML = '';
    if (!r.grants.length) list.innerHTML = '<li class="empty">None yet.</li>';
    for (const g of r.grants) {
      const li = document.createElement('li');
      li.dataset.testid = 'grant-' + g.id;
      li.innerHTML = `<span>Grant #${g.id} to Reviewer R-1, ${g.active ? 'active' : 'revoked'}</span>`;
      if (g.active) {
        const b = document.createElement('button');
        b.textContent = 'Revoke';
        b.dataset.testid = 'revoke-' + g.id;
        b.onclick = () => revoke(g.id);
        li.append(b);
      }
      list.append(li);
    }
  } catch (_) { /* api down */ }
}

async function connect() {
  if (!wallet) { $('no-wallet').hidden = false; return; }
  const [a] = await wallet.request({ method: 'eth_requestAccounts' });
  account = a.toLowerCase();
  $('connect').hidden = true;
  $('who').hidden = false;
  $('account').textContent = account.slice(0, 6) + '…' + account.slice(-4);
  $('admin').hidden = account !== cfg.admin;
  await onChain(await wallet.request({ method: 'eth_chainId' }));
  wallet.on?.('chainChanged', onChain);
  const pending = payments.pending(account);
  if (pending) follow(ui, wallet, pending, { payAgain: pay, done: () => payments.settled(account) });
  refreshButtons();
  refresh();
  setInterval(refresh, 1000);
}

async function accept() {
  try {
    const signature = await wallet.request({ method: 'personal_sign', params: ['0x' + [...new TextEncoder().encode(cfg.terms)].map((b) => b.toString(16).padStart(2, '0')).join(''), account] });
    const r = await fetch('/api/acceptance', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ account, message: cfg.terms, signature }) });
    if (!r.ok) throw new Error((await r.json()).error);
    accepted = true;
    $('accept').hidden = true;
    $('accepted').hidden = false;
  } catch (e) {
    ui.setState(e.code === 4001 ? 'rejected' : 'error', '');
  }
  refreshButtons();
}

async function sendTx(data) {
  if ((await wallet.request({ method: 'eth_chainId' })) !== cfg.chainId) { await onChain('0x0'); return { hash: null, rejected: false }; }
  ui.setState('awaiting-signature');
  try {
    return { hash: await wallet.request({ method: 'eth_sendTransaction', params: [{ from: account, to: cfg.grants, data }] }) };
  } catch (e) {
    ui.setState(e.code === 4001 ? 'rejected' : 'error', '');
    return { hash: null, rejected: e.code === 4001 };
  }
}

async function pay() {
  if (!accepted || payments.locked(account)) return;
  const intent = payments.begin(account);
  refreshButtons();
  const { hash, rejected } = await sendTx(cfg.selectors.grantAndPay + pad(cfg.reader) + pad(cfg.datasetId) + pad(toHex(cfg.fee)) + pad(intent));
  if (!hash) { payments.abandon(account, rejected); refreshButtons(); return; }
  payments.sent(account, hash);
  await follow(ui, wallet, hash, { payAgain: pay, done: () => payments.settled(account) });
  refresh();
}

async function revoke(id) {
  const { hash } = await sendTx(cfg.selectors.revoke + pad(toHex(id)));
  if (hash) await follow(ui, wallet, hash, { payAgain: () => revoke(id) });
  refresh();
}

$('connect').onclick = connect;
$('accept').onclick = accept;
$('pay').onclick = pay;
$('switch').onclick = async () => {
  try { await wallet.request({ method: 'wallet_switchEthereumChain', params: [{ chainId: cfg.chainId }] }); } catch (_) { /* user stays on wrong chain */ }
};
$('admin-add').onclick = async () => {
  const addr = $('admin-addr').value.trim().toLowerCase();
  const { hash } = await sendTx(cfg.selectors.setAllowlisted + pad(addr) + pad('0x1'));
  if (hash) await follow(ui, wallet, hash);
};
document.body.dataset.ready = '1';
