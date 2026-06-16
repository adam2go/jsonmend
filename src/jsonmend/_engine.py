"""jsonmend core engine.

A single resumable state machine repairs broken JSON.  The same code path
serves both batch repair (one feed, then close) and true incremental
streaming (many feeds): the parser is a generator that suspends ("needs
more input") whenever it reaches the end of the buffer before the input is
final.  Batch mode never suspends, so it pays no streaming tax.

Design rules (the jsonmend conformance semantics, see corpus/):

* No recursion anywhere: containers are parsed with an explicit stack, so
  100k-deep nesting cannot crash the parser.
* Strings are scanned with ``str.find`` (C speed) instead of per-character
  Python loops; a clean string costs one find and one slice.
* Bounded backtracking only: a string close decision may fall back to one
  previously recorded candidate quote, never a full rescan.
"""

from __future__ import annotations

import json as _json
import re as _re

__all__ = ["JSONMendError", "MendMachine", "SKIP"]


class JSONMendError(ValueError):
    """Raised in strict mode when input contains nothing mendable."""


# ---------------------------------------------------------------------------
# Character tables
# ---------------------------------------------------------------------------

_WS = frozenset(
    " \t\n\r\x0b\x0c\x85        "
    "     ​    　"
    "﻿᠎"
)

# open quote -> acceptable closing quotes
_QUOTES = {
    '"': '"',
    "'": "'",
    "“": '”"',
    "‘": "’'",
    "`": "´`'",
    "«": "»",
}

_LITERALS = {
    "true": True, "True": True, "TRUE": True,
    "false": False, "False": False, "FALSE": False,
    "null": None, "Null": None, "NULL": None,
    "None": None, "none": None, "undefined": None, "nil": None,
    "NaN": float("nan"), "nan": float("nan"),
    "Infinity": float("inf"), "inf": float("inf"),
}

_NUM_RE = _re.compile(r"[-+]?(?:\d[\d_]*(?:\.[\d_]*)?|\.\d[\d_]*)(?:[eE][+-]?\d*)?")
_WORD_RE = _re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")
_LEADING_ZERO_RE = _re.compile(r"[-+]?0\d")

_ESC_MAP = {
    '"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f",
    "n": "\n", "r": "\r", "t": "\t", "'": "'", "\n": "\n",
}
_ESC_RE = _re.compile(r"\\(u[0-9a-fA-F]{0,4}|.|$)", _re.S)
_SURR_RE = _re.compile("[\ud800-\udfff]")
_PAIR_RE = _re.compile("[\ud800-\udbff][\udc00-\udfff]")
_PARTIAL_U_RE = _re.compile(r"\\u[0-9a-fA-F]{0,3}$")

# contexts
_TOP, _OKEY, _OVAL, _ARR = 0, 1, 2, 3

SKIP = object()  # "no value produced"

# characters that may legitimately follow a closed string (fast path).
# '+' (concat) is deliberately absent: it needs the slow path.
_STR_DELIMS = frozenset(',}]):')
_HIGH_SURR_RE = _re.compile(r"\\u[dD][89abAB][0-9a-fA-F]{2}$")


def _reject_constant(name):
    raise ValueError(name)


# speculative C-speed decoder for complete, clean sub-values.  It must
# reject anything the mending machine handles differently: non-finite
# constants (parse_constant) and surrogate escapes (input guard below).
_SPEC_DECODER = _json.JSONDecoder(parse_constant=_reject_constant)
_SPEC_GUARD_RE = _re.compile("[\ud800-\udfff]|\\\\[uU][dD][89a-fA-F]")


def _decode_escapes(raw):
    """Decode backslash escapes; tolerate broken ones; never raise."""
    def repl(m):
        g = m.group(1)
        if not g:
            return ""  # lone trailing backslash
        if g[0] == "u":
            h = g[1:]
            if len(h) == 4:
                return chr(int(h, 16))
            return ""  # truncated \uXX -> drop
        return _ESC_MAP.get(g, g)  # unknown escape: drop the backslash

    out = _ESC_RE.sub(repl, raw)
    if _SURR_RE.search(out):
        # combine surrogate pairs produced by 😀 style escapes,
        # replace lone surrogates so the result is always UTF-8 encodable
        out = _PAIR_RE.sub(
            lambda m: chr(0x10000 + ((ord(m.group()[0]) - 0xD800) << 10)
                          + (ord(m.group()[1]) - 0xDC00)),
            out,
        )
        out = _SURR_RE.sub("�", out)
    return out


def _strip_partial_escape(raw):
    """Trim an incomplete trailing escape (for truncated strings)."""
    k = len(raw)
    b = 0
    while b < k and raw[k - 1 - b] == "\\":
        b += 1
    if b % 2 == 1:
        return raw[:-1]
    m = _PARTIAL_U_RE.search(raw)
    if m is None:
        # a complete high-surrogate escape whose pair was cut off
        m = _HIGH_SURR_RE.search(raw)
    if m:
        j = m.start()
        b = 0
        while j - 1 - b >= 0 and raw[j - 1 - b] == "\\":
            b += 1
        if b % 2 == 0:
            return raw[:j]
    return raw


def _finish_string(raw):
    if "\\" in raw:
        return _decode_escapes(raw)
    return raw


