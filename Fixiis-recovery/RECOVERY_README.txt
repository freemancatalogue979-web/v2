Fixiis recovery package
=======================
Repo:   https://github.com/freemancatalogue979-web/Fixiis
Branch: fix/dom-capture-rendering-issues

Includes session fixes:
- DOM capture / page_transition / iframe swap
- img box sizing, lazy images, dynamic canvas
- citi.com override removed; streaming mirror fixes
- true mobile viewport; no artificial floors
- scroll sync; duplicate node prevention
- CDP clicks; latency / 120Hz deltas
- SingleFile interference reduced

Quick start:
  unzip Fixiis-recovery.zip && cd Fixiis-recovery
  python3 -m venv .venv && source .venv/bin/activate
  pip install -r requirements.txt
  # Chrome/Chromium + xvfb recommended on server
  python main.py

Tunnel example:
  cloudflared tunnel --url http://127.0.0.1:8446
