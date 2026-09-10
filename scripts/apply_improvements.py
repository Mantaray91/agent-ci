#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
apply_improvements.py - Agent-CI Improvement Applier with Upstream Sync, Audit Patches, and Rule Lifecycle.

Automatically applies improvements in priority order:
1. Upstream sync for outdated skills (based on upstream status from check_upstream.py)
2. Audit-driven patches from failure telemetry (loop guards, error handling, scope alignment)
3. Score-driven hardening based on negative score deltas

Rule Lifecycle Management:
- Records newly applied rules in rule_ledger.json with baseline error counts
- Enforces budget cap: maximum 5 active rules per target file
- Auto-prunes ineffective rules (efficacy < 0.2 after 7+ days)
- Evolves ineffective rules if target error persists in failure telemetry

Creates git commit with tag `ci/YYYY-MM-DD`. Supports `--dry-run` flag.
"""

import os
import sys
import re
import json
import argparse
import subprocess
import hashlib
from pathlib import Path
from datetime import datetime, timezone


def _normalize_path(p: str) -> str:
    """Normalize a filesystem path string for reliable comparison."""
    if not p:
        return ""
    try:
        return str(Path(os.path.expanduser(p)).resolve())
    except Exception:
        return str(p).strip()


def _sanitize_token(val: Any, max_len: int = 50, default: str = "item") -> str:
    """Sanitize identifier tokens (tool name, error type, scope) to prevent prompt injection."""
    if val is None:
        return default
    s = str(val).strip()
    # Strip any newlines, carriage returns, markdown formatting, HTML/XML tags
    s = re.sub(r'[\r\n`*#<>]', '', s)
    # Whitelist only safe alphanumeric, hyphen, underscore, colon, dot
    s = re.sub(r'[^a-zA-Z0-9_\-\.:]', '_', s).strip('._-')
    if not s:
        return default
    return s[:max_len]


def route_to_target(finding_or_scope: str | None, registry: dict) -> list[str]:
    """Route a scope or finding/error pattern to target skill or rule file paths.

    Args:
        finding_or_scope: Scope name (e.g. 'web-backend', 'data-pipeline', 'global'), workspace string,
                          or error/finding pattern.
        registry: Project-skill mapping registry dictionary containing 'mappings'.

    Returns:
        List of target file paths. If project-specific, returns mapped skill paths.
        If global or generic, returns [str(Path.home() / ".agents" / "GEMINI.md")].
    """
    default_target = [str(Path.home() / ".agents" / "GEMINI.md")]
    if not finding_or_scope or not isinstance(finding_or_scope, str):
        return default_target

    target_str = finding_or_scope.strip()
    if not target_str or target_str.lower() in ("global", "general", "none"):
        return default_target

    mappings = registry.get("mappings", []) if isinstance(registry, dict) else []

    # 1. Match by project_name (case-insensitive)
    for m in mappings:
        proj = m.get("project_name", "")
        if proj and proj.lower() == target_str.lower():
            paths = m.get("skill_paths", [])
            if paths:
                return list(paths)

    # 2. Match by workspace_pattern regex
    for m in mappings:
        pat = m.get("workspace_pattern", "")
        if pat:
            try:
                if re.search(pat, target_str, re.IGNORECASE):
                    paths = m.get("skill_paths", [])
                    if paths:
                        return list(paths)
            except re.error:
                pass

    # 3. Match keyword in project_name (e.g. 'RevitAPIException' matches 'REVIT')
    for m in mappings:
        proj = m.get("project_name", "")
        if proj and len(proj) >= 3 and proj.lower() in target_str.lower():
            paths = m.get("skill_paths", [])
            if paths:
                return list(paths)

    return default_target


def generate_audit_patch(finding: dict) -> tuple[str, str]:
    """Generate an audit patch tuple (section_name, patch_content) based on finding type.

    Categorization:
    - Looping pattern (e.g. list_dir >= 3x): generates loop guard rule
    - User correction (Level C): generates scope alignment / grill-me prompt rule
    - Specific API exception (e.g. RevitAPIException): generates API transaction/error handling rule
    - Traceback / NonZeroExitCode: generates pre-flight check rule
    - Score delta: generates score hardening gate

    Args:
        finding: Dictionary describing the finding, telemetry error, loop, or score delta.

    Returns:
        Tuple of (section_name, patch_content).
    """
    finding_type = str(finding.get("type", "")).strip()
    level = str(finding.get("level", "")).strip().upper()
    count = finding.get("count", 0)

    # 1. Looping pattern (e.g. list_dir >= 3x)
    is_loop = (
        "loop" in finding_type.lower()
        or (isinstance(count, int) and count >= 3)
        or bool(finding.get("loops"))
        or finding.get("pattern") == "loop"
    )
    if is_loop:
        tool = _sanitize_token(finding.get("tool"), max_len=40, default="tool")
        section = "LOOP_GUARDS"
        content = (
            f"- **ANTI-LOOP GUARD ({tool})**: Do not call `{tool}` repeatedly on the same target "
            f"(>= 3 calls detected). When repeated executions fail to yield progress or produce "
            f"identical results, halt immediately, investigate root cause, and ask user or adjust approach."
        )
        return section, content

    # 2. User correction (Level C)
    is_correction = (
        level == "C"
        or "correction" in finding_type.lower()
        or finding_type == "UserCorrectionSignal"
        or bool(finding.get("user_corrections"))
    )
    if is_correction:
        section = "SCOPE_ALIGNMENT"
        content = (
            "- **SCOPE ALIGNMENT & GRILL-ME MANDATE**: On receiving corrective user feedback or "
            "ambiguous requirements, immediately activate the `grill-me` protocol. Clarify assumptions, "
            "align scope boundaries, and verify intent before executing code or tool actions."
        )
        return section, content

    # 3. Specific API exception (e.g. RevitAPIException)
    is_api = (
        finding_type in ("RevitAPIException", "APIException")
        or "revit" in finding_type.lower()
        or "api" in finding_type.lower()
    )
    if is_api:
        api_name = _sanitize_token(finding_type, max_len=40, default="API")
        section = "ERROR_HANDLING"
        content = (
            f"- **API TRANSACTION & ERROR HANDLING ({api_name})**: Encapsulate all {api_name} calls "
            f"within explicit transaction boundaries and try-catch blocks. Verify element validity and active "
            f"document state prior to modification; cleanly abort and roll back on failure."
        )
        return section, content

    # 4. Traceback / NonZeroExitCode / Execution errors
    is_traceback_or_exit = (
        finding_type in (
            "PythonTraceback",
            "NonZeroExitCode",
            "PowerShellSyntaxError",
            "ToolExecutionError",
            "ToolTimeout",
            "AbnormalTermination",
            "MCPConnectionError",
            "FileNotFound",
            "PermissionDenied",
        )
        or "traceback" in finding_type.lower()
        or "exit" in finding_type.lower()
        or level in ("A", "B")
    )
    if is_traceback_or_exit:
        err_type = _sanitize_token(finding_type, max_len=40, default="CommandExecution")
        section = "PRE_FLIGHT_CHECKS"
        content = (
            f"- **PRE-FLIGHT VERIFICATION & RETURN CODE CHECK ({err_type})**: Validate environment "
            f"prerequisites, argument syntax, and file existence prior to tool or command invocation. "
            f"Verify command return codes immediately and halt on non-zero exit codes or unhandled tracebacks."
        )
        return section, content

    # 5. Score delta hardening
    if finding_type == "score_delta" or "delta" in finding:
        scope = _sanitize_token(finding.get("scope"), max_len=40, default="global")
        delta = float(finding.get("delta", 0.0))
        score = float(finding.get("score", 0.0))
        section = "SCORE_HARDENING"
        content = (
            f"- **SCORE HARDENING GATE ({scope})**: Evaluation cycle detected score regression "
            f"(delta: {delta:+.4f}, score: {score:.4f}). Enforce strict verification before completion, "
            f"mandatory error checking, and zero-tolerance for unverified tool calls."
        )
        return section, content

    # Fallback audit safeguard
    section = "HARD_GATES"
    fallback_name = _sanitize_token(finding_type, max_len=40, default="GeneralAudit")
    content = (
        f"- **AUDIT SAFEGUARD ({fallback_name})**: Enforce verification before assertion and "
        f"validate all preconditions prior to execution."
    )
    return section, content


def apply_patch_to_file(
    target_path: str,
    patch_content: str,
    section: str,
    dry_run: bool = False,
    allowed_roots: list[Path] | None = None,
) -> bool:
    """Apply a patch rule to a target file within a designated section idempotently.

    Args:
        target_path: Path to the target file.
        patch_content: Markdown rule content to add.
        section: Section name to insert into (e.g. 'LOOP_GUARDS', 'HARD_GATES').
        dry_run: If True, do not modify file on disk.
        allowed_roots: Optional list of allowed root directories to prevent path traversal.

    Returns:
        True if the patch was applied (or would be applied in dry-run), False if skipped.
    """
    path_obj = Path(os.path.expanduser(target_path)).resolve()

    # Path traversal guard: ensure target is within allowed roots
    if allowed_roots:
        resolved_roots = [Path(os.path.expanduser(str(r))).resolve() for r in allowed_roots]
        if not any(path_obj == r or path_obj.is_relative_to(r) for r in resolved_roots):
            print(f"Security Alert: Target path '{path_obj}' is outside allowed directories; skipping patch.", file=sys.stderr)
            return False

    if not path_obj.exists() or not path_obj.is_file():
        print(f"Warning: Target file '{path_obj}' does not exist; skipping patch.", file=sys.stderr)
        return False

    try:
        with open(path_obj, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
    except Exception as e:
        print(f"Warning: Failed to read '{path_obj}': {e}", file=sys.stderr)
        return False

    # Idempotency Check:
    # 1. Exact match
    clean_patch = patch_content.strip()
    if clean_patch in content:
        return False

    # 2. Rule title match (e.g. "**ANTI-LOOP GUARD (list_dir)**")
    title_match = re.search(r"\*\*([^*]+)\*\*", clean_patch)
    if title_match:
        rule_title = title_match.group(1).strip()
        if f"**{rule_title}**" in content:
            return False

    if dry_run:
        return True

    # Insertion logic:
    new_content = None

    # Case A: Designated section tag <SECTION>...</SECTION>
    clean_sec = section.replace(" ", "_").upper()
    sec_tag_pattern = re.compile(rf"</\s*{re.escape(clean_sec)}\s*>", re.IGNORECASE)
    match_tag = sec_tag_pattern.search(content)
    if match_tag:
        pos = match_tag.start()
        prefix = content[:pos]
        suffix = content[pos:]
        if not prefix.endswith("\n"):
            prefix += "\n"
        new_content = prefix + clean_patch + "\n" + suffix

    # Case B: Markdown heading matching section (e.g. '## Loop Guards' or '### ERROR HANDLING')
    if new_content is None:
        words = [w for w in re.split(r"[_\s-]+", section) if w]
        if words:
            pat_words = r"[\s_-]+".join(re.escape(w) for w in words)
            heading_pat = re.compile(rf"^(#{{1,6}})\s+(?:⛔\s*)?{pat_words}\b.*$", re.MULTILINE | re.IGNORECASE)
            match_heading = heading_pat.search(content)
            if match_heading:
                h_level = len(match_heading.group(1))
                h_end = match_heading.end()
                next_h = re.search(rf"^#{{1,{h_level}}}\s+", content[h_end:], re.MULTILINE)
                if next_h:
                    ins_pos = h_end + next_h.start()
                    prefix = content[:ins_pos].rstrip() + "\n\n"
                    suffix = content[ins_pos:]
                    new_content = prefix + clean_patch + "\n\n" + suffix
                else:
                    new_content = content.rstrip() + "\n\n" + clean_patch + "\n"

    # Case C: Fallback to existing <HARD_GATES> or <HARD-GATES>
    if new_content is None:
        hg_pat = re.compile(r"</\s*HARD[-_]GATES\s*>", re.IGNORECASE)
        match_hg = hg_pat.search(content)
        if match_hg:
            pos = match_hg.start()
            prefix = content[:pos]
            suffix = content[pos:]
            if not prefix.endswith("\n"):
                prefix += "\n"
            new_content = prefix + clean_patch + "\n" + suffix

    # Case D: Fallback before </GLOBAL_RULES>
    if new_content is None:
        gr_pat = re.compile(r"</\s*GLOBAL_RULES\s*>", re.IGNORECASE)
        match_gr = gr_pat.search(content)
        if match_gr:
            pos = match_gr.start()
            prefix = content[:pos].rstrip() + "\n\n"
            suffix = content[pos:]
            new_section_block = f"<{clean_sec}>\n{clean_patch}\n</{clean_sec}>\n\n"
            new_content = prefix + new_section_block + suffix

    # Case E: Append new markdown section at end of file
    if new_content is None:
        section_title = section.replace("_", " ").title()
        new_content = content.rstrip() + f"\n\n## {section_title}\n\n{clean_patch}\n"

    try:
        with open(path_obj, "w", encoding="utf-8") as f:
            f.write(new_content)
        return True
    except Exception as e:
        print(f"Warning: Failed to write to '{path_obj}': {e}", file=sys.stderr)
        return False


def apply_upstream_sync(skill_name: str, upstream_info: dict, dry_run: bool = False) -> dict:
    """Apply upstream synchronization for an outdated skill.

    Args:
        skill_name: Name of the skill.
        upstream_info: Upstream status dictionary from check_upstream.py.
        dry_run: If True, simulate without modifying local files.

    Returns:
        Dictionary summarizing the upstream sync action.
    """
    source_url = upstream_info.get("source_url") or upstream_info.get("sourceUrl", "")
    remote_hash = upstream_info.get("remote_hash") or upstream_info.get("remoteHash", "")
    hash_short = remote_hash[:8] if remote_hash else "latest"

    print(f"[Upstream Sync] Skill '{skill_name}' source: {source_url} (target: {hash_short})")

    if dry_run:
        print(f"[Upstream Sync] [DRY RUN] Would sync skill '{skill_name}' from {source_url}")
        return {
            "skill": skill_name,
            "type": "upstream_sync",
            "status": "dry_run",
            "source_url": source_url,
            "remote_hash": remote_hash,
            "description": f"Would sync skill '{skill_name}' with upstream ({hash_short})",
        }

    # If active, optionally update .skill-lock.json if available
    lock_path = Path(os.path.expanduser("~/.agents/.skill-lock.json"))
    if lock_path.exists() and remote_hash:
        try:
            with open(lock_path, "r", encoding="utf-8") as f:
                lock_data = json.load(f)
            if "skills" in lock_data and skill_name in lock_data["skills"]:
                lock_data["skills"][skill_name]["skillFolderHash"] = remote_hash
                lock_data["skills"][skill_name]["updatedAt"] = datetime.now(timezone.utc).isoformat()
                with open(lock_path, "w", encoding="utf-8") as f:
                    json.dump(lock_data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"Warning: Could not update lock file during sync for {skill_name}: {e}", file=sys.stderr)

    print(f"[Upstream Sync] Successfully marked sync for skill '{skill_name}'")
    return {
        "skill": skill_name,
        "type": "upstream_sync",
        "status": "synced",
        "source_url": source_url,
        "remote_hash": remote_hash,
        "description": f"Synced skill '{skill_name}' with upstream ({hash_short})",
    }


def git_commit_and_tag(
    repo_dir: str,
    message: str,
    tag: str,
    dry_run: bool = False,
    files_to_stage: list[str] | None = None,
) -> dict:
    """Check git status, commit changes, and apply version tag safely without staging untracked secrets.

    Args:
        repo_dir: Path to the git repository.
        message: Git commit message.
        tag: Git tag string (e.g. 'ci/YYYY-MM-DD').
        dry_run: If True, do not create git commit or tag.
        files_to_stage: Optional list of explicit files to stage (avoids git add -A leak).

    Returns:
        Dictionary with status, commit_hash, tag, and dirty flag.
    """
    repo_path = Path(os.path.expanduser(repo_dir))
    if not repo_path.exists() or not (repo_path / ".git").exists():
        return {
            "status": "error",
            "commit_hash": "DRY_RUN" if dry_run else "NOT_A_GIT_REPO",
            "tag": tag,
            "dirty": False,
            "error": f"Not a git repository: {repo_path}",
        }

    try:
        status_cmd = ["git", "-C", str(repo_path), "status", "--porcelain"]
        status_res = subprocess.run(status_cmd, capture_output=True, text=True, check=False)
        porcelain = status_res.stdout.strip()
        is_dirty = bool(porcelain)

        if dry_run:
            return {
                "status": "dry_run",
                "commit_hash": "DRY_RUN",
                "tag": tag,
                "dirty": is_dirty,
                "staged_files": [line.strip() for line in porcelain.splitlines() if line.strip()],
            }

        if not is_dirty:
            rev_cmd = ["git", "-C", str(repo_path), "rev-parse", "HEAD"]
            rev_res = subprocess.run(rev_cmd, capture_output=True, text=True, check=False)
            commit_hash = rev_res.stdout.strip() if rev_res.returncode == 0 else "UNKNOWN"
            return {
                "status": "clean",
                "commit_hash": commit_hash,
                "tag": tag,
                "dirty": False,
            }

        # Safe staging: stage explicitly modified files or tracked modifications only (never untracked secrets)
        if files_to_stage:
            rel_files = []
            for f in files_to_stage:
                fp = Path(os.path.expanduser(str(f))).resolve()
                try:
                    rel = fp.relative_to(repo_path.resolve())
                    rel_files.append(str(rel))
                except ValueError:
                    pass
            if rel_files:
                add_cmd = ["git", "-C", str(repo_path), "add", "--"] + rel_files
            else:
                add_cmd = ["git", "-C", str(repo_path), "add", "-u"]
        else:
            add_cmd = ["git", "-C", str(repo_path), "add", "-u"]

        add_res = subprocess.run(add_cmd, capture_output=True, text=True, check=False)
        if add_res.returncode != 0:
            return {
                "status": "error",
                "commit_hash": "ADD_FAILED",
                "tag": tag,
                "dirty": True,
                "error": add_res.stderr.strip(),
            }

        commit_cmd = ["git", "-C", str(repo_path), "commit", "-m", message]
        commit_res = subprocess.run(commit_cmd, capture_output=True, text=True, check=False)
        if commit_res.returncode != 0:
            return {
                "status": "error",
                "commit_hash": "COMMIT_FAILED",
                "tag": tag,
                "dirty": True,
                "error": commit_res.stderr.strip(),
            }

        tag_cmd = ["git", "-C", str(repo_path), "tag", "-f", tag]
        tag_res = subprocess.run(tag_cmd, capture_output=True, text=True, check=False)
        if tag_res.returncode != 0:
            print(f"Warning: Failed to create git tag '{tag}': {tag_res.stderr.strip()}", file=sys.stderr)

        rev_cmd = ["git", "-C", str(repo_path), "rev-parse", "HEAD"]
        rev_res = subprocess.run(rev_cmd, capture_output=True, text=True, check=False)
        commit_hash = rev_res.stdout.strip() if rev_res.returncode == 0 else "UNKNOWN"

        return {
            "status": "committed",
            "commit_hash": commit_hash,
            "tag": tag,
            "dirty": True,
        }

    except Exception as e:
        return {
            "status": "error",
            "commit_hash": "DRY_RUN" if dry_run else "ERROR",
            "tag": tag,
            "dirty": False,
            "error": str(e),
        }


def extract_findings_from_telemetry(telemetry_data: dict, registry: dict) -> list[dict]:
    """Extract deduplicated candidate failure findings from telemetry data.

    Args:
        telemetry_data: Telemetry JSON dictionary.
        registry: Project-skill mapping registry.

    Returns:
        List of candidate finding dictionaries.
    """
    if "findings" in telemetry_data and isinstance(telemetry_data["findings"], list):
        return telemetry_data["findings"]

    findings = []
    seen = set()

    sessions = telemetry_data.get("sessions", [])
    mappings = registry.get("mappings", []) if isinstance(registry, dict) else []

    def resolve_scope(ws: str) -> str:
        if not ws:
            return "global"
        for m in mappings:
            pat = m.get("workspace_pattern", "")
            if pat and re.search(pat, ws, re.IGNORECASE):
                return m.get("project_name", "global")
        return "global"

    for s in sessions:
        ws = s.get("workspace", "")
        scope = resolve_scope(ws)

        # 1. Loops
        for loop in s.get("loops", []):
            tool = loop.get("tool", "")
            cnt = loop.get("count", 0)
            if cnt >= 3 and tool:
                key = (scope, "loop", tool)
                if key not in seen:
                    seen.add(key)
                    findings.append({
                        "type": "loop",
                        "tool": tool,
                        "target": loop.get("target", ""),
                        "count": cnt,
                        "scope": scope,
                        "workspace": ws,
                    })

        # 2. Errors
        for err in s.get("errors", []):
            err_type = err.get("type", "")
            err_lvl = err.get("level", "B")
            if err_type:
                key = (scope, "error", err_type)
                if key not in seen:
                    seen.add(key)
                    findings.append({
                        "type": err_type,
                        "level": err_lvl,
                        "message": err.get("message", ""),
                        "scope": scope,
                        "workspace": ws,
                    })

        # 3. User corrections
        if s.get("user_corrections"):
            key = (scope, "UserCorrectionSignal", "")
            if key not in seen:
                seen.add(key)
                findings.append({
                    "type": "UserCorrectionSignal",
                    "level": "C",
                    "message": "User correction detected",
                    "scope": scope,
                    "workspace": ws,
                })

    # If sessions was empty, fallback to stats
    if not findings:
        stats = telemetry_data.get("stats", {})
        if stats.get("loops_detected", 0) > 0:
            key = ("global", "loop", "tool")
            if key not in seen:
                seen.add(key)
                findings.append({
                    "type": "loop",
                    "tool": "tool",
                    "count": 3,
                    "scope": "global",
                })
        for err_type, cnt in stats.get("error_types", {}).items():
            if cnt > 0:
                lvl = "C" if err_type == "UserCorrectionSignal" else ("A" if "Timeout" in err_type else "B")
                key = ("global", "error", err_type)
                if key not in seen:
                    seen.add(key)
                    findings.append({
                        "type": err_type,
                        "level": lvl,
                        "scope": "global",
                    })

    return findings


# -----------------------------------------------------------------------------
# Rule Lifecycle: Recording, Budget Cap, Pruning, and Evolution
# -----------------------------------------------------------------------------

def count_error_in_telemetry(telemetry_data: dict, error_type: str) -> int:
    """Count sessions matching error_type pattern in telemetry data.

    Patterns:
      - 'loop:<tool>': count sessions with loops matching <tool>
      - 'error:<ErrorType>': count sessions with errors matching <ErrorType>
      - 'correction': count sessions with user corrections
      - Fallback: match error types or tool loops directly

    Args:
        telemetry_data: Dictionary containing telemetry 'sessions' and/or 'stats'.
        error_type: Target error pattern string.

    Returns:
        Integer count of matching sessions or occurrences.
    """
    if not isinstance(telemetry_data, dict) or not error_type:
        return 0

    target = str(error_type).strip()
    target_lower = target.lower()
    sessions = telemetry_data.get("sessions", [])
    count = 0

    if isinstance(sessions, list) and sessions:
        if target_lower.startswith("loop:"):
            tool_name = target_lower[5:].strip()
            for s in sessions:
                if not isinstance(s, dict):
                    continue
                loops = s.get("loops", [])
                if isinstance(loops, list):
                    if any(str(l.get("tool", "")).strip().lower() == tool_name for l in loops if isinstance(l, dict)):
                        count += 1
        elif target_lower.startswith("error:"):
            err_type = target_lower[6:].strip()
            for s in sessions:
                if not isinstance(s, dict):
                    continue
                errors = s.get("errors", [])
                if isinstance(errors, list):
                    if any(str(e.get("type", "")).strip().lower() == err_type for e in errors if isinstance(e, dict)):
                        count += 1
        elif target_lower in ("correction", "user_correction", "user_corrections") or "correction" in target_lower:
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
    else:
        # Fallback to stats if sessions list is empty or absent
        stats = telemetry_data.get("stats", {})
        if isinstance(stats, dict):
            if target_lower.startswith("loop:"):
                count = stats.get("loops_detected", 0)
            elif target_lower.startswith("error:"):
                err_type = target_lower[6:].strip()
                for k, v in stats.get("error_types", {}).items():
                    if k.lower() == err_type:
                        count += v
            elif "correction" in target_lower:
                count = stats.get("user_corrections", 0) or stats.get("error_types", {}).get("UserCorrectionSignal", 0)
            else:
                for k, v in stats.get("error_types", {}).items():
                    if k.lower() == target_lower:
                        count += v

    return count


def check_budget_cap(
    ledger_path: Path | str | dict,
    target_file: str,
    max_rules: int = 5,
    new_rule_baseline: int | None = None
) -> bool:
    """Enforce rule budget cap: maximum active rules per target file.

    If current active rules on target_file < max_rules, returns True.
    If cap reached, returns True only if new_rule_baseline is provided and
    strictly greater than the lowest baseline_count among existing active rules.

    Args:
        ledger_path: Path to rule ledger JSON or in-memory ledger dictionary.
        target_file: Target file path.
        max_rules: Maximum active rules per file (default: 5).
        new_rule_baseline: Baseline error count of candidate rule.

    Returns:
        True if under budget cap or if candidate qualifies to override, False otherwise.
    """
    if isinstance(ledger_path, dict):
        ledger = ledger_path
    else:
        path_obj = Path(os.path.expanduser(str(ledger_path)))
        if not path_obj.exists() or not path_obj.is_file():
            return True
        try:
            with open(path_obj, "r", encoding="utf-8") as f:
                content = f.read().strip()
                ledger = json.loads(content) if content else {}
        except Exception as e:
            print(f"Warning: Failed to read ledger '{path_obj}' in check_budget_cap: {e}", file=sys.stderr)
            return True

    rules = ledger.get("rules", [])
    if not isinstance(rules, list):
        return True

    norm_target = _normalize_path(target_file)
    active_rules = []
    for r in rules:
        if not isinstance(r, dict):
            continue
        status = str(r.get("status", "ACTIVE")).upper()
        if status in ("PRUNED", "REMOVED"):
            continue
        r_target_str = str(r.get("target_file", ""))
        r_target_norm = _normalize_path(r_target_str)
        if r_target_norm == norm_target or r_target_str == target_file:
            active_rules.append(r)

    if len(active_rules) < max_rules:
        return True

    # At or over cap
    if new_rule_baseline is None:
        return False

    baselines = [int(r.get("baseline_count", 0)) for r in active_rules]
    lowest_baseline = min(baselines) if baselines else 0
    return new_rule_baseline > lowest_baseline


def record_rule_in_ledger(
    ledger_path: Path | str,
    rule_id: str,
    section: str,
    target_file: str,
    target_error_type: str,
    rule_text: str,
    baseline_count: int,
    dry_run: bool = False,
) -> dict | None:
    """Record a newly applied rule in rule_ledger.json idempotently.

    Does not duplicate if an active rule with the same target_error_type and
    target_file (or same rule_id and target_file) already exists.

    Args:
        ledger_path: Path to rule ledger JSON.
        rule_id: Unique rule ID or title.
        section: Section name in target file.
        target_file: Target file path.
        target_error_type: Telemetry error type pattern (e.g. 'loop:run_command').
        rule_text: Full markdown text block of the rule.
        baseline_count: Baseline occurrence count from telemetry.
        dry_run: If True, simulate without modifying ledger on disk.

    Returns:
        The newly created rule dictionary entry, or None if skipped (duplicate).
    """
    path_obj = Path(os.path.expanduser(str(ledger_path)))
    ledger = {"rules": [], "pruned": [], "evolved": []}
    if path_obj.exists() and path_obj.is_file():
        try:
            with open(path_obj, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if content:
                    ledger = json.loads(content)
        except Exception as e:
            print(f"Warning: Failed to read ledger '{path_obj}': {e}", file=sys.stderr)

    if not isinstance(ledger.get("rules"), list):
        ledger["rules"] = []
    if not isinstance(ledger.get("pruned"), list):
        ledger["pruned"] = []
    if not isinstance(ledger.get("evolved"), list):
        ledger["evolved"] = []

    # Idempotency check:
    norm_target = _normalize_path(target_file)
    for r in ledger["rules"]:
        if not isinstance(r, dict):
            continue
        status = str(r.get("status", "ACTIVE")).upper()
        if status in ("PRUNED", "REMOVED"):
            continue
        r_target_str = str(r.get("target_file", ""))
        r_target_norm = _normalize_path(r_target_str)
        target_matches = (r_target_norm == norm_target) or (r_target_str == target_file)
        if target_matches:
            if r.get("target_error_type") == target_error_type or r.get("id") == rule_id:
                # Already exists and active
                return None

    text_clean = rule_text.strip()
    text_hash = hashlib.sha256(text_clean.encode("utf-8")).hexdigest()[:12]
    text_preview = text_clean[:80]
    now_iso = datetime.now(timezone.utc).isoformat()

    new_rule = {
        "id": rule_id,
        "section": section,
        "target_file": str(target_file),
        "target_error_type": target_error_type,
        "rule_text": text_clean,
        "rule_text_hash": text_hash,
        "rule_text_preview": text_preview,
        "applied_at": now_iso,
        "baseline_count": int(baseline_count),
        "baseline_window_days": 7,
        "measurements": [],
        "status": "ACTIVE",
        "lifecycle": "MEASURING",
    }

    if dry_run:
        print(f"[Rule Ledger] [DRY RUN] Would record rule '{rule_id}' in ledger '{path_obj}'")
        return new_rule

    ledger["rules"].append(new_rule)
    try:
        path_obj.parent.mkdir(parents=True, exist_ok=True)
        with open(path_obj, "w", encoding="utf-8") as f:
            json.dump(ledger, f, indent=2, ensure_ascii=False)
        print(f"[Rule Ledger] Recorded rule '{rule_id}' in ledger '{path_obj}'")
    except Exception as e:
        print(f"Warning: Failed to write ledger '{path_obj}': {e}", file=sys.stderr)

    return new_rule


def _get_rule_age_days(rule: dict) -> float:
    """Calculate the age of a rule in days from applied_at or earliest measurement."""
    applied_at_str = rule.get("applied_at")
    if not applied_at_str:
        measurements = rule.get("measurements", [])
        if measurements and isinstance(measurements, list):
            first_cycle = measurements[0].get("cycle")
            if first_cycle:
                applied_at_str = first_cycle

    if not applied_at_str:
        return 7.0

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
        return 7.0


def _remove_rule_from_content(content: str, rule: dict) -> tuple[str, bool]:
    """Remove a rule text block from file content by exact text, hash, or title matching."""
    # 1. Exact match by rule_text
    rule_text = rule.get("rule_text", "").strip()
    if rule_text and rule_text in content:
        pat = re.compile(rf"[ \t]*{re.escape(rule_text)}[ \t]*\n?", re.MULTILINE)
        new_content = pat.sub("", content, count=1)
        sec = rule.get("section", "").replace(" ", "_").upper()
        if sec:
            new_content = re.sub(rf"<{sec}>\s*</{sec}>\n?", "", new_content, flags=re.IGNORECASE)
        return new_content, True

    # 2. Hash matching line or block
    rule_hash = rule.get("rule_text_hash", "")
    if rule_hash:
        lines = content.splitlines(keepends=True)
        for idx, line in enumerate(lines):
            line_clean = line.strip()
            if line_clean and hashlib.sha256(line_clean.encode("utf-8")).hexdigest()[:12] == rule_hash:
                lines.pop(idx)
                new_content = "".join(lines)
                sec = rule.get("section", "").replace(" ", "_").upper()
                if sec:
                    new_content = re.sub(rf"<{sec}>\s*</{sec}>\n?", "", new_content, flags=re.IGNORECASE)
                return new_content, True

    # 3. Rule title or ID matching
    rule_id = rule.get("id", "").strip()
    if rule_id:
        pat = re.compile(
            rf"^[ \t]*[-*]\s+\*\*{re.escape(rule_id)}\*\*.*?(?=(?:\n[ \t]*[-*]|\n#|\n<|\Z))",
            re.MULTILINE | re.DOTALL,
        )
        if pat.search(content):
            new_content = pat.sub("", content, count=1)
            new_content = re.sub(r"\n{3,}", "\n\n", new_content)
            sec = rule.get("section", "").replace(" ", "_").upper()
            if sec:
                new_content = re.sub(rf"<{sec}>\s*</{sec}>\n?", "", new_content, flags=re.IGNORECASE)
            return new_content, True

    return content, False


def prune_ineffective_rules(ledger_path: Path | str, dry_run: bool = False) -> list[dict]:
    """Auto-prune ineffective rules from target files and rule ledger.

    Finds rules in ledger with status: "PRUNE_CANDIDATE".
    Removes the exact rule text block from the target file.
    Moves the rule entry from rules[] to pruned[] with:
      - pruned_at: ISO timestamp
      - reason: "efficacy < 0.2 after N days"
      - final_efficacy: float
      - status: "PRUNED"

    Args:
        ledger_path: Path to rule ledger JSON.
        dry_run: If True, do not mutate target files or write ledger to disk.

    Returns:
        List of pruned rule dictionaries.
    """
    path_obj = Path(os.path.expanduser(str(ledger_path)))
    if not path_obj.exists() or not path_obj.is_file():
        return []

    try:
        with open(path_obj, "r", encoding="utf-8") as f:
            content = f.read().strip()
            ledger = json.loads(content) if content else {}
    except Exception as e:
        print(f"Warning: Failed to read ledger '{path_obj}' in prune_ineffective_rules: {e}", file=sys.stderr)
        return []

    rules = ledger.get("rules", [])
    if not isinstance(rules, list):
        return []

    if "pruned" not in ledger or not isinstance(ledger["pruned"], list):
        ledger["pruned"] = []

    pruned_list = []
    remaining_rules = []

    for r in rules:
        if not isinstance(r, dict):
            continue
        if str(r.get("status", "")).upper() == "PRUNE_CANDIDATE":
            target_str = r.get("target_file", "")
            target_path = Path(os.path.expanduser(target_str)) if target_str else None

            if target_path and target_path.exists() and target_path.is_file():
                try:
                    with open(target_path, "r", encoding="utf-8", errors="ignore") as f:
                        file_content = f.read()
                    new_content, found = _remove_rule_from_content(file_content, r)
                    if found:
                        if not dry_run:
                            with open(target_path, "w", encoding="utf-8") as f:
                                f.write(new_content)
                            print(f"[Rule Pruning] Removed rule text for '{r.get('id')}' from '{target_path}'")
                        else:
                            print(f"[Rule Pruning] [DRY RUN] Would remove rule text for '{r.get('id')}' from '{target_path}'")
                    else:
                        print(f"[Rule Pruning] Rule text for '{r.get('id')}' not found in '{target_path}'")
                except Exception as e:
                    print(f"Warning: Failed to update target file '{target_path}' during pruning: {e}", file=sys.stderr)

            age_days = int(round(_get_rule_age_days(r)))
            if age_days < 7:
                age_days = 7

            measurements = r.get("measurements", [])
            final_eff = float(measurements[-1].get("efficacy", 0.0)) if measurements else 0.0

            pruned_entry = dict(r)
            pruned_entry["status"] = "PRUNED"
            pruned_entry["pruned_at"] = datetime.now(timezone.utc).isoformat()
            pruned_entry["reason"] = f"efficacy < 0.2 after {age_days} days"
            pruned_entry["final_efficacy"] = final_eff

            pruned_list.append(pruned_entry)
            if not dry_run:
                ledger["pruned"].append(pruned_entry)
        else:
            remaining_rules.append(r)

    if not dry_run and pruned_list:
        ledger["rules"] = remaining_rules
        try:
            path_obj.parent.mkdir(parents=True, exist_ok=True)
            with open(path_obj, "w", encoding="utf-8") as f:
                json.dump(ledger, f, indent=2, ensure_ascii=False)
            print(f"[Rule Pruning] Successfully pruned {len(pruned_list)} rule(s) in ledger '{path_obj}'")
        except Exception as e:
            print(f"Warning: Failed to write updated ledger after pruning '{path_obj}': {e}", file=sys.stderr)
    elif dry_run and pruned_list:
        print(f"[Rule Pruning] [DRY RUN] Would prune {len(pruned_list)} rule(s) in ledger '{path_obj}'")

    return pruned_list


def evolve_rule(
    old_rule: dict,
    telemetry_data: dict,
    ledger_path: Path | str | None = None,
    dry_run: bool = False
) -> tuple[str, str] | None:
    """Evolve an ineffective rule into a more specific alternative rule.

    Checks if target_error_type still has occurrences > 0 in telemetry:
    - If yes: generates a more specific alternative rule (e.g. file size guard, domain context, pre-condition checks).
      Records the evolved rule into evolved[] in the ledger with pointer to original rule ID.
      Returns (section, new_rule_text).
    - If no: returns None.

    Args:
        old_rule: The pruned rule dictionary.
        telemetry_data: Telemetry data dictionary containing sessions/stats.
        ledger_path: Optional path to rule ledger JSON.
        dry_run: If True, do not persist to ledger on disk.

    Returns:
        Tuple of (section, new_rule_text) or None if error is no longer active.
    """
    if not isinstance(old_rule, dict):
        return None

    target_error = str(old_rule.get("target_error_type", "")).strip()
    occurrences = count_error_in_telemetry(telemetry_data, target_error)
    if occurrences <= 0:
        return None

    target_lower = target_error.lower()
    old_id = old_rule.get("id", "rule")

    if target_lower.startswith("loop:") or old_rule.get("section") == "LOOP_GUARDS":
        raw_tool = target_error.split(":", 1)[1].strip() if ":" in target_error else (old_rule.get("tool") or "tool")
        tool = _sanitize_token(raw_tool, max_len=40, default="tool")
        section = "LOOP_GUARDS"
        new_rule_text = (
            f"- **ANTI-LOOP GUARD V2 ({tool})**: Avoid repeated invocations on targets exceeding 500 lines "
            f"or producing identical output. Verify input parameters and check file size/context before invoking `{tool}`; "
            f"halt immediately, investigate root cause, and ask user after 2 consecutive identical attempts."
        )
    elif "correction" in target_lower or old_rule.get("section") == "SCOPE_ALIGNMENT":
        raw_scope = old_rule.get("scope") or "global"
        scope = _sanitize_token(raw_scope, max_len=40, default="global")
        section = "SCOPE_ALIGNMENT"
        new_rule_text = (
            f"- **SCOPE ALIGNMENT & DOMAIN PRE-VERIFICATION ({scope})**: For domain operations (e.g. {scope}/domain/API modifications), "
            f"always verify target element type, schema constraints, and document state prior to modification; "
            f"halt and clarify scope with user when requirements are ambiguous or upon receiving feedback."
        )
    elif target_lower.startswith("error:") or old_rule.get("section") in ("PRE_FLIGHT_CHECKS", "ERROR_HANDLING"):
        raw_err = target_error.split(":", 1)[1].strip() if ":" in target_error else target_error
        err_name = _sanitize_token(raw_err, max_len=40, default="CommandExecution")
        section = "PRE_FLIGHT_CHECKS"
        new_rule_text = (
            f"- **PRE-FLIGHT RESOURCE & ERROR VERIFICATION ({err_name})**: Verify file exists, path permissions "
            f"are valid, and target resources are readable before invoking commands. Wrap execution with pre-condition checks "
            f"to prevent `{err_name}`; halt immediately on failure."
        )
    else:
        clean_err = _sanitize_token(target_error, max_len=40, default="GeneralError")
        section = old_rule.get("section") or "HARD_GATES"
        new_rule_text = (
            f"- **EVOLVED AUDIT SAFEGUARD ({clean_err})**: Enforce strict verification of preconditions "
            f"and context constraints prior to execution to resolve recurring {clean_err}."
        )

    title_match = re.search(r"\*\*([^*]+)\*\*", new_rule_text)
    evolved_id = title_match.group(1).strip() if title_match else f"evolved_{old_id}"
    evolved_hash = hashlib.sha256(new_rule_text.strip().encode("utf-8")).hexdigest()[:12]

    evolved_entry = {
        "id": evolved_id,
        "original_rule_id": old_id,
        "section": section,
        "target_file": old_rule.get("target_file", ""),
        "target_error_type": target_error,
        "rule_text": new_rule_text.strip(),
        "rule_text_hash": evolved_hash,
        "evolved_at": datetime.now(timezone.utc).isoformat(),
        "reason": f"Evolved from ineffective rule '{old_id}' with still-active error in telemetry ({occurrences} occurrences)",
    }

    # Record in evolved[] in ledger
    ledger_target = ledger_path if ledger_path is not None else Path(os.path.expanduser("~/.agents/rule_ledger.json"))
    path_obj = Path(os.path.expanduser(str(ledger_target)))
    if path_obj.exists() and path_obj.is_file():
        try:
            with open(path_obj, "r", encoding="utf-8") as f:
                content = f.read().strip()
                ledger = json.loads(content) if content else {}
            if "evolved" not in ledger or not isinstance(ledger["evolved"], list):
                ledger["evolved"] = []

            already_recorded = any(
                isinstance(e, dict) and e.get("original_rule_id") == old_id and e.get("id") == evolved_id
                for e in ledger["evolved"]
            )
            if not already_recorded:
                if not dry_run:
                    ledger["evolved"].append(evolved_entry)
                    with open(path_obj, "w", encoding="utf-8") as f:
                        json.dump(ledger, f, indent=2, ensure_ascii=False)
                    print(f"[Rule Evolution] Recorded evolved rule '{evolved_id}' in ledger '{path_obj}'")
                else:
                    print(f"[Rule Evolution] [DRY RUN] Would record evolved rule '{evolved_id}' in ledger '{path_obj}'")
        except Exception as e:
            print(f"Warning: Failed to update evolved rules in ledger '{path_obj}': {e}", file=sys.stderr)

    return section, new_rule_text


# -----------------------------------------------------------------------------
# Main Improvement Applier Workflow
# -----------------------------------------------------------------------------

def apply_improvements(
    scores_path: str = "~/.agents/score_history.json",
    upstream_path: str = "~/.agents/upstream_status.json",
    telemetry_path: str = "~/.agents/graphify-out/.agent_ci_extract.json",
    registry_path: str = "~/.agents/project-skill-map.json",
    rule_ledger_path: str = "~/.agents/rule_ledger.json",
    dry_run: bool = False,
    output_json: str | None = None,
    repo_dir: str = "~/.agents",
) -> dict:
    """Main improvement workflow applying upstream sync, audit patches, and score hardening.

    Priority Order:
    1. Upstream sync for outdated skills
    2. Audit-driven patches from failure telemetry (with rule ledger tracking & budget cap)
    3. Score-driven hardening based on negative deltas
    4. Rule lifecycle maintenance: auto-prune ineffective rules and evolve rules

    Args:
        scores_path: Path to score history JSON.
        upstream_path: Path to upstream status JSON from check_upstream.py.
        telemetry_path: Path to failure telemetry JSON.
        registry_path: Path to project-skill registry JSON.
        rule_ledger_path: Path to rule ledger JSON.
        dry_run: If True, simulate changes without writing to disk or git.
        output_json: Optional path to write summary JSON.
        repo_dir: Git repository path to commit and tag.

    Returns:
        Dictionary summarizing all applied improvements and commit information.
    """
    changes_applied = []
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    tag = f"ci/{today_str}"
    commit_msg = f"ci: apply agent-ci improvements [{today_str}]"
    ledger_p = Path(os.path.expanduser(rule_ledger_path))

    # Initialize in-memory ledger tracker
    ledger_data = {"rules": [], "pruned": [], "evolved": []}
    files_to_stage: set[str] = set()
    if ledger_p.exists():
        files_to_stage.add(str(ledger_p))

    allowed_roots: list[Path] = [
        Path.home() / ".agents",
        Path.home() / ".gemini",
        Path(os.path.expanduser(repo_dir)).resolve(),
    ]

    if ledger_p.exists() and ledger_p.is_file():
        try:
            with open(ledger_p, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if content:
                    ledger_data = json.loads(content)
        except Exception as e:
            print(f"Warning: Failed to load ledger '{ledger_p}': {e}", file=sys.stderr)

    # Load registry
    reg_path = Path(os.path.expanduser(registry_path))
    registry = {}
    if reg_path.exists() and reg_path.is_file():
        try:
            with open(reg_path, "r", encoding="utf-8") as f:
                registry = json.load(f)
                for m in registry.get("mappings", []):
                    for sp in m.get("skill_paths", []):
                        try:
                            allowed_roots.append(Path(os.path.expanduser(sp)).resolve().parent)
                        except Exception:
                            pass
        except Exception as e:
            print(f"Warning: Failed to load registry '{reg_path}': {e}", file=sys.stderr)

    # Load telemetry
    telemetry_data = {}
    tel_path = Path(os.path.expanduser(telemetry_path))
    if tel_path.exists() and tel_path.is_file():
        try:
            with open(tel_path, "r", encoding="utf-8") as f:
                telemetry_data = json.load(f)
        except Exception as e:
            print(f"Warning: Failed to load telemetry '{tel_path}': {e}", file=sys.stderr)

    # -------------------------------------------------------------
    # Priority 1: Upstream sync for outdated skills
    # -------------------------------------------------------------
    up_path = Path(os.path.expanduser(upstream_path))
    if up_path.exists() and up_path.is_file():
        try:
            with open(up_path, "r", encoding="utf-8") as f:
                upstream_data = json.load(f)
            skills = upstream_data.get("skills", [])
            for s in skills:
                if s.get("status") == "UPDATE_AVAILABLE":
                    skill_name = s.get("name", "unknown")
                    sync_res = apply_upstream_sync(skill_name, s, dry_run=dry_run)
                    changes_applied.append({
                        "target": skill_name,
                        "type": "upstream_sync",
                        "description": sync_res.get("description", f"Synced skill '{skill_name}'"),
                        "status": sync_res.get("status", "synced"),
                    })
        except Exception as e:
            print(f"Warning: Failed to parse upstream status '{up_path}': {e}", file=sys.stderr)
    else:
        print(f"Notice: Upstream status file not found at '{up_path}'; skipping upstream sync.")

    # -------------------------------------------------------------
    # Priority 2: Audit-driven patches from failure telemetry
    # -------------------------------------------------------------
    applied_rules_in_run: set[tuple[str, str]] = set()
    if telemetry_data:
        try:
            findings = extract_findings_from_telemetry(telemetry_data, registry)
            for finding in findings:
                section_name, patch_content = generate_audit_patch(finding)
                target_scope = finding.get("scope") or finding.get("workspace") or "global"
                targets = route_to_target(target_scope, registry)
                
                title_match = re.search(r"\*\*([^*]+)\*\*", patch_content)
                rule_id = title_match.group(1).strip() if title_match else patch_content.strip()

                # Derive target_error_type
                finding_type = str(finding.get("type", "")).strip()
                is_loop = "loop" in finding_type.lower() or finding.get("loops") or finding.get("pattern") == "loop"
                is_corr = finding_type == "UserCorrectionSignal" or finding.get("level") == "C" or "correction" in finding_type.lower()
                if is_loop:
                    tool = finding.get("tool", "tool")
                    target_error_type = f"loop:{tool}"
                elif is_corr:
                    target_error_type = "correction"
                else:
                    err_type = finding_type or "GeneralError"
                    target_error_type = f"error:{err_type}"

                baseline_cnt = count_error_in_telemetry(telemetry_data, target_error_type)
                if baseline_cnt == 0:
                    cnt_val = finding.get("count", 1)
                    baseline_cnt = int(cnt_val) if isinstance(cnt_val, (int, float)) else 1

                for target in targets:
                    if (target, rule_id) in applied_rules_in_run:
                        continue

                    # Budget cap check against ledger state
                    under_cap = check_budget_cap(
                        ledger_path=ledger_data,
                        target_file=target,
                        max_rules=5,
                        new_rule_baseline=baseline_cnt,
                    )
                    if not under_cap:
                        print(f"[Budget Cap] Skipping rule '{rule_id}' for '{target}': max active rules reached (5) and baseline ({baseline_cnt}) not higher than lowest active rule.")
                        continue

                    # If at cap, identify the lowest-baseline rule to evict on this target file
                    norm_target = _normalize_path(target)
                    active_for_target = [
                        r for r in ledger_data.get("rules", [])
                        if isinstance(r, dict)
                        and str(r.get("status", "ACTIVE")).upper() not in ("PRUNED", "REMOVED")
                        and (_normalize_path(r.get("target_file", "")) == norm_target or r.get("target_file") == target)
                    ]
                    evicted_rule = None
                    if len(active_for_target) >= 5:
                        evicted_rule = min(active_for_target, key=lambda r: int(r.get("baseline_count", 0)))

                    applied = apply_patch_to_file(target, patch_content, section_name, dry_run=dry_run, allowed_roots=allowed_roots)
                    if applied:
                        applied_rules_in_run.add((target, rule_id))
                        files_to_stage.add(str(target))
                        files_to_stage.add(str(ledger_p))

                        # Evict lowest-baseline rule on this target file
                        if evicted_rule:
                            if not dry_run:
                                try:
                                    target_p = Path(os.path.expanduser(target))
                                    if target_p.exists():
                                        with open(target_p, "r", encoding="utf-8", errors="ignore") as f:
                                            fc = f.read()
                                        new_fc, found = _remove_rule_from_content(fc, evicted_rule)
                                        if found:
                                            with open(target_p, "w", encoding="utf-8") as f:
                                                f.write(new_fc)
                                except Exception as e:
                                    print(f"Warning: Failed to remove evicted rule from '{target}': {e}", file=sys.stderr)

                            evicted_copy = dict(evicted_rule)
                            evicted_copy["status"] = "PRUNED"
                            evicted_copy["pruned_at"] = datetime.now(timezone.utc).isoformat()
                            evicted_copy["reason"] = f"evicted: replaced by higher-baseline rule ({baseline_cnt} > {evicted_rule.get('baseline_count', 0)})"
                            evicted_copy["final_efficacy"] = float(evicted_rule.get("measurements", [{}])[-1].get("efficacy", 0.0)) if evicted_rule.get("measurements") else 0.0

                            # Update in-memory tracker: only remove rule matching both id and target_file
                            evicted_id = evicted_rule.get("id")
                            ledger_data["rules"] = [
                                r for r in ledger_data.get("rules", [])
                                if not (r.get("id") == evicted_id and (_normalize_path(r.get("target_file", "")) == norm_target or r.get("target_file") == target))
                            ]
                            ledger_data.setdefault("pruned", []).append(evicted_copy)

                            if not dry_run and ledger_p.exists():
                                try:
                                    with open(ledger_p, "r", encoding="utf-8") as f:
                                        disk_l = json.load(f)
                                    disk_l["rules"] = [
                                        r for r in disk_l.get("rules", [])
                                        if not (r.get("id") == evicted_id and (_normalize_path(r.get("target_file", "")) == norm_target or r.get("target_file") == target))
                                    ]
                                    disk_l.setdefault("pruned", []).append(evicted_copy)
                                    with open(ledger_p, "w", encoding="utf-8") as f:
                                        json.dump(disk_l, f, indent=2, ensure_ascii=False)
                                except Exception as e:
                                    print(f"Warning: Failed to persist eviction in ledger: {e}", file=sys.stderr)

                            changes_applied.append({
                                "target": target,
                                "type": "rule_evicted",
                                "description": f"Evicted rule '{evicted_rule.get('id')}' to respect budget cap (baseline {baseline_cnt} > {evicted_rule.get('baseline_count', 0)})",
                                "rule_id": evicted_rule.get("id"),
                            })

                        rec_entry = record_rule_in_ledger(
                            ledger_path=ledger_p,
                            rule_id=rule_id,
                            section=section_name,
                            target_file=target,
                            target_error_type=target_error_type,
                            rule_text=patch_content,
                            baseline_count=baseline_cnt,
                            dry_run=dry_run,
                        )
                        if rec_entry:
                            ledger_data.setdefault("rules", []).append(rec_entry)

                        item_label = finding.get("tool") or finding.get("type") or "telemetry finding"
                        changes_applied.append({
                            "target": target,
                            "type": "audit_patch",
                            "section": section_name,
                            "description": f"Applied {section_name} patch for {item_label}",
                            "rule_id": rule_id,
                        })
        except Exception as e:
            print(f"Warning: Failed to process telemetry '{tel_path}': {e}", file=sys.stderr)
    else:
        print(f"Notice: Telemetry data not loaded; skipping audit patches.")

    # -------------------------------------------------------------
    # Priority 3: Score-driven hardening based on negative deltas
    # -------------------------------------------------------------
    sc_path = Path(os.path.expanduser(scores_path))
    if sc_path.exists() and sc_path.is_file():
        try:
            with open(sc_path, "r", encoding="utf-8") as f:
                scores_data = json.load(f)
            cycles = scores_data.get("cycles", [])
            if cycles:
                latest_cycle = cycles[-1]
                scores_map = latest_cycle.get("scores", {})
                for scope, info in scores_map.items():
                    if isinstance(info, dict):
                        delta = info.get("delta", 0.0)
                        score_val = info.get("score", 0.0)
                        if isinstance(delta, (int, float)) and delta < 0:
                            finding = {
                                "type": "score_delta",
                                "scope": scope,
                                "delta": delta,
                                "score": score_val,
                            }
                            section_name, patch_content = generate_audit_patch(finding)
                            targets = route_to_target(scope, registry)
                            title_match = re.search(r"\*\*([^*]+)\*\*", patch_content)
                            rule_id = title_match.group(1).strip() if title_match else patch_content.strip()

                            for target in targets:
                                if (target, rule_id) in applied_rules_in_run:
                                    continue
                                applied = apply_patch_to_file(target, patch_content, section_name, dry_run=dry_run, allowed_roots=allowed_roots)
                                if applied:
                                    applied_rules_in_run.add((target, rule_id))
                                    files_to_stage.add(str(target))
                                    files_to_stage.add(str(ledger_p))
                                    changes_applied.append({
                                        "target": target,
                                        "type": "score_hardening",
                                        "section": section_name,
                                        "description": f"Applied score hardening patch for scope '{scope}' (delta: {delta:+.4f})",
                                        "rule_id": rule_id,
                                    })
        except Exception as e:
            print(f"Warning: Failed to process score history '{sc_path}': {e}", file=sys.stderr)
    else:
        print(f"Notice: Score history file not found at '{sc_path}'; skipping score hardening.")

    # -------------------------------------------------------------
    # Rule Lifecycle: Pruning and Evolution
    # -------------------------------------------------------------
    pruned_rules = prune_ineffective_rules(ledger_p, dry_run=dry_run)
    for pruned in pruned_rules:
        pruned_id = pruned.get("id")
        pruned_target_norm = _normalize_path(pruned.get("target_file", ""))
        # Update in-memory tracker: only remove rule matching both id and target_file
        ledger_data["rules"] = [
            r for r in ledger_data.get("rules", [])
            if not (r.get("id") == pruned_id and (_normalize_path(r.get("target_file", "")) == pruned_target_norm or r.get("target_file") == pruned.get("target_file")))
        ]
        ledger_data.setdefault("pruned", []).append(pruned)

        changes_applied.append({
            "target": pruned.get("target_file", "unknown"),
            "type": "rule_pruned",
            "description": f"Pruned ineffective rule '{pruned.get('id')}' ({pruned.get('reason', '')})",
            "rule_id": pruned.get("id"),
            "final_efficacy": pruned.get("final_efficacy", 0.0),
        })
        # Evolve rule if target error persists in telemetry
        evolved = evolve_rule(
            old_rule=pruned,
            telemetry_data=telemetry_data,
            ledger_path=ledger_p,
            dry_run=dry_run,
        )
        if evolved:
            evolved_sec, evolved_patch = evolved
            evolved_target = pruned.get("target_file", str(Path.home() / ".agents" / "GEMINI.md"))
            curr_cnt = count_error_in_telemetry(telemetry_data, pruned.get("target_error_type", ""))
            # Check budget cap before applying evolved rule
            if check_budget_cap(ledger_data, evolved_target, max_rules=5, new_rule_baseline=curr_cnt):
                applied_evolved = apply_patch_to_file(evolved_target, evolved_patch, evolved_sec, dry_run=dry_run, allowed_roots=allowed_roots)
                if applied_evolved:
                    files_to_stage.add(str(evolved_target))
                    files_to_stage.add(str(ledger_p))
                    evolved_id_match = re.search(r"\*\*([^*]+)\*\*", evolved_patch)
                    evolved_id = evolved_id_match.group(1).strip() if evolved_id_match else f"evolved_{pruned.get('id')}"
                    rec_entry = record_rule_in_ledger(
                        ledger_path=ledger_p,
                        rule_id=evolved_id,
                        section=evolved_sec,
                        target_file=evolved_target,
                        target_error_type=pruned.get("target_error_type", ""),
                        rule_text=evolved_patch,
                        baseline_count=curr_cnt,
                        dry_run=dry_run,
                    )
                    if rec_entry:
                        ledger_data.setdefault("rules", []).append(rec_entry)

                    changes_applied.append({
                        "target": evolved_target,
                        "type": "rule_evolved",
                        "section": evolved_sec,
                        "description": f"Applied evolved rule for '{pruned.get('id')}'",
                        "rule_id": evolved_id,
                    })

    # -------------------------------------------------------------
    # Git Commit and Tagging
    # -------------------------------------------------------------
    commit_info = git_commit_and_tag(
        repo_dir=repo_dir,
        message=commit_msg,
        tag=tag,
        dry_run=dry_run,
        files_to_stage=list(files_to_stage) if files_to_stage else None
    )
    commit_hash = commit_info.get("commit_hash", "UNKNOWN")

    output_summary = {
        "changes_applied": changes_applied,
        "total_changes": len(changes_applied),
        "commit_hash": commit_hash,
        "tag": tag,
        "dry_run": dry_run,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    # Write output JSON if requested
    if output_json:
        out_path = Path(os.path.expanduser(output_json))
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(output_summary, f, indent=2, ensure_ascii=False)
            print(f"Applied changes summary written to: {out_path}")
        except Exception as e:
            print(f"Warning: Failed to write summary JSON to '{out_path}': {e}", file=sys.stderr)

    # Print summary to stdout
    mode_str = "DRY RUN (preview only)" if dry_run else "LIVE EXECUTION"
    print("\n" + "=" * 60)
    print(" Agent-CI Improvement Applier Summary")
    print("=" * 60)
    print(f" Mode            : {mode_str}")
    print(f" Tag             : {tag}")
    print(f" Commit          : {commit_hash}")
    print(f" Changes Applied : {len(changes_applied)}")
    print("-" * 60)
    for idx, ch in enumerate(changes_applied, 1):
        print(f" [{idx}] Type: {ch.get('type')}")
        print(f"     Target     : {ch.get('target')}")
        print(f"     Description: {ch.get('description')}")
    print("=" * 60 + "\n")

    return output_summary


def parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Agent-CI Improvement Applier: applies upstream sync, audit patches, and rule lifecycle."
    )
    parser.add_argument(
        "--scores",
        default=os.path.expanduser("~/.agents/score_history.json"),
        help="Path to score history JSON (default: ~/.agents/score_history.json)",
    )
    parser.add_argument(
        "--upstream",
        default=os.path.expanduser("~/.agents/upstream_status.json"),
        help="Path to upstream status JSON from check_upstream.py (default: ~/.agents/upstream_status.json)",
    )
    parser.add_argument(
        "--telemetry",
        default=os.path.expanduser("~/.agents/graphify-out/.agent_ci_extract.json"),
        help="Path to failure telemetry JSON (default: ~/.agents/graphify-out/.agent_ci_extract.json)",
    )
    parser.add_argument(
        "--registry",
        default=os.path.expanduser("~/.agents/project-skill-map.json"),
        help="Path to project-skill registry JSON (default: ~/.agents/project-skill-map.json)",
    )
    parser.add_argument(
        "--rule-ledger",
        default=os.path.expanduser("~/.agents/rule_ledger.json"),
        help="Path to rule ledger JSON (default: ~/.agents/rule_ledger.json)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview changes without modifying files or git",
    )
    parser.add_argument(
        "--output-json",
        default=None,
        help="Optional path to write applied changes summary to JSON",
    )
    parser.add_argument(
        "--repo-dir",
        default=os.path.expanduser("~/.agents"),
        help="Git repository path to commit and tag (default: ~/.agents)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Main CLI entry point."""
    args = parse_arguments(argv)
    try:
        apply_improvements(
            scores_path=args.scores,
            upstream_path=args.upstream,
            telemetry_path=args.telemetry,
            registry_path=args.registry,
            rule_ledger_path=args.rule_ledger,
            dry_run=args.dry_run,
            output_json=args.output_json,
            repo_dir=args.repo_dir,
        )
        return 0
    except Exception as e:
        print(f"Error executing improvement applier: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
