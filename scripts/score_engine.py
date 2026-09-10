#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
score_engine.py - Agent-CI composite scoring engine with delta tracking.

Computes composite scores per project scope based on session health, loop
rates, user correction rates, and upstream freshness. Tracks score deltas
across cycles in score_history.json.
"""

import os
import sys
import re
import json
import argparse
from pathlib import Path
from datetime import datetime, timezone


def route_session_to_scope(session: dict, registry: dict) -> str:
    """Route a session to its project scope based on workspace pattern matching.

    Args:
        session: Session dictionary containing at least 'workspace' and optionally 'filename'.
        registry: Registry dictionary containing a 'mappings' list of dicts with
                  'workspace_pattern' and 'project_name'.

    Returns:
        The matched project_name, or 'global' if no pattern matches.
    """
    workspace = session.get("workspace")
    if not isinstance(workspace, str):
        workspace = str(workspace or "")

    for mapping in registry.get("mappings", []):
        pattern = mapping.get("workspace_pattern", "")
        if pattern and re.search(pattern, workspace, re.IGNORECASE):
            return mapping.get("project_name", "global")

    # Fallback to filename if workspace is empty or did not match
    filename = session.get("filename")
    if filename and isinstance(filename, str):
        for mapping in registry.get("mappings", []):
            pattern = mapping.get("workspace_pattern", "")
            if pattern and re.search(pattern, filename, re.IGNORECASE):
                return mapping.get("project_name", "global")

    return "global"


def count_rule_occurrences(rule: dict, telemetry_data: dict) -> int:
    """Count sessions matching rule's target_error_type in telemetry_data.

    Args:
        rule: Dictionary containing 'target_error_type'.
        telemetry_data: Telemetry dictionary containing 'sessions' list.

    Returns:
        Integer count of matching sessions.
    """
    if not isinstance(rule, dict) or not isinstance(telemetry_data, dict):
        return 0

    target = str(rule.get("target_error_type", "")).strip()
    if not target:
        return 0

    sessions = telemetry_data.get("sessions", [])
    if not isinstance(sessions, list):
        return 0

    target_lower = target.lower()
    count = 0

    if target_lower.startswith("loop:"):
        tool_name = target[5:].strip().lower()
        for s in sessions:
            if not isinstance(s, dict):
                continue
            loops = s.get("loops", [])
            if isinstance(loops, list):
                if any(str(l.get("tool", "")).strip().lower() == tool_name for l in loops if isinstance(l, dict)):
                    count += 1

    elif target_lower.startswith("error:"):
        err_type = target[6:].strip().lower()
        for s in sessions:
            if not isinstance(s, dict):
                continue
            errors = s.get("errors", [])
            if isinstance(errors, list):
                if any(str(e.get("type", "")).strip().lower() == err_type for e in errors if isinstance(e, dict)):
                    count += 1

    elif target_lower in ("correction", "user_correction", "user_corrections"):
        for s in sessions:
            if not isinstance(s, dict):
                continue
            has_corr = bool(s.get("user_corrections"))
            if not has_corr:
                errors = s.get("errors", [])
                if isinstance(errors, list):
                    has_corr = any(str(e.get("type", "")).strip().lower() == "usercorrectionsignal" for e in errors if isinstance(e, dict))
            if has_corr:
                count += 1

    else:
        for s in sessions:
            if not isinstance(s, dict):
                continue
            errors = s.get("errors", [])
            matched = False
            if isinstance(errors, list):
                matched = any(str(e.get("type", "")).strip().lower() == target_lower for e in errors if isinstance(e, dict))
            if not matched:
                loops = s.get("loops", [])
                if isinstance(loops, list):
                    matched = any(str(l.get("tool", "")).strip().lower() == target_lower for l in loops if isinstance(l, dict))
            if matched:
                count += 1

    return count


def compute_rule_efficacy(rule: dict, telemetry_data: dict) -> float:
    """Compute efficacy for a single rule against telemetry data.

    Formula:
      efficacy = 1.0 - (current_count / baseline_count), clamped to [0.0, 1.0].
      If baseline_count <= 0, returns 1.0.

    Args:
        rule: Rule dictionary with 'target_error_type' and 'baseline_count'.
        telemetry_data: Telemetry data dictionary containing 'sessions'.

    Returns:
        Efficacy score clamped to [0.0, 1.0] (float).
    """
    if not isinstance(rule, dict):
        return 1.0

    baseline = rule.get("baseline_count", 0)
    try:
        baseline_val = float(baseline)
    except (ValueError, TypeError):
        baseline_val = 0.0

    if baseline_val <= 0.0:
        return 1.0

    current_count = count_rule_occurrences(rule, telemetry_data)
    raw_efficacy = 1.0 - (float(current_count) / baseline_val)
    clamped_efficacy = max(0.0, min(1.0, raw_efficacy))
    return round(clamped_efficacy, 4)


def compute_learning_rate(ledger: dict, telemetry_data: dict) -> float:
    """Compute the learning rate based on active rule efficacy.

    Ratio of effective active rules (efficacy >= 0.3) over total active rules.
    Defaults to 1.0 if no active rules in ledger.

    Args:
        ledger: Rule ledger dictionary containing 'rules' list.
        telemetry_data: Telemetry dictionary containing 'sessions'.

    Returns:
        Learning rate as a float between 0.0 and 1.0.
    """
    if not isinstance(ledger, dict):
        return 1.0

    rules = ledger.get("rules", [])
    if not isinstance(rules, list) or not rules:
        return 1.0

    active_statuses = {"ACTIVE", "EFFECTIVE", "PERMANENT", "PRUNE_CANDIDATE"}
    active_rules = [
        r for r in rules
        if isinstance(r, dict) and str(r.get("status", "ACTIVE")).upper() in active_statuses
    ]

    if not active_rules:
        return 1.0

    effective_count = sum(
        1 for r in active_rules if compute_rule_efficacy(r, telemetry_data) >= 0.3
    )

    rate = effective_count / len(active_rules)
    return round(max(0.0, min(1.0, rate)), 4)


def _get_rule_age_days(rule: dict) -> float:
    """Calculate the age of a rule in days from applied_at or first measurement.

    Args:
        rule: Rule dictionary.

    Returns:
        Age in days as float (0.0 if not determinable).
    """
    applied_at_str = rule.get("applied_at")
    if not applied_at_str:
        measurements = rule.get("measurements", [])
        if measurements and isinstance(measurements, list):
            first_cycle = measurements[0].get("cycle")
            if first_cycle:
                applied_at_str = first_cycle

    if not applied_at_str:
        return 0.0

    try:
        s = str(applied_at_str).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        time_diff_days = (now - dt).total_seconds() / 86400.0
        cal_diff_days = float((now.date() - dt.date()).days)
        return max(0.0, max(time_diff_days, cal_diff_days))
    except Exception:
        return 0.0


def update_rule_measurements(ledger_path: Path | str, telemetry_data: dict) -> dict:
    """Update active rule measurements and transition rule status based on lifecycle.

    Appends measurement: {"cycle": "YYYY-MM-DD", "count": N, "efficacy": float}.
    Lifecycle transitions:
      - efficacy >= 0.5 after 14+ days -> PERMANENT
      - efficacy >= 0.3 after 7+ days  -> EFFECTIVE
      - efficacy < 0.2 after 7+ days   -> PRUNE_CANDIDATE

    Args:
        ledger_path: Path to rule_ledger.json.
        telemetry_data: Telemetry data dictionary containing 'sessions'.

    Returns:
        The updated ledger dictionary.
    """
    ledger_path = Path(ledger_path)

    ledger: dict = {"rules": [], "pruned": [], "evolved": []}
    if ledger_path.exists():
        try:
            content = ledger_path.read_text(encoding="utf-8")
            if content.strip():
                loaded = json.loads(content)
                if isinstance(loaded, dict):
                    ledger = loaded
        except Exception as e:
            print(f"Warning: Failed to read rule ledger '{ledger_path}': {e}. Using empty ledger.", file=sys.stderr)

    for key in ("rules", "pruned", "evolved"):
        if key not in ledger or not isinstance(ledger[key], list):
            ledger[key] = []

    cycle_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    active_statuses = {"ACTIVE", "EFFECTIVE", "PERMANENT", "PRUNE_CANDIDATE"}

    for rule in ledger["rules"]:
        if not isinstance(rule, dict):
            continue

        status = str(rule.get("status", "ACTIVE")).upper()
        if status not in active_statuses:
            continue

        count = count_rule_occurrences(rule, telemetry_data)
        efficacy = compute_rule_efficacy(rule, telemetry_data)

        if "measurements" not in rule or not isinstance(rule["measurements"], list):
            rule["measurements"] = []

        rule["measurements"].append({
            "cycle": cycle_date,
            "count": count,
            "efficacy": efficacy
        })

        # Determine days active
        days = _get_rule_age_days(rule)

        # Lifecycle status transitions
        if days >= 14 and efficacy >= 0.5:
            rule["status"] = "PERMANENT"
        elif days >= 7 and efficacy >= 0.3:
            rule["status"] = "EFFECTIVE"
        elif days >= 7 and efficacy < 0.2:
            rule["status"] = "PRUNE_CANDIDATE"
        else:
            if "status" not in rule:
                rule["status"] = "ACTIVE"

    # Persist updated ledger
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.write_text(
        json.dumps(ledger, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8"
    )

    return ledger


def compute_scope_scores(
    sessions_by_scope: dict,
    upstream_freshness: float | dict | None = None,
    learning_rate: float = 1.0
) -> dict:
    """Compute composite scores for each project scope.

    Formula:
      health_rate = healthy_count / total_count (or 1.0 if no sessions)
      loop_rate = sessions_with_loops / total_count (or 0.0 if no sessions)
      correction_rate = sessions_with_corrections / total_count (or 0.0 if no sessions)
      freshness = upstream_freshness clamped to [0.0, 1.0] (default: 1.0)
      learning_rate = ratio of effective active rules (default: 1.0)
      score = (
          (0.30 * health_rate)
          + (0.25 * (1.0 - loop_rate))
          + (0.15 * (1.0 - correction_rate))
          + (0.10 * freshness)
          + (0.20 * learning_rate)
      )

    Args:
        sessions_by_scope: Mapping of scope name to list of session dictionaries.
        upstream_freshness: Float freshness score (default: 1.0), or a dict with
                            scoped freshness / top-level freshness.
        learning_rate: Float learning rate in [0.0, 1.0] (default: 1.0).

    Returns:
        Dictionary mapping scope name to computed composite score (float).
    """
    scores = {}
    lr = max(0.0, min(1.0, float(learning_rate)))

    for scope, sessions in sessions_by_scope.items():
        total_count = len(sessions)

        if total_count == 0:
            health_rate = 1.0
            loop_rate = 0.0
            correction_rate = 0.0
        else:
            healthy_count = sum(
                1 for s in sessions if str(s.get("health", "")).upper() == "HEALTHY"
            )
            health_rate = healthy_count / total_count

            sessions_with_loops = sum(
                1 for s in sessions if bool(s.get("loops"))
            )
            loop_rate = sessions_with_loops / total_count

            sessions_with_corrections = sum(
                1 for s in sessions if bool(s.get("user_corrections"))
            )
            correction_rate = sessions_with_corrections / total_count

        # Resolve upstream freshness for this scope
        freshness = 1.0
        if upstream_freshness is not None:
            if isinstance(upstream_freshness, (int, float)):
                freshness = float(upstream_freshness)
            elif isinstance(upstream_freshness, dict):
                if scope in upstream_freshness:
                    val = upstream_freshness[scope]
                    if isinstance(val, dict):
                        freshness = float(
                            val.get("freshness", val.get("upstream_freshness", val.get("score", 1.0)))
                        )
                    elif isinstance(val, (int, float)):
                        freshness = float(val)
                elif "freshness" in upstream_freshness:
                    freshness = float(upstream_freshness["freshness"])
                elif "upstream_freshness" in upstream_freshness:
                    freshness = float(upstream_freshness["upstream_freshness"])
                elif "score" in upstream_freshness:
                    freshness = float(upstream_freshness["score"])

        # Clamp freshness to [0.0, 1.0]
        freshness = max(0.0, min(1.0, freshness))

        score = (
            (0.30 * health_rate)
            + (0.25 * (1.0 - loop_rate))
            + (0.15 * (1.0 - correction_rate))
            + (0.10 * freshness)
            + (0.20 * lr)
        )
        scores[scope] = round(score, 4)

    return scores


def compute_deltas(current_scores: dict, history: dict) -> dict:
    """Compare current scores vs the last cycle in history.

    Args:
        current_scores: Mapping of scope name to current score (float or dict with 'score').
        history: History dictionary containing 'cycles' list.

    Returns:
        Dictionary mapping scope name to {"score": X, "delta": Y}.
    """
    cycles = history.get("cycles", [])
    last_cycle = cycles[-1] if cycles else {}
    last_scores = last_cycle.get("scores", {})

    deltas = {}
    for scope, val in current_scores.items():
        if isinstance(val, dict):
            curr_score = float(val.get("score", 0.0))
        else:
            curr_score = float(val)

        prev_val = last_scores.get(scope)
        if prev_val is not None:
            if isinstance(prev_val, dict):
                prev_score = float(prev_val.get("score", 0.0))
            else:
                prev_score = float(prev_val)
            delta = round(curr_score - prev_score, 4)
        else:
            delta = 0.0

        deltas[scope] = {
            "score": round(curr_score, 4),
            "delta": delta
        }

    return deltas


def update_history(scores: dict, history_path: Path) -> dict:
    """Append the current cycle to history and persist to disk (max 30 cycles FIFO).

    Args:
        scores: Dictionary of scores or deltas for the current cycle.
        history_path: Path to the score_history.json file.

    Returns:
        The updated history dictionary.
    """
    history_path = Path(history_path)

    history_data = {"cycles": []}
    if history_path.exists():
        try:
            content = history_path.read_text(encoding="utf-8")
            if content.strip():
                loaded = json.loads(content)
                if isinstance(loaded, dict) and "cycles" in loaded and isinstance(loaded["cycles"], list):
                    history_data = loaded
        except Exception as e:
            print(f"Warning: Failed to read existing history from '{history_path}': {e}. Starting fresh.", file=sys.stderr)

    cycle_record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "scores": scores
    }

    history_data["cycles"].append(cycle_record)

    # Keep max 30 cycles (FIFO)
    if len(history_data["cycles"]) > 30:
        history_data["cycles"] = history_data["cycles"][-30:]

    history_path.parent.mkdir(parents=True, exist_ok=True)
    history_path.write_text(
        json.dumps(history_data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8"
    )

    return history_data


def print_summary(
    results: dict,
    sessions_by_scope: dict,
    history_path: Path,
    total_cycles: int,
    learning_rate: float = 1.0,
    ledger_data: dict | None = None
) -> None:
    """Print human-readable summary table to stdout."""
    print("=" * 70)
    print("  Agent-CI Score Engine - Composite Scores & Delta Tracking")
    print("=" * 70)
    print(f"History file  : {history_path} ({total_cycles} cycle{'s' if total_cycles != 1 else ''} recorded)")
    print(f"Learning rate : {learning_rate:.4f}")
    print("-" * 70)
    header = f"{'Scope':<15} {'Score':>7} {'Delta':>9} {'Sessions':>10} {'Health%':>9} {'Loops%':>8} {'Corr%':>7}"
    print(header)
    print("-" * 70)

    for scope, data in results.items():
        score = data.get("score", 0.0)
        delta = data.get("delta", 0.0)
        sessions = sessions_by_scope.get(scope, [])
        n_sess = len(sessions)

        if n_sess > 0:
            h_count = sum(1 for s in sessions if str(s.get("health", "")).upper() == "HEALTHY")
            l_count = sum(1 for s in sessions if bool(s.get("loops")))
            c_count = sum(1 for s in sessions if bool(s.get("user_corrections")))

            h_rate = (h_count / n_sess) * 100
            l_rate = (l_count / n_sess) * 100
            c_rate = (c_count / n_sess) * 100

            h_str = f"{h_rate:.1f}%"
            l_str = f"{l_rate:.1f}%"
            c_str = f"{c_rate:.1f}%"
        else:
            h_str = "100.0%"
            l_str = "0.0%"
            c_str = "0.0%"

        delta_str = f"{delta:+.4f}" if delta != 0 else " 0.0000"
        print(f"{scope:<15} {score:>7.4f} {delta_str:>9} {n_sess:>10} {h_str:>9} {l_str:>8} {c_str:>7}")

    print("=" * 70)

    rules = (ledger_data or {}).get("rules", [])
    if rules:
        print("\n" + "=" * 70)
        print(f"  Rule Efficacy Table ({len(rules)} rule{'s' if len(rules) != 1 else ''} tracked, Learning Rate: {learning_rate:.4f})")
        print("-" * 70)
        rule_header = f"{'Rule ID':<20} {'Target':<22} {'Base':>5} {'Curr':>5} {'Efficacy':>9} {'Status':<16}"
        print(rule_header)
        print("-" * 70)
        for r in rules:
            r_id = str(r.get("id", "unnamed"))[:19]
            target = str(r.get("target_error_type", ""))[:21]
            base = r.get("baseline_count", 0)
            status = str(r.get("status", "ACTIVE"))[:16]
            measurements = r.get("measurements", [])
            if measurements and isinstance(measurements, list):
                latest = measurements[-1]
                curr = latest.get("count", "-")
                eff = latest.get("efficacy", 0.0)
                eff_str = f"{eff:.4f}"
            else:
                curr = "-"
                eff_str = "-"
            print(f"{r_id:<20} {target:<22} {base:>5} {str(curr):>5} {eff_str:>9} {status:<16}")
        print("=" * 70)


def main() -> None:
    """CLI entry point for score_engine.py."""
    parser = argparse.ArgumentParser(
        description="Agent-CI Score Engine - Computes composite scores per project scope with delta tracking."
    )
    parser.add_argument(
        "--telemetry",
        type=str,
        required=True,
        help="Path to telemetry JSON from parse_logs.py (required)."
    )
    parser.add_argument(
        "--registry",
        type=str,
        default=str(Path.home() / ".agents" / "project-skill-map.json"),
        help="Path to project-skill mapping registry (default: ~/.agents/project-skill-map.json)."
    )
    parser.add_argument(
        "--history",
        type=str,
        default=str(Path.home() / ".agents" / "score_history.json"),
        help="Path to score history file (default: ~/.agents/score_history.json)."
    )
    parser.add_argument(
        "--upstream-status",
        type=str,
        default=None,
        help="Path to upstream check JSON for freshness score (optional)."
    )
    parser.add_argument(
        "--rule-ledger",
        type=str,
        default=str(Path.home() / ".agents" / "rule_ledger.json"),
        help="Path to rule ledger state file (default: ~/.agents/rule_ledger.json)."
    )

    args = parser.parse_args()

    # 1. Validate & load telemetry
    telem_path = Path(os.path.expanduser(args.telemetry))
    if not telem_path.exists():
        print(f"Error: Telemetry file not found: '{telem_path}'", file=sys.stderr)
        sys.exit(1)

    try:
        telemetry_data = json.loads(telem_path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"Error: Failed to parse telemetry JSON from '{telem_path}': {e}", file=sys.stderr)
        sys.exit(1)

    if not isinstance(telemetry_data, dict):
        print(f"Error: Telemetry data must be a JSON object, got {type(telemetry_data).__name__}", file=sys.stderr)
        sys.exit(1)

    # 2. Validate & load registry
    registry_path = Path(os.path.expanduser(args.registry))
    if not registry_path.exists():
        print(f"Error: Registry file not found: '{registry_path}'", file=sys.stderr)
        sys.exit(1)

    try:
        registry_data = json.loads(registry_path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"Error: Failed to parse registry JSON from '{registry_path}': {e}", file=sys.stderr)
        sys.exit(1)

    if not isinstance(registry_data, dict):
        print(f"Error: Registry data must be a JSON object, got {type(registry_data).__name__}", file=sys.stderr)
        sys.exit(1)

    # 3. Validate & load upstream status (optional)
    upstream_freshness = 1.0
    if args.upstream_status:
        upstream_path = Path(os.path.expanduser(args.upstream_status))
        if not upstream_path.exists():
            print(f"Error: Upstream status file not found: '{upstream_path}'", file=sys.stderr)
            sys.exit(1)
        try:
            upstream_freshness = json.loads(upstream_path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"Error: Failed to parse upstream status JSON from '{upstream_path}': {e}", file=sys.stderr)
            sys.exit(1)

    # 4. Load history
    history_path = Path(os.path.expanduser(args.history))
    history_data = {"cycles": []}
    if history_path.exists():
        try:
            content = history_path.read_text(encoding="utf-8")
            if content.strip():
                loaded = json.loads(content)
                if isinstance(loaded, dict) and "cycles" in loaded:
                    history_data = loaded
        except Exception as e:
            print(f"Warning: Failed to parse history JSON from '{history_path}': {e}. Using empty history.", file=sys.stderr)

    # 5. Load rule ledger & update measurements & compute learning rate
    ledger_path = Path(os.path.expanduser(args.rule_ledger))
    ledger_data = {"rules": [], "pruned": [], "evolved": []}
    learning_rate = 1.0

    if ledger_path.exists():
        try:
            ledger_data = update_rule_measurements(ledger_path, telemetry_data)
            learning_rate = compute_learning_rate(ledger_data, telemetry_data)
        except Exception as e:
            print(f"Warning: Failed to update rule ledger '{ledger_path}': {e}. Using baseline learning rate 1.0.", file=sys.stderr)
            learning_rate = 1.0
    else:
        learning_rate = 1.0

    # 6. Group sessions by scope
    # Initialize scopes from registry and global
    known_scopes = ["global"]
    for mapping in registry_data.get("mappings", []):
        p_name = mapping.get("project_name")
        if p_name and p_name not in known_scopes:
            known_scopes.append(p_name)

    sessions_by_scope: dict[str, list[dict]] = {s: [] for s in known_scopes}
    sessions = telemetry_data.get("sessions", [])

    for sess in sessions:
        if isinstance(sess, dict):
            scope = route_session_to_scope(sess, registry_data)
            if scope not in sessions_by_scope:
                sessions_by_scope[scope] = []
            sessions_by_scope[scope].append(sess)

    # 7. Compute scores with learning_rate
    current_scores = compute_scope_scores(sessions_by_scope, upstream_freshness, learning_rate)

    # 8. Compute deltas against history
    deltas = compute_deltas(current_scores, history_data)

    # 9. Persist updated history
    updated_history = update_history(deltas, history_path)

    # 10. Output summary to stdout
    print_summary(
        deltas,
        sessions_by_scope,
        history_path,
        len(updated_history["cycles"]),
        learning_rate,
        ledger_data
    )


if __name__ == "__main__":
    main()
