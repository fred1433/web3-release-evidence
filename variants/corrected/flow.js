// How the interface follows a payment after the wallet returns a transaction hash.
// Corrected version: "Confirmed" only from a receipt included in a block.
// After repeated RPC failures the status is "unknown", and the retry action
// checks the SAME transaction again instead of paying a second time.
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const randomIntent = () => '0x' + [...crypto.getRandomValues(new Uint8Array(32))].map((b) => b.toString(16).padStart(2, '0')).join('');
export const RETRY_LABEL = 'Check status again';

// A payment in progress is remembered per account (localStorage) until its receipt arrives.
// The Pay button stays locked while it is pending or unknown, a reload resumes following it,
// and its intent id is reused, so the contract refuses a second payment for the same intent.
export function createPayments(storage = window.localStorage) {
  const key = (a) => 'sample-grants:payment:' + a;
  const read = (a) => { try { return JSON.parse(storage.getItem(key(a)) || 'null'); } catch (_) { return null; } };
  const write = (a, v) => { try { if (v) storage.setItem(key(a), JSON.stringify(v)); else storage.removeItem(key(a)); } catch (_) { /* memory lock still holds */ } };
  const inFlight = new Set();
  return {
    locked: (a) => inFlight.has(a) || Boolean(read(a)?.hash),
    begin(a) { inFlight.add(a); const r = read(a) || { intent: randomIntent() }; write(a, r); return r.intent; },
    sent(a, hash) { write(a, { ...read(a), hash }); },
    // A refusal sent nothing. Any other error may hide a payment that went out: keep its intent.
    abandon(a, rejected) { inFlight.delete(a); if (rejected) write(a, null); },
    settled(a) { inFlight.delete(a); write(a, null); },
    pending: (a) => read(a)?.hash || null,
  };
}

function lost(ui, wallet, hash, opts) {
  ui.setState('unknown', hash);
  ui.offerRetry(RETRY_LABEL, () => follow(ui, wallet, hash, opts));
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
