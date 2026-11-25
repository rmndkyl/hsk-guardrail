// Kharon on-chain reader — a direct port of agent/kharon.py so a deployed page
// computes exactly what the local agent computes. No server: the HSK RPC answers
// CORS preflight with allow-headers:*, verified before this was written.
//
// Everything is BigInt. The Python original stays in integers for the pool math and
// only converts to float at the display edge; doing the same here is what keeps the
// two implementations agreeing to the last basis point.

export const RPC = "https://mainnet.hsk.xyz";
export const POOL = "0x4ff515761864e7838B0AdB8b5b2B8B7A068905dC";
export const POOL_FEE = 3000;
export const DEC0 = 18;   // WHSK
export const DEC1 = 6;    // USDT
export const WINDOWS = [300, 900, 1800, 3600];

const SEL_SLOT0     = "0x3850c7bd";
const SEL_LIQUIDITY = "0x1a686502";
const SEL_OBSERVE   = "0x883bdbfd";
const Q96 = 1n << 96n;

async function rpc(method, params, tries = 3) {
  let lastErr;
  for (let i = 0; i < tries; i++) {
    try {
      const r = await fetch(RPC, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ jsonrpc: "2.0", id: 1, method, params }),
      });
      const j = await r.json();
      if (j.error) throw new Error(JSON.stringify(j.error).slice(0, 160));
      return j.result;
    } catch (e) {
      lastErr = e;
      await new Promise((res) => setTimeout(res, 400 * (i + 1)));
    }
  }
  throw lastErr;
}

async function ethCall(to, data) {
  return rpc("eth_call", [{ to, data }, "latest"]);
}

function word(hex, i) {
  return hex.slice(2 + i * 64, 2 + (i + 1) * 64);
}

function toSigned(wordHex) {
  let v = BigInt("0x" + wordHex);
  if (v >= 1n << 255n) v -= 1n << 256n;
  return v;
}

export async function readSlot0() {
  const res = await ethCall(POOL, SEL_SLOT0);
  if (!res || res.length < 2 + 64 * 3) return null;
  const sqrt = BigInt("0x" + word(res, 0));
  let tick = Number(toSigned(word(res, 1)));
  return { sqrt, tick };
}

export async function readLiquidity() {
  const res = await ethCall(POOL, SEL_LIQUIDITY);
  return res && res !== "0x" ? BigInt(res) : 0n;
}

export function tickToPrice(tick) {
  return Math.pow(1.0001, tick) * Math.pow(10, DEC0 - DEC1);
}

export async function twapAt(window) {
  const data =
    SEL_OBSERVE +
    (32).toString(16).padStart(64, "0") +
    (2).toString(16).padStart(64, "0") +
    window.toString(16).padStart(64, "0") +
    (0).toString(16).padStart(64, "0");
  const res = await ethCall(POOL, data);
  if (!res || res === "0x") return null;
  try {
    const b = res.slice(2);
    const words = [];
    for (let i = 0; i < Math.floor(b.length / 64); i++) words.push(b.slice(i * 64, (i + 1) * 64));
    const off0 = Number(BigInt("0x" + words[0]) / 32n);
    const len0 = Number(BigInt("0x" + words[off0]));
    if (len0 !== 2) return null;
    // tickCumulatives are int56 — up to ~3.6e16 — so they must be subtracted as
    // BigInt. Converting to Number first loses the low bits and shifts the TWAP by
    // about 4e-9, which is small but is still a different number from the agent's.
    const cum0 = toSigned(words[off0 + 1]);
    const cum1 = toSigned(words[off0 + 2]);
    const delta = cum1 - cum0;
    return Number(delta) / window;
  } catch {
    return null;
  }
}

// token1 in -> token0 out (USDT -> WHSK), single tick, fee included
export function amountOut1For0(dy, L, sp, fee = POOL_FEE) {
  if (L === 0n || sp === 0n) return 0n;
  const dyNet = (dy * BigInt(1_000_000 - fee)) / 1_000_000n;
  const spNew = sp + (dyNet * Q96) / L;
  const inv0 = (L * Q96) / sp;
  const inv1 = (L * Q96) / spNew;
  return inv0 > inv1 ? inv0 - inv1 : 0n;
}

// token0 in -> token1 out (WHSK -> USDT), single tick, fee included
export function amountOut0For1(dx, L, sp, fee = POOL_FEE) {
  if (L === 0n || sp === 0n) return 0n;
  const dxNet = (dx * BigInt(1_000_000 - fee)) / 1_000_000n;
  const num = L * sp;
  const den = L * Q96 + dxNet * sp;
  if (den === 0n) return 0n;
  const spNew = (num * Q96) / den;
  return sp > spNew ? (L * (sp - spNew)) / Q96 : 0n;
}

const MAX = (1n << 256n) - 1n;

export class Market {
  constructor(slot0, liquidity) {
    this.sqrt = slot0.sqrt;
    this.tick = slot0.tick;
    this.liquidity = liquidity;
    // Python: (sqrt_price / 2**96) ** 2 * 10 ** (DEC0 - DEC1)
    const s = Number(this.sqrt) / Number(Q96);
    this.spot = s * s * Math.pow(10, DEC0 - DEC1);
    this.readAt = new Date().toISOString().replace(/\.\d+Z$/, "+00:00");
  }

