#!/usr/bin/env node
// ts-parse.mjs -- syntactic parse check for a TS/JS file (verify-harness Phase 2).
// The TS analogue of `python3 -c "ast.parse(open(f).read())"`: the scaffold's
// verify.sh and ollama-dispatch-preflight both need to know a .ts/.tsx/.js target
// PARSES before anything downstream trusts it -- today a TS syntax error slips
// past the preflight's ast.parse-only target check. Uses the vendored typescript
// (the same dependency ts-extract/ts-mutator reuse), so it needs no tsconfig.
//
// CONTRACT
//   argv : <path>
//   exit : 0 if the file parses with zero syntactic diagnostics,
//          1 if it does not (prints the first few), 2 on usage/read error.
import ts from "typescript";
import fs from "fs";

function scriptKindFor(p) {
  if (p.endsWith(".tsx")) return ts.ScriptKind.TSX;
  if (p.endsWith(".jsx")) return ts.ScriptKind.JSX;
  if (p.endsWith(".ts")) return ts.ScriptKind.TS;
  return ts.ScriptKind.JS; // .js .mjs .cjs
}

const p = process.argv[2];
if (!p) { console.error("usage: ts-parse.mjs <file>"); process.exit(2); }

let text;
try { text = fs.readFileSync(p, "utf8"); }
catch (e) { console.error("read " + p + ": " + e.message); process.exit(2); }

// createSourceFile is error-tolerant, but records every syntactic problem on
// the node's parseDiagnostics -- that list is exactly "did this file parse".
const sf = ts.createSourceFile(p, text, ts.ScriptTarget.Latest, true, scriptKindFor(p));
const diags = sf.parseDiagnostics || [];
if (diags.length) {
  for (const d of diags.slice(0, 5)) {
    const at = d.start != null ? sf.getLineAndCharacterOfPosition(d.start) : { line: -1, character: 0 };
    console.error(p + ":" + (at.line + 1) + ": " + ts.flattenDiagnosticMessageText(d.messageText, "\n"));
  }
  process.exit(1);
}
console.log(p + " parses (" + ts.ScriptKind[scriptKindFor(p)] + ")");
process.exit(0);
