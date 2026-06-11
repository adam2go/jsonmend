"""Run the full conformance corpus against jsonmend.

jsonmend must pass 100% of its own corpus; this is the scoreboard claim
in the README, re-verified on every commit.
"""

import pytest

import jsonmend
from conftest import load_corpus, strict_loads, values_equal

CORPUS = load_corpus()


@pytest.mark.parametrize("name,case", CORPUS, ids=[c[0] for c in CORPUS])
def test_corpus_case(name, case):
    verdict = case["verdict"]
    if verdict == "unrecoverable":
        try:
            out = jsonmend.repair_json(case["input"])
        except jsonmend.JSONMendError:
            return
        assert out.strip() in ('', '""', "null"), out
        # strict mode must raise
        with pytest.raises(jsonmend.JSONMendError):
            jsonmend.repair_json(case["input"], strict=True)
        return

    out = jsonmend.repair_json(case["input"])
    value = strict_loads(out)  # output must always be valid RFC 8259 JSON

    if case.get("check") == "valid":
        return
    if verdict == "deterministic":
        assert values_equal(value, case["expected"]), (
            "got %r, expected %r" % (value, case["expected"]))
    else:
        assert any(values_equal(value, acc) for acc in case["accepted"]), (
            "got %r, accepted %r" % (value, case["accepted"]))


@pytest.mark.parametrize("name,case", CORPUS, ids=[c[0] for c in CORPUS])
def test_corpus_case_skip_fast_path(name, case):
    """The repair machine must agree with itself without the
    json.loads fast path."""
    if case["verdict"] == "unrecoverable":
        return
    out = jsonmend.repair_json(case["input"], skip_json_loads=True)
    value = strict_loads(out)
    if case.get("check") == "valid":
        return
    if case["verdict"] == "deterministic":
        assert values_equal(value, case["expected"]), (
            "got %r, expected %r" % (value, case["expected"]))
    else:
        assert any(values_equal(value, acc) for acc in case["accepted"]), (
            "got %r, accepted %r" % (value, case["accepted"]))
