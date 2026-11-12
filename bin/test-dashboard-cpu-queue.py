#!/usr/bin/env python3
"""CPU-lane stages as first-class QUEUE rows (the owner 2026-10-10): the dashboard showed CPU activity in the
CPU lane panel but the Queue panel said "The queue is empty." Proves, running the page's own
cpuQueueRows()/cpuQueueRowHtml()/renderCpuQueue() under node with a stub DOM:
  * running (runner), pending, and local_running (audit-trivial on the Mac, with its fallback reason)
    each become a Queue row with stage / label / bundle / where / status / seconds;
  * GPU idle + CPU running: rows are listed, #activeEmpty is HIDDEN, the count line names the CPU stages;
  * truly nothing anywhere: #activeEmpty is shown and no rows render;
  * the Queue tile counts CPU pending and the idle tile says the CPU lane is active.
Remote lane state (2026-11-04), from /api/cpu-lane: cpuRowDetail() (cancelling, phase, the runner's progress line,
model calls with the failed ones and the ones that met a Darkbloom Sandbox hold, why a pending job waits) shows,
escaped, on the Queue rows; refreshCpuLane() puts the hold in the lane summary line and the detail in the lane table
(the state cell carries cancelling/phase, so the time cell does not repeat them).
Runner limits (2026-11-05), from /api/cpu-lane `runners`: cpuRunnerText() is one line per runner (jobs running / slots,
lane jobs running / lane_max, its hold reasons per kind - all, lane and plain, the last one being what a lane-only runner
(CPU_RUNNER_ACCEPT=lane) always holds - its disk: work GB and free GB, the uv / npm / node_modules caches against their
caps, the report's age); refreshCpuLane() draws the lines, HTML-escaped (the runner chooses its own id and hold text),
into #cpuRunners; a hold on all or lane jobs marks the line (amber), a lane-only runner's standing plain hold does not.
Run: python3 test-dashboard-cpu-queue.py [--src PATH] [--mutants]"""
import importlib.util, json, os, re, subprocess, sys, tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

HERE = Path(__file__).resolve().parent
REAL_HOME = os.environ["HOME"]
FAILS = []
DEFAULT_SRC = Path(REAL_HOME) / "Desktop/GitHub Projects/ollama-queue-dashboard/src/ollama-queue-api.py"

