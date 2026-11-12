"""Demand- and payout-aware model choice for a Darkbloom provider (SHADOW ONLY: it decides, it never acts).

Pure functions plus tiny stdlib IO. Nothing here restarts, switches or configures anything."""
import json
import os
import urllib.request
import tempfile

BASE = "https://console.darkbloom.dev"


def fetch_json(url, timeout=20, opener=None):
    """GET with urllib; parse JSON; raise on any error."""
    if opener is not None:
        raw = opener(url, timeout)
    else:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    return json.loads(raw.decode("utf-8"))


def _parse_usd(s):
    """Parse '$0.0420' -> 0.042."""
    return float(str(s).lstrip("$"))


def load_snapshot(fetch=None, now_ts=None):
    """Fetch network stats, pricing, and model info; return a snapshot dict."""
    if fetch is None:
        fetch = lambda p: fetch_json(BASE + p)

    net = fetch("/api/network/models?window=24h")
    pricing = fetch("/api/pricing")
    models_resp = fetch("/api/models")

    # Build models dict
    models = {}
    for row in net.get("models", []):
        mid = row["model"]
        models[mid] = row

    # Build prices dict: build_name -> {"in": float, "out": float}
    prices = {}
    for p in pricing.get("prices", []):
        pname = p["model"]
        prices[pname] = {"in": _parse_usd(p["input_usd"]), "out": _parse_usd(p["output_usd"])}

    # Build min_ram_gb and alias_of
    min_ram_gb = {}
    alias_of = {}
    for m in models_resp.get("data", []):
        mid = m["id"]
        hfid = m.get("hugging_face_id")
        ram = m.get("min_ram_gb")
        if ram is not None:
            min_ram_gb[mid] = float(ram)
        if hfid is not None:
            alias_of[hfid] = mid

    for a in models_resp.get("aliases", []):
        aid = a["id"]
        for k in ("primary_build", "desired_build", "previous_build"):
            b = a.get(k)
            if b is not None:
                alias_of[b] = aid
        for b in a.get("retired_builds", []):
            alias_of[b] = aid

    return {
        "ts": now_ts,
        "models": models,
        "prices": prices,
        "min_ram_gb": min_ram_gb,
        "alias_of": alias_of,
    }


def model_economics(row, price):
    """Compute economics for a single model row. Returns None if data is missing."""
    if row is None:
        return None
    demand = row.get("demand")
    supply = row.get("supply")
    pmd = row.get("per_machine_day")
    if demand is None or supply is None or pmd is None:
        return None
    if price is None:
        return None

    machines = supply.get("avg_loaded_machines", 0)
    if machines is None:
        machines = 0

    published_hours = max(demand.get("published_hours", 24) or 24, 1)
    # Handle 0 published_hours -> default 24
    if demand.get("published_hours", 24) == 0 or demand.get("published_hours") is None:
        published_hours = 24

    total_input_cost = demand.get("input_tokens", 0) * price["in"]
    total_output_cost = demand.get("output_tokens", 0) * price["out"]
    total_usd_day = (total_input_cost + total_output_cost) / 1e6 * 24 / published_hours

    pmd_input_cost = pmd.get("input_tokens", 0) * price["in"]
    pmd_output_cost = pmd.get("output_tokens", 0) * price["out"]
    per_machine_usd_day = (pmd_input_cost + pmd_output_cost) / 1e6

    turned_away = demand.get("turned_away") or {}
    total_turned_away = sum(turned_away.values())
    requests = demand.get("requests", 0)
    turned_away_share = (total_turned_away / requests) if requests > 0 else 0.0

    saturated = (row.get("status") == "needs_machines" or
                 turned_away_share > 0.02 or
                 (supply.get("busy_share", 0) or 0) > 0.7)

    share_usd_day = (total_usd_day / machines) if machines > 0 else 0.0

    if saturated:
        marginal_new_usd_day = per_machine_usd_day
    else:
        marginal_new_usd_day = total_usd_day / (machines + 1)

    return {
        "machines": machines,
        "total_usd_day": total_usd_day,
        "per_machine_usd_day": per_machine_usd_day,
        "turned_away_share": turned_away_share,
        "saturated": saturated,
        "share_usd_day": share_usd_day,
        "marginal_new_usd_day": marginal_new_usd_day,
    }


def update_history(history, snapshot, now_ts, keep=48):
    """Append one entry per model in snapshot to history. Dedupe within 1800s."""
    if history is None:
        history = {}
    result = {k: list(v) for k, v in history.items()}
    for model_id, model_data in snapshot.get("models", {}).items():
        entries = list(history.get(model_id, []))
        # Check if last entry is within 1800s (skip append)
        if entries:
            last_t = entries[-1]["t"]
            if abs(now_ts - last_t) <= 1800:
                result[model_id] = entries
                continue
        # Append new entry
        entries.append({"t": now_ts, "status": model_data.get("status", "")})
        # Cap at keep entries
        if len(entries) > keep:
            entries = entries[-keep:]
        result[model_id] = entries
    return result


def persistently_needs(history, model, n=3):
    """True when model has at least n entries and last n all have status needs_machines."""
    if history is None:
        return False
    entries = history.get(model)
    if entries is None:
        return False
    if len(entries) < n:
        return False
    last_n = entries[-n:]
    return all(e.get("status") == "needs_machines" for e in last_n)


