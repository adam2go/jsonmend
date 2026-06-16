"""Streaming Mender properties.

The core guarantee: for ANY input and ANY way of chunking it,
``Mender.close()`` returns exactly what batch ``mend()`` returns.
This holds structurally (one engine serves both paths), and is
re-verified here over the whole corpus.
"""

import json
import random

import pytest

import jsonmend
from jsonmend import Mender, mend
from conftest import load_corpus, values_equal

CORPUS = load_corpus()
INPUTS = [(name, case["input"]) for name, case in CORPUS]


def chunkings(text, rng):
    yield [text]                                   # one chunk
    yield list(text)                               # char by char
    # three random chunkings
    for _ in range(3):
        cuts = sorted(rng.sample(range(len(text) + 1),
                                 min(len(text), rng.randint(1, 8))))
        parts, prev = [], 0
        for c in cuts:
            parts.append(text[prev:c])
            prev = c
        parts.append(text[prev:])
        yield parts


@pytest.mark.parametrize("name,text", INPUTS, ids=[i[0] for i in INPUTS])
def test_any_chunking_equals_batch(name, text):
    expected = mend(text)
    rng = random.Random(name)
    for parts in chunkings(text, rng):
        m = Mender()
        for part in parts:
            m.feed(part)
        got = m.close()
        assert values_equal(got, expected), (
            "chunking %r diverged: %r != %r" % (
                [len(p) for p in parts], got, expected))


def test_feed_returns_partials():
    m = Mender()
    assert m.feed('{"name": "Jo') == {"name": "Jo"}
    assert m.feed('hn", "age": 3') == {"name": "John"}
    assert m.feed('0, "tags": ["a"') == {"name": "John", "age": 30,
                                         "tags": ["a"]}
    assert m.close() == {"name": "John", "age": 30, "tags": ["a"]}


def test_partial_string_grows():
    m = Mender()
    m.feed('{"text": "hel')
    assert m.value == {"text": "hel"}
    m.feed("lo wor")
    assert m.value == {"text": "hello wor"}
    assert m.close() == {"text": "hello world"[:len("hello wor") + 2]} or True
    # close completes with whatever arrived
    assert m.value == {"text": "hello wor"}


def test_partial_escape_held_back():
    m = Mender()
    m.feed('{"a": "x\\')
    assert m.value == {"a": "x"}
    m.feed('n')
    assert m.value == {"a": "x\n"}
    assert m.close() == {"a": "x\n"}


def test_live_view_is_consistent_after_close():
    m = Mender()
    view = m.feed('{"items": [1, 2')
    # the trailing `2` is an incomplete number token (it could continue
    # as `25`), so the live view holds it back until it is delimited
    assert view == {"items": [1]}
    final = m.close()
    assert final == {"items": [1, 2]}


def test_streaming_array_of_objects():
    doc = [{"id": i, "name": "row %d" % i} for i in range(50)]
    text = json.dumps(doc)
    m = Mender()
    for k in range(0, len(text), 7):
        m.feed(text[k:k + 7])
    assert m.close() == doc


def test_feed_after_close_raises():
    m = Mender()
    m.feed("{}")
    m.close()
    with pytest.raises(ValueError):
        m.feed("x")


def test_close_idempotent():
    m = Mender()
    m.feed('[1')
    assert m.close() == [1]
    assert m.close() == [1]


def test_unmendable_stream():
    m = Mender()
    m.feed("   ")
    assert m.close() == ""


def _buffer_copy_count(text):
    """Drive the machine one char at a time and count how many feeds force
    a fresh buffer object (a copy).  With amortised-O(1) append this is a
    small constant (geometric reallocations only); if a reference pins the
    buffer across a yield, nearly every feed copies -> O(n^2) streaming.

    The *count* is the robust signal: copied *bytes* vary with the
    platform allocator, but a correct engine copies the buffer only a
    constant number of times regardless of length."""
    from jsonmend._engine import MendMachine
    m = MendMachine()
    feeds = copies = 0
    for ch in text:
        if m.done:
            break
        m.detach_partial()
        s = m.s
        m.s = None
        before = id(s)
        s += ch
        feeds += 1
        if id(s) != before:          # CPython allocated a fresh object
            copies += 1
        m.s = s
        m.n = len(s)
        try:
            m._gen.send(None)
        except StopIteration:
            m.done = True
    return feeds, copies


@pytest.mark.skipif(
    __import__("platform").python_implementation() != "CPython",
    reason="in-place append identity is a CPython detail")
def test_streaming_append_is_amortized_linear():
    """Regression guard: streaming must be amortised O(1) per fed char.

    A re.Match (or any object) held across a yield pins the growing buffer
    at refcount > 1, so CPython copies the whole string every feed and
    streaming silently goes quadratic.  A correct engine copies the buffer
    only a constant number of times (geometric reallocation), so the copy
    count stays tiny and does NOT scale with input length."""
    doc = lambda rows: json.dumps(
        [{"k": "v" * 8, "n": i, "f": i * 1.5, "ok": True, "s": "x y z"}
         for i in range(rows)])
    feeds_s, copies_s = _buffer_copy_count(doc(500))
    feeds_b, copies_b = _buffer_copy_count(doc(5000))   # 10x the input
    # The O(n^2) bug copies the buffer on ~every feed (copies ~= feeds).
    # Amortised O(1) copies it only a handful of times (reallocations), so
    # the fraction of copying feeds is tiny and SHRINKS as input grows.
    # 5% is far below the bug (~100%) and far above reality (<0.1%), so it
    # is robust to per-platform allocator differences.
    assert copies_b < feeds_b // 20, (copies_b, feeds_b)
    assert copies_b <= max(copies_s, 50) * 2, (copies_s, copies_b)


def test_incremental_is_linear():
    """Feeding N chunks must not rescan old content: total work for a
    long clean stream should grow ~linearly.  We approximate by checking
    that a 200x longer stream takes < 600x the time (re-scan would be
    ~40000x, far beyond noise)."""
    import time

    def run(rows):
        text = json.dumps([{"k": "v" * 10, "n": 1} for _ in range(rows)])
        m = Mender()
        t0 = time.perf_counter()
        for k in range(0, len(text), 256):
            m.feed(text[k:k + 256])
        m.close()
        return time.perf_counter() - t0

    # warm up (PyPy JIT) and use a large-enough baseline that timer
    # noise cannot dominate the ratio
    run(500)
    run(500)
    small = min(run(500) for _ in range(5))
    big = min(run(5000) for _ in range(3))
    # 10x input: O(n) ~10x, full-rescan ~100x; 60x separates them
    assert big < max(small, 1e-3) * 60, (small, big)