MUTANTS = [
    ("empty line ignores CPU stages",
     "ae.hidden = !!(q.bundles || q.jobs || n.total);", "ae.hidden = !!(q.bundles || q.jobs);"),
    ("local_running stages are not rows",
     "for (const c of (d.local_running || [])) rows.push(", "for (const c of []) rows.push("),
    ("pending stages are not rows",
     "for (const c of (d.pending || [])) rows.push(", "for (const c of []) rows.push("),
    ("running runner stages are not rows",
     "for (const c of (d.running || [])) rows.push(", "for (const c of []) rows.push("),
    ("fallback reason is not shown",
     "const why = r.reason ? ", "const why = false ? "),
    ("owning bundle is not shown",
     "const bundle = nested ? '' : (standaloneHash", "const bundle = true ? '' : (standaloneHash"),
    ("owner label never shown on standalone rows",
     "const owner = (r.owner && !nested) ? ", "const owner = false ? "),
    ("standalone hash row shows a fake bundle link instead of the tag",
     "const standaloneHash = !r.bundle || /^[0-9a-f]{12}$/.test(r.bundle);", "const standaloneHash = false;"),
    ("pending rows drop the owner", "owner: c.owner || '', where: 'waiting for a runner'", "owner: '', where: 'waiting for a runner'"),
    ("summary never renders the cpu rows",
     "  renderCpuQueue(q);\n", ""),
    ("cpu poll never renders the cpu rows",
     "      (j.duration_s == null ? '' : j.duration_s + 's') + (j.queue_wait_s == null ? '' : ' / wait ' + j.queue_wait_s + 's')));\n    renderCpuQueue();\n",
     "      (j.duration_s == null ? '' : j.duration_s + 's') + (j.queue_wait_s == null ? '' : ' / wait ' + j.queue_wait_s + 's')));\n"),
    ("cpu data never captured", "    window.cpuLaneData = d;\n", ""),
    ("count line omits CPU stages", "(n.total ? ` · ${n.total} CPU stage", "(false ? ` · ${n.total} CPU stage"),
    ("queue tile ignores cpu pending", "pend + paused + planned + cpuN.pending", "pend + paused + planned"),
    ("idle wait text still says 'Queue empty' with CPU busy", "return cn.total ? 'GPU queue empty - CPU lane busy (", "return false ? 'GPU queue empty - CPU lane busy ("),
    ("cpu rows never nested in the bundle group", "    if (!hd) { plan.standalone.push(...plan.nested[k]); continue; }", "    plan.standalone.push(...(plan.nested[k] || [])); continue;"),
    ("cpu row lands before the bundle's GPU rows, not after", "let last = hd;\n    while (", "let last = hd;\n    while (false && "),
    ("cpu-only bundle gets no header", "if (!hasHeader(k) && !cpuOnly.includes(k)) cpuOnly.push(k);", ""),
    ("re-render does not clear earlier nested rows", "forEach(t => t.remove());", "forEach(t => 0);"),
    ("collapsed bundle still shows cpu rows", "if (hd.dataset.expanded === '0') continue;", ""),
    ("bundle-less rows are nested nowhere and dropped", "if (!k || (!hasHeader(k) && /^[0-9a-f]{12}$/.test(k))) { standalone.push(r); continue; }", "if (!k) continue;"),
    ("idle tile still says nothing", "cpuQueueCounts(window.cpuLaneData).running ? ' - CPU lane active (", "false ? ' - CPU lane active ("),
    # remote lane state (2026-11-04)
    ("hold banner never shown",
     "(mh.held ? ' - DARKBLOOM SANDBOX HOLD: model jobs wait'", "(false ? ' - DARKBLOOM SANDBOX HOLD: model jobs wait'"),
    ("pending row drops why it waits", "'s' + (j.waiting ? ' - ' + j.waiting : '')));", "'s'));"),
    ("held model calls not in the detail", "+ (c.llm_held ? ', ' + c.llm_held + ' met a sandbox hold' : '')", "+ ''"),
    ("progress line dropped",
     "c.phase ? 'phase ' + c.phase : '', c.progress || '', llm", "c.phase ? 'phase ' + c.phase : '', '', llm"),
    ("lane table repeats phase/cancelling in the time cell",
     "cpuRowDetail(Object.assign({}, j, {cancelling: false, phase: ''}))", "cpuRowDetail(j)"),
    ("queue row never shows the detail",
     "${r.detail ? ' <span class=\"cpu-detail\">' + escapeHtml(r.detail) + '</span>' : ''}", "${''}"),
    ("detail not escaped",
     "' <span class=\"cpu-detail\">' + escapeHtml(r.detail) + '</span>'", "' <span class=\"cpu-detail\">' + r.detail + '</span>'"),
    ("running rows carry no detail",                      # the first of the two: the running rows
     "reason: '', id: c.id || '', detail: cpuRowDetail(c)});", "reason: '', id: c.id || '', detail: ''});"),
    ("cancelling state not in the lane table", "add((j.cancelling ? 'cancelling' : 'running')", "add(('running')"),
    ("cancelling not in the detail", "[c.cancelling ? 'cancelling' : '', ", "['', "),
    ("failed model calls not counted", "(c.llm_errors ? ', ' + c.llm_errors + ' failed' : '')", "''"),
    ("model call count never singular", "' model call' + (n === 1 ? '' : 's')", "' model call' + 's'"),
    ("pending wait reason not in the detail", "c.progress || '', llm, c.waiting || '']", "c.progress || '', llm, '']"),
    ("lane table time cell drops the detail",
     "Math.round(j.running_s) + 's' + (det ? ' - ' + det : '')", "Math.round(j.running_s) + 's'"),
    # runner limits (2026-11-05)
    ("runner lines are never drawn",
     "document.getElementById('cpuRunners').innerHTML = (d.runners || []).map(r =>",
     "document.getElementById('cpuRunners').innerHTML = ([]).map(r =>"),
    ("runner lines are not escaped", "${escapeHtml(cpuRunnerText(r))}</div>`", "${cpuRunnerText(r)}</div>`"),
    ("hold text not rendered (lane)", "if (h.lane) out.push('HOLD lane: ' + h.lane);", "if (false) out.push('HOLD lane: ' + h.lane);"),
    ("hold text not rendered (all)", "if (h.all) out.push('HOLD all: ' + h.all);", "if (false) out.push('HOLD all: ' + h.all);"),
    ("hold text not rendered (plain: a lane-only runner)", "if (h.plain) out.push('HOLD plain: ' + h.plain);",
     "if (false) out.push('HOLD plain: ' + h.plain);"),
    ("a held runner's line is not marked", "${r.hold && (r.hold.all || r.hold.lane) ? ' held' : ''}", "${''}"),
    ("a hold on lane jobs does not mark the line", "r.hold && (r.hold.all || r.hold.lane) ? ' held' : ''",
     "r.hold && r.hold.all ? ' held' : ''"),
    ("a hold on all jobs does not mark the line", "r.hold && (r.hold.all || r.hold.lane) ? ' held' : ''",
     "r.hold && r.hold.lane ? ' held' : ''"),
    ("a lane-only runner's standing plain hold marks its line", "r.hold && (r.hold.all || r.hold.lane) ? ' held' : ''",
     "r.hold ? ' held' : ''"),
    ("busy/slots not shown", "if (num(r.slots) && r.slots > 0) out.push(", "if (false) out.push("),
    ("lane busy/lane_max not shown", "if (num(r.lane_max)) out.push(", "if (false) out.push("),
    ("lane_max 0 (lane jobs off) reads as not reported", "if (num(r.lane_max)) out.push(", "if (r.lane_max) out.push("),
    ("caches shown without their caps", "(num(d[k + '_max_gb']) ? '/' + gb(d[k + '_max_gb']) : '')", "''"),
    ("free space of the cache dir not shown", "if (num(d.cache_free_gb)) p.push(", "if (false) p.push("),
    ("free space of the work dir not shown", "(num(d.work_free_gb) ? ' (' + gb(d.work_free_gb) + ' GB free)' : '')", "''"),
    ("disk line not shown", "if (p.length) out.push('disk: ' + p.join(', '));", ""),
    ("report age not shown", "if (num(r.disk_s_ago)) out.push(", "if (false) out.push("),
    ("a runner that stopped polling looks online", "if (num(r.seen_s_ago) && r.seen_s_ago >= 120) out.push(", "if (false) out.push("),
]


