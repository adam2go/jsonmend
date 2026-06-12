"""jsonmend — mends the JSON your LLM almost wrote.

Batch API (drop-in for json_repair):

    from jsonmend import repair_json, loads, load, from_file

Streaming API (true incremental, O(new bytes) per feed):

    from jsonmend import Mender
    m = Mender()
    for chunk in stream:
        partial = m.feed(chunk)   # best-effort value so far
    value = m.close()
"""

from __future__ import annotations

import json as _json
import math as _math

from ._engine import SKIP, JSONMendError, MendMachine

__version__ = "0.1.1"

__all__ = [
    "repair_json", "loads", "load", "from_file",
    "mend", "Mender", "JSONMendError", "__version__",
]


def mend(text, *, strict=False, _doom_hint=None):
    """Repair ``text`` and return the parsed Python value.

    This always runs the repair machine (no ``json.loads`` fast path).
    Returns ``""`` for unmendable input, or raises :class:`JSONMendError`
    when ``strict`` is true.
    """
    if not isinstance(text, str):
        text = _coerce_text(text)
    if text and text[0] == "﻿":
        text = text.lstrip("﻿")
        _doom_hint = None
    machine = MendMachine()
    machine.final = True
    if _doom_hint is not None:
        machine.doomed_from = _doom_hint
    machine.feed(text)
    result = machine.close()
    if result is SKIP:
        if strict:
            raise JSONMendError("no JSON content found in input")
        return ""
    return result


def loads(json_str, *, skip_json_loads=False, strict=False, **_compat):
    """Repair and parse, returning Python objects.

    Valid JSON takes a C-speed ``json.loads`` fast path unless
    ``skip_json_loads`` is true.
    """
    if not isinstance(json_str, str):
        json_str = _coerce_text(json_str)
    if not skip_json_loads:
        try:
            return _json.loads(json_str)
        except Exception as e:
            p = getattr(e, "pos", None)
            if p is not None and p >= len(json_str):
                # truncated input: the machine need not rescan the root
                return mend(json_str, strict=strict, _doom_hint=p)
    return mend(json_str, strict=strict)


def repair_json(json_str="", return_objects=False, skip_json_loads=False,
                ensure_ascii=True, strict=False, **json_dumps_args):
    """Repair broken JSON.  Returns a JSON string (or objects).

    API-compatible with ``json_repair.repair_json`` for the core
    parameters.  Unlike json_repair, the output is always *valid* JSON:
    non-finite numbers (NaN/Infinity) are serialized as ``null``.
    """
    if json_dumps_args.pop("logging", False):
        raise TypeError(
            "jsonmend does not support json_repair's logging=True "
            "(incompatible with single-pass repair); remove the flag")
    json_dumps_args.pop("stream_stable", None)  # Mender is always stable
    if not isinstance(json_str, str):
        json_str = _coerce_text(json_str)
    value = None
    hint = None
    if not skip_json_loads:
        try:
            value = _json.loads(json_str)
            parsed = True
        except Exception as e:
            parsed = False
            p = getattr(e, "pos", None)
            if p is not None and p >= len(json_str):
                hint = p
    else:
        parsed = False
    if not parsed:
        value = mend(json_str, strict=strict, _doom_hint=hint)
    if return_objects:
        return value
    return _dumps(value, ensure_ascii=ensure_ascii, **json_dumps_args)


def load(fd, **kwargs):
    """Repair and parse JSON from a file-like object."""
    return loads(fd.read(), **kwargs)


def from_file(filename, **kwargs):
    """Repair and parse JSON from a file path."""
    with open(filename, encoding="utf-8-sig", newline="") as fd:
        return loads(fd.read(), **kwargs)


