"""Public API surface and json_repair drop-in compatibility."""

import io
import json
import math
import os
import tempfile

import pytest

import jsonmend
from jsonmend import JSONMendError, from_file, load, loads, mend, repair_json


def test_repair_json_returns_string():
    out = repair_json("{'a': 1}")
    assert out == '{"a": 1}'
    assert isinstance(out, str)


def test_repair_json_separators_match_json_repair():
    # json_repair emits ", " / ": " separators; we match for drop-in diffs
    assert repair_json('{"a":1,"b":[1,2]}', skip_json_loads=True) == \
        '{"a": 1, "b": [1, 2]}'


def test_return_objects():
    assert repair_json("[1, 2", return_objects=True) == [1, 2]
    assert repair_json("{}", return_objects=True) == {}


def test_skip_json_loads():
    assert repair_json('{"a": 1}', skip_json_loads=True) == '{"a": 1}'


def test_ensure_ascii():
    assert repair_json("{'试': '验'}", ensure_ascii=False) == '{"试": "验"}'
    assert "\\u" in repair_json("{'试': '验'}")


def test_loads():
    assert loads("{'a': 1}") == {"a": 1}
    assert loads('{"a": 1}') == {"a": 1}


def test_load_fd():
    assert load(io.StringIO('{"a": [1, 2')) == {"a": [1, 2]}


def test_from_file():
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                     encoding="utf-8") as f:
        f.write('﻿{"a": 1,')  # BOM + truncation
        path = f.name
    try:
        assert from_file(path) == {"a": 1}
    finally:
        os.unlink(path)


def test_mend_strict():
    with pytest.raises(JSONMendError):
        mend("", strict=True)
    with pytest.raises(JSONMendError):
        mend("   ", strict=True)
    assert mend("") == ""


def test_nonfinite_serializes_as_null():
    # unlike json_repair, output is always valid JSON
    assert repair_json("NaN", skip_json_loads=True) == "null"
    assert repair_json('{"a": NaN, "b": Infinity}', skip_json_loads=True) \
        == '{"a": null, "b": null}'
    out = repair_json("[NaN]", skip_json_loads=True)
    json.loads(out, parse_constant=lambda c: pytest.fail(c))


def test_loads_keeps_nonfinite_floats():
    v = loads('{"a": NaN, "b": Infinity}', skip_json_loads=True)
    assert math.isnan(v["a"]) and math.isinf(v["b"])


def test_big_integers_preserved():
    assert loads('{"key": 12345678901234567890}') == {
        "key": 12345678901234567890}


def test_json_dumps_kwargs_passthrough():
    assert repair_json("{'b':1,'a':2}", sort_keys=True) == \
        '{"a": 2, "b": 1}'
    assert repair_json("{'a':1}", indent=2) == '{\n  "a": 1\n}'


def test_version():
    assert jsonmend.__version__