def check(name, got, want):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f": {name}" + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(name)


def html_of(src):
    home = tempfile.mkdtemp(prefix="cpu-queue-")
    os.environ["HOME"] = home
    os.symlink(os.environ.get("OLLAMA_PIPELINE_BIN", str(Path(REAL_HOME) / "bin")), os.path.join(home, "bin"))
    ld = SourceFileLoader("qapi_cpuq", str(src))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader("qapi_cpuq", ld))
    ld.exec_module(m)
    return m.FRONTEND_HTML


def node(js):
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True)
    return json.loads(out.stdout) if out.returncode == 0 else {"err": out.stderr[-600:]}


STUB = r"""
const els = {};
const mk = (id) => els[id] || (els[id] = {id, textContent: '', hidden: false, innerHTML: '', children: [], dataset: {},
  className: '', classList: {add(){}, remove(){}},
  appendChild(c){ this.children.push(c); }, addEventListener(){}, querySelectorAll(){ return []; },
  querySelector(){ return mk(id + '>tbody'); }});
// a tiny DOM for the #jobs table: rows with parent/sibling links, classList, insertBefore, remove
class El {
  constructor(){ this.children = []; this.parentNode = null; this.dataset = {}; this.innerHTML = ''; this.cls = new Set();
    const c = this.cls; this.classList = {add: x => c.add(x), remove: x => c.delete(x), contains: x => c.has(x)}; }
  set className(v){ this.cls.clear(); String(v).split(/\s+/).filter(Boolean).forEach(x => this.cls.add(x)); }
  get className(){ return [...this.cls].join(' '); }
  get nextElementSibling(){ const i = this.parentNode ? this.parentNode.children.indexOf(this) : -1; return i >= 0 ? (this.parentNode.children[i + 1] || null) : null; }
  appendChild(c){ c.parentNode = this; this.children.push(c); return c; }
  insertBefore(c, ref){ c.parentNode = this; const i = ref ? this.children.indexOf(ref) : -1;
    if (i < 0) this.children.push(c); else this.children.splice(i, 0, c); return c; }
  remove(){ if (this.parentNode) this.parentNode.children = this.parentNode.children.filter(x => x !== this); this.parentNode = null; }
  addEventListener(){}
}
const jobsBody = new El();
const jobsSel = sel => sel.split(',').map(x => x.trim()).filter(x => x.startsWith('#jobs tbody tr')).flatMap(x => {
  const cl = x.replace('#jobs tbody tr', '').split('.').filter(Boolean);
  return jobsBody.children.filter(c => cl.every(k => c.cls.has(k))); });
global.window = {};
global.document = {getElementById: mk, createElement: () => new El(),
  querySelector: s => s === '#jobs tbody' ? jobsBody : null, querySelectorAll: jobsSel};
const mkHdr = (key, expanded) => { const h = new El(); h.className = 'queue-parent'; h.dataset.bundle = key;
  h.dataset.expanded = expanded === false ? '0' : '1'; jobsBody.appendChild(h); return h; };
const mkKid = (label) => { const k = new El(); k.className = 'queue-child'; k.dataset.label = label; jobsBody.appendChild(k); return k; };
const jobsOrder = () => jobsBody.children.map(c => (c.cls.has('queue-parent') ? 'H:' + c.dataset.bundle : c.cls.has('cpu-nested') ? 'CPU:' + c.dataset.stage : c.dataset.label || '?'));
const escapeHtml=s=>String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;');
"""


