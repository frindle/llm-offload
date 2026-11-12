#!/usr/bin/env python3
"""
verify_claims.py - A general-purpose, reusable verifier for claims about code.

Verifies claims (bugs, fixes, improvements) against source files using:
1. Mechanical citation checks (line number validation, quote substring search)
2. Context windowing around cited lines (-50 to +50)
3. One-at-a-time model verification with JSON Schema format enforcement
4. Fault-injection test mode for harness-level testing

Usage:
    python3 verify_claims.py --claims claims.json --source dedup_subsystem.py \
        --model qwen3.5:9b --host http://localhost:11434 [--test-harness-validation]
"""

import argparse
import json
import os
import re
import requests
from typing import Any, Dict, List, Tuple, Optional


def check_claim_mechanically(claim: Dict[str, Any], source_lines: List[str]) -> Tuple[bool, str]:
    """
    Perform mechanical citation checks on a claim.

    Args:
        claim: Claim dict with 'line', 'quote' keys (and others)
        source_lines: Full list of lines from the source file

    Returns:
        (success, reason): success=True if passes all checks; 
                          success=False with reason string for failure reasons.
    
    Checks performed:
    - FAIL if claim["line"] is not a valid line number (< 1 or > len(source_lines))
    - Normalize before comparing: strip leading/trailing whitespace from quote and each candidate line
    - FAIL if normalized quote is under 8 characters
    - FAIL if normalized quote does not appear as substring in any line within +/-3 lines of cited line
    """
    
    # Check 1: Line number validity
    try:
        claimed_line = int(claim["line"])
    except (ValueError, TypeError):
        return False, f"Invalid line number format: {claim.get('line', 'missing')}"
    
    if claimed_line < 1 or claimed_line > len(source_lines):
        return False, f"Cited line {claimed_line} is out of range [1-{len(source_lines)}]"
    
    # Check 2: Quote length after normalization (must be >= 8 chars)
    normalized_quote = claim["quote"].strip() if isinstance(claim.get("quote"), str) else ""
    if len(normalized_quote) < 8:
        return False, f"Normalized quote too short ({len(normalized_quote)} chars), requires minimum 8 characters"
    
    # Check 3: Substring search within +/-3 lines of cited line
    window_start = max(0, claimed_line - 4)  # Lines are 1-indexed in claim, but we'll use 0-index internally
    window_end = min(len(source_lines), claimed_line + 3)  # inclusive
    
    found_in_window = False
    for i in range(window_start, window_end):
        line_content = source_lines[i] if isinstance(source_lines[i], str) else ""
        normalized_line = line_content.strip()
        
        # Check if normalized quote appears as substring in this line (after stripping whitespace from both)
        if normalized_quote in normalized_line:
            found_in_window = True
            break
    
    if not found_in_window:
        return False, f"Normalized quote does not appear within +/-3 lines of cited line {claimed_line}"
    
    # All checks passed
    return True, ""


def extract_context_window(source_lines: List[str], claimed_line: int) -> Tuple[List[str], str]:
    """
    Extract a window of source text around the cited claim.

    Args:
        source_lines: Full list of lines from source file (0-indexed internally)
        claimed_line: The line number cited in the claim (1-indexed as per JSON schema)

    Returns:
        Tuple of (window_text, context_info_string):
            - window_text: List of actual line strings for this window
            - context_info_string: Human-readable description like "lines 184-234 of a 680-line file"
    
    Window is from (cited_line - 50) to (cited_line + 50), clamped to bounds.
    """
    # Convert claimed line to 0-indexed, then apply windowing
    cited_idx = claimed_line - 1
    
    start_idx = max(0, cited_idx - 50)
    end_idx = min(len(source_lines), cited_idx + 51)  # inclusive on right side for slicing logic
    
    window_lines = source_lines[start_idx:end_idx]
    
    actual_start_1idx = start_idx + 1
    actual_end_1idx = len(window_lines) if not window_lines else end_idx
    
    context_info = f"lines {actual_start_1idx}-{min(actual_end_1idx, len(source_lines))} of a {len(source_lines)}-line file, shown below:"
    
    return window_lines, actual_start_1idx, context_info


