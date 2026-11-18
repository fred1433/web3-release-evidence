// How the interface follows a payment after the wallet returns a transaction hash.
// Faulty version, as shipped before the release review.
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
export const RETRY_LABEL = 'Try the payment again';

export async function follow(ui, wallet, hash, payAgain) {
  ui.setState('submitted', hash);
  let failures = 0;
  for (let i = 0; i < 240; i++) {
    let receipt = null;
    try {
      receipt = await wallet.request({ method: 'eth_getTransactionReceipt', params: [hash] });
    } catch (e) {
      // The node is flaky: fall back to the transaction lookup.
      try {
        const tx = await wallet.request({ method: 'eth_getTransactionByHash', params: [hash] });
        if (tx) {
          ui.setState('confirmed', hash);
          return tx;
        }
      } catch (_) { /* still down */ }
      failures += 1;
      if (failures >= 3) {
        ui.setState('unknown', hash);
        ui.offerRetry(RETRY_LABEL, payAgain);
        return null;
      }
      await sleep(250);
      continue;
    }
    if (receipt && receipt.blockNumber) {
      ui.setState(receipt.status === '0x1' ? 'confirmed' : 'reverted', hash);
      return receipt;
    }
    await sleep(250);
  }
  ui.setState('unknown', hash);
  ui.offerRetry(RETRY_LABEL, payAgain);
  return null;
}
