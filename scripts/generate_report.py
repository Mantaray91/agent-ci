#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
generate_report.py - Synthesizes Graphify analysis and telemetry into actionable Continuous Improvement reports.
Outputs formatted Markdown reports to <project_root>/agent-ci-reports/REPORT_YYYY-MM-DD_HH-mm.md.
Optionally generates Obsidian dashboard at ~/.agents/agent-ci-reports/OBSIDIAN_DASHBOARD.md.
"""

import os
import sys
import json
import argparse
from pathlib import Path
from datetime import datetime, timezone
from collections import defaultdict, Counter

try:
    from score_engine import count_rule_occurrences, compute_rule_efficacy
except ImportError:
    script_dir = Path(__file__).resolve().parent
    if str(script_dir) not in sys.path:
        sys.path.insert(0, str(script_dir))
    try:
        from score_engine import count_rule_occurrences, compute_rule_efficacy
    except Exception:
        count_rule_occurrences = None
        compute_rule_efficacy = None


def load_optional_json(path_str: str | None, label: str, fallback_path: Path | None = None) -> dict | None:
    """Load JSON data from an optional path or fallback location with graceful error handling."""
    target_path = None
    if path_str:
        target_path = Path(os.path.expanduser(path_str))
    elif fallback_path and fallback_path.exists() and fallback_path.is_file():
        target_path = fallback_path

    if not target_path:
        return None

    if not target_path.exists() or not target_path.is_file():
        if path_str:
            print(f"Notice: {label} file not found at '{target_path}'.", file=sys.stderr)
        return None

    try:
        content = target_path.read_text(encoding="utf-8")
        if not content.strip():
            return None
        data = json.loads(content)
        if isinstance(data, dict):
            return data
        print(f"Warning: {label} file '{target_path}' does not contain a JSON object.", file=sys.stderr)
        return None
    except Exception as e:
        print(f"Warning: Failed to load {label} JSON from '{target_path}': {e}", file=sys.stderr)
        return None


def render_composite_scores_section(
    telemetry_data: dict,
    scores_data: dict | None = None,
    upstream_data: dict | None = None
) -> list[str]:
    """Render the Composite Scores & Trend table."""
    lines = []
    lines.append("## 🎯 Composite Scores & Trend")
    lines.append("")

    sessions = telemetry_data.get("sessions", [])
    sessions_by_scope = defaultdict(list)
    for s in sessions:
        tag = s.get("project_tag") or "global"
        sessions_by_scope[tag].append(s)

    latest_cycle = {}
    if scores_data and isinstance(scores_data, dict):
        cycles = scores_data.get("cycles", [])
        if cycles and isinstance(cycles[-1], dict):
            latest_cycle = cycles[-1].get("scores", {})

    freshness_val = 1.0
    if upstream_data and isinstance(upstream_data, dict):
        freshness_val = float(upstream_data.get("freshness", 1.0))
    freshness_str = f"{freshness_val * 100:.1f}%"

    all_scopes = ["global"]
    for sc in list(latest_cycle.keys()) + list(sessions_by_scope.keys()):
        if sc not in all_scopes:
            all_scopes.append(sc)

    lines.append("| Scope | Composite Score | Delta (vs Prev) | Health Rate | Loop Rate | Correction Rate | Upstream Freshness |")
    lines.append("| :--- | :---: | :---: | :---: | :---: | :---: | :---: |")

    for scope in all_scopes:
        scope_sess = sessions_by_scope.get(scope, [])
        n_sess = len(scope_sess)

        if n_sess > 0:
            h_count = sum(1 for s in scope_sess if str(s.get("health", "")).upper() == "HEALTHY")
            l_count = sum(1 for s in scope_sess if bool(s.get("loops")))
            c_count = sum(1 for s in scope_sess if bool(s.get("user_corrections")))

            h_rate = h_count / n_sess
            l_rate = l_count / n_sess
            c_rate = c_count / n_sess
        else:
            h_rate = 1.0
            l_rate = 0.0
            c_rate = 0.0

        score_info = latest_cycle.get(scope)
        if score_info is not None:
            if isinstance(score_info, dict):
                score_val = float(score_info.get("score", 0.0))
                delta_val = float(score_info.get("delta", 0.0))
            else:
                score_val = float(score_info)
                delta_val = 0.0
        else:
            score_val = (0.4 * h_rate) + (0.3 * (1.0 - l_rate)) + (0.2 * (1.0 - c_rate)) + (0.1 * freshness_val)
            delta_val = 0.0

        if delta_val > 0:
            delta_str = f"+{delta_val:.4f} 🟢"
        elif delta_val < 0:
            delta_str = f"{delta_val:.4f} 🔴"
        else:
            delta_str = "0.0000 ⚪"

        h_str = f"{h_rate * 100:.1f}%"
        l_str = f"{l_rate * 100:.1f}%"
        c_str = f"{c_rate * 100:.1f}%"

        lines.append(f"| **{scope}** | `{score_val:.4f}` | {delta_str} | {h_str} | {l_str} | {c_str} | {freshness_str} |")

    lines.append("")
    return lines


def render_rule_efficacy_section(
    rule_ledger_data: dict | None,
    telemetry_data: dict | None = None,
) -> list[str]:
    """Render the Rule Efficacy & Learning Rate dashboard section."""
    if not rule_ledger_data or not isinstance(rule_ledger_data, dict):
        return []

    rules = rule_ledger_data.get("rules", [])
    if not rules or not isinstance(rules, list):
        return []

    active_statuses = {"ACTIVE", "EFFECTIVE", "PERMANENT", "PRUNE_CANDIDATE"}
    active_rules = [
        r for r in rules
        if isinstance(r, dict) and str(r.get("status", "ACTIVE")).upper() in active_statuses
    ]

    if not active_rules:
        return []

    effective_count = 0
    rule_rows = []

    for r in active_rules:
        rule_id = r.get("id", "unknown")
        target_error = r.get("target_error_type") or r.get("target_error", "unknown")
        raw_target_file = r.get("target_file", "GEMINI.md")
        target_file = os.path.basename(raw_target_file) if raw_target_file else "GEMINI.md"
        applied_at = str(r.get("applied_at", ""))[:10] or "N/A"
        baseline = int(r.get("baseline_count", 0))

        # Current count & efficacy
        measurements = r.get("measurements", [])
        if measurements and isinstance(measurements, list):
            last_m = measurements[-1]
            current = int(last_m.get("count", 0)) if isinstance(last_m, dict) else 0
            eff = float(last_m.get("efficacy", 0.0)) if isinstance(last_m, dict) else 0.0
        elif telemetry_data:
            if count_rule_occurrences and compute_rule_efficacy:
                try:
                    current = count_rule_occurrences(r, telemetry_data)
                    eff = compute_rule_efficacy(r, telemetry_data)
                except Exception:
                    current = int(r.get("current_count", baseline))
                    raw_eff = 1.0 - (float(current) / baseline) if baseline > 0 else 1.0
                    eff = max(0.0, min(1.0, raw_eff))
            else:
                current = int(r.get("current_count", baseline))
                raw_eff = 1.0 - (float(current) / baseline) if baseline > 0 else 1.0
                eff = max(0.0, min(1.0, raw_eff))
        else:
            current = int(r.get("current_count", baseline))
            raw_eff = 1.0 - (float(current) / baseline) if baseline > 0 else 1.0
            eff = max(0.0, min(1.0, raw_eff))

        status = str(r.get("status", "ACTIVE")).upper()

        if eff >= 0.3 or status in ("EFFECTIVE", "PERMANENT"):
            effective_count += 1
            indicator = "🟢"
        elif eff < 0.2 or status == "PRUNE_CANDIDATE":
            indicator = "🔴"
        else:
            indicator = "🟡"

        eff_str = f"{eff * 100:.1f}% {indicator}"
        rule_rows.append({
            "id": rule_id,
            "target_error": target_error,
            "target_file": target_file,
            "applied": applied_at,
            "baseline": baseline,
            "current": current,
            "efficacy_str": eff_str,
            "status": status,
        })

    learning_rate = round(effective_count / len(active_rules), 4) if active_rules else 1.0
    lr_pct = f"{learning_rate * 100:.1f}%"

    lines = []
    lines.append("## 🧬 Rule Efficacy & Learning Rate")
    lines.append("")
    lines.append(f"> **Learning Rate**: `{lr_pct}` ({effective_count}/{len(active_rules)} active rules effective)")
    lines.append("")
    lines.append("| Rule ID | Target Error | Target File | Applied | Baseline | Current | Efficacy | Status |")
    lines.append("| :--- | :--- | :--- | :---: | :---: | :---: | :---: | :---: |")

    for row in rule_rows:
        lines.append(
            f"| `{row['id']}` | `{row['target_error']}` | `{row['target_file']}` | "
            f"{row['applied']} | {row['baseline']} | {row['current']} | "
            f"{row['efficacy_str']} | {row['status']} |"
        )

    lines.append("")
    return lines


def render_pruned_rules_section(rule_ledger_data: dict | None) -> list[str]:
    """Render the Pruned Rules Log table."""
    if not rule_ledger_data or not isinstance(rule_ledger_data, dict):
        return []

    pruned = rule_ledger_data.get("pruned", [])
    if not pruned or not isinstance(pruned, list):
        return []

    lines = []
    lines.append("## 🗑️ Pruned Rules (Auto-Removed)")
    lines.append("")
    lines.append("| Rule ID | Target Error | Reason | Final Efficacy | Pruned At |")
    lines.append("| :--- | :--- | :--- | :---: | :---: |")

    for p in pruned:
        if not isinstance(p, dict):
            continue
        p_id = p.get("id", "unknown")
        target_error = p.get("target_error_type") or p.get("target_error", "unknown")
        reason = p.get("reason", "efficacy < 0.2")
        val = p.get("final_efficacy", 0.0)
        try:
            eff_str = f"{float(val) * 100:.1f}%"
        except (ValueError, TypeError):
            eff_str = str(val)
        pruned_at = str(p.get("pruned_at", ""))[:10] or "N/A"

        lines.append(f"| `{p_id}` | `{target_error}` | {reason} | {eff_str} | {pruned_at} |")

    lines.append("")
    return lines


def render_auto_discovered_skills_section(skill_map_data: dict | None) -> list[str]:
    """Render the Auto-Discovered Skill Landscape section."""
    if not skill_map_data or not isinstance(skill_map_data, dict):
        return []

    mappings = skill_map_data.get("mappings", [])
    global_skills = skill_map_data.get("global_skills", [])

    if not mappings and not global_skills:
        return []

    lines = []
    lines.append("## 🔍 Auto-Discovered Skill Landscape")
    lines.append("")
    lines.append("| Project | Active Skills | Source |")
    lines.append("| :--- | :---: | :--- |")

    if isinstance(mappings, list):
        for m in mappings:
            if not isinstance(m, dict):
                continue
            proj = m.get("project_name", "Unknown")
            skill_paths = m.get("skill_paths", [])
            count = len(skill_paths) if isinstance(skill_paths, list) else 0
            raw_source = str(m.get("source", "workspace scan"))
            if "workspace_scan" in raw_source:
                source = "workspace scan"
            elif raw_source == "manual":
                source = "manual"
            else:
                source = raw_source.replace("auto:", "").replace("_", " ")

            lines.append(f"| {proj} | {count} | {source} |")

    if isinstance(global_skills, list) and global_skills:
        type_counts = Counter()
        for gs in global_skills:
            if isinstance(gs, dict):
                itype = str(gs.get("install_type", "local")).lower()
                type_counts[itype] += 1

        if type_counts["npx"] > 0:
            lines.append(f"| Global (NPX) | {type_counts['npx']} | .skill-lock.json |")
        if type_counts["symlink"] > 0:
            lines.append(f"| Global (symlink) | {type_counts['symlink']} | symlink + git |")
        if type_counts["local"] > 0:
            lines.append(f"| Global (local) | {type_counts['local']} | local directory |")
        if type_counts["system"] > 0:
            lines.append(f"| Global (system) | {type_counts['system']} | system packages |")

        for other_type, cnt in sorted(type_counts.items()):
            if other_type not in ("npx", "symlink", "local", "system") and cnt > 0:
                lines.append(f"| Global ({other_type}) | {cnt} | global scan |")

    lines.append("")
    return lines


def render_upstream_currency_section(upstream_data: dict | None) -> list[str]:
    """Render the Upstream Skill Currency section."""
    lines = []
    lines.append("## 🔄 Upstream Skill Currency")
    lines.append("")

    if not upstream_data or not isinstance(upstream_data, dict):
        lines.append("*Upstream skill currency not evaluated in this cycle.*")
        lines.append("")
        return lines

    total = upstream_data.get("total_skills", 0)
    freshness = float(upstream_data.get("freshness", 1.0))
    current = upstream_data.get("current", 0)
    updates = upstream_data.get("update_available", 0)
    skills = upstream_data.get("skills", [])

    lines.append(f"> **Checked at**: `{upstream_data.get('checked_at', 'N/A')}`  ")
    lines.append(f"> **Status Summary**: `{current}/{total}` current | **Freshness Score**: `{freshness * 100:.1f}%` | **Updates Available**: `{updates}`")
    lines.append("")

    if skills:
        lines.append("| Skill Name | Status | Local Hash | Remote Hash | Source |")
        lines.append("| :--- | :---: | :---: | :---: | :--- |")
        for s in skills:
            name = s.get("name", "unknown")
            status = s.get("status", "UNKNOWN")
            if status == "CURRENT":
                status_str = "🟢 `CURRENT`"
            elif status == "UPDATE_AVAILABLE":
                status_str = "🟡 `UPDATE_AVAILABLE`"
            elif status == "LOCAL_ONLY":
                status_str = "⚪ `LOCAL_ONLY`"
            elif status == "CHECK_FAILED":
                status_str = "🔴 `CHECK_FAILED`"
            else:
                status_str = f"`{status}`"

            loc_h = s.get("local_hash") or "—"
            if len(loc_h) > 8:
                loc_h = loc_h[:8]
            rem_h = s.get("remote_hash") or "—"
            if len(rem_h) > 8:
                rem_h = rem_h[:8]

            source = s.get("source_url") or s.get("source") or "local"
            lines.append(f"| **`{name}`** | {status_str} | `{loc_h}` | `{rem_h}` | `{source}` |")
    else:
        lines.append("✅ **No skills tracked in `.skill-lock.json`.**")

    lines.append("")
    return lines


def render_autonomous_improvements_section(changes_data: dict | None) -> list[str]:
    """Render the Autonomous Improvements Applied section."""
    lines = []
    lines.append("## ⚡ Autonomous Improvements Applied")
    lines.append("")

    if not changes_data or not isinstance(changes_data, dict):
        lines.append("*Autonomous improvement applier was not executed in this cycle.*")
        lines.append("")
        return lines

    commit_hash = changes_data.get("commit_hash", "UNKNOWN")
    tag = changes_data.get("tag", "N/A")
    total_changes = changes_data.get("total_changes", 0)
    dry_run = changes_data.get("dry_run", False)
    changes = changes_data.get("changes_applied", [])

    dry_run_suffix = " *(DRY RUN - No changes written)*" if dry_run else ""
    lines.append(f"> **Commit**: `{commit_hash}` | **Tag**: `{tag}` | **Total Changes**: `{total_changes}`{dry_run_suffix}  ")
    lines.append("")

    if changes:
        lines.append("| Target File / Skill | Change Type | Section / Category | Description |")
        lines.append("| :--- | :---: | :--- | :--- |")
        for ch in changes:
            target = os.path.basename(ch.get("target", "unknown"))
            ch_type = ch.get("type", "patch")
            section = ch.get("section", "Hardening")
            desc = ch.get("description", "")
            lines.append(f"| **`{target}`** | `{ch_type}` | {section} | {desc} |")
    else:
        lines.append("✅ **No new improvements required this cycle (system is stable).**")

    lines.append("")
    return lines


def render_verification_status_section(verification_data: dict | None) -> list[str]:
    """Render the Verification & Auto-Revert Status section."""
    lines = []
    lines.append("## 🛡️ Verification & Auto-Revert Status")
    lines.append("")

    if not verification_data or not isinstance(verification_data, dict):
        lines.append("*Verification engine was not executed in this cycle.*")
        lines.append("")
        return lines

    status = verification_data.get("status", "UNKNOWN")
    details = verification_data.get("details", "")
    ts = verification_data.get("timestamp", "")
    skills_eval = verification_data.get("skills_evaluated", [])
    revert_action = verification_data.get("revert_action")

    if status == "PASS":
        badge = "🟢 `PASS`"
    elif status == "SKIPPED":
        badge = "🟡 `SKIPPED`"
    elif status == "REVERTED":
        badge = "🔴 `REVERTED`"
    else:
        badge = f"`{status}`"

    lines.append(f"> **Verification Status**: {badge}  ")
    if ts:
        lines.append(f"> **Evaluated at**: `{ts}`  ")
    if details:
        lines.append(f"> **Details**: {details}  ")
    lines.append("")

    if revert_action and isinstance(revert_action, dict) and revert_action.get("reverted"):
        revert_hash = revert_action.get("revert_commit", "UNKNOWN")
        revert_tag = revert_action.get("tag", "N/A")
        reason = revert_action.get("reason", "Regression detected exceeding threshold")
        lines.append("> [!CAUTION]")
        lines.append("> **AUTOMATIC REVERT TRIGGERED**: Score regression exceeded allowable threshold!")
        lines.append(f"> - **Reverted Commit**: `{revert_hash}`")
        lines.append(f"> - **Revert Tag**: `{revert_tag}`")
        lines.append(f"> - **Reason**: {reason}")
        lines.append("")

    if skills_eval:
        lines.append("### Evaluated Skills")
        lines.append("| Skill Name | Status | Pass Rate | Baseline | Drop |")
        lines.append("| :--- | :---: | :---: | :---: | :---: |")
        for sk in skills_eval:
            sk_name = sk.get("skill") or sk.get("name", "unknown")
            sk_status = sk.get("status", "UNKNOWN")
            st_str = "🟢 PASS" if sk_status == "PASS" else "🔴 FAIL" if sk_status == "FAIL" else "⚪ NO_EVALS"
            pr = f"{sk.get('pass_rate', 1.0) * 100:.1f}%"
            bl = f"{sk.get('baseline', 1.0) * 100:.1f}%"
            dp = f"{sk.get('drop', 0.0) * 100:.1f}%"
            lines.append(f"| **`{sk_name}`** | {st_str} | {pr} | {bl} | {dp} |")
        lines.append("")

    return lines


def generate_markdown_report(
    telemetry_data: dict,
    project_root: Path,
    target_logs_dir: Path,
    scores_data: dict | None = None,
    upstream_data: dict | None = None,
    changes_data: dict | None = None,
    verification_data: dict | None = None,
    rule_ledger_data: dict | None = None,
    skill_map_data: dict | None = None,
) -> str:
    """Generate comprehensive CI Markdown report incorporating scoring, upstream, changes and verification."""
    stats = telemetry_data.get("stats", {})
    sessions = telemetry_data.get("sessions", [])

    total_sess = stats.get("total_sessions", len(sessions))
    healthy = stats.get("healthy_sessions", 0)
    warning = stats.get("warning_sessions", 0)
    failed = stats.get("failed_sessions", 0)

    healthy_pct = round((healthy / total_sess * 100), 1) if total_sess else 0.0
    warn_pct = round((warning / total_sess * 100), 1) if total_sess else 0.0
    fail_pct = round((failed / total_sess * 100), 1) if total_sess else 0.0

    sources = stats.get("sources", {})
    roles = stats.get("roles", {})
    top_tools = stats.get("top_tools", {})
    error_types = stats.get("error_types", {})

    all_loops = []
    for s in sessions:
        for lp in s.get("loops", []):
            all_loops.append({
                "session": s.get("conversation_id", "")[:8] or s.get("filename", ""),
                "tool": lp.get("tool", ""),
                "target": lp.get("target", ""),
                "count": lp.get("count", 0)
            })

    all_errors = []
    for s in sessions:
        for err in s.get("errors", []):
            all_errors.append({
                "session": s.get("conversation_id", "")[:8] or s.get("filename", ""),
                "level": err.get("level", "B"),
                "type": err.get("type", "Error"),
                "message": err.get("message", ""),
                "tool": err.get("tool", "")
            })

    errors_by_type = Counter(e["type"] for e in all_errors)
    loops_by_tool = Counter(l["tool"] for l in all_loops)

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    report = []
    report.append("# 📊 Agent Continuous Improvement (CI) Report")
    report.append("")
    report.append(f"> **Generated at**: `{now_str}`  ")
    report.append(f"> **Target Workspace**: `{project_root.resolve()}`  ")
    report.append(f"> **Logs Source**: `{target_logs_dir.resolve()}`  ")
    report.append("")
    report.append("---")
    report.append("")

    # 1. Executive Health Summary
    report.append("## 📈 Executive Health Summary")
    report.append("")
    report.append("| Metric | Count | Percentage | Status Indicator |")
    report.append("| :--- | :--- | :--- | :--- |")
    report.append(f"| **Total Session Turns** | `{total_sess}` | 100% | ℹ️ Total Executions |")
    report.append(f"| 🟢 **Healthy Sessions** | `{healthy}` | {healthy_pct}% | {'✅ Good' if healthy_pct >= 70 else '⚠️ Needs Attention'} |")
    report.append(f"| 🟡 **Warning Sessions (Friction/Loops)** | `{warning}` | {warn_pct}% | {'⚠️ Minor Friction' if warn_pct < 40 else '🔴 High Friction'} |")
    report.append(f"| 🔴 **Failed Sessions (Crashes/Errors)** | `{failed}` | {fail_pct}% | {'✅ Low' if fail_pct < 10 else '🚨 Critical Fix Required'} |")
    report.append("")
    report.append("### 🌐 Source Distribution & Agent Roles")
    report.append("- **Execution Sources**: " + (", ".join([f"`{k}`: {v}" for k, v in sources.items()]) if sources else "`None`"))
    report.append("- **Agent Roles**: " + (", ".join([f"`{k}`: {v}" for k, v in roles.items()]) if roles else "`None`"))
    report.append("")
    report.append("---")
    report.append("")

    # 2. Composite Scores & Trend
    report.extend(render_composite_scores_section(telemetry_data, scores_data, upstream_data))
    report.append("---")
    report.append("")

    # Rule Efficacy Dashboard (after Composite Scores)
    rule_eff_lines = render_rule_efficacy_section(rule_ledger_data, telemetry_data)
    if rule_eff_lines:
        report.extend(rule_eff_lines)
        report.append("---")
        report.append("")

    # Pruned Rules Log (after Rule Efficacy)
    pruned_lines = render_pruned_rules_section(rule_ledger_data)
    if pruned_lines:
        report.extend(pruned_lines)
        report.append("---")
        report.append("")

    # Auto-Discovered Skill Landscape (before Upstream section)
    skill_disc_lines = render_auto_discovered_skills_section(skill_map_data)
    if skill_disc_lines:
        report.extend(skill_disc_lines)
        report.append("---")
        report.append("")

    # 3. Upstream Skill Currency
    report.extend(render_upstream_currency_section(upstream_data))
    report.append("---")
    report.append("")

    # 4. Autonomous Improvements Applied
    report.extend(render_autonomous_improvements_section(changes_data))
    report.append("---")
    report.append("")

    # 5. Verification Status
    report.extend(render_verification_status_section(verification_data))
    report.append("---")
    report.append("")

    # 6. Bottleneck Analysis
    report.append("## 🚨 Bottleneck Analysis (Graphify God Nodes & Errors)")
    report.append("")
    if errors_by_type:
        report.append("### Top Error Signatures Detected")
        report.append("| Error Signature | Level | Occurrences | Impact Area |")
        report.append("| :--- | :--- | :--- | :--- |")
        for err_type, count in errors_by_type.most_common(8):
            lvl = next((e["level"] for e in all_errors if e["type"] == err_type), "B")
            impact = "Fatal / Timeout" if lvl == "A" else "Tool Friction" if lvl == "B" else "User Intent Mismatch"
            report.append(f"| **`{err_type}`** | Level {lvl} | `{count}` | {impact} |")
        report.append("")
        report.append("#### 🔍 Sample Error Snippets")
        seen_snippets = set()
        snippet_count = 0
        for err in all_errors:
            msg_key = err["message"][:80]
            if msg_key not in seen_snippets and err["message"]:
                seen_snippets.add(msg_key)
                report.append(f"- `[{err['type']}]` *(Session `{err['session']}`)*: `{err['message'][:180]}`")
                snippet_count += 1
                if snippet_count >= 5:
                    break
        report.append("")
    else:
        report.append("✅ **No critical error signatures found in the scanned logs.**")
        report.append("")

    report.append("---")
    report.append("")

    # 7. Looping & Tool Thrashing Detection
    report.append("## 🔁 Looping & Tool Thrashing Detection")
    report.append("")
    if all_loops:
        report.append(f"Detected **{len(all_loops)}** instances where tools were called repeatedly (>= 3x) on identical targets without state change:")
        report.append("")
        report.append("| Tool Name | Target / Command Sample | Repetitions | Session ID |")
        report.append("| :--- | :--- | :--- | :--- |")
        for lp in all_loops[:10]:
            clean_tgt = lp['target'].replace('\n', ' ')[:70]
            report.append(f"| **`{lp['tool']}`** | `{clean_tgt}` | `{lp['count']}x` | `{lp['session']}` |")
        report.append("")
    else:
        report.append("✅ **No excessive looping or tool thrashing patterns detected.**")
        report.append("")

    report.append("---")
    report.append("")

    # 8. Actionable Hardening Recommendations
    report.append("## 🛠️ Actionable Hardening Recommendations")
    report.append("")

    recommendations = []
    if loops_by_tool.get("list_dir", 0) > 0 or loops_by_tool.get("find_by_name", 0) > 0:
        recommendations.append(
            "1. **Exploration Loop Guard**: Agents repeatedly listed directories. Add a rule to `GEMINI.md`: *'Cache directory structure before searching; do not re-run list_dir on the same directory repeatedly without file mutations.'*"
        )

    if errors_by_type.get("NonZeroExitCode", 0) > 0 or errors_by_type.get("PythonTraceback", 0) > 0:
        recommendations.append(
            "2. **Script Pre-flight Checks**: Shell commands failed with non-zero exit codes. Ensure Python helper scripts import dependencies gracefully and use `--help` or pre-check libraries instead of trial-and-error execution."
        )

    if errors_by_type.get("RevitAPIException", 0) > 0 or top_tools.get("call_mcp_tool", 0) > 500:
        recommendations.append(
            "3. **Revit MCP Transaction Wrapping**: Batch Revit API transactions in `execute_revit_code` rather than firing hundreds of atomic calls to prevent UI lockup and memory pressure."
        )

    if any(e.get("type") == "UserCorrectionSignal" for e in all_errors):
        recommendations.append(
            "4. **Scope Alignment (Grill-Me Gate)**: User corrections were detected. Ensure the agent triggers `grill-me` on ambiguous instructions before taking exploratory actions."
        )

    if not recommendations:
        recommendations.append("1. **Baseline Maintenance**: Agent performance is within healthy thresholds. Continue monitoring logs periodically via `/agent-ci`.")

    report.extend(recommendations)
    report.append("")
    report.append("---")
    report.append("*Continuous Improvement Pipeline powered by `agent-ci` and Graphify.*")

    return "\n".join(report)


def generate_obsidian_dashboard(
    telemetry_data: dict,
    project_root: Path,
    target_logs_dir: Path,
    scores_data: dict | None = None,
    upstream_data: dict | None = None,
    changes_data: dict | None = None,
    verification_data: dict | None = None,
    rule_ledger_data: dict | None = None,
    skill_map_data: dict | None = None,
) -> str:
    """Format a clean Obsidian dashboard note for Agent-CI/Dashboard.md in Obsidian vault."""
    stats = telemetry_data.get("stats", {})
    sessions = telemetry_data.get("sessions", [])

    total_sess = stats.get("total_sessions", len(sessions))
    healthy = stats.get("healthy_sessions", 0)
    warning = stats.get("warning_sessions", 0)
    failed = stats.get("failed_sessions", 0)

    healthy_pct = round((healthy / total_sess * 100), 1) if total_sess else 0.0
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    freshness_val = float(upstream_data.get("freshness", 1.0)) if upstream_data else 1.0
    freshness_str = f"{freshness_val * 100:.1f}%"

    changes_count = changes_data.get("total_changes", 0) if changes_data else 0
    changes_commit = changes_data.get("commit_hash", "none") if changes_data else "none"

    verif_status = verification_data.get("status", "SKIPPED") if verification_data else "SKIPPED"

    lr_line = ""
    if rule_ledger_data and isinstance(rule_ledger_data, dict):
        rules = rule_ledger_data.get("rules", [])
        active_statuses = {"ACTIVE", "EFFECTIVE", "PERMANENT", "PRUNE_CANDIDATE"}
        active_rules = [
            r for r in rules
            if isinstance(r, dict) and str(r.get("status", "ACTIVE")).upper() in active_statuses
        ]
        if active_rules:
            eff_cnt = 0
            for r in active_rules:
                eff = 0.0
                measurements = r.get("measurements", [])
                if measurements and isinstance(measurements, list):
                    last_m = measurements[-1]
                    if isinstance(last_m, dict) and "efficacy" in last_m:
                        try:
                            eff = float(last_m["efficacy"])
                        except (ValueError, TypeError):
                            eff = 0.0
                elif str(r.get("status", "")).upper() in ("EFFECTIVE", "PERMANENT"):
                    eff = 1.0
                elif telemetry_data and compute_rule_efficacy:
                    try:
                        eff = compute_rule_efficacy(r, telemetry_data)
                    except Exception:
                        eff = 0.0

                if eff >= 0.3 or str(r.get("status", "")).upper() in ("EFFECTIVE", "PERMANENT"):
                    eff_cnt += 1

            lr_val = eff_cnt / len(active_rules)
            lr_line = f"> - **Rule Learning Rate**: `{lr_val * 100:.1f}%` ({eff_cnt}/{len(active_rules)} active rules effective)"

    lines = [
        "---",
        "title: Agent-CI Quality & Telemetry Dashboard",
        f"updated: {now_str}",
        "tags:",
        "  - agent-ci",
        "  - telemetry",
        "  - quality-assurance",
        "  - autonomous-hardening",
        "---",
        "",
        "# 📊 Agent Continuous Improvement (CI) Dashboard",
        "",
        "> [!abstract] Executive Snapshot",
        f"> - **Last Synchronized**: `{now_str}`",
        f"> - **Overall Session Health**: `{healthy_pct}%` ({healthy} healthy / {warning} warn / {failed} fail of {total_sess} turns)",
        f"> - **Upstream Freshness**: `{freshness_str}`",
    ]
    if lr_line:
        lines.append(lr_line)
    lines.extend([
        f"> - **Cycle Changes**: `{changes_count}` applied (Commit: `{changes_commit[:8] if changes_commit != 'none' else 'none'}`)",
        f"> - **Verification**: `{verif_status}`",
        "",
    ])

    # Composite Scores
    lines.extend(render_composite_scores_section(telemetry_data, scores_data, upstream_data))

    # Rule Efficacy Dashboard
    lines.extend(render_rule_efficacy_section(rule_ledger_data, telemetry_data))

    # Pruned Rules Log
    lines.extend(render_pruned_rules_section(rule_ledger_data))

    # Auto-Discovered Skill Landscape
    lines.extend(render_auto_discovered_skills_section(skill_map_data))

    # Upstream Skill Currency
    lines.extend(render_upstream_currency_section(upstream_data))

    # Autonomous Improvements Applied
    lines.extend(render_autonomous_improvements_section(changes_data))

    # Verification Status
    lines.extend(render_verification_status_section(verification_data))

    # Top Bottlenecks Summary
    lines.append("## 🚨 Top Bottlenecks & Friction Points")
    lines.append("")
    top_tools = stats.get("top_tools", {})
    error_types = stats.get("error_types", {})
    if error_types:
        lines.append("| Error Type | Occurrences |")
        lines.append("| :--- | :---: |")
        for err, cnt in Counter(error_types).most_common(5):
            lines.append(f"| `{err}` | `{cnt}` |")
        lines.append("")
    else:
        lines.append("✅ *No active errors detected in this evaluation window.*")
        lines.append("")

    lines.append("---")
    lines.append("## 🧭 Navigation")
    lines.append("- [[Agent-CI/Future Upgrades|Future Upgrades & Roadmap]]")
    lines.append("- [[Agent-CI/Manual Script (Zero Token Cost)|Zero Token Manual Run Guide]]")
    lines.append("- [[Agent Tools/Tools & MCP Catalog|Tools Catalog]]")
    lines.append("")
    lines.append("---")
    lines.append("*Dashboard mirrored automatically by `agent-ci v2`. Use Obsidian MCP `create_note` or vault symlink.*")
    lines.append("")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Generate CI report from telemetry data.")
    parser.add_argument("--telemetry", type=str, required=True, help="Path to telemetry JSON.")
    parser.add_argument("--logs-dir", type=str, default="", help="Target logs directory (default: ./logs or ~/.agents/logs).")
    parser.add_argument("--project-root", type=str, default="", help="Project workspace root (default: ~/.agents or current directory).")
    parser.add_argument("--scores", type=str, default=None, help="Path to score_history.json.")
    parser.add_argument("--upstream", type=str, default=None, help="Path to upstream status JSON.")
    parser.add_argument("--changes", type=str, default=None, help="Path to applied changes JSON.")
    parser.add_argument("--verification", type=str, default=None, help="Path to verification result JSON.")
    parser.add_argument("--rule-ledger", type=str, default=os.path.expanduser("~/.agents/rule_ledger.json"), help="Path to rule ledger JSON (default: ~/.agents/rule_ledger.json).")
    parser.add_argument("--skill-map", "--registry", dest="skill_map", type=str, default=os.path.expanduser("~/.agents/project-skill-map.json"), help="Path to project-skill-map.json (default: ~/.agents/project-skill-map.json).")
    parser.add_argument("--vault-dir", type=str, default="", help="Path to Obsidian vault Agent-CI directory (default: auto-detects $OBSIDIAN_VAULT_DIR or ~/Obsidian/Agent-CI).")
    parser.add_argument("--mirror-obsidian", action="store_true", help="Output/trigger Obsidian dashboard format.")
    args = parser.parse_args()

    telem_path = Path(os.path.expanduser(args.telemetry))
    if not telem_path.exists():
        print(f"Error: Telemetry file '{telem_path}' not found.", file=sys.stderr)
        sys.exit(1)

    try:
        telemetry_data = json.loads(telem_path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"Error: Failed to parse telemetry JSON from '{telem_path}': {e}", file=sys.stderr)
        sys.exit(1)

    if not isinstance(telemetry_data, dict):
        print(f"Error: Telemetry data must be a JSON object, got {type(telemetry_data).__name__}", file=sys.stderr)
        sys.exit(1)

    if args.project_root:
        project_root = Path(os.path.expanduser(args.project_root))
    elif Path("./agent-ci-reports").exists():
        project_root = Path(".")
    else:
        project_root = Path(os.path.expanduser("~/.agents"))

    if args.logs_dir:
        logs_dir = Path(os.path.expanduser(args.logs_dir))
    elif Path("./logs").exists():
        logs_dir = Path("./logs")
    else:
        logs_dir = Path(os.path.expanduser("~/.agents/logs"))

    # Load optional auxiliary data with fallbacks
    scores_fallback = Path.home() / ".agents" / "score_history.json"
    scores_data = load_optional_json(args.scores, "Scores history", fallback_path=scores_fallback)

    upstream_fallback = Path.home() / ".agents" / "upstream_status.json"
    upstream_data = load_optional_json(args.upstream, "Upstream status", fallback_path=upstream_fallback)

    changes_fallback = Path.home() / ".agents" / "applied_changes.json"
    changes_data = load_optional_json(args.changes, "Applied changes", fallback_path=changes_fallback)

    verif_fallback = Path.home() / ".agents" / "verification_result.json"
    verification_data = load_optional_json(args.verification, "Verification result", fallback_path=verif_fallback)

    rule_ledger_fallback = Path.home() / ".agents" / "rule_ledger.json"
    rule_ledger_data = load_optional_json(args.rule_ledger, "Rule ledger", fallback_path=rule_ledger_fallback)

    skill_map_fallback = Path.home() / ".agents" / "project-skill-map.json"
    skill_map_data = load_optional_json(args.skill_map, "Project skill map", fallback_path=skill_map_fallback)

    report_md = generate_markdown_report(
        telemetry_data=telemetry_data,
        project_root=project_root,
        target_logs_dir=logs_dir,
        scores_data=scores_data,
        upstream_data=upstream_data,
        changes_data=changes_data,
        verification_data=verification_data,
        rule_ledger_data=rule_ledger_data,
        skill_map_data=skill_map_data,
    )

    # Save to agent-ci-reports/
    reports_dir = project_root / "agent-ci-reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    timestamp_slug = datetime.now().strftime("%Y-%m-%d_%H-%M")
    report_file = reports_dir / f"REPORT_{timestamp_slug}.md"
    report_file.write_text(report_md, encoding="utf-8")

    print("Successfully generated Continuous Improvement Report:")
    print(f"-> {report_file.resolve()}")

    # Mirror Obsidian Dashboard if requested
    if args.mirror_obsidian:
        obsidian_md = generate_obsidian_dashboard(
            telemetry_data=telemetry_data,
            project_root=project_root,
            target_logs_dir=logs_dir,
            scores_data=scores_data,
            upstream_data=upstream_data,
            changes_data=changes_data,
            verification_data=verification_data,
            rule_ledger_data=rule_ledger_data,
            skill_map_data=skill_map_data,
        )
        obsidian_file = reports_dir / "OBSIDIAN_DASHBOARD.md"
        obsidian_file.write_text(obsidian_md, encoding="utf-8")
        print(f"-> Obsidian Dashboard mirrored to: {obsidian_file.resolve()}")

        # Ensure ~/.agents/agent-ci-reports/OBSIDIAN_DASHBOARD.md is also written
        central_reports_dir = Path(os.path.expanduser("~/.agents/agent-ci-reports"))
        central_reports_dir.mkdir(parents=True, exist_ok=True)
        central_obsidian_file = central_reports_dir / "OBSIDIAN_DASHBOARD.md"
        if central_obsidian_file.resolve() != obsidian_file.resolve():
            central_obsidian_file.write_text(obsidian_md, encoding="utf-8")
            print(f"-> Central Obsidian Dashboard mirrored to: {central_obsidian_file.resolve()}")

        # Auto-write to native Obsidian Vault directory to ensure Obsidian MCP & desktop app compatibility
        vault_targets = []
        if args.vault_dir:
            vault_targets.append(Path(os.path.expanduser(args.vault_dir)))
        else:
            env_vault = os.environ.get("OBSIDIAN_VAULT_DIR")
            if env_vault:
                vault_targets.append(Path(os.path.expanduser(env_vault)))
            else:
                default_vault = Path(os.path.expanduser("~/Obsidian/Agent-CI"))
                if default_vault.parent.exists():
                    vault_targets.append(default_vault)

        for vt in vault_targets:
            try:
                vt.mkdir(parents=True, exist_ok=True)
                target_file = vt / "Dashboard.md"
                target_file.write_text(obsidian_md, encoding="utf-8")
                print(f"-> Live Obsidian Vault note updated: {target_file.resolve()}")
            except Exception as e:
                print(f"Warning: Failed to update Obsidian vault at {vt}: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
