# web3-release-evidence

A release bench for EVM dApps. It drives the interface with Playwright and a test wallet, cuts the RPC on purpose, reads the chain directly, and ends on a status computed from written criteria:

- `Blocked`: a required check failed on a named assertion.
- `Incomplete`: no failure, but a required check was skipped, timed out or crashed. The denominator never shrinks.
- `Demonstrated checks passed`: every required check ran and passed.

Waiting is never a verdict: when something the bench waits for does not happen in time (a screen state, a transaction in the mempool, the indexer catching up), the check ends in error and the run is `Incomplete`. Only a named assertion that fails is a detection.

Every report also says `Client MVP: not assessed`. The bench runs here against **Sample Grants**, a synthetic app built for this demo: a holder grants a reader access to a fictitious dataset, pays the reader 25 `TEST` units, and can revoke the grant. Local chain only, development accounts, no real funds, no private key anywhere in this repository.

Page with the recorded runs: https://theaipipe.com/demos/web3-release-evidence/

## What the recorded runs show

Every check that opens the app runs twice, and both runs are required:

- **Still chain**: the bench mines a block only when the check decides, so the moment between "sent" and "in a block" lasts as long as the check needs.
- **Moving chain**: a block every second while the user acts, plus one just before every page load, reload, click and double click. The blocks are empty: a payment waits in the mempool, known to the node, until the check lets it into a block, as a transaction does on a busy chain. Blocks keep coming while a payment waits, during an outage, between two devices and between two clicks.

C05 does not open the app and runs once: 21 required runs per version.

| App | Status | Failed checks |
| --- | --- | --- |
| `variants/faulty` | Blocked | C08, C09, C10, on both chains |
| `variants/corrected` | Demonstrated checks passed | none |

Three faults are planted in `variants/faulty` (`variants/faults.json`); `variants/corrected` is the same app with the three fixes and nothing else (`variants.diff`). The checks are identical for both. A planted fault counts as detected only when its own critical assertion fails.

| Fault | What happens | Found by (critical assertion) |
| --- | --- | --- |
| D1 | The receipt request fails, the app falls back to the transaction lookup and shows **Confirmed** while the transaction is still in the mempool. | C08 (`never_confirmed_without_receipt`). Receipt requests are cut on the app's path for at least 15 seconds after the payment reaches the mempool, and 5 seconds after the screen settles; the node still answers the bench. Then the bench restores the RPC and mines, and both versions end on Confirmed: only the timed history of the status line, state and visible text, tells them apart. The same rule is an invariant on every check that opens the app, for the whole page (I1 below). |
| D2 | `setAllowlisted` has no `onlyAdmin`. The interface hides it, the contract does not refuse it. | C10 (`ordinary_account_cannot_add`). An allowlisted ordinary account calls the contract directly, to add and to remove; both must revert with `NotAdmin()`, and the admin must still be able to edit. |
| D3 | After "status unknown", the retry button sends a second payment. Nothing remembers the payment in progress: the Pay button stays active, a reload forgets it, each click makes a new order id in the browser, and the contract accepts the same order twice. | C09 (`retry_sends_no_new_payment`). The retry must look up the same transaction, send nothing new, and end on Confirmed. Then five other ways to pay again (Pay during "unknown", reload then Pay, Pay from a second device, double click, Pay again after the wallet broadcast the payment then answered an error), each done while the first payment still waits and again after it landed in a block (the double click once: there is no time between its two clicks). Each must move 25 TEST once, and after the first payment landed a Pay must send nothing at all. A duplicate that the contract reverts is listed in the report and not counted as a debit. Finally, straight on the contract, the paid order sent again must revert with `OrderAlreadyPaid()` and an id that is not the open order with `NotCurrentOrder()`. |

### One order, at most one debit