class Mender:
    """Stateful incremental mender.

    Each :meth:`feed` consumes one chunk and returns the best-effort
    parsed value so far; the cost of a feed is proportional to the new
    bytes, not to everything fed so far.  The returned value is a *live
    view* that later feeds may extend in place; call :meth:`close` to get
    the final result.
    """

    def __init__(self):
        self._machine = MendMachine()
        self._closed = False
        self._result = None

    def feed(self, chunk):
        """Feed one chunk; returns the current best-effort value."""
        if self._closed:
            raise ValueError("Mender is closed")
        if not isinstance(chunk, str):
            chunk = _coerce_text(chunk)
        self._machine.feed(chunk)
        return self._machine.current()

    @property
    def value(self):
        """Current best-effort value without feeding."""
        if self._closed:
            return self._result
        return self._machine.current()

    def close(self):
        """Finish parsing and return the final mended value."""
        if not self._closed:
            result = self._machine.close()
            self._result = "" if result is SKIP else result
            self._closed = True
        return self._result


# ---------------------------------------------------------------------------
# serialization helpers
# ---------------------------------------------------------------------------


def _coerce_text(obj):
    if isinstance(obj, (bytes, bytearray)):
        return obj.decode("utf-8", errors="replace")
    return str(obj)


def _sanitize_nonfinite(value):
    """Replace NaN/Infinity floats with None (iterative, no recursion)."""
    if isinstance(value, float):
        return value if _math.isfinite(value) else None
    if not isinstance(value, (dict, list)):
        return value
    root = [] if isinstance(value, list) else {}
    todo = [(value, root)]
    while todo:
        src, dst = todo.pop()
        items = src.items() if isinstance(src, dict) else enumerate(src)
        for k, v in items:
            if isinstance(v, float) and not _math.isfinite(v):
                v = None
            elif isinstance(v, dict):
                new = {}
                todo.append((v, new))
                v = new
            elif isinstance(v, list):
                new = []
                todo.append((v, new))
                v = new
            if isinstance(dst, dict):
                dst[k] = v
            else:
                dst.append(v)
    return root


def _has_nonfinite(value):
    todo = [value]
    while todo:
        v = todo.pop()
        if isinstance(v, float):
            if not _math.isfinite(v):
                return True
        elif isinstance(v, dict):
            todo.extend(v.values())
        elif isinstance(v, list):
            todo.extend(v)
    return False


def _dumps(value, ensure_ascii=True, **kw):
    kw.setdefault("separators", (", ", ": "))
    try:
        return _json.dumps(value, ensure_ascii=ensure_ascii,
                           allow_nan=False, **kw)
    except ValueError:
        return _json.dumps(_sanitize_nonfinite(value),
                           ensure_ascii=ensure_ascii, **kw)
    except RecursionError:
        return _iter_dumps(value, ensure_ascii=ensure_ascii)


def _iter_dumps(value, ensure_ascii=True):
    """Iterative serializer for absurdly deep structures."""
    out = []
    enc = _json.encoder.encode_basestring_ascii if ensure_ascii \
        else _json.encoder.encode_basestring
    stack = [("v", value)]
    while stack:
        op, v = stack.pop()
        if op == "t":  # literal text
            out.append(v)
            continue
        if isinstance(v, dict):
            out.append("{")
            stack.append(("t", "}"))
            items = list(v.items())
            for idx in range(len(items) - 1, -1, -1):
                k, val = items[idx]
                stack.append(("v", val))
                stack.append(("t", enc(k) + ": "))
                if idx:
                    stack.append(("t", ", "))
            continue
        if isinstance(v, list):
            out.append("[")
            stack.append(("t", "]"))
            for idx in range(len(v) - 1, -1, -1):
                stack.append(("v", v[idx]))
                if idx:
                    stack.append(("t", ", "))
            continue
        if v is True:
            out.append("true")
        elif v is False:
            out.append("false")
        elif v is None:
            out.append("null")
        elif isinstance(v, str):
            out.append(enc(v))
        elif isinstance(v, float):
            out.append(repr(v) if _math.isfinite(v) else "null")
        else:
            out.append(str(v))
    return "".join(out)