def build_model_prompt(window_text: List[str], window_start_line: int, claimed_line: int,
                       claim_quote: str, claim_description: str) -> str:
    """
    Build the prompt for model verification.

    Args:
        window_text: The actual lines from source file (0-indexed list of strings)
        window_start_line: Actual 1-indexed starting line number of this window (from
            extract_context_window -- may be clamped near the start of the file, so this
            must NOT be recomputed as claimed_line - 50)
        claimed_line: Original 1-indexed line number cited in claim
        claim_quote: The quote string from the claim
        claim_description: The description field from the claim

    Returns:
        Formatted prompt text for the model.
    """

    # Add line numbers to window lines (using actual file line numbers)
    numbered_lines = []
    start_line_num = window_start_line

    for i, line in enumerate(window_text):
        actual_file_line = start_line_num + i + 1
        numbered_lines.append(f"{actual_file_line}: {line}")
    
    window_display = "\n".join(numbered_lines)
    
    prompt = f"""You are given a code snippet and a claim about it.

WINDOWED SOURCE CODE (with line numbers):
{window_display}

CLAIM:
- Cited Line Number: {claimed_line}
- Quote from Code: "{claim_quote}"
- Description/Assertion: "{claim_description}"

TASK:
Determine whether the description is correct about what the code at that location does.

Respond with a JSON object containing exactly these three fields in this order:
1. "quote": A string quoting relevant text from the windowed source (must be an actual substring present)
2. "justification": Your reasoning for why you made your judgment, referencing specific lines if helpful
3. "verdict": One of exactly ["confirmed", "discarded", "unverifiable"]

- Use "confirmed" if the description accurately describes what's in the code at that location
- Use "discarded" if the description is incorrect about the code
- Use "unverifiable" if you cannot determine truth from this window alone (e.g., relevant context not shown)

IMPORTANT: Your quote must be an actual substring present in the windowed source text above.
"""
    
    return prompt


def parse_model_response(response_content: str, schema_format: Dict[str, Any]) -> Tuple[bool, Optional[Dict], str]:
    """
    Parse model response and validate against expected format.

    Args:
        response_content: The raw string content from the model's /api/chat response (json.loads() needed)
        schema_format: JSON Schema object defining required fields and constraints

    Returns:
        Tuple of (success, parsed_dict_or_none, error_reason):
            - success=True if parsing/validation passed
            - success=False with reason for failure
    
    Validation checks:
    - Must be valid JSON after json.loads()
    - Must have exactly three keys in order: quote, justification, verdict
    - "verdict" must be one of ["confirmed", "discarded", "unverifiable"]
    """
    
    # Try to parse as JSON
    try:
        parsed = json.loads(response_content)
    except json.JSONDecodeError as e:
        return False, None, f"JSON parsing failed: {e}"
    
    if not isinstance(parsed, dict):
        return False, None, "Response is not a JSON object (dict)"
    
    # Check for exactly three keys in the expected order
    required_keys = ["quote", "justification", "verdict"]
    
    actual_keys = list(parsed.keys())
    
    if len(actual_keys) != 3:
        return False, None, f"Expected exactly 3 fields (quote, justification, verdict), got {len(actual_keys)} keys: {actual_keys}"
    
    # Check field order matches required order
    for i, expected_key in enumerate(required_keys):
        if actual_keys[i] != expected_key:
            return False, None, f"Field order incorrect at position {i}: expected '{expected_key}', got '{actual_keys[i]}'"
    
    quote = parsed["quote"]
    justification = parsed["justification"]
    verdict = parsed["verdict"]
    
    # Validate verdict is one of the allowed values
    if verdict not in ["confirmed", "discarded", "unverifiable"]:
        return False, None, f"Invalid verdict value: '{verdict}'. Must be exactly 'confirmed', 'discarded', or 'unverifiable'"
    
    # All validations passed - parse is successful
    result_dict = {
        "quote": quote,
        "justification": justification, 
        "verdict": verdict
    }
    
    return True, result_dict, ""