def nest(code, d):
    """CPU stages render INSIDE their bundle's group in #jobs (the owner 2026-10-10)."""
    d = dict(d); d["local_running"] = [dict(d["local_running"][0], bundle="rt-amazon-cardlink-rs1")]
    d["running"] = [dict(d["running"][0], bundle="rt-amazon-cardlink-rs1")]
    d["pending"] = [dict(d["pending"][0], bundle="cpu-only-bundle")]
    setup = ("window.lastQueueQ={bundles:2,jobs:3};"
             "mkHdr('other-bundle'); mkKid('other-gpu'); mkHdr('rt-amazon-cardlink-rs1'); mkKid('gpu-slice-1'); mkKid('gpu-slice-2');"
             "mkHdr('collapsed-b', false); mkKid('hidden-gpu'); const solo = new El(); solo.dataset.label = 'standalone'; jobsBody.appendChild(solo);"
             "window.cpuLaneData=" + json.dumps(d) + ";")
    r = node(code + setup + "renderCpuQueue(); const a = jobsOrder(); renderCpuQueue(); renderCpuQueue(); const b = jobsOrder();"
             "console.log(JSON.stringify({a, b, tbl: els['cpuQueue>tbody'].children.length, tblHidden: els.cpuQueue.hidden,"
             "emptyHidden: els.activeEmpty.hidden, count: els.activeCount.textContent}));")
    want = ["H:other-bundle", "other-gpu", "H:rt-amazon-cardlink-rs1", "gpu-slice-1", "gpu-slice-2", "CPU:relevance", "CPU:audit-trivial",
            "H:collapsed-b", "hidden-gpu", "standalone", "H:cpu-only-bundle", "CPU:baseline-verify"]
    check("CPU stages sit at the end of their bundle's group; CPU-only bundle gets its own header", r.get("a"), want)
    check("re-render is idempotent (no duplicates)", r.get("b"), want)
    check("nothing nested falls back to the standalone table; table hidden", (r.get("tbl"), r.get("tblHidden")), (0, True))
    check("count line still counts nested CPU stages", "3 CPU stages (2 running, 1 pending)" in str(r.get("count")), True)
    # bundle-less and bare-job-id rows fall back; collapsed bundle hides its CPU children but does not leak to the table
    d2 = {"running": [{"id": "a1", "label": "x", "stage": "relevance", "bundle": "", "runner": "r", "running_s": 1}],
          "pending": [{"id": "b2", "label": "y", "stage": "audit", "bundle": "9800001065f0", "waiting_s": 1}],
          "local_running": [{"stage": "audit-trivial", "bundle": "collapsed-b", "reason": "r", "running_s": 2}]}
    r = node(code + "mkHdr('collapsed-b', false); mkKid('hidden-gpu'); window.cpuLaneData=" + json.dumps(d2) + ";renderCpuQueue({bundles:1,jobs:1});"
             "console.log(JSON.stringify({o: jobsOrder(), tbl: els['cpuQueue>tbody'].children.length, tblHidden: els.cpuQueue.hidden}));")
    check("collapsed bundle hides its CPU row; bundle-less + bare-id rows use the standalone table",
          (r.get("o"), r.get("tbl"), r.get("tblHidden")), (["H:collapsed-b", "hidden-gpu"], 2, False))
    # the CPU-only header disappears when the stage finishes
    r = node(code + "window.cpuLaneData=" + json.dumps(d) + ";renderCpuQueue({bundles:0,jobs:0}); window.cpuLaneData={}; renderCpuQueue({bundles:0,jobs:0});"
             "console.log(JSON.stringify({o: jobsOrder(), emptyHidden: els.activeEmpty.hidden}));")
    check("finished CPU work leaves no header/rows behind; queue reads empty", (r.get("o"), r.get("emptyHidden")), ([], False))


