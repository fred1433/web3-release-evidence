# web3-release-evidence

A release bench for EVM dApps. It drives the interface with Playwright and a test wallet, cuts the RPC on purpose, reads the chain directly, and ends on a status computed from written criteria:

- `Blocked`: a required check failed on a named assertion.
- `Incomplete`: no failure, but a required check was skipped, timed out or crashed. The denominator never shrinks.
- `Demonstrated checks passed`: every required check ran and passed.

Waiting is never a verdict: when something the bench waits for does not happen in time (a screen state, a transaction in the mempool, the indexer catching up), the check ends in error and the run is `Incomplete`. Only a named assertion that fails is a detection.

Every report also says `Client MVP: not assessed`. The bench runs here against **Sample Grants**, a synthetic app built for this demo: a holder grants a reader access to a fictitious dataset, pays the reader 25 `TEST` units, and can revoke the grant. Local chain only, development accounts, no real funds, no private key anywhere in this repository.

Page with the recorded runs: https://theaipipe.com/demos/web3-release-evidence/

## What the recorded runs show

| App | Status | Failed checks |
| --- | --- | --- |
| `variants/faulty` | Blocked | C08, C09, C10 |
| `variants/corrected` | Demonstrated checks passed | none |

Three faults are planted in `variants/faulty` (`variants/faults.json`); `variants/corrected` is the same app with the three fixes and nothing else (`variants.diff`). The checks are identical for both. A planted fault counts as detected only when its own critical assertion fails.

| Fault | What happens | Found by (critical assertion) |
| --- | --- | --- |
| D1 | The receipt request fails, the app falls back to the transaction lookup and shows **Confirmed** while the transaction is still in the mempool. | C08 (`never_confirmed_without_receipt`). Receipt requests are cut on the app's path until the screen settles, plus 5 seconds; the node still answers the bench. Then the bench restores the RPC and mines, and both versions end on Confirmed: only the timed history of the status line, state and visible text, tells them apart. The same rule is also an invariant on every check that opens the app (below). |
| D2 | `setAllowlisted` has no `onlyAdmin`. The interface hides it, the contract does not refuse it. | C10 (`ordinary_account_cannot_add`). An allowlisted ordinary account calls the contract directly, to add and to remove; both must revert with `NotAdmin()`, and the admin must still be able to edit. |
| D3 | After "status unknown", the retry button sends a second payment. Nothing remembers the payment in progress: the Pay button stays active, a reload forgets it, each click makes a new intent in the browser, and the contract accepts the same intent twice. | C09 (`retry_sends_no_new_payment`). The retry must look up the same transaction, send nothing new, and end on Confirmed. Then five other ways to pay again (Pay during "unknown", reload then Pay, Pay from a second device, double click, Pay again after the wallet broadcast the payment then answered an error) must each move 25 TEST once: at most one debit per intent. A duplicate that the contract reverts is listed in the report and not counted as a debit. Finally the same intent, sent straight to the contract, must revert with `IntentAlreadyPaid()`. |

In the corrected app the payment intent does not come from the browser. The contract derives it from the order (`intentFor(payer, reader, dataset)`: the payer, the reader, the dataset and how many such orders the payer has already paid), so a retry, a reload, a second device or a cleared browser storage all send the same intent while the order is unpaid, and the contract refuses a paid intent. The browser keeps only the transaction in progress (`localStorage`), to lock the Pay button while it is pending or unknown and to resume following it after a reload. When a duplicate gets through anyway (second device, wallet error), the contract reverts it and the screen says "Already paid", not "Failed".

### Invariants

- **D1, everywhere the bench watches the screen.** In every check that opens the app (all but C05), the status line never says Confirmed before the block that holds the receipt of the transaction it names (`no_confirmed_before_receipt_block`). The test wallet sends each change of the status line to the bench as it happens, on every tab and across reloads; the bench notes the time of each block just before mining it.
- **D3: at most one debit per payment intent.** C09 counts the TEST that left the payer in each of its six pay-again situations, and lists every payment transaction with its status.
- **Waits.** Every wait has a deadline. A slow but correct app passes (a retry that checks the transaction after 2 seconds does); a passed deadline makes the run Incomplete, never Blocked. Before mining, the bench holds a short observation window (1 s with no change on the status line, 0.5 s after a pay-again) so a wrong claim has time to show; a window can only give a fault time to appear, it cannot make a correct app fail.

