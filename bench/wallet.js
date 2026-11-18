// EIP-1193 test provider injected before the page loads (Playwright add_init_script).
// It answers the wallet-only methods itself (accounts, chain switching, user refusal 4001)
// and forwards everything else to the RPC endpoint the app is configured with.
// It also records every wallet call and every change of the interface status, so the
// bench judges the whole history of what the screen said, not just its final state.
(() => {
  const cfg = window.__WALLET_CONFIG;
  let chainId = cfg.chainId;
  const listeners = {};
  const emit = (ev, v) => (listeners[ev] || []).forEach((f) => { try { f(v); } catch (_) {} });
  window.__walletCalls = [];
  window.__uiStates = [];

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
          return forward('eth_sendTransaction', [tx]);
        }
        default:
          return forward(method, params);
      }
    },
  };
  window.__testWallet = { setChain(id) { chainId = id; emit('chainChanged', id); } };

  const record = () => {
    const el = document.querySelector('[data-testid="tx-status"]');
    if (!el) return;
    const s = el.dataset.state;
    const last = window.__uiStates[window.__uiStates.length - 1];
    if (!last || last.state !== s) {
      const code = el.querySelector('code');
      window.__uiStates.push({ state: s, t: Date.now(), hash: code ? code.textContent : '' });
    }
  };
  document.addEventListener('DOMContentLoaded', () => {
    new MutationObserver(record).observe(document.documentElement, { subtree: true, attributes: true, attributeFilter: ['data-state'] });
  });
})();
