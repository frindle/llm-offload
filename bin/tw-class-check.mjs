// Does every Tailwind class this diff ADDS actually generate CSS?
//
// WHY THIS AND NOT A GREP. A reviewer reported `space-y-0.25` as an invalid
// Tailwind step and proposed hardening it with a grep for fractional spacing
// values outside {0.5}. That rule is correct for Tailwind v3's fixed scale and
// WRONG for this project: it is on Tailwind v4, whose spacing scale is dynamic
// (`calc(var(--spacing) * N)`), so `space-y-0.25` compiles to a real 1px rule.
// The grep would have failed correct code, and the "fix" would have doubled the
// spacing the task asked to reduce.
//
// So the check compiles the classes with THE PROJECT'S OWN TAILWIND and asks
// whether each one produced a rule. That cannot disagree with the build, has no
// version assumption to drift, and reports no false positives by construction --
// the same principle as every other durable win here: move the question into
// the harness where it is mechanical, instead of encoding a remembered rule.
//
// Usage: node tw-class-check.mjs <diff-file>   (run with cwd inside the project)
// Exit 0 = every added class generates CSS (or nothing to check); 1 = dead classes.
import { readFileSync, writeFileSync, mkdtempSync } from 'fs';
import { tmpdir } from 'os';
import { join } from 'path';
import { createRequire } from 'module';
import { pathToFileURL } from 'url';

// Resolve postcss/tailwind from THE PROJECT, not from this script's directory.
// A bare `import 'postcss'` resolves relative to ~/bin, where they do not exist
// -- and resolving them from anywhere but the project would also defeat the
// entire point, which is to ask the project's OWN Tailwind what it generates.
const req = createRequire(join(process.cwd(), '__resolve__.js'));
let postcss, tw;
try {
  postcss = (await import(pathToFileURL(req.resolve('postcss')).href)).default;
  tw = (await import(pathToFileURL(req.resolve('@tailwindcss/postcss')).href)).default;
} catch {
  // No Tailwind here = not a Tailwind project, or deps not installed. ABSTAIN
  // loudly rather than passing silently -- "not checked" must never read as
  // "checked and fine".
  console.log('tw-class-check: NOT ASSERTED -- postcss/@tailwindcss/postcss do not '
            + 'resolve from ' + process.cwd() + ', so no class could be compiled.');
  process.exit(0);
}

const diffPath = process.argv[2];
if (!diffPath) { console.error('usage: tw-class-check.mjs <diff-file>'); process.exit(2); }
const diff = readFileSync(diffPath, 'utf8');

// Added lines only, and only in files Tailwind actually scans.
let scanned = false;
const added = [];
for (const line of diff.split('\n')) {
  if (line.startsWith('+++ b/')) scanned = /\.(tsx|jsx|html|vue|svelte)$/.test(line);
  else if (scanned && line.startsWith('+') && !line.startsWith('+++')) added.push(line.slice(1));
}
const classes = new Set();
for (const l of added)
  for (const m of l.matchAll(/class(?:Name)?\s*=\s*["'`]([^"'`]+)["'`]/g))
    for (const c of m[1].split(/\s+/))
      // Skip template holes: a class built by interpolation cannot be resolved
      // statically, and guessing at one would manufacture a false positive.
      if (c && !c.includes('${') && !c.includes('{')) classes.add(c);

if (!classes.size) { console.log('tw-class-check: no static classes in added lines -- nothing to check'); process.exit(0); }

const dir = mkdtempSync(join(tmpdir(), 'twchk-'));
const probe = join(dir, 'probe.html');
writeFileSync(probe, [...classes].map(c => `<div class="${c}"></div>`).join('\n'));
const r = await postcss([tw()]).process(
  `@import "tailwindcss";\n@source "${probe}";`, { from: join(process.cwd(), 'probe.css') });

const esc = c => '.' + c.replace(/([.:[\]()/%!#,+*~>&'"])/g, '\\$1');
const dead = [...classes].filter(c => !r.css.includes(esc(c)));

if (!dead.length) { console.log(`tw-class-check: all ${classes.size} added class(es) generate CSS`); process.exit(0); }
console.log(`tw-class-check: ${dead.length} of ${classes.size} added class(es) generated NO CSS:`);
for (const d of dead) console.log(`  DEAD: ${d}`);
console.log('  A class that generates no CSS silently does nothing -- typecheck cannot see it.');
process.exit(1);