In the corrected app an order is one payer granting one reader one dataset for one access period. Its id comes from the contract, `orderOf(payer, reader, dataset)`: a hash of those three and of the number of access periods that ended by revocation. It never depends on a block number or on a count of payments, so it is the same on every device, in every browser, after every reload and every wallet error, however many blocks pass, until the order is paid and its grant revoked. The contract keeps the rule itself: `grantAndPay` refuses an order already paid (`OrderAlreadyPaid`) and an id that is not the payer's open order (`NotCurrentOrder`).

The interface writes the order id down before it sends anything, and the transaction once the wallet returns it (`localStorage`): that locks the Pay button while a payment is pending or unknown, and lets the app follow it again after a reload. Before Pay is opened again, the app asks the contract whether the order is paid: once it is, Pay stays closed and the screen says "Already paid" rather than "Something went wrong", even after a wallet error that hid the payment.

What a dApp without a server cannot prevent is in "Not covered": a device that has never seen the first payment, asking while that payment is not yet in a block, sends a duplicate. The contract reverts it, at the cost of its gas, and nothing is debited twice.

### Invariants

- **I1, D1 on the whole page.** In every check that opens the app, in both regimes, nothing says Confirmed too early (`no_confirmed_before_receipt_block`). On the status line, not before the block that holds the receipt of the transaction it names; a status line that names no transaction (Already paid) is judged like the rest of the page. Anywhere else on the page (every other visible text, the rest of the status card included, the tab title, alert and confirm dialogs), not while a transaction of the same account, sent from any tab, is still outside a block. There the bench cannot tell which transaction a sentence means, so it judges the moment: a fact already in a block (a grant listed from the indexer, an order already paid) may say confirmed, and so may a sentence said while nothing of the account waits. The test wallet sends each change to the bench as it happens, on every tab and across reloads; the bench notes the time of each block just before mining it. A sentence that says the opposite or the future ("not confirmed", "Sent: it will be confirmed once it is in a block") is not a claim.
- **I2, D3: one order, at most one debit**, in each of the eleven pay-again situations C09 runs: blocks passing while the order is unpaid, a second device opened 40 days later on the chain clock once the first payment landed, reloads, a double click and wallet errors. C09 counts the TEST that left the payer in each of them, and lists every payment transaction with its block and status.
- **I3, waits.** Every appearance the bench expects (a state, a button, a banner, the indexer) is awaited until a deadline, and a passed deadline makes the run Incomplete, never Blocked: a retry button shown 400 ms late, or a banner 500 ms late, passes. The only fixed windows are observation windows: a payment waits 15 s in the mempool before its block in C07 and C08, the revocation 3 s before its block in C11, and a window of 0.5 to 1 s follows a click. They only give a wrong claim or a wrong send time to show; what follows them checks that something did not happen. Two locks are read at a moment rather than awaited, because a lock that comes late is already the fault: the Pay button before the terms are signed (C04), and while the screen says unknown (C09).

Each rule is turned false on purpose by a mutant of the app (`bench/mutants.py`, report in `reports/mutants.json`), including the corrected app of commit e5008da, which debited twice once a block had passed (`bench/fixtures/e5008da`), a D1 fault that only the moving chain catches (`confirm_on_next_block`), and an order id that changes with the day (`x3_d3_order_per_day`), which only the 40 days between the two devices catch. These checks catch the mutants listed in that report. They are tests, not a proof: a fault they were not written for can pass.

C11 checks revocation with the reader's identity simulated: the API trusts the address declared in the `x-reader-address` header, and the demo has no reader sign-in. It shows that the API applies the grant state to that identity once the revocation is mined and indexed, not that the caller owns the address.

Reports: `reports/faulty.json`, `reports/corrected.json`. Ten independent runs per variant: `reports/stability-*.json`. A run with one check removed: `reports/corrected-skip-C08.json`, and a run on the still chain only: `reports/corrected-still-only.json` (both `Incomplete`).

## How it works

```
interface (Playwright, test wallet) ─┐
indexer + API (app/server.mjs) ──────┼─> RPC proxy (cuts chosen methods) ─> anvil (manual mining)
                                     │                                        ^
bench (bench/checks.py) ─────────────┴──── reads the chain directly ──────────┘
```

