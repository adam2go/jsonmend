#!/usr/bin/env node
/* Corpus + property tests for the JS port. */
"use strict";

const fs = require("fs");
const path = require("path");
const { repairJson, mend, Mender, JSONMendError } = require("./index.cjs");

const CASES = path.join(__dirname, "..", "corpus", "cases");

function valuesEqual(a, b) {
  const stack = [[a, b]];
  while (stack.length) {
    const [x, y] = stack.pop();
    const xb = typeof x === "boolean", yb = typeof y === "boolean";
    if (xb || yb) {
      if (!(xb && yb && x === y)) return false;
      continue;
    }
    const xn = typeof x === "number" || typeof x === "bigint";
    const yn = typeof y === "number" || typeof y === "bigint";
    if (xn && yn) {
      if (typeof x === "number" && Number.isNaN(x)) {
        if (!(typeof y === "number" && Number.isNaN(y))) return false;
        continue;
      }
      if (Number(x) !== Number(y)) return false;
      continue;
    }
    if (Array.isArray(x) !== Array.isArray(y)) return false;
    if (Array.isArray(x)) {
      if (x.length !== y.length) return false;
      for (let i = 0; i < x.length; i++) stack.push([x[i], y[i]]);
      continue;
    }
    const xo = x !== null && typeof x === "object";
    const yo = y !== null && typeof y === "object";
    if (xo !== yo) return false;
    if (xo) {
      const xk = Object.keys(x), yk = Object.keys(y);
      if (xk.length !== yk.length) return false;
      for (const k of xk) {
        if (!Object.prototype.hasOwnProperty.call(y, k)) return false;
        stack.push([x[k], y[k]]);
      }
      continue;
    }
    if (x !== y) return false;
  }
  return true;
}

function judge(c, name) {
  if (c.verdict === "unrecoverable") {
    let out;
    try {
      out = repairJson(c.input);
    } catch (e) {
      if (e instanceof JSONMendError) return null;
      return "threw " + e;
    }
    const t = out.trim();
    if (t === "" || t === '""' || t === "null") return null;
    return "produced a value from garbage: " + JSON.stringify(out);
  }
  let out;
  try {
    out = repairJson(c.input);
  } catch (e) {
    return "threw " + (e && e.stack);
  }
  let value;
  try {
    value = JSON.parse(out); // RFC: JSON.parse rejects NaN literals
  } catch (e) {
    return "output not valid JSON: " + JSON.stringify(out.slice(0, 80));
  }
  if (c.check === "valid") return null;
  if (c.verdict === "deterministic") {
    if (valuesEqual(value, c.expected)) return null;
    return "got " + JSON.stringify(value) + " expected " +
      JSON.stringify(c.expected);
  }
  for (const acc of c.accepted) {
    if (valuesEqual(value, acc)) return null;
  }
  return "got " + JSON.stringify(value) + " not in accepted";
}

function* walk(dir) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const p = path.join(dir, entry.name);
    if (entry.isDirectory()) yield* walk(p);
    else if (entry.name.endsWith(".json")) yield p;
  }
}

let total = 0, pass = 0;
const fails = [];
for (const file of walk(CASES)) {
  const c = JSON.parse(fs.readFileSync(file, "utf8"));
  const name = path.relative(CASES, file).replace(/\.json$/, "");
  total++;
  const why = judge(c, name);
  if (why === null) pass++;
  else fails.push([name, why]);
}
console.log(`corpus: ${pass}/${total} (${(100 * pass / total).toFixed(1)}%)`);
for (const [name, why] of fails.slice(0, Number(process.env.SHOW || 20))) {
  console.log(`- ${name}: ${why.slice(0, 200)}`);
}

// property: chunked streaming === batch (sampled)
let streamFails = 0, checked = 0;
for (const file of walk(CASES)) {
  const c = JSON.parse(fs.readFileSync(file, "utf8"));
  if (c.input.length > 5000) continue;
  checked++;
  const batch = mend(c.input);
  const m = new Mender();
  for (const ch of c.input) m.feed(ch);
  const streamed = m.close();
  if (JSON.stringify({ v: streamed }, bigintRepl) !==
      JSON.stringify({ v: batch }, bigintRepl)) {
    streamFails++;
    if (streamFails <= 5) {
      console.log("STREAM DIVERGENCE:", JSON.stringify(c.input.slice(0, 80)));
      console.log("  batch :", JSON.stringify(batch, bigintRepl));
      console.log("  stream:", JSON.stringify(streamed, bigintRepl));
    }
  }
}
function bigintRepl(_, v) {
  return typeof v === "bigint" ? v.toString() + "n" : v;
}
console.log(`streaming property: ${checked - streamFails}/${checked}`);

process.exit(fails.length || streamFails ? 1 : 0);
