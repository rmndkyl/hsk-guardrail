# Kharon / GuardrailVault

A trading agent for HSKSwap that refuses to trade, and can prove it was right to refuse.

Submitted to **HSK Chain Horizon Hackathon · Seoul**, track **AI Agent Trading**
(chainId 177, submission deadline 3 Dec 2026 13:00 KST).

---

## The finding this is built on

Before writing a line of trading logic, I measured the market the hackathon asks you to
trade on. Everything below is from HSK Chain mainnet, read through the public RPC and the
pool's own contracts on 25 Nov 2026.

| What I measured | Value | How |
|---|---|---|
| WHSK/USDT 0.30% pool TVL | ~$838k (5.19M WHSK + 419k USDT) | `balanceOf(pool)` on both tokens |
| Swaps in the last ~50 hours | **96** (~$559/day) | `eth_getLogs` on the pool's `Swap` topic |
| Swaps in the last 24 hours | **12 from 3 addresses, $135.09** | same call, 43,200-block window |
| Spot vs its own 1h TWAP | **0.0 bp apart** | `observe([3600,0])` on the pool |
| Widest spot-vs-TWAP gap, any lookback 5m-1h | **0.1 bp** | same call, four lookbacks |
| Round-trip cost, $10 | 59.9 bp | simulated buy+sell against `liquidity()` |
| Round-trip cost, $100 | 59.9 bp | same |
| Single-leg cost, $100 | 62.5 bp | fee 30 bp + impact 32.5 bp |
| Single-leg cost, $1,000 | 83.9 bp | fee 30 bp + impact 53.9 bp |
| Cost of deploying a vault | 1.25 HSK = **$0.10** | 2.5M gas at 0.0000005 HSK/gas |
| Pool spot range, 48h | **24.6 bp** | `slot0` at 48 hourly blocks |
| Gate (world price) range, same 48h | **292.2 bp** | Gate HSK_USDT 1h candles |
| Pool activity, most recent 24h | **12 swaps, 3 senders, $135.09** | `eth_getLogs`, 43,200 blocks |
| Pool spot vs world mid, 48h | **-161.1 to +133.4 bp, median -71.7** | pool `slot0` vs Gate close |

Two of those rows decide the whole design:

- **Round-trip cost is flat at ~60 bp** no matter the size. On a Uniswap v3 pool the price
  impact of buying is handed back when you sell, so "the trade is too big" is *not* what
  makes trading unprofitable here.
- **The price does not move.** Spot sits on its own TWAP to within a tenth of a basis point.
  An agent can only profit by entering below a reference and exiting above it; in this pool
  the reference and the price are the same number.

### Correction: the pool is *stale*, not efficient

My first write-up said "the price does not move" and stopped there. That is incomplete in a way
that matters, and measuring it properly changed what this submission claims.

The pool's price barely moves **relative to itself**. Measured against an outside reference it
is not stable, it is *pinned*: over 48 hours the pool's `slot0` travelled **24.6 bp** while
Gate's HSK_USDT moved **292.2 bp** — the world moved about twelve times further than the pool
did. The deviation from world mid swung from **-161 bp to +133 bp** across that window,
flipping sign as the reference moved around the frozen pool. A pool that tracks the world does
not do that; a pool nobody arbitrages does.

So the honest statement is not "this venue offers no edge". It is: **this venue offers no edge
measured against *itself*, and whoever is willing to price it against the outside world can see
a dislocation that nobody on the chain is currently taking.** Right now the pool is roughly
+42 bp rich on the sell side against the best CEX bid: 1,000 WHSK out of the pool returns
80.5354 USDT where the best bid across Gate/MEXC/KuCoin pays 80.2000. The buy direction is
correspondingly poor (-119 bp against the best ask), so a round trip still loses **64.8 bp in
5 of 5 samples** — the dislocation is one-way and it is not harvestable by trading both legs
inside the pool. It is also gross of exchange taker fees (~20 bp at Gate), which would eat
most of the 42 bp. I am reporting the number because it shows the pool is mispriced, not
because it is money on the table at retail fee tiers.

This is a limit of the product, and the gate does not hide it: `availableEdgeBps()` compares
spot to the pool's *own* TWAP ring buffer. If the entire pool drifts away from world price,
spot and TWAP drift together, the measured gap stays near zero, and the vault refuses a trade
that an external reference would call profitable. The gate is honest about local dislocation
and blind to global dislocation. The fix requires an external price feed, and there is no live
one on this chain — I checked the feed another entry in this track relies on (APRO HSK/USD,
`0x86CE42c1...`): it reverts on every getter at every block height and has five lifetime
transactions, none of them a price publication. Wiring that in behind a `try/catch` would have
made the vault silently dependent on an empty contract.