- `anvil --no-mining`: the bench mines with `evm_mine`, so the moment between "sent" and "in a block" is observable. Each check starts from the same `evm_snapshot`. On the moving chain, `BlockProducer` (`bench/harness.py`) mines an empty block every second and before every gesture: the block gas limit is lowered below the cost of any transaction for that one block, then restored, so waiting payments stay in the mempool. One lock serialises every request to the node, the bench's and the app's, so nothing reaches the node while the limit is low.
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
python bench/run.py --variant corrected --skip C08 --out reports/corrected-skip-C08.json        # Incomplete, exit code 3
python bench/run.py --variant corrected --regime still --out reports/corrected-still-only.json  # Incomplete, exit code 3
python bench/run.py --variant corrected --skip C09/moving # one regime of one check left out: Incomplete
python bench/run.py --variant corrected --repeat 10        # writes reports/stability-corrected.json
python bench/run.py --variant faulty --expect Blocked --expect-detected D1,D2,D3
    # exit 0 only if the status is Blocked AND exactly D1, D2 and D3 were detected on their critical
    # assertions, the failed checks are exactly C08, C09 and C10, and no check errored or was left out
python bench/run.py --variant corrected --expect "Demonstrated checks passed" --expect-detected none
python bench/mutants.py --jobs 3                          # every mutant, writes reports/mutants.json; exit 0 only if each ended as expected
python bench/mutants.py n3_d3_order_per_block ok1_retry_after_400ms   # some of them
python bench/mutants.py --shard 2/3 --jobs 2              # one third of them, as CI does
```

One run of both regimes takes about two and a half minutes, measured on an Apple Silicon laptop: 146.2 s for `reports/corrected.json` and 166.1 s for `reports/faulty.json`, run side by side; 143.2 to 166.7 s over the 20 runs in `reports/stability-*.json` (the two series side by side); the still chain alone takes 67.8 s (`reports/corrected-still-only.json`). C07 and C08 each hold a payment 15 s in each regime. `bench/mutants.py --jobs 2` took 48 min 55 s for the 34 mutants on the same laptop, while other heavy jobs ran on it. CI runs both variants on every push (`.github/workflows/release-evidence.yml`) with the two `--expect` commands above, and every mutant in three shards with `bench/mutants.py --shard`.

## Pointing it at another dApp

Ready in the bench: the RPC proxy and its outage modes, the test wallet, manual mining with snapshots, the status line recorder, the direct chain and mempool reads, the manifest and the status rule, the JSON report and CI.

Depends on the product: the contract addresses and ABIs, the indexer or backend and how it reaches the node, the selectors of the screens, the wallet and sign-in flow (SIWE, embedded wallet, account abstraction), the compliance steps (KYC or KYB provider, allowlist source), and which journey goes first. EVM is an assumption of this demo.

Phases: on a testnet, replay the transaction journeys with funded test wallets. Before mainnet, rehearse the deployment on a local fork (`anvil --fork-url <mainnet RPC>`, impersonation, no key) and run read-only configuration checks against mainnet: chain id, deployed bytecode and addresses, admin role held by the expected multisig.

## Not covered

See `not_covered` in any report: real wallet extensions and mobile wallets; notifications outside the page (browser or system notifications, sounds, the wallet's own popups; I1 covers the status line, the rest of the status card, the rest of the page, the tab title and alert or confirm dialogs); a duplicate sent while the first payment still waits, from a device that has never seen it (the order id comes from the contract, so the contract refuses the second debit, but a dApp without a server cannot stop that device from sending the transaction, which reverts at the cost of its gas); reader authentication; an economic or security audit of the contract; real fee markets, replaced or cancelled transactions (on the moving chain a payment waits while empty blocks pass, as an outbid one would); chain reorganisations; the legal drafting of the terms; load and indexer restarts.

Built by [The AI Pipe](https://theaipipe.com). MIT licence.
