#!/usr/bin/env node
// Self-test for ts-mutator.mjs. Drives the sidecar over crafted snippets and
// asserts the mutation contract, with a focus on the array-predicate
// method-swap operator (.every <-> .some) and the safe-direction guards it must
// keep: added-lines-only, innocuous-call skipped, every emitted mutant
// re-parses. Exit 0 + SELF_CHECK_OK on success, else exit 1.
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import fs from "node:fs";
import os from "node:os";
import ts from "typescript";

const HERE = dirname(fileURLToPath(import.meta.url));
const SIDECAR = join(HERE, "ts-mutator.mjs");

function mutate(text, addedLines, ext = ".ts") {
  const p = join(fs.mkdtempSync(join(os.tmpdir(), "tsmut-")), "m" + ext);
  fs.writeFileSync(p, text);
  const out = execFileSync("node", [SIDECAR], {
    input: JSON.stringify({ path: p, addedLines }), encoding: "utf8",
  });
  return JSON.parse(out);
}

let fails = 0;
const chk = (n, c) => { if (!c) { console.error("FAIL:", n); fails++; } else console.log("ok:", n); };
const parses = (src) =>
  (ts.createSourceFile("x", src, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS).parseDiagnostics || []).length === 0;

// The exact ANY-vs-ALL shape a "fully credited" extraction fix introduces.
const IOFC = `export function isOrderFullyCredited(orderTrackings: string[], creditedTrackings: Set<string>): boolean {
  if (orderTrackings.length === 0) return false;
  return orderTrackings.every((t) => creditedTrackings.has(t));
}
`;

// (1) method-swap fires on `.every` on an added line and re-parses to `.some`
let r = mutate(IOFC, [1, 2, 3]);
chk("iofc: no error", r.error === null);
let sw = r.mutants.filter((m) => m.class === "method-swap");
chk("iofc: exactly one method-swap", sw.length === 1);
chk("iofc: swap is .every() -> .some()", sw[0] && sw[0].desc === ".every() -> .some()");
chk("iofc: swap lands on the .every line (3)", sw[0] && sw[0].line === 3);
chk("iofc: swap replaces the method identifier only",
    sw[0] && sw[0].original === "every" && sw[0].replacement === "some");
chk("iofc: swapped source contains .some(", sw[0] && sw[0].source.includes("orderTrackings.some("));
chk("iofc: swapped source no longer contains .every(", sw[0] && !sw[0].source.includes(".every("));
chk("iofc: swapped source preserves the signature literal",
    sw[0] && sw[0].source.includes("export function isOrderFullyCredited"));

// (2) EVERY emitted mutant (all classes) re-parses -- the invariant the whole
//     tool leans on: a mutant that fails to re-parse must never be emitted.
chk("iofc: every emitted mutant re-parses", r.mutants.every((m) => parses(m.source)));

// (3) the swap is symmetric: .some -> .every
let s = mutate(`export const f = (xs: number[]): boolean => xs.some((x) => x > 0);\n`, [1]);
let sw2 = s.mutants.filter((m) => m.class === "method-swap");
chk("some: swap is .some() -> .every()",
    sw2.length === 1 && sw2[0].desc === ".some() -> .every()" && sw2[0].replacement === "every");

// (4) added-lines-only: if the .every line is NOT in addedLines, no swap is emitted
let r2 = mutate(IOFC, [1, 2]); // line 3 (the .every) excluded
chk("added-only: no method-swap when the .every line is not added",
    r2.mutants.filter((m) => m.class === "method-swap").length === 0);

// (5) innocuous-call skip: a `.every` used INSIDE console.log(...) is not mutated
let inno = mutate(`export function g(xs: number[]) {\n  console.log(xs.every((x) => x > 0));\n}\n`, [1, 2, 3]);
chk("innocuous: no method-swap inside console.log(...)",
    inno.mutants.filter((m) => m.class === "method-swap").length === 0);

// (6) a plain .every not on an added line stays untouched while an added one is
//     mutated (mixed) -- proves the added-line gate is per-site
let mixed = mutate(`export const a = [1].every((x) => x > 0);\nexport const b = [2].some((x) => x > 0);\n`, [2]);
let sw3 = mixed.mutants.filter((m) => m.class === "method-swap");
chk("mixed: only the added line's predicate is swapped",
    sw3.length === 1 && sw3[0].line === 2 && sw3[0].desc === ".some() -> .every()");

console.log(`--- ${fails} failed ---`);
if (fails === 0) console.log("SELF_CHECK_OK"); else process.exit(1);