def decide(snapshot, history, current, allowed, budget_gb, queue_busy, last_switch_ts, now_ts, cfg=None):
    """Make a stay/switch decision."""
    if cfg is None:
        cfg = {}
    min_gain_usd_day = cfg.get("min_gain_usd_day", 0.10)
    min_gain_ratio = cfg.get("min_gain_ratio", 0.25)
    persist_n = cfg.get("persist_n", 3)
    cooldown_s = cfg.get("cooldown_s", 43200)

    # Compute current value: max share_usd_day over current builds that have economics
    current_value = 0.0
    for build in current:
        eco = model_economics(
            snapshot["models"].get(snapshot["alias_of"].get(build, build)),
            snapshot["prices"].get(build)
        )
        if eco is not None:
            current_value = max(current_value, eco["share_usd_day"])

    # Build candidates
    candidates = []
    for build in allowed:
        if build in current:
            continue
        net_id = snapshot["alias_of"].get(build, build)
        row = snapshot["models"].get(net_id)
        price = snapshot["prices"].get(build)
        eco = model_economics(row, price) if row and price else None

        ram = snapshot["min_ram_gb"].get(net_id)
        fits = (ram is not None) and (ram <= budget_gb)
        # Missing min_ram_gb counts as not fitting
        if ram is None:
            fits = False

        score = eco["marginal_new_usd_day"] if eco else 0.0
        persistent = False
        saturated_flag = False
        if eco:
            saturated_flag = eco["saturated"]
            if saturated_flag:
                persistent = persistently_needs(history, net_id, persist_n)

        candidates.append({
            "model": build,
            "score": score,
            "fits": fits,
            "persistent_need": persistent,
            "saturated": saturated_flag,
        })

    # Find best fitting candidate
    fitting_candidates = [c for c in candidates if c["fits"]]
    reasons = []

    if not fitting_candidates:
        reasons.append("no fitting candidate")
        return {
            "action": "stay",
            "target": None,
            "current_usd_day": current_value,
            "gain_usd_day": 0.0,
            "candidates": candidates,
            "reasons": reasons,
        }

    best = max(fitting_candidates, key=lambda c: c["score"])
    gain = best["score"] - current_value
    gain_ratio = gain / max(current_value, 0.01)

    # Check conditions for switch
    if queue_busy:
        reasons.append("queue busy")
    elapsed = (now_ts - last_switch_ts) if last_switch_ts is not None else cooldown_s
    if elapsed < cooldown_s:
        reasons.append("cooldown not elapsed")
    if gain < min_gain_usd_day:
        reasons.append("gain below threshold")
    if gain < min_gain_ratio * max(current_value, 0.01):
        reasons.append("gain ratio below threshold")

    # Persistence check: if candidate is saturated, must have persistence
    if best["saturated"] and not best["persistent_need"]:
        reasons.append("saturated without persistence")

    if reasons:
        return {
            "action": "stay",
            "target": None,
            "current_usd_day": current_value,
            "gain_usd_day": gain,
            "candidates": candidates,
            "reasons": reasons,
        }

    return {
        "action": "switch",
        "target": best["model"],
        "current_usd_day": current_value,
        "gain_usd_day": gain,
        "candidates": candidates,
        "reasons": [],
    }


def shadow_tick(state_path, log_path, fetch, current, allowed, budget_gb, queue_busy, now_ts, min_interval_s=3600):
    """Shadow mode tick: read state, possibly fetch, decide, write state and log."""
    # Load state
    state = {}
    try:
        with open(state_path, "r") as f:
            state = json.load(f)
    except Exception:
        state = {}
    if not isinstance(state, dict):
        state = {}

    last_run = state.get("last_run")
    try:
        if last_run is not None and (now_ts - last_run) < min_interval_s:
            return None
    except Exception:
        pass

    history = state.get("history") if isinstance(state.get("history"), dict) else {}
    saved_last_switch_ts = state.get("last_switch_ts")
    if not isinstance(saved_last_switch_ts, (int, float)):
        saved_last_switch_ts = None

    try:
        snap = load_snapshot(fetch, now_ts)
        history = update_history(history, snap, now_ts)
        decision = decide(snap, history, current, allowed, budget_gb, queue_busy,
                          saved_last_switch_ts, now_ts)

        # Save state (write to temp then replace)
        new_state = {"last_run": now_ts, "history": history, "last_switch_ts": saved_last_switch_ts}
        # Do NOT update last_switch_ts in shadow mode
        tmp_fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(state_path) if os.path.dirname(state_path) else ".")
        try:
            with os.fdopen(tmp_fd, "w") as f:
                json.dump(new_state, f)
            os.replace(tmp_path, state_path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

        # Write log line
        log_entry = {
            "ts": now_ts,
            "action": decision["action"],
            "target": decision.get("target"),
            "current": current,
            "current_usd_day": decision.get("current_usd_day", 0.0),
            "gain_usd_day": decision.get("gain_usd_day", 0.0),
            "reasons": decision.get("reasons", []),
            "candidates": decision.get("candidates", []),
        }
        with open(log_path, "a") as f:
            f.write(json.dumps(log_entry) + "\n")

        return decision

    except Exception as e:
        # On fetch error: do not touch history, set last_run, return error
        err_state = {"last_run": now_ts, "history": state.get("history") if isinstance(state.get("history"), dict) else {},
                     "last_switch_ts": saved_last_switch_ts}
        try:
            tmp_fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(state_path) if os.path.dirname(state_path) else ".")
            with os.fdopen(tmp_fd, "w") as f:
                json.dump(err_state, f)
            os.replace(tmp_path, state_path)
        except Exception:
            pass

        # Log error line
        log_entry = {"ts": now_ts, "error": f"{type(e).__name__}: {e}"}
        try:
            with open(log_path, "a") as f:
                f.write(json.dumps(log_entry) + "\n")
        except Exception:
            pass

        return {"action": "stay", "target": None, "error": f"{type(e).__name__}: {e}"}
