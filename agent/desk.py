#!/usr/bin/env python3
"""
Kharon desk — a conversational interface over the GuardrailVault.

The hackathon track asks for a conversational interface that lets a user see positions and
P&L in real time and adjust position size, trade frequency, risk appetite and target assets.
This serves exactly that, against live HSK Chain state, with no build step and no framework.

Endpoints
    GET  /              the desk (single HTML page)
    GET  /api/state     live market + vault state + decision history
    POST /api/command   {"text": "..."}  natural-language style command

Commands understood (deliberately a small, explicit grammar — an LLM that picks sizes is
the thing this whole design exists to replace):
    status                     show market, limits, last decision
    why                        explain the most recent decision in full
    size <usd>                 proposed trade size
    claim <bps>                the edge you believe you have
    margin <bps>               required margin above cost
    appetite conservative|balanced|aggressive
                               sets size + margin together
    decide                     run an evaluation now and record it
    history [n]                last n decisions
"""
from __future__ import annotations

import json
import os
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kharon import Market, evaluate, record, LEDGER  # noqa: E402

HOST = os.environ.get("DESK_HOST", "127.0.0.1")
PORT = int(os.environ.get("DESK_PORT", "8791"))

# ------------------------------------------------------------------ mutable desk state
# Risk appetite presets. The point of naming them is that they only ever move between
# pre-approved shapes; there is no free-text "trade $5,000" that bypasses the vault.
APPETITES = {
    "conservative": {"notional": 10.0, "margin": 150.0},
    "balanced": {"notional": 50.0, "margin": 75.0},
    "aggressive": {"notional": 100.0, "margin": 50.0},
}

STATE = {
    "notional": 10.0,
    "claim": 0.0,
    "margin": 50.0,
    "appetite": "aggressive",
}
LOCK = threading.Lock()


