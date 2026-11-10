"""Delta relay framing: python tests/test_delta_relay.py

The delta channel is the low-latency path: the page observes its own
mutations and streams compact ops to the mirror.  Every op in a batch
describes one small change — a new message, a class swap, a text change —
so a batch that does not fit in one frame has to be SPLIT, not dropped.

It used to be dropped, silently:

    if not isinstance(payload, str) or not payload or len(payload) > 1_000_000:
        return

The whole batch — every op in it — went into the void with no log, no
drop counter and no resync.  On a CSS-heavy page that was not a rare
edge case: one new message could push the batch past the cap, and the
message never arrived at the mirror.

These tests pin the relay's framing contract:

  1. a batch that fits is forwarded BYTE-EXACT (never parsed and
     re-serialised — the browser already paid for JSON.stringify);
  2. a batch that does not fit is split into several frames, each under
     the cap, and the ops keep their order (the observer's phase order
     puts structure before state, and that must survive the split);
  3. the split pieces are still valid JSON the client can apply;
  4. a batch whose SINGLE op is over the cap cannot be split — that is a
     recorded drop (so the mirror converges on a fresh document), never a
     silent one.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dom_capture  # noqa: E402

PASSED = 0


def ok(cond, label):
    global PASSED
    assert cond, label
    PASSED += 1


LIMIT = 1000

# ---------------------------------------------------------------------------
# 1. A batch that fits is forwarded byte-exact
# ---------------------------------------------------------------------------
small = '[["a",1,"class","x"],["t",2,0,"hello"]]'
chunks = dom_capture._split_delta_ops(small, LIMIT)
ok(chunks == [small], "a batch under the limit is not split at all")
ok(chunks[0] is small, "and it is the very same string (forwarded byte-exact)")


# ---------------------------------------------------------------------------
# 2. A batch that does not fit is split, in order
# ---------------------------------------------------------------------------
ops = [["s", i, "x" * 200] for i in range(20)]
big = json.dumps(ops, separators=(",", ":"))
ok(len(big) > LIMIT, "the sample batch really is over the limit")
parts = dom_capture._split_delta_ops(big, LIMIT)
ok(len(parts) > 1, "an over-limit batch is split into several frames")
ok(all(len(p) <= LIMIT for p in parts), "every piece is under the limit")
ok(all(p.startswith("[[") and p.endswith("]]") for p in parts),
   "every piece is still an ops array")

# ---------------------------------------------------------------------------
# 3. The split preserves the ops AND their order
# ---------------------------------------------------------------------------
rebuilt = []
for piece in parts:
    parsed = json.loads(piece)
    ok(isinstance(parsed, list) and parsed, "each piece parses as a JSON array")
    rebuilt.extend(parsed)
ok(rebuilt == ops, "the ops survive the split unchanged")
ok([op[1] for op in rebuilt] == list(range(20)), "and in their original order")


# ---------------------------------------------------------------------------
# 4. Strings containing brackets must not confuse the splitter
# ---------------------------------------------------------------------------
tricky = [["t", 1, 0, 'a ] b [ c "quoted" \\ backslash'], ["a", 2, "x", "]]"]]
tricky_text = json.dumps(tricky, separators=(",", ":"))
rebuilt_tricky = []
for piece in dom_capture._split_delta_ops(tricky_text, LIMIT):
    rebuilt_tricky.extend(json.loads(piece))
ok(rebuilt_tricky == tricky, "brackets and quotes inside op payloads are safe")


# ---------------------------------------------------------------------------
# 5. A single op over the cap cannot be split — that is a recorded drop
# ---------------------------------------------------------------------------
one_huge = json.dumps([["s", 1, "y" * (LIMIT * 2)]], separators=(",", ":"))
ok(dom_capture._split_delta_ops(one_huge, LIMIT) == [],
   "an op that alone is over the limit cannot be split")

# ---------------------------------------------------------------------------
# 6. The relay never throws a batch away for being large, and records the
#    drop it cannot deliver
# ---------------------------------------------------------------------------
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "..", "dom_capture.py"), encoding="utf-8") as fh:
    relay_src = fh.read()
start = relay_src.index("async def _on_delta")
end = relay_src.index('if not getattr(session, "_delta_binding_installed"', start)
relay = relay_src[start:end]

ok("len(payload) > 1_000_000" not in relay,
   "no batch is thrown away for being large")
ok("_split_delta_ops(payload_text" in relay,
   "the relay splits a batch that does not fit one frame")
ok('_record_delta_drop("oversize")' in relay,
   "a batch that cannot be split at all is a recorded drop")
ok('"ops":\' + payload_text' in relay,
   "the common path still forwards the ops JSON verbatim")

print("delta relay: %d checks passed" % PASSED)
