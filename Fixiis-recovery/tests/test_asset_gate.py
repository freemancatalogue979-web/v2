#!/usr/bin/env python3
"""The asset gate: how fast we are allowed to download a page's assets.

The old shape was one flat semaphore.  That is the wrong instrument, because
the two things it has to protect fail differently:

  * this box and its uplink — one global cap is right;
  * the SITE — thirty simultaneous connections to one origin is how an asset
    fetch earns a 429, and a 429 used to mean a font that never arrived.  That
    is an accuracy loss wearing a speed costume.

So the per-host number is LEARNED while the capture runs: it grows with clean
responses and halves the moment a host says "too many", and a throttled asset
is retried once instead of being silently left pointing at the site.

What this pins:

  * the global cap holds under a wide fan-out;
  * two hosts get their own limits (one being throttled must not slow the
    other);
  * a 429 halves that host's limit, pauses it briefly, and the asset is
    RETRIED — and the retry succeeds, so the page is still complete;
  * clean responses grow the limit again, but not while the cooldown runs;
  * HTTP/2 is requested exactly when the runtime can actually do it.

    PYTHONPATH=. python3 tests/test_asset_gate.py
"""
import asyncio
import sys
import types

sys.path.insert(0, "/home/user/v2/Fixiis-recovery")

PASSED = 0


def ok(cond, label):
    global PASSED
    assert cond, label
    PASSED += 1


PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32


class Response:
    def __init__(self, status, body=PNG, ctype="image/png"):
        self.status_code = status
        self.content = body
        self.headers = {"content-type": ctype}


class Client:
    """Answers per URL from `script`, and records what ran at the same time."""

    def __init__(self, script=None, latency=0.01):
        self.script = list(script or [])
        self.latency = latency
        self.calls = []
        self.peak = 0
        self.host_peak = {}
        self._now = 0
        self._per_host = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *e):
        return False

    async def get(self, url, timeout=None):
        self.calls.append(url)
        self._now += 1
        self.peak = max(self.peak, self._now)
        host = url.split("/")[2]
        self._per_host[host] = self._per_host.get(host, 0) + 1
        self.host_peak[host] = max(self.host_peak.get(host, 0), self._per_host[host])
        await asyncio.sleep(self.latency)
        self._per_host[host] -= 1
        self._now -= 1
        if self.script:
            return self.script.pop(0)
        return Response(200)


