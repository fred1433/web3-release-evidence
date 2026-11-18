// How the interface follows a payment after the wallet returns a transaction hash.
// Corrected version: "Confirmed" only from a receipt included in a block.
// After repeated RPC failures the status is "unknown", and the retry action
// checks the SAME transaction again instead of paying a second time.
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const pad = (hex) => hex.replace(/^0x/, '').padStart(64, '0');
export const RETRY_LABEL = 'Check status again';

// The order id is not made in the browser. The contract derives it from the order itself (payer,
// reader, dataset, access period), so every device, every browser and every retry that pays the same
// order sends the same id, however many blocks pass, and the contract refuses a second debit of it.
// The browser writes the order id down before anything is sent, and the transaction in progress once
// the wallet returns it (localStorage): that locks the Pay button while a payment is pending or
// unknown and lets the app follow it again after a reload. Before Pay is opened again, the app asks
// the contract whether the order is already paid.
export function createPayments({ wallet, cfg, storage = window.localStorage }) {
  const key = (a) => 'sample-grants:payment:' + a;
  const read = (a) => { try { return JSON.parse(storage.getItem(key(a)) || 'null'); } catch (_) { return null; } };
  const write = (a, v) => { try { if (v) storage.setItem(key(a), JSON.stringify(v)); else storage.removeItem(key(a)); } catch (_) { /* memory lock still holds */ } };
  const call = (data) => wallet.request({ method: 'eth_call', params: [{ to: cfg.grants, data }, 'latest'] });
  const openOrder = (a) => call(cfg.selectors.orderOf + pad(a) + pad(cfg.reader) + pad(cfg.datasetId));
  const inFlight = new Set();
  return {
    locked: (a) => inFlight.has(a) || Boolean(read(a)?.hash),
    async begin(a) {
      inFlight.add(a);
      try {
        const order = read(a)?.order || await openOrder(a);
        write(a, { order });
        return order;
      } catch (e) { inFlight.delete(a); throw e; }
    },
    sent(a, hash, order) { write(a, { order, hash }); },
    // A refusal sent nothing: the record goes. Any other error may hide a payment that went out: the order
    // id stays written down, and the next Pay first asks the contract whether that order is paid.
    abandon(a, rejected) { inFlight.delete(a); if (rejected) write(a, null); },
    settled(a) { inFlight.delete(a); write(a, null); },
    pending: (a) => read(a)?.hash || null,
    orderOf: (a) => read(a)?.order || null,
    // Is this account's order paid? The order written down if there is one, otherwise the open order.
    async paid(a, order) {
      const o = order || read(a)?.order || await openOrder(a);
      return BigInt(await call(cfg.selectors.orderPaid + pad(o))) === 1n;
    },
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
      // A reverted duplicate is not a failed payment if an earlier transaction paid the same order.
      if (state === 'reverted' && opts.paid) { try { if (await opts.paid()) state = 'already-paid'; } catch (_) { /* stays reverted */ } }
      ui.setState(state, hash);
      if (opts.done) opts.done(receipt);
      return receipt;
    }
    await sleep(250);
  }
  return lost(ui, wallet, hash, opts);
}
