// How the interface follows a payment after the wallet returns a transaction hash.
// Faulty version, as shipped before the release review.
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const randomIntent = () => '0x' + [...crypto.getRandomValues(new Uint8Array(32))].map((b) => b.toString(16).padStart(2, '0')).join('');
export const RETRY_LABEL = 'Try the payment again';

// No memory of a payment in progress: the Pay button stays active and every click is a new intent,
// made in the browser.
export function createPayments() {
  return {
    locked: () => false,
    begin: async () => randomIntent(),
    sent() {},
    abandon() {},
    settled() {},
    pending: () => null,
    intentOf: () => null,
    paid: async () => false,
  };
}

function lost(ui, wallet, hash, opts) {
  ui.setState('unknown', hash);
  ui.offerRetry(RETRY_LABEL, opts.payAgain || (() => {}));
  return null;
}

export async function follow(ui, wallet, hash, opts = {}) {
  ui.setState('submitted', hash);
  let failures = 0;
  for (let i = 0; i < 240; i++) {
    let receipt = null;
    try {
      receipt = await wallet.request({ method: 'eth_getTransactionReceipt', params: [hash] });
      failures = 0;
    } catch (e) {
      // The node is flaky: fall back to the transaction lookup.
      try {
        const tx = await wallet.request({ method: 'eth_getTransactionByHash', params: [hash] });
        if (tx) {
          ui.setState('confirmed', hash);
          if (opts.done) opts.done(tx);
          return tx;
        }
      } catch (_) { /* still down */ }
      failures += 1;
      if (failures >= 3) return lost(ui, wallet, hash, opts);
      await sleep(250);
      continue;
    }
    if (receipt && receipt.blockNumber) {
      ui.setState(receipt.status === '0x1' ? 'confirmed' : 'reverted', hash);
      if (opts.done) opts.done(receipt);
      return receipt;
    }
    await sleep(250);
  }
  return lost(ui, wallet, hash, opts);
}
