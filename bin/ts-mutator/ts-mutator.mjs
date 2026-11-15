#!/usr/bin/env node
// ts-mutator.mjs -- AST-span mutant generator for verify-relevance.py.
//
// The TS/JS analogue of python_mutants(): given a target file and the set of
// line numbers the reference impl ADDED (the diff hunks), emit property-breaking
// mutants restricted to those lines, mirroring the Python mutation table
// (comparison flip / boolean glue / literal nudge / condition negate|force /
// call deletion) and the three safe-direction filters (added-lines-only,
// innocuous calls skipped, compile-only sites skipped).
//
// CONTRACT
//   stdin : JSON {"path": "<abs file>", "addedLines": [<1-based ints>]}
//   stdout: JSON {"mutants": [ {class, desc, line, start, end, original,
//                               replacement, source} ], "error": null}
//           `source` is the FULL mutated file text (the caller wraps it in a
//           Mutant exactly as python_mutants does; no offset math on the Python
//           side).  On a fatal error: {"mutants": [], "error": "<msg>"}.
//
// Only span replacements that (a) fall on an added line and (b) re-parse with
// zero parse diagnostics are emitted -- an invalid mutant is dropped, never
// counted, the safe direction.
import ts from "typescript";
import fs from "fs";

const INNOCUOUS = new Set([
  "console", "log", "debug", "info", "warn", "warning", "error", "exception",
  "critical", "trace", "logger", "logging", "print", "assert", "dir", "pprint",
]);

function scriptKindFor(p) {
  if (p.endsWith(".tsx")) return ts.ScriptKind.TSX;
  if (p.endsWith(".jsx")) return ts.ScriptKind.JSX;
  if (p.endsWith(".ts")) return ts.ScriptKind.TS;
  return ts.ScriptKind.JS; // .js .mjs .cjs
}