def run(coro):
    """Run a coroutine — or a zero-arg factory, when gather() must be built
    inside the loop it runs on (gather bakes the loop in at call time)."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        if callable(coro):
            async def _go():
                return await coro()
            return loop.run_until_complete(_go())
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def install_fake_httpx():
    mod = types.ModuleType("httpx")
    mod.AsyncClient = Client

    class Limits:
        def __init__(self, max_connections=None, max_keepalive_connections=None, **k):
            self.max_connections = max_connections
            self.max_keepalive_connections = max_keepalive_connections

    mod.Limits = Limits
    sys.modules["httpx"] = mod


install_fake_httpx()

import dom_capture  # noqa: E402


def fresh_gate():
    dom_capture._ASSET_GATE = None
    return dom_capture.get_asset_gate()


def gate_with(**kw):
    """A gate whose constants the test controls (no 15 s cooldowns)."""
    saved = {k: getattr(dom_capture, k) for k in kw}
    for k, v in kw.items():
        setattr(dom_capture, k, v)
    gate = fresh_gate()
    return gate, saved


def restore(saved):
    for k, v in saved.items():
        setattr(dom_capture, k, v)


# ---------------------------------------------------------------------------
# 1. the global cap holds
# ---------------------------------------------------------------------------
def test_the_global_cap_holds():
    gate, saved = gate_with(ASSET_FETCH_CONCURRENCY=5,
                            ASSET_PER_HOST_START=5, ASSET_PER_HOST_MAX=5)
    try:
        client = Client(latency=0.01)
        urls = ["https://one.example/a%d.png" % i for i in range(20)]
        run(lambda: asyncio.gather(*(gate.fetch(client, u) for u in urls)))
    finally:
        restore(saved)
    ok(client.peak <= 5, "global cap breached: peak %d" % client.peak)
    ok(client.peak >= 4, "the pool never filled (peak %d) — the cap is inert"
       % client.peak)


# ---------------------------------------------------------------------------
# 2. per host, not just global
# ---------------------------------------------------------------------------
def test_two_hosts_get_their_own_limits():
    gate, saved = gate_with(ASSET_FETCH_CONCURRENCY=20,
                            ASSET_PER_HOST_START=3, ASSET_PER_HOST_MAX=20)
    try:
        client = Client(latency=0.02)
        urls = (["https://a.example/%d.png" % i for i in range(9)]
                + ["https://b.example/%d.png" % i for i in range(9)])
        run(lambda: asyncio.gather(*(gate.fetch(client, u) for u in urls)))
        snap = gate.snapshot()
    finally:
        restore(saved)
    ok(client.peak > 3,
       "the two hosts did not run concurrently (peak %d) — a per-host limit "
       "that also caps the whole box is just the old semaphore" % client.peak)
    ok(all(v <= 3 for v in client.host_peak.values()),
       "a host's own limit was breached: %r" % (client.host_peak,))
    ok(all(v >= 2 for v in client.host_peak.values()),
       "a host never got its own parallelism: %r" % (client.host_peak,))


def test_one_throttled_host_does_not_block_another():
    class Picky(Client):
        async def get(self, url, timeout=None):
            if "picky.example" in url:
                self.calls.append(url)
                return Response(429, body=b"slow down")
            return await super().get(url, timeout=timeout)

    gate, saved = gate_with(ASSET_FETCH_CONCURRENCY=10,
                            ASSET_PER_HOST_START=4, ASSET_PER_HOST_MAX=10,
                            ASSET_RETRY_AFTER_S=0.05, ASSET_HOST_COOLDOWN_S=0.2)
    try:
        client = Picky(latency=0.005)
        run(lambda: asyncio.gather(gate.fetch(client, "https://picky.example/a.png"),
                           gate.fetch(client, "https://fine.example/a.png")))
    finally:
        restore(saved)
    ok(client.calls.count("https://fine.example/a.png") == 1,
       "the healthy host was dragged into the throttled one's backoff")
    ok(client.calls.count("https://picky.example/a.png") == 2,
       "the throttled asset was not retried: %r" % client.calls)


# ---------------------------------------------------------------------------
# 3. a 429 is a request to slow down, not a missing file
# ---------------------------------------------------------------------------
def test_a_throttled_asset_is_retried_and_survives():
    gate, saved = gate_with(ASSET_FETCH_CONCURRENCY=8,
                            ASSET_PER_HOST_START=8, ASSET_PER_HOST_MAX=8,
                            ASSET_RETRY_AFTER_S=0.02, ASSET_HOST_COOLDOWN_S=0.5)
    try:
        client = Client(script=[Response(429, body=b"nope"), Response(200)])
        got = run(gate.fetch(client, "https://site.example/font.woff2"))
    finally:
        restore(saved)
    ok(got is not None,
       "a rate-limited font was dropped — that is the accuracy loss this is "
       "supposed to prevent")
    ok(got[0] == PNG, "the retry returned the wrong body")
    ok(len(client.calls) == 2, "expected exactly one retry, got %r" % client.calls)


def test_a_throttled_host_halves_its_limit():
    gate, saved = gate_with(ASSET_FETCH_CONCURRENCY=16,
                            ASSET_PER_HOST_START=16, ASSET_PER_HOST_MAX=16,
                            ASSET_RETRY_AFTER_S=0.01, ASSET_HOST_COOLDOWN_S=0.3)
    try:
        client = Client(script=[Response(429, body=b""), Response(429, body=b"")])
        run(gate.fetch(client, "https://site.example/a.png"))
        snap = gate.snapshot()
    finally:
        restore(saved)
    # 16 -> 8 on the first 429, -> 4 on the retry's 429: it halves every time
    # the host says no, which is the point.
    ok(snap["hosts"]["site.example"]["limit"] == 4,
       "a throttled host must halve its window on every refusal: %r"
       % snap["hosts"]["site.example"])
    ok(snap["hosts"]["site.example"]["throttled"] == 2,
       "the throttle was not recorded: %r" % snap["hosts"]["site.example"])


def test_a_missing_asset_is_not_retried():
    """404 is an answer.  Retrying it is pure latency."""
    gate, saved = gate_with(ASSET_FETCH_CONCURRENCY=8, ASSET_PER_HOST_START=8,
                            ASSET_PER_HOST_MAX=8)
    try:
        client = Client(script=[Response(404, body=b""), Response(200)])
        got = run(gate.fetch(client, "https://site.example/gone.png"))
    finally:
        restore(saved)
    ok(got is None, "a 404 must stay unresolved")
    ok(len(client.calls) == 1, "a 404 was retried: %r" % client.calls)


def test_a_failed_request_does_not_leak_a_slot():
    class Broken(Client):
        async def get(self, url, timeout=None):
            self.calls.append(url)
            raise RuntimeError("connection reset")

    gate, saved = gate_with(ASSET_FETCH_CONCURRENCY=2, ASSET_PER_HOST_START=2,
                            ASSET_PER_HOST_MAX=2)
    try:
        client = Broken(latency=0.001)
        run(lambda: asyncio.gather(*(gate.fetch(client, "https://x.example/%d.png" % i)
                             for i in range(6))))
        snap = gate.snapshot()
        # ...and the gate still works afterwards.
        okclient = Client(latency=0.001)
        got = run(gate.fetch(okclient, "https://x.example/after.png"))
    finally:
        restore(saved)
    ok(snap["inflight"] == 0, "a raised exception leaked an in-flight slot: %r" % snap)
    ok(snap["hosts"]["x.example"]["inflight"] == 0,
       "a raised exception leaked a per-host slot: %r" % snap["hosts"])
    ok(got is not None, "the gate was wedged after a connection failure")


# ---------------------------------------------------------------------------
# 4. it grows back — but not during the cooldown
# ---------------------------------------------------------------------------
def test_the_limit_grows_after_clean_responses():
    gate, saved = gate_with(ASSET_FETCH_CONCURRENCY=20,
                            ASSET_PER_HOST_START=4, ASSET_PER_HOST_MAX=8,
                            ASSET_HOST_GROW_AFTER=2, ASSET_HOST_GROW_STEP=1,
                            ASSET_HOST_COOLDOWN_S=0.0)
    try:
        client = Client(latency=0.001)
        run(lambda: asyncio.gather(*(gate.fetch(client, "https://g.example/%d.png" % i)
                             for i in range(12))))
        snap = gate.snapshot()
    finally:
        restore(saved)
    ok(snap["hosts"]["g.example"]["limit"] > 4,
       "the limit never grew on a healthy host: %r" % snap["hosts"]["g.example"])
    ok(snap["hosts"]["g.example"]["limit"] <= 8,
       "the limit grew past the ceiling: %r" % snap["hosts"]["g.example"])


def test_the_limit_does_not_grow_during_the_cooldown():
    gate, saved = gate_with(ASSET_FETCH_CONCURRENCY=20,
                            ASSET_PER_HOST_START=8, ASSET_PER_HOST_MAX=20,
                            ASSET_HOST_GROW_AFTER=1, ASSET_HOST_GROW_STEP=2,
                            ASSET_RETRY_AFTER_S=0.01,
                            ASSET_HOST_COOLDOWN_S=60.0)
    try:
        # One 429 (halves 8 -> 4), then a clean retry: the retry must NOT be
        # allowed to climb straight back up.
        client = Client(script=[Response(429, body=b""), Response(200)],
                        latency=0.001)
        run(gate.fetch(client, "https://c.example/a.png"))
        snap = gate.snapshot()
    finally:
        restore(saved)
    ok(snap["hosts"]["c.example"]["limit"] == 4,
       "the host climbed back into the same 429: %r" % snap["hosts"]["c.example"])


# ---------------------------------------------------------------------------
# 5. the transport shape
# ---------------------------------------------------------------------------
def test_http2_is_requested_only_when_it_can_work():
    kwargs = dom_capture._asset_client_kwargs({"Accept": "*/*"})
    if dom_capture.HTTP2_ASSETS:
        ok(kwargs.get("http2") is True,
           "h2 is installed but HTTP/2 was not requested — the fan-out then "
           "costs one connection per asset")
        ok("limits" in kwargs, "no connection pool limits were set")
        ok(kwargs["limits"].max_connections >= dom_capture.ASSET_FETCH_CONCURRENCY // 2,
           "the pool is narrower than the concurrency cap")
    else:
        ok("http2" not in kwargs,
           "http2=True without the h2 package raises on every capture")


def test_the_first_wave_tracks_the_learned_width():
    """The first wave must be what one round trip can actually carry."""
    ok(dom_capture.ASSET_FIRST_WAVE <= dom_capture.ASSET_FETCH_CONCURRENCY,
       "the first wave is wider than the whole pool")
    if dom_capture.HTTP2_ASSETS:
        ok(dom_capture.ASSET_FIRST_WAVE >= 8,
           "with HTTP/2 available the first wave is needlessly small (%d)"
           % dom_capture.ASSET_FIRST_WAVE)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("asset gate: %d checks passed" % PASSED)
