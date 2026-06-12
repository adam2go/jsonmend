#!/usr/bin/env python3
"""Cross-language differential: Python jsonmend vs JS jsonmend.

Verifies the README claim that the two implementations repair
identically: every corpus input is run through both engines and the
parsed outputs are compared value-by-value (numbers mathematically,
booleans strictly, key sets exactly).
"""

import json
import math
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
sys.path.insert(0, os.path.join(ROOT, "src"))

NODE_RUNNER = """
const { repairJson } = require('./js/index.cjs');
const fs = require('fs');
const cases = JSON.parse(fs.readFileSync(0, 'utf8'));
const out = {};
for (const [name, input] of Object.entries(cases)) {
  try {
    out[name] = { ok: true, output: repairJson(input) };
  } catch (e) {
    out[name] = { ok: false, error: String(e && e.message) };
  }
}
process.stdout.write(JSON.stringify(out));
"""


def values_equal(a, b):
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


def main():
    import jsonmend

    cases_dir = os.path.join(ROOT, "corpus", "cases")
    inputs = {}
    for dirpath, _d, filenames in os.walk(cases_dir):
        for fn in sorted(filenames):
            if fn.endswith(".json"):
                with open(os.path.join(dirpath, fn), encoding="utf-8") as f:
                    case = json.load(f)
                key = "%s/%s" % (os.path.basename(dirpath), fn[:-5])
                inputs[key] = case["input"]

    proc = subprocess.run(["node", "-e", NODE_RUNNER],
                          input=json.dumps(inputs), capture_output=True,
                          text=True, cwd=ROOT, timeout=300)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[:800])
    js_results = json.loads(proc.stdout)

    same = 0
    diffs = []
    for key, text in inputs.items():
        py_out = jsonmend.repair_json(text)
        js = js_results[key]
        try:
            py_val = json.loads(py_out) if py_out else ""
        except ValueError:
            py_val = ("<invalid>", py_out)
        if not js["ok"]:
            js_val = ("<error>", js.get("error"))
        else:
            try:
                js_val = json.loads(js["output"]) if js["output"] else ""
            except ValueError:
                js_val = ("<invalid>", js["output"])
        if values_equal(py_val, js_val):
            same += 1
        else:
            diffs.append((key, py_val, js_val))

    print("cross-language agreement: %d/%d" % (same, len(inputs)))
    for key, py_val, js_val in diffs:
        print("- %s" % key)
        print("    py: %r" % (py_val,))
        print("    js: %r" % (js_val,))
    return 1 if diffs else 0


if __name__ == "__main__":
    sys.exit(main())
