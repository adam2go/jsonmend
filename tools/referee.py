#!/usr/bin/env python3
"""Run the conformance corpus against jsonmend, json_repair and jsonrepair.

Usage:
    python tools/referee.py            # full scoreboard
    python tools/referee.py --fails    # list jsonmend failures in detail
    python tools/referee.py --impl jsonmend --fails
"""

import argparse
import json
import math
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
CASES = os.path.join(ROOT, "corpus", "cases")
sys.path.insert(0, os.path.join(ROOT, "src"))

NODE_RUNNER = """
const {jsonrepair} = require('jsonrepair');
const fs = require('fs');
const cases = JSON.parse(fs.readFileSync(0, 'utf8'));
const out = {};
for (const [name, input] of Object.entries(cases)) {
  try {
    out[name] = {ok: true, output: jsonrepair(input)};
  } catch (e) {
    out[name] = {ok: false, error: String(e.message)};
  }
}
process.stdout.write(JSON.stringify(out));
"""


def values_equal(a, b):
    """Type-aware deep equality: numbers compare mathematically,
    booleans are not numbers."""
    stack = [(a, b)]
    while stack:
        x, y = stack.pop()
        if isinstance(x, bool) or isinstance(y, bool):
            if not (isinstance(x, bool) and isinstance(y, bool) and x == y):
                return False
            continue
        if isinstance(x, (int, float)) and isinstance(y, (int, float)):
            if isinstance(x, float) and math.isnan(x):
                if not (isinstance(y, float) and math.isnan(y)):
                    return False
                continue
            if x != y:
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


def strict_loads(text):
    """RFC 8259 parse: reject NaN/Infinity literals in output."""
    def bad_const(name):
        raise ValueError("non-RFC constant in output: " + name)
    return json.loads(text, parse_constant=bad_const)


def judge(case, result):
    """result: {"ok": bool, "output": str} or {"ok": False, "error": ...}"""
    verdict = case["verdict"]
    if verdict == "unrecoverable":
        if not result["ok"]:
            return True, "raised"
        out = result["output"].strip()
        if out in ('', '""', "null"):
            return True, "empty"
        return False, "produced a value from garbage: %r" % out[:60]
    if not result["ok"]:
        return False, "raised: %s" % result.get("error", "?")
    try:
        value = strict_loads(result["output"])
    except Exception as e:
        return False, "output is not valid JSON: %s | %r" % (
            e, result["output"][:80])
    if case.get("check") == "valid":
        return True, "valid"
    if verdict == "deterministic":
        if values_equal(value, case["expected"]):
            return True, "match"
        return False, "value mismatch: got %r" % (value,)
    for acc in case["accepted"]:
        if values_equal(value, acc):
            return True, "accepted"
    return False, "value not in accepted set: got %r" % (value,)


def run_jsonmend(cases):
    import importlib
    for mod in list(sys.modules):
        if mod.startswith("jsonmend"):
            del sys.modules[mod]
    import jsonmend
    out = {}
    for name, text in cases.items():
        try:
            out[name] = {"ok": True, "output": jsonmend.repair_json(text)}
        except Exception as e:
            out[name] = {"ok": False, "error": "%s: %s" % (
                type(e).__name__, e)}
    return out


def run_json_repair(cases):
    from json_repair import repair_json
    out = {}
    for name, text in cases.items():
        try:
            r = repair_json(text)
            if not isinstance(r, str):
                r = json.dumps(r)
            out[name] = {"ok": True, "output": r}
        except Exception as e:
            out[name] = {"ok": False, "error": "%s: %s" % (
                type(e).__name__, e)}
    return out


def run_jsonrepair_node(cases):
    proc = subprocess.run(
        ["node", "-e", NODE_RUNNER], input=json.dumps(cases),
        capture_output=True, text=True, cwd=ROOT, timeout=300)
    if proc.returncode != 0:
        raise RuntimeError("node runner failed: " + proc.stderr[:500])
    return json.loads(proc.stdout)


def load_cases():
    cases = {}
    for dirpath, _dirnames, filenames in os.walk(CASES):
        for fn in sorted(filenames):
            if not fn.endswith(".json"):
                continue
            with open(os.path.join(dirpath, fn), encoding="utf-8") as f:
                case = json.load(f)
            cat = os.path.basename(dirpath)
            cases["%s/%s" % (cat, fn[:-5])] = case
    return cases


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fails", action="store_true")
    ap.add_argument("--impl", default=None,
                    choices=["jsonmend", "json_repair", "jsonrepair"])
    ap.add_argument("--category", default=None)
    args = ap.parse_args()

    cases = load_cases()
    if args.category:
        cases = {k: v for k, v in cases.items()
                 if k.startswith(args.category + "/")}
    inputs = {k: v["input"] for k, v in cases.items()}

    impls = {}
    wanted = [args.impl] if args.impl else [
        "jsonmend", "json_repair", "jsonrepair"]
    if "jsonmend" in wanted:
        impls["jsonmend"] = run_jsonmend(inputs)
    if "json_repair" in wanted:
        impls["json_repair"] = run_json_repair(inputs)
    if "jsonrepair" in wanted:
        impls["jsonrepair"] = run_jsonrepair_node(inputs)

    cats = sorted({k.split("/")[0] for k in cases})
    table = {}
    fails = {name: [] for name in impls}
    for impl_name, results in impls.items():
        for key, case in cases.items():
            ok, why = judge(case, results[key])
            cat = key.split("/")[0]
            t = table.setdefault(cat, {}).setdefault(impl_name, [0, 0])
            t[1] += 1
            if ok:
                t[0] += 1
            else:
                fails[impl_name].append((key, why))

    width = max(len(c) for c in cats) + 2
    print("%-*s" % (width, "category"), end="")
    for name in impls:
        print("%-22s" % name, end="")
    print()
    totals = {name: [0, 0] for name in impls}
    for cat in cats:
        print("%-*s" % (width, cat), end="")
        for name in impls:
            ok, total = table[cat][name]
            totals[name][0] += ok
            totals[name][1] += total
            print("%-22s" % ("%d/%d" % (ok, total)), end="")
        print()
    print("%-*s" % (width, "TOTAL"), end="")
    for name in impls:
        ok, total = totals[name]
        print("%-22s" % ("%d/%d (%.1f%%)" % (ok, total, 100 * ok / total)),
              end="")
    print()

    if args.fails:
        for name in impls:
            if args.impl and name != args.impl:
                continue
            print("\n==== %s failures ====" % name)
            for key, why in fails[name]:
                print("- %s: %s" % (key, why))
                print("    input: %r" % cases[key]["input"][:100])


if __name__ == "__main__":
    main()
