#!/bin/bash
# Standard qwen dispatch wrapper: streams from Ollama's raw API (not the blocking
# ollama_chat MCP tool), tees live output to ~/qwen-dispatch.log for the
# qwen.example.com viewer, and writes a live tokens/sec figure to
# ~/qwen-rate.txt for the tmux status bar to display.
#
# Usage: qwen-dispatch.sh "<prompt text>" [model] [num_ctx] [temperature] [target_file] [history_file] [ollama_host]
# ollama_host defaults to Studio (127.0.0.1:11434). Pass "198.51.100.145:11434" for Unraid.
#
# If history_file is given: on first call (file doesn't exist yet), a system
# message is included instructing the model to STOP immediately and name
# exactly what it needs -- under a "## NEED:" marker -- rather than guess,
# if it realizes partway through that it's missing something. The full
# exchange (including this turn) is saved to history_file afterward.
#
# To continue a prior exchange (supply what it asked for and resume): call
# again with the SAME history_file and a new prompt like "Here is what you
# asked for: <answer>. Continue from where you left off." -- the script
# loads the prior turns from history_file and sends the full conversation,
# so the model picks up with everything it already produced.
#
# Prints the full accumulated response content on stdout as the last line,
# prefixed with FINAL_CONTENT: -- capture that if the caller needs the raw
# output to apply to a file. If the model invoked the stop-and-ask pattern,
# a line "NEEDS_INPUT: <what it asked for>" is printed before FINAL_CONTENT
# so a caller/orchestrator can detect it programmatically.
set -uo pipefail

PROMPT="$1"
# Coding default flipped 2026-09-02 (the owner approved directly; Fable SEARCH-DONE).
# Bake-off over 6 real tasks, every verify re-run by hand, all arms at 131072 ctx
# / pinned host / matched Q4_K_M / staged from a proven-failing baseline:
#   qwen3.8:27b-q4_K_M  6/6   <-- only model that solved automerge
#   qwen3.6:27b         5/6   ~half the tokens; the throughput alternate
#   qwen3-coder:30b     1/6   previous default, 11 months stale
# NOTE the quant changed too: this was q8_0 and the bake-off scored q4_K_M. Ship
# the artifact that was actually tested -- a different quant is different weights.
MODEL="${2:-qwen3.8:27b-q4_K_M}"
NUM_CTX="${3:-8192}"
TEMP="${4:-0}"
TARGET_FILE="${5:-response}"
HISTORY_FILE="${6:-}"
OLLAMA_HOST="${7:-127.0.0.1:11434}"

LOG="$HOME/qwen-dispatch.log"
RATE="$HOME/qwen-rate.txt"
REQ_BODY="$(mktemp -t qwen-request)"
trap 'rm -f "$REQ_BODY"' EXIT

STOP_AND_ASK_INSTRUCTION='If, partway through this task, you realize you are missing something you genuinely need to do it well (a piece of reference material, a missing detail, an ambiguous requirement) -- STOP IMMEDIATELY. Do not guess, do not proceed, do not produce a partial or best-effort answer. Instead, output ONLY a section starting with the exact marker "## NEED:" followed by a precise description of exactly what you need and why, then stop. If you have everything you need, proceed normally and do not include a "## NEED:" section.'

# Build the request body (messages array, with optional prior history)
python3 -c "
import json, sys, os

model = sys.argv[1]
num_ctx = int(sys.argv[2])
temp = float(sys.argv[3])
prompt = sys.argv[4]
history_file = sys.argv[5]
stop_and_ask = sys.argv[6]

messages = []
is_continuation = False
if history_file and os.path.exists(history_file):
    with open(history_file) as f:
        messages = json.load(f)
    is_continuation = True

if not is_continuation:
    messages.append({'role': 'system', 'content': stop_and_ask})

messages.append({'role': 'user', 'content': prompt})

body = {
    'model': model,
    'messages': messages,
    'stream': True,
    'options': {'num_ctx': num_ctx, 'temperature': temp},
}
with open(sys.argv[7], 'w') as f:
    json.dump(body, f)
" "$MODEL" "$NUM_CTX" "$TEMP" "$PROMPT" "$HISTORY_FILE" "$STOP_AND_ASK_INSTRUCTION" "$REQ_BODY"

# ANSI: bold-cyan divider line, bold-white header text, reset after.
{
  echo ""
  printf '\033[1;36m════════════════════════════════════════════════════════════\033[0m\n'
  printf '\033[1;37m▶ NEW DISPATCH %s — %s%s\033[0m\n' "$(date '+%H:%M:%S')" "$MODEL" "$([ -n "$HISTORY_FILE" ] && [ -f "$HISTORY_FILE" ] && echo ' (continuation)')"
  printf '\033[1;36m════════════════════════════════════════════════════════════\033[0m\n'
} >> "$LOG"