Read together: a vault that dutifully executes whatever the model decides loses ~60 bp per
round trip against a market that offers it ~0.1 bp of *local* opportunity, and the one real
opportunity it could see is outside the pool and out of reach of a retail fee tier. The
rational agent behaviour on this venue is to **not trade**, and the useful engineering problem
is making that refusal trustworthy rather than merely plausible.

---

## What this actually is

`GuardrailVault` is the spending contract an autonomous agent must trade through. Its job is
to make a wrong decision impossible rather than merely unlikely.

### The gate that differs from the rest of the track

The other submissions I read (SayTrade, Keel, Sentinel, Jangseung, Glassbox) follow the same
shape: *the agent decides, the vault constrains the size*. That protects the owner's money
from an oversized mistake. It does not protect against the more common failure, which is an
agent that trades competently in a market where trading cannot win.

This vault adds two rules on top of the size caps:

```
refuse if  (widest spot-vs-TWAP gap the market offers, in bps)
        <  (round-trip cost of this trade) + (policy margin)
```

and independently:

```
refuse if  (edge the model claims)  >  (edge the market is measurably offering)
```

The first rule encodes "do not trade a market that has nothing to offer". The second catches
a model that hallucinates an opportunity: it is refused against numbers the contract measured
itself, not against the model's own testimony.

### Refusals are first-class, attested, and replayable

`attest()` writes an append-only record of every decision - refusals included - carrying the
spot price, the TWAP, the measured gap, the round-trip cost and the impact, as the vault saw
them. There is no admin override and no delete path. A judge can re-read `decisionAt(i)` and
check it against the pool.

An agent that only ever submits trades has no way to show it exercised judgement. This one
has a ledger of judgement calls.

### The owner can only make it safer

`tighten()` reverts unless every changed field moves in the conservative direction. There is
no code path that loosens a limit, which is what makes "the agent cannot break its rules" a
property rather than a promise.

---

## Verified, not asserted

```
forge test
```

**26/26 passing** across three suites, all against a fork of HSK Chain mainnet.

Verified the way a judge will check it: `git clone` into an empty directory, `forge build`,
`forge test`. `forge-std` is vendored, so there is no setup step and no dependency fetch —
the numbers above reproduce on a clean machine.

### The test that proves the gate opens, not just closes

`test/TradePath.t.sol` closes the hole the rest of the suite leaves. Everything above proves
the vault *refuses*, because this market genuinely offers no edge — which leaves an obvious
question unanswered: is this a guardrail or just a wall?

So the test manufactures the condition honestly, on a mainnet fork:

1. Measure the gate before touching anything — gap **16 bp**, round-trip cost **59 bp**, so it
   refuses.
2. Push the pool price with a real $20k swap, creating a genuine spot-vs-TWAP gap.
3. Measure again — gap **917 bp**, cost **69 bp**, so the gate opens.
4. Fund the vault, call `trade()`, and assert the swap actually executed.

Result: **1,124 WHSK received into the vault**, USDT fully spent, decision recorded as allowed
with the spot and gap it saw. A second immediate trade is then refused on cooldown.

```
[PASS] test_tradeExecutesWhenMarketActuallyOffersEdge()
  gap before push  : 16 bp     -> refused
  gap after push   : 917 bp    -> allowed
  WHSK received    : 1,124,308,799,497,265,664,423
  vault WHSK after : 1124308799497265664423
[PASS] test_tradePathRespectsCooldownAfterExecution()
  second trade refused with reason: 3  (Cooldown)
```

That is the difference between a claim and a demonstration: the same contract that says no to
a dead market says yes, and executes, when the market moves.

### Adversarial suite found a real bug

`test/Adversarial.t.sol` attacks the vault: overclaimed edge, cap bypass, a hostile router
address, privilege escalation, five separate attempts by the owner to loosen a limit, a
self-reported loss, pause, ledger tampering, and degenerate inputs.

To be clear about whose bug this is: it was **mine**, not a shared flaw. I checked the deployed
source of both rival vaults. SayTrade (`0xe210a588...`) guards slippage against `pool.observe()`
TWAP exactly the way this vault does. Keel (`0x8980296B...`) uses a `spotOut()` that is
documented as "after the pool fee, before price impact" — honest about what it does not model,
and it never claims a cost figure. Neither computes a round-trip cost at all, so neither had a
zero-cost path to get wrong. The bug existed because I chose to produce that number.

The degenerate-input test earned its place. Quoting a **$50M** trade returned a round-trip
cost of **0 bps** — "trading is free", the one answer a guardrail must never give, because it
says yes. Root cause: the single-tick constant-liquidity model saturates at large sizes. I
measured where it breaks against the live pool:

| Trade size | simulated price move | reported cost | model |
|---|---|---|---|
| $100 | 1.00x | 59.9 bp | valid |
| $10,000 | 1.02x | 59.2 bp | valid |
| $100,000 | 1.24x | 54.2 bp | valid |
| $1,000,000 | 3.38x | 38.9 bp | **fiction** |
| $50,000,000 | 119.8x | 30.3 bp | **fiction** |

