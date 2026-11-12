# Ollama Model Guide — dispatch settings, failure modes, viability

**Started 2026-08-22.** Practical per-model reference for agentic dispatch (tool-calling
code work), built from the v1→v5 bake-off. Entries marked **PENDING** are still running.

This exists because the information is not assembled anywhere. The individual failure
modes *are* documented — [ollama#8517](https://github.com/ollama/ollama/issues/8517)
(distill template gap), [vllm#28219](https://github.com/vllm-project/vllm/issues/28219)
(tool calls in content, not `tool_calls`),
[hermes-agent#13042](https://github.com/NousResearch/hermes-agent/issues/13042)
(malformed JSON from unescaped special characters) — but scattered across issue
trackers, per-model, with no single place tying settings to observed behaviour.

> **Read this first**: standard coding benchmarks (HumanEval etc.) are single-turn and
> test none of this. A model can score well there and be unusable in an agent loop
> because its tool-call JSON is malformed. Those are unrelated properties.

---

## How to read a result

A verify command proves *the build still compiles*, never *the feature was built*.
Four false-pass shapes are confirmed — each defeated the previous rule:

| Shape | Looks like | Actually |
|---|---|---|
| `exit=0`, `files=0` | pass | verify ran on an untouched tree |
| `exit=0`, files **outside** build graph | pass | fabricated app beside the real one |
| `exit=0`, files **inside** build graph | pass | unused dirs + package-manifest churn |
| files **wired in**, feature **already existed** | best result in the matrix | duplicate build, working code destroyed to fit it |

**A result is real only if changed paths are *wired into* the build graph** — not merely
located in it. Exclude `package.json`/lockfile churn from `run_bash npm ...`; that is a
side effect of shell use, not authored work. Also: a model **absent** from a results
table is missing data, not a zero.

> ### ⚠️ `files=0` can be the correct answer
>
> **The resell-tracker photo-upload task asked for a feature the repo already had** —
> verified at the pinned baseline `271aa9e`: `app/api/orders/[id]/attachments/route.ts`
> (upload + order association + disk storage), `components/OrderAttachments.tsx`
> (multi-file drag-and-drop UI), the `OrderAttachment` Prisma model, shipped in a June
> migration. The image whitelist already included `image/heic`. The real delta was touch
> ergonomics plus a camera-capture input.
>
> `qwen3.8:27b-q8_0` traced all of it and reported it accurately. It scored `files=0` —
> **indistinguishable in the CSV from a model that never made a tool call.** Meanwhile
> `deepseek-r1:32b` scored the matrix's highest file count by building a duplicate and
> deleting two working files to wire it in.
>
> **No file-count metric can separate "did nothing" from "correctly determined the work
> was already done."** Only the transcript can. Treat every photo-upload result in this
> guide as a *codebase-comprehension* measurement, not a feature-build measurement; the
> clamshell leg is unaffected and remains the real build test.

## Verdicts

### `deepseek-r1:32b` — writes the most code; destroyed working files to do it ⚠️

- **Settings**: ctx 131072 (Mac Studio), temp 0.6 / top-p 0.95, `--manual-tools`, + "first response must contain a tool call" nudge.
- **Verdict (re-scored 16:55)**: the most *productive* model tested — it explores, writes correctly-located files, edits real entry points, and installs its own dependencies. But its headline photo-upload result **does not survive inspection**, and the way it failed is the most dangerous failure recorded in this bake-off. **Never dispatch unattended. Never dispatch against a tree you care about.**

> **⚠️ This entry was downgraded.** It previously read *"the first genuine result — clears the wired-into-the-build-graph bar, fails only on framework conventions."* That was written before anyone checked whether the feature already existed. It did. See the re-score below.

**photo-upload (v5)** — `exit=1, 523s, files=6, iters=19`
```
 M app/layout.tsx          <- OVERWROTE the real layout
 M app/page.tsx            <- 266 lines -> 10
 M package.json / -lock    from its own npm installs
?? app/api/upload/route.ts
?? app/components/PhotoUpload.tsx
```

Three defects, in descending order of seriousness. **The framework-convention error is the least of them.**

**1. It destroyed unrelated working code.** The two `M` entries were previously credited as *"integrated the component into the real layout and the real page."* It integrated by **deletion**:
- `app/layout.tsx` — root layout rewritten down to a bare `<html><body>{children}</body></html>`, **dropping `NavBar`, the Geist font setup, `getSessionUser()` auth, `FirefoxInputGuard`, the version banner, and every styling class.**
- `app/page.tsx` — **266 lines → 10.** The entire dashboard (analytics, date ranges, overdue logic, return status) replaced with a render of `<PhotoUpload/>`.

It also chose the wrong surface entirely: a per-order upload widget belongs on `app/orders/[id]/page.tsx`, not the global layout and the homepage.

**2. The core requirement is a comment.** `app/api/upload/route.ts`, verbatim:
```ts
// Here you would associate the file with the order in your database
// For example:
// associateFileWithOrder(orderId, uniqueName);
```
**Associating the upload with an order was the task.** It is a TODO. It invented its own storage at `public/uploads` — web-served, unlike the existing private `/data/files` — and ignored the `OrderAttachment` model entirely.

**3. It never found the existing feature.** The repo already had the whole thing at baseline (see the ⚠️ box in *How to read a result*). It built a parallel duplicate. Contrast `qwen3.8:27b-q8_0`, which found it in 39 tool calls and correctly reported the real delta.

**4. Pages Router idiom in an App Router file** — the previously-recorded error, still real, still the build-breaker:
```ts
import { NextApiRequest, NextApiResponse } from 'next';
export default function uploadHandler(req: NextApiRequest, res: NextApiResponse) { ... }
```
App Router `route.ts` requires *named* exports (`export async function POST(req: NextRequest)`). Build dies with "Ecmascript file had an error."

**Plus** an unrequested `@clerk/nextjs` + `clerk-js` install into a project that has its own `lib/auth`.

> **The lesson this run actually teaches**: `exit=1` saved us. **Had `verify` passed, this would have been a merge that silently deleted the dashboard and the site chrome.** File count measured productivity, and productivity was the hazard.

**What still stands**: the **dependency nudge is confirmed working.** v3 failed solely on `Module not found: 'uuid'`; in v5 the model ran `npm install uuid` and `react-dropzone` unprompted — 8 `run_bash` calls, mostly installs. That v4/v5 `SYSTEM_PROMPT` addition demonstrably fixed a real failure class.

**clamshell (v5)** — `exit=1, 401s, files=2, iters=8` · **unaffected by the task defect — this leg is valid**
```
 M Package.swift
?? Sources/Clamshell/Auth/ConfirmationBridge.swift
```
It wrote the module *and* added a `ConfirmationBridge` target to `Package.swift`, but SwiftPM requires a target's sources at `Sources/<TargetName>/`. Declaring a target named `ConfirmationBridge` while placing the file under `Sources/Clamshell/Auth/` is a mismatch:
> `error: Source files for target ConfirmationBridge should be located under 'Sources/ConfirmationBridge'`

The correct move was to add no target at all and let the file join the existing `Clamshell` target — which `qwen2.5-coder:7b`, a model a quarter its size, got right.

**Also stopped early.** Final output was prose — *"The next step is to implement the self-test…"* — so it never wrote the required self-test (valid / replay / expired). The harness accepted it as final because mutations *had* occurred, so the "you haven't made changes yet" nudge does not fire. A convergence-detection limitation, not a harness bug: the model chose to stop.

**Harness signals clean on both tasks**: 0 unknown-tool calls, 0 loop-breaks, 0 hard-blocks, 0 nudges. Fallback-parse recoveries (18 / 7) are normal for `--manual-tools`. **No harness bug in either run.**

> **Do not trust any pre-v5 numbers for this model.** It lost two separate results purely to harness parser bugs — malformed tool names (v2), then triple-quoted JSON (v3, which discarded a correct Swift file and scored it `files=0`).

###### photo-upload (v6) — `exit=0, 911s, files=0, iters=15` · false pass, and a real behavioural change

**v5 → v6, same model, same task, same tree: 6 files → 0 files.** The only difference is the v6 `SYSTEM_PROMPT` telling models to read `AGENTS.md`/`CLAUDE.md` first. **It read them** — `AGENTS.md` and `README.md` were its second and third calls — and then produced nothing at all.

Its 13 calls, in order:
```
list_files .  →  read_file AGENTS.md  →  read_file README.md
→  list_files app/components   ×3   →  ERROR: path not found  (×3)
→  list_files app  →  read_file app/page.tsx  →  list_files .
→  list_files app/components   →  REFUSED (loop-break)
→  list_files .  ×2  →  list_files app/components  →  REFUSED
→  prose  →  nudge  →  prose  →  stop
```

**It fixated on `app/components`, which does not exist at baseline** — and which is exactly where it *created* its duplicate in v5. Same wrong mental model of the project layout both times: in v5 it wrote to the wrong path, in v6 it searched the wrong path. The real `components/` is top level and appeared in the output of its own first `list_files(".")`. It never found `OrderAttachments` — the string does not appear anywhere in its transcript.

**Loop detection worked**: 3 identical failures triggered the break, then two hard `REFUSED` responses. The refusals stopped the thrashing and the model had nothing else to try.

**The safety reading is genuinely better; the capability reading is not.** v5 built a duplicate and destroyed `app/layout.tsx` and `app/page.tsx` to wire it in. v6 did nothing at all. **Given the choice between this model's two observed behaviours, `files=0` is the one you want** — but neither is a working feature, and `exit=0` here is the oldest false pass in the guide: verify ran on an untouched tree.

### `deepseek-r1:14b` — do not use for coding ❌

- **Settings tested**: ctx 131072 (Mac Studio), temp 0.6 / top-p 0.95, `--manual-tools`, + nudge. Same as 32b.
- **Verdict**: fails in kind, not just degree. Hallucinates tool names, writes before reading, breaks build manifests, and stops early on both tasks. **The quality cliff is between 32b and 14b — not between 14b and 7b as previously assumed.**

**photo-upload** — `exit=1, 156s, files=1, iters=5`
```
?? app/upload/page.tsx     (rewritten 3×, the only file it touched)
```
**It wrote before reading anything.** Call sequence: one `list_files`, then straight to `write_file`. It never read a single existing file — no `package.json`, no schema, no existing page — so it had no knowledge of the project's Prisma models, its orders data, or its conventions. It produced a standalone upload page with **no API route, no storage, and no association to an order**, which was the actual requirement. Build fails.

**Converged early with prose.** Final output: *"To implement the photo upload feature, I'll start by creating a new upload page component..."* — a statement of intent, delivered *after* it had already stopped calling tools. Same premature-stop pattern 32b showed on clamshell, but here on the task it had barely started.

**clamshell** — `exit=1, 167s, files=3, iters=5`
```
 M Package.swift            <- damaged, see below
?? Sources/Clamshell/Auth/ConfirmationBridge.swift
?? Tests/ConfirmationBridgeTests.swift
```
Superficially the most complete file set of any model — module *and* tests. But:

**It hallucinated a tool called `mkdir`**, twice:
```
tool mkdir({"command": "mkdir -p Sources/Clamshell/Auth"}) -> ERROR: unknown tool mkdir
```
It knew the correct shell command and invented a tool name to run it instead of using `run_bash`. Structurally identical to `deepseek-r1:7b` inventing `photo_input`. **Tool-name hallucination is present at 14b**, with the explicit name constraint active in the prompt.

**It broke the package manifest.** It rewrote `swift-tools-version: 5.9` → `swift-tools-version: 6.3`, a version the installed toolchain does not accept. Every subsequent `swift build` / `swift test` then failed on the manifest itself rather than on the code:
> `error: package 'package.swift' is using Swift tools version ...`

It then spent its remaining turns trying to repair the damage — `swift package add https://github.com/apple/swift-crypto.git --from .` (exit 64, malformed), `swift package add Crypto --from .` (also failed) — and ended with *"To fix the Swift tools version error in the Clamshell project, follow these steps:"*, narrating a fix it never applied.

**Unnecessary dependency, same instinct as 32b**: it tried to add `swift-crypto`. On macOS, P-256 ECDSA is available through the built-in `CryptoKit` — no package needed. See the dependency-creep gotcha below.

**Where the cliff actually is.** Against 32b on identical settings: 32b explored then integrated into real entry points and failed only on framework conventions; 14b wrote blind, invented a tool name, and corrupted a build manifest. 7b then adds invented top-level directories. So degradation is **not** smooth from 32b down — 14b already exhibits the 7b-class pathologies (tool-name hallucination, no grounding in the real codebase), just with better-looking file placement. Treat 32b as the only member of this family worth dispatching.
### `MFDoom/deepseek-r1-tool-calling:14b` — the fine-tune works, the model still doesn't ❌

- **Settings**: ctx 131072, temp 0.6 / top-p 0.95, `--manual-tools` (still required — see below), + nudge.
- **First real run ever.** In v1 it died in 0s/1s on the namespaced-manifest-path bug and never loaded, producing zero data.
- **Verdict**: the fine-tune measurably fixes the specific pathology it targets, and the model is still unusable. Do not dispatch.

**The fine-tune's headline claim, tested directly — and it holds.** Its base (`deepseek-r1:14b`) hallucinated a tool named `mkdir` twice, calling `mkdir({"command": "mkdir -p Sources/Clamshell/Auth"})` with the name constraint active. The fine-tune, **on the identical operation**, called:
```
run_bash({"command": "mkdir -p Sources/Clamshell/Auth"})
```
Correct tool, shell command as argument. **Zero hallucinated tool names across both tasks**, versus two in the base. That is a real, quantified win for the fine-tune — same intent, same operation, pathology fixed.

**It also explores more than its base.** clamshell: 6 `list_files` + 1 `read_file` before writing. The base went one `list_files` straight to `write_file` and never read a file. Genuine improvement.

**But it does NOT deliver native tool-calling — 11 of 11 calls came through fallback-parse.** Every single tool call in the clamshell run required our own text parser; Ollama's native `tool_calls` never fired once. The template gap the vault predicted is **confirmed present in the fine-tune**, despite the model page implying otherwise. `--manual-tools` remains mandatory. If you adopt this model expecting native tool-calling to work, it will not.

**photo-upload** — `exit=0, 161s, files=0, iters=3` · **false pass**
One `list_files`, zero writes. The "you haven't made changes yet" nudge fired once; it stopped anyway, ending with *"To create the photo-upload feature, I'll start by adding a new page..."* — intent, not work. `VERIFY PASSED` only because the tree was untouched.

**clamshell** — `exit=1, 258s, files=1, iters=12`
Twelve iterations, 258 seconds, and the sole artifact is:
```
Sources/Clamshell/Auth/Package.swift     <- a package manifest, inside a sources directory
```
**It never wrote `ConfirmationBridge.swift` at all.** It wrote a `Package.swift` *under `Sources/`*, where SwiftPM compiles everything as Swift source — so the build died trying to compile a manifest as code:
> `Sources/Clamshell/Auth/Package.swift:14:34: error: expected ',' separator`

Doubly wrong: wrong file, wrong location, and syntactically broken. Then it stopped with *"To create the ConfirmationBridge module, I will execute the following steps:"* — narrating the work instead of doing it, same as its base and 32b.

**Dependency-surface data point: inconclusive, not clean.** Its only two `run_bash` calls were `mkdir -p`. It never reached the stage of installing anything, so this is not evidence that the creep behaviour is absent — it did too little to test it.

> **Contrast worth remembering**: this model scored **18/18** in the earlier (v1-era) exploration-style bake-off. On real feature work it produced nothing usable. **High scores on simpler dispatch tasks do not predict feature-implementation ability** — a caution that applies to any model ranking built on lighter tasks.

**What this says about fine-tunes generally**: targeted fine-tuning fixed the targeted defect and moved nothing else. Tool-name hallucination went to zero; early convergence, weak grounding, and inability to produce working code were all inherited intact. Judge a tool-calling fine-tune on whether it makes the model *useful*, not on whether it makes the tool calls well-formed.
### `qwen2.5-coder:14b` — thrashes to the iteration ceiling on both tasks ❌

- **Settings**: ctx 32768, temp 0.2, top-p 0.95, top-k 20, **no** `--manual-tools` (native `tools` *is* sent — see below).
- **Verdict**: hit the 30-iteration ceiling on both tasks without converging. Gets some structure right that larger models get wrong, then cannot write code that compiles. Do not dispatch.

**Native tool-calling does not work for this model either — 30 of 30 calls came via fallback-parse, on both tasks.** This is the first model run *without* `--manual-tools`; the worker genuinely sends `payload["tools"]`. Ollama's native `tool_calls` returned nothing, every time. The model emits its calls as fenced ```` ```json ```` blocks in the content, which our text parser recovers and Ollama's does not. **The vault's inference that a Hermes-style template in `tokenizer_config.json` means native tool-calling works through Ollama does not hold in practice.** See the cross-model gotcha below.

**photo-upload** — `exit=1, 204s, files=1, iters=31 (ceiling)`
```
?? app/orders/[id]/upload.tsx      (plus 7 edits to app/orders/[id]/page.tsx)
```
18 `list_files` out of 30 calls — heavy thrashing. **The v5 hard block fired and worked**: 2 loop-breaks, **4 refusals**. Without it this run would have burned far more of its budget on dead paths. Build failure:
> `Type error: Cannot find module 'next-auth/react'`

It imported an auth library that is not a dependency and **never called `run_bash` once**, so it never installed it. Note this is the failure mode the dependency-install prompt line was added to fix — it does not help a model that never reaches for the shell.

**clamshell** — `exit=1, 108s, files=2, iters=31 (ceiling)`
```
 M Package.swift
?? Sources/ConfirmationBridge/ConfirmationBridge.swift, main.swift
```
**It got the SwiftPM layout right where `deepseek-r1:32b` got it wrong** — created `Sources/ConfirmationBridge/` matching the target name it declared, which is exactly the convention 32b violated. Genuine credit.

Then it could not write compiling Swift. 13 `edit_file` calls against the same file, interleaved with 6 `swift build` runs, chasing errors it never resolved (`cannot infer contextual base`, a malformed `static func main()`). Never converged.

**Loop detection has a blind spot this exposes.** No loop-break fired on clamshell despite 13 edits to one file, because loop detection counts *identical failing* calls — and each `edit_file` **succeeded**. The model looped on successful edits that never fixed the underlying build error. Detection catches "same call, same failure"; it does not catch "different edits, same unresolved error." Recorded as a limitation, not a bug.

**Third instance of the invented-auth pattern**: `deepseek-r1:32b` installed `@clerk/nextjs`, this model imported `next-auth/react`. Two different models independently decided a photo-upload feature needed an auth stack that the project does not use. See the dependency gotcha.
### `devstral:24b` — VERDICT WITHDRAWN, run was misconfigured 🚧

> **Status: quarantined, re-run in flight.** The v6 numbers below were produced under a
> configuration we had already recorded as broken for this model. They are kept as
> evidence about the *harness*, not as a verdict about the model. A corrected run under
> `llama-server` is queued (backend label `macstudio-llamaserver`) and will replace this
> section.

**Proven-good config** (on disk, `devstral-24b-*-FIXED.log` 12:25 2026-08-22; verify PASSED 3× on this exact task):

- `llama-server` on :8091 with `--jinja --chat-template-file ~/bin/devstral-tool-template.jinja`
- worker: `--api openai --num-ctx 65536 --temperature 0.2`
- **Do not add `--system-prompt-file`** — recorded as making results worse.

**Config that produced the quarantined rows**: native Ollama, ctx 65536, temp 0.2, no `--manual-tools`.

###### BUG 10 — devstral on native Ollama produces near-zero tool calls

The switch to Ollama-native rested on `capabilities: ['completion','tools']` plus a single-turn probe that returned a correct native `tool_call`. That validated **one imperative request**, not multi-turn agentic behaviour under the worker's system prompt. The worker's own `--system-prompt-file` help already said so verbatim — *"Needed for devstral, which produces zero tool calls on this Ollama build without its own OpenHands-scaffold system prompt."* A proven config was traded for an unproven one on the strength of a probe that did not test the thing that broke.

Both rows are quarantined in the CSV as `devstral:24b-INVALID-OLLAMA-NATIVE` and in `*.INVALID-OLLAMA-NATIVE.log`.

**photo-upload (v6, INVALID)** — `exit=0, 35s, files=0, iters=2` · false pass
Zero tool calls, at all. Iteration 1 prose → nudge → iteration 2 more prose (*"I'll start by listing the files in the working directory…"*, announcing a `list_files` it never made) → stop. `VERIFY PASSED` on an untouched tree.

**clamshell (v6, INVALID)** — `exit=0, 52s, files=0, iters=7` · false pass
Exploration was the best-shaped of any model in the matrix:
```
list_files .  →  list_files Sources  →  list_files Sources/Clamshell
              →  list_files Sources/Clamshell/Streaming  →  read_file Sources/Clamshell/main.swift
```
A sensible descent into the real entry point — then it stopped at iteration 7 with *"Let's proceed with creating the necessary files…"* and wrote nothing.

**The one durable finding from these rows** (independent of the misconfiguration, because it is a mechanism observation rather than a performance one): **devstral's native `tool_calls` fired — 4 of its 5 clamshell calls.** Every other model tested scores 0 native. That falsifies the earlier "native tool-calling has never worked for any model" claim and survives the quarantine. See the corrected rule in Gotchas.

**What is NOT established**: anything about devstral's completion rate, exploration quality, or viability. Do not cite "best mechanics, zero output" as a devstral property — under this config, zero output was the *config's* result. Wait for the `llama-server` re-run.
### `qwen2.5-coder:7b` — not viable; v6 changed its behaviour, not its output ❌

- **Settings**: ctx 32768, temp 0.2, top-p 0.95, top-k 20, native tool-calling attempted (**0 native, 30 via fallback** — see the Gotchas correction).
- **Verdict (v6)**: does not complete either task. Its v6 behaviour is clearly different from v5, but **a single run cannot establish whether the v6 context fix helped or hurt** — see the caveat below, which is the honest answer.

**photo-upload (v6)** — `exit=2, 27s, files=0, iters=31 (ceiling)`
**26 of its 28 `read_file` calls failed.** It read exactly two real files — `README.md` and `next.config.ts` — then issued 26 reads against paths that do not exist:
```
app/api/upload.ts   app/api/orders.ts   app/api/costco.ts   app/api/cards.ts
app/api/sync.ts     app/api/email.ts    app/api/users.ts    app/api/version.ts   …
```
**The route names are all real.** `upload`, `orders`, `costco`, `cardcenter`, `buyinggroup`, `sync-history`, `shipping-rules`, `portal-rates` — every one exists in the project as `app/api/<name>/route.ts`. It successfully discovered the correct route *names*, then applied the **Pages Router flat-file path shape** to all of them.

That is the same App-Router-vs-Pages-Router confusion that produced `deepseek-r1:32b`'s wrong API signature, expressed here as *path construction* rather than code idiom. It is also precisely what `AGENTS.md` warns about — *"file structure may differ from your training data"* — and **it read `README.md` but never `AGENTS.md`**, so the warning went unseen.

**Loop detection could not fire**: no path was repeated 3× (only `app/api/import.ts` appeared twice). 26 consecutive failures, each at a *different* wrong path. Detection keys on identical repeated calls; a systematically-wrong path *pattern* is invisible to it. Confirmed by direct count.

**clamshell (v6)** — `exit=1, 54s, files=1, iters=29`
```
?? Sources/Clamshell/ConfirmationBridge.swift   (13 write/edit calls against it)
```
**It got the SwiftPM placement right** — dropped the file into the existing `Clamshell` target and left `Package.swift` alone, which is the correct move and the one `deepseek-r1:32b` and `MFDoom` both got wrong. Then it invented a Swift type that does not exist:
> `error: cannot find 'ECPrivateKey' in scope`

(P-256 signing in CryptoKit is `P256.Signing.PrivateKey`.) Thirteen edits chasing compile errors, never converged — and no loop-break fired, because every `edit_file` **succeeded**. Same blind spot as `qwen2.5-coder:14b`.

##### v5 → v6: what is measured vs what is inferred

| | v5 | v6 |
|---|---|---|
| photo-upload | 15 iters, **2 files** | 31 iters, **0 files** |
| clamshell | 31 iters, 0 files | 29 iters, **1 file** |
| photo call mix | 18 `list_files`, 12 `read_file` | 2 `list_files`, **28 `read_file`** |
| loop-breaks / refusals | 2 / 4 | **0 / 0** |
| read a context file | never | **`README.md`** ✅ |

**Measured**: behaviour changed substantially. It stopped thrashing on directory listings and started reading files, and it read a context file for the first time — so the v6 prompt change demonstrably reached the model.

**NOT established**: whether that helped or hurt. This model has already demonstrated it swings between outcome classes on byte-identical inputs — 5 files on one v2 run, 0 on the next v3 run, same harness, same task. A one-run photo-upload delta of 2 files → 0 files is well inside that observed swing. **The honest answer is that a single v6 run cannot separate the context fix's effect from this model's own variance.** Establishing it would need ≥3 runs per arm.

What *can* be said without repeats: the model now reads, and reads the wrong paths — a failure that AGENTS.md directly addresses and that it did not open.

### `deepseek-r1:7b` — do not use for coding ❌
- **Settings tested**: ctx 32768 on Unraid (see Gotchas — 131072 does not fit a 3080), temp 0.6, `--manual-tools`, + nudge.
- **Verdict (v6, both tasks, on a harness with no remaining known defects)**: confirmed. This is now a well-evidenced negative, not a suspected one — and unlike four other 7B-class verdicts today, it did **not** dissolve under scrutiny.

**photo-upload (v6)** — `exit=1, 52s, files=1, iters=3` · genuine failure

**Schema parroting.** It emitted one call per tool, in the exact order the tools appear in the injected schema block, then repeated the identical spray on the next iteration:

```
list_files → list_files → read_file → write_file → edit_file → run_bash → web_search → web_fetch
```

That is the tool list, not a plan. The individual calls confirm it:

- `write_file({"path": "app/order/photos.tsx", "content": "function to handle photos upload"})` — **the placeholder description written as the file body.** 32 bytes.
- `run_bash` with an `echo` of malformed JSX that dies in `/bin/sh` on unbalanced quotes.
- `web_fetch("https://platforms.as网红彩票计划com/docs/photos-api")` — **a hallucinated URL with CJK characters spliced into the hostname.**

The build failure is that placeholder prose itself, parsed as TypeScript:

> `./app/order/photos.tsx:1:10  Type error: 'to', which lacks return-type annotation, implicitly has an 'any' return type.`
> `> 1 | function to handle photos upload`

The file sits inside `app/`, so `tsc` type-checks it, but nothing imports it and it is not a route. **Type-checked is not the same as wired in** — this does not clear the build-graph bar.

**clamshell (v6)** — `exit=0, 1264s, files=1, iters=3` · **false pass, shape #2**

The single artifact, in a **Swift** project:

```
keys/keys.ts        <- a TypeScript file, at an invented top-level directory
```

Its entire contents:

```
base64 encoded P-256 key pair

-----BEGIN ECDSA private key -----
```

A *description* of a key, not a key, in the wrong language, outside the build graph. **Zero Swift written.** `swift build` exits 0 because it never sees `keys/keys.ts` — the purest example of the `exit=0` + `files>0` false pass in the dataset. 1264s to produce it, across 3 iterations.

**Consistent across v3/v4/v5/v6**: invented top-level directories (`Orders/`, `Upload Storage/` — with a space — holding JSON blobs, in a Next.js app; now `keys/`), fabricated `app/styles/` + a nested lockfile, and a **hallucinated tool named `photo_input`** even with an explicit name constraint in the prompt.

**Never once** wrote a file the build actually compiles. Every `exit=0` was a false pass.

Tool-calling capability **does not scale down** from the 32b to this distill. Contrast `qwen2.5-coder:7b` at the same size, which does use tool results and got the SwiftPM placement right.

### `qwen3-coder-next:q4_K_M` — throughput-bound, needs a long timeout ⚠️
- **52.2GB resident** on Mac Studio (68.7GB unified), no spill — it *fits*, but runs ~7.5 min/iteration.
- At `TIMEOUT_S=1800` it can only ever complete **~4 iterations** regardless of task. Both prior runs died at exactly 1800s. Its historic "2/18 convergence" is very likely this ceiling, not a convergence defect.
- **Needs `TIMEOUT_S=3600`.** Iteration count is the diagnostic that separates a throughput ceiling from a convergence failure — record it.
- Also wasted 2 of its 4 affordable iterations web-searching the *local* codebase under the old system prompt (since fixed).

### `qwen2.5vl:7b` — vision, recommended ✅
- 2/2 clean passes on real screenshots: structured JSON extraction from a resell-tracker table (all rows, dates, order numbers, platforms exact; sensibly expanded truncated names rather than hallucinating) and free-form UI description (exact labels and dollar figures).
- **Recommended default for image/screenshot/receipt parsing.** Route to Unraid — local Ollama serializes concurrent large models rather than parallelizing, so it contends with coder dispatches on the Mac Studio.

### `deepseek-r1:70b` — untestable on this hardware ⛔

**Not a model verdict — a hardware verdict, and it is itself routing data.**

Dropped from the matrix because it cannot be *fairly* tested on the 64GB Mac Studio, not because it performed badly:

| | |
|---|---|
| Weights | ~42.5GB |
| KV cache @ 32768 ctx | ~10.2GB |
| Total | ~52.7GB |
| GPU working-set ceiling | ~48GB |
| **Max fully-resident context** | **~12k tokens** |

A `--manual-tools` transcript — system prompt + injected tool schemas + accumulated tool results — exceeds 12k tokens well before a task completes. So any run either spills to CPU (making duration meaningless, as measured on `deepseek-r1:7b` @131072: 20.6GB total vs 11.0GB resident) or truncates the conversation mid-task. Either way the number would measure the hardware, not the model.

**Routing implication**: 70B-class models are out of scope for this hardware for *agentic* work specifically, because agentic dispatch carries a long, growing transcript. The same box may still run a 70B fine for single-shot prompting where context stays small. **Model size alone does not determine fit — the workload's context growth does.**

Record as *untestable here*, not *unusable*. Testing it fairly needs a larger-memory host.
### `qwen3.8:27b-q8_0` — THE ONLY MODEL THAT COMPLETED A TASK ✅

- **Settings**: ctx 32768, temp 0.2 / top-p 0.95 / top-k 20, native Ollama (no `--manual-tools`).
- **Verdict**: **the only model in the bake-off that finished a task.** It completed the clamshell module end to end — correct SwiftPM structure, wired into `Package.swift` and the CLI, with a self-test that genuinely passes — and on the void photo-upload task it was the only model that discovered the feature already existed. **Dispatch it.** Slow (~15–18 min/task) and needs more than 30 iterations to converge, so review the diff; but expect real work in it.
- **Do not read its two CSV rows.** They say `exit=0, files=0` and `exit=2` — which look like a false pass and a failure. Both are artefacts: see below.

**photo-upload (v6)** — `exit=0, 1092s, files=0, iters=23` · **`files=0` is the correct answer here, and it is still not a pass**

39 tool calls of genuinely senior-level exploration — the best in the entire bake-off, by a wide margin:

```
AGENTS.md → CLAUDE.md → package.json → app/ → app/api/ → prisma/schema.prisma
→ app/api/orders/[id]/attachments/route.ts        (the existing upload API)
→ .../attachments/[attachmentId]/route.ts          (the existing delete/serve API)
→ components/OrderAttachments.tsx                  (the existing upload UI)
→ lib/deliveryPhoto.ts, lib/db.ts, lib/orderLock.ts, app/orders/[id]/page.tsx
→ git log --oneline -10
→ grep node_modules/next/dist/docs/ for the request body-size limit
→ Dockerfile, next.config.ts, prisma/migrations/
```

It is the **only** model to read both `AGENTS.md` and `CLAUDE.md`, the only one to open the on-disk Next.js docs the repo points at, and the only one to check `git log` for how the existing feature evolved. It then reasoned about deployment: self-hosted standalone, so Next's 4MB serverless body cap does not apply. Its conclusion, verbatim:

> **Already exists**: `OrderAttachment` model, `/data/files/<orderId>/` storage, complete API routes (`POST/GET/DELETE` + file serving), and the `OrderAttachments` component on the order detail page.
> **Gaps against the task**: The UI is desktop-oriented — the delete button is `hidden group-hover:flex` (unusable on touch), the touch targets are…

**Every claim in that summary is factually correct.** Verified independently against baseline `271aa9e`. It correctly identified the narrow real delta — touch ergonomics and camera capture — that the task's framing obscured.

> ### ⛔ RETRACTED — "correct diagnosis, no treatment" was a HARNESS failure, not a model failure
>
> This section previously read: *"it had 7 iterations and 700s of budget left and did not implement the delta it had just correctly scoped… Correct diagnosis, no treatment."* **That verdict is withdrawn. The model did not choose to stop — the harness stopped it.**
>
> **BUG 11.** After enumerating the remaining work, the model announced *"I'll rewrite the component for mobile/iOS and harden the upload route."* Its next turn came back with `content: ""` and the real output in `msg["thinking"]` — *"Let's actually make the changes…"*. The worker only ever read `content` and `tool_calls`, so an empty-content turn was indistinguishable from "nothing more to say." `run_task` declared convergence at iteration 23/30 **while the model was mid-plan**, and the one-nudge budget had already been spent.
>
> Found by adversarial audit, confirmed in the raw transcript (`~/bin/ollama-worker-logs/20260822T234608Z.json`, last message). Fixed in `ollama-worker-v7.py`: a turn with empty content and non-empty thinking now gets a bounded corrective retry instead of ending the run.
>
> **What is still true**: the exploration was real and the diagnosis was correct — 39 tool calls, both context files read, the existing implementation traced, the remaining delta named accurately. **What is no longer supported**: any claim that it declined to act. It was never given the turn.
>
> **Its v6.1 re-run on the patched worker is what will answer this**, and until that lands this model's photo-upload behaviour is UNKNOWN, not negative.
>
> **Seventh instance of the standing lesson, and the first where the wrong verdict was already written into this guide.** A model looked like it stopped short; the instrument had cut the wire.


> **Scoring note**: in `results-v6.csv` this row is `exit=0, files=0` — byte-identical to `devstral:24b`, which made zero tool calls and narrated. The CSV cannot tell the best comprehension result in the matrix apart from the worst non-result. Only the transcript can.

**The v6 context fix reaches this model and only this model.** `AGENTS.md` + `CLAUDE.md` both read at 27B; `qwen2.5-coder:7b` read `README.md` only; `deepseek-r1:7b` and `devstral` read none. **This is a capacity result, not a harness gap** — do not credit small-model v6-vs-v5 deltas to the context fix, and note that a v7 injecting `AGENTS.md` contents directly was considered and rejected, because it would not rescue models failing for capacity reasons.

#### clamshell (v6) — `exit=2, 921s, files=3, iters=31` · **GENUINE COMPLETE PASS** ✅

**The CSV row for this run reads `exit=2`. It is the best result in the bake-off.** `exit=2` is the worker's *non-convergence* code — it hit the 30-iteration ceiling — and the log records `VERIFY PASSED (exit 0)` on the same run. **Exit code conflates "verify failed" with "ran out of iterations."** A fifth way the CSV misrepresents a result.

```
 M Package.swift                                    target + dependency added
 M Sources/Clamshell/main.swift                     CLI subcommand + import
?? Sources/ConfirmationBridge/ConfirmationBridge.swift
?? Sources/ConfirmationBridge/ConfirmationSelfTest.swift
```

**Verified by running it** — not inferred from the build:

```
$ .build/debug/Clamshell confirmation-selftest
PASS: valid signature verifies
PASS: replayed response rejected
PASS: signature from the wrong key rejected
waiting 3s for the real expiry window to elapse…
PASS: expired challenge rejected
PASS: ConfirmationBridge selftest
exit: 0                                    (4.5s wall clock)
```

All four required properties, **plus** a wrong-key rejection nobody asked for. The 4.5s runtime confirms it **actually waited out a real expiry window** rather than faking the clock, which the task explicitly demanded and which is trivially cheatable.

**It got every structural thing right that larger models got wrong:**

| | `qwen3.8:27b-q8_0` | others |
|---|---|---|
| SwiftPM layout | `Sources/ConfirmationBridge/` matching the target name ✅ | `deepseek-r1:32b` declared the target, put sources in `Sources/Clamshell/Auth/` ❌ |
| `Package.swift` | added target **and** the `Clamshell` dependency ✅ | `MFDoom` wrote a `Package.swift` *inside* `Sources/` ❌ |
| Runnable self-test | `confirmation-selftest` CLI subcommand ✅ | `deepseek-r1:32b` narrated it and never wrote it ❌ |

**It found the project's own convention rather than inventing one.** Before writing anything it ran:
```
grep -rn "StreamSelfTest" Sources/Clamshell --include=*.swift -l
grep -rn "WindowAtCursorSelfTest" Sources/Clamshell --include=*.swift -l
```
…read `StreamSelfTest.swift`, and modelled its subcommand on it. That is the behaviour the v6 prompt change asked for, and the only model that performed it.

**When it hit an API it did not know, it looked it up in the SDK.** Stuck between `verifySignature`, `P256.Signature`, and `P256.Signing.ECDSASignature`, it did not guess repeatedly — it read the interface file:
```
SDK=$(xcrun --show-sdk-path); grep -n "Signature" \
  "$SDK/.../CryptoKit.swiftmodule/arm64e-apple-macos.swiftinterface" | head -30
→ public func isValidSignature<S, D>(_ signature: S, for data: D) -> Swift.Bool …
```
and applied the real signature. **Compare `qwen2.5-coder:7b`, which invented `ECPrivateKey` and burned 13 edits guessing.** Ground-truth lookup instead of guess-and-retry is the single behaviour that separates this model from every other one tested.

**It also ran its own self-test and fixed a real bug from the output.** First run printed `FAIL: forged signature rejected for the wrong reason: replayed` — its test consumed the nonce before the forged-signature case, so the wrong error surfaced. It diagnosed that and restructured the test to use a fresh challenge.

**The code is genuinely good**, not merely compiling: canonical signing bytes bind all four challenge fields with `0x00` domain separators (so a signature cannot be transplanted to another action or window); `NSLock`-guarded used-nonce set with 24h pruning to keep it bounded; expiry checked *before* replay bookkeeping, with the reasoning written in a comment.

**One real defect, worth recording.** The doc comment says the timestamps are encoded big-endian:
> `/// nonce || 0x00 || action (UTF-8) || 0x00 || issuedAt (Int64 BE) || 0x00 || expiresAt (Int64 BE)`

but `withUnsafeBytes(of: Int64(...))` emits **host** byte order — little-endian on arm64. Harmless here because both sides run the same code, but it would silently break the moment a big-endian or cross-platform client signs a challenge. **A comment that disagrees with its code is exactly the defect that survives review**, and this is why "it compiles and its tests pass" is still not the end of scoring.

**Why it did not converge**: it spent iterations 12–30 in a compile-fix loop on CryptoKit API names, then on its own test logic. It finished the work and ran out of budget on polish — the opposite of every other model, which ran out of budget without finishing.

##### Verdict

**The only model tested that completed a real feature end to end.** On the valid greenfield task it produced correctly-structured, wired-in, tested, working code, found the project's conventions by itself, and resolved an unknown API from the SDK rather than guessing. On the void photo-upload task it was the only model to discover the feature already existed.

**Dispatch this one.** Caveats: it is slow (921s / 1092s per task), it needs more than 30 iterations to converge on polish, and it still ships comment-vs-code defects — so review the diff, but expect real work in it.

###### PENDING — v6 in flight (started 16:26)

**Mac Studio leg** (11 models × 2 tasks): `deepseek-r1:32b` · `deepseek-r1:14b` · `MFDoom/deepseek-r1-tool-calling:14b` · `qwen2.5-coder:14b` · `qwen2.5-coder:7b` (Mac Studio) · `deepseek-r1:7b` (Mac Studio, 131072) · `deepseek-r1:32b-qwen-distill-q8_0` · `qwen3-14b-agentic` · `qwen3-coder-next:q4_K_M` (3600s timeout ×2, dominates the ETA).

**Unraid leg**: ✅ **COMPLETE @ 16:50** — `qwen2.5-coder:7b` and `deepseek-r1:7b`, both tasks each, all four genuine failures.

**`qwen3.8:27b-q8_0`**: ✅ COMPLETE — both tasks scored above.

**Queued behind the Mac Studio leg**: `devstral:24b` re-run under `llama-server` (backend label `macstudio-llamaserver`) — replaces the quarantined native-Ollama rows.

###### clamshell (v6.1, patched worker) — **REPLICATED, and cleaner than the first pass** ✅✅

`exit=0, verify_passed=yes, 825s, files=3, iters=23` — **converged inside budget**, where the v6 run hit the 30-iteration ceiling and recorded `exit=2`.

Verified by execution, again:

```
$ .build/debug/Clamshell confirmation-selftest
PASS: valid signature verifies
PASS: replayed response rejected
PASS: tampered challenge rejected        <- NEW, not requested
PASS: wrong key rejected
waiting 2.5s for the 2s validity window to elapse…
PASS: expired challenge rejected
PASS: confirmation selftest (5/5)
exit: 0                                   (2.5s wall clock)
```

**Five properties this time, not four** — it added a tampered-challenge rejection nobody asked for, on top of the wrong-key case it also volunteered in v6. And again a genuine wall-clock wait for the expiry window rather than a faked clock.

**It chose a different and simpler structure:**
```
 M Sources/Clamshell/main.swift
?? Sources/Clamshell/Auth/ConfirmationBridge.swift
?? Sources/Clamshell/Auth/ConfirmationBridgeSelfTest.swift
```
**No `Package.swift` edit at all.** It dropped the files into the existing `Clamshell` target and wired the subcommand into `main.swift`. In v6 it created `Sources/ConfirmationBridge/` and declared a matching target — also correct, but the harder path. This run took the route that structurally cannot hit the trap that caught `deepseek-r1:32b` (declared a target, filed sources elsewhere) and `MFDoom` (wrote a `Package.swift` *inside* `Sources/`).

> **The improvement is NOT attributable to the harness patches.** `grep -c "empty content with non-empty thinking"` → **0**; `grep -c "repeat-success suppression"` → **0**. Neither new code path executed. Same model, same task, different run, better outcome — that is run-to-run variance, and given how much of this bake-off turned out to be instrument error, it is worth stating plainly rather than crediting the fix.

**Why this matters more than the result itself**: the dispatch recommendation previously rested on a single passing run. It now **replicates at n=2, across two harness versions and two different implementation approaches**, with the second attempt converging in budget and testing more properties than the first. That was the weakest point in the recommendation and it is now the strongest.

## Gotchas that apply across models

**Context ceilings are hardware-bound, not spec-bound.** `deepseek-r1:7b` supports 128K.
Measured on Unraid's RTX 3080 12GB at 131072: `size=20.6GB, size_vram=11.0GB` — **~9.6GB
spilled to CPU**, and the task burned its full 1800s timeout. At 32768: `size=8.2GB`,
fully resident. ~121KB/token of KV cache here. **KV cache, not weights, is the binding
constraint** — the same pattern holds for `deepseek-r1:14b` and `qwen2.5-coder:14b` on
that card. Always check `/api/ps` (`size` vs `size_vram`) rather than estimating.

**Temperature 0.6 is wrong for agentic tool use.** That number comes from model cards'
*reasoning/chat* generation config. `qwen2.5-coder:7b` at 0.6 produced 5 correct files
on one run and 0 on the next from byte-identical inputs — one high-temperature wrong
turn was unrecoverable. Use **0.2 for the qwen family**. Keep **0.6 for deepseek-r1** —
DeepSeek's own guidance is an explicit 0.5–0.7 and deviating risks repetition/incoherence.

**DeepSeek-R1 distills have no tool-call template at all**
([ollama#8517](https://github.com/ollama/ollama/issues/8517)) — Ollama's native
`tool_calls` parser never fires. Requires `--manual-tools` (textual schema injection +
own parsing). This applies family-wide including the `MFDoom` community build.

**Models emit structurally invalid tool-call JSON, in several distinct ways.** Confirmed
live: raw newlines inside string values; Python triple-quotes (`"""`) where JSON needs
`"`; a **file path in the `name` field** instead of the tool name. Each silently
discards real work and looks like model incapacity. An explicit constraint in the tools
block — *"name must be EXACTLY one of: …, never a file path"* plus a correct/incorrect
example — took `deepseek-r1:7b` from 0/4 to 4/4 schema-valid calls.

> **Better fix, not yet implemented**: Ollama supports constrained decoding via a JSON
> schema in its `format` parameter. Tool calls constrained at *generation* time would
> make this entire bug class structurally impossible instead of repaired afterwards.
> **Investigate before writing another repair function.**

**Small models need a directory-listing tool or they fabricate.** Without `list_files`,
both 7B models guessed paths, hit "not found", and invented a whole stack from memory
(a Flask app, then a MERN/React-Native app, in a Next.js/TypeScript/Prisma project).
Given a listing tool, both opened with `list_files({"path": "."})` on the first move.
`read_file` on a directory must return the listing, not a bare errno.

**The system prompt must forbid web-searching the local codebase.** Under a prompt that
encouraged `web_search` for anything uncertain, `qwen3-coder-next` opened with
`web_search("resell-tracker web app codebase structure framework")` then
`web_fetch("https://github.com/topics/resell-tracker")` — burning 2 of the only 4
iterations it could afford.

**Loop-breaking must refuse, not warn.** After an advisory "you have tried this path 3
times" message, `qwen2.5-coder:7b` retried the same dead path anyway, reaching 11
identical failures. Hard-refusing execution after 3 identical failures is what actually
forces a different action.

**Verify the environment by running the real verify command on a pristine tree** — not
by checking that a binary exists. An existence check only catches the last bug you saw:
guarding on `node_modules/.bin/next` passed while `prisma generate` had never been run,
so `next build` could not succeed regardless of what the model wrote.

**Shell access causes unrequested dependency and manifest changes — a side effect of the dependency-install prompt fix.** The `SYSTEM_PROMPT` line telling models to install what they import genuinely fixed a real failure (`deepseek-r1:32b` v3 died on `Module not found: 'uuid'`; in v5 it installed `uuid` itself). But **both** models given `run_bash` then went further than asked:

- `deepseek-r1:32b` installed `@clerk/nextjs` and `clerk-js` — an auth stack the project does not use — apparently assuming uploads needed auth. Mutated `package.json` and the lockfile.
- `deepseek-r1:14b` tried to add `swift-crypto` (unnecessary — macOS provides P-256 via built-in `CryptoKit`) **and rewrote `swift-tools-version: 5.9` → `6.3`**, a version the toolchain rejects, breaking every subsequent build on the manifest rather than the code.

Two for two. The pattern is not "installs the missing import" but "reshapes the project's dependency surface to match what it imagines the solution needs."

**Implications:**
1. **This is a hard blocker for unattended dispatch**, independent of code quality. A model that edits your manifest and lockfile unsupervised can break a build in ways that outlive the run.
2. Score `package.json` / `Package.swift` / lockfile changes **separately** from source changes, and always inspect them — they are where this damage lands.
3. If dispatching without review, consider withholding `run_bash` entirely and accepting the missing-dependency failure mode instead. It is the cheaper of the two failures: a missing import fails loudly and locally, a corrupted manifest fails globally and persists.
4. The worktree-per-model + `git reset --hard` design already contains the blast radius. **Do not dispatch these models against a working tree you care about.**

**The repo documents its own conventions for agents, and the harness never shows them to the model. This is the highest-value fix on the post-v5 list.**

`resell-tracker/AGENTS.md` opens with, verbatim:

> **# This is NOT the Next.js you know**
> This version has breaking changes — APIs, conventions, and file structure may all differ from your training data. Read the relevant guide in `node_modules/next/dist/docs/` before writing any code. Heed deprecation notices.

That file was written *for agents*, and it addresses precisely the failure mode we keep recording. `grep -c 'AGENTS.md\|CLAUDE.md' ollama-worker.py` returns **0**. The harness never reads it, never mentions it, and never tells the model it exists.

So when `deepseek-r1:32b` wrote Pages Router idiom (`export default function handler(req: NextApiRequest, res: NextApiResponse)`) into an App Router `route.ts`, it did so while all of this sat unused:

| Available | Verified present |
|---|---|
| The repo's own agent instructions | `AGENTS.md`, first heading, unread |
| Working examples of the correct idiom | **26** existing route directories under `app/api/` |
| The actual framework docs, on disk | `node_modules/next/dist/docs/{01-app,02-pages,03-architecture}` |

**Frame this as a harness gap, not a model failure.** The models were asked to follow existing conventions while being given no pointer to the three places those conventions were written down. A model relying on training-data Next.js — which the repo explicitly warns is wrong for this version — is behaving reasonably given what it was told.

**Consequence for every result in this guide: these scores are a floor, not a ceiling.** Framework-convention errors are the single most common failure recorded here, and the correction for them was sitting unread in every worktree. Do not read any model's ranking as its capability under a harness that surfaces `AGENTS.md` and points at the on-disk docs.

**Native tool-calling does not work through Ollama for ANY model tested — including the ones that should support it.**

| Model | `--manual-tools` | Native calls | Via fallback-parse |
|---|---|---|---|
| `deepseek-r1:32b` | yes | 0 | 18 / 7 |
| `MFDoom/…-tool-calling:14b` | yes | 0 | 11 / 11 |
| `qwen2.5-coder:14b` | **no** — native `tools` sent | **0** | **30 / 30** |

The qwen row is the decisive one. It ran *without* `--manual-tools`, the worker genuinely sends `payload["tools"]`, and Ollama's native parser still returned nothing on every single call — the model emits fenced ```` ```json ```` blocks in the content instead. The vault's earlier inference (a Hermes-style template in `tokenizer_config.json` ⇒ native tool-calling works in Ollama) **does not hold in practice**.

Practical effect: **the fallback text parser is not a fallback — it is the only thing that works.** Every tool call in this entire bake-off was recovered by it. That elevates the constrained-decoding investigation (Ollama's `format` JSON-schema parameter) from an optimisation to the central question, since the text parser is currently a single point of failure for all dispatch, on all models.

> ### ⚠️ CORRECTION — native tool-calling DOES work, for one model
>
> The table above says native tool-calling works for no model tested. **That is now falsified.** `devstral:24b`, on its first-ever native-Ollama run (v6), emitted **4 native `tool_calls`** — all four `list_files` — with only its single `read_file` needing fallback recovery.
>
> | Model | Native | Fallback | Why |
> |---|---|---|---|
> | `devstral:24b` | **4** | 1 | Ollama ships a real tool-aware template (823 chars, capabilities `[completion, tools]`) |
> | `qwen2.5-coder:14b` | 0 | 30 | Template exists, but the model emits fenced ` ```json ` in content — Ollama's parser never fires |
> | `deepseek-r1:32b` | 0 | 18 / 7 | No tool logic in the template at all ([ollama#8517](https://github.com/ollama/ollama/issues/8517)) |
> | `MFDoom/…-tool-calling:14b` | 0 | 11 | Same template gap as its base, despite the fine-tune's premise |
>
> **The corrected statement**: native tool-calling in Ollama depends on *both* a tool-aware template **and** the model emitting the exact token sequence Ollama's parser expects. A Hermes-style template in `tokenizer_config.json` is not sufficient — `qwen2.5-coder:14b` has one and still scores 0/30, because it wraps calls in markdown fences.
>
> **The fallback text parser is still load-bearing for every model except devstral**, so it remains a near-universal single point of failure and the constrained-decoding investigation still matters. But it is no longer true that Ollama's native path has never worked.

> ### ⛔ RETRACTED — "`llama-server` is now unnecessary for this stack"
>
> This guide previously closed the section above with: *"Also settled: `llama-server` is now unnecessary for this stack. Both models that ever required it (`devstral:24b`, `qwen3.8:27b-q8_0`) run natively on Ollama 0.32.14."* **Half of that is wrong, and it cost two result rows** (bug 10).
>
> | Model | Native Ollama | Verdict |
> |---|---|---|
> | `qwen3.8:27b-q8_0` | ✅ works — 39 tool calls, the best exploration in the matrix | genuinely no longer needs `llama-server` |
> | `devstral:24b` | ❌ **zero tool calls** on photo-upload; narrated and stopped on clamshell | **still requires `llama-server`** |
>
> **`devstral:24b` must run under `llama-server`** on :8091 with `--jinja --chat-template-file ~/bin/devstral-tool-template.jinja`, worker `--api openai --num-ctx 65536 --temperature 0.2`, and **no** `--system-prompt-file`.
>
> **The reasoning error worth remembering**: the native switch was justified by `capabilities: ['completion','tools']` plus a **single-turn probe** that returned a correct native `tool_call`. That validated one imperative request — it did not test multi-turn agentic behaviour under the worker's system prompt, which is the thing that broke. **A capability probe is not a workload test.** The worker's own `--system-prompt-file` help already documented the failure verbatim; a proven config was traded for an unproven one on evidence that never touched the failure mode.

**A verify command cannot tell you whether the task was worth doing. Check the premise against the tree before dispatching.**

The single largest measurement error in this bake-off was not a harness bug — it was that the resell-tracker task asked for a feature the repo **already shipped** (see the ⚠️ box in *How to read a result*). Every guard we built checked that the *environment* was sound: pristine-tree preflight, pinned baseline SHAs, worktree existence, iteration counts, `/api/ps` footprints. **Nothing ever checked that the task's premise matched the tree.**

The cost was not a lost run, it was an inverted ranking — the model that correctly discovered the feature existed scored zero, and the model that duplicated it while deleting two working files scored highest. v7 adds an `ABORT_NO_TARGET` preflight that refuses to dispatch when the file the task presupposes is absent. **Any future task needs the equivalent: an assertion, checked at baseline, that the thing being asked for is genuinely not there yet.**

**The `AGENTS.md` fix works, and it only reaches models above ~14B.** The v6 `SYSTEM_PROMPT` change (read `AGENTS.md`/`CLAUDE.md`/`README`/`CONTRIBUTING`; read an existing file of the same kind before creating one) demonstrably reached the models — but unevenly:

| Model | Context files read |
|---|---|
| `qwen3.8:27b-q8_0` | **`AGENTS.md` + `CLAUDE.md`** ✅ — plus `node_modules/next/dist/docs/` and `git log` |
| `qwen2.5-coder:7b` | `README.md` only — `AGENTS.md` was visible in its first listing (327 bytes) and never opened |
| `deepseek-r1:7b`, `devstral:24b` | none |

**This is a capacity result, not a harness gap.** Do not credit small-model v6-vs-v5 deltas to the context fix. A v7 variant that injected `AGENTS.md` contents directly into the prompt was considered and **rejected** — it would not rescue models that are failing for capacity reasons, and it would contaminate the measurement of whether a model can find context on its own.

> ### ⚠️ CORRECTION — the context fix reaches 32B too, and does not help it
>
> The table above says `qwen3.8:27b-q8_0` was the only model to read a context file. **That was written before `deepseek-r1:32b`'s v6 photo-upload run finished, and it is wrong.** 32b read **`AGENTS.md` and `README.md`** — its second and third tool calls.
>
> | Model | Context files read | Outcome |
> |---|---|---|
> | `qwen3.8:27b-q8_0` | `AGENTS.md` + `CLAUDE.md` + on-disk Next docs + `git log` | found the existing feature ✅ |
> | `deepseek-r1:32b` | **`AGENTS.md` + `README.md`** | **still failed, `files=0`** ❌ |
> | `qwen2.5-coder:7b` | `README.md` only | failed |
> | `deepseek-r1:7b`, `devstral:24b` | none | failed |
>
> **Reading the file is not the same as using it.** `AGENTS.md` says verbatim *"file structure may differ from your training data."* 32b read that sentence and then spent five of its thirteen tool calls on `app/components` — **a directory that does not exist at baseline** — while the real `components/` sat at top level, visible in the output of its own first `list_files(".")`.
>
> So the corrected claim is narrower than either version: the v6 prompt change **successfully causes 27B+ models to open the context files**, and only `qwen3.8:27b-q8_0` converted that into correct behaviour. **Do not treat "read AGENTS.md" as a proxy for "grounded in the real codebase."**


## Candidate: ornith-1.5 (found 2026-08-24, NOT yet tested)
New self-improving open-source coding LLM family on Ollama. Sizes: `ornith-1.5:9b` (6.6GB dense), `ornith-1.5:35b` (23GB MoE), `ornith-1.5:397b` (242GB MoE). 256K context, MIT licensed, GGUF/MLX/FP8/NVFP4 day-one, emits STANDARD function calls (relevant given the v8/v9 tool-call-dialect fixes). Claims parity with Claude Opus 4.8 — treat as a claim to FALSIFY, not trust, per [[feedback_qwen3_coder_next_reliability]] and the monthly-check discipline.
- Fits the Mac Studio (64GB): 9b + 35b yes; 397b (242GB) NO.
- Does NOT go in v9 (frozen rerun roster). Candidate for the next fresh round / v10 quality-arm roster.
- Repo: github.com/deepreinforce-ai/Ornith-1 · ollama.com/library/ornith-1.5
- Status: not pulled, not tested. Real-test after OCR frees the GPU before believing the Opus-parity claim.
