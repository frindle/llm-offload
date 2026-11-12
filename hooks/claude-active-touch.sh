#!/bin/sh
# Claude activity beacon for bloom_control session-aware set (UserPromptSubmit + PreToolUse).
# Touches ~/.ollama-dispatch/claude-active; never blocks, never prints.
mkdir -p "$HOME/.ollama-dispatch" 2>/dev/null
touch "$HOME/.ollama-dispatch/claude-active" 2>/dev/null
exit 0