curl -s -N "http://$OLLAMA_HOST/api/chat" --data-binary "@$REQ_BODY" | TARGET_FILE="$TARGET_FILE" HISTORY_FILE="$HISTORY_FILE" REQ_BODY="$REQ_BODY" python3 -u -c "
import sys, json, time, os

target_file = os.environ.get('TARGET_FILE', 'response')

start = time.time()
last_rate_write = 0
last_status_write = 0
token_count = 0
think_chars = 0
think_buf = ''
content_parts = []
phase = None  # None -> 'thinking' -> 'writing'
seen_checkpoints = set()

def snippet(text, n=100):
    text = ' '.join(text.split())  # collapse newlines/whitespace
    if len(text) <= n:
        return text
    tail = text[-n:]
    # Prefer cutting at the start of the last complete sentence within the
    # tail window, so the snippet reads as a real thought instead of a
    # mid-word fragment.
    for sep in ('. ', '? ', '! '):
        idx = tail.rfind(sep)
        if idx != -1 and idx < len(tail) - 15:  # don't cut right at the end
            return tail[idx + len(sep):]
    return tail

def ts():
    return time.strftime('%H:%M:%S')

import re
_bold_re = re.compile(r'\*\*([^*]{4,80})\*\*')
_numbered_re = re.compile(r'(?:^|\n)\s*(?:\d+[.):]|Step \d+)\s*([A-Z][^\n.]{4,80})', re.MULTILINE)
_checklist_re = re.compile(r'(?:^|\n)\s*-\s*([A-Z][a-zA-Z ]{2,30}):\s*[^\n]{0,60}?(✓|✗|\bMet\b|\bmatch(?:es)?\b)', re.MULTILINE)

def find_new_checkpoints(text, seen, margin=20):
    \"\"\"Pull out bold headers / numbered-step / checklist markers as they
    appear, so the live view shows real structure instead of an arbitrary
    rolling text window. margin: only accept a match that ends at least
    this many chars before the end of text -- otherwise, on a streaming
    buffer, a still-growing partial line matches a slightly-longer version
    of itself on every token and spams one line per token.\"\"\"
    found = []
    limit = len(text) - margin
    for pattern, build in (
        (_bold_re, lambda m: m.group(1).strip()),
        (_numbered_re, lambda m: m.group(1).strip()),
        (_checklist_re, lambda m: f'{m.group(1).strip()}: {m.group(2)}'),
    ):
        for m in pattern.finditer(text):
            if m.end() > limit:
                continue  # too close to the live edge, may still be growing
            c = build(m)
            if c not in seen:
                seen.add(c)
                found.append(c)
    return found

DIM = '\033[2m'
YELLOW = '\033[1;33m'
CYAN = '\033[1;36m'
GREEN = '\033[1;32m'
RED = '\033[1;31m'
RESET = '\033[0m'

