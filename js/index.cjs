/**
 * jsonmend — mends the JSON your LLM almost wrote.
 *
 * JavaScript port of the jsonmend engine (https://github.com/adam2go/jsonmend).
 * One resumable state machine serves both batch repair and true incremental
 * streaming; semantics are defined by the jsonmend conformance corpus.
 */

"use strict";

class JSONMendError extends Error {
  constructor(message) {
    super(message);
    this.name = "JSONMendError";
  }
}

// ---------------------------------------------------------------------------
// character tables
// ---------------------------------------------------------------------------

const WS = new Set(
  " \t\n\r\v\f        " +
  "     ​    　" +
  "﻿᠎"
);

const QUOTES = new Map([
  ['"', '"'],
  ["'", "'"],
  ["“", "”\""],
  ["‘", "’'"],
  ["`", "´`'"],
  ["«", "»"],
]);

const LITERALS = new Map(Object.entries({
  true: true, True: true, TRUE: true,
  false: false, False: false, FALSE: false,
  null: null, Null: null, NULL: null,
  None: null, none: null, undefined: null, nil: null,
  NaN: NaN, nan: NaN,
  Infinity: Infinity, inf: Infinity,
}));

const NUM_RE = /[-+]?(?:\d[\d_]*(?:\.[\d_]*)?|\.\d[\d_]*)(?:[eE][+-]?\d*)?/y;
const WORD_RE = /[A-Za-z_$][A-Za-z0-9_$]*/y;
const LEADING_ZERO_RE = /^[-+]?0\d/;
const ESC_RE = /\\(u[0-9a-fA-F]{0,4}|[\s\S]|$)/g;
const LONE_SURR_RE =
  /[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/g;
const SURR_TEST = /[\uD800-\uDFFF]/;
const PARTIAL_U_RE = /\\u[0-9a-fA-F]{0,3}$/;
const HIGH_SURR_RE = /\\u[dD][89abAB][0-9a-fA-F]{2}$/;
const ALPHA_RE = /\p{L}/u;
const ALNUM_RE = /[\p{L}\p{N}]/u;

const TOP = 0, OKEY = 1, OVAL = 2, ARR = 3;
const SKIP = Symbol("skip");

const STR_DELIMS = new Set([",", "}", "]", ")", ":"]);
const ESC_MAP = new Map(Object.entries({
  '"': '"', "\\": "\\", "/": "/", b: "\b", f: "\f",
  n: "\n", r: "\r", t: "\t", "'": "'", "\n": "\n",
}));

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------

function isDigit(c) {
  return c >= "0" && c <= "9";
}

function isAlpha(c) {
  return c !== "" && ALPHA_RE.test(c);
}

function isAlnum(c) {
  return c !== "" && ALNUM_RE.test(c);
}

function findIn(s, sub, start, end) {
  const idx = s.indexOf(sub, start);
  return idx !== -1 && idx < end ? idx : -1;
}

function trimWs(str) {
  let a = 0, b = str.length;
  while (a < b && WS.has(str[a])) a++;
  while (b > a && WS.has(str[b - 1])) b--;
  return str.slice(a, b);
}

function trimEndWs(str) {
  let b = str.length;
  while (b > 0 && WS.has(str[b - 1])) b--;
  return str.slice(0, b);
}

function decodeEscapes(raw) {
  let out = raw.replace(ESC_RE, (_, g) => {
    if (!g) return "";
    if (g[0] === "u") {
      const h = g.slice(1);
      if (h.length === 4) return String.fromCharCode(parseInt(h, 16));
      return "";
    }
    const m = ESC_MAP.get(g);
    return m !== undefined ? m : g;
  });
  if (SURR_TEST.test(out)) {
    out = out.replace(LONE_SURR_RE, "�");
  }
  return out;
}

function stripPartialEscape(raw) {
  let b = 0;
  const k = raw.length;
  while (b < k && raw[k - 1 - b] === "\\") b++;
  if (b % 2 === 1) return raw.slice(0, -1);
  let m = PARTIAL_U_RE.exec(raw);
  if (m === null) m = HIGH_SURR_RE.exec(raw);
  if (m) {
    const j = m.index;
    let bb = 0;
    while (j - 1 - bb >= 0 && raw[j - 1 - bb] === "\\") bb++;
    if (bb % 2 === 0) return raw.slice(0, j);
  }
  return raw;
}

function finishString(raw) {
  if (raw.indexOf("\\") !== -1) return decodeEscapes(raw);
  if (SURR_TEST.test(raw)) return raw.replace(LONE_SURR_RE, "�");
  return raw;
}

function toKey(v) {
  if (v === true) return "true";
  if (v === false) return "false";
  if (v === null || v === undefined) return "null";
  if (typeof v === "number" && Number.isInteger(v)) return String(v);
  return String(v);
}

const NONFINITE = Symbol("nonfinite");

function convertNumber(tok) {
  NUM_RE.lastIndex = 0;
  const m = NUM_RE.exec(tok);
  if (!m || m[0].length !== tok.length) return null;
  if (LEADING_ZERO_RE.test(tok) && tok.indexOf(".") === -1 &&
      tok.indexOf("e") === -1 && tok.indexOf("E") === -1) {
    return null; // 0789 style: keep as string
  }
  let t = tok.replace(/_/g, "");
  if (t[0] === "+") t = t.slice(1);
  if (".eE+-".includes(t[t.length - 1])) t += "0";
  if (t.includes(".") || t.includes("e") || t.includes("E")) {
    const f = Number(t);
    return Number.isNaN(f) ? null : f;
  }
  const v = Number(t);
  if (!Number.isSafeInteger(v)) {
    try {
      return BigInt(t); // preserve big integer digits exactly
    } catch (e) {
      return null;
    }
  }
  return v;
}

function isContainer(v) {
  return v !== null && typeof v === "object";
}

class Frame {
  constructor(kind, container) {
    this.kind = kind; // 'o' | 'a' | 'p'
    this.container = container;
    this.key = undefined;
    this.eager = false;
  }
}

function closeSeq(fr) {
  if (fr.kind === "p") {
    const items = fr.container;
    if (items.length === 1) return items[0];
    return items;
  }
  return fr.container;
}

function lastKey(obj) {
  let k;
  for (const key of Object.keys(obj)) k = key;
  return k;
}

function matchAt(re, s, i) {
  re.lastIndex = i;
  return re.exec(s);
}

// ---------------------------------------------------------------------------
// the machine
// ---------------------------------------------------------------------------

class MendMachine {
  constructor() {
    this.s = "";
    this.n = 0;
    this.final = false;
    this.stack = [];
    this.values = [];
    this.prose = [];
    this.done = false;
    this.result = SKIP;
    this.hadNonfinite = false;
    this.partial = null;       // [container|null, start]
    this.partialEnd = null;
    this._undo = null;
    this._gen = this._run();
    this._gen.next(); // prime
  }

  feed(chunk) {
    if (this.done) return;
    this.detachPartial();
    this.s += chunk;
    this.n = this.s.length;
    if (this._gen.next().done) this.done = true;
  }

  close() {
    if (!this.done) {
      this.detachPartial();
      this.final = true;
      this._gen.next();
      this.done = true;
      this._gen = null;
    }
    return this.result;
  }

  attachPartial() {
    const p = this.partial;
    if (p === null) return SKIP;
    const [container, start] = p;
    const end = this.partialEnd !== null ? this.partialEnd : this.n;
    const text = finishString(stripPartialEscape(this.s.slice(start, end)));
    if (container === null) return text;
    if (Array.isArray(container)) {
      this._undo = [container, container.length, false, null];
      container.push(text);
    } else {
      let key;
      for (let fi = this.stack.length - 1; fi >= 0; fi--) {
        if (this.stack[fi].container === container) {
          key = this.stack[fi].key;
          break;
        }
      }
      if (key === undefined) return SKIP;
      const existed = Object.prototype.hasOwnProperty.call(container, key);
      this._undo = [container, key, existed, container[key]];
      container[key] = text;
    }
    return SKIP;
  }

  detachPartial() {
    const u = this._undo;
    if (u === null) return;
    const [container, slot, existed, prev] = u;
    if (Array.isArray(container)) {
      if (container.length === slot + 1) container.pop();
    } else if (existed) {
      container[slot] = prev;
    } else {
      delete container[slot];
    }
    this._undo = null;
  }

  current() {
    if (this.done) return this.result === SKIP ? null : this.result;
    const vals = this.values.slice();
    if (this.stack.length) vals.push(this.stack[0].container);
    const extra = this.attachPartial();
    if (extra !== SKIP && !this.stack.length) vals.push(extra);
    if (!vals.length) return null;
    if (vals.length === 1) return vals[0];
    return vals;
  }

  popValue(fr) {
    const v = fr.kind === "o" ? fr.container : closeSeq(fr);
    if (!fr.eager) return v;
    if (v === fr.container) return SKIP;
    const stack = this.stack;
    if (!stack.length) return v;
    const par = stack[stack.length - 1];
    if (par.kind === "o") {
      if (par.key !== undefined) par.container[par.key] = v;
    } else if (par.container.length &&
               par.container[par.container.length - 1] === fr.container) {
      par.container[par.container.length - 1] = v;
    }
    return SKIP;
  }

  * _run() {
    const stack = this.stack;
    const values = this.values;
    const prose = this.prose;

    let s = this.s;
    let n = this.n;
    let i = 0;
    let c = "";
    let fenceSeen = false;
    let inFence = false;
    let wrapperDepth = 0;
    let value = SKIP;
    let mode = 0;

    mainloop:
    for (;;) {
      // ---------------------------------------------- ws + comments
      wsloop:
      for (;;) {
        while (i < n && WS.has(s[i])) i++;
        if (i >= n) {
          // mode 2 (attach a finished value) needs no new bytes
          if (this.final || mode === 2) break;
          s = null; yield; s = this.s; n = this.n;
          continue;
        }
        c = s[i];
        if (c === "/") {
          while (i + 1 >= n && !this.final) {
            s = null; yield; s = this.s; n = this.n;
          }
          const nxt = i + 1 < n ? s[i + 1] : "";
          if (nxt === "/") {
            let j = s.indexOf("\n", i + 2);
            while (j === -1 && !this.final) {
              i = n;
              s = null; yield; s = this.s; n = this.n;
              j = s.indexOf("\n", i);
            }
            i = j === -1 ? n : j + 1;
            continue;
          }
          if (nxt === "*") {
            let j = s.indexOf("*/", i + 2);
            while (j === -1 && !this.final) {
              s = null; yield; s = this.s; n = this.n;
              j = s.indexOf("*/", i + 2);
            }
            i = j === -1 ? n : j + 2;
            continue;
          }
          break;
        }
        if (c === "#" && mode !== 0) {
          let j = s.indexOf("\n", i + 1);
          while (j === -1 && !this.final) {
            s = null; yield; s = this.s; n = this.n;
            j = s.indexOf("\n", i);
          }
          i = j === -1 ? n : j + 1;
          continue;
        }
        if (c === "." && (mode === 3 || mode === 4 || mode === 5 ||
            (mode === 1 && i + 1 < n && s[i + 1] === "."))) {
          let j = i;
          for (;;) {
            while (j < n && s[j] === ".") j++;
            if (j < n || this.final) break;
            s = null; yield; s = this.s; n = this.n;
          }
          if (j - i >= 2) { i = j; continue; }
          break;
        }
        break;
      }

      const atEof = i >= n;
      if (atEof && mode === 0) break;

      // ==================================================== dispatch
      if (mode === 1) {
        const inObj = stack.length > 0 &&
          stack[stack.length - 1].kind === "o";
        if (atEof) {
          value = (inObj && stack[stack.length - 1].key !== undefined)
            ? null : SKIP;
          mode = 2;
          continue;
        }
        if (c === "{" || c === "[" || c === "(") {
          const child = c === "{" ? {} : [];
          const nf = new Frame(c === "{" ? "o" : (c === "[" ? "a" : "p"),
                               child);
          if (stack.length) {
            const par = stack[stack.length - 1];
            if (par.kind === "o") {
              if (par.key !== undefined) {
                par.container[par.key] = child;
                nf.eager = true;
              }
            } else {
              par.container.push(child);
              nf.eager = true;
            }
          }
          stack.push(nf);
          i++;
          mode = c === "{" ? 3 : 1;
          continue;
        }
        if (c === "}" || c === "]" || c === ",") {
          value = (inObj && stack[stack.length - 1].key !== undefined)
            ? null : SKIP;
          mode = 2;
          continue;
        }
        // ---- fast path: simple number
        if (isDigit(c) || c === "-") {
          const m = matchAt(NUM_RE, s, i);
          const k = m ? i + m[0].length : i;
          if (m && k < n) {
            const nc = s[k];
            if (",}]".includes(nc) || WS.has(nc)) {
              const tok = m[0];
              if (isDigit(tok[tok.length - 1]) && !tok.includes("_") &&
                  !LEADING_ZERO_RE.test(tok)) {
                const conv = convertNumber(tok);
                if (conv !== null) {
                  value = conv;
                  i = k;
                  if (stack.length) {
                    const fr = stack[stack.length - 1];
                    if (fr.kind === "o") {
                      if (fr.key !== undefined) {
                        fr.container[fr.key] = value;
                        fr.key = undefined;
                      }
                      value = SKIP;
                      mode = 4;
                    } else {
                      fr.container.push(value);
                      value = SKIP;
                      mode = 5;
                    }
                    while (i < n && (s[i] === " " || s[i] === "\t")) i++;
                    if (i < n && s[i] === ",") {
                      i++;
                      mode = mode === 4 ? 3 : 1;
                    }
                    continue;
                  }
                  mode = 2;
                  continue;
                }
              }
            }
          }
        }
        // ---- fast path: literals
        if (c === "t" && s.startsWith("true", i) && i + 4 < n &&
            ",}]".includes(s[i + 4])) {
          value = true; i += 4; mode = 2; continue;
        }
        if (c === "f" && s.startsWith("false", i) && i + 5 < n &&
            ",}]".includes(s[i + 5])) {
          value = false; i += 5; mode = 2; continue;
        }
        if (c === "n" && s.startsWith("null", i) && i + 4 < n &&
            ",}]".includes(s[i + 4])) {
          value = null; i += 4; mode = 2; continue;
        }
        if (c === "T" && s.startsWith("True", i) && i + 4 < n &&
            ",}]".includes(s[i + 4])) {
          value = true; i += 4; mode = 2; continue;
        }
        if (c === "F" && s.startsWith("False", i) && i + 5 < n &&
            ",}]".includes(s[i + 5])) {
          value = false; i += 5; mode = 2; continue;
        }
        if (c === "N" && s.startsWith("None", i) && i + 4 < n &&
            ",}]".includes(s[i + 4])) {
          value = null; i += 4; mode = 2; continue;
        }
        // ---- fast path: clean quoted string
        if (c === '"' || c === "'") {
          const j = s.indexOf(c, i + 1);
          if (j !== -1 && j + 1 < n) {
            const d = s[j + 1];
            if ((STR_DELIMS.has(d) || WS.has(d)) &&
                findIn(s, "\\", i + 1, j) === -1 &&
                findIn(s, "\n", i + 1, j) === -1) {
              let ok;
              if (WS.has(d)) {
                let p = j + 1;
                while (p < n && WS.has(s[p])) p++;
                ok = (p >= n && this.final) ||
                     (p < n && STR_DELIMS.has(s[p]));
              } else {
                ok = true;
              }
              if (ok) {
                value = s.slice(i + 1, j);
                i = j + 1;
                if (stack.length) {
                  const fr = stack[stack.length - 1];
                  if (fr.kind === "o") {
                    if (fr.key !== undefined) {
                      fr.container[fr.key] = value;
                      fr.key = undefined;
                    }
                    value = SKIP;
                    mode = 4;
                  } else {
                    fr.container.push(value);
                    value = SKIP;
                    mode = 5;
                  }
                  while (i < n && (s[i] === " " || s[i] === "\t")) i++;
                  if (i < n && s[i] === ",") {
                    i++;
                    mode = mode === 4 ? 3 : 1;
                  }
                  continue;
                }
                mode = 2;
                continue;
              }
            }
          }
        }
        const ctx = !stack.length ? TOP :
          (stack[stack.length - 1].kind === "o" ? OVAL : ARR);
        const pos = i;
        s = null;
        [value, i] = yield* this._scalar(pos, ctx);
        s = this.s;
        n = this.n;
        mode = 2;
        continue;
      }

      if (mode === 3) {
        // -------------------------------------- object expects key
        const fr = stack[stack.length - 1];
        if (atEof) {
          stack.pop();
          value = this.popValue(fr);
          mode = 2;
          continue;
        }
        if (c === "}") {
          i++;
          stack.pop();
          value = this.popValue(fr);
          mode = 2;
          continue;
        }
        if (c === ",") { i++; continue; }
        if (c === "]") {
          let other = false;
          for (let fi = 0; fi < stack.length - 1; fi++) {
            if (stack[fi].kind !== "o") { other = true; break; }
          }
          if (!other) i++;
          stack.pop();
          value = this.popValue(fr);
          mode = 2;
          continue;
        }
        if (c === "[" && Object.keys(fr.container).length) {
          const lk = lastKey(fr.container);
          if (Array.isArray(fr.container[lk])) {
            fr.key = lk;
            const nf = new Frame("a", fr.container[lk]);
            nf.eager = true;
            stack.push(nf);
            i++;
            mode = 1;
            continue;
          }
          stack.pop();
          value = this.popValue(fr);
          mode = 2;
          continue;
        }
        if (c === "{" || c === "[") {
          stack.pop();
          value = this.popValue(fr);
          mode = 2;
          continue;
        }
        if (c === ")" && wrapperDepth) {
          i++;
          wrapperDepth--;
          continue;
        }
        if (c === ":") {
          i++;
          fr.key = undefined;
          mode = 1;
          continue;
        }
        // ---- fast path: bare `key:`
        if ((c === "_" || (c >= "A" && c <= "Z") || (c >= "a" && c <= "z"))) {
          const m = matchAt(WORD_RE, s, i);
          const k2 = i + m[0].length;
          if (k2 < n) {
            let e2 = k2;
            while (e2 < n && (s[e2] === " " || s[e2] === "\t")) e2++;
            if (e2 < n && s[e2] === ":") {
              fr.key = s.slice(i, k2);
              i = e2 + 1;
              mode = 1;
              continue;
            }
          }
        }
        // ---- fast path: clean `"key":`
        if (c === '"') {
          const j = s.indexOf('"', i + 1);
          if (j !== -1 && findIn(s, "\\", i + 1, j) === -1 &&
              findIn(s, "\n", i + 1, j) === -1) {
            let k2 = j + 1;
            while (k2 < n && (s[k2] === " " || s[k2] === "\t")) k2++;
            if (k2 < n && s[k2] === ":") {
              fr.key = s.slice(i + 1, j);
              i = k2 + 1;
              mode = 1;
              continue;
            }
          }
        }
        const pos = i;
        s = null;
        let key;
        [key, i] = yield* this._key(pos);
        s = this.s;
        n = this.n;
        if (key === SKIP) {
          if (i < n && s[i] !== "}" && s[i] !== "]") i++;
          continue;
        }
        fr.key = key;
        for (;;) {
          while (i < n && WS.has(s[i])) i++;
          if (i >= n && !this.final) {
            s = null; yield; s = this.s; n = this.n;
            continue;
          }
          break;
        }
        if (i < n && s[i] === '"') {
          let p = i + 1;
          for (;;) {
            while (p < n && WS.has(s[p])) p++;
            if (p >= n && !this.final) {
              s = null; yield; s = this.s; n = this.n;
              continue;
            }
            break;
          }
          if (p < n && (s[p] === ":" || s[p] === "：")) i = p;
        }
        if (i < n && (s[i] === ":" || s[i] === "：")) {
          i++;
        } else if (i < n && s[i] === "=") {
          i++;
          if (i < n && s[i] === ">") i++;
        } else if (i >= n || s[i] === "," || s[i] === "}") {
          fr.container[key] = null;
          fr.key = undefined;
          mode = 4;
          continue;
        }
        mode = 1;
        continue;
      }

      if (mode === 4) {
        // ------------------------------------- object after member
        const fr = stack[stack.length - 1];
        if (atEof) {
          stack.pop();
          value = this.popValue(fr);
          mode = 2;
          continue;
        }
        if (c === ",") { i++; mode = 3; continue; }
        if (c === "}") {
          i++;
          stack.pop();
          value = this.popValue(fr);
          mode = 2;
          continue;
        }
        if (c === "]") {
          let other = false;
          for (let fi = 0; fi < stack.length - 1; fi++) {
            if (stack[fi].kind !== "o") { other = true; break; }
          }
          if (!other) i++;
          stack.pop();
          value = this.popValue(fr);
          mode = 2;
          continue;
        }
        if (c === ";") { i++; mode = 3; continue; }
        if (c === ")" && wrapperDepth) {
          i++;
          wrapperDepth--;
          continue;
        }
        if (c === "{" || c === "[") {
          stack.pop();
          value = this.popValue(fr);
          mode = 2;
          continue;
        }
        mode = 3;
        continue;
      }

      if (mode === 5) {
        // --------------------------------------- array after element
        const fr = stack[stack.length - 1];
        if (atEof) {
          stack.pop();
          value = this.popValue(fr);
          mode = 2;
          continue;
        }
        if (c === ",") { i++; mode = 1; continue; }
        if (c === "]") {
          i++;
          stack.pop();
          value = this.popValue(fr);
          mode = 2;
          continue;
        }
        if (c === ")" && fr.kind === "p") {
          i++;
          stack.pop();
          value = this.popValue(fr);
          mode = 2;
          continue;
        }
        if (c === "}") {
          let hasObj = false;
          for (let fi = 0; fi < stack.length - 1; fi++) {
            if (stack[fi].kind === "o") { hasObj = true; break; }
          }
          if (hasObj) {
            stack.pop();
            value = this.popValue(fr);
            mode = 2;
          } else {
            i++;
          }
          continue;
        }
        if (c === ":" && fr.container.length) {
          i++;
          const key = fr.container.pop();
          const obj = {};
          const nf = new Frame("o", obj);
          nf.key = typeof key === "string" ? key : toKey(key);
          fr.container.push(obj);
          nf.eager = true;
          stack.push(nf);
          mode = 1;
          continue;
        }
        if (c === ";") { i++; mode = 1; continue; }
        mode = 1;
        continue;
      }

      if (mode === 2) {
        // ------------------------------------------- have a value
        if (!stack.length) {
          if (value !== SKIP && typeof value === "string") {
            // `"key": ...` — headless object body
            let j = i;
            for (;;) {
              while (j < n && WS.has(s[j])) j++;
              if (j >= n && !this.final) {
                s = null; yield; s = this.s; n = this.n;
                continue;
              }
              break;
            }
            if (j < n && s[j] === ":") {
              const nf = new Frame("o", {});
              nf.key = value;
              stack.push(nf);
              value = SKIP;
              i = j + 1;
              mode = 1;
              continue;
            }
          }
          while (wrapperDepth) {
            for (;;) {
              while (i < n && WS.has(s[i])) i++;
              if (i >= n && !this.final) {
                s = null; yield; s = this.s; n = this.n;
                continue;
              }
              break;
            }
            if (i < n && s[i] === ")") i++;
            wrapperDepth--;
          }
          if (value !== SKIP) {
            values.push(value);
            if (isContainer(value) && !Array.isArray(value)) {
              // `}, "key": ...` — object continuation
              let j = i;
              for (;;) {
                while (j < n && WS.has(s[j])) j++;
                if (j >= n && !this.final) {
                  s = null; yield; s = this.s; n = this.n;
                  continue;
                }
                break;
              }
              if (j < n && s[j] === ",") {
                j++;
                for (;;) {
                  while (j < n && WS.has(s[j])) j++;
                  if (j >= n && !this.final) {
                    s = null; yield; s = this.s; n = this.n;
                    continue;
                  }
                  break;
                }
                let ok = false;
                let probe = -1;
                if (j < n && QUOTES.has(s[j])) {
                  const qc = QUOTES.get(s[j]);
                  let e;
                  for (;;) {
                    e = -1;
                    for (const cc of qc) {
                      const f = s.indexOf(cc, j + 1);
                      if (f !== -1 && (e === -1 || f < e)) e = f;
                    }
                    if (e === -1 && !this.final) {
                      s = null; yield; s = this.s; n = this.n;
                      continue;
                    }
                    break;
                  }
                  probe = e !== -1 ? e + 1 : -1;
                } else if (j < n) {
                  let mm = matchAt(WORD_RE, s, j);
                  while (mm && j + mm[0].length >= n && !this.final) {
                    s = null; yield; s = this.s; n = this.n;
                    mm = matchAt(WORD_RE, s, j);
                  }
                  probe = mm ? j + mm[0].length : -1;
                }
                if (probe !== -1) {
                  for (;;) {
                    while (probe < n &&
                           (s[probe] === " " || s[probe] === "\t")) probe++;
                    if (probe >= n && !this.final) {
                      s = null; yield; s = this.s; n = this.n;
                      continue;
                    }
                    break;
                  }
                  if (probe < n && s[probe] === ":") ok = true;
                }
                if (ok) {
                  values.pop();
                  stack.push(new Frame("o", value));
                  i = j;
                  mode = 3;
                  continue;
                }
              }
            }
          }
          value = SKIP;
          mode = 0;
          continue;
        }
        const fr = stack[stack.length - 1];
        if (fr.kind === "o") {
          if (value !== SKIP && fr.key !== undefined) {
            fr.container[fr.key] = value;
          }
          fr.key = undefined;
          value = SKIP;
          mode = 4;
          continue;
        }
        if (value !== SKIP) {
          fr.container.push(value);
          value = SKIP;
        }
        mode = 5;
        continue;
      }

      // mode === 0 ------------------------------------------- top level
      if (inFence || c === "`") {
        while (i + 3 > n && !this.final) {
          s = null; yield; s = this.s; n = this.n;
        }
        if (s.startsWith("```", i)) {
          i += 3;
          if (inFence) {
            inFence = false;
          } else {
            fenceSeen = true;
            inFence = true;
            prose.length = 0;
            const m = matchAt(WORD_RE, s, i);
            if (m) i += m[0].length;
          }
          continue;
        }
        if (c === "`") { i++; continue; }
      }
      if (fenceSeen && !inFence) {
        let j = s.indexOf("```", i);
        while (j === -1 && !this.final) {
          i = Math.max(i, n - 2);
          s = null; yield; s = this.s; n = this.n;
          j = s.indexOf("```", i);
        }
        if (j === -1) break;
        i = j;
        continue;
      }
      if (c === "{" || c === "[") {
        prose.length = 0;
        if (c === "{") {
          stack.push(new Frame("o", {}));
          mode = 3;
        } else {
          stack.push(new Frame("a", []));
          mode = 1;
        }
        i++;
        continue;
      }
      if (isDigit(c) || c === "+" || c === "-" || c === "." ||
          c === "−") {
        // numbered prose line check
        let m = matchAt(NUM_RE, s, i);
        if (m) {
          while (i + m[0].length >= n && !this.final) {
            s = null; yield; s = this.s; n = this.n;
            m = matchAt(NUM_RE, s, i);
          }
          let k = i + m[0].length;
          while (k < n && (s[k] === " " || s[k] === "\t")) k++;
          while (k >= n && !this.final) {
            s = null; yield; s = this.s; n = this.n;
          }
          if (k < n && (isAlpha(s[k]) || "*_#".includes(s[k]))) {
            const mm = matchAt(WORD_RE, s, k);
            if (!(mm && LITERALS.has(mm[0]))) {
              let e = s.indexOf("\n", k);
              while (e === -1 && !this.final) {
                s = null; yield; s = this.s; n = this.n;
                e = s.indexOf("\n", k);
              }
              if (!values.length && !fenceSeen && !stack.length) {
                prose.push(trimWs(s.slice(i, e === -1 ? n : e)));
              }
              i = e === -1 ? n : e + 1;
              continue;
            }
          }
        }
        prose.length = 0;
        mode = 1;
        continue;
      }
      if (QUOTES.has(c)) {
        prose.length = 0;
        mode = 1;
        continue;
      }
      if (c === "\\") {
        while (i + 1 >= n && !this.final) {
          s = null; yield; s = this.s; n = this.n;
        }
        if (i + 1 < n && QUOTES.has(s[i + 1])) {
          prose.length = 0;
          mode = 1;
        } else {
          i++;
        }
        continue;
      }
      if (",;:)}]=".includes(c)) { i++; continue; }
      {
        let m = matchAt(WORD_RE, s, i);
        if (m) {
          while (i + m[0].length >= n && !this.final) {
            s = null; yield; s = this.s; n = this.n;
            m = matchAt(WORD_RE, s, i);
          }
          const word = m[0];
          const k = i + word.length;
          let p = k;
          for (;;) {
            while (p < n && (s[p] === " " || s[p] === "\t")) p++;
            if (p >= n && !this.final) {
              s = null; yield; s = this.s; n = this.n;
              continue;
            }
            break;
          }
          const pc = p < n ? s[p] : "";
          if (LITERALS.has(word) &&
              (pc === "" || ",]}\n".includes(pc))) {
            prose.length = 0;
            mode = 1;
            continue;
          }
          if (pc === "(" && p === k) {
            prose.length = 0;
            i = k + 1;
            wrapperDepth++;
            mode = 1;
            continue;
          }
          // prose: consume this line, stop early at structure
          let j = i;
          let stop;
          for (;;) {
            stop = n;
            for (const ch of ["\n", "{", "[", "`", '"']) {
              const f = findIn(s, ch, j, stop);
              if (f !== -1) stop = f;
            }
            if (stop >= n && !this.final) {
              j = stop;
              s = null; yield; s = this.s; n = this.n;
              continue;
            }
            break;
          }
          if (stop < n && s[stop] === '"') {
            let p2 = stop + 1;
            for (;;) {
              while (p2 < n && (s[p2] === " " || s[p2] === "\t")) p2++;
              if (p2 >= n && !this.final) {
                s = null; yield; s = this.s; n = this.n;
                continue;
              }
              break;
            }
            if (p2 >= n || s[p2] === "\n") {
              if (!values.length && !fenceSeen && !stack.length) {
                prose.push(trimWs(s.slice(i, stop)));
              }
              i = p2;
              continue;
            }
          }
          if (!values.length && !fenceSeen && !stack.length) {
            prose.push(trimWs(s.slice(i, stop)));
          }
          i = stop;
          if (i < n && s[i] === "\n") i++;
          continue;
        }
      }
      if (c === "(") {
        let eol = s.indexOf("\n", i);
        while (eol === -1 && !this.final) {
          s = null; yield; s = this.s; n = this.n;
          eol = s.indexOf("\n", i);
        }
        const bound = eol === -1 ? n : eol;
        let close = s.lastIndexOf(")", bound - 1);
        if (close < i) close = -1;
        const isValue = (close !== -1 &&
                         trimWs(s.slice(close + 1, bound)) === "") ||
                        (close === -1 && eol === -1);
        if (isValue) {
          prose.length = 0;
          stack.push(new Frame("p", []));
          i++;
          mode = 1;
          continue;
        }
        if (!values.length && !fenceSeen) {
          prose.push(trimWs(s.slice(i, bound)));
        }
        i = bound < n ? bound + 1 : n;
        continue;
      }
      i++;
    }

    // ----------------------------------------------------------- EOF
    while (stack.length) {
      const fr = stack.pop();
      if (fr.kind === "o" && fr.key !== undefined &&
          !Object.prototype.hasOwnProperty.call(fr.container, fr.key)) {
        fr.container[fr.key] = null;
      }
      const v = this.popValue(fr);
      if (v === SKIP) {
        if (stack.length && stack[stack.length - 1].kind === "o") {
          stack[stack.length - 1].key = undefined;
        }
        continue;
      }
      if (stack.length) {
        const top = stack[stack.length - 1];
        if (top.kind === "o") {
          if (top.key !== undefined) {
            top.container[top.key] = v;
            top.key = undefined;
          }
        } else {
          top.container.push(v);
        }
      } else {
        values.push(v);
      }
    }

    if (values.length) {
      this.result = values.length === 1 ? values[0] : values.slice();
    } else if (prose.length) {
      const text = prose.filter(Boolean).join("\n");
      this.result = text ? text : SKIP;
    } else {
      this.result = SKIP;
    }
  }

  // ------------------------------------------------------------------
  // scalar sub-machines
  // ------------------------------------------------------------------

  * _scalar(i, ctx) {
    let s = this.s;
    let n = this.n;
    let wrapped = 0;
    let value;
    scalarloop:
    for (;;) {
      let c = s[i];
      if (QUOTES.has(c)) {
        s = null;
        [value, i] = yield* this._string(i, ctx, false);
        s = this.s;
        n = this.n;
        // string concatenation: "a" + "b"
        concat:
        for (;;) {
          let j = i;
          for (;;) {
            while (j < n && WS.has(s[j])) j++;
            if (j >= n && !this.final) {
              s = null; yield; s = this.s; n = this.n;
              continue;
            }
            break;
          }
          if (j < n && s[j] === "+") {
            j++;
            for (;;) {
              while (j < n && WS.has(s[j])) j++;
              if (j >= n && !this.final) {
                s = null; yield; s = this.s; n = this.n;
                continue;
              }
              if (j + 1 < n && s[j] === "/" && s[j + 1] === "*") {
                const e = s.indexOf("*/", j + 2);
                if (e === -1 && !this.final) {
                  s = null; yield; s = this.s; n = this.n;
                  continue;
                }
                if (e !== -1) { j = e + 2; continue; }
              }
              if (j < n && s[j] === "/" && !this.final && j + 1 >= n) {
                s = null; yield; s = this.s; n = this.n;
                continue;
              }
              break;
            }
            if (j < n && QUOTES.has(s[j]) && typeof value === "string") {
              s = null;
              let more;
              [more, i] = yield* this._string(j, ctx, false);
              s = this.s;
              n = this.n;
              value = value + more;
              continue;
            }
            if (j >= n) i = j;
          }
          break;
        }
        break;
      }
      if (c === "\\") {
        while (i + 1 >= n && !this.final) {
          s = null; yield; s = this.s; n = this.n;
        }
        if (i + 1 < n && QUOTES.has(s[i + 1])) {
          s = null;
          [value, i] = yield* this._string(i + 1, ctx, true);
          s = this.s;
          n = this.n;
          break;
        }
        i++;
        value = SKIP;
        break;
      }
      if (c === "−") { // unicode minus
        c = "-";
        s = s.slice(0, i) + "-" + s.slice(i + 1);
        this.s = s;
      }
      if (isDigit(c) || c === "+" || c === "-" || c === ".") {
        let m = matchAt(NUM_RE, s, i);
        while (m === null && i + 1 >= n && !this.final) {
          s = null; yield; s = this.s; n = this.n;
          m = matchAt(NUM_RE, s, i);
        }
        if (m) {
          while (i + m[0].length >= n && !this.final) {
            s = null; yield; s = this.s; n = this.n;
            m = matchAt(NUM_RE, s, i);
          }
          const tok = m[0];
          const k = i + tok.length;
          const nc = k < n ? s[k] : "";
          if (nc === "" || WS.has(nc) || ",}]);{[".includes(nc)) {
            let v = convertNumber(tok);
            if (v === null) {
              v = tok;
            } else if (typeof v === "number" && !Number.isFinite(v)) {
              this.hadNonfinite = true;
            }
            value = v;
            i = k;
            break;
          }
          if (nc === '"') {
            const v = convertNumber(tok);
            if (v !== null &&
                !(typeof v === "number" && !Number.isFinite(v))) {
              value = v;
              i = k + 1;
              break;
            }
          }
          s = null;
          [value, i] = yield* this._unquoted(i, ctx);
          s = this.s;
          n = this.n;
          break;
        }
        let mw = matchAt(WORD_RE, s, i + 1);
        while (mw === null && i + 1 >= n && !this.final) {
          s = null; yield; s = this.s; n = this.n;
          mw = matchAt(WORD_RE, s, i + 1);
        }
        if (mw && (c === "+" || c === "-")) {
          while (i + 1 + mw[0].length >= n && !this.final) {
            s = null; yield; s = this.s; n = this.n;
            mw = matchAt(WORD_RE, s, i + 1);
          }
          const word = mw[0];
          const lit = LITERALS.has(word) ? LITERALS.get(word) : SKIP;
          if (typeof lit === "number" && !Number.isFinite(lit)) {
            this.hadNonfinite = true;
            value = c === "-" ? -lit : lit;
            i = i + 1 + word.length;
            break;
          }
        }
        i++;
        value = SKIP;
        break;
      }
      {
        let m = matchAt(WORD_RE, s, i);
        if (m) {
          while (i + m[0].length >= n && !this.final) {
            s = null; yield; s = this.s; n = this.n;
            m = matchAt(WORD_RE, s, i);
          }
          const word = m[0];
          const k = i + word.length;
          const nc = k < n ? s[k] : "";
          if (LITERALS.has(word)) {
            let p = k;
            for (;;) {
              while (p < n && WS.has(s[p])) p++;
              if (p >= n && !this.final) {
                s = null; yield; s = this.s; n = this.n;
                continue;
              }
              break;
            }
            const pc = p < n ? s[p] : "";
            if (pc === "" || ',}])"'.includes(pc) ||
                (pc === ":" && ctx === ARR)) {
              value = LITERALS.get(word);
              if (typeof value === "number" && !Number.isFinite(value)) {
                this.hadNonfinite = true;
              }
              i = k;
              break;
            }
          }
          if (nc === "(") {
            i = k + 1;
            wrapped++;
            for (;;) {
              while (i < n && WS.has(s[i])) i++;
              if (i >= n && !this.final) {
                s = null; yield; s = this.s; n = this.n;
                continue;
              }
              break;
            }
            if (i >= n) {
              value = SKIP;
              break;
            }
            continue;
          }
          s = null;
          [value, i] = yield* this._unquoted(i, ctx);
          s = this.s;
          n = this.n;
          break;
        }
      }
      const i0 = i;
      s = null;
      [value, i] = yield* this._unquoted(i, ctx);
      s = this.s;
      n = this.n;
      if (value === SKIP && i <= i0) i = i0 + 1;
      break;
    }

    while (wrapped) {
      for (;;) {
        while (i < n && WS.has(s[i])) i++;
        if (i >= n && !this.final) {
          s = null; yield; s = this.s; n = this.n;
          continue;
        }
        break;
      }
      if (i < n && s[i] === ")") i++;
      wrapped--;
    }
    return [value, i];
  }

  // ------------------------------------------------------------------

  * _unquoted(i, ctx) {
    let s = this.s;
    let n = this.n;
    const start = i;
    let depth = 0;
    const stops = ctx === TOP ? "\n" : ',}]"\n';
    for (;;) {
      while (i < n) {
        const ch = s[i];
        if (depth === 0 && stops.includes(ch)) break;
        if (ch === "{" || ch === "[" || ch === "(") {
          depth++;
        } else if (ch === "}" || ch === "]" || ch === ")") {
          if (depth === 0) break;
          depth--;
        }
        i++;
      }
      if (i >= n && !this.final) {
        s = null; yield; s = this.s; n = this.n;
        continue;
      }
      break;
    }
    const raw = trimWs(s.slice(start, i));
    if (i < n && s[i] === '"' && i > start && !WS.has(s[i - 1]) &&
        !raw.startsWith("```")) {
      i++;
    }
    if (raw.startsWith("```")) {
      const inner = raw.slice(3);
      const m = matchAt(WORD_RE, inner, 0);
      if (m && m.index === 0 && !trimWs(inner.slice(m[0].length))) {
        return [SKIP, i];
      }
    }
    if (i >= n && this.final && raw) {
      for (const lit of ["true", "false", "null", "True", "False", "None"]) {
        if (lit.startsWith(raw) && raw.length < lit.length) {
          return [LITERALS.get(lit), i];
        }
      }
    }
    if (!raw || /^\.+$/.test(raw)) return [SKIP, i];
    const v = convertNumber(raw);
    if (v !== null) {
      if (typeof v === "number" && !Number.isFinite(v)) {
        this.hadNonfinite = true;
      }
      return [v, i];
    }
    if (LITERALS.has(raw)) {
      const lv = LITERALS.get(raw);
      if (typeof lv === "number" && !Number.isFinite(lv)) {
        this.hadNonfinite = true;
      }
      return [lv, i];
    }
    return [raw, i];
  }

  // ------------------------------------------------------------------

  * _key(i) {
    let s = this.s;
    let n = this.n;
    const c = s[i];
    if (QUOTES.has(c)) {
      s = null;
      let value;
      [value, i] = yield* this._string(i, OKEY, false);
      return [typeof value === "string" ? value : toKey(value), i];
    }
    if (c === "\\") {
      while (i + 1 >= n && !this.final) {
        s = null; yield; s = this.s; n = this.n;
      }
      if (i + 1 < n && QUOTES.has(s[i + 1])) {
        s = null;
        let value;
        [value, i] = yield* this._string(i + 1, OKEY, true);
        return [typeof value === "string" ? value : toKey(value), i];
      }
      return [SKIP, i + 1];
    }
    const start = i;
    for (;;) {
      while (i < n && !':,}]"=\n'.includes(s[i]) && !WS.has(s[i])) i++;
      if (i >= n && !this.final) {
        s = null; yield; s = this.s; n = this.n;
        continue;
      }
      break;
    }
    if (i < n && s[i] === '"' && i > start && !WS.has(s[i - 1])) {
      const raw = trimWs(s.slice(start, i));
      i++;
      return [raw ? raw : SKIP, i];
    }
    const raw = trimWs(s.slice(start, i));
    if (!raw) return [SKIP, i];
    return [raw, i];
  }

  // ------------------------------------------------------------------

  * _string(i, ctx, escDelim) {
    let s = this.s;
    let n = this.n;
    const q = s[i];
    const closers = QUOTES.get(q);
    i++;
    let start = i;

    // snapshot bookkeeping for streaming previews
    const stk = this.stack;
    if (ctx === OVAL && stk.length &&
        stk[stk.length - 1].key !== undefined) {
      this.partial = [stk[stk.length - 1].container, start];
    } else if (ctx === ARR && stk.length) {
      this.partial = [stk[stk.length - 1].container, start];
    } else if (ctx === TOP) {
      this.partial = [null, start];
    }

    let scan = start;
    let kscan = start;
    let nlChecked = start;
    let lastCand = -1;

    try {
      for (;;) {
        let j;
        if (closers.length === 1) {
          j = s.indexOf(closers, scan);
        } else {
          j = -1;
          for (const cc of closers) {
            const f = s.indexOf(cc, scan);
            if (f !== -1 && (j === -1 || f < j)) j = f;
          }
        }
        let cpos = -1;
        if (ctx === OKEY) {
          cpos = findIn(s, ":", kscan, j !== -1 ? j : n);
        }

        if (j === -1) {
          if (!this.final) {
            scan = n;
            s = null; yield; s = this.s; n = this.n;
            continue;
          }
          // ---- truncated input
          if (cpos !== -1 && ctx === OKEY) {
            return [finishString(s.slice(start, cpos)), cpos];
          }
          const raw = s.slice(start, n);
          const rs = trimEndWs(raw);
          if (ctx !== TOP && rs) {
            let m = 0;
            while (m < rs.length &&
                   (rs[rs.length - 1 - m] === "}" ||
                    rs[rs.length - 1 - m] === "]")) m++;
            if (m && m === this.stack.length) {
              const body = trimEndWs(rs.slice(0, rs.length - m));
              return [finishString(body), start + rs.length - m];
            }
          }
          if (rs.endsWith("+")) {
            return [finishString(trimEndWs(rs.slice(0, -1))), n];
          }
          return [finishString(stripPartialEscape(raw)), n];
        }

        // escaped quote?
        let b = 0;
        while (j - 1 - b >= start - 1 && s[j - 1 - b] === "\\") b++;
        let endContent;
        if (escDelim) {
          if (b >= 2) { scan = kscan = j + 1; continue; }
          endContent = j - b;
        } else {
          if (b % 2 === 1) { scan = kscan = j + 1; continue; }
          endContent = j;
        }

        // ---- newline heuristics
        let nl = findIn(s, "\n", nlChecked, j);
        let early = -1;
        while (nl !== -1) {
          let p = nl + 1;
          while (p < j && (s[p] === " " || s[p] === "\t")) p++;
          const cnl = p < j ? s[p] : (p === j ? q : "");
          if (p >= j || cnl === "}" || cnl === "]") {
            early = nl;
            break;
          }
          nlChecked = nl + 1;
          nl = findIn(s, "\n", nlChecked, j);
        }
        // [nlChecked, j) is now known newline-free; never rescan it
        nlChecked = j;
        if (early !== -1) {
          const raw = trimEndWs(s.slice(start, early));
          if (raw.endsWith(",")) {
            const body = trimEndWs(raw.slice(0, -1));
            return [finishString(body), start + raw.length - 1];
          }
          return [finishString(raw), early];
        }

        if (ctx === OKEY && cpos !== -1) {
          let p = j + 1;
          for (;;) {
            while (p < n && WS.has(s[p])) p++;
            if (p >= n && !this.final) {
              s = null; yield; s = this.s; n = this.n;
              continue;
            }
            break;
          }
          if (p < n && (s[p] === ":" || s[p] === "：")) {
            return [finishString(s.slice(start, endContent)), j + 1];
          }
          return [finishString(s.slice(start, cpos)), cpos];
        }

        // ---- the close decision: peek the next meaningful char
        this.partialEnd = j;
        let p = j + 1;
        for (;;) {
          while (p < n && WS.has(s[p])) p++;
          if (p >= n && !this.final) {
            s = null; yield; s = this.s; n = this.n;
            continue;
          }
          if (p < n && s[p] === "/" && p + 1 >= n && !this.final) {
            s = null; yield; s = this.s; n = this.n;
            continue;
          }
          break;
        }
        this.partialEnd = null;
        const nxt = p < n ? s[p] : "";

        if (j === start && p === j + 1 && (isAlnum(nxt) || nxt === "_")) {
          start = scan = kscan = j + 1;
          if (ctx !== OKEY) cpos = -1;
          continue;
        }
        if (nxt === "") {
          return [finishString(s.slice(start, endContent)), j + 1];
        }
        if (p === j + 1 && nxt === q && j > start) {
          return [finishString(s.slice(start, endContent)), p + 1];
        }
        if (",}])+".includes(nxt)) {
          return [finishString(s.slice(start, endContent)), j + 1];
        }
        if (nxt === "#" || (nxt === "/" && p + 1 < n &&
            (s[p + 1] === "/" || s[p + 1] === "*"))) {
          return [finishString(s.slice(start, endContent)), j + 1];
        }
        if ((ctx === OVAL || ctx === ARR) &&
            findIn(s, "\n", j + 1, p) !== -1) {
          return [finishString(s.slice(start, endContent)), j + 1];
        }
        if (ctx === OKEY) {
          if (nxt === "：" || isDigit(nxt) || nxt === "{" ||
              nxt === "[" ||
              (p > j + 1 && (isAlpha(nxt) || QUOTES.has(nxt)))) {
            return [finishString(s.slice(start, endContent)), j + 1];
          }
        }
        if (nxt === ":") {
          if (ctx === OKEY || ctx === ARR || ctx === TOP) {
            return [finishString(s.slice(start, endContent)), j + 1];
          }
          if (lastCand !== -1) {
            const raw = s.slice(start, lastCand);
            const stripped = trimEndWs(raw);
            if (stripped.endsWith(",")) {
              const body = trimEndWs(stripped.slice(0, -1));
              return [finishString(body), start + stripped.length - 1];
            }
            return [finishString(raw), lastCand + 1];
          }
          return [finishString(s.slice(start, endContent)), j + 1];
        }
        if (QUOTES.has(nxt) &&
            (ctx === ARR || ctx === TOP || ctx === OKEY)) {
          return [finishString(s.slice(start, endContent)), j + 1];
        }
        if (ctx === ARR && (isDigit(nxt) || nxt === "+" || nxt === "-")) {
          return [finishString(s.slice(start, endContent)), j + 1];
        }
        if (ctx === ARR && isAlpha(nxt)) {
          let mm = matchAt(WORD_RE, s, p);
          while (mm && p + mm[0].length >= n && !this.final) {
            s = null; yield; s = this.s; n = this.n;
            mm = matchAt(WORD_RE, s, p);
          }
          if (mm && LITERALS.has(mm[0])) {
            let e = p + mm[0].length;
            for (;;) {
              while (e < n && WS.has(s[e])) e++;
              if (e >= n && !this.final) {
                s = null; yield; s = this.s; n = this.n;
                continue;
              }
              break;
            }
            if (e >= n || ",]}".includes(s[e])) {
              return [finishString(s.slice(start, endContent)), j + 1];
            }
          }
        }

        lastCand = j;
        scan = kscan = j + 1;
      }
    } finally {
      this.partial = null;
      this.partialEnd = null;
    }
  }
}

// ---------------------------------------------------------------------------
// serialization (BigInt-aware, RFC 8259-safe)
// ---------------------------------------------------------------------------

function dumps(value) {
  const out = [];
  const stack = [["v", value]];
  while (stack.length) {
    const [op, v] = stack.pop();
    if (op === "t") { out.push(v); continue; }
    if (Array.isArray(v)) {
      out.push("[");
      stack.push(["t", "]"]);
      for (let idx = v.length - 1; idx >= 0; idx--) {
        stack.push(["v", v[idx]]);
        if (idx) stack.push(["t", ", "]);
      }
      continue;
    }
    if (v !== null && typeof v === "object" && typeof v !== "function" &&
        typeof v !== "bigint") {
      out.push("{");
      stack.push(["t", "}"]);
      const keys = Object.keys(v);
      for (let idx = keys.length - 1; idx >= 0; idx--) {
        stack.push(["v", v[keys[idx]]]);
        stack.push(["t", JSON.stringify(keys[idx]) + ": "]);
        if (idx) stack.push(["t", ", "]);
      }
      continue;
    }
    if (v === true) out.push("true");
    else if (v === false) out.push("false");
    else if (v === null || v === undefined) out.push("null");
    else if (typeof v === "bigint") out.push(v.toString());
    else if (typeof v === "number") {
      out.push(Number.isFinite(v) ? JSON.stringify(v) : "null");
    } else out.push(JSON.stringify(String(v)));
  }
  return out.join("");
}

// ---------------------------------------------------------------------------
// public API
// ---------------------------------------------------------------------------

function mend(text, options) {
  const strict = options && options.strict;
  if (typeof text !== "string") text = String(text);
  if (text && text[0] === "﻿") text = text.replace(/^﻿+/, "");
  const machine = new MendMachine();
  machine.final = true;
  machine.feed(text);
  const result = machine.close();
  if (result === SKIP) {
    if (strict) throw new JSONMendError("no JSON content found in input");
    return "";
  }
  return result;
}

// JSON.parse loses precision on integers beyond 2^53; route inputs
// containing long digit runs through the mender, which uses BigInt.
const LONG_INT_RE = /\d{16,}/;

function loads(text, options) {
  if (typeof text !== "string") text = String(text);
  if (!(options && options.skipJsonParse) && !LONG_INT_RE.test(text)) {
    try {
      return JSON.parse(text);
    } catch (e) {
      // fall through to the mender
    }
  }
  return mend(text, options);
}

function repairJson(text, options) {
  options = options || {};
  if (typeof text !== "string") text = String(text);
  let value;
  let parsed = false;
  if (!options.skipJsonParse && !LONG_INT_RE.test(text)) {
    try {
      value = JSON.parse(text);
      parsed = true;
    } catch (e) {
      parsed = false;
    }
  }
  if (!parsed) value = mend(text, options);
  if (options.returnObjects) return value;
  return dumps(value);
}

class Mender {
  constructor() {
    this._machine = new MendMachine();
    this._closed = false;
    this._result = null;
  }

  feed(chunk) {
    if (this._closed) throw new Error("Mender is closed");
    if (typeof chunk !== "string") chunk = String(chunk);
    this._machine.feed(chunk);
    return this._machine.current();
  }

  get value() {
    if (this._closed) return this._result;
    return this._machine.current();
  }

  close() {
    if (!this._closed) {
      const result = this._machine.close();
      this._result = result === SKIP ? "" : result;
      this._closed = true;
    }
    return this._result;
  }
}

module.exports = {
  repairJson,
  jsonmend: repairJson,
  loads,
  mend,
  Mender,
  JSONMendError,
};