def ledger_rows(limit: int | None = None):
    if not os.path.exists(LEDGER):
        return []
    rows = []
    with open(LEDGER, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except Exception:
                    pass
    return rows[-limit:] if limit else rows


def fresh_eval():
    """Evaluate the current state against live chain. Returns (decision, error)."""
    try:
        m = Market()
        d = evaluate(m, STATE["notional"], STATE["claim"], STATE["margin"])
        return d, None
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def build_state():
    d, err = fresh_eval()
    rows = ledger_rows(20)
    trades = [r for r in ledger_rows() if r.get("allowed")]
    return {
        "ok": err is None,
        "error": err,
        "decision": d,
        "desk": dict(STATE),
        "appetites": APPETITES,
        "history": list(reversed(rows)),
        "totals": {
            "decisions": len(ledger_rows()),
            "allowed": len(trades),
            "refused": len(ledger_rows()) - len(trades),
        },
        "chain": {
            "name": "HSK Chain",
            "chain_id": 177,
            "rpc": os.environ.get("HSK_RPC", "https://mainnet.hsk.xyz"),
            "pool": "0x4ff515761864e7838B0AdB8b5b2B8B7A068905dC",
            "pair": "WHSK/USDT 0.30%",
        },
        "server_time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def run_command(text: str):
    """Returns (reply_text, should_refresh)."""
    parts = text.strip().split()
    if not parts:
        return "Empty command. Try `status`.", False
    cmd = parts[0].lower()

    with LOCK:
        if cmd == "status":
            d, err = fresh_eval()
            if err:
                return f"Chain read failed: {err}", True
            m = d["measured"]
            return (
                f"{m['spot']:.6f} USDT/WHSK | widest market gap {m['widest_gap_bps']:.2f} bp "
                f"at {m['gap_window_seconds']}s | round trip {m['round_trip_cost_bps']} bp | "
                f"need {m['required_bps']} bp | decision {d['action']}"
            ), True

        if cmd == "why":
            rows = ledger_rows(1)
            if not rows:
                return "No decisions recorded yet. Run `decide`.", False
            r = rows[-1]
            m = r["measured"]
            lines = [
                f"Decision {r['action']} at {r['read_at']}",
                f"  spot            {m['spot']:.6f} USDT/WHSK (tick {m['spot_tick']})",
                f"  TWAPs           " + ", ".join(f"{k}s={v:.6f}" for k, v in sorted(m["twaps"].items())),
                f"  widest gap      {m['widest_gap_bps']:.2f} bp ({m['widest_gap_signed_bps']:+.2f}) "
                f"at {m['gap_window_seconds']}s",
                f"  round-trip cost {m['round_trip_cost_bps']} bp",
                f"  single-leg cost {m['single_leg_cost_bps']} bp",
                f"  required        {m['required_bps']} bp",
                f"  agent claimed   {r['claimed_edge_bps']:.1f} bp",
            ]
            for why in r["reasons"]:
                lines.append(f"  -> {why}")
            return "\n".join(lines), False

        if cmd == "size" and len(parts) > 1:
            try:
                v = float(parts[1])
            except ValueError:
                return "size needs a number in USDT, e.g. `size 25`", False
            if v <= 0 or v > 100:
                return "Size must be between 0 and 100 USDT (the vault's per-trade cap).", False
            STATE["notional"] = v
            STATE["appetite"] = "custom"
            return f"Proposed size set to ${v:.2f}.", True

        if cmd == "claim" and len(parts) > 1:
            try:
                v = float(parts[1])
            except ValueError:
                return "claim needs a number in bps, e.g. `claim 40`", False
            STATE["claim"] = v
            return f"Claimed edge set to {v:.1f} bp. The vault will check this against the market.", True

        if cmd == "margin" and len(parts) > 1:
            try:
                v = float(parts[1])
            except ValueError:
                return "margin needs a number in bps", False
            STATE["margin"] = v
            return f"Required margin set to {v:.1f} bp.", True

        if cmd == "appetite" and len(parts) > 1:
            name = parts[1].lower()
            if name not in APPETITES:
                return f"Unknown appetite. Choose one of: {', '.join(APPETITES)}", False
            preset = APPETITES[name]
            STATE.update(notional=preset["notional"], margin=preset["margin"], appetite=name)
            return (
                f"Appetite {name}: size ${preset['notional']:.0f}, margin {preset['margin']:.0f} bp."
            ), True

        if cmd == "decide":
            d, err = fresh_eval()
            if err:
                return f"Chain read failed: {err}", True
            record(d, note=f"desk: notional={STATE['notional']} claim={STATE['claim']} "
                           f"margin={STATE['margin']} appetite={STATE['appetite']}")
            return f"Recorded: {d['action']} — {d['reasons'][0]}", True

        if cmd == "history":
            n = 5
            if len(parts) > 1:
                try:
                    n = max(1, min(50, int(parts[1])))
                except ValueError:
                    pass
            rows = ledger_rows(n)
            if not rows:
                return "No history yet.", False
            out = []
            for r in reversed(rows):
                out.append(f"{r['ts'][11:19]}  {r['action']:7} ${r['notional_usd']:.0f}  "
                           f"gap {r['measured']['widest_gap_bps']:.2f}bp  "
                           f"cost {r['measured']['round_trip_cost_bps']}bp")
            return "\n".join(out), False

        if cmd in ("help", "?"):
            return (
                "status | why | decide | history [n] | size <usd> | claim <bps> | "
                "margin <bps> | appetite conservative|balanced|aggressive"
            ), False

        return f"Unknown command `{cmd}`. Type `help`.", False


PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Kharon desk</title>
<style>
  :root { --bg:#0b0c10; --panel:#12141a; --line:#232733; --text:#e6e8ee; --dim:#9aa3b8;
          --good:#37d67a; --bad:#ff5c7a; --accent:#5b8cff; }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--text);font:14.5px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace}
  .wrap{max-width:1080px;margin:0 auto;padding:20px}
  header{display:flex;justify-content:space-between;align-items:baseline;gap:12px;
         border-bottom:1px solid var(--line);padding-bottom:12px;margin-bottom:16px;flex-wrap:wrap}
  h1{font-size:16px;margin:0;letter-spacing:.04em}
  .sub{color:var(--dim);font-size:12.5px}
  .grid{display:grid;grid-template-columns:1.15fr 1fr;gap:14px}
  @media(max-width:820px){.grid{grid-template-columns:1fr}}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px}
  .card h2{margin:0 0 10px;font-size:11px;letter-spacing:.12em;color:var(--dim);
           text-transform:uppercase;font-weight:600}
  .kv{display:flex;justify-content:space-between;gap:10px;padding:3px 0}
  .kv span:first-child{color:var(--dim)}
  .pill{display:inline-block;padding:2px 9px;border-radius:999px;font-size:11px;font-weight:700;letter-spacing:.06em}
  .allow{background:rgba(55,214,122,.14);color:var(--good);border:1px solid rgba(55,214,122,.35)}
  .refuse{background:rgba(255,92,122,.13);color:var(--bad);border:1px solid rgba(255,92,122,.35)}
  .reason{margin-top:10px;padding:10px;background:#0e1015;border-left:2px solid var(--bad);
          border-radius:4px;font-size:12.5px;color:#c7cdda;white-space:pre-wrap}
  #log{height:190px;overflow-y:auto;background:#0e1015;border:1px solid var(--line);
       border-radius:8px;padding:10px;font-size:12.5px;white-space:pre-wrap;margin-bottom:10px}
  .u{color:var(--accent)} .a{color:#c7cdda}
  form{display:flex;gap:8px}
  input{flex:1;background:#0e1015;border:1px solid var(--line);color:var(--text);
        border-radius:7px;padding:9px 11px;font:inherit;outline:none}
  input:focus{border-color:var(--accent)}
  button{background:var(--accent);border:0;color:#fff;border-radius:7px;padding:9px 15px;
         font:inherit;font-weight:600;cursor:pointer}
  button:hover{filter:brightness(1.1)}
  .hint{color:var(--dim);font-size:12.5px;margin-top:8px}
  table{width:100%;border-collapse:collapse;font-size:12px}
  th{text-align:left;color:var(--dim);font-weight:500;padding:4px 6px;border-bottom:1px solid var(--line)}
  td{padding:4px 6px;border-bottom:1px solid #191c24}
  .ok{color:var(--good)} .no{color:var(--bad)}
</style></head><body><div class="wrap">
<header>
  <div><h1>KHARON DESK</h1>
  <div class="sub">GuardrailVault &middot; HSK Chain &middot; <span id="pair">WHSK/USDT 0.30%</span>
  &middot; <span id="clock">-</span></div></div>
  <div><span id="verdict" class="pill refuse">-</span></div>
</header>

<div class="grid">
  <div class="card">
    <h2>Market (live, read from the pool)</h2>
    <div class="kv"><span>spot</span><b id="spot">-</b></div>
    <div class="kv"><span>widest spot-vs-TWAP gap</span><b id="gap">-</b></div>
    <div class="kv"><span>round-trip cost</span><b id="rt">-</b></div>
    <div class="kv"><span>single-leg cost</span><b id="leg">-</b></div>
    <div class="kv"><span>policy requires</span><b id="req">-</b></div>
    <div class="kv"><span>agent claims</span><b id="claimv">-</b></div>
    <div id="reason" class="reason">-</div>
  </div>

  <div class="card">
    <h2>Desk</h2>
    <div class="kv"><span>appetite</span><b id="appetite">-</b></div>
    <div class="kv"><span>proposed size</span><b id="size">-</b></div>
    <div class="kv"><span>required margin</span><b id="marginv">-</b></div>
    <div class="kv"><span>decisions recorded</span><b id="tot">-</b></div>
    <h2 style="margin-top:16px">Talk to it</h2>
    <div id="log"></div>
    <form onsubmit="send(event)">
      <input id="cmd" placeholder="type a command…" autocomplete="off">
      <button>Send</button>
    </form>
    <div class="hint">Try: <b>status</b> &middot; <b>why</b> &middot; <b>decide</b> &middot; <b>size 100</b> &middot; <b>claim 200</b></div>
  </div>
</div>

<div class="card" style="margin-top:14px">
  <h2>Decision ledger (append-only)</h2>
  <table><thead><tr><th>time</th><th>decision</th><th>size</th><th>gap</th><th>round trip</th><th>claim</th><th>reason</th></tr></thead>
  <tbody id="hist"></tbody></table>
</div>
</div>

<script>
function log(cls, txt){
  const el = document.getElementById('log');
  const d = document.createElement('div');
  d.className = cls; d.textContent = txt;
  el.appendChild(d); el.scrollTop = el.scrollHeight;
}
function fmt(v, dp){ return v==null ? '-' : Number(v).toFixed(dp); }

async function refresh(){
  try{
    const r = await fetch('/api/state');
    const s = await r.json();
    if (s.error){ log('a', 'chain read failed: ' + s.error); return; }
    const d = s.decision, m = d.measured;
    document.getElementById('spot').textContent = fmt(m.spot, 6) + ' USDT/WHSK';
    document.getElementById('gap').textContent = fmt(m.widest_gap_bps, 2) + ' bp @ ' + m.gap_window_seconds + 's';
    document.getElementById('rt').textContent = fmt(m.round_trip_cost_bps, 2) + ' bp';
    document.getElementById('leg').textContent = fmt(m.single_leg_cost_bps, 2) + ' bp';
    document.getElementById('req').textContent = fmt(m.required_bps, 2) + ' bp';
    document.getElementById('claimv').textContent = fmt(d.claimed_edge_bps, 1) + ' bp';
    document.getElementById('reason').textContent = d.reasons.join('\\n');
    document.getElementById('appetite').textContent = s.desk.appetite;
    document.getElementById('size').textContent = '$' + fmt(s.desk.notional, 2);
    document.getElementById('marginv').textContent = fmt(s.desk.margin, 0) + ' bp';
    document.getElementById('tot').textContent = s.totals.decisions +
        ' (' + s.totals.allowed + ' allowed / ' + s.totals.refused + ' refused)';
    const v = document.getElementById('verdict');
    v.textContent = d.action;
    v.className = 'pill ' + (d.allowed ? 'allow' : 'refuse');
    document.getElementById('clock').textContent = s.server_time.replace('T',' ').replace('+00:00',' UTC');
    const tb = document.getElementById('hist');
    tb.innerHTML = '';
    for (const row of s.history){
      const tr = document.createElement('tr');
      const m2 = row.measured || {};
      tr.innerHTML = '<td>' + (row.ts||'').slice(11,19) + '</td>' +
        '<td class="' + (row.allowed?'ok':'no') + '">' + row.action + '</td>' +
        '<td>$' + (row.notional_usd||0).toFixed(0) + '</td>' +
        '<td>' + fmt(m2.widest_gap_bps,2) + ' bp</td>' +
        '<td>' + (m2.round_trip_cost_bps ?? '-') + ' bp</td>' +
        '<td>' + (row.claimed_edge_bps||0).toFixed(0) + ' bp</td>' +
        '<td style="color:var(--dim)">' + (row.reasons && row.reasons[0] ? row.reasons[0].split(':')[0] : '-') + '</td>';
      tb.appendChild(tr);
    }
  }catch(e){ log('a','refresh failed: '+e.message); }
}

async function send(ev){
  ev.preventDefault();
  const inp = document.getElementById('cmd');
  const text = inp.value.trim(); if(!text) return;
  inp.value='';
  log('u', '> ' + text);
  try{
    const r = await fetch('/api/command', {method:'POST', headers:{'Content-Type':'application/json'},
                                          body: JSON.stringify({text})});
    const j = await r.json();
    log('a', j.reply || '(no reply)');
    if (j.refresh) refresh();
  }catch(e){ log('a','command failed: '+e.message); }
}

refresh();
setInterval(refresh, 15000);
</script></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass  # keep the console clean

    def _send(self, code, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/state"):
            body = json.dumps(build_state(), default=str).encode()
            self._send(200, body, "application/json")
        elif self.path in ("/", "/index.html"):
            self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        else:
            self._send(404, b'{"error":"not found"}', "application/json")

    def do_POST(self):
        if self.path != "/api/command":
            self._send(404, b'{"error":"not found"}', "application/json")
            return
        try:
            n = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(n) or b"{}")
            text = str(payload.get("text", ""))
        except Exception:
            self._send(400, b'{"error":"bad json"}', "application/json")
            return
        reply, refresh = run_command(text)
        body = json.dumps({"reply": reply, "refresh": refresh}).encode()
        self._send(200, body, "application/json")


def main():
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Kharon desk on http://{HOST}:{PORT}")
    print(f"  pool: WHSK/USDT 0.30% on HSK Chain 177")
    print(f"  reading: {os.environ.get('HSK_RPC', 'https://mainnet.hsk.xyz')}")
    print("  Ctrl-C to stop")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
