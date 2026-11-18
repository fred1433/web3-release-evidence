// How the interface follows a payment after the wallet returns a transaction hash.
// Corrected version: "Confirmed" only from a receipt included in a block.
// After repeated RPC failures the status is "unknown", and the retry action
// checks the SAME transaction again instead of paying a second time.
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
export const RETRY_LABEL = 'Check status again';

export async function follow(ui, wallet, hash, payAgain) {
  ui.setState('submitted', hash);
  let failures = 0;
  for (let i = 0; i < 240; i++) {
    let receipt = null;
    try {
      receipt = await wallet.request({ method: 'eth_getTransactionReceipt', params: [hash] });
    } catch (e) {
      failures += 1;
      if (failures >= 3) {
        ui.setState('unknown', hash);
        ui.offerRetry(RETRY_LABEL, () => follow(ui, wallet, hash, payAgain));
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
  ui.offerRetry(RETRY_LABEL, () => follow(ui, wallet, hash, payAgain));
  return null;
}
