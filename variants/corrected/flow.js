// How the interface follows a payment after the wallet returns a transaction hash.
// Corrected version: "Confirmed" only from a receipt included in a block.
// After repeated RPC failures the status is "unknown", and the retry action
// checks the SAME transaction again instead of paying a second time.
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const pad = (hex) => hex.replace(/^0x/, '').padStart(64, '0');
export const RETRY_LABEL = 'Check status again';

// The payment intent is not made in the browser. The contract derives it from the order
// (payer, reader, dataset, orders already paid), so every device, every browser and every
// retry that pays the same unpaid order sends the same intent, and the contract refuses an
// intent that was already paid: at most one debit per order.
// The browser only remembers the transaction in progress (localStorage), to lock the Pay
// button while it is pending or unknown and to resume following it after a reload.
export function createPayments({ wallet, cfg, storage = window.localStorage }) {
  const key = (a) => 'sample-grants:payment:' + a;
  const read = (a) => { try { return JSON.parse(storage.getItem(key(a)) || 'null'); } catch (_) { return null; } };
  const write = (a, v) => { try { if (v) storage.setItem(key(a), JSON.stringify(v)); else storage.removeItem(key(a)); } catch (_) { /* memory lock still holds */ } };
  const call = (data) => wallet.request({ method: 'eth_call', params: [{ to: cfg.grants, data }, 'latest'] });
  const inFlight = new Set();
  return {
    locked: (a) => inFlight.has(a) || Boolean(read(a)?.hash),
    async begin(a) {
      inFlight.add(a);
      try { return await call(cfg.selectors.intentFor + pad(a) + pad(cfg.reader) + pad(cfg.datasetId)); } catch (e) { inFlight.delete(a); throw e; }
    },
    sent(a, hash, intent) { write(a, { hash, intent }); },
    // A refusal sent nothing. Any other error may hide a payment that went out: the next Pay asks the
    // contract for the intent again and gets the same one while that payment is unpaid.
    abandon(a, rejected) { inFlight.delete(a); if (rejected) write(a, null); },
    settled(a) { inFlight.delete(a); write(a, null); },
    pending: (a) => read(a)?.hash || null,
    intentOf: (a) => read(a)?.intent || null,
    async paid(a, intent) { return Boolean(intent) && BigInt(await call(cfg.selectors.intentPaid + pad(a) + pad(intent))) === 1n; },
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
      let state = receipt.status === '0x1' ? 'confirmed' : 'reverted';
      // A reverted duplicate is not a failed payment if an earlier transaction paid the same intent.
      if (state === 'reverted' && opts.paid) { try { if (await opts.paid()) state = 'already-paid'; } catch (_) { /* stays reverted */ } }
      ui.setState(state, hash);
      if (opts.done) opts.done(receipt);
      return receipt;
    }
    await sleep(250);
  }
  return lost(ui, wallet, hash, opts);
}
