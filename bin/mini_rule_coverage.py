"""TASK.md's `## Rule coverage` section, shared by `mini-spec --lint` and mini-validate's V8 (stdlib only, no I/O).

Why: 27 of the first 35 sampled-review rejects (2026-11-03) were a rule TASK.md stated that no frozen-test assertion checked,
most often bad or missing input (None, empty, a wrong type, an absent key or file) or an error path. The local model's fix
passed every gate and still broke that rule. The section makes the spec writer list each rule with the frozen test that pins
it, and the check holds every line to a test the frozen file really defines.

Format: one bullet per rule (`-`, `*`, `+` or `1.`; indented lines continue it), naming the test in backticks, e.g.
`- a row without a date is skipped -> `test_skips_undated_rows``. Prose lines in the section are ignored. A test may pin several
rules. Python tests are `def test*` functions; node tests are test()/it() titles."""
import re

HEAD = re.compile(r"^##[ \t]+Rule coverage\b.*$", re.M)
NEXT_HEAD = re.compile(r"^#{1,2}[ \t]", re.M)
BULLET = re.compile(r"^[ \t]*(?:[-*+]|\d+[.)])[ \t]+(.*)$")
BAD_INPUT = re.compile(r"\b(?:missing|absent|without|lacks?|none|null|nil|undefined|nan|empty|blank|bad|invalid|malformed|unknown|"
                       r"wrong|not an?|non-\w+|negative|zero|unreadable|corrupt|truncated|oversized?|too (?:long|big|large|many)|"
                       r"no such|skip\w*|ignor\w*|reject\w*|refus\w*|fails?|raises?|throws?|\w*error|\w*exception)\b", re.I)
NO_SECTION = ("TASK has no `## Rule coverage` section: list each rule of Required change, bad and missing input included, "
              "with the frozen test that asserts it")


def test_names(text, test_file):
    """Names of the tests a frozen test file defines: python `def test*` functions (methods included); node test()/it() titles."""
    if str(test_file).endswith(".py"):
        return set(re.findall(r"^[ \t]*(?:async[ \t]+)?def[ \t]+(test\w*)[ \t]*\(", text or "", re.M))
    return {m.group(2) for m in re.finditer(r"(?<![\w.$])(?:test|it)(?:\.only|\.skip|\.todo)?\(\s*(['\"`])(.+?)\1", text or "")}


def rules(task):
    """The rule lines of task's `## Rule coverage` section (continuations joined), or None when it has no such section."""
    m = HEAD.search(task or "")
    if not m:
        return None
    body = task[m.end():]
    nxt = NEXT_HEAD.search(body)
    out = []
    for ln in (body[:nxt.start()] if nxt else body).splitlines():
        b = BULLET.match(ln)
        if b:
            out.append(b.group(1).strip())
        elif out and ln.strip() and ln[:1] in " \t":
            out[-1] += " " + ln.strip()
    return out


def cited(rule):
    """Backticked names in a rule line, with a `path::` prefix and a trailing `()` dropped."""
    return {re.sub(r"^.*::|\(\)$", "", x.strip()) for x in re.findall(r"`([^`\n]+)`", rule)}


def check(task, test_text, test_file):
    """(errors, warnings). No section: one warning (TASK files written before the section existed still validate). An empty
    section, or a rule line that names no test the file defines, is an error. No rule about bad or missing input or an error
    path is a warning."""
    rs = rules(task)
    if rs is None:
        return [], [NO_SECTION]
    if not rs:
        return ["`## Rule coverage` lists no rules"], []
    names = test_names(test_text, test_file)
    errs = [f"rule {i} names no test that {test_file} defines (end the line with the test's name in backticks): {r[:90]!r}"
            for i, r in enumerate(rs, 1) if not cited(r) & names]
    warns = [] if any(BAD_INPUT.search(r) for r in rs) else [
        "no rule in `## Rule coverage` is about bad or missing input or an error path (None, empty, a wrong type, malformed, an absent "
        "key or file)"]
    return errs, warns
