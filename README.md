# web3-release-evidence

A release bench for EVM dApps. It drives the interface with Playwright and a test wallet, cuts the RPC on purpose, reads the chain directly, and ends on a status computed from written criteria:

- `Blocked`: a required check failed on a named assertion.
- `Incomplete`: no failure, but a required check was skipped, timed out or crashed. The denominator never shrinks.
- `Demonstrated checks passed`: every required check ran and passed.

Every report also says `Client MVP: not assessed`. The bench runs here against **Sample Grants**, a synthetic app built for this demo: a holder grants a reader access to a fictitious dataset, pays the reader 25 `TEST` units, and can revoke the grant. Local chain only, development accounts, no real funds, no private key anywhere in this repository.

Page with the recorded runs: https://theaipipe.com/demos/web3-release-evidence/

## What the recorded runs show

| App | Status | Failed checks |
| --- | --- | --- |
| `variants/faulty` | Blocked | C08, C09, C10 |
| `variants/corrected` | Demonstrated checks passed | none |

Three faults are planted in `variants/faulty` (`variants/faults.json`); `variants/corrected` is the same app with the three fixes and nothing else (`variants.diff`). The checks are identical for both.

| Fault | What happens | Found by |
| --- | --- | --- |
| D1 | The receipt request fails, the app falls back to the transaction lookup and shows **Confirmed** while the transaction is still in the mempool. | C08, from the full history of the status element, not its final state |
| D2 | `setAllowlisted` has no `onlyAdmin`. The interface hides it, the contract does not refuse it. | C10, a direct call to the contract from an ordinary account |
| D3 | After "status unknown", the retry button sends a second payment. | C09, a scan of the mined blocks: two payments for one intent |

Reports: `reports/faulty.json`, `reports/corrected.json`. Twenty independent runs per variant: `reports/stability-*.json`. A run with one check removed: `reports/corrected-skip-C08.json` (status `Incomplete`).

## How it works

```
interface (Playwright, test wallet) ─┐
indexer + API (app/server.mjs) ──────┼─> RPC proxy (cuts chosen methods) ─> anvil (manual mining)
                                     │                                        ^
bench (bench/checks.py) ─────────────┴──── reads the chain directly ──────────┘
```

- `anvil --no-mining`: the bench mines with `evm_mine`, so the moment between "sent" and "in a block" is observable. Each check starts from the same `evm_snapshot`.
- The app only reaches the node through `RpcProxy` (`bench/harness.py`), which can answer HTTP 503 to `eth_getTransactionReceipt` or any other method. The bench reads anvil directly, so it knows what the chain holds while the app is being lied to.
- `bench/wallet.js` is an EIP-1193 test provider: it answers `eth_requestAccounts`, `wallet_switchEthereumChain` (4902 for an unknown chain), user refusal (4001) and forwards the rest, including `personal_sign` and `eth_sendTransaction`. It records every wallet call and every change of the status element.
- `bench/required-checks.json` maps each check to an acceptance criterion and a rule (marked when the rule is fictitious), with what is allowed and what is forbidden. `bench/status.py` turns the results into the status.

## Run it

Requirements: Foundry v1.9.0 (`foundryup --install v1.9.0`), Node 20 or newer, Python 3.10 or newer.

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium

python bench/selftest.py                                  # the status rule on synthetic results
python bench/run.py --variant faulty                      # Blocked, exit code 1
python bench/run.py --variant corrected                   # Demonstrated checks passed, exit code 0
python bench/run.py --variant corrected --skip C08 --out reports/corrected-skip-C08.json   # Incomplete, exit code 3
python bench/run.py --variant corrected --repeat 20        # writes reports/stability-corrected.json
python bench/run.py --variant faulty --expect Blocked     # exit 0 only if the status is Blocked (CI)
```

One run takes about 15 seconds. CI runs both variants on every push (`.github/workflows/release-evidence.yml`).

## Pointing it at another dApp

Ready in the bench: the RPC proxy and its outage modes, the test wallet, manual mining with snapshots, the state history recorder, the direct chain reads, the manifest and the status rule, the JSON report and CI.

Depends on the product: the contract addresses and ABIs, the indexer or backend and how it reaches the node, the selectors of the screens, the wallet and sign-in flow (SIWE, embedded wallet, account abstraction), the compliance steps (KYC or KYB provider, allowlist source), and which journey goes first. EVM is an assumption of this demo.

Phases: on a testnet, replay the transaction journeys with funded test wallets. Before mainnet, rehearse the deployment on a local fork (`anvil --fork-url <mainnet RPC>`, impersonation, no key) and run read-only configuration checks against mainnet: chain id, deployed bytecode and addresses, admin role held by the expected multisig. A reorg case (`anvil_reorg`) belongs to that rehearsal; it is not built here.

## Not covered

See `not_covered` in any report: real wallet extensions and mobile wallets, an economic or security audit of the contract, gas spikes on a public network, chain reorganisations, the legal drafting of the terms, load and indexer restarts, and the client's own application.

Built by [The AI Pipe](https://theaipipe.com). MIT licence.