Each rule is turned false on purpose by a mutant of the app (`bench/mutants.py`, report in `reports/mutants.json`).

C11 checks revocation with the reader's identity simulated: the API trusts the address declared in the `x-reader-address` header, and the demo has no reader sign-in. It shows that the API applies the grant state to that identity once the revocation is mined and indexed, not that the caller owns the address.

Reports: `reports/faulty.json`, `reports/corrected.json`. Twenty independent runs per variant: `reports/stability-*.json`. A run with one check removed: `reports/corrected-skip-C08.json` (status `Incomplete`).

## How it works

```
interface (Playwright, test wallet) ─┐
indexer + API (app/server.mjs) ──────┼─> RPC proxy (cuts chosen methods) ─> anvil (manual mining)
                                     │                                        ^
bench (bench/checks.py) ─────────────┴──── reads the chain directly ──────────┘
```

- `anvil --no-mining`: the bench mines with `evm_mine`, so the moment between "sent" and "in a block" is observable. Each check starts from the same `evm_snapshot`.
- The app only reaches the node through `RpcProxy` (`bench/harness.py`), which can answer HTTP 503 to `eth_getTransactionReceipt` or any other method, and logs every receipt or transaction lookup with its hash. The bench reads anvil directly, block by block and in the mempool, so it knows what the chain holds while the app is being lied to.
- `bench/wallet.js` is an EIP-1193 test provider: it answers `eth_requestAccounts`, `wallet_switchEthereumChain` (4902 for an unknown chain), user refusal (4001) and forwards the rest, including `personal_sign` and `eth_sendTransaction`. It records every wallet call and every change of the status line, its state and its visible text.
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
python bench/run.py --variant faulty --expect Blocked --expect-detected D1,D2,D3
    # exit 0 only if the status is Blocked AND exactly D1, D2 and D3 were detected on their critical
    # assertions, the failed checks are exactly C08, C09 and C10, and no check errored or was left out
python bench/run.py --variant corrected --expect "Demonstrated checks passed" --expect-detected none
python bench/mutants.py --jobs 3                          # every mutant, writes reports/mutants.json; exit 0 only if each ended as expected
python bench/mutants.py m1_confirm_on_retry m4_slow_retry # some of them
```

One run takes about 30 seconds, measured: 28.7 to 31.9 s over the 40 runs in `reports/stability-*.json` (an Apple Silicon laptop, the two series side by side), 30.5 s and 31.1 s for `reports/corrected.json` and `reports/faulty.json`. `bench/mutants.py --jobs 3` took 3 min 37 s on the same laptop. CI runs both variants on every push (`.github/workflows/release-evidence.yml`) with the two `--expect` commands above, and every mutant with `bench/mutants.py`.

## Pointing it at another dApp

Ready in the bench: the RPC proxy and its outage modes, the test wallet, manual mining with snapshots, the status line recorder, the direct chain and mempool reads, the manifest and the status rule, the JSON report and CI.

Depends on the product: the contract addresses and ABIs, the indexer or backend and how it reaches the node, the selectors of the screens, the wallet and sign-in flow (SIWE, embedded wallet, account abstraction), the compliance steps (KYC or KYB provider, allowlist source), and which journey goes first. EVM is an assumption of this demo.

Phases: on a testnet, replay the transaction journeys with funded test wallets. Before mainnet, rehearse the deployment on a local fork (`anvil --fork-url <mainnet RPC>`, impersonation, no key) and run read-only configuration checks against mainnet: chain id, deployed bytecode and addresses, admin role held by the expected multisig.

## Not covered

See `not_covered` in any report: real wallet extensions and mobile wallets; a payment started on another device or with browser storage cleared or refused (the intent comes from the contract, so these cannot debit twice while the order is unpaid, but the Pay lock and the follow-up after a reload live in the browser: there a duplicate can still be sent, and the contract reverts it at the cost of its gas; once the first payment is in a block, a new Pay is a new order); reader authentication; an economic or security audit of the contract; gas spikes on a public network; chain reorganisations; the legal drafting of the terms; load and indexer restarts.

Built by [The AI Pipe](https://theaipipe.com). MIT licence.