def run(src):
    html = html_of(src)
    m = re.search(r"function cpuQueueRows\(d\) \{.*?\nfunction renderSummary\(", html, re.S)
    check("cpu queue helpers present", bool(m), True)
    for needle in ("renderCpuQueue(q);", "window.cpuLaneData = d;", "id=\"cpuQueue\"", "pend + paused + planned + cpuN.pending",
                   "(j.duration_s == null ? '' : j.duration_s + 's') + (j.queue_wait_s == null ? '' : ' / wait ' + j.queue_wait_s + 's')));\n    renderCpuQueue();\n",
                   "cpuQueueCounts(window.cpuLaneData).running ? ' - CPU lane active ("):
        check(f"page wires {needle}", needle in html, True)
    if not m:
        return
    code = STUB + m.group(0).rsplit("function renderSummary(", 1)[0]
    d = {"running": [{"id": "9ed5c661", "label": "relevance@cb2015", "stage": "relevance", "bundle": "9800001065f0",
                      "runner": "unraid-cpu-1", "running_s": 188.1}],
         "pending": [{"id": "59c09289", "label": "baseline-verify@271c", "stage": "baseline-verify", "bundle": "infixture",
                      "waiting_s": 1.8}],
         "local_running": [{"stage": "audit-trivial", "bundle": "rt-amazon-cardlink-rs1",
                            "reason": "runs locally by design (audit rewrites the target in this worktree)", "running_s": 62.4}]}
    r = node(code + "console.log(JSON.stringify({rows: cpuQueueRows(" + json.dumps(d) + "),"
             "html: cpuQueueRows(" + json.dumps(d) + ").map(r => cpuQueueRowHtml(r)), counts: cpuQueueCounts(" + json.dumps(d) + ")}));")
    rows = r.get("rows") or []
    check("three rows: running, local_running, pending (in that order)", [x["kind"] for x in rows], ["running", "local_running", "pending"])
    check("row fields", [(x["stage"], x["bundle"], x["where"], x["status"]) for x in rows], [
        ("relevance", "9800001065f0", "unraid-cpu-1", "running"),
        ("audit-trivial", "rt-amazon-cardlink-rs1", "local (Mac)", "running"),
        ("baseline-verify", "infixture", "waiting for a runner", "pending")])
    h = r.get("html") or ["", "", ""]
    check("local row shows the fallback reason", "local: runs locally by design" in h[1], True)
    check("local row names the owning bundle as a link", 'data-bundle="rt-amazon-cardlink-rs1"' in h[1], True)
    check("elapsed / wait seconds shown", ("running 188s" in h[0], "running 62s" in h[1], "waiting 2s" in h[2]), (True, True, True))
    # owner provenance (2026-10-10): a bare 12-hex "bundle" is a standalone harness, not a queue bundle
    d_o = {"pending": [{"id": "f1", "label": "relevance@f92d4002bea1:cf6b1a322604e0e7f5ef320a", "stage": "relevance",
                        "bundle": "d698b2cdbee3", "owner": "preflight: ab-sampling v2 d698b2cdbee3", "waiting_s": 3}],
           "running": [{"id": "f2", "label": "x", "stage": "audit-trivial", "bundle": "rt-amazon-cardlink-rs1", "owner": "slice y", "running_s": 1}]}
    ro = node(code + "console.log(JSON.stringify(cpuQueueRows(" + json.dumps(d_o) + ").map(r => cpuQueueRowHtml(r))));")
    ho = ro if isinstance(ro, list) else ["", ""]
    check("standalone hash row: owner shown", "preflight: ab-sampling v2 d698b2cdbee3" in ho[1], True)
    check("standalone hash row: muted 'standalone preflight' tag, no fake bundle link",
          ("standalone preflight" in ho[1], "bundle d698b2cdbee3" in ho[1], "cpu-bundle" in ho[1]), (True, False, False))
    check("real bundle row keeps its bundle link", ('data-bundle="rt-amazon-cardlink-rs1"' in ho[0], "standalone preflight" in ho[0]), (True, False))
    rn = node(code + "console.log(JSON.stringify(cpuQueueRowHtml({kind:'pending',status:'pending',stage:'a',label:'b',bundle:'d698b2cdbee3',owner:'OWN',where:'w',secs:1,reason:''}, true)));")
    check("nested rows stay unchanged (no owner/tag/link)", ("OWN" in str(rn), "standalone" in str(rn), "cpu-bundle" in str(rn)), (False, False, False))
    check("old payload without owner still renders (fail-open)", "standalone preflight" in str(node(code + "console.log(JSON.stringify(cpuQueueRowHtml({kind:'pending',status:'pending',stage:'a',label:'b',bundle:'d698b2cdbee3',where:'w',secs:1,reason:''})));")), True)
    check("counts", r.get("counts"), {"running": 2, "pending": 1, "total": 3})

    # GPU idle + CPU running -> rows, NOT empty
    r2 = node(code + "window.lastQueueQ={bundles:0,jobs:0}; window.cpuLaneData=" + json.dumps(d) + ";"
              "renderCpuQueue(); const e=els.activeEmpty, c=els.activeCount, t=els.cpuQueue;"
              "console.log(JSON.stringify({emptyHidden:e.hidden, count:c.textContent, tblHidden:t.hidden, "
              "n: els['cpuQueue>tbody'].children.length}));")
    check("GPU-idle + CPU-running: empty line hidden", r2.get("emptyHidden"), True)
    check("GPU-idle + CPU-running: only the bundle-less row stays in the standalone table", (r2.get("tblHidden"), r2.get("n")), (False, 1))
    check("count line names the CPU stages", "3 CPU stages (2 running, 1 pending)" in str(r2.get("count")), True)
    nest(code, d)
    # nothing anywhere -> empty
    r3 = node(code + "window.lastQueueQ={bundles:0,jobs:0}; window.cpuLaneData={running:[],pending:[],local_running:[]};"
              "renderCpuQueue(); console.log(JSON.stringify({emptyHidden:els.activeEmpty.hidden, tblHidden:els.cpuQueue.hidden, "
              "count:els.activeCount.textContent}));")
    check("truly idle: 'queue is empty' shown, no cpu table", (r3.get("emptyHidden"), r3.get("tblHidden")), (False, True))
    check("truly idle: count line has no CPU part", "CPU" in str(r3.get("count")), False)
    # GPU jobs present, no cpu -> still hidden, no cpu text
    r4 = node(code + "window.lastQueueQ={bundles:1,jobs:2}; window.cpuLaneData={}; renderCpuQueue();"
              "console.log(JSON.stringify({emptyHidden:els.activeEmpty.hidden, count:els.activeCount.textContent}));")
    check("GPU jobs, no CPU stages: unchanged count line", r4, {"emptyHidden": True, "count": "1 bundle · 2 jobs"})
    mi = re.search(r"function idleWaitText\(.*?\n\}\n", html, re.S)
    check("idleWaitText present", bool(mi), True)
    if mi:
        w = node(code + mi.group(0) + "window.cpuLaneData=" + json.dumps(d) + ";const a=idleWaitText(null,[],null);"
                 "window.cpuLaneData={};const b=idleWaitText(null,[],null);console.log(JSON.stringify({a,b}));")
        check("GPU queue empty but CPU busy: idle text says CPU lane busy", "CPU lane busy (2 running, 1 pending)" in str(w.get("a")), True)
        check("nothing anywhere: idle text stays 'Queue empty'", w.get("b"), "Queue empty")
    esc = node(code + "console.log(JSON.stringify(cpuQueueRowHtml({kind:'running',status:'running',stage:'<script>',label:'<img>',bundle:'<svg>',where:'w',secs:1,reason:''})));")
    check("html escapes labels", any(t in str(esc) for t in ("<script>", "<img>", "<svg>")), False)
    remote_lane_state(code, html)
    runner_lines(code, html)