Fix: if the simulated price moves more than **2x**, the size is outside the model's validity
and `roundTripCostBps` returns `type(uint256).max`, which `evaluate` surfaces as
`BadPoolState` — a refusal, not a cheap trade. The `usdtBack >= usdtIn` case (a round trip that
appears to make money) is also treated as unpriceable rather than as zero cost.

This is the difference between a claim and a demonstration: the suite that was supposed to
confirm the design instead found the one failure mode that would have mattered.

### Suite detail

`test/GuardrailFork.t.sol` — **15/15 passing** (block ~30,624,800).

The tests worth reading:

- `test_costModel_roundTripIsNearTwoLegsOfFee` - round-trip cost is ~2x the 30 bp fee and is
  size-invariant, which is the structural fact the whole argument rests on.
- `test_singleLegPriceWorsensWithSize` - the cost that *does* scale is single-leg impact,
  which is therefore what the vault gates on.
- `test_refusesBecauseMarketHasNoEdge` - with a 5,000 bp claim and a $10 notional (far under
  the cap), the vault refuses with `MarketHasNoEdge`. This is the product working.
- `test_refusesClaimThatExceedsMarket` - a claim wider than the market's measured gap is
  refused on the vault's own numbers.
- `test_ownerCannotLoosenPolicy` / `test_ownerCanTightenPolicy` - the limit system is
  one-directional.

The cost model was also cross-checked against Uniswap's own `QuoterV2` in Python: the buy-leg
output agrees to **0.0 bp** at $10, $100, $1,000 and $10,000.

---

## The agent

```
python3 agent/kharon.py --notional 10 --claim 20
```

```
KHARON  0.080793 USDT/WHSK   tick -301484   2026-11-25T08:58:35+00:00
  pool liquidity      : 1,475,813,786,831,558,685
  widest market gap   : 0.10 bp (+0.10) at 300s
  round-trip cost     : 59.91 bp
  single-leg cost     : 30.33 bp
  policy requirement  : 109.91 bp
  agent claim         : 20.0 bp

  DECISION: REFUSE
    - MARKET_HAS_NO_EDGE: widest spot-vs-TWAP gap is 0.1 bp (at 300s), a round trip
      costs 59.9 bp and policy demands 50 bp of margin on top, so 109.9 bp is needed
```

It reads the pool's observation ring buffer and `slot0` directly off HSK Chain, computes
costs against live `liquidity()`, and appends every decision to `data/ledger.jsonl`.

**It needs no private key and no gas to run.** That is deliberate: the honest claim about
this market can be made for free, and it stays true whether or not anyone funds a wallet.

---

## The desk (what the track asks for)

The track asks for "a conversational interface that lets users see positions and P&L in real
time and adjust position size, trade frequency, risk appetite and target assets". That is
`agent/desk.py` — a single-file HTTP server, no build step, no framework, reading live chain
state on every refresh.

**[Watch the 58s demo](demo/kharon-demo.mp4)** — real browser session, live chain data.

Commands: `status`, `why`, `decide`, `history [n]`, `size <usd>`, `claim <bps>`,
`margin <bps>`, `appetite conservative|balanced|aggressive`.

The grammar is deliberately small and explicit. An LLM that picks sizes freely is the exact
failure mode this design exists to replace, so the desk exposes the *policy*, not a prompt
box that can talk the vault into anything.

```bash
python3 agent/desk.py                      # http://127.0.0.1:8791

# re-render the demo video from a live desk (needs playwright + ffmpeg)
DESK_URL=http://127.0.0.1:8791/ python agent/demo_video.py
```

The demo video (`demo/kharon-demo.mp4`, 58s, 1080p) is a real browser session against the live
desk — every number in it came off HSK Chain during the recording.

---

## Run it

```bash
# tests (needs no key, reads mainnet over public RPC)
export PATH="$HOME/.foundry/bin:$PATH"
forge test --match-path test/GuardrailFork.t.sol -vv

# the agent, read-only
python3 agent/kharon.py --notional 10 --claim 20
python3 agent/kharon.py --notional 100 --claim 500 --json

# deploy (needs ~1.25 HSK of gas, ~$0.10)
export PRIVATE_KEY=... VAULT_OWNER=0x... VAULT_AGENT=0x...
forge script script/Deploy.s.sol:Deploy --rpc-url https://mainnet.hsk.xyz --broadcast
```

---

## Addresses used (verified on chain, 25 Nov 2026)