function main() {
  let input;
  try {
    input = JSON.parse(fs.readFileSync(0, "utf8"));
  } catch (e) {
    return { mutants: [], error: "bad stdin json: " + e.message };
  }
  const { path, addedLines } = input;
  const added = new Set(addedLines || []);
  let text;
  try {
    text = fs.readFileSync(path, "utf8");
  } catch (e) {
    return { mutants: [], error: "read " + path + ": " + e.message };
  }
  const kind = scriptKindFor(path);
  const target = ts.ScriptTarget.Latest;

  function parseErrs(src) {
    const sf = ts.createSourceFile("m", src, target, true, kind);
    return (sf.parseDiagnostics || []).length;
  }
  const root = ts.createSourceFile(path, text, target, true, kind);
  if (parseErrs(text) > 0) {
    // like python_mutants returning [] on SyntaxError: refuse to mutate a file
    // we cannot parse cleanly.
    return { mutants: [], error: "source does not parse cleanly" };
  }

  const out = [];
  const seen = new Set();

  const lineOf = (pos) => root.getLineAndCharacterOfPosition(pos).line + 1;

  function emit(klass, desc, start, end, replacement) {
    const line = lineOf(start);
    if (!added.has(line)) return;
    const src = text.slice(0, start) + replacement + text.slice(end);
    if (src === text) return;
    if (parseErrs(src) > 0) return;
    if (seen.has(src)) return;
    seen.add(src);
    out.push({
      class: klass, desc, line, start, end,
      original: text.slice(start, end), replacement, source: src,
    });
  }

  // ---- ancestor-based skip filters (compile-only / innocuous positions) ----
  function inSkippedContext(node) {
    for (let p = node.parent; p; p = p.parent) {
      if (ts.isTypeNode(p)) return true;
      const k = p.kind;
      if (k === ts.SyntaxKind.TypeAliasDeclaration ||
          k === ts.SyntaxKind.InterfaceDeclaration ||
          k === ts.SyntaxKind.ImportDeclaration ||
          k === ts.SyntaxKind.ImportEqualsDeclaration ||
          k === ts.SyntaxKind.ExportDeclaration ||
          k === ts.SyntaxKind.ExportAssignment ||
          k === ts.SyntaxKind.ModuleDeclaration && p.flags & ts.NodeFlags.Namespace)
        return true;
    }
    return false;
  }

  function calleeNames(call) {
    const names = [];
    let e = call.expression;
    if (ts.isIdentifier(e)) names.push(e.text);
    else if (ts.isPropertyAccessExpression(e)) {
      names.push(e.name.text);
      let r = e.expression;
      while (ts.isPropertyAccessExpression(r)) r = r.expression;
      if (ts.isIdentifier(r)) names.push(r.text);
    }
    return names;
  }
  function isInnocuousCall(call) {
    const ns = calleeNames(call);
    return ns.some((n) => INNOCUOUS.has(n) || n.startsWith("log"));
  }
  function insideInnocuousCall(node) {
    for (let p = node.parent; p; p = p.parent) {
      if (ts.isCallExpression(p) && isInnocuousCall(p)) return true;
    }
    return false;
  }

  const CMP = {
    [ts.SyntaxKind.LessThanToken]: ["<", ["<=", ">="]],
    [ts.SyntaxKind.LessThanEqualsToken]: ["<=", ["<", ">"]],
    [ts.SyntaxKind.GreaterThanToken]: [">", [">=", "<="]],
    [ts.SyntaxKind.GreaterThanEqualsToken]: [">=", [">", "<"]],
    [ts.SyntaxKind.EqualsEqualsToken]: ["==", ["!="]],
    [ts.SyntaxKind.ExclamationEqualsToken]: ["!=", ["=="]],
    [ts.SyntaxKind.EqualsEqualsEqualsToken]: ["===", ["!=="]],
    [ts.SyntaxKind.ExclamationEqualsEqualsToken]: ["!==", ["==="]],
  };
  const LOGIC = {
    [ts.SyntaxKind.AmpersandAmpersandToken]: ["&&", "||"],
    [ts.SyntaxKind.BarBarToken]: ["||", "&&"],
  };
  // Array-predicate method swaps whose swap changes the truth value the call
  // yields -- the ANY-vs-ALL bug class this whole check exists to catch. Only
  // pairs that are unambiguous renames AND behaviour-changing are listed:
  //   .every <-> .some : universal vs existential quantifier. `[].every` is
  //       true, `[].some` is false, and for any non-uniform array the two
  //       disagree -- the exact `.every(t => has(t))` "fully credited" bug.
  // find/findLast (and findIndex/findLastIndex) are deliberately NOT here: on
  // the common single-match input they return the same element, so the swap is
  // an equivalent mutant that would SURVIVE and wrongly drag a relevant verify
  // down. The docstring's safe-direction rule (fewer mutants, never noise that
  // flatters or falsely penalises) governs -- leave the ambiguous ones out.
  const METHOD_SWAP = { every: "some", some: "every" };

  function visit(node) {
    if (!inSkippedContext(node)) {
      handle(node);
    }
    ts.forEachChild(node, visit);
  }

  function handle(node) {
    // ---- binary: comparison flips, logical glue swap ----
    if (ts.isBinaryExpression(node)) {
      const op = node.operatorToken;
      const s = op.getStart(root), e = op.getEnd();
      if (CMP[op.kind]) {
        const [cur, flips] = CMP[op.kind];
        for (const nw of flips) emit("compare-flip", `${cur} -> ${nw}`, s, e, nw);
      } else if (LOGIC[op.kind]) {
        const [cur, nw] = LOGIC[op.kind];
        emit("boolop-swap", `${cur} -> ${nw}`, s, e, nw);
      }
    }
    // ---- boolean literal flip ----
    if (node.kind === ts.SyntaxKind.TrueKeyword) {
      emit("const-bool", "true -> false", node.getStart(root), node.getEnd(), "false");
    } else if (node.kind === ts.SyntaxKind.FalseKeyword) {
      emit("const-bool", "false -> true", node.getStart(root), node.getEnd(), "true");
    }
    // ---- numeric literal perturbation ----
    if (ts.isNumericLiteral(node) && !insideInnocuousCall(node)) {
      const v = Number(node.getText());
      if (Number.isFinite(v)) {
        const s = node.getStart(root), e = node.getEnd();
        const par = node.parent;
        const isFallback = par && ts.isBinaryExpression(par) &&
          par.operatorToken.kind === ts.SyntaxKind.BarBarToken && par.right === node;
        if (isFallback) {
          // a `x || 0` default: nudging by one stays under any threshold, so
          // push it far (mirror Python const-default).
          emit("const-default", `fallback ${v} -> ${v + 1000003}`, s, e, String(v + 1000003));
        } else {
          for (const nv of [v + 1, v - 1]) emit("const-int", `${v} -> ${nv}`, s, e, String(nv));
        }
      }
    }
    // ---- string literal perturbation (behaviour-bearing positions only) ----
    if (ts.isStringLiteral(node) && !insideInnocuousCall(node)) {
      const par = node.parent;
      const bearing = par && (
        ts.isBinaryExpression(par) || ts.isReturnStatement(par) ||
        ts.isVariableDeclaration(par) || ts.isPropertyAssignment(par) ||
        ts.isElementAccessExpression(par) || ts.isCaseClause(par) ||
        (ts.isCallExpression(par) && !isInnocuousCall(par)));
      if (bearing && node.text.indexOf("\n") < 0) {
        const raw = node.getText();
        const q = raw[0];
        if (q === '"' || q === "'" || q === "`") {
          const mutated = raw.slice(0, -1) + "_X" + q;
          emit("const-str", `${JSON.stringify(node.text)} -> append _X`,
               node.getStart(root), node.getEnd(), mutated);
        }
      }
    }
    // ---- condition negate / force (if / while / ternary) ----
    let cond = null;
    if (ts.isIfStatement(node) || ts.isWhileStatement(node)) cond = node.expression;
    else if (ts.isConditionalExpression(node)) cond = node.condition;
    if (cond) {
      const s = cond.getStart(root), e = cond.getEnd();
      const t = text.slice(s, e);
      emit("cond-negate", `negate \`${t.slice(0, 40)}\``, s, e, `!(${t})`);
      emit("cond-force-true", `force \`${t.slice(0, 40)}\` true`, s, e, "true");
      emit("cond-force-false", `force \`${t.slice(0, 40)}\` false`, s, e, "false");
    }
    // ---- array-predicate method swap (.every <-> .some) ----
    // Mutates only the method-name identifier of a `recv.method(...)` call, so
    // it re-uses emit() (added-line + re-parse guards apply) and is skipped in
    // logging/innocuous calls exactly like the literal operators.
    if (ts.isCallExpression(node) && !isInnocuousCall(node) &&
        !insideInnocuousCall(node) &&
        ts.isPropertyAccessExpression(node.expression)) {
      const nameNode = node.expression.name;
      const cur = nameNode.text;
      if (Object.prototype.hasOwnProperty.call(METHOD_SWAP, cur)) {
        const nw = METHOD_SWAP[cur];
        emit("method-swap", `.${cur}() -> .${nw}()`,
             nameNode.getStart(root), nameNode.getEnd(), nw);
      }
    }
    // ---- call deletion (with innocuous filtering) ----
    if (ts.isCallExpression(node) && !isInnocuousCall(node)) {
      const par = node.parent;
      if (par && ts.isExpressionStatement(par)) {
        // a side-effecting statement call: delete the whole statement.
        emit("stmt-delete", `delete \`${text.slice(par.getStart(root), par.getEnd()).slice(0, 40)}\``,
             par.getStart(root), par.getEnd(), ";");
      } else if (node.arguments.length > 0) {
        // a call whose value flows on: replace with its first argument (drops
        // the transform/guard the call applied).
        const a = node.arguments[0];
        emit("call-delete", `replace call with its first arg`,
             node.getStart(root), node.getEnd(), text.slice(a.getStart(root), a.getEnd()));
      } else {
        emit("call-delete", `replace call with undefined`,
             node.getStart(root), node.getEnd(), "undefined");
      }
    }
  }

  visit(root);
  return { mutants: out, error: null };
}

try {
  process.stdout.write(JSON.stringify(main()));
} catch (e) {
  process.stdout.write(JSON.stringify({ mutants: [], error: String(e && e.stack || e) }));
}
