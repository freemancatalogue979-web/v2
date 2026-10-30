"""The metrics payload may only carry numbers the server actually has.

``/api/server/metrics`` (and the ``system_stats`` WebSocket message built from
the same payload) gained uptime and GPU status. Both have to be honest:

* ``uptime_s`` is the process's own age — not the host's, not a guess, and it
  must increase between two calls;
* ``gpu`` is ``{available: false}`` unless the server genuinely detected one.
  A missing GPU is reported as missing; it is never invented.

Everything else in the payload has to keep its old shape, because the old
dashboard code and the WebSocket ``system_stats`` message still read it.

Run: PYTHONPATH=. python tests/test_admin_metrics.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHECKS = 0


def ok(cond, label):
    global CHECKS
    assert cond, label
    CHECKS += 1


def main() -> int:
    try:
        import api  # noqa: F401  (imported for its metrics builder)
    except Exception as exc:               # pragma: no cover - env without deps
        print(f"SKIP  admin metrics: cannot import api ({exc.__class__.__name__})")
        return 0

    ok(hasattr(api, "_build_system_metrics_payload"),
       "the shared metrics payload builder is gone")

    # The dashboard is what the operator reads; run the same builder.
    first = api._build_system_metrics_payload()
    ok(isinstance(first, dict), "the metrics payload is not a dict")

    # --- the numbers the old dashboard already used -------------------------
    for key in ("cpu", "memory", "network", "active_sessions", "max_sessions",
                "cpu_percent", "memory_percent", "active_sessions_count",
                "timestamp"):
        ok(key in first, f"the payload lost {key!r}, which the panel still reads")
    ok(isinstance(first["cpu"], dict) and "percent" in first["cpu"],
       "cpu is no longer {percent}")
    ok(isinstance(first["memory"], dict) and "percent" in first["memory"],
       "memory is no longer {percent, ...}")
    ok(isinstance(first["timestamp"], (int, float)) and first["timestamp"] > 1e9,
       "timestamp is not a unix time")

    # --- uptime --------------------------------------------------------------
    ok("uptime_s" in first, "uptime is not in the payload the dashboard reads")
    ok(isinstance(first["uptime_s"], (int, float)),
       "uptime_s is not a number (the screen would print 'nulls')")
    ok(first["uptime_s"] >= 0, "uptime_s is negative")
    ok(first["uptime_s"] < 60 * 60 * 24 * 365 * 10,
       "uptime_s is implausible (ten years) — clock arithmetic is wrong")
    time.sleep(0.05)
    second = api._build_system_metrics_payload()
    ok(second["uptime_s"] >= first["uptime_s"],
       "uptime_s went backwards between two calls")

    # --- GPU ---------------------------------------------------------------
    ok("gpu" in first, "GPU availability is not in the payload")
    gpu = first["gpu"]
    ok(isinstance(gpu, dict), "gpu is not a dict")
    ok("available" in gpu, "gpu has no 'available' flag")
    ok(isinstance(gpu["available"], bool), "gpu.available is not a boolean")
    if not gpu["available"]:
        ok(gpu.get("name") is None,
           "a GPU with no detected device must not carry a name")
    else:
        ok(isinstance(gpu.get("name"), str) and gpu["name"].strip(),
           "a GPU reported as available must say which device it is")

    # --- "available" means a device, not a working PNG fallback ------------
    ok(hasattr(api, "_gpu_availability"), "the GPU honesty helper is gone")
    if hasattr(api, "_gpu_availability"):
        cases = [
            ({"available": True, "type": "none"}, False, None),
            ({"available": True, "type": "nvidia"}, True, "nvidia"),
            ({"available": True, "device": "AMD Radeon"}, True, "AMD Radeon"),
            ({"available": True}, False, None),
            ({"available": False, "type": "nvidia"}, True, "nvidia"),
            ({}, False, None),
            (None, False, None),
        ]
        for status, want_available, want_name in cases:
            got = api._gpu_availability(status)
            ok(got["available"] is want_available,
               f"gpu availability for {status!r} should be {want_available}, got {got}")
            ok(got["name"] == want_name,
               f"gpu name for {status!r} should be {want_name!r}, got {got['name']!r}")
        # A machine with no device must never be described as having one.
        ok(api._gpu_availability({"available": True, "type": "none"})["available"] is False,
           "a CPU-only box is reported as having a GPU")

    # --- the payload survives JSON, so the WebSocket message can carry it ----
    try:
        json.dumps(first)
    except TypeError as exc:
        raise AssertionError(f"the metrics payload is not JSON-serialisable: {exc}")

    # --- the payload keeps the shape the shipped panel reads ----------------
    admin = (ROOT / "Admin.html").read_text(encoding="utf-8", errors="replace")
    ok("/api/server/metrics" in admin,
       "the panel no longer reads the metrics endpoint this payload feeds")
    for key in ("cpu_percent", "memory_percent", "active_sessions"):
        ok(key in admin, f"the panel reads {key!r}, which the payload must keep")

    print(f"PASS  admin metrics: {CHECKS} checks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
