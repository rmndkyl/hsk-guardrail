#!/usr/bin/env python3
"""
Kharon agent — the decision layer for GuardrailVault.

The name is the point: in myth, Kharon ferries the dead and turns away the living who do
not belong. This agent's job is mostly to turn trades away.

What it does, in order:
  1. Reads the pool's own observation ring buffer and slot0 straight off HSK Chain.
  2. Computes the round-trip cost of the trade it is considering, and the widest
     spot-vs-TWAP gap the market is currently offering.
  3. Decides: trade, or refuse — and records the measured numbers either way.
  4. Writes an append-only JSONL ledger, so a judge can replay every decision.

It needs NO private key and NO gas to run in `observe` mode. That is deliberate: the
honest statement about this market is that it does not offer edge, and the agent can say so
without spending anything.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

import requests

# ------------------------------------------------------------------ config

RPC = os.environ.get("HSK_RPC", "https://mainnet.hsk.xyz")
CHAIN_ID = 177
POOL = "0x4ff515761864e7838B0AdB8b5b2B8B7A068905dC"  # WHSK/USDT 0.30%
WHSK = "0xB210D2120d57b758EE163cFfb43e73728c471Cf1"
USDT = "0xF1B50eD67A9e2CC94Ad3c477779E2d4cBfFf9029"
POOL_FEE = 3000
DEC0, DEC1 = 18, 6

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LEDGER = os.path.join(ROOT, "data", "ledger.jsonl")

SEL_SLOT0 = "0x3850c7bd"
SEL_LIQUIDITY = "0x1a686502"
SEL_OBSERVE = "0x883bdbfd"

WINDOWS = (300, 900, 1800, 3600)


# ------------------------------------------------------------------ rpc

def rpc(method: str, params: list, timeout: int = 25, retries: int = 3) -> dict:
    last = None
    for _ in range(retries):
        try:
            r = requests.post(
                RPC,
                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                timeout=timeout,
                headers={"Content-Type": "application/json"},
            )
            if r.status_code == 200 and "json" in r.headers.get("content-type", ""):
                return r.json()
            last = f"HTTP {r.status_code}"
        except Exception as e:  # network hiccup, rate limit, html block
            last = f"{type(e).__name__}"
        time.sleep(2)
    return {"error": {"message": last or "unreachable"}}


def eth_call(to: str, data: str):
    j = rpc("eth_call", [{"to": to, "data": data}, "latest"])
    return j.get("result")


def pad(addr: str) -> str:
    return addr.lower().replace("0x", "").rjust(64, "0")


# ------------------------------------------------------------------ pool math

def read_slot0():
    res = eth_call(POOL, SEL_SLOT0)
    if not res or len(res) < 2 + 64 * 3:
        return None
    b = res[2:]
    sqrt_price = int(b[0:64], 16)
    tick = int.from_bytes(bytes.fromhex(b[64:128]), "big", signed=True)
    return {"sqrtPriceX96": sqrt_price, "tick": tick}


def read_liquidity() -> int:
    res = eth_call(POOL, SEL_LIQUIDITY)
    return int(res, 16) if res and res != "0x" else 0


def tick_to_price(tick: int) -> float:
    """1.0001^tick, scaled from raw token units to human USDT per WHSK."""
    return (1.0001 ** tick) * 10 ** (DEC0 - DEC1)


def twap_at(window: int):
    """TWAP tick over `window` seconds, from the pool's ring buffer."""
    data = (SEL_OBSERVE
            + hex(32)[2:].rjust(64, "0")
            + hex(2)[2:].rjust(64, "0")
            + hex(window)[2:].rjust(64, "0")
            + hex(0)[2:].rjust(64, "0"))
    res = eth_call(POOL, data)
    if not res or res == "0x":
        return None
    b = res[2:]
    words = [b[i * 64:(i + 1) * 64] for i in range(len(b) // 64)]
    # first word = offset to int56[], second = offset to uint160[]
    try:
        off0 = int(words[0], 16) // 32
        ln0 = int(words[off0], 16)
        cum = []
        for i in range(ln0):
            v = int(words[off0 + 1 + i], 16)
            if v >= 1 << 255:
                v -= 1 << 256
            cum.append(v)
        if len(cum) != 2:
            return None
        twap_tick = (cum[1] - cum[0]) / window
        return twap_tick
    except Exception:
        return None


def amount_out_1_for_0(dy: int, L: int, sp: int, fee: int = POOL_FEE) -> int:
    """token1 in -> token0 out (USDT -> WHSK), single tick, fee included."""
    if L == 0 or sp == 0:
        return 0
    dy_net = dy * (1_000_000 - fee) // 1_000_000
    sp_new = sp + (dy_net * (1 << 96)) // L
    inv0 = (L * (1 << 96)) // sp
    inv1 = (L * (1 << 96)) // sp_new
    return max(0, inv0 - inv1)


def amount_out_0_for_1(dx: int, L: int, sp: int, fee: int = POOL_FEE) -> int:
    """token0 in -> token1 out (WHSK -> USDT), single tick, fee included."""
    if L == 0 or sp == 0:
        return 0
    dx_net = dx * (1_000_000 - fee) // 1_000_000
    num = L * sp
    den = L * (1 << 96) + dx_net * sp
    if den == 0:
        return 0
    sp_new = (num * (1 << 96)) // den
    return max(0, (L * (sp - sp_new)) // (1 << 96))


# ------------------------------------------------------------------ the decision

class Market:
    def __init__(self):
        s0 = read_slot0()
        if not s0:
            raise RuntimeError("cannot read slot0 — RPC unreachable or pool changed")
        self.sqrt_price = s0["sqrtPriceX96"]
        self.tick = s0["tick"]
        self.liquidity = read_liquidity()
        self.spot = (self.sqrt_price / 2 ** 96) ** 2 * 10 ** (DEC0 - DEC1)
        self.read_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    def twaps(self):
        out = {}
        for w in WINDOWS:
            t = twap_at(w)
            if t is not None:
                out[w] = {"tick": t, "price": tick_to_price(t)}
        return out

    def round_trip_bps(self, notional_usd: float):
        """Buy then immediately sell. Stays ~2x fee because impact is handed back."""
        amount_in = int(notional_usd * 10 ** DEC1)
        base_out = amount_out_1_for_0(amount_in, self.liquidity, self.sqrt_price)
        if base_out == 0:
            return None, None
        sp_after = self.sqrt_price + (amount_in * (1 << 96)) // self.liquidity
        back = amount_out_0_for_1(base_out, self.liquidity, sp_after)
        if back >= amount_in:
            return 0.0, base_out
        cost_bps = (amount_in - back) / amount_in * 10_000
        return cost_bps, base_out

    def single_leg_cost_bps(self, notional_usd: float):
        """What one execution actually costs: fee + impact. This is the real gate."""
        amount_in = int(notional_usd * 10 ** DEC1)
        base_out = amount_out_1_for_0(amount_in, self.liquidity, self.sqrt_price)
        if base_out == 0:
            return None
        exec_price = notional_usd / (base_out / 10 ** DEC0)
        return (exec_price / self.spot - 1) * 10_000

    def best_gap_bps(self, twaps: dict):
        best = (0.0, None, 0.0)
        for w, t in twaps.items():
            gap = (self.spot / t["price"] - 1) * 10_000
            if abs(gap) > abs(best[0]):
                best = (gap, w, t["price"])
        return best


def evaluate(market: Market, notional_usd: float, claimed_edge_bps: float, min_margin_bps: float):
    twaps = market.twaps()
    gap, gap_window, gap_price = market.best_gap_bps(twaps)
    rt_cost, base_out = market.round_trip_bps(notional_usd)
    leg_cost = market.single_leg_cost_bps(notional_usd)

    reasons = []
    allowed = True

    if rt_cost is None:
        allowed = False
        reasons.append("BAD_POOL_STATE: cannot price this size against current liquidity")

    market_edge = abs(gap)
    required = (rt_cost or 0) + min_margin_bps
    if allowed and market_edge < required:
        allowed = False
        reasons.append(
            f"MARKET_HAS_NO_EDGE: widest spot-vs-TWAP gap is {market_edge:.1f} bp "
            f"(at {gap_window}s), a round trip costs {rt_cost:.2f} bp and policy demands "
            f"{min_margin_bps:.2f} bp of margin on top, so {required:.2f} bp is needed"
        )
    if allowed and claimed_edge_bps > market_edge:
        allowed = False
        reasons.append(
            f"CLAIM_EXCEEDS_MARKET: model claims {claimed_edge_bps:.2f} bp but the market "
            f"only offers {market_edge:.2f} bp"
        )
    if allowed and claimed_edge_bps < required:
        allowed = False
        reasons.append(
            f"EDGE_BELOW_COST: claim {claimed_edge_bps:.2f} bp does not clear {required:.2f} bp"
        )

    return {
        "allowed": allowed,
        "action": "TRADE" if allowed else "REFUSE",
        "reasons": reasons or ["OK: claim fits inside a measured market gap"],
        "notional_usd": notional_usd,
        "claimed_edge_bps": claimed_edge_bps,
        "measured": {
            "spot": market.spot,
            "spot_tick": market.tick,
            "liquidity": market.liquidity,
            "widest_gap_bps": round(market_edge, 2),
            "widest_gap_signed_bps": round(gap, 2),
            "gap_window_seconds": gap_window,
            "round_trip_cost_bps": round(rt_cost, 2) if rt_cost is not None else None,
            "single_leg_cost_bps": round(leg_cost, 2) if leg_cost is not None else None,
            "required_bps": round(required, 2),
            "twaps": {str(w): round(t["price"], 8) for w, t in twaps.items()},
            "base_out": (base_out / 10 ** DEC0) if base_out else None,
        },
        "read_at": market.read_at,
    }


# ------------------------------------------------------------------ output

def record(decision: dict, note: str = ""):
    os.makedirs(os.path.dirname(LEDGER), exist_ok=True)
    row = {**decision, "note": note, "ts": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    with open(LEDGER, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, sort_keys=True) + "\n")
    return row


def main():
    ap = argparse.ArgumentParser(description="Kharon: a trading agent that mostly refuses")
    ap.add_argument("--notional", type=float, default=10.0, help="trade size in USDT")
    ap.add_argument("--claim", type=float, default=0.0, help="edge the model claims, in bps")
    ap.add_argument("--margin", type=float, default=50.0, help="policy margin on top of cost")
    ap.add_argument("--loop", type=int, default=0, help="repeat N times (0 = once)")
    ap.add_argument("--sleep", type=int, default=60, help="seconds between loop iterations")
    ap.add_argument("--json", action="store_true", help="print raw JSON only")
    args = ap.parse_args()

    runs = max(1, args.loop)
    for i in range(runs):
        try:
            market = Market()
            d = evaluate(market, args.notional, args.claim, args.margin)
            record(d, note=f"run {i+1}/{runs}")

            if args.json:
                print(json.dumps(d, indent=2, sort_keys=True))
            else:
                m = d["measured"]
                print("=" * 74)
                print(f"KHARON  {m['spot']:.6f} USDT/WHSK   tick {m['spot_tick']}   {d['read_at']}")
                print("=" * 74)
                print(f"  pool liquidity      : {m['liquidity']:,}")
                print(f"  widest market gap   : {m['widest_gap_bps']:.2f} bp "
                      f"({m['widest_gap_signed_bps']:+.2f}) at {m['gap_window_seconds']}s")
                print(f"  round-trip cost     : {m['round_trip_cost_bps']} bp")
                print(f"  single-leg cost     : {m['single_leg_cost_bps']} bp")
                print(f"  policy requirement  : {m['required_bps']} bp")
                print(f"  agent claim         : {d['claimed_edge_bps']:.1f} bp")
                print()
                print(f"  DECISION: {d['action']}")
                for r in d["reasons"]:
                    print(f"    - {r}")
                print()
        except Exception as e:
            print(f"run {i+1} failed: {type(e).__name__}: {e}", file=sys.stderr)

        if i < runs - 1:
            time.sleep(args.sleep)

    # closing summary across the ledger
    if os.path.exists(LEDGER):
        rows = [json.loads(l) for l in open(LEDGER, encoding="utf-8") if l.strip()]
        if rows:
            trades = sum(1 for r in rows if r["allowed"])
            print(f"ledger: {len(rows)} decisions, {trades} allowed, "
                  f"{len(rows)-trades} refused  -> {LEDGER}")


if __name__ == "__main__":
    main()
