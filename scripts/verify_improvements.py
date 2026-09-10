#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
verify_improvements.py - Agent-CI verification engine with eval-based auto-revert.

Verifies applied improvements via automated evaluations (Python unittest or JSON eval specs).
If a score regression exceeding the allowable threshold (> 5%) is detected, automatically
executes `git revert --no-edit HEAD` and tags the commit with `ci/reverted-<timestamp>`.
"""

import os
import sys
import re
import json
import shutil
import argparse
import subprocess
from pathlib import Path
from datetime import datetime, timezone
from typing import Any


def find_skill_evals(skill_dir: Path) -> Path | None:
    """Look for evals/evals.json or evals/test_*.py within a skill directory.

    Args:
        skill_dir: Directory of the skill to inspect.

    Returns:
        Path to the eval file if found, otherwise None.
    """
    if not isinstance(skill_dir, Path):
        skill_dir = Path(skill_dir)

    if not skill_dir.exists() or not skill_dir.is_dir():
        return None

    # Check for evals/evals.json first
    evals_json = skill_dir / "evals" / "evals.json"
    if evals_json.is_file():
        return evals_json

    # Check for evals/test_*.py
    evals_dir = skill_dir / "evals"
    if evals_dir.is_dir():
        test_py_files = sorted(evals_dir.glob("test_*.py"))
        if test_py_files:
            return test_py_files[0]

    # Check fallback: evals.json in root of skill_dir
    root_evals = skill_dir / "evals.json"
    if root_evals.is_file():
        return root_evals

    # Check fallback: test_*.py in tests/ or root
    tests_dir = skill_dir / "tests"
    if tests_dir.is_dir():
        test_py_files = sorted(tests_dir.glob("test_*.py"))
        if test_py_files:
            return test_py_files[0]

    root_tests = sorted(skill_dir.glob("test_*.py"))
    if root_tests:
        return root_tests[0]

    return None


def _run_python_test(test_file: Path) -> dict[str, Any]:
    """Execute a Python test file via unittest and parse results.

    Args:
        test_file: Path to the Python test file.

    Returns:
        Dict with 'passed', 'total', 'pass_rate', and details.
    """
    test_file = test_file.resolve()
    test_dir = test_file.parent

    env = dict(os.environ)
    curr_pythonpath = env.get("PYTHONPATH", "")
    additional_paths = [str(test_dir), str(test_dir.parent)]
    new_pythonpath = os.pathsep.join(additional_paths + ([curr_pythonpath] if curr_pythonpath else []))
    env["PYTHONPATH"] = new_pythonpath

    cmd = [sys.executable, "-m", "unittest", str(test_file)]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(test_dir),
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
        output = proc.stderr + "\n" + proc.stdout
    except subprocess.TimeoutExpired:
        return {
            "passed": 0,
            "total": 1,
            "pass_rate": 0.0,
            "error": "Test execution timed out after 60s",
        }
    except Exception as e:
        return {
            "passed": 0,
            "total": 1,
            "pass_rate": 0.0,
            "error": f"Failed to execute test: {e}",
        }

    # Parse unittest output format
    ran_match = re.search(r"Ran\s+(\d+)\s+tests?", output)
    total = int(ran_match.group(1)) if ran_match else 0

    if proc.returncode == 0:
        passed = total
    else:
        fail_match = re.search(r"failures=(\d+)", output)
        err_match = re.search(r"errors=(\d+)", output)
        failures = int(fail_match.group(1)) if fail_match else 0
        errors = int(err_match.group(1)) if err_match else 0
        failed_count = failures + errors
        if failed_count == 0 and total == 0:
            total = 1
            passed = 0
        else:
            passed = max(0, total - failed_count)

    pass_rate = round(passed / total, 4) if total > 0 else 0.0
    return {
        "passed": passed,
        "total": total,
        "pass_rate": pass_rate,
        "details": output.strip()[:1000],
    }


def _run_json_eval(eval_file: Path) -> dict[str, Any]:
    """Evaluate a JSON eval specification (evals/evals.json).

    Args:
        eval_file: Path to JSON eval specification.

    Returns:
        Dict with 'passed', 'total', 'pass_rate', and details.
    """
    eval_file = eval_file.resolve()
    skill_dir = eval_file.parent.parent

    try:
        with open(eval_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        return {
            "passed": 0,
            "total": 0,
            "pass_rate": 0.0,
            "error": f"Failed to parse JSON eval spec: {e}",
        }

    # Check for direct pre-computed results
    if "passed" in data and "total" in data:
        passed = int(data["passed"])
        total = int(data["total"])
        pass_rate = float(data.get("pass_rate", round(passed / total, 4) if total > 0 else 0.0))
        return {"passed": passed, "total": total, "pass_rate": pass_rate}

    # Extract eval items
    eval_cases = data.get("evals") or data.get("test_cases") or data.get("cases")
    if eval_cases is None and isinstance(data, list):
        eval_cases = data
    elif eval_cases is None:
        eval_cases = [data]

    total = len(eval_cases)
    if total == 0:
        return {"passed": 0, "total": 0, "pass_rate": 1.0}

    # Check if skill-creator eval runner exists
    quick_validate_script = None
    candidate_runners = [
        skill_dir.parent / "skill-creator" / "scripts" / "quick_validate.py",
        Path.home() / ".agents" / "skills" / "skill-creator" / "scripts" / "quick_validate.py",
        Path.home() / ".gemini" / "config" / "skills" / "skill-creator" / "scripts" / "quick_validate.py",
    ]
    for runner in candidate_runners:
        if runner.exists() and runner.is_file():
            quick_validate_script = runner
            break

    quick_validate_ok = True
    if quick_validate_script and (skill_dir / "SKILL.md").exists():
        try:
            env = dict(os.environ)
            env["PYTHONPATH"] = str(quick_validate_script.parent.parent)
            v_res = subprocess.run(
                [sys.executable, str(quick_validate_script), str(skill_dir)],
                cwd=str(skill_dir),
                capture_output=True,
                text=True,
                timeout=30,
                env=env,
            )
            quick_validate_ok = (v_res.returncode == 0)
        except Exception:
            quick_validate_ok = True  # Graceful fallback

    # Built-in check for SKILL.md validity (pure stdlib)
    skill_md = skill_dir / "SKILL.md"
    skill_md_valid = False
    if skill_md.exists() and skill_md.is_file():
        try:
            skill_content = skill_md.read_text(encoding="utf-8")
            fm_match = re.match(r"^---\n(.*?)\n---", skill_content, re.DOTALL)
            if fm_match:
                fm_text = fm_match.group(1)
                skill_md_valid = ("name:" in fm_text and "description:" in fm_text)
        except Exception:
            skill_md_valid = False

    passed = 0
    for case in eval_cases:
        if not isinstance(case, dict):
            if skill_md_valid and quick_validate_ok:
                passed += 1
            continue

        if "passed" in case:
            if bool(case["passed"]):
                passed += 1
            continue

        if "status" in case:
            if str(case["status"]).upper() in ("PASS", "PASSED", "OK", "SUCCESS"):
                passed += 1
            continue

        case_passed = True

        # Check required files if specified
        case_files = case.get("files", [])
        if isinstance(case_files, list):
            for cf in case_files:
                target_f = skill_dir / cf
                if not target_f.exists():
                    case_passed = False
                    break

        # Check assertions if specified
        assertions = case.get("assertions", [])
        if isinstance(assertions, list):
            for assertion in assertions:
                if isinstance(assertion, dict):
                    if "file" in assertion and "contains" in assertion:
                        target_f = skill_dir / assertion["file"]
                        if not target_f.exists() or assertion["contains"] not in target_f.read_text(encoding="utf-8"):
                            case_passed = False
                            break
                    elif "file" in assertion and "regex" in assertion:
                        target_f = skill_dir / assertion["file"]
                        if not target_f.exists() or not re.search(assertion["regex"], target_f.read_text(encoding="utf-8")):
                            case_passed = False
                            break
                    elif "file" in assertion and assertion.get("exists") is True:
                        target_f = skill_dir / assertion["file"]
                        if not target_f.exists():
                            case_passed = False
                            break
                elif isinstance(assertion, bool) and not assertion:
                    case_passed = False
                    break

        # Check expectations (require valid skill manifest)
        expectations = case.get("expectations", [])
        if expectations and not (skill_md_valid and quick_validate_ok):
            case_passed = False

        if case_passed:
            passed += 1

    pass_rate = round(passed / total, 4) if total > 0 else 0.0
    return {
        "passed": passed,
        "total": total,
        "pass_rate": pass_rate,
    }


def run_skill_eval(eval_file: Path) -> dict[str, Any]:
    """Execute evaluation for a skill file (Python test or JSON eval spec).

    Args:
        eval_file: Path to eval file.

    Returns:
        Dict with 'passed' (int), 'total' (int), 'pass_rate' (float).
    """
    if not isinstance(eval_file, Path):
        eval_file = Path(eval_file)

    if not eval_file.exists() or not eval_file.is_file():
        return {
            "passed": 0,
            "total": 0,
            "pass_rate": 0.0,
            "error": f"Eval file not found: {eval_file}",
        }

    suffix = eval_file.suffix.lower()
    if suffix == ".py":
        return _run_python_test(eval_file)
    elif suffix == ".json":
        return _run_json_eval(eval_file)
    else:
        return {
            "passed": 0,
            "total": 0,
            "pass_rate": 0.0,
            "error": f"Unsupported eval file type '{suffix}': {eval_file}",
        }


def auto_revert(repo_dir: Path, reason: str, dry_run: bool = False) -> dict[str, Any]:
    """Execute git revert --no-edit HEAD and tag with ci/reverted-<timestamp>.

    Args:
        repo_dir: Path to git repository.
        reason: Explanation for the regression revert.
        dry_run: If True, do not mutate git state.

    Returns:
        Dict with 'reverted' (bool), 'reason' (str), and optional details.
    """
    repo_path = Path(repo_dir).resolve()
    timestamp_str = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    tag_name = f"ci/reverted-{timestamp_str}"

    if dry_run:
        return {
            "reverted": True,
            "reason": reason,
            "dry_run": True,
            "tag": tag_name,
            "details": f"[DRY-RUN] Would execute 'git revert --no-edit HEAD' and create tag '{tag_name}'",
        }

    # Verify repository has commits
    head_check = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo_path),
        capture_output=True,
        text=True,
    )
    if head_check.returncode != 0:
        return {
            "reverted": False,
            "reason": reason,
            "error": "Cannot revert: repository has no commits or invalid HEAD",
        }

    head_commit = head_check.stdout.strip()

    # Execute git revert --no-edit HEAD
    revert_proc = subprocess.run(
        ["git", "revert", "--no-edit", "HEAD"],
        cwd=str(repo_path),
        capture_output=True,
        text=True,
    )

    if revert_proc.returncode != 0:
        # Abort revert to preserve clean tree
        subprocess.run(
            ["git", "revert", "--abort"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
        )
        return {
            "reverted": False,
            "reason": reason,
            "error": f"git revert failed: {revert_proc.stderr.strip()}",
        }

    # Create tag ci/reverted-<timestamp>
    tag_proc = subprocess.run(
        ["git", "tag", tag_name],
        cwd=str(repo_path),
        capture_output=True,
        text=True,
    )
    tag_created = (tag_proc.returncode == 0)

    # Get new HEAD hash
    new_head_proc = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo_path),
        capture_output=True,
        text=True,
    )
    new_head = new_head_proc.stdout.strip() if new_head_proc.returncode == 0 else "UNKNOWN"

    return {
        "reverted": True,
        "reason": reason,
        "reverted_commit": head_commit,
        "new_head": new_head,
        "tag": tag_name if tag_created else None,
        "dry_run": False,
    }


def extract_modified_skills(
    changes_data: dict | list | None,
    repo_dir: Path,
    registry_path: Path | None = None,
) -> list[tuple[str, Path | None]]:
    """Resolve distinct modified skill names and directories from changes data or git HEAD.

    Args:
        changes_data: Changes structure from apply_improvements or None.
        repo_dir: Path to repository.
        registry_path: Optional path to project-skill-map.json.

    Returns:
        List of tuples: (skill_name, skill_dir_or_None).
    """
    repo_path = Path(repo_dir).resolve()

    # Load registry if available
    registry = {}
    reg_candidates = [
        registry_path,
        repo_path / "project-skill-map.json",
        Path.home() / ".agents" / "project-skill-map.json",
    ]
    for rc in reg_candidates:
        if rc and Path(rc).exists() and Path(rc).is_file():
            try:
                with open(rc, "r", encoding="utf-8") as f:
                    registry = json.load(f)
                break
            except Exception:
                pass

    # Extract items
    items = []
    if isinstance(changes_data, dict):
        items = (
            changes_data.get("changes_applied")
            or changes_data.get("modified_skills")
            or changes_data.get("changes")
            or []
        )
        if not items and "target" in changes_data:
            items = [changes_data]
    elif isinstance(changes_data, list):
        items = changes_data

    # Fallback to inspecting git HEAD if no items provided
    if not items:
        git_diff = subprocess.run(
            ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
        )
        if git_diff.returncode == 0 and git_diff.stdout.strip():
            for line in git_diff.stdout.splitlines():
                if line.strip():
                    items.append({"target": line.strip()})

    seen: set[str] = set()
    results: list[tuple[str, Path | None]] = []

    for item in items:
        target = ""
        if isinstance(item, dict):
            target = item.get("target") or item.get("skill") or item.get("name") or ""
        elif isinstance(item, str):
            target = item

        if not target:
            continue

        # Match against project-skill-map registry scopes
        matched_from_registry = False
        for mapping in registry.get("mappings", []):
            if mapping.get("project_name") == target:
                matched_from_registry = True
                for sp in mapping.get("skill_paths", []):
                    p = Path(os.path.expanduser(sp))
                    skill_dir = p.parent if (p.is_file() or p.name.endswith(".md")) else p
                    s_name = skill_dir.name
                    if s_name not in seen:
                        seen.add(s_name)
                        results.append((s_name, skill_dir if skill_dir.is_dir() else None))

        if matched_from_registry:
            continue

        # Check if target contains skills/<name>
        skills_match = re.search(r"(?:^|[/\\])skills[/\\]([^/\\]+)", target)
        if skills_match:
            s_name = skills_match.group(1)
            s_dir = repo_path / "skills" / s_name
            if not s_dir.is_dir():
                # Check absolute or relative directly
                cand = Path(os.path.expanduser(target))
                if cand.is_file():
                    s_dir = cand.parent
                elif cand.is_dir():
                    s_dir = cand
            if s_name not in seen:
                seen.add(s_name)
                results.append((s_name, s_dir if s_dir.is_dir() else None))
            continue

        # Global rule target (GEMINI.md)
        if "GEMINI.md" in target or target.lower() == "global":
            s_name = "global (GEMINI.md)"
            if s_name not in seen:
                seen.add(s_name)
                results.append((s_name, None))
            continue

        # Target might be skill name directly in repo_path / skills
        direct_dir = repo_path / "skills" / target
        if direct_dir.is_dir():
            if target not in seen:
                seen.add(target)
                results.append((target, direct_dir))
            continue

        # Direct path check
        p = Path(os.path.expanduser(target))
        if not p.is_absolute():
            p = (repo_path / target).resolve()

        if p.is_file() and p.name == "SKILL.md":
            s_name = p.parent.name
            if s_name not in seen:
                seen.add(s_name)
                results.append((s_name, p.parent))
        elif p.is_dir() and (p / "SKILL.md").exists():
            s_name = p.name
            if s_name not in seen:
                seen.add(s_name)
                results.append((s_name, p))
        else:
            # Fallback entry
            if target not in seen:
                seen.add(target)
                results.append((target, None))

    return results


def check_rule_lifecycle(
    ledger_path: Path | str | None = None,
    telemetry_data: dict | None = None,
    dry_run: bool = False,
) -> tuple[list[dict[str, Any]], float]:
    """Inspect rule ledger to identify rules pending prune and compute learning rate.

    Args:
        ledger_path: Path to rule ledger JSON file.
        telemetry_data: Optional telemetry data dict for live measurement updates.
        dry_run: If True, do not persist rule measurement updates.

    Returns:
        Tuple of (rules_pending_prune, learning_rate).
    """
    if ledger_path is None:
        target_path = Path.home() / ".agents" / "rule_ledger.json"
    else:
        target_path = Path(os.path.expanduser(str(ledger_path))).resolve()

    rules_pending_prune: list[dict[str, Any]] = []
    learning_rate = 1.0

    if not target_path.exists() or not target_path.is_file():
        return rules_pending_prune, learning_rate

    ledger: dict[str, Any] = {"rules": [], "pruned": [], "evolved": []}
    if telemetry_data and isinstance(telemetry_data, dict):
        try:
            from score_engine import update_rule_measurements
            if not dry_run:
                ledger = update_rule_measurements(target_path, telemetry_data)
            else:
                content = target_path.read_text(encoding="utf-8")
                if content.strip():
                    ledger = json.loads(content)
        except Exception:
            try:
                content = target_path.read_text(encoding="utf-8")
                if content.strip():
                    ledger = json.loads(content)
            except Exception as e:
                print(f"Warning: Failed to read rule ledger '{target_path}': {e}", file=sys.stderr)
                return rules_pending_prune, learning_rate
    else:
        try:
            content = target_path.read_text(encoding="utf-8")
            if content.strip():
                ledger = json.loads(content)
        except Exception as e:
            print(f"Warning: Failed to read rule ledger '{target_path}': {e}", file=sys.stderr)
            return rules_pending_prune, learning_rate

    if not isinstance(ledger, dict):
        return rules_pending_prune, learning_rate

    rules = ledger.get("rules", [])
    if not isinstance(rules, list) or not rules:
        return rules_pending_prune, 1.0

    active_statuses = {"ACTIVE", "EFFECTIVE", "PERMANENT", "PRUNE_CANDIDATE"}
    active_rules: list[dict[str, Any]] = []
    effective_count = 0

    for rule in rules:
        if not isinstance(rule, dict):
            continue
        status = str(rule.get("status", "ACTIVE")).upper()
        if status not in active_statuses:
            continue
        active_rules.append(rule)

        eff = 0.0
        measurements = rule.get("measurements", [])
        if measurements and isinstance(measurements, list):
            last_m = measurements[-1]
            if isinstance(last_m, dict) and "efficacy" in last_m:
                try:
                    eff = float(last_m["efficacy"])
                except (ValueError, TypeError):
                    eff = 0.0
        elif status in ("EFFECTIVE", "PERMANENT"):
            eff = 1.0
        elif telemetry_data:
            try:
                from score_engine import compute_rule_efficacy
                eff = compute_rule_efficacy(rule, telemetry_data)
            except Exception:
                eff = 0.0

        if eff >= 0.3 or status in ("EFFECTIVE", "PERMANENT"):
            effective_count += 1

        if status == "PRUNE_CANDIDATE":
            rules_pending_prune.append(dict(rule))

    if active_rules:
        learning_rate = round(effective_count / len(active_rules), 4)
    else:
        learning_rate = 1.0

    return rules_pending_prune, learning_rate


def verify_cycle(
    changes_data: dict | list | None,
    history_path: Path,
    threshold: float = 0.05,
    dry_run: bool = False,
    repo_dir: Path | None = None,
    rule_ledger_path: Path | str | None = None,
    telemetry_data: dict | None = None,
) -> dict[str, Any]:
    """Verify applied improvements via automated evals and auto-revert on regression.

    Args:
        changes_data: Dict or list with applied changes (from apply_improvements.py).
        history_path: Path to score_history.json.
        threshold: Maximum allowable pass rate drop before auto-revert (default: 0.05).
        dry_run: If True, do not mutate git or history state.
        repo_dir: Optional path to repository (default: ~/.agents).
        rule_ledger_path: Optional path to rule_ledger.json.
        telemetry_data: Optional telemetry data dict.

    Returns:
        Dict adhering to verification engine output JSON schema:
        {
          "timestamp": str,
          "status": "PASS|REVERTED|SKIPPED",
          "skills_evaluated": [...],
          "revert_action": dict | None,
          "details": str,
          "rules_pending_prune": [...],
          "learning_rate": float
        }
    """
    if repo_dir is None:
        repo_dir = Path("~/.agents").expanduser().resolve()
    else:
        repo_dir = Path(os.path.expanduser(str(repo_dir))).resolve()

    history_path = Path(os.path.expanduser(str(history_path))).resolve()

    # Rule lifecycle check
    rules_pending_prune, learning_rate = check_rule_lifecycle(
        ledger_path=rule_ledger_path,
        telemetry_data=telemetry_data,
        dry_run=dry_run,
    )

    # Load history
    history: dict[str, Any] = {"cycles": [], "eval_baselines": {}}
    if history_path.exists() and history_path.is_file():
        try:
            with open(history_path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                if isinstance(loaded, dict):
                    history = loaded
        except Exception as e:
            print(f"Warning: Failed to load history '{history_path}': {e}", file=sys.stderr)

    eval_baselines: dict[str, float] = history.get("eval_baselines", {})

    # Extract modified skills
    modified_skills = extract_modified_skills(changes_data, repo_dir=repo_dir)

    if not modified_skills:
        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "status": "SKIPPED",
            "skills_evaluated": [],
            "revert_action": None,
            "details": "No skill modifications detected to verify",
            "rules_pending_prune": rules_pending_prune,
            "learning_rate": learning_rate,
        }

    skills_evaluated: list[dict[str, Any]] = []
    regression_detected = False
    revert_reason = ""
    revert_action = None

    for skill_name, skill_dir in modified_skills:
        if skill_dir is None:
            # No directory found or global rule file
            msg = "accepted (no automated evals found)"
            print(f"Notice: Skill '{skill_name}' - {msg}")
            skills_evaluated.append({
                "skill": skill_name,
                "eval_file": None,
                "status": "SKIPPED",
                "reason": msg,
            })
            continue

        eval_file = find_skill_evals(skill_dir)
        if eval_file is None:
            msg = "accepted (no automated evals found)"
            print(f"Notice: Skill '{skill_name}' at '{skill_dir}' - {msg}")
            skills_evaluated.append({
                "skill": skill_name,
                "eval_file": None,
                "status": "SKIPPED",
                "reason": msg,
            })
            continue

        # Run eval
        eval_result = run_skill_eval(eval_file)
        pass_rate = float(eval_result.get("pass_rate", 0.0))
        passed_count = int(eval_result.get("passed", 0))
        total_count = int(eval_result.get("total", 0))

        # Determine baseline
        if skill_name in eval_baselines:
            baseline = float(eval_baselines[skill_name])
        else:
            baseline = 1.0  # Default expected baseline pass rate (100%)

        drop = round(baseline - pass_rate, 4)

        if drop > threshold:
            regression_detected = True
            revert_reason = (
                f"Eval regression in skill '{skill_name}': pass rate dropped from "
                f"{baseline:.2%} to {pass_rate:.2%} (regression {drop:.2%} > threshold {threshold:.2%})"
            )
            eval_entry = {
                "skill": skill_name,
                "eval_file": str(eval_file),
                "status": "REGRESSION",
                "passed": passed_count,
                "total": total_count,
                "pass_rate": pass_rate,
                "baseline": baseline,
                "score_drop": drop,
                "reason": revert_reason,
                "eval_result": eval_result,
            }
            skills_evaluated.append(eval_entry)
            print(f"ALERT: {revert_reason}", file=sys.stderr)

            # Trigger auto-revert
            revert_action = auto_revert(repo_dir=repo_dir, reason=revert_reason, dry_run=dry_run)
            break
        else:
            eval_entry = {
                "skill": skill_name,
                "eval_file": str(eval_file),
                "status": "PASS",
                "passed": passed_count,
                "total": total_count,
                "pass_rate": pass_rate,
                "baseline": baseline,
                "score_drop": drop,
                "eval_result": eval_result,
            }
            skills_evaluated.append(eval_entry)
            print(f"Verification PASS: Skill '{skill_name}' pass rate {pass_rate:.2%} (baseline {baseline:.2%}, drop {drop:.2%})")
            eval_baselines[skill_name] = pass_rate

    # Compute overall cycle status
    if regression_detected:
        cycle_status = "REVERTED"
        details = revert_reason
    else:
        has_passed = any(s.get("status") == "PASS" for s in skills_evaluated)
        if has_passed:
            cycle_status = "PASS"
            details = "All evaluated skills passed verification without regression"
        else:
            cycle_status = "SKIPPED"
            details = "All changes accepted (no automated evals found for modified skills)"

    # Update history baseline if not dry_run and no regression
    if not dry_run and not regression_detected and eval_baselines:
        history["eval_baselines"] = eval_baselines
        try:
            history_path.parent.mkdir(parents=True, exist_ok=True)
            with open(history_path, "w", encoding="utf-8") as f:
                json.dump(history, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"Warning: Failed to update history '{history_path}': {e}", file=sys.stderr)

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": cycle_status,
        "skills_evaluated": skills_evaluated,
        "revert_action": revert_action,
        "details": details,
        "rules_pending_prune": rules_pending_prune,
        "learning_rate": learning_rate,
    }


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments for verify_improvements."""
    parser = argparse.ArgumentParser(
        description="Verify applied Agent-CI improvements via automated evals and auto-revert on regression."
    )
    parser.add_argument(
        "--changes",
        type=str,
        default=None,
        help="Path to JSON file with changes applied (from apply_improvements.py).",
    )
    parser.add_argument(
        "--history",
        type=str,
        default="~/.agents/score_history.json",
        help="Path to score_history.json (default: ~/.agents/score_history.json).",
    )
    parser.add_argument(
        "--eval-threshold",
        type=float,
        default=0.05,
        help="Maximum allowable regression before auto-revert (default: 0.05).",
    )
    parser.add_argument(
        "--target-repo",
        type=str,
        default="~/.agents",
        help="Path to git repository to verify (default: ~/.agents).",
    )
    parser.add_argument(
        "--rule-ledger",
        type=str,
        default="~/.agents/rule_ledger.json",
        help="Path to rule ledger JSON (default: ~/.agents/rule_ledger.json).",
    )
    parser.add_argument(
        "--telemetry",
        type=str,
        default=None,
        help="Path to telemetry JSON for live measurement check (optional).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Test verification flow without executing git revert.",
    )
    parser.add_argument(
        "--output-json",
        type=str,
        default=None,
        help="Optional path to write verification output JSON.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON to stdout.",
    )
    return parser.parse_args()