  // Buy then immediately sell. ~2x fee, because impact is handed back.
  roundTripBps(notionalUsd) {
    const amountIn = BigInt(Math.round(notionalUsd * 10 ** DEC1));
    const baseOut = amountOut1For0(amountIn, this.liquidity, this.sqrt);
    if (baseOut === 0n) return { costBps: null, baseOut: null };
    const spAfter = this.sqrt + (amountIn * Q96) / this.liquidity;
    // Same validity bound as the contract: past a 2x simulated move the single-tick
    // model is fiction, and reporting a cheap price there is how a guardrail says yes.
    if (spAfter > this.sqrt * 2n) return { costBps: null, baseOut: null };
    const back = amountOut0For1(baseOut, this.liquidity, spAfter);
    if (back >= amountIn) return { costBps: null, baseOut: null };
    const loss = Number(amountIn - back);
    return { costBps: (loss / Number(amountIn)) * 10_000, baseOut };
  }

  singleLegCostBps(notionalUsd) {
    const amountIn = BigInt(Math.round(notionalUsd * 10 ** DEC1));
    const baseOut = amountOut1For0(amountIn, this.liquidity, this.sqrt);
    if (baseOut === 0n) return null;
    const execPrice = notionalUsd / (Number(baseOut) / 10 ** DEC0);
    return (execPrice / this.spot - 1) * 10_000;
  }

  bestGapBps(twaps) {
    let best = { gap: 0, window: null, price: 0 };
    for (const [w, t] of Object.entries(twaps)) {
      const gap = (this.spot / t.price - 1) * 10_000;
      if (Math.abs(gap) > Math.abs(best.gap)) best = { gap, window: Number(w), price: t.price };
    }
    return best;
  }
}

export async function blockNumber() {
  const hex = await rpc("eth_blockNumber", []);
  return Number(BigInt(hex));
}

export async function readMarket() {
  const [slot0, liquidity, block] = await Promise.all([
    readSlot0(), readLiquidity(), blockNumber(),
  ]);
  if (!slot0) throw new Error("cannot read slot0 — RPC unreachable or pool changed");
  const m = new Market(slot0, liquidity);
  const twaps = {};
  for (const w of WINDOWS) {
    const t = await twapAt(w);
    if (t !== null) twaps[w] = { tick: t, price: tickToPrice(t) };
  }
  m.block = block;
  return { market: m, twaps, block };
}

// Port of evaluate() in agent/kharon.py. Same checks, same order, same wording.
export function evaluate(market, twaps, notionalUsd, claimedEdgeBps, minMarginBps) {
  const { gap, window } = market.bestGapBps(twaps);
  const rt = market.roundTripBps(notionalUsd);
  const legCost = market.singleLegCostBps(notionalUsd);
  const reasons = [];
  let allowed = true;

  if (rt.costBps === null) {
    allowed = false;
    reasons.push("BAD_POOL_STATE: cannot price this size against current liquidity");
  }

  const marketEdge = Math.abs(gap);
  const required = (rt.costBps || 0) + minMarginBps;

  if (allowed && marketEdge < required) {
    allowed = false;
    reasons.push(
      `MARKET_HAS_NO_EDGE: widest spot-vs-TWAP gap is ${marketEdge.toFixed(1)} bp ` +
      `(at ${window}s), a round trip costs ${rt.costBps.toFixed(2)} bp and policy demands ` +
      `${minMarginBps.toFixed(2)} bp of margin on top, so ${required.toFixed(2)} bp is needed`
    );
  }
  if (allowed && claimedEdgeBps > marketEdge) {
    allowed = false;
    reasons.push(
      `CLAIM_EXCEEDS_MARKET: model claims ${claimedEdgeBps.toFixed(2)} bp but the market ` +
      `only offers ${marketEdge.toFixed(2)} bp`
    );
  }
  if (allowed && claimedEdgeBps < required) {
    allowed = false;
    reasons.push(
      `EDGE_BELOW_COST: claim ${claimedEdgeBps.toFixed(2)} bp does not clear ${required.toFixed(2)} bp`
    );
  }

  return {
    allowed,
    action: allowed ? "TRADE" : "REFUSE",
    reasons: reasons.length ? reasons : ["OK: claim fits inside a measured market gap"],
    notional_usd: notionalUsd,
    claimed_edge_bps: claimedEdgeBps,
    measured: {
      spot: market.spot,
      spot_tick: market.tick,
      liquidity: market.liquidity.toString(),
      widest_gap_bps: Number(marketEdge.toFixed(2)),
      widest_gap_signed_bps: Number(gap.toFixed(2)),
      gap_window_seconds: window,
      round_trip_cost_bps: rt.costBps === null ? null : Number(rt.costBps.toFixed(2)),
      single_leg_cost_bps: legCost === null ? null : Number(legCost.toFixed(2)),
      required_bps: Number(required.toFixed(2)),
      twaps: Object.fromEntries(Object.entries(twaps).map(([w, t]) => [w, t.price])),
      base_out: rt.baseOut === null ? null : Number(rt.baseOut) / 10 ** DEC0,
    },
    read_at: market.readAt,
  };
}