def verify_claim_with_model(claim: Dict[str, Any], window_text: List[str], 
                           window_start_line: int, model: str, host: str) -> Dict[str, Any]:
    """
    Verify a single claim using the LLM with one API call.

    Args:
        claim: The claim dict to verify (with id, type, line, quote, description)
        window_text: List of actual source lines for this window
        window_start_line: Starting 1-indexed line number of the window
        model: Model name string (e.g., "qwen3.5:9b")
        host: API endpoint URL

    Returns:
        Dict with keys: id, result, quote, justification, verdict (if applicable), reason
    
    The function makes exactly ONE fresh /api/chat call per claim. If validation fails,
    it retries ONCE then marks as HARNESS-REJECTED if still failing.
    
    Validation failures trigger retry/reject path:
    - JSON parsing failure -> retry once -> reject on second fail
    - Missing required field -> same path
    - Invalid verdict value -> same path  
    - Echoed-quote grounding check fails (model's quote not in window) -> reject
    
    HARNESS-REJECTED reasons are specific about WHICH check failed.
    """
    
    # Build the prompt for this claim only (fresh conversation, no shared history)
    model_prompt = build_model_prompt(window_text, window_start_line, claim["line"],
                                      claim.get("quote", ""),
                                      claim.get("description", ""))
    
    # Prepare request payload with JSON Schema format constraint
    json_schema_format = {
        "type": "object",
        "properties": {
            "quote": {"type": "string"},
            "justification": {"type": "string"}, 
            "verdict": {"type": "string", "enum": ["confirmed", "discarded", "unverifiable"]}
        },
        "required": ["quote", "justification", "verdict"],
        "additionalProperties": False
    }
    
    request_payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You are a code analysis assistant. Analyze the provided windowed source and claim."},
            {"role": "user", "content": model_prompt}
        ],
        "format": json_schema_format,
        "stream": False,
        "options": {
            "temperature": 0,
            # 2026-08-29, per Fable's diagnosis: thinking traces on real (non-fixture) claims
            # showed genuine forward-progress reasoning cut short, not repetition loops -- a
            # bigger budget is expected to let those actually reach a conclusion at zero safety
            # cost (thinking stays ON; this does not touch the disable-thinking path, which was
            # shown to produce confidently wrong verdicts and stays permanently off).
            "num_predict": 8192,
        }
    }
    
    # Make the API call (using requests library)
    try:
        response = requests.post(
            host + "/api/chat",
            json=request_payload,
            timeout=120
        )
        
        if response.status_code != 200:
            return {
                "id": claim["id"],
                "result": "HARNESS-REJECTED", 
                "reason": f"API request failed with status {response.status_code}: {response.text[:500]}"
            }
        
        response_content = response.json()
        
    except requests.exceptions.RequestException as e:
        return {
            "id": claim["id"],
            "result": "HARNESS-REJECTED", 
            "reason": f"Network error during API call: {e}"
        }
    
    # Fable's ruling 2026-08-29: thinking stays enabled by default (see design notes at top
    # of file) -- a claim that exhausts its budget on internal reasoning without ever emitting
    # an answer must fail SAFE (rejected, escalate to a human/Claude reviewer) rather than being
    # retried with thinking disabled, which was empirically shown to produce confidently WRONG
    # verdicts on exactly the hardest claims. This helper distinguishes that specific failure
    # (empty content, substantial thinking trace present -- the model was reasoning, it just
    # never finished) from other malformed-output cases (bad JSON, wrong field, etc.), and
    # captures the thinking trace itself so a human can tell whether it was making real forward
    # progress (worth a bigger num_predict next time) or stuck in a loop (no budget will fix it).
    def _diagnose_response(raw_message):
        content = raw_message.get("content", "")
        thinking = raw_message.get("thinking") or raw_message.get("reasoning_content") or ""
        return content, thinking

    # Parse and validate the response (first attempt)
    msg1 = response_content.get("message", {})
    content1, thinking1 = _diagnose_response(msg1)
    parse_success, parsed_data, parse_error = parse_model_response(content1, json_schema_format)
    runaway_thinking = (not content1.strip()) and len(thinking1) > 200

    if not parse_success:
        # First validation failure - retry once with same request
        try:
            response_retry = requests.post(
                host + "/api/chat",
                json=request_payload,  # Same payload for retry
                timeout=120
            )

            if response_retry.status_code != 200:
                return {
                    "id": claim["id"],
                    "result": "HARNESS-REJECTED",
                    "reason": f"Retry API request failed with status {response_retry.status_code}"
                }

            retry_content = response_retry.json()
            msg2 = retry_content.get("message", {})
            content2, thinking2 = _diagnose_response(msg2)
            parse_success, parsed_data, parse_error = parse_model_response(content2, json_schema_format)
            runaway_thinking = runaway_thinking or ((not content2.strip()) and len(thinking2) > 200)
            thinking1 = thinking2 or thinking1  # keep whichever attempt actually reasoned

        except requests.exceptions.RequestException as e:
            return {
                "id": claim["id"],
                "result": "HARNESS-REJECTED",
                "reason": f"Retry network error: {e}"
            }

    if not parse_success:
        # Second validation failure -- REJECTED-RUNAWAY-THINKING is a distinct, more specific
        # label than plain HARNESS-REJECTED when the evidence points at exhausted reasoning
        # budget rather than a malformed/wrong-shape answer. Per Fable: route either kind to
        # escalation (human/Claude review), never silently drop or retry non-thinking.
        if runaway_thinking:
            return {
                "id": claim["id"],
                "result": "REJECTED-RUNAWAY-THINKING",
                "reason": (f"Model produced no answer after {len(thinking1)} chars of internal "
                           f"reasoning across 2 attempts -- exhausted generation budget on "
                           f"thinking, never emitted the final JSON. Escalate to a human/Claude "
                           f"reviewer, do not retry with thinking disabled (empirically produces "
                           f"confidently wrong verdicts on hard claims)."),
                "thinking_trace": thinking1[:4000],
            }
        return {
            "id": claim["id"], 
            "result": "HARNESS-REJECTED",
            "reason": f"Malformed model output after 1 retry. Check failed: {parse_error}"
        }
    
    # Parse succeeded - now perform echoed-quote grounding check
    model_quote = parsed_data.get("quote", "")
    normalized_model_quote = model_quote.strip() if isinstance(model_quote, str) else ""
    
    window_text_str = "\n".join(window_text).strip()
    
    # Check if the model's quote appears as a substring in the window text (normalized comparison)
    if not normalized_model_quote:
        return {
            "id": claim["id"], 
            "result": "HARNESS-REJECTED",
            "reason": f"Echoed-quote grounding check failed: model returned empty string for quote field"
        }
    
    # Check against each line in the window (normalized comparison) -- catches single-line quotes.
    found_in_window = False
    for i, line in enumerate(window_text):
        if isinstance(line, str):
            normalized_line = line.strip()
            if normalized_model_quote in normalized_line:
                found_in_window = True
                break

    # Fallback for multi-line quotes: collapse ALL internal whitespace (not just leading/
    # trailing) to single spaces on both sides before comparing, so a quote spanning multiple
    # source lines matches regardless of exact newline/indentation reproduction -- a quote like
    # "else:\n    log.info(...)" must match even though the model won't reproduce the source's
    # exact original indentation on continuation lines. No length gate: multi-line quotes are
    # exactly the case this fallback exists for, arbitrarily excluding longer ones defeats it.
    if not found_in_window:
        collapse = lambda s: " ".join(s.split())
        collapsed_quote = collapse(normalized_model_quote)
        collapsed_window = collapse("\n".join(window_text))
        if collapsed_quote and collapsed_quote in collapsed_window:
            found_in_window = True
    
    if not found_in_window:
        return {
            "id": claim["id"], 
            "result": "HARNESS-REJECTED",
            "reason": f"Echoed-quote grounding check failed: model's quote '{model_quote[:50]}...' does not appear in window text (lines {window_start_line}-{window_start_line + len(window_text) - 1})"
        }
    
    # All checks passed - return the parsed result with verdict included
    return {
        "id": claim["id"], 
        "result": parsed_data.get("verdict"),
        "quote": model_quote,
        "justification": parsed_data.get("justification", ""),
        "verdict": parsed_data.get("verdict")  # Include for completeness
    }


