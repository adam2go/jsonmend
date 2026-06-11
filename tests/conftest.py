import json
import math
import os

CASES_DIR = os.path.join(os.path.dirname(__file__), "..", "corpus", "cases")


def load_corpus():
    cases = []
    for dirpath, _dirnames, filenames in os.walk(CASES_DIR):
        for fn in sorted(filenames):
            if not fn.endswith(".json"):
                continue
            with open(os.path.join(dirpath, fn), encoding="utf-8") as f:
                case = json.load(f)
            cases.append(("%s/%s" % (os.path.basename(dirpath), fn[:-5]),
                          case))
    cases.sort()
    return cases


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


def strict_loads(text):
    def bad_const(name):
        raise ValueError("non-RFC constant in output: " + name)
    return json.loads(text, parse_constant=bad_const)