def remote_lane_state(code, html):
    """The remote lane's per-job state (2026-11-04) on the Queue rows and in the CPU lane panel."""
    det = node(code + "console.log(JSON.stringify([cpuRowDetail({llm:true,llm_calls:1}),"
               "cpuRowDetail({llm:true,llm_calls:3,llm_errors:1,llm_held:2}),"
               "cpuRowDetail({cancelling:true,phase:'deps',progress:'npm ci 3/10',llm:true,llm_calls:0,waiting:'x'}),"
               "cpuRowDetail({llm:false,llm_calls:5}), cpuRowDetail(null)]));")
    check("cpuRowDetail: cancelling, phase, progress, model calls (failed / held), why it waits", det,
          ["1 model call", "3 model calls, 1 failed, 2 met a sandbox hold",
           "cancelling - phase deps - npm ci 3/10 - 0 model calls - x", "", ""])
    dd = {"running": [{"id": "r1", "label": "a", "stage": "verify", "bundle": "b1", "runner": "u1", "running_s": 5,
                       "cancelling": True, "phase": "setup", "progress": "pip install", "llm": True, "llm_calls": 1}],
          "local_running": [{"stage": "audit-trivial", "bundle": "b2", "reason": "r", "running_s": 2}],
          "pending": [{"id": "p1", "label": "c", "stage": "verify", "bundle": "b3", "waiting_s": 3,
                       "waiting": "no runner reports llm_relay"}]}
    rd = node(code + "const rows = cpuQueueRows(" + json.dumps(dd) + ");"
              "console.log(JSON.stringify({det: rows.map(r => r.detail || ''), html: rows.map(r => cpuQueueRowHtml(r)),"
              "esc: cpuQueueRowHtml({kind:'running',status:'running',stage:'s',label:'l',bundle:'b1',where:'w',secs:1,"
              "reason:'',detail:'<b>x</b>'})}));")
    check("queue rows carry the lane detail (running: state/phase/progress/model calls; pending: why it waits; "
          "local: none)", rd.get("det"),
          ["cancelling - phase setup - pip install - 1 model call", "", "no runner reports llm_relay"])
    hh = rd.get("html") or ["", "", ""]
    check("queue row html shows the detail",
          ('<span class="cpu-detail">cancelling - phase setup - pip install - 1 model call</span>' in hh[0],
           "cpu-detail" in hh[1], '<span class="cpu-detail">no runner reports llm_relay</span>' in hh[2]),
          (True, False, True))
    check("the detail is escaped", ("&lt;b>x&lt;/b>" in str(rd.get("esc")), "<b>x</b>" in str(rd.get("esc"))),
          (True, False))
    mr = re.search(r"async function refreshCpuLane\(\) \{.*?\n\}\n", html, re.S)
    check("refreshCpuLane present", bool(mr), True)
    if not mr:
        return
    held_t = "darkbloom sandbox hold: model jobs wait until it is released"
    run_j = {"id": "j1", "stage": "verify", "label": "verify@x", "bundle": "b1", "runner": "u1", "running_s": 12.2,
             "phase": "deps", "progress": "npm ci 3/10", "llm": True, "llm_calls": 3, "llm_errors": 1, "llm_held": 2,
             "cancelling": False}
    base = {"enabled": True, "counts": {"running": 1, "done": 0}, "queue_depth": 1,
            "runners": [{"runner": "u1", "seen_s_ago": 3}], "model_hold": {"held": True, "cause": "ci-job-77"},
            "running": [run_j], "pending": [{"id": "j2", "stage": "verify", "label": "verify@y", "bundle": "b2",
                                             "waiting_s": 30.4, "waiting": held_t}],
            "recent": [], "local_running": [], "why_local": {"n": 0, "groups": []}}
    cases = [base, dict(base, running=[dict(run_j, cancelling=True)]),
             dict(base, model_hold={"held": False, "cause": None}), dict(base, model_hold={"held": True, "cause": None})]
    out = node(code + mr.group(0)
               + "const laneBody = new El();"
               "global.document.querySelector = s => s === '#cpuLaneTable tbody' ? laneBody : (s === '#jobs tbody' ? jobsBody : null);"
               "let LANE = null;"
               "Object.defineProperty(globalThis, 'fetch', {value: async () => ({json: async () => LANE}), writable: true,"
               " configurable: true});"
               "const once = async d => { LANE = d; laneBody.children = []; await refreshCpuLane();"
               " return {sum: els.cpuLaneSummary.textContent, rows: laneBody.children.map(tr => tr.children.map(td => td.textContent))}; };"
               "(async () => { const o = []; for (const d of " + json.dumps(cases) + ") o.push(await once(d));"
               " console.log(JSON.stringify(o)); })();")
    o = out if isinstance(out, list) and len(out) == 4 else [{}] * 4
    check("lane panel: the runs rendered (refreshCpuLane swallows errors)", [len(x.get("rows") or []) for x in o],
          [2, 2, 2, 2])
    rows = (o[0].get("rows") or [[], []]) + [[]]
    check("lane panel: a hold is in the summary line with its cause",
          str(o[0].get("sum")).endswith(" - DARKBLOOM SANDBOX HOLD: model jobs wait (ci-job-77)"), True)
    check("lane panel: a running row carries its phase in the state cell and the detail after its time",
          rows[0], ["running (deps)", "verify", "verify@x", "b1", "u1",
                    "12s - npm ci 3/10 - 3 model calls, 1 failed, 2 met a sandbox hold"])
    check("lane panel: a pending row says why it waits", rows[1],
          ["pending", "verify", "verify@y", "b2", "", "waiting 30s - " + held_t])
    check("lane panel: a cancelling job says so in the state cell", ((o[1].get("rows") or [[""]])[0] or [""])[0],
          "cancelling (deps)")
    check("lane panel: no hold, no banner", "DARKBLOOM" in str(o[2].get("sum")), False)
    check("lane panel: a hold without a cause", str(o[3].get("sum")).endswith(" - DARKBLOOM SANDBOX HOLD: model jobs wait"),
          True)