def run_test_harness_validation():
    """
    Run fault-injection test mode against five canned fake responses.

    Tests:
    (a) missing "verdict" key
    (b) verdict value outside the enum (e.g., "maybe")
    (c) truncated/invalid JSON
    (d) empty string content  
    (e) syntactically valid response whose quote field is NOT in sample window text

    For each, prints whether it correctly triggered one retry then HARNESS-REJECTED.
    
    No live API calls are made - uses canned responses directly.
    """
    
    print("=" * 70)
    print("FAULT-INJECTION TEST MODE")
    print("Testing harness-level validation logic against canned fake responses")
    print("=" * 70)
    print()
    
    # Sample window text for testing (a small snippet to use in the grounding check)
    sample_window = [
        "1: def process_data(data):",
        "2:     if data is None:",
        "3:         return []",
        "4:     result = []",
        "5:     for item in data:",
        "6:         result.append(item * 2)",
        "7:     return result",
    ]

    # Placeholder schema -- parse_model_response's current logic doesn't branch on its
    # contents, it only checks the parsed JSON's own shape (field count/order, verdict enum).
    schema_format = {
        "type": "object",
        "properties": {
            "quote": {"type": "string"},
            "justification": {"type": "string"},
            "verdict": {"type": "string", "enum": ["confirmed", "discarded", "unverifiable"]},
        },
        "required": ["quote", "justification", "verdict"],
        "additionalProperties": False,
    }

    test_cases = [
        {
            "name": "(a) missing verdict key",
            "response_content": '{"quote":"def process_data(data):","justification":"The function is defined here."}',
        },
        {
            "name": "(b) invalid verdict value",
            "response_content": '{"quote":"for item in data:","justification":"This loops through items.","verdict":"maybe"}',
        },
        {
            "name": "(c) truncated/invalid JSON",
            "response_content": '{"quote":"def process_data(data):","justification":"The function is defined here.",',
        },
        {
            "name": "(d) empty string content",
            "response_content": '',
        },
        {
            "name": "(e) quote not in window text",
            "response_content": '{"quote":"NONEXISTENT_FUNCTION_CALL_HERE","justification":"This function is called somewhere.","verdict":"confirmed"}',
        },
    ]

    for test_case in test_cases:
        print(f"\n--- Testing: {test_case['name']} ---")
        print(f"Input: {test_case['response_content'][:80]}" if test_case['response_content'] else "Input: (empty)")

        # Call the REAL validation function, exactly as verify_claim_with_model does,
        # including its own retry-on-failure behavior (same canned response both times --
        # this is testing the retry-then-reject STATE MACHINE, not real model recovery).
        success, parsed_data, reason = parse_model_response(test_case["response_content"], schema_format)
        if not success:
            print(f"First attempt: REJECTED ({reason})")
            success, parsed_data, reason = parse_model_response(test_case["response_content"], schema_format)
            if not success:
                print(f"Retry: REJECTED ({reason}) -> HARNESS-REJECTED")
            else:
                print(f"Retry: unexpectedly succeeded -> {parsed_data}")
        else:
            print(f"First attempt: PASSED -> {parsed_data}")
            # Case (e) needs the echoed-quote grounding check too, which lives in
            # verify_claim_with_model rather than parse_model_response -- replicate just
            # that one check here against the real sample_window, since it's the one
            # piece of validation this function doesn't otherwise exercise.
            model_quote = (parsed_data.get("quote") or "").strip()
            grounded = any(model_quote in line.strip() for line in sample_window)
            if not grounded:
                print(f"Echoed-quote grounding check: FAILED (quote not in window) -> HARNESS-REJECTED")
            else:
                print(f"Echoed-quote grounding check: PASSED")

    print()
    print("=" * 70)
    print("FAULT-INJECTION TEST MODE COMPLETE")
    print("=" * 70)


