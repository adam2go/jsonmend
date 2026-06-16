/**
 * jsonmend — mends the JSON your LLM almost wrote.
 * Type declarations for the JavaScript port.
 */

/** A parsed JSON-ish value. Integers beyond 2^53 are returned as bigint. */
export type MendValue =
  | null
  | boolean
  | number
  | bigint
  | string
  | MendValue[]
  | { [key: string]: MendValue };

export interface RepairOptions {
  /** Return the parsed value instead of a JSON string. */
  returnObjects?: boolean;
  /** Skip the `JSON.parse` fast path and always run the repair machine. */
  skipJsonParse?: boolean;
  /** Throw `JSONMendError` instead of returning "" when nothing is mendable. */
  strict?: boolean;
}

export class JSONMendError extends Error {
  readonly name: "JSONMendError";
}

/** Repair broken JSON and return a valid JSON string (or the parsed value
 *  when `returnObjects` is set). Output is always valid RFC 8259 JSON. */
export function repairJson(text: string, options?: RepairOptions): string;
export function repairJson(
  text: string,
  options: RepairOptions & { returnObjects: true },
): MendValue;

/** Alias of {@link repairJson} (jsonrepair-style name). */
export const jsonmend: typeof repairJson;

/** Repair and parse, returning the value. Uses a `JSON.parse` fast path
 *  for already-valid input unless `skipJsonParse` is set. */
export function loads(text: string, options?: RepairOptions): MendValue;

/** Repair and parse, always through the repair machine. Returns "" for
 *  unmendable input, or throws `JSONMendError` when `strict` is set. */
export function mend(
  text: string,
  options?: { strict?: boolean },
): MendValue | "";

/**
 * Stateful incremental mender. Each `feed` consumes one chunk and returns
 * the best-effort parsed value so far, paying only for the new bytes.
 */
export class Mender {
  constructor();
  /** Feed one chunk; returns the current best-effort value. */
  feed(chunk: string): MendValue | null;
  /** The current best-effort value without feeding. */
  readonly value: MendValue | null;
  /** Finish parsing and return the final mended value (or "" if empty). */
  close(): MendValue | "";
}

declare const _default: {
  repairJson: typeof repairJson;
  jsonmend: typeof repairJson;
  loads: typeof loads;
  mend: typeof mend;
  Mender: typeof Mender;
  JSONMendError: typeof JSONMendError;
};
export default _default;
