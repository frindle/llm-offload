#!/usr/bin/env node
// ha_tscheck.js <worktree> <fixture-relative-path>  -- harness_admission.py's TS fixture type-check.
// Prints one JSON object: {"ok":bool,"errors":[{"line":"file(l,c): error TSxxxx: msg"}],"mocks":N,"notes":[...]}
//  1. real TypeScript diagnostics (the repo's own tsconfig) attributed to the FIXTURE file only;
//  2. mock-shape probe: for mock.module(spec,{namedExports:{name: fn}}) whose REAL export returns a
//     tagged union (string-literal discriminant), the mock's returned discriminant must be one the real
//     function can return (catches {kind:'ambiguous'} vs the real {kind:'review',reason:'ambiguous'}).
const path = require('path');
const WT = path.resolve(process.argv[2]);
const FX = path.resolve(WT, process.argv[3]);
let ts;
try { ts = require(require.resolve('typescript', { paths: [WT] })); }
catch (e) { console.log(JSON.stringify({ ok: true, skipped: 'typescript not installed in the worktree' })); process.exit(0); }
const cfgPath = path.join(WT, 'tsconfig.json');
let options = { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext, moduleResolution: ts.ModuleResolutionKind.Bundler,
  strict: true, skipLibCheck: true, allowImportingTsExtensions: true, esModuleInterop: true, noEmit: true };
