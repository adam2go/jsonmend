# jsonmend

[![CI](https://github.com/adam2go/jsonmend/actions/workflows/ci.yml/badge.svg)](https://github.com/adam2go/jsonmend/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/jsonmend)](https://pypi.org/project/jsonmend/)
[![conformance](https://img.shields.io/badge/conformance_corpus-460%2F460-brightgreen)](corpus/scoreboard.md)
[![license](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

**Mends the JSON your LLM almost wrote.**

Truncated tool calls, markdown fences, single quotes, bare keys, Python
literals, comments, trailing commas, prose around the payload — jsonmend
turns them into valid JSON. It is a drop-in replacement for
[json_repair](https://github.com/mangiucugna/json_repair) that is
**5–10× faster** on batch repair, **~50× faster** on streaming, ships a
**true incremental streaming API** (O(new bytes) per chunk, not O(buffer)),
and is the reference implementation of an open, cross-language
[**conformance corpus**](corpus/) for JSON repair.

Pure Python, zero dependencies, zero binaries. Works on CPython 3.9–3.14,
PyPy, Pyodide/WASM, AWS Lambda — anywhere `pip install` works.

```bash
pip install jsonmend
```

## Scoreboard

JSON repair has no standard: the same broken input is repaired
*differently* by the Python and JavaScript incumbents, which is a real
source of production bugs. The [jsonmend conformance corpus](corpus/)
(460 cases, 19 categories, CC0) defines repair semantics as data —
including the genuinely ambiguous cases, where every defensible answer
is accepted.

| | jsonmend 0.1.0 | json_repair 0.60.1 | jsonrepair 3.14.0 (JS) |
|---|---|---|---|
| corpus pass rate | **460/460 (100%)** | 320/460 (69.6%) | 354/460 (77.0%) |

Per-category breakdown: [corpus/scoreboard.md](corpus/scoreboard.md).
Reproduce: `python tools/referee.py --write` (needs `pip install
json_repair` and `npm install jsonrepair`, dev-only).

## Performance

Median of 7, three independent rounds within ±5%, Python 3.12, M-series
macOS. All inputs are *broken* JSON (the `json.loads` fast path never
runs). Verified-then-timed: outputs are checked equal before timing.
Reproduce: `python tools/bench.py --verify && python tools/bench.py`.

| workload | size | jsonmend | json_repair | speedup |
|---|---|---|---|---|
| truncated tool call | 1 KB | 0.027 ms | 0.199 ms | **7.3×** |
| truncated row payload | 75 KB | 1.48 ms | 12.6 ms | **8.5×** |
| markdown-fenced output | 49 KB | 0.25 ms | 2.6 ms | **10.6×** |
| dirty (quotes/keys/literals) | 5 KB | 0.38 ms | 2.6 ms | **7.0×** |

### Streaming is a different complexity class

A streaming UI re-renders the partial value on every chunk. With a batch
repairer you must re-parse the whole buffer each time — O(n²) total. The
stateful `Mender` only pays for the new bytes:

| workload | jsonmend `Mender` | json_repair (`stream_stable=True`) | |
|---|---|---|---|
| 150 KB in 4 KB chunks | 6.9 ms | 323 ms | **47×** |
| 10 MB in 4 KB chunks | 1.2 s | est. >20 min (quadratic) | — |

## Usage

### Drop-in for json_repair

```python
# before
from json_repair import repair_json, loads
# after — same call sites
from jsonmend import repair_json, loads

repair_json("{'name': 'John', age: 31")     # '{"name": "John", "age": 31}'
loads('```json\n{"ok": true,}\n```')         # {'ok': True}
```

`repair_json(json_str, return_objects=..., skip_json_loads=...,
ensure_ascii=..., **json_dumps_args)`, `loads`, `load(fd)`,
`from_file(path)` match json_repair's signatures. Valid JSON
short-circuits through C-speed `json.loads`.

### Streaming

```python
from jsonmend import Mender

m = Mender()
for chunk in llm_stream:           # feed as the tokens arrive
    partial = m.feed(chunk)        # best-effort value, O(new bytes)
    render(partial)                # e.g. {"answer": "The capital of Fr"}
value = m.close()                  # final mended value
```

`feed()` returns a live view that grows in place — including the string
that is currently streaming in. Any chunking gives byte-identical results
to batch repair (property-tested over the whole corpus).

### Strict mode

```python
from jsonmend import loads, JSONMendError

loads("complete garbage")                  # "" (json_repair-compatible)
loads("complete garbage", strict=True)     # raises JSONMendError
```

## What it fixes

truncated objects/arrays/strings/numbers/literals · markdown fences with
prose around them · single/smart/backtick quotes · unescaped inner quotes
· missing quotes · bare keys and values · `True/False/None/undefined/NaN/
Infinity` · `//`, `#`, `/* */` comments · trailing/missing/extra commas ·
missing colons · mismatched brackets · concatenated/NDJSON documents ·
string concatenation (`"a" + "b"`) · JSONP/MongoDB wrappers
(`ObjectId("…")`) · Python tuples/sets · ellipsis placeholders ·
non-string keys · BOM and exotic whitespace · escaped-JSON documents
(`{\"a\": 1}`) · broken `\u` escapes and surrogate pairs · 100k-deep
nesting (no recursion anywhere)

## Why it's fast

* **One resumable state machine** serves batch and streaming — batch is
  a single feed that never suspends, so there is no streaming tax.
* **Strings cost one `str.find` + one slice** when clean; never a
  per-character Python loop.
* **Speculative C parsing**: complete sub-trees inside broken documents
  are recognized and handed to the C `json` scanner, with a salvage step
  that parses the longest clean prefix of a broken container in one shot.
  Semantics-affecting inputs (NaN, control chars, surrogate escapes)
  fall back to the machine, so behavior never changes.
* **Bounded backtracking**: a string-close decision can revisit one
  recorded candidate quote, never rescan; adversarial quote storms stay
  linear (tested).

## Guarantees

* Output is always **valid RFC 8259 JSON** (or `""`/an exception). Unlike
  json_repair, `NaN`/`Infinity` never leak into the output text — they
  serialize as `null` (`loads` still gives you the floats).
* Output is always UTF-8 encodable (lone surrogates are replaced).
* Never crashes, never recurses: fuzzed and property-tested, 100k-deep
  inputs are fine.
* `Mender.close()` ≡ batch result, for every chunking (property-tested).

## Honest differences vs json_repair

* `logging=True` is not supported (it is incompatible with the
  single-pass design and is one reason json_repair is slow); a no-op
  shim raises `TypeError` so you notice.
* Schema-guided repair (`schema=`) is not implemented in v0.1.
* json_repair's `stream_stable=True` flag changes how *truncated escapes*
  render mid-stream; jsonmend's `Mender` is always stream-stable.
* On `ambiguous` corpus cases the libraries may legitimately differ;
  jsonmend's choices are documented case-by-case in the corpus
  rationales.

## The corpus is the point

If you maintain a JSON-repair library in any language: please steal
[corpus/](corpus/). It is CC0, the format is three fields, and 460 cases
with rationales are more valuable than any of our engines. Cross-language
agreement on repair semantics helps everyone shipping LLM systems.

## License

MIT. The conformance corpus is CC0.
