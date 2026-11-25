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


# The page lives next to this file so it can be edited as HTML, not as a Python
# string literal. Keeping it out of the source also keeps this module readable.
PAGE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "desk_page.html")


def load_page() -> str:
    """Read the desk page from disk. Re-read on every request so an edit shows up
    on refresh without restarting the server."""
    try:
        with open(PAGE_PATH, encoding="utf-8") as f:
            return f.read()
    except OSError as exc:
        return (
            "<!doctype html><meta charset=utf-8>"
            "<body style=\"font:16px/1.5 Georgia,serif;padding:40px;max-width:40em\">"
            f"<h1>Desk page missing</h1><p>Could not read <code>{PAGE_PATH}</code>: "
            f"{exc}</p><p>The API is still up: <a href=/api/state>/api/state</a>.</p>"
        )



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
            self._send(200, load_page().encode(), "text/html; charset=utf-8")
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
