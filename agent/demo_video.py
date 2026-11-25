#!/usr/bin/env python3
"""
Render the Kharon demo video: a real browser session against the live desk, driven by
Playwright, with typed commands and narrative captions. Everything shown is real output
from HSK Chain — no mock data, no staged numbers.

    ~/LYID-BOTS/.venv-shared/bin/python agent/demo_video.py

Output: demo/kharon-demo.mp4  (1920x1080, ~75s)
Requires: the desk running on 127.0.0.1:8792 (see README), ffmpeg on PATH.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time

from playwright.sync_api import sync_playwright

DESK = os.environ.get("DESK_URL", "http://127.0.0.1:8792/")
OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "demo")
FRAMES = os.path.join(OUT_DIR, "frames")
W, H = 1600, 900   # 16:9 so the 1080p render does not letterbox
FPS = 12

# (seconds to hold, caption, action) — action is a callable or None
SCRIPT = [
    (3.5, "HSK Chain Horizon · AI Agent Trading track", None),
    (4.0, "I measured the pool before writing any trading logic", "scroll"),
    (4.5, "96 swaps in 50 hours on $838k of TVL — about $558/day", None),
    (4.5, "Spot sits on its own 1h TWAP to within 0.1 bp", None),
    (4.0, "A round trip costs ~60 bp. The market offers 0.1 bp.", None),
    (3.5, "So the honest agent behaviour is to refuse", None),
    (5.0, "The vault enforces that, on numbers it measures itself", "cmd:status"),
    (5.0, "Ask why — it shows every number behind the refusal", "cmd:why"),
    (4.5, "Try to trade anyway. Size and claim don't help.", "cmd:size 100"),
    (5.0, "", "cmd:claim 200"),
    (5.5, "Recorded on chain. Refusals are first-class evidence.", "cmd:decide"),
    (5.0, "15/15 fork tests against HSK Chain mainnet", "scrollbottom"),
    (4.5, "github.com/rmndkyl/hsk-guardrail", None),
]


def ensure_dir(p):
    os.makedirs(p, exist_ok=True)


def main():
    ensure_dir(OUT_DIR)
    if os.path.isdir(FRAMES):
        shutil.rmtree(FRAMES)
    ensure_dir(FRAMES)

    idx = 0
    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--force-device-scale-factor=1"])
        page = browser.new_page(viewport={"width": W, "height": H})

        # A caption bar pinned to the bottom of the viewport, on top of the page.
        page.goto(DESK, wait_until="networkidle")
        page.add_style_tag(content="""
            body { padding-bottom: 120px !important; }
            #demo-cap {
              position: fixed; left: 0; right: 0; bottom: 0; z-index: 99999;
              background: linear-gradient(to top, rgba(6,7,10,.97), rgba(6,7,10,.86));
              border-top: 1px solid #2b3040;
              padding: 22px 34px; font: 500 26px/1.4 ui-monospace, Menlo, monospace;
              color: #eef1f7; letter-spacing: .01em;
              transform: translateY(110%); transition: transform .35s ease;
              text-align: center;
            }
            #demo-cap.on { transform: translateY(0); }
            #demo-cap b { color: #7fa6ff; font-weight: 700; }
        """)
        page.evaluate("""() => {
            const d = document.createElement('div');
            d.id = 'demo-cap';
            document.body.appendChild(d);
        }""")

        def caption(text):
            if not text:
                page.evaluate("document.getElementById('demo-cap').classList.remove('on')")
                return
            page.evaluate(
                """(t) => {
                    const d = document.getElementById('demo-cap');
                    d.innerHTML = t.replace(/\\*\\*(.+?)\\*\\*/g, '<b>$1</b>');
                    d.classList.add('on');
                }""",
                text,
            )

        def type_command(text):
            page.click("#cmd")
            page.fill("#cmd", "")
            page.type("#cmd", text, delay=55)
            page.wait_for_timeout(280)
            page.press("#cmd", "Enter")
            page.wait_for_timeout(1500)

        for hold, cap, action in SCRIPT:
            caption(cap)
            page.wait_for_timeout(400)
            if action == "scroll":
                page.mouse.wheel(0, 220)
            elif action == "scrollbottom":
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            elif action and action.startswith("cmd:"):
                type_command(action[4:])

            # sample frames across the hold
            frames = max(2, int(hold * FPS))
            for _ in range(frames):
                page.screenshot(path=os.path.join(FRAMES, f"f{idx:05d}.png"))
                idx += 1
                time.sleep(hold / frames)

        browser.close()

    print(f"captured {idx} frames")

    mp4 = os.path.join(OUT_DIR, "kharon-demo.mp4")
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-framerate", str(FPS),
        "-i", os.path.join(FRAMES, "f%05d.png"),
        "-vf", "scale=1920:1080:flags=lanczos,format=yuv420p",
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-movflags", "+faststart",
        mp4,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print("ffmpeg failed:", r.stderr[-800:], file=sys.stderr)
        return 1
    size = os.path.getsize(mp4) / 1e6
    print(f"wrote {mp4}  ({size:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