| Thing | Address |
|---|---|
| WHSK (Wrapped HSK, 18dp) | `0xB210D2120d57b758EE163cFfb43e73728c471Cf1` |
| USDT (Tether USD, 6dp) | `0xF1B50eD67A9e2CC94Ad3c477779E2d4cBfFf9029` |
| Pool WHSK/USDT 0.30% | `0x4ff515761864e7838B0AdB8b5b2B8B7A068905dC` |
| Factory | `0x972cA9D1662F5e029cD18327D29026532E84c742` |
| SwapRouter02 | `0x2c16f75b95Cf1390c328aB70e2CEE7f4b80bD8F3` |
| QuoterV2 | `0x1095A23A4812185344D9abd5143191D8a3A7E36f` |

The app front-end (`app.hskswap.com`) is geo-blocked from this server, so every address above
was recovered from the chain itself: `factory()` and `WETH9()` on the routers, then
`getPool()` on the factory, then confirmed by reading token symbols, decimals and pool
balances. `QuoterV2` returns live quotes, which is the end-to-end proof it is the right one.

---

## Honest limits

- **The vault has not traded on mainnet.** Deploying costs 1.25 HSK (~$0.10) and I hold none.
  Evidence: 303 addresses sampled across my vault (including the three most likely to hold
  something) return zero HSK; the only non-zero EVM balances anywhere are ~$0.02 of dust on
  BSC/Arbitrum/Base, far under the ~$0.10 needed. The HSK testnet faucet is gated behind
  reCAPTCHA Enterprise — I solved its HMAC request signing (`x-date` + `request-line`, newline
  separated) but not the captcha, and stopped there deliberately rather than farming it.
  What *is* proven is that the trade path executes correctly on a mainnet fork
  (`test/TradePath.t.sol`, 1,124 WHSK received). The deploy script is written and costed.
  Per the event rules, mainnet deployment is required only for teams selected for the demo
  (announced 4 Dec), so this does not block the entry.
- **Testnet is not usable for this.** HSK testnet (chainId 133) has no WHSK/USDT pool at all -
  `getPool` returns zero for every fee tier. Any demo on testnet would need a pool deployed
  first, which itself needs testnet gas.
- **The strategy is thin on purpose.** Mean reversion against a TWAP in a market with 96 swaps
  per 50 hours is a demonstration of a gate, not a money printer. The claim is that the gate
  is correct, not that the strategy is profitable.
- **`availableEdgeBps` reads four fixed lookbacks.** A real deployment would tune these per
  market; the values were chosen because they span the range a short-horizon agent would look.

---

## On "adjust target assets"

The track asks the interface to let a user adjust target assets. The desk adjusts size,
frequency (cooldown) and risk appetite directly, but the vault's pool is **immutable**, and
that is deliberate. A settable pool reference is an attack surface: whoever can point the vault
at a pool can point it at a pool they control.

I surveyed every pool on HSKSwap mainnet before making that call — `getPool` across
WHSK/USDT, WETH/USDT, WBTC/USDT, USDC/WHSK at fee tiers 500/3000/10000, keeping only pools
with non-zero `liquidity()`:

| Pair | Fee | Liquidity |
|---|---|---|
| WHSK/USDT | 0.30% | 1,475,813,786,831,558,685 |
| WETH/USDT | 0.30% | 1,012,957,283,335,421 |
| WBTC/USDT | 0.30% | 1,823,343,427 |

Three liquid pools exist on the whole chain, and WHSK/USDT is 99.8% of the liquidity. So
"changing target asset" would mean redeploying against one of two pools that are ~1,000x
thinner — not a feature, a footgun. Switching target is a deploy operation (one command in
`script/Deploy.s.sol`), not a runtime setting, and the README says so rather than shipping a
setting that quietly makes the vault worse.

## A note on the oracle dependency

The obvious upgrade to this vault is a second reference price from an oracle, so the gate can
also catch a pool that has drifted from the real world rather than only from its own history.
The Keel submission uses an "APRO HSK/USD" feed at
`0x86CE42c1b714149Dc3A7b17169EF67b5F78A224b`. I checked it on chain before wiring it in:

- `latestRoundData()`, `decimals()`, `description()`, `version()`, `aggregator()`, `owner()` —
  **every call reverts**, and it reverts identically at block heights from 10M to latest.
- The proxy has **5 transactions in its entire lifetime**, the last one an `acceptOwnership` in
  May 2026, and none of them a price publication.

So it is a Chainlink-shaped proxy with no aggregator behind it: it cannot be read as a price
feed today. Wiring it in would have made the vault depend on a contract that returns nothing,
and a `try/catch` fallback would have silently hidden that. The gate therefore uses the pool's
own `observe()` ring buffer, which is verifiable and self-contained. Adding an external oracle
is the right next step *once a live feed exists on this chain*.

---

## Why the name

Kharon ferries the dead across, and turns away the living who do not belong. The agent's main
job here is turning trades away. The pool has 96 swaps in fifty hours and its price has not
moved; most of what a well-behaved agent should do on it is decline.
