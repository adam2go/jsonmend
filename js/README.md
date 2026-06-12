# jsonmend (JavaScript)

**Mends the JSON your LLM almost wrote.**

JavaScript port of [jsonmend](https://github.com/adam2go/jsonmend) — same
engine design, same repair semantics, verified against the same
[conformance corpus](https://github.com/adam2go/jsonmend/tree/main/corpus)
(485/485). The Python and JS implementations repair identically, so a
model output parses the same on both sides of your stack.

Zero dependencies. Node ≥ 14, browsers, workers.

```bash
npm install jsonmend
```

## Usage

```js
import { repairJson, loads, Mender } from "jsonmend";

repairJson("{'name': 'John', age: 31");
// '{"name": "John", "age": 31}'

loads('```json\n{"ok": true,}\n```');
// { ok: true }
```

### Streaming (true incremental)

```js
const m = new Mender();
for await (const chunk of llmStream) {
  const partial = m.feed(chunk);   // best-effort value, O(new bytes)
  render(partial);                  // e.g. { answer: "The capital of Fr" }
}
const value = m.close();
```

Each `feed()` costs only the new bytes — re-parsing the whole buffer per
chunk (what you have to do with a batch repairer) is O(n²) in total and
hundreds of times slower on long outputs.

### API

* `repairJson(text, options?)` → repaired JSON string.
  Options: `{ returnObjects, skipJsonParse, strict }`.
* `loads(text, options?)` → parsed value (uses a `JSON.parse` fast path
  for valid input).
* `mend(text, options?)` → parsed value, always through the repair
  machine.
* `new Mender()` → `feed(chunk)`, `value`, `close()`.

Integers beyond `Number.MAX_SAFE_INTEGER` are preserved as `BigInt` and
serialized with exact digits. Output is always valid RFC 8259 JSON
(`NaN`/`Infinity` serialize as `null`) and always UTF-8 encodable (lone
surrogates are replaced).

## What it fixes

Truncated objects/arrays/strings/numbers/literals · markdown fences ·
single/smart quotes · unescaped inner quotes · bare keys/values · Python
literals · comments · trailing/missing commas · mismatched brackets ·
NDJSON · string concatenation · JSONP/MongoDB wrappers · tuples ·
ellipsis · BOM · broken escapes · 100k-deep nesting (no recursion).

See the [main README](https://github.com/adam2go/jsonmend#readme) for
the full story, benchmarks and the conformance corpus.

## License

MIT. The conformance corpus is CC0.