FULL_RUNNER = {"runner": "unraid-cpu-1", "seen_s_ago": 3.2, "claims": 9, "slots": 5, "busy": 2, "saturated": False,
               "lane_max": 2, "lane_busy": 1, "hold": {"lane": "low disk: 3.1 GB free on /work, lane jobs need 6.0 GB"},
               "disk": {"work_gb": 1.24, "work_free_gb": 3.14, "cache_free_gb": 410.26, "min_free_gb": 6.0,
                        "uv_gb": 3.07, "uv_max_gb": 10.0, "npm_gb": 0.44, "npm_max_gb": 4.0, "nm_gb": 1.2, "nm_max_gb": 6.0},
               "disk_s_ago": 12.4}
LANE_ONLY_WHY = "lane-only runner (CPU_RUNNER_ACCEPT=lane)"        # the agent's LANE_ONLY_HOLD: its standing hold "plain"
LANE_ONLY = {"runner": "lane-1", "seen_s_ago": 2, "slots": 2, "busy": 1, "lane_max": 2, "lane_busy": 1,
             "hold": {"plain": LANE_ONLY_WHY}}
LANE_ONLY_FULL = {"runner": "lane-2", "seen_s_ago": 2, "slots": 2, "busy": 2, "lane_max": 2, "lane_busy": 2,
                  "hold": {"lane": "lane-job limit: 2 of 2 running", "plain": LANE_ONLY_WHY}}
ALL_HELD = {"runner": "r9", "hold": {"all": "clearing the npm cache (over its cap)"}}
FULL_TEXT = ("unraid-cpu-1 - busy 2/5 - lane 1/2 - HOLD lane: low disk: 3.1 GB free on /work, lane jobs need 6.0 GB"
             " - disk: work 1.2 GB (3.1 GB free), cache 410.3 GB free, uv 3.1/10 GB, npm 0.4/4 GB, nm 1.2/6 GB"
             " - report 12s ago")
TEXT_CASES = [
    ("everything reported, a lane hold", FULL_RUNNER, FULL_TEXT),
    ("only an id", {"runner": "r2"}, "r2"),
    ("both holds", {"runner": "r3", "slots": 1, "busy": 1, "hold": {"all": "no space", "lane": "lane limit"}},
     "r3 - busy 1/1 - HOLD all: no space - HOLD lane: lane limit"),
    ("a lane-only runner's standing plain hold", LANE_ONLY,
     "lane-1 - busy 1/2 - lane 1/2 - HOLD plain: " + LANE_ONLY_WHY),
    ("a lane-only runner at its lane limit", LANE_ONLY_FULL,
     "lane-2 - busy 2/2 - lane 2/2 - HOLD lane: lane-job limit: 2 of 2 running - HOLD plain: " + LANE_ONLY_WHY),
    ("all three kinds, in the order all / lane / plain",
     {"runner": "r3b", "hold": {"plain": "p", "lane": "l", "all": "a"}}, "r3b - HOLD all: a - HOLD lane: l - HOLD plain: p"),
    ("lane_max 0 is shown (lane jobs off)", {"runner": "r4", "slots": 3, "lane_max": 0, "lane_busy": 0},
     "r4 - busy 0/3 - lane 0/0"),
    ("a partial disk report", {"runner": "r5", "disk": {"work_free_gb": 7.5, "uv_gb": 11.0, "nm_gb": 0, "nm_max_gb": 6}},
     "r5 - disk: work 7.5 GB free, uv 11 GB, nm 0/6 GB"),
    ("a runner that stopped polling", {"runner": "r6", "seen_s_ago": 340}, "r6 - last poll 340s ago"),
    ("a runner that polled 119 s ago is online", {"runner": "r6b", "seen_s_ago": 119}, "r6b"),
    ("junk types are left out",
     {"runner": "r7", "slots": "x", "lane_max": "2", "hold": "why", "disk": {"work_gb": "9", "uv_gb": None, "npm_gb": True},
      "disk_s_ago": "5", "seen_s_ago": "9999"}, "r7"),
    ("zero slots and empty reports", {"runner": "r8", "slots": 0, "busy": 0, "hold": {}, "disk": {}}, "r8"),
    ("nothing at all", None, "?"),
]


