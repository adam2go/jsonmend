#!/usr/bin/env python3
"""jsonmend vs json_repair benchmark.

Methodology (pure* series playbook):
  * --verify first: for every workload, both libraries must produce the
    same parsed value before anything is timed.
  * every measurement is the median of N runs; run the whole script
    multiple times and compare before quoting numbers.
  * all inputs are *broken* JSON, so the json.loads fast path of both
    libraries never short-circuits the comparison.

Usage:
    python tools/bench.py --verify
    python tools/bench.py
    python tools/bench.py --streaming-full   # adds the 10MB linearity run
"""

import argparse
import json
import math
import statistics
import sys
import time

import json_repair

sys.path.insert(0, "src")
import jsonmend  # noqa: E402

MEDIAN_OF = 7


# ---------------------------------------------------------------------------
# workloads: name -> broken JSON text
# ---------------------------------------------------------------------------

def make_toolcall_1kb():
    doc = {
        "id": "call_4fL2xWqz81", "type": "function",
        "function": {
            "name": "search_products",
            "arguments": json.dumps({
                "query": "wireless noise cancelling headphones",
                "filters": {"price_max": 350, "brand": ["sony", "bose"],
                            "in_stock": True},
                "sort": "relevance", "page": 1, "per_page": 25,
            }),
        },
        "metadata": {"user": "u_8812", "session": "s_4419",
                     "locale": "en-US", "ts": 1765432100,
                     "tags": ["shopping", "electronics", "q4"],
                     "notes": "user asked for ANC headphones under $350 "
                              "with replaceable earpads and long battery "
                              "life, prefers over-ear style"},
    }
    text = json.dumps(doc)
    return text[:int(len(text) * 0.93)].rstrip()  # truncate mid-string


def make_payload_100kb():
    rows = [{"id": i, "sku": "SKU-%06d" % i, "title": "Product %d" % i,
             "price": round(3.99 + i * 0.07, 2), "active": i % 3 != 0,
             "tags": ["a", "b", "c"][:1 + i % 3]} for i in range(700)]
    text = json.dumps({"total": 700, "rows": rows})
    return text[:int(len(text) * 0.97)]


def make_fenced_64kb():
    items = [{"title": "Result %d" % i,
              "summary": "A fairly long natural-language summary of result "
                         "%d with some details about why it matters." % i,
              "score": round(1 / (i + 1), 4)} for i in range(300)]
    body = json.dumps({"results": items}, indent=1)
    return ("Here are the search results you asked for:\n\n"
            "```json\n" + body + "\n```\n\nLet me know if you need more.")


def make_dirty_10kb():
    parts = []
    for i in range(80):
        parts.append("{name: 'item %d', value: %d, active: True, "
                     "note: 'plain text %d',}" % (i, i, i))
    return "[" + ", ".join(parts) + ",]"


WORKLOADS = {
    "toolcall_1kb_truncated": make_toolcall_1kb,
    "payload_100kb_truncated": make_payload_100kb,
    "fenced_64kb": make_fenced_64kb,
    "dirty_10kb_pyliterals": make_dirty_10kb,
}


def values_equal(a, b):
    stack = [(a, b)]
    while stack:
        x, y = stack.pop()
        if isinstance(x, bool) or isinstance(y, bool):
            if x is not y:
                return False
            continue
        if isinstance(x, (int, float)) and isinstance(y, (int, float)):
            if x != y and not (math.isnan(x) and math.isnan(y)):
                return False
            continue
        if type(x) is not type(y):
            return False
        if isinstance(x, dict):
            if x.keys() != y.keys():
                return False
            stack.extend((x[k], y[k]) for k in x)
        elif isinstance(x, list):
            if len(x) != len(y):
                return False
            stack.extend(zip(x, y))
        elif x != y:
            return False
    return True