def main() -> int:
    """Entry point for CLI execution."""
    args = parse_args()

    repo_dir = Path(os.path.expanduser(args.target_repo)).resolve()
    history_path = Path(os.path.expanduser(args.history)).resolve()

    changes_data = None
    if args.changes:
        changes_path = Path(os.path.expanduser(args.changes)).resolve()
        if changes_path.exists() and changes_path.is_file():
            try:
                with open(changes_path, "r", encoding="utf-8") as f:
                    changes_data = json.load(f)
            except Exception as e:
                print(f"Error: Failed to load changes file '{changes_path}': {e}", file=sys.stderr)
                return 1
        else:
            print(f"Warning: Changes file '{changes_path}' not found; falling back to git HEAD diff.", file=sys.stderr)

    telemetry_data = None
    if args.telemetry:
        telem_p = Path(os.path.expanduser(args.telemetry)).resolve()
        if telem_p.exists() and telem_p.is_file():
            try:
                with open(telem_p, "r", encoding="utf-8") as f:
                    telemetry_data = json.load(f)
            except Exception as e:
                print(f"Warning: Failed to load telemetry from '{telem_p}': {e}", file=sys.stderr)

    result = verify_cycle(
        changes_data=changes_data,
        history_path=history_path,
        threshold=args.eval_threshold,
        dry_run=args.dry_run,
        repo_dir=repo_dir,
        rule_ledger_path=args.rule_ledger,
        telemetry_data=telemetry_data,
    )

    # Write output JSON if requested
    if args.output_json:
        out_path = Path(os.path.expanduser(args.output_json)).resolve()
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(result, f, indent=2, ensure_ascii=False)
            print(f"Verification output JSON written to: {out_path}")
        except Exception as e:
            print(f"Warning: Failed to write output JSON to '{out_path}': {e}", file=sys.stderr)

    # Print results
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        mode_str = "DRY RUN (preview only)" if args.dry_run else "LIVE EXECUTION"
        print("\n" + "=" * 60)
        print(" Agent-CI Verification Engine Summary")
        print("=" * 60)
        print(f" Mode            : {mode_str}")
        print(f" Status          : {result['status']}")
        print(f" Timestamp       : {result['timestamp']}")
        print(f" Target Repo     : {repo_dir}")
        print(f" Threshold       : {args.eval_threshold:.2%}")
        print(f" Details         : {result['details']}")
        if "learning_rate" in result:
            lr_val = result.get("learning_rate", 1.0)
            pending_cnt = len(result.get("rules_pending_prune", []))
            print(f" Learning Rate   : {lr_val * 100:.1f}%")
            print(f" Pending Prune   : {pending_cnt} rule(s)")
        if result["revert_action"]:
            rev = result["revert_action"]
            print(f" Revert Action   : Reverted={rev.get('reverted')}, Tag={rev.get('tag')}, Commit={rev.get('commit', rev.get('new_head', 'N/A'))}")
        print("-" * 60)
        print(f" Evaluated Skills ({len(result['skills_evaluated'])}):")
        for item in result["skills_evaluated"]:
            s_name = item.get("skill", "unknown")
            s_status = item.get("status", "UNKNOWN")
            if s_status == "PASS":
                p = item.get("passed", 0)
                t = item.get("total", 0)
                pr = item.get("pass_rate", 0.0)
                base = item.get("baseline", 1.0)
                print(f"  [+] {s_name}: PASS ({p}/{t} passed, rate: {pr:.2%}, baseline: {base:.2%})")
            elif s_status == "REGRESSION":
                p = item.get("passed", 0)
                t = item.get("total", 0)
                pr = item.get("pass_rate", 0.0)
                drop = item.get("score_drop", 0.0)
                print(f"  [-] {s_name}: REGRESSION ({p}/{t} passed, rate: {pr:.2%}, drop: {drop:.2%})")
            else:
                reason = item.get("reason", "no evals")
                print(f"  [o] {s_name}: SKIPPED ({reason})")
        print("=" * 60 + "\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
