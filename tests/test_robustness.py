"""Robustness invariants: any input -> no crash, valid JSON or "" out,
bounded behavior on adversarial shapes."""

import json
import random
import string
import sys
import time

import pytest

import jsonmend
from conftest import strict_loads

ALPHABET = ('{}[](),:"\'\\/+-.0123456789abctfn ulrse\n\t#*`'
            + string.ascii_letters + "“”‘’«»😀中ß")


def _check(text):
    out = jsonmend.repair_json(text)
    assert isinstance(out, str)
    if out != "":
        strict_loads(out)  # must be valid RFC 8259 JSON


def test_fuzz_random_garbage():
    rng = random.Random(20260612)
    for trial in range(2000):
        text = "".join(rng.choice(ALPHABET)
                       for _ in range(rng.randint(0, 60)))
        try:
            _check(text)
        except Exception:
            print("FUZZ INPUT: %r" % text)
            raise


def test_fuzz_mutated_valid_json():
    rng = random.Random(42)
    base = json.dumps({"name": "test", "items": [1, 2.5, True, None],
                       "nested": {"a": "x y", "b": [{"c": "d"}]}})
    for trial in range(2000):
        text = base
        for _ in range(rng.randint(1, 6)):
            op = rng.randrange(4)
            pos = rng.randrange(len(text) + 1)
            if op == 0 and text:
                text = text[:pos] + text[pos + 1:]
            elif op == 1:
                text = text[:pos] + rng.choice(ALPHABET) + text[pos:]
            elif op == 2 and text:
                k = rng.randrange(len(text))
                text = text[:k] + rng.choice(ALPHABET) + text[k + 1:]
            else:
                text = text[:pos]
        try:
            _check(text)
        except Exception:
            print("FUZZ INPUT: %r" % text)
            raise


def test_fuzz_chunked_equals_batch():
    rng = random.Random(7)
    for trial in range(300):
        text = "".join(rng.choice(ALPHABET)
                       for _ in range(rng.randint(0, 80)))
        batch = jsonmend.mend(text)
        m = jsonmend.Mender()
        k = 0
        while k < len(text):
            step = rng.randint(1, 9)
            m.feed(text[k:k + step])
            k += step
        streamed = m.close()
        assert repr(streamed) == repr(batch) or streamed == batch, (
            "divergence on %r: %r != %r" % (text, streamed, batch))


def test_deep_nesting_no_recursion_error():
    depth = 100_000
    text = "[" * depth + "1" + "]" * depth
    value = jsonmend.loads(text, skip_json_loads=True)
    for _ in range(depth):
        assert isinstance(value, list) and len(value) == 1
        value = value[0]
    assert value == 1


def test_deep_nesting_truncated():
    text = "[" * 100_000
    out = jsonmend.repair_json(text, skip_json_loads=True)
    assert out.count("[") == 100_000 and out.count("]") == 100_000


def test_deep_object_nesting():
    depth = 50_000
    text = '{"a":' * depth + "1" + "}" * depth
    value = jsonmend.loads(text, skip_json_loads=True)
    for _ in range(depth):
        value = value["a"]
    assert value == 1


def test_quote_storm_is_linear():
    """Candidate-quote heuristics must not be quadratic."""
    def run(reps):
        text = '{"a": "' + 'x " y ' * reps + '"}'
        t0 = time.perf_counter()
        jsonmend.repair_json(text, skip_json_loads=True)
        return time.perf_counter() - t0

    # warm up (PyPy JIT) and use a large-enough baseline that timer noise
    # and JIT effects cannot dominate the ratio
    run(2000)
    run(2000)
    small = min(run(2000) for _ in range(5))
    big = min(run(20000) for _ in range(3))
    # 10x input: O(n) ~10x, O(n^2) ~100x; 60x cleanly separates them
    # while tolerating 6x of scheduling/JIT noise
    assert big < max(small, 1e-3) * 60, (small, big)


def test_huge_clean_string_fast():
    text = '{"text": "' + "lorem ipsum " * 100_000 + '"}'  # ~1.2MB
    t0 = time.perf_counter()
    value = jsonmend.loads(text, skip_json_loads=True)
    dt = time.perf_counter() - t0
    assert value == {"text": "lorem ipsum " * 100_000}
    assert dt < 1.0, dt


def test_bytes_input():
    assert jsonmend.loads(b'{"a": 1}') == {"a": 1}
    assert jsonmend.loads(bytearray(b'[1, 2')) == [1, 2]


def test_bom_input():
    assert jsonmend.loads("﻿{\"a\": 1}") == {"a": 1}


def test_output_always_utf8_encodable():
    cases = ['"a\\ud800b"', '{"s": "x\\udfff"}', '"\\ud83d\\ude00"']
    for text in cases:
        out = jsonmend.repair_json(text, skip_json_loads=True,
                                   ensure_ascii=False)
        out.encode("utf-8")  # must not raise