def emit(line):
    # Color by line shape: checkpoints (structure found) pop in yellow,
    # phase-transition lines (thinking.../writing.../done) pop in cyan,
    # generic heartbeat filler stays dim so it doesn't compete for attention.
    if line.startswith('[qwen] thinking about:') or line.startswith('[qwen]   ...writing'):
        colored = f'{YELLOW}{line}{RESET}'
    elif line.startswith('[qwen] done'):
        colored = f'{GREEN}{line}{RESET}'
    elif line.startswith('[qwen] NEEDS INPUT'):
        colored = f'{RED}{line}{RESET}'
    elif '   ...' in line:
        colored = f'{DIM}{line}{RESET}'
    else:
        colored = f'{CYAN}{line}{RESET}'
    print(f'{DIM}[{ts()}]{RESET} {colored}')
    sys.stdout.flush()

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        d = json.loads(line)
    except Exception:
        continue
    msg = d.get('message', {})
    think = msg.get('thinking', '')
    content = msg.get('content', '')

    if think:
        if phase != 'thinking':
            emit('[qwen] thinking...')
            phase = 'thinking'
        think_chars += len(think)
        think_buf += think
        token_count += max(1, len(think) // 4)
        for cp in find_new_checkpoints(think_buf, seen_checkpoints):
            topic = cp if len(cp) <= 45 else cp[:42].rsplit(' ', 1)[0] + '...'
            emit(f'[qwen] thinking about: {topic}')
            last_status_write = time.time()

    if content:
        if phase != 'writing':
            emit(f'[qwen] thought for ~{think_chars} chars, now writing {target_file}...' if think_chars else f'[qwen] writing {target_file}...')
            phase = 'writing'
        content_parts.append(content)
        token_count += max(1, len(content) // 4)

    now = time.time()
    if phase == 'thinking' and now - last_status_write > 8:
        # Fallback for stretches with no bold/numbered structure to latch onto.
        emit(f'[qwen]   ...still thinking: \"{snippet(think_buf)}\"')
        last_status_write = now
    elif phase == 'writing' and now - last_status_write > 2:
        cur_lines = ''.join(content_parts).count(chr(10)) + 1
        emit(f'[qwen]   ...writing {target_file}: line {cur_lines}')
        last_status_write = now

    if now - last_rate_write > 0.5:
        elapsed = now - start
        rate = token_count / elapsed if elapsed > 0 else 0
        with open('$RATE', 'w') as f:
            f.write(f'qwen3.8: {rate:.1f} tok/s (est)')
        last_rate_write = now

    if d.get('done'):
        eval_count = d.get('eval_count')
        eval_duration = d.get('eval_duration')
        if eval_count and eval_duration:
            real_rate = eval_count / (eval_duration / 1e9)
            with open('$RATE', 'w') as f:
                f.write(f'qwen3.8: {real_rate:.1f} tok/s (last run, done)')

full_content = ''.join(content_parts)
n_lines = full_content.count(chr(10)) + 1 if full_content else 0
elapsed = time.time() - start

needs_input = None
if '## NEED:' in full_content:
    needs_input = full_content.split('## NEED:', 1)[1].strip()
    emit(f'[qwen] NEEDS INPUT: {needs_input[:80]}')

emit(f'[qwen] done — wrote {n_lines} lines ({len(full_content)} chars) to {target_file} in {elapsed:.1f}s')
print(f'\033[1;32m┌─ RESULT ' + '─' * 53 + f'\033[0m')
for l in full_content.split(chr(10)):
    print(f'\033[1;32m│\033[0m {l}')
print(f'\033[1;32m└' + '─' * 63 + f'\033[0m')

if needs_input:
    print('NEEDS_INPUT:' + needs_input)

print('FINAL_CONTENT:' + full_content)

# Save updated history (prior turns + this exchange) if history tracking is on
history_file = os.environ.get('HISTORY_FILE', '')
if history_file:
    messages = []
    if os.path.exists(history_file):
        with open(history_file) as f:
            messages = json.load(f)
    else:
        with open(os.environ['REQ_BODY']) as f:
            req = json.load(f)
        messages = req['messages'][:-1]  # everything except the user turn we're about to re-add
    with open(os.environ['REQ_BODY']) as f:
        req = json.load(f)
    user_turn = req['messages'][-1]
    messages.append(user_turn)
    messages.append({'role': 'assistant', 'content': full_content})
    with open(history_file, 'w') as f:
        json.dump(messages, f)
" | tee -a "$LOG"

# Spillover check -- only meaningful on a real VRAM-limited host (i.e. not the
# default Studio unified-memory host), and only worth doing per-dispatch here
# since ~/bin/unraid-spillover-watch.sh already covers continuous/background
# monitoring independent of this script.
if [ "$OLLAMA_HOST" != "127.0.0.1:11434" ]; then
  SPILL_CHECK=$(curl -s -m 5 "http://$OLLAMA_HOST/api/ps" 2>/dev/null | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print('SPILLOVER CHECK FAILED: could not reach $OLLAMA_HOST/api/ps')
    sys.exit(0)
for m in d.get('models', []):
    if m.get('name') == '$MODEL':
        size = m.get('size', 0)
        vram = m.get('size_vram', 0)
        if size != vram:
            spilled = (size - vram) / (1024**3)
            print(f'SPILLOVER DETECTED on $OLLAMA_HOST: {spilled:.2f}GB of \'$MODEL\' is off-GPU (size={size}, size_vram={vram}) -- results from this dispatch may be slow/unreliable')
        else:
            print(f'no spillover: $MODEL fully on GPU ({vram/(1024**3):.1f}GB)')
        break
else:
    print(f'SPILLOVER CHECK: \$MODEL not found in ollama_ps on $OLLAMA_HOST (already unloaded?)')
" 2>&1)
  {
    printf '\033[1;35m[spillover check] %s\033[0m\n' "$SPILL_CHECK"
  } >> "$LOG"
  echo "$SPILL_CHECK" | grep -q "SPILLOVER DETECTED" && printf '\033[1;31m⚠ %s\033[0m\n' "$SPILL_CHECK"
fi
