#!/bin/bash
# Real research/analysis questions with KNOWN answers, drawn from actual vault research
# this session and prior sessions -- not invented fixtures, same "real ground truth"
# principle as the f2p coding bench. Scored by hand (open-ended Q&A has no shell verifier).
cd "/Users/user/Desktop/GitHub Projects"
HOST="http://198.51.100.145:11434"
M="deepseek-r1:7b"
OUT=".research-bench/results.log"
mkdir -p .research-bench
echo "=== research bench: $M -- $(date) ===" > "$OUT"

ask() {
  local id="$1" q="$2"
  local CWD=".research-bench/$id"
  mkdir -p "$CWD"
  echo "--- $id ---" | tee -a "$OUT"
  python3 /Users/user/bin/ollama-worker-v7.py \
    --model "$M" --host "$HOST" --cwd "$CWD" \
    --task "$q" --max-iters 8 --num-ctx 32768 --temperature 0.3 \
    > ".research-bench/$id.log" 2>&1
  tail -5 ".research-bench/$id.log" | tee -a "$OUT"
}

ask "3080-vram" "Does the NVIDIA RTX 3080 exist in a 12GB VRAM configuration, or only 10GB? If a 12GB variant exists, when was it released and how does it differ from the original. Use web search to confirm -- do not answer from memory alone. State your final answer clearly."

ask "dflash" "What is DFlash (sometimes written dFlash or DFlash2)? Which lab published it, what conference/venue is it associated with, and is it directly compatible with Ollama for local LLM inference? Use web search. State your final answer clearly."

ask "rivian-session" "What is the typical session/token lifetime for Rivian's mobile app API before it requires re-authentication? Use web search to find community documentation on this (e.g. reverse-engineered API docs). State your final answer clearly."

ask "firewalla-msp" "Does Firewalla's MSP (managed service provider) tier require a paid subscription, and roughly what does it cost per year? Use web search. State your final answer clearly."

echo "done -> $OUT"
