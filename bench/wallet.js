// EIP-1193 test provider injected before the page loads (Playwright add_init_script).
// It answers the wallet-only methods itself (accounts, chain switching, user refusal 4001)
// and forwards everything else to the RPC endpoint the app is configured with.
// It also records every wallet call, every transaction it sent, every change of the status line (its
// state and the text of its title) and every moment the rest of the page claims confirmed (every other
// visible text, the rest of the status card included, the tab title, alert and confirm dialogs), so the bench judges the whole
// history of the page, not just the final state of one line. Each record is also sent to the bench as
// it happens (window.__benchRecord), so the history survives a reload and a closed tab.
(() => {
  const cfg = window.__WALLET_CONFIG;
  let chainId = cfg.chainId;
  const listeners = {};
  const emit = (ev, v) => (listeners[ev] || []).forEach((f) => { try { f(v); } catch (_) {} });
  window.__walletCalls = [];
  window.__uiStates = [];
  window.__pageClaims = [];
  const tell = (entry) => { try { if (window.__benchRecord) window.__benchRecord(entry); } catch (_) { /* bench not listening */ } };

  const rejected = () => Object.assign(new Error('User rejected the request.'), { code: 4001 });
  async function forward(method, params) {
    const r = await fetch(cfg.rpcUrl, {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ jsonrpc: '2.0', id: Date.now(), method, params }),
    });
    if (!r.ok) throw Object.assign(new Error(`RPC unavailable (HTTP ${r.status})`), { code: -32603 });
    const j = await r.json();
    if (j.error) throw Object.assign(new Error(j.error.message), { code: j.error.code, data: j.error.data });
    return j.result;
  }

  window.ethereum = {
    isTestWallet: true,
    on(ev, f) { (listeners[ev] = listeners[ev] || []).push(f); },
    removeListener(ev, f) { listeners[ev] = (listeners[ev] || []).filter((g) => g !== f); },
    async request({ method, params = [] }) {
      window.__walletCalls.push({ method, t: Date.now() });
      switch (method) {
        case 'eth_requestAccounts':
        case 'eth_accounts':
          return [cfg.account];
        case 'eth_chainId':
          return chainId;
        case 'wallet_switchEthereumChain': {
          const target = params[0] && params[0].chainId;
          if (target !== cfg.homeChainId) throw Object.assign(new Error('Unrecognized chain ID.'), { code: 4902 });
          chainId = target;
          emit('chainChanged', chainId);
          return null;
        }
        case 'personal_sign':
          if (cfg.rejectSign) throw rejected();
          return forward('personal_sign', params);
        case 'eth_sendTransaction': {
          if (cfg.rejectSend) throw rejected();
          const tx = Object.assign({}, params[0]);
          if (cfg.fixedGas) tx.gas = cfg.fixedGas;
          if (cfg.failAfterSend && !window.__failedAfterSend) {
            // The wallet broadcasts the transaction, then answers an error instead of its hash (a timeout).
            window.__failedAfterSend = true;
            tell({ kind: 'sent', hash: String(await forward('eth_sendTransaction', [tx])).toLowerCase(), t: Date.now() });
            throw Object.assign(new Error('Request timed out'), { code: -32603 });
          }
          const hash = await forward('eth_sendTransaction', [tx]);
          tell({ kind: 'sent', hash: String(hash).toLowerCase(), t: Date.now() });
          return hash;
        }
        default:
          return forward(method, params);
      }
    },
  };
  window.__testWallet = { setChain(id) { chainId = id; emit('chainChanged', id); } };

  // Same rule as the bench (checks.py CONFIRMED_WORD): the word confirmed, unless it says the opposite or
  // the future ("not confirmed", "isn't confirmed", "will be confirmed", "not yet confirmed", "until confirmed").
  const CLAIM = /(?<![a-z])(?<!not )(?<!n't )(?<!be )(?<!yet )(?<!until )confirmed\b/i;
  let lastPage = '';
  const pageClaim = (extra) => {
    const found = [];
    if (CLAIM.test(document.title)) found.push('tab title: ' + document.title.trim());
    if (document.body) {
      const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
      for (let n = w.nextNode(); n; n = w.nextNode()) {
        const p = n.parentElement;
        if (!p || p.closest('#status-title, #status-hash, script, style, template') || !p.checkVisibility()) continue;
        const t = n.textContent.replace(/\s+/g, ' ').trim();
        if (CLAIM.test(t)) found.push(t.slice(0, 160));
      }
    }
    if (extra) found.push(extra);
    const text = found.join(' | ');
    if (text !== lastPage || extra) {
      lastPage = extra ? '' : text;
      if (text) {
        const entry = { kind: 'page', text, t: Date.now() };
        window.__pageClaims.push(entry);
        tell(entry);
      }
    }
  };
  for (const name of ['alert', 'confirm']) {
    const original = window[name];
    window[name] = function (msg) {
      if (CLAIM.test(String(msg))) pageClaim(`${name} dialog: ${String(msg).slice(0, 160)}`);
      return original.apply(this, arguments);
    };
  }

  const record = () => {
    pageClaim();
    const el = document.querySelector('[data-testid="tx-status"]');
    if (!el) return;
    const s = el.dataset.state;
    const title = el.querySelector('#status-title');
    const text = title ? title.textContent.trim() : '';
    const last = window.__uiStates[window.__uiStates.length - 1];
    if (!last || last.state !== s || last.text !== text) {
      const code = el.querySelector('code');
      const entry = { kind: 'status', state: s, text, t: Date.now(), hash: code ? code.textContent : '' };
      window.__uiStates.push(entry);
      tell(entry);
    }
  };
  document.addEventListener('DOMContentLoaded', () => {
    new MutationObserver(record).observe(document.documentElement, { subtree: true, childList: true, characterData: true, attributes: true });
  });
})();