def main():
    """Main entry point for verify_claims.py."""
    
    parser = argparse.ArgumentParser(
        description="Verify claims about code using mechanical checks and LLM verification"
    )
    
    parser.add_argument("--claims", help="Path to JSON file with claim objects (required unless --test-harness-validation)")
    parser.add_argument("--source", help="Path to source Python file (required unless --test-harness-validation)")
    parser.add_argument("--model", default="qwen3.5:9b", help="Model name for API calls (default: qwen3.5:9b)")
    parser.add_argument("--host", default="http://localhost:11434", 
                       help="API endpoint URL (default: http://localhost:11434)")
    parser.add_argument("--test-harness-validation", action="store_true",
                       help="Run fault-injection test mode against canned fake responses")
    
    args = parser.parse_args()
    
    # Handle test harness validation mode first (no live API calls)
    if args.test_harness_validation:
        run_test_harness_validation()
        return

    if not args.claims or not args.source:
        parser.error("--claims and --source are required unless --test-harness-validation is given")

    # Load claims from JSON file
    with open(args.claims, "r", encoding="utf-8") as f:
        claims = json.load(f)
    
    # Read source file lines (keeping original line endings for accurate comparison)
    try:
        with open(args.source, "r", encoding="utf-8") as sf:
            source_content = sf.read()
        
        # Split into lines preserving content but stripping trailing newline from each
        if source_content.endswith('\n'):
            source_lines = source_content[:-1].splitlines(keepends=True)  # Keep line endings for accurate comparison
        else:
            source_lines = source_content.splitlines(True)
            
    except FileNotFoundError:
        print(f"ERROR: Source file not found: {args.source}")
        return
    
    if len(source_lines) == 0:
        print("WARNING: Source file is empty")
    
    # Process each claim in order
    results = []
    
    for idx, claim in enumerate(claims):
        claim_id = claim.get("id", f"claim-{idx}")
        
        print(f"\n{'='*60}")
        print(f"Processing claim {idx + 1}/{len(claims)}: id={claim_id}, line={claim.get('line')}")
        print('=' * 60)
        
        # Step 1: Mechanical citation check (no model call yet)
        mechanical_success, mechanical_reason = check_claim_mechanically(claim, source_lines)
        
        if not mechanical_success:
            result_entry = {
                "id": claim_id, 
                "result": "MECHANICAL-REJECT", 
                "reason": mechanical_reason
            }
            print(f"  Mechanical Check: FAILED - {mechanical_reason}")
            results.append(result_entry)
        else:
            # Step 2 & 3: Extract window and perform model verification
            claimed_line = int(claim["line"])
            
            try:
                window_lines, start_line_num, context_info = extract_context_window(source_lines, claimed_line)
                
                print(f"  Context Window: {context_info}")
                
                # Verify with model (one-at-a-time, fresh conversation per claim)
                verification_result = verify_claim_with_model(
                    claim=claim, 
                    window_text=window_lines, 
                    window_start_line=start_line_num,
                    model=args.model, 
                    host=args.host
                )
                
                # Extract the verdict/result from verification_result
                result_entry = {
                    "id": claim_id,
                    "result": verification_result.get("verdict", verification_result["result"]),
                    "quote": verification_result.get("quote", ""),
                    "justification": verification_result.get("justification", "")
                }
                
                print(f"  Model Verdict: {verification_result['result']}")
                if not mechanical_success or verification_result.get('result') in (
                        'HARNESS-REJECTED', 'REJECTED-RUNAWAY-THINKING'):
                    result_entry["reason"] = verification_result.get("reason", "")
                if verification_result.get("thinking_trace"):
                    result_entry["thinking_trace"] = verification_result["thinking_trace"]

                results.append(result_entry)

            except Exception as e:
                # Catch any unexpected errors during model verification
                print(f"  ERROR during model verification: {e}")
                results.append({"id": claim_id, "result": "ERROR", "reason": str(e)})

    # Write results to JSON file
    with open("results.json", "w", encoding="utf-8") as rf:
        json.dump(results, rf, indent=2)
    
    print()
    print("=" * 60)
    print(f"VERIFICATION COMPLETE")
    print(f"Total claims processed: {len(claims)}")
    print(f"Results written to: results.json")
    print("=" * 60)


if __name__ == "__main__":
    main()