def _to_key(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if v is None:
        return "null"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


class _Frame:
    __slots__ = ("kind", "container", "key", "eager")

    def __init__(self, kind, container):
        self.kind = kind          # 'o' | 'a' | 'p'
        self.container = container
        self.key = None
        self.eager = False  # already attached to the parent container


class MendMachine:
    """Resumable mending machine.  Drives both batch and streaming APIs."""

    __slots__ = ("s", "n", "final", "stack", "values", "prose", "done",
                 "result", "had_nonfinite", "partial", "partial_end",
                 "_undo", "_gen", "_spec", "_spec_fails", "doomed_from")

    def __init__(self):
        self.s = ""
        self.n = 0
        self.final = False
        self.stack = []
        self.values = []          # completed top-level values
        self.prose = []           # pending top-level prose lines
        self.done = False
        self.result = SKIP
        self.had_nonfinite = False
        self.partial = None       # (container|None, start) for snapshots
        self.partial_end = None   # display end for a pending candidate
        self._spec = None         # speculative raw_decode: None=unknown
        self._spec_fails = 0
        self.doomed_from = None   # raw_decode is known to fail before here
        self._undo = None
        self._gen = self._run()
        next(self._gen)           # prime to the first suspension point

    # -- driving -----------------------------------------------------------

    def feed(self, chunk):
        if self.done:
            return
        self.detach_partial()
        # Grow the buffer via a *local* so CPython appends in place
        # (amortised O(1)); `self.s += chunk` would keep a second
        # reference live and copy the whole buffer every feed -> O(n^2).
        # The generator's suspension idiom (`s = None`/`m = None` before
        # every yield) keeps this the only live reference.
        s = self.s
        self.s = None
        s += chunk
        self.s = s
        self.n = len(s)
        try:
            self._gen.send(None)
        except StopIteration:
            self.done = True

    def close(self):
        if not self.done:
            self.detach_partial()
            self.final = True
            try:
                self._gen.send(None)
            except StopIteration:
                pass
            self.done = True
            self._gen = None
        return self.result

    # -- snapshots (streaming) ----------------------------------------------

    def attach_partial(self):
        """Temporarily attach the in-progress string to the live tree.

        Returns a top-level partial value when there is no container yet.
        """
        p = self.partial
        if p is None:
            return SKIP
        container, start = p
        end = self.partial_end if self.partial_end is not None else self.n
        raw = _strip_partial_escape(self.s[start:end])
        text = _finish_string(raw)
        if container is None:
            return text
        if isinstance(container, dict):
            key = None
            for fr in reversed(self.stack):
                if fr.container is container:
                    key = fr.key
                    break
            if key is None:
                return SKIP
            self._undo = (container, key, key in container,
                          container.get(key))
            container[key] = text
        else:
            self._undo = (container, len(container), False, None)
            container.append(text)
        return SKIP

    def detach_partial(self):
        u = self._undo
        if u is None:
            return
        container, slot, existed, prev = u
        if isinstance(container, dict):
            if existed:
                container[slot] = prev
            else:
                container.pop(slot, None)
        else:
            if len(container) == slot + 1:
                container.pop()
        self._undo = None

    def current(self):
        """Best-effort value right now."""
        if self.done:
            return None if self.result is SKIP else self.result
        vals = list(self.values)
        if self.stack:
            vals.append(self.stack[0].container)
        extra = self.attach_partial()
        if extra is not SKIP and not self.stack:
            vals.append(extra)
        if not vals:
            return None
        if len(vals) == 1:
            return vals[0]
        return vals

    # ------------------------------------------------------------------
    # The machine.  One generator; `yield` means "need more input".
    # ------------------------------------------------------------------

    def _run(self):
        ws = _WS
        quotes = _QUOTES
        word_re = _WORD_RE

        stack = self.stack
        values = self.values
        prose = self.prose

        s = self.s
        n = self.n
        i = 0
        c = ""
        fence_seen = False
        in_fence = False
        wrapper_depth = 0

        value = SKIP
        # mode: 0 top junk/value | 1 expect value | 2 have value
        #       3 object expects key | 4 object after member
        #       5 array after element
        mode = 0

        while True:
            # ---------------------------------------------- ws + comments
            while True:
                while i < n and s[i] in ws:
                    i += 1
                if i >= n:
                    # mode 2 (attach a finished value) needs no new bytes;
                    # let it run so snapshots see the value immediately
                    if self.final or mode == 2:
                        break
                    s = None
                    yield
                    s = self.s
                    n = self.n
                    continue
                c = s[i]
                if c == "/":
                    while i + 1 >= n and not self.final:
                        s = None
                        yield
                        s = self.s
                        n = self.n
                    nxt = s[i + 1] if i + 1 < n else ""
                    if nxt == "/":
                        j = s.find("\n", i + 2)
                        while j == -1 and not self.final:
                            i = n
                            s = None
                            yield
                            s = self.s
                            n = self.n
                            j = s.find("\n", i)
                        i = n if j == -1 else j + 1
                        continue
                    if nxt == "*":
                        j = s.find("*/", i + 2)
                        while j == -1 and not self.final:
                            s = None
                            yield
                            s = self.s
                            n = self.n
                            j = s.find("*/", i + 2)
                        i = n if j == -1 else j + 2
                        continue
                    break
                if c == "#" and mode != 0:
                    j = s.find("\n", i + 1)
                    while j == -1 and not self.final:
                        s = None
                        yield
                        s = self.s
                        n = self.n
                        j = s.find("\n", i)
                    i = n if j == -1 else j + 1
                    continue
                if c == "." and (mode in (3, 4, 5) or
                                 (mode == 1 and i + 1 < n and s[i + 1] == ".")):
                    # ellipsis token at structural positions
                    j = i
                    while True:
                        while j < n and s[j] == ".":
                            j += 1
                        if j < n or self.final:
                            break
                        s = None
                        yield
                        s = self.s
                        n = self.n
                    if j - i >= 2:
                        i = j
                        continue
                    break
                break

            at_eof = i >= n
            if at_eof and mode == 0:
                break

            # ==================================================== dispatch
            if mode == 1:
                # --------------------------------------- expecting a value
                in_obj = bool(stack) and stack[-1].kind == "o"
                if at_eof:
                    value = None if (in_obj and stack[-1].key is not None) \
                        else SKIP
                    mode = 2
                    continue
                if c == "{" or c == "[" or c == "(":
                    if c != "(" and self.final and self._spec is not False:
                        if self._spec or self._spec_ok():
                            smode, payload, i2 = self._speculate(i)
                            if smode == 2:
                                value = payload
                                i = i2
                                mode = 2
                                continue
                            if smode:
                                nf = payload
                                if stack:
                                    par = stack[-1]
                                    if par.kind == "o":
                                        if par.key is not None:
                                            par.container[par.key] = \
                                                nf.container
                                            nf.eager = True
                                    else:
                                        par.container.append(nf.container)
                                        nf.eager = True
                                stack.append(nf)
                                i = i2
                                mode = smode
                                continue
                    child = {} if c == "{" else []
                    nf = _Frame("o" if c == "{" else
                                ("a" if c == "[" else "p"), child)
                    if stack:
                        par = stack[-1]
                        if par.kind == "o":
                            if par.key is not None:
                                par.container[par.key] = child
                                nf.eager = True
                        else:
                            par.container.append(child)
                            nf.eager = True
                    stack.append(nf)
                    i += 1
                    mode = 3 if c == "{" else 1
                    continue
                if c in "}],":
                    value = None if (in_obj and stack[-1].key is not None) \
                        else SKIP
                    mode = 2
                    continue
                # ---- inline fast path: simple number
                if c.isdigit() or c == "-":
                    m = _NUM_RE.match(s, i)
                    k = m.end() if m else i
                    m = None  # don't let the Match outlive this block and
                    # pin the stream buffer across the _scalar yield below
                    if k < n and (k > i):
                        nc = s[k]
                        if nc in ",}]" or nc in ws:
                            tok = s[i:k]
                            if tok[-1].isdigit() and "_" not in tok and \
                                    not _LEADING_ZERO_RE.match(tok):
                                if "." in tok or "e" in tok or "E" in tok:
                                    value = float(tok)
                                else:
                                    value = int(tok)
                                i = k
                                if stack:
                                    fr = stack[-1]
                                    if fr.kind == "o":
                                        if fr.key is not None:
                                            fr.container[fr.key] = value
                                            fr.key = None
                                        value = SKIP
                                        mode = 4
                                    else:
                                        fr.container.append(value)
                                        value = SKIP
                                        mode = 5
                                    while i < n and s[i] in " \t":
                                        i += 1
                                    if i < n and s[i] == ",":
                                        i += 1
                                        mode = 3 if mode == 4 else 1
                                    continue
                                mode = 2
                                continue
                # ---- inline fast path: literals
                if c == "t" and s.startswith("true", i) and i + 4 < n and \
                        s[i + 4] in ",}]":
                    value = True
                    i += 4
                    mode = 2
                    continue
                if c == "f" and s.startswith("false", i) and i + 5 < n and \
                        s[i + 5] in ",}]":
                    value = False
                    i += 5
                    mode = 2
                    continue
                if c == "n" and s.startswith("null", i) and i + 4 < n and \
                        s[i + 4] in ",}]":
                    value = None
                    i += 4
                    mode = 2
                    continue
                if c == "T" and s.startswith("True", i) and i + 4 < n and \
                        s[i + 4] in ",}]":
                    value = True
                    i += 4
                    mode = 2
                    continue
                if c == "F" and s.startswith("False", i) and i + 5 < n and \
                        s[i + 5] in ",}]":
                    value = False
                    i += 5
                    mode = 2
                    continue
                if c == "N" and s.startswith("None", i) and i + 4 < n and \
                        s[i + 4] in ",}]":
                    value = None
                    i += 4
                    mode = 2
                    continue
                # ---- inline fast path: clean quoted string
                if c == '"' or c == "'":
                    j = s.find(c, i + 1)
                    if j != -1 and j + 1 < n:
                        d = s[j + 1]
                        if (d in _STR_DELIMS or d in ws) and \
                                s.find("\\", i + 1, j) == -1 and \
                                s.find("\n", i + 1, j) == -1:
                            if d in ws:
                                # confirm next non-ws is a delimiter
                                p = j + 1
                                while p < n and s[p] in ws:
                                    p += 1
                                ok = (p >= n and self.final) or \
                                     (p < n and s[p] in _STR_DELIMS)
                            else:
                                ok = True
                            if ok:
                                value = s[i + 1:j]
                                i = j + 1
                                if stack:
                                    fr = stack[-1]
                                    if fr.kind == "o":
                                        if fr.key is not None:
                                            fr.container[fr.key] = value
                                            fr.key = None
                                        value = SKIP
                                        mode = 4
                                    else:
                                        fr.container.append(value)
                                        value = SKIP
                                        mode = 5
                                    while i < n and s[i] in " \t":
                                        i += 1
                                    if i < n and s[i] == ",":
                                        i += 1
                                        mode = 3 if mode == 4 else 1
                                    continue
                                mode = 2
                                continue
                # scalar (string / number / literal / unquoted)
                ctx = _TOP if not stack else (
                    _OVAL if stack[-1].kind == "o" else _ARR)
                pos = i
                s = None
                value, i = yield from self._scalar(pos, ctx)
                s = self.s
                n = self.n
                mode = 2
                continue

            if mode == 3:
                # -------------------------------------- object expects key
                fr = stack[-1]
                if at_eof:
                    stack.pop()
                    value = self._pop_value(fr)
                    mode = 2
                    continue
                if c == "}":
                    i += 1
                    stack.pop()
                    value = self._pop_value(fr)
                    mode = 2
                    continue
                if c == ",":
                    i += 1
                    continue
                if c == "]":
                    if not any(f.kind != "o" for f in stack[:-1]):
                        i += 1
                    stack.pop()
                    value = self._pop_value(fr)
                    mode = 2
                    continue
                if c == "[" and fr.container:
                    # LLM split-array error: `"k": [...], [...]` — merge
                    last_key = next(reversed(fr.container))
                    if isinstance(fr.container[last_key], list):
                        fr.key = last_key
                        nf = _Frame("a", fr.container[last_key])
                        nf.eager = True
                        stack.append(nf)
                        i += 1
                        mode = 1
                        continue
                    stack.pop()
                    value = self._pop_value(fr)
                    mode = 2
                    continue
                if c == "{" or c == "[":
                    stack.pop()
                    value = self._pop_value(fr)
                    mode = 2
                    continue
                if c == ")" and wrapper_depth:
                    i += 1
                    wrapper_depth -= 1
                    continue
                if c == ":":
                    i += 1
                    fr.key = None
                    mode = 1
                    continue
                # ---- inline fast path: bare `key:`
                if (c.isalpha() or c == "_") and c.isascii():
                    m = _WORD_RE.match(s, i)
                    k2 = m.end()
                    if k2 < n:
                        e2 = k2
                        while e2 < n and s[e2] in " \t":
                            e2 += 1
                        if e2 < n and s[e2] == ":":
                            fr.key = s[i:k2]
                            i = e2 + 1
                            mode = 1
                            continue
                # ---- inline fast path: clean `"key":`
                if c == '"':
                    j = s.find('"', i + 1)
                    if j != -1 and \
                            s.find("\\", i + 1, j) == -1 and \
                            s.find("\n", i + 1, j) == -1:
                        k2 = j + 1
                        while k2 < n and s[k2] in " \t":
                            k2 += 1
                        if k2 < n and s[k2] == ":":
                            fr.key = s[i + 1:j]
                            i = k2 + 1
                            mode = 1
                            continue
                pos = i
                s = None
                key, i = yield from self._key(pos)
                s = self.s
                n = self.n
                if key is SKIP:
                    if i < n and s[i] not in "}]":
                        i += 1
                    continue
                fr.key = key
                while True:
                    while i < n and s[i] in ws:
                        i += 1
                    if i >= n and not self.final:
                        s = None
                        yield
                        s = self.s
                        n = self.n
                        continue
                    break
                if i < n and s[i] == '"':
                    # stray quote between key and colon (`{""a"": 1}`)
                    p = i + 1
                    while True:
                        while p < n and s[p] in ws:
                            p += 1
                        if p >= n and not self.final:
                            s = None
                            yield
                            s = self.s
                            n = self.n
                            continue
                        break
                    if p < n and (s[p] == ":" or s[p] == "："):
                        i = p
                if i < n and (s[i] == ":" or s[i] == "："):
                    i += 1
                elif i < n and s[i] == "=":
                    i += 1
                    if i < n and s[i] == ">":
                        i += 1
                elif i >= n or s[i] in ",}":
                    fr.container[key] = None
                    fr.key = None
                    mode = 4
                    continue
                mode = 1
                continue

            if mode == 4:
                # ------------------------------------- object after member
                fr = stack[-1]
                if at_eof:
                    stack.pop()
                    value = self._pop_value(fr)
                    mode = 2
                    continue
                if c == ",":
                    i += 1
                    mode = 3
                    continue
                if c == "}":
                    i += 1
                    stack.pop()
                    value = self._pop_value(fr)
                    mode = 2
                    continue
                if c == "]":
                    if not any(f.kind != "o" for f in stack[:-1]):
                        i += 1
                    stack.pop()
                    value = self._pop_value(fr)
                    mode = 2
                    continue
                if c == ";":
                    i += 1
                    mode = 3
                    continue
                if c == ")" and wrapper_depth:
                    i += 1
                    wrapper_depth -= 1
                    continue
                if c == "{" or c == "[":
                    stack.pop()
                    value = self._pop_value(fr)
                    mode = 2
                    continue
                mode = 3
                continue

            if mode == 5:
                fr = stack[-1]
                if at_eof:
                    stack.pop()
                    value = self._pop_value(fr)
                    mode = 2
                    continue
                if c == ",":
                    i += 1
                    mode = 1
                    continue
                if c == "]":
                    i += 1
                    stack.pop()
                    value = self._pop_value(fr)
                    mode = 2
                    continue
                if c == ")" and fr.kind == "p":
                    i += 1
                    stack.pop()
                    value = self._pop_value(fr)
                    mode = 2
                    continue
                if c == "}":
                    if any(f.kind == "o" for f in stack[:-1]):
                        stack.pop()
                        value = self._pop_value(fr)
                        mode = 2
                    else:
                        # stray closer inside an array is junk
                        i += 1
                    continue
                if c == ":" and fr.container:
                    i += 1
                    key = fr.container.pop()
                    obj = {}
                    nf = _Frame("o", obj)
                    nf.key = key if isinstance(key, str) else _to_key(key)
                    fr.container.append(obj)
                    nf.eager = True
                    stack.append(nf)
                    mode = 1
                    continue
                if c == ";":
                    i += 1
                    mode = 1
                    continue
                mode = 1
                continue

            if mode == 2:
                # ------------------------------------------- have a value
                if not stack:
                    if value is not SKIP and isinstance(value, str):
                        # `"key": ...` — a headless object body
                        j = i
                        while True:
                            while j < n and s[j] in ws:
                                j += 1
                            if j >= n and not self.final:
                                s = None
                                yield
                                s = self.s
                                n = self.n
                                continue
                            break
                        if j < n and s[j] == ":":
                            nf = _Frame("o", {})
                            nf.key = value
                            stack.append(nf)
                            value = SKIP
                            i = j + 1
                            mode = 1
                            continue
                    while wrapper_depth:
                        # consume closing `)` / `;` of a wrapper call
                        while True:
                            while i < n and s[i] in ws:
                                i += 1
                            if i >= n and not self.final:
                                s = None
                                yield
                                s = self.s
                                n = self.n
                                continue
                            break
                        if i < n and s[i] == ")":
                            i += 1
                        wrapper_depth -= 1
                    if value is not SKIP:
                        values.append(value)
                        if isinstance(value, dict):
                            # `}, "key": ...` — object continuation
                            j = i
                            while True:
                                while j < n and s[j] in ws:
                                    j += 1
                                if j >= n and not self.final:
                                    s = None
                                    yield
                                    s = self.s
                                    n = self.n
                                    continue
                                break
                            if j < n and s[j] == ",":
                                j += 1
                                while True:
                                    while j < n and s[j] in ws:
                                        j += 1
                                    if j >= n and not self.final:
                                        s = None
                                        yield
                                        s = self.s
                                        n = self.n
                                        continue
                                    break
                                ok = False
                                if j < n and s[j] in quotes:
                                    qc = _QUOTES[s[j]]
                                    while True:
                                        e = -1
                                        for cc in qc:
                                            f = s.find(cc, j + 1)
                                            if f != -1 and (e == -1 or
                                                            f < e):
                                                e = f
                                        if e == -1 and not self.final:
                                            s = None
                                            yield
                                            s = self.s
                                            n = self.n
                                            continue
                                        break
                                    probe = e + 1 if e != -1 else -1
                                elif j < n:
                                    mm = word_re.match(s, j)
                                    while mm and mm.end() >= n and \
                                            not self.final:
                                        s = None
                                        mm = None  # drop Match: keep stream buffer refcount-1 for O(1) append
                                        yield
                                        s = self.s
                                        n = self.n
                                        mm = word_re.match(s, j)
                                    probe = mm.end() if mm else -1
                                else:
                                    probe = -1
                                if probe != -1:
                                    while True:
                                        while probe < n and s[probe] in " \t":
                                            probe += 1
                                        if probe >= n and not self.final:
                                            s = None
                                            yield
                                            s = self.s
                                            n = self.n
                                            continue
                                        break
                                    if probe < n and s[probe] == ":":
                                        ok = True
                                if ok:
                                    values.pop()
                                    stack.append(_Frame("o", value))
                                    i = j
                                    mode = 3
                                    continue
                    value = SKIP
                    mode = 0
                    continue
                fr = stack[-1]
                if fr.kind == "o":
                    if value is not SKIP and fr.key is not None:
                        fr.container[fr.key] = value
                    fr.key = None
                    value = SKIP
                    mode = 4
                    continue
                if value is not SKIP:
                    fr.container.append(value)
                    value = SKIP
                mode = 5
                continue

            if mode == 0:
                # ------------------------------------------- top level
                if in_fence or c == "`":
                    while i + 3 > n and not self.final:
                        s = None
                        yield
                        s = self.s
                        n = self.n
                    if s.startswith("```", i):
                        i += 3
                        if in_fence:
                            in_fence = False
                        else:
                            fence_seen = True
                            in_fence = True
                            prose.clear()
                            m = word_re.match(s, i)
                            if m:
                                i = m.end()
                        continue
                    if c == "`":
                        i += 1
                        continue
                if fence_seen and not in_fence:
                    # after a fence closed, only more fences carry values
                    j = s.find("```", i)
                    while j == -1 and not self.final:
                        i = max(i, n - 2)
                        s = None
                        yield
                        s = self.s
                        n = self.n
                        j = s.find("```", i)
                    if j == -1:
                        break
                    i = j
                    continue
                if c == "{" or c == "[":
                    prose.clear()
                    if self.final and self._spec is not False:
                        if self._spec or self._spec_ok():
                            smode, payload, i2 = self._speculate(i)
                            if smode == 2:
                                value = payload
                                i = i2
                                mode = 2
                                continue
                            if smode:
                                stack.append(payload)
                                i = i2
                                mode = smode
                                continue
                    if c == "{":
                        stack.append(_Frame("o", {}))
                        mode = 3
                    else:
                        stack.append(_Frame("a", []))
                        mode = 1
                    i += 1
                    continue
                if c.isdigit() or c in "+-.−":
                    # `1. The user wants x.` — a numbered prose line is junk
                    m = _NUM_RE.match(s, i)
                    if m:
                        while m.end() >= n and not self.final:
                            s = None
                            m = None  # drop Match: keep stream buffer refcount-1 for O(1) append
                            yield
                            s = self.s
                            n = self.n
                            m = _NUM_RE.match(s, i)
                        k = m.end()
                        while k < n and s[k] in " \t":
                            k += 1
                        while k >= n and not self.final:
                            s = None
                            mm = None  # drop Match: keep stream buffer refcount-1 for O(1) append
                            yield
                            s = self.s
                            n = self.n
                        if k < n and (s[k].isalpha() or s[k] in "*_#"):
                            mm = _WORD_RE.match(s, k)
                            if not (mm and mm.group() in _LITERALS):
                                # prose line: skip it entirely
                                e = s.find("\n", k)
                                while e == -1 and not self.final:
                                    s = None
                                    yield
                                    s = self.s
                                    n = self.n
                                    e = s.find("\n", k)
                                if not values and not fence_seen and not stack:
                                    prose.append(
                                        s[i:e if e != -1 else n].strip())
                                i = n if e == -1 else e + 1
                                continue
                    prose.clear()
                    mode = 1
                    continue
                if c in quotes:
                    prose.clear()
                    mode = 1
                    continue
                if c == "\\":
                    while i + 1 >= n and not self.final:
                        s = None
                        yield
                        s = self.s
                        n = self.n
                    if i + 1 < n and s[i + 1] in quotes:
                        prose.clear()
                        mode = 1
                    else:
                        i += 1
                    continue
                if c in ",;:)}]=":
                    i += 1
                    continue
                m = word_re.match(s, i)
                if m:
                    while m.end() >= n and not self.final:
                        s = None
                        m = None  # drop Match: keep stream buffer refcount-1 for O(1) append
                        yield
                        s = self.s
                        n = self.n
                        m = word_re.match(s, i)
                    word = m.group()
                    k = m.end()
                    p = k
                    while True:
                        while p < n and s[p] in " \t":
                            p += 1
                        if p >= n and not self.final:
                            s = None
                            yield
                            s = self.s
                            n = self.n
                            continue
                        break
                    pc = s[p] if p < n else ""
                    if word in _LITERALS and (pc == "" or pc in ",]}\n"):
                        prose.clear()
                        mode = 1
                        continue
                    if pc == "(" and p == k:
                        # JSONP / MongoDB wrapper: ident( ... )
                        prose.clear()
                        i = k + 1
                        wrapper_depth += 1
                        mode = 1
                        continue
                    # prose: consume this line, stop early at structure
                    j = i
                    while True:
                        stop = n
                        for ch in ("\n", "{", "[", "`", '"'):
                            f = s.find(ch, j, stop)
                            if f != -1:
                                stop = f
                        if stop >= n and not self.final:
                            j = stop
                            s = None
                            yield
                            s = self.s
                            n = self.n
                            continue
                        break
                    if stop < n and s[stop] == '"':
                        # `abc"` — quote at end of a prose line is a stray
                        # closer, not the start of a value
                        p2 = stop + 1
                        while True:
                            while p2 < n and s[p2] in " \t":
                                p2 += 1
                            if p2 >= n and not self.final:
                                s = None
                                yield
                                s = self.s
                                n = self.n
                                continue
                            break
                        if p2 >= n or s[p2] == "\n":
                            if not values and not fence_seen and not stack:
                                prose.append(s[i:stop].strip())
                            i = p2
                            continue
                    if not values and not fence_seen and not stack:
                        prose.append(s[i:stop].strip())
                    i = stop
                    if i < n and s[i] == "\n":
                        i += 1
                    continue
                if c == "(":
                    # tuple gate: must close on this line w/o trailing prose
                    eol = s.find("\n", i)
                    while eol == -1 and not self.final:
                        s = None
                        yield
                        s = self.s
                        n = self.n
                        eol = s.find("\n", i)
                    bound = n if eol == -1 else eol
                    close = s.rfind(")", i, bound)
                    is_value = (close != -1 and
                                s[close + 1:bound].strip() == "") or \
                               (close == -1 and eol == -1)
                    if is_value:
                        prose.clear()
                        stack.append(_Frame("p", []))
                        i += 1
                        mode = 1
                        continue
                    if not values and not fence_seen:
                        prose.append(s[i:bound].strip())
                    i = bound + 1 if bound < n else n
                    continue
                i += 1
                continue

        # ----------------------------------------------------------- EOF
        while stack:
            fr = stack.pop()
            if fr.kind == "o" and fr.key is not None and \
                    fr.key not in fr.container:
                fr.container[fr.key] = None
            v = self._pop_value(fr)
            if v is SKIP:
                if stack and stack[-1].kind == "o":
                    stack[-1].key = None
                continue
            if stack:
                top = stack[-1]
                if top.kind == "o":
                    if top.key is not None:
                        top.container[top.key] = v
                        top.key = None
                else:
                    top.container.append(v)
            else:
                values.append(v)

        if values:
            self.result = values[0] if len(values) == 1 else list(values)
        elif prose:
            text = "\n".join(x for x in prose if x)
            self.result = text if text else SKIP
        else:
            self.result = SKIP


    # ------------------------------------------------------------------

    def _spec_ok(self):
        """Decide once whether speculative raw_decode is semantically safe
        for this input (no surrogate material anywhere)."""
        s = self.s
        if "\\ud" in s or "\\uD" in s:
            ok = False
        else:
            try:
                s.encode("utf-8")
                ok = True
            except UnicodeEncodeError:
                ok = False
        self._spec = ok
        return ok

    # ------------------------------------------------------------------

    def _speculate(self, i):
        """Try to parse a complete sub-value at C speed; on failure try to
        salvage the longest clean prefix of the container.

        Returns (mode, payload, new_i): mode 2 -> payload is a complete
        value; mode 4/5 -> payload is a partially-filled frame to resume
        (object/array); mode 0 -> speculation failed.
        """
        s = self.s
        doom = self.doomed_from
        if doom is not None and i <= doom:
            # an enclosing attempt already failed past this point: a fresh
            # raw_decode would rescan and fail at the same place
            pos = doom
        else:
            try:
                v, end = _SPEC_DECODER.raw_decode(s, i)
                self._spec_fails = 0
                return 2, v, end
            except RecursionError:
                self._spec_fails += 1
                if self._spec_fails >= 8:
                    self._spec = False
                return 0, SKIP, i
            except ValueError as e:
                pos = getattr(e, "pos", None)
                self._spec_fails += 1
                if self._spec_fails >= 8:
                    self._spec = False
                if pos is None:
                    return 0, SKIP, i
                self.doomed_from = pos
        # prefix salvage: a cut at a structural comma of *this* container
        # is the only cut that parses cleanly (any cut inside a nested
        # value leaves something unclosed), so a successful parse below
        # is exactly machine-equivalent.  Failed attempts cost a full
        # C scan, so only a few high-probability cut points are tried.
        bound = min(pos, self.n)
        if bound - i < 256:
            return 0, SKIP, i
        c = s[i]
        closer = "}" if c == "{" else "]"
        cands = []
        for pat in ("},", "],", '",'):
            k = s.rfind(pat, i, bound)
            if k != -1:
                cands.append(k + 1)
        k = s.rfind(",", i, bound)
        if k != -1 and k not in cands:
            cands.append(k)
        cands.sort(reverse=True)
        need_brace = 1 if c == "{" else 0
        need_brack = 1 - need_brace
        for k in cands[:3]:
            # cheap balance pre-filter before paying for a C parse
            if s.count('"', i, k) % 2:
                continue
            if s.count("{", i, k) - s.count("}", i, k) != need_brace:
                continue
            if s.count("[", i, k) - s.count("]", i, k) != need_brack:
                continue
            try:
                v = _SPEC_DECODER.decode(s[i:k] + closer)
            except (ValueError, RecursionError):
                continue
            fr = _Frame("o" if c == "{" else "a", v)
            self._spec_fails = 0
            return (4 if c == "{" else 5), fr, k
        return 0, SKIP, i

    # ------------------------------------------------------------------

    def _pop_value(self, fr):
        """Value for a just-popped frame; SKIP when it is already attached
        to its parent (eager attach for streaming snapshots)."""
        v = fr.container if fr.kind == "o" else _close_seq(fr)
        if not fr.eager:
            return v
        if v is fr.container:
            return SKIP
        # a paren group collapsed to a scalar: fix the parent slot
        stack = self.stack
        if not stack:
            return v
        par = stack[-1]
        if par.kind == "o":
            if par.key is not None:
                par.container[par.key] = v
        elif par.container and par.container[-1] is fr.container:
            par.container[-1] = v
        return SKIP

    # ------------------------------------------------------------------
    # scalar sub-machines
    # ------------------------------------------------------------------

    def _scalar(self, i, ctx):
        """Parse one scalar value at position i.  Returns (value, new_i)."""
        s = self.s
        n = self.n
        wrapped = 0
        while True:
            c = s[i]
            if c in _QUOTES:
                s = None
                value, i = yield from self._string(i, ctx)
                s = self.s
                n = self.n
                # string concatenation: "a" + "b"
                while True:
                    j = i
                    while True:
                        while j < n and s[j] in _WS:
                            j += 1
                        if j >= n and not self.final:
                            s = None
                            yield
                            s = self.s
                            n = self.n
                            continue
                        break
                    if j < n and s[j] == "+":
                        j += 1
                        while True:
                            while j < n and s[j] in _WS:
                                j += 1
                            if j >= n and not self.final:
                                s = None
                                yield
                                s = self.s
                                n = self.n
                                continue
                            if j + 1 < n and s[j] == "/" and s[j + 1] == "*":
                                e = s.find("*/", j + 2)
                                if e == -1 and not self.final:
                                    s = None
                                    yield
                                    s = self.s
                                    n = self.n
                                    continue
                                if e != -1:
                                    j = e + 2
                                    continue
                            if j < n and s[j] == "/" and not self.final \
                                    and j + 1 >= n:
                                s = None
                                yield
                                s = self.s
                                n = self.n
                                continue
                            break
                        if j < n and s[j] in _QUOTES and isinstance(value, str):
                            s = None
                            more, i = yield from self._string(j, ctx)
                            s = self.s
                            n = self.n
                            value = value + more
                            continue
                        if j >= n:
                            i = j
                    break
                break
            if c == "\\":
                while i + 1 >= n and not self.final:
                    s = None
                    yield
                    s = self.s
                    n = self.n
                if i + 1 < n and s[i + 1] in _QUOTES:
                    s = None
                    value, i = yield from self._string(i + 1, ctx,
                                                       esc_delim=True)
                    s = self.s
                    n = self.n
                    break
                i += 1
                value = SKIP
                break
            if c == "−":  # unicode minus
                c = "-"
                s = s[:i] + "-" + s[i + 1:]
                self.s = s
            if c.isdigit() or c in "+-.":
                m = _NUM_RE.match(s, i)
                while m is None and i + 1 >= n and not self.final:
                    # a sign/dot at the buffer edge may grow into a number
                    s = None
                    m = None  # drop Match: keep stream buffer refcount-1 for O(1) append
                    yield
                    s = self.s
                    n = self.n
                    m = _NUM_RE.match(s, i)
                if m:
                    while m.end() >= n and not self.final:
                        s = None
                        m = None  # drop Match: keep stream buffer refcount-1 for O(1) append
                        yield
                        s = self.s
                        n = self.n
                        m = _NUM_RE.match(s, i)
                    tok = m.group()
                    k = m.end()
                    nc = s[k] if k < n else ""
                    if nc == "" or nc in _WS or nc in ',}]);{[':
                        value = _convert_number(tok)
                        if value is None:
                            value = tok
                        elif value is _NONFINITE:
                            self.had_nonfinite = True
                            value = float(tok.replace("_", ""))
                        i = k
                        break
                    if nc == '"':
                        v = _convert_number(tok)
                        if v is not None and v is not _NONFINITE:
                            # stray closing quote after a number: consume it
                            value = v
                            i = k + 1
                            break
                    # not a clean number: fall through to unquoted string
                    s = None
                    value, i = yield from self._unquoted(i, ctx)
                    s = self.s
                    n = self.n
                    break
                # sign + word (-Infinity) or lone sign
                m = _WORD_RE.match(s, i + 1)
                while m is None and i + 1 >= n and not self.final:
                    s = None
                    m = None  # drop Match: keep stream buffer refcount-1 for O(1) append
                    yield
                    s = self.s
                    n = self.n
                    m = _WORD_RE.match(s, i + 1)
                if m and c in "+-":
                    while m.end() >= n and not self.final:
                        s = None
                        m = None  # drop Match: keep stream buffer refcount-1 for O(1) append
                        yield
                        s = self.s
                        n = self.n
                        m = _WORD_RE.match(s, i + 1)
                    word = m.group()
                    lit = _LITERALS.get(word, SKIP)
                    if isinstance(lit, float):
                        self.had_nonfinite = True
                        value = -lit if c == "-" else lit
                        i = m.end()
                        break
                i += 1
                value = SKIP
                break
            m = _WORD_RE.match(s, i)
            if m:
                while m.end() >= n and not self.final:
                    s = None
                    m = None  # drop Match: keep stream buffer refcount-1 for O(1) append
                    yield
                    s = self.s
                    n = self.n
                    m = _WORD_RE.match(s, i)
                word = m.group()
                k = m.end()
                nc = s[k] if k < n else ""
                if word in _LITERALS:
                    # peek: literal must be followed by a delimiter
                    p = k
                    while True:
                        while p < n and s[p] in _WS:
                            p += 1
                        if p >= n and not self.final:
                            s = None
                            yield
                            s = self.s
                            n = self.n
                            continue
                        break
                    pc = s[p] if p < n else ""
                    if pc == "" or pc in ',}])"' or pc == ":" and ctx == _ARR:
                        value = _LITERALS[word]
                        if isinstance(value, float):
                            self.had_nonfinite = True
                        i = k
                        break
                if nc == "(":
                    # ident( ... ) wrapper around a scalar
                    i = k + 1
                    wrapped += 1
                    while True:
                        while i < n and s[i] in _WS:
                            i += 1
                        if i >= n and not self.final:
                            s = None
                            yield
                            s = self.s
                            n = self.n
                            continue
                        break
                    if i >= n:
                        value = SKIP
                        break
                    continue
                s = None
                value, i = yield from self._unquoted(i, ctx)
                s = self.s
                n = self.n
                break
            # unparseable char at value position
            i0 = i
            s = None
            value, i = yield from self._unquoted(i, ctx)
            s = self.s
            n = self.n
            if value is SKIP and i <= i0:
                i = i0 + 1
            break

        # close wrapper parens
        while wrapped:
            while True:
                while i < n and s[i] in _WS:
                    i += 1
                if i >= n and not self.final:
                    s = None
                    yield
                    s = self.s
                    n = self.n
                    continue
                break
            if i < n and s[i] == ")":
                i += 1
            wrapped -= 1
        return value, i

    # ------------------------------------------------------------------

    def _unquoted(self, i, ctx):
        """Scan an unquoted token.  Tracks bracket balance so prose like
        ``words{in brackets}more`` stays one string."""
        s = self.s
        n = self.n
        start = i
        depth = 0
        if ctx == _TOP:
            stops = "\n"
        else:
            stops = ',}]"\n'
        while True:
            while i < n:
                ch = s[i]
                if depth == 0 and ch in stops:
                    break
                if ch in "{[(":
                    depth += 1
                elif ch in "}])":
                    if depth == 0:
                        break
                    depth -= 1
                i += 1
            if i >= n and not self.final:
                s = None
                yield
                s = self.s
                n = self.n
                continue
            break
        raw = s[start:i].strip()
        if i < n and s[i] == '"' and i > start and s[i - 1] not in _WS \
                and not raw.startswith("```"):
            # `abcdef"` — stray closing quote glued to the token: consume
            i += 1
        if raw.startswith("```"):
            # ```json {...}``` inside a value slot
            inner = raw[3:]
            m = _WORD_RE.match(inner)
            if m and not inner[m.end():].strip():
                # bare ``` followed by a language tag only: skip it
                return SKIP, i
        if i >= n and self.final and raw:
            # truncation: a unique prefix of a literal completes it
            for lit in ("true", "false", "null", "True", "False", "None"):
                if lit.startswith(raw) and len(raw) < len(lit):
                    return _LITERALS[lit], i
        if not raw or set(raw) == {"."}:
            return SKIP, i
        v = _convert_number(raw)
        if v is _NONFINITE:
            self.had_nonfinite = True
            return float(raw.replace("_", "")), i
        if v is not None:
            return v, i
        if raw in _LITERALS:
            value = _LITERALS[raw]
            if isinstance(value, float):
                self.had_nonfinite = True
            return value, i
        return raw, i

    # ------------------------------------------------------------------

    def _key(self, i):
        """Parse an object key.  Returns (key_str | SKIP, new_i)."""
        s = self.s
        n = self.n
        c = s[i]
        if c in _QUOTES:
            s = None
            value, i = yield from self._string(i, _OKEY)
            s = self.s
            return (value if isinstance(value, str) else _to_key(value)), i
        if c == "\\":
            while i + 1 >= n and not self.final:
                s = None
                yield
                s = self.s
                n = self.n
            if i + 1 < n and s[i + 1] in _QUOTES:
                s = None
                value, i = yield from self._string(i + 1, _OKEY,
                                                   esc_delim=True)
                s = self.s
                return (value if isinstance(value, str)
                        else _to_key(value)), i
            return SKIP, i + 1
        start = i
        while True:
            while i < n and s[i] not in ':,}]"=\n' and s[i] not in _WS:
                i += 1
            if i >= n and not self.final:
                s = None
                yield
                s = self.s
                n = self.n
                continue
            break
        if i < n and s[i] == '"' and i > start and s[i - 1] not in _WS:
            # `{a":...}` — orphaned closing quote glued to a bare key
            raw = s[start:i].strip()
            i += 1
            return (raw if raw else SKIP), i
        raw = s[start:i].strip()
        if not raw:
            return SKIP, i
        return raw, i

    # ------------------------------------------------------------------

    def _string(self, i, ctx, esc_delim=False):
        """Parse a quoted string starting at the open quote.

        Returns (text, new_i).  Implements the jsonmend close rules:
        a quote closes the string iff what follows makes structural sense.
        """
        s = self.s
        n = self.n
        q = s[i]
        closers = _QUOTES[q]
        i += 1
        start = i

        # snapshot bookkeeping for streaming previews
        if ctx == _OVAL and self.stack and self.stack[-1].key is not None:
            self.partial = (self.stack[-1].container, start)
        elif ctx == _ARR and self.stack:
            self.partial = (self.stack[-1].container, start)
        elif ctx == _TOP:
            self.partial = (None, start)

        scan = start          # next position to search for a quote
        kscan = start         # next position to search for a key colon
        nl_checked = start    # newlines before this are content
        last_cand = -1        # most recent rejected candidate quote

        try:
            while True:
                # find the next candidate closing quote
                if len(closers) == 1:
                    j = s.find(closers, scan)
                else:
                    j = -1
                    for cc in closers:
                        f = s.find(cc, scan)
                        if f != -1 and (j == -1 or f < j):
                            j = f
                # for keys: a bare colon before any quote closes the key
                if ctx == _OKEY:
                    cpos = s.find(":", kscan, j if j != -1 else n)
                else:
                    cpos = -1

                if j == -1:
                    if not self.final:
                        scan = n
                        s = None
                        yield
                        s = self.s
                        n = self.n
                        continue
                    # ---- truncated input
                    if cpos != -1 and ctx == _OKEY:
                        return _finish_string(s[start:cpos]), cpos
                    raw = s[start:n]
                    rs = raw.rstrip()
                    if ctx != _TOP and rs:
                        # `{"a":"b}` — when the trailing closer run exactly
                        # matches the open containers, the quote was simply
                        # missing: give the closers back to the structure
                        m = 0
                        while m < len(rs) and rs[-1 - m] in "}]":
                            m += 1
                        if m and m == len(self.stack):
                            body = rs[:-m].rstrip()
                            return _finish_string(body), \
                                start + len(rs) - m
                    if rs.endswith("+"):
                        # dangling concatenation: `"hello +`
                        return _finish_string(rs[:-1].rstrip()), n
                    return _finish_string(_strip_partial_escape(raw)), n

                # escaped quote?
                b = 0
                while j - 1 - b >= start - 1 and s[j - 1 - b] == "\\":
                    b += 1
                if esc_delim:
                    if b >= 2:
                        scan = kscan = j + 1
                        continue
                    # closer; drop the escaping backslash from content
                    end_content = j - b
                else:
                    if b % 2 == 1:
                        scan = kscan = j + 1
                        continue
                    end_content = j

                # ---- newline heuristics for content between nl_checked..j
                nl = s.find("\n", nl_checked, j)
                early = -1
                while nl != -1:
                    p = nl + 1
                    while p < j and s[p] in " \t":
                        p += 1
                    cnl = s[p] if p < j else (q if p == j else "")
                    if p >= j or cnl in "}]":
                        # next line starts with the candidate quote or a
                        # closing bracket: close the string at this newline
                        early = nl
                        break
                    nl_checked = nl + 1
                    nl = s.find("\n", nl_checked, j)
                # [nl_checked, j) is now known newline-free; never rescan
                # it for the next candidate (this keeps quote storms O(n))
                nl_checked = j
                if early != -1:
                    raw = s[start:early].rstrip()
                    if raw.endswith(","):
                        body = raw[:-1].rstrip()
                        comma_at = start + len(raw) - 1
                        return _finish_string(body), comma_at
                    return _finish_string(raw), early

                if ctx == _OKEY and cpos != -1:
                    # `"key:"value"` — prefer the quote only when it is a
                    # clean key closer (followed by a colon)
                    p = j + 1
                    while True:
                        while p < n and s[p] in _WS:
                            p += 1
                        if p >= n and not self.final:
                            s = None
                            yield
                            s = self.s
                            n = self.n
                            continue
                        break
                    if p < n and (s[p] == ":" or s[p] == "："):
                        return _finish_string(s[start:end_content]), j + 1
                    return _finish_string(s[start:cpos]), cpos

                # ---- the close decision: peek the next meaningful char
                self.partial_end = j
                p = j + 1
                while True:
                    while p < n and s[p] in _WS:
                        p += 1
                    if p >= n and not self.final:
                        s = None
                        yield
                        s = self.s
                        n = self.n
                        continue
                    if p < n and s[p] == "/" and p + 1 >= n and \
                            not self.final:
                        # can't tell yet whether a comment follows
                        s = None
                        yield
                        s = self.s
                        n = self.n
                        continue
                    break
                self.partial_end = None
                nxt = s[p] if p < n else ""

                if j == start and p == j + 1 and (nxt.isalnum() or
                                                  nxt == "_"):
                    # doubled opening quote (`""answer""`): the second
                    # quote is the real opener
                    start = scan = kscan = j + 1
                    if ctx != _OKEY:
                        cpos = -1
                    continue
                if nxt == "":
                    return _finish_string(s[start:end_content]), j + 1
                if p == j + 1 and nxt == q and j > start:
                    # doubled closing quote: consume both
                    return _finish_string(s[start:end_content]), p + 1
                if nxt in ",}])+":
                    return _finish_string(s[start:end_content]), j + 1
                if nxt == "#" or (nxt == "/" and p + 1 < n and
                                  s[p + 1] in "/*"):
                    return _finish_string(s[start:end_content]), j + 1
                if (ctx == _OVAL or ctx == _ARR) and "\n" in s[j + 1:p]:
                    # the text continues on a new line: missing comma
                    return _finish_string(s[start:end_content]), j + 1
                if ctx == _OKEY:
                    if nxt == "：" or nxt.isdigit() or nxt in "{[" or \
                            (p > j + 1 and (nxt.isalpha() or
                                            nxt in _QUOTES)):
                        # `{"a" 2}` / `{"a" "b"}` — key closed, colon missing
                        return _finish_string(s[start:end_content]), j + 1
                if nxt == ":":
                    if ctx == _OKEY or ctx == _ARR or ctx == _TOP:
                        return _finish_string(s[start:end_content]), j + 1
                    # _OVAL: this segment was the *next key* — close at the
                    # previous candidate instead
                    if last_cand != -1:
                        raw = s[start:last_cand]
                        stripped = raw.rstrip()
                        if stripped.endswith(","):
                            body = stripped[:-1].rstrip()
                            comma_at = start + len(stripped) - 1
                            return _finish_string(body), comma_at
                        return _finish_string(raw), last_cand + 1
                    return _finish_string(s[start:end_content]), j + 1
                if nxt in _QUOTES and (ctx == _ARR or ctx == _TOP or
                                       ctx == _OKEY):
                    return _finish_string(s[start:end_content]), j + 1
                if ctx == _ARR and (nxt.isdigit() or nxt in "+-"):
                    # `["a" 2]` — a number follows: missing comma
                    return _finish_string(s[start:end_content]), j + 1
                if ctx == _ARR and nxt.isalpha():
                    # close only when a *literal* (true/false/null) follows
                    mm = _WORD_RE.match(s, p)
                    while mm and mm.end() >= n and not self.final:
                        s = None
                        mm = None  # drop Match: keep stream buffer refcount-1 for O(1) append
                        yield
                        s = self.s
                        n = self.n
                        mm = _WORD_RE.match(s, p)
                    if mm and mm.group() in _LITERALS:
                        e = mm.end()
                        while True:
                            while e < n and s[e] in _WS:
                                e += 1
                            if e >= n and not self.final:
                                s = None
                                yield
                                s = self.s
                                n = self.n
                                continue
                            break
                        if e >= n or s[e] in ",]}":
                            return _finish_string(s[start:end_content]), j + 1

                # not a close: quote is content
                last_cand = j
                scan = kscan = j + 1
        finally:
            self.partial = None
            self.partial_end = None


def _close_seq(fr):
    if fr.kind == "p":
        items = fr.container
        if len(items) == 1:
            return items[0]
        return items
    return fr.container


_NONFINITE = object()


def _convert_number(tok):
    """Token -> int/float, _NONFINITE marker, or None if not a number."""
    m = _NUM_RE.match(tok)
    if not m or m.end() != len(tok):
        return None
    if _LEADING_ZERO_RE.match(tok) and "." not in tok and \
            "e" not in tok and "E" not in tok:
        return None  # 0789 style: keep as string
    t = tok.replace("_", "")
    if t[0] == "+":
        t = t[1:]
    try:
        if t[-1] in ".eE+-":
            t += "0"
        if "." in t or "e" in t or "E" in t:
            return float(t)
        return int(t)
    except ValueError:
        return None