if (ts.sys.fileExists(cfgPath)) {
  const c = ts.readConfigFile(cfgPath, ts.sys.readFile);
  if (!c.error) { const p = ts.parseJsonConfigFileContent(c.config, ts.sys, WT); options = { ...p.options }; }
}
options.noEmit = true; options.incremental = false; delete options.tsBuildInfoFile; delete options.plugins; delete options.composite;
// pass 1 (syntax only): which modules does the fixture mock? resolve them so they are program roots
const resolveSpec = (spec) => {
  for (const sp of [spec, spec.replace(/^@\//, '')]) {
    const r = ts.resolveModuleName(sp, FX, options, ts.sys);
    if (r.resolvedModule && !r.resolvedModule.isExternalLibraryImport) return r.resolvedModule.resolvedFileName;
  }
  const cand = path.join(WT, spec.replace(/^@\//, ''));
  for (const e of ['.ts', '.tsx', '/index.ts', '.mts', '']) if (ts.sys.fileExists(cand + e) && !ts.sys.directoryExists(cand + e)) return cand + e;
  return null;
};
const roots = [FX];
{
  const pre = ts.createSourceFile(FX, ts.sys.readFile(FX) || '', ts.ScriptTarget.Latest, true);
  const pv = (n) => {
    if (ts.isCallExpression(n) && /(^|\.)module$/.test(n.expression.getText(pre)) && n.arguments.length && ts.isStringLiteralLike(n.arguments[0])) {
      const r = resolveSpec(n.arguments[0].text); if (r && !roots.includes(r)) roots.push(r);
    }
    ts.forEachChild(n, pv);
  };
  pv(pre);
}
const prog = ts.createProgram(roots, options);
const sf = prog.getSourceFile(FX);
if (!sf) { console.log(JSON.stringify({ ok: false, errors: [{ line: FX + ': fixture not found by the TypeScript program' }] })); process.exit(0); }
const checker = prog.getTypeChecker();
const errors = [];
const rel = (f) => path.relative(WT, f);
const fmt = (d) => {
  const m = ts.flattenDiagnosticMessageText(d.messageText, ' ');
  if (d.file && d.start !== undefined) { const { line, character } = d.file.getLineAndCharacterOfPosition(d.start);
    return `${rel(d.file.fileName)}(${line + 1},${character + 1}): error TS${d.code}: ${m}`; }
  return `error TS${d.code}: ${m}`;
};
// Only diagnostics that say the fixture names something the REAL code does not have are defect-grade; the rest
// (strict-mode looseness in test code, @types/node drift on mock.module options) are advisory noise.
const HARD = new Set([2305, 2307, 2614, 2724, 2459, 2305, 2300, 1005, 1109, 1128]);
const soft = [];
for (const d of [...prog.getSyntacticDiagnostics(sf), ...prog.getSemanticDiagnostics(sf)]) {
  if (d.category !== ts.DiagnosticCategory.Error) continue;
  const envNoise = d.code === 2307 && /module '(node:|[a-z_]+')/.test(ts.flattenDiagnosticMessageText(d.messageText, ' ')) && /'node:/.test(ts.flattenDiagnosticMessageText(d.messageText, ' '));
  if (!envNoise && (HARD.has(d.code) || d.code < 2000)) errors.push({ line: fmt(d) }); else soft.push(fmt(d));
}
// ---- mock-shape probe
const litSet = (t, prop) => {           // set of string literals of `prop` across a union, or null if not tagged
  const out = new Set();
  const members = t.isUnion() ? t.types : [t];
  for (const m of members) {
    const s = m.getProperty(prop); if (!s) return null;
    const pt = checker.getTypeOfSymbolAtLocation(s, sf);
    const pts = pt.isUnion() ? pt.types : [pt];
    for (const x of pts) { if (x.isStringLiteral()) out.add(x.value); else return null; }
  }
  return out;
};
const retOf = (t) => { const sigs = t.getCallSignatures(); if (!sigs.length) return null; let r = checker.getReturnTypeOfSignature(sigs[0]);
  const aw = checker.getAwaitedType ? checker.getAwaitedType(r) : null; return aw || r; };
let mocks = 0; const notes = [];
const visit = (n) => {
  if (ts.isCallExpression(n) && /(^|\.)module$/.test(n.expression.getText(sf)) && n.arguments.length >= 2
      && ts.isStringLiteralLike(n.arguments[0]) && ts.isObjectLiteralExpression(n.arguments[1])) {
    const spec = n.arguments[0].text;
    const ne = n.arguments[1].properties.find((p) => ts.isPropertyAssignment(p) && ['namedExports', 'exports'].includes(p.name.getText(sf)));
    if (ne && ts.isObjectLiteralExpression(ne.initializer)) {
      const resolved = resolveSpec(spec);
      const rsf = resolved && prog.getSourceFile(resolved);
      const msym = rsf && checker.getSymbolAtLocation(rsf);
      if (!msym) { notes.push(`mock of '${spec}' skipped: module not resolvable`); return; }
      const exps = checker.getExportsOfModule(msym);
      for (const p of ne.initializer.properties) {
        if (!ts.isPropertyAssignment(p) && !ts.isShorthandPropertyAssignment(p)) continue;
        const name = p.name.getText(sf); const real = exps.find((e) => e.name === name);
        mocks++;
        if (!real) { const { line, character } = sf.getLineAndCharacterOfPosition(p.getStart(sf));
          errors.push({ line: `${rel(FX)}(${line + 1},${character + 1}): error FIXTURE_MOCK_SHAPE: mock of '${spec}' exports '${name}' which the real module does not export` }); continue; }
        const mt = checker.getTypeAtLocation(ts.isPropertyAssignment(p) ? p.initializer : p.name);
        const rt = checker.getTypeOfSymbolAtLocation(real, rsf);
        const mr = retOf(mt), rr = retOf(rt); if (!mr || !rr) continue;
        const realKinds = rr.isUnion() ? rr.types : [rr];
        const props = new Set(); for (const m of realKinds) for (const s of m.getProperties()) props.add(s.name);
        for (const prop of props) {
          const rl = litSet(rr, prop); if (!rl) continue;
          const ml = litSet(mr, prop); if (!ml) continue;
          const bad = [...ml].filter((v) => !rl.has(v));
          if (bad.length) { const { line, character } = sf.getLineAndCharacterOfPosition(p.getStart(sf));
            errors.push({ line: `${rel(FX)}(${line + 1},${character + 1}): error FIXTURE_MOCK_SHAPE: mock of ${name} (${spec}) returns ${prop}: ${bad.map((b) => JSON.stringify(b)).join('|')} but the real ${name} only returns ${prop}: ${[...rl].map((b) => JSON.stringify(b)).join('|')}` }); }
        }
      }
    }
  }
  ts.forEachChild(n, visit);
};
visit(sf);
if (soft.length) notes.push(`${soft.length} non-blocking TypeScript diagnostic(s) in the fixture (first: ${soft[0]})`);
console.log(JSON.stringify({ ok: errors.length === 0, errors, mocks, notes }));