def verify():
    ok = True
    for name, make in WORKLOADS.items():
        text = make()
        ours = jsonmend.loads(text)
        theirs = json_repair.loads(text)
        if values_equal(ours, theirs):
            print("VERIFY %-26s OK (%d bytes)" % (name, len(text)))
        else:
            ok = False
            print("VERIFY %-26s MISMATCH" % name)
            print("  jsonmend   : %r" % (repr(ours)[:200],))
            print("  json_repair: %r" % (repr(theirs)[:200],))
    # streaming: final value equality
    text = make_payload_100kb()
    m = jsonmend.Mender()
    for k in range(0, len(text), 4096):
        m.feed(text[k:k + 4096])
    if values_equal(m.close(), json_repair.loads(text)):
        print("VERIFY %-26s OK" % "streaming_final_value")
    else:
        ok = False
        print("VERIFY %-26s MISMATCH" % "streaming_final_value")
    return ok


def timed(fn, *args, repeat=MEDIAN_OF):
    times = []
    for _ in range(repeat):
        t0 = time.perf_counter()
        fn(*args)
        times.append(time.perf_counter() - t0)
    return statistics.median(times)


def bench_batch():
    print("\n== batch repair (median of %d) ==" % MEDIAN_OF)
    print("%-26s %12s %14s %14s %8s" % (
        "workload", "size", "jsonmend", "json_repair", "speedup"))
    for name, make in WORKLOADS.items():
        text = make()
        repeat = MEDIAN_OF if len(text) < 50_000 else 5
        ours = timed(jsonmend.repair_json, text, repeat=repeat)
        theirs = timed(json_repair.repair_json, text, repeat=repeat)
        print("%-26s %10dB %12.3fms %12.3fms %7.1fx" % (
            name, len(text), ours * 1e3, theirs * 1e3, theirs / ours))


def bench_streaming(full=False):
    print("\n== streaming: feed in 4KB chunks, re-render each chunk ==")
    print("(jsonmend: stateful Mender, O(new bytes); json_repair: "
          "stream_stable re-parse of the whole buffer)")
    rows = [{"id": i, "text": "streamed row %d with some payload" % i,
             "ok": True} for i in range(2000)]
    text = json.dumps({"rows": rows})  # ~150KB
    text = text[:int(len(text) * 0.999)]
    chunk = 4096

    def ours():
        m = jsonmend.Mender()
        for k in range(0, len(text), chunk):
            m.feed(text[k:k + chunk])
        return m.close()

    def theirs():
        buf = ""
        out = None
        for k in range(0, len(text), chunk):
            buf += text[k:k + chunk]
            out = json_repair.repair_json(buf, stream_stable=True,
                                          return_objects=True)
        return out

    t_ours = timed(ours, repeat=3)
    t_theirs = timed(theirs, repeat=3)
    print("%-26s %10dB %12.3fms %12.3fms %7.1fx" % (
        "stream_150kb_4kb_chunks", len(text), t_ours * 1e3,
        t_theirs * 1e3, t_theirs / t_ours))

    if full:
        rows = [{"id": i, "text": "streamed row %d with some payload" % i,
                 "ok": True} for i in range(140_000)]
        big = json.dumps({"rows": rows})  # ~10MB
        t0 = time.perf_counter()
        m = jsonmend.Mender()
        for k in range(0, len(big), chunk):
            m.feed(big[k:k + chunk])
        m.close()
        dt = time.perf_counter() - t0
        print("%-26s %10dB %12.3fms   (jsonmend only; linear check)" % (
            "stream_10mb_4kb_chunks", len(big), dt * 1e3))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--streaming-full", action="store_true")
    args = ap.parse_args()

    print("jsonmend %s vs json_repair %s (Python %s)" % (
        jsonmend.__version__,
        getattr(json_repair, "__version__", "?"),
        sys.version.split()[0]))

    if not verify():
        print("\nverification FAILED — refusing to time mismatched outputs")
        return 1
    if args.verify:
        return 0
    bench_batch()
    bench_streaming(full=args.streaming_full)
    return 0


if __name__ == "__main__":
    sys.exit(main())