def runner_lines(code, html):
    """The runners' limits (2026-11-05): cpuRunnerText() and the #cpuRunners block that refreshCpuLane() fills."""
    for needle in ('id="cpuRunners"', "escapeHtml(cpuRunnerText(r))"):
        check(f"page wires {needle}", needle in html, True)
    got = node(code + "console.log(JSON.stringify(" + json.dumps([c for _, c, _ in TEXT_CASES]) + ".map(r => cpuRunnerText(r))));")
    got = got if isinstance(got, list) else [got] * len(TEXT_CASES)
    for i, (label, _, want) in enumerate(TEXT_CASES):
        check("cpuRunnerText: " + label, got[i], want)
    mr = re.search(r"async function refreshCpuLane\(\) \{.*?\n\}\n", html, re.S)
    check("refreshCpuLane present", bool(mr), True)
    if not mr:
        return
    evil = {"runner": "<img src=x onerror=alert(1)>", "slots": 2, "busy": 1,
            "hold": {"all": "<script>alert('hold')</script> & more", "plain": "<b>plain</b>"}}
    base = {"enabled": True, "counts": {"running": 0}, "queue_depth": 0, "model_hold": {"held": False, "cause": None},
            "running": [], "pending": [], "recent": [], "local_running": [], "why_local": {"n": 0, "groups": []}}
    cases = [dict(base, runners=[FULL_RUNNER, {"runner": "r2", "seen_s_ago": 400}]),
             dict(base, runners=[evil]),
             dict(base, runners=[{"runner": "only-one", "slots": 1, "busy": 0}]),
             dict(base, runners=[]), dict(base),
             dict(base, runners=[LANE_ONLY, LANE_ONLY_FULL, ALL_HELD])]
    out = node(code + mr.group(0)
               + "const laneBody = new El();"
               "global.document.querySelector = s => s === '#cpuLaneTable tbody' ? laneBody : (s === '#jobs tbody' ? jobsBody : null);"
               "let LANE = null;"
               "Object.defineProperty(globalThis, 'fetch', {value: async () => ({json: async () => LANE}), writable: true,"
               " configurable: true});"
               "const once = async d => { LANE = d; await refreshCpuLane(); return els.cpuRunners.innerHTML; };"
               "(async () => { const o = []; for (const d of " + json.dumps(cases) + ") o.push(await once(d));"
               " console.log(JSON.stringify(o)); })();")
    o = out if isinstance(out, list) and len(out) == len(cases) else [str(out)] * len(cases)
    check("lane panel: one line per runner, the held one marked, its text escaped by the page",
          o[0], '<div class="cpu-runner held">' + FULL_TEXT + '</div><div class="cpu-runner">r2 - last poll 400s ago</div>')
    check("lane panel: a runner's own id and hold text are HTML-escaped",
          (any(t in o[1] for t in ("<img", "<script>", "<b>")), "&lt;img src=x onerror=alert(1)>" in o[1],
           "&lt;script>alert('hold')&lt;/script> &amp; more" in o[1], "HOLD plain: &lt;b>plain&lt;/b>" in o[1]),
          (False, True, True, True))
    check("lane panel: a refresh replaces the earlier lines",
          o[2], '<div class="cpu-runner">only-one - busy 0/1</div>')
    check("lane panel: no runners (or a queue that sends none) leaves no lines", (o[3], o[4]), ("", ""))
    check("lane panel: a lane-only runner's standing plain hold shows its text but is not marked; a hold on lane jobs "
          "or on all jobs is",
          o[5], '<div class="cpu-runner">lane-1 - busy 1/2 - lane 1/2 - HOLD plain: ' + LANE_ONLY_WHY + '</div>'
          '<div class="cpu-runner held">lane-2 - busy 2/2 - lane 2/2 - HOLD lane: lane-job limit: 2 of 2 running'
          ' - HOLD plain: ' + LANE_ONLY_WHY + '</div>'
          '<div class="cpu-runner held">r9 - HOLD all: clearing the npm cache (over its cap)</div>')


def main():
    src = Path(sys.argv[sys.argv.index("--src") + 1]) if "--src" in sys.argv else DEFAULT_SRC
    if "--mutants" in sys.argv:
        text = src.read_text()
        bad = 0
        for name, old, new in MUTANTS:
            if old not in text:
                print(f"FAIL: mutant anchor missing: {name}"); bad += 1; continue
            tmp = src.parent / ".mutant-ollama-queue-api.py"
            tmp.write_text(text.replace(old, new, 1))
            try:
                r = subprocess.run([sys.executable, __file__, "--src", str(tmp)], capture_output=True, text=True)
            finally:
                tmp.unlink(missing_ok=True)
            red = r.returncode != 0 and "FAIL:" in r.stdout and "crashed" not in r.stdout
            print(("ok  " if red else "FAIL") + f": mutant goes red -- {name}")
            bad += 0 if red else 1
        return 1 if bad else 0
    try:
        run(src)
    except Exception as e:
        import traceback; traceback.print_exc(); FAILS.append(f"crashed: {e}")
    print(f"\n{len(FAILS)} failure(s)" if FAILS else "\nALL PASSED")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
