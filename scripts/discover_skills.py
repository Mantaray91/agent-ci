#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""discover_skills.py - Auto-discovery engine for Agent CI v2.1.

Discovers project skills across active workspaces from session logs
and global skills from skill directories, generating or updating
the project-skill-map.json registry while preserving manual entries.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Dict, List, Optional, Set, Tuple


def normalize_workspace_path(ws: str) -> Optional[Path]:
    """Normalize a raw workspace string from logs to an existing Path on Linux/POSIX.

    Handles Windows paths (e.g. 'G:/My Drive/...' or 'G:\\My Drive\\...'),
    translating them to Linux local mount points (e.g. '~/gdrive/...').

    Args:
        ws: Raw workspace path string from session logs.

    Returns:
        Resolved Path if the directory exists, None otherwise.
    """
    if not ws or not isinstance(ws, str):
        return None

    cleaned = ws.strip()
    if not cleaned:
        return None

    # Normalize backslashes to forward slashes
    norm = cleaned.replace("\\", "/")

    # Translate Windows Google Drive mount (G:/My Drive or g:/My Drive)
    m = re.match(r"^[A-Za-z]:/My Drive(?:/(.*))?$", norm, re.IGNORECASE)
    if m:
        subpath = m.group(1) or ""
        candidate = Path.home() / "gdrive" / subpath
        if candidate.exists() and candidate.is_dir():
            return candidate.resolve()

    # Direct path expansion
    try:
        candidate = Path(norm).expanduser()
        if candidate.exists() and candidate.is_dir():
            return candidate.resolve()
    except Exception:
        pass

    return None


def extract_active_workspaces(scan_logs_dir: Path) -> Set[Path]:
    """Extract active workspace paths from session log files (.jsonl and .md).

    Args:
        scan_logs_dir: Directory containing session log files.

    Returns:
        Set of resolved active workspace Path objects that exist on the filesystem.
    """
    active_workspaces: Set[Path] = set()

    if not scan_logs_dir.is_dir():
        print(f"[WARN] Session logs directory does not exist: {scan_logs_dir}", file=sys.stderr)
        return active_workspaces

    # 1. Parse JSONL logs
    for jsonl_file in scan_logs_dir.glob("*.jsonl"):
        try:
            with open(jsonl_file, "r", encoding="utf-8-sig", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                        ws_raw = record.get("workspace")
                        if ws_raw and isinstance(ws_raw, str):
                            p = normalize_workspace_path(ws_raw)
                            if p:
                                active_workspaces.add(p)
                    except Exception:
                        continue
        except Exception as e:
            print(f"[WARN] Error reading {jsonl_file.name}: {e}", file=sys.stderr)

    # 2. Parse legacy Markdown logs (header contains Workspace field)
    for md_file in scan_logs_dir.glob("*.md"):
        try:
            with open(md_file, "r", encoding="utf-8-sig", errors="ignore") as f:
                for idx, line in enumerate(f):
                    if idx > 200:
                        break
                    m = re.search(r"-\s*\*\*Workspace\*\*:\s*`([^`]+)`", line)
                    if m:
                        p = normalize_workspace_path(m.group(1))
                        if p:
                            active_workspaces.add(p)
        except Exception as e:
            print(f"[WARN] Error reading {md_file.name}: {e}", file=sys.stderr)

    return active_workspaces


def derive_project_scope(workspace: Path) -> Tuple[str, str]:
    """Derive project_name and workspace_pattern regex from workspace path.

    Normalization rules:
    - '9. IBKR' -> 'IBKR'
    - '11. Ai REVIT DRAFTER' -> 'REVIT'
    - '14. Ai STRUCTURES ANALYSIS' -> 'STRUCTURES'
    - '10. IHSG' -> 'IHSG'

    Args:
        workspace: Path to the workspace directory.

    Returns:
        Tuple of (project_name, workspace_pattern).
    """
    dirname = workspace.name
    # Match leading numbering prefix e.g. '9. IBKR', '11. Ai REVIT DRAFTER'
    m_num = re.match(r"^(\d+)[\.\-_ ]*(.+)$", dirname)
    num = m_num.group(1) if m_num else ""
    remainder = m_num.group(2).strip() if m_num else dirname.strip()
    rem_upper = remainder.upper()

    if "REVIT" in rem_upper:
        project_name = "REVIT"
    elif "STRUCTURE" in rem_upper:
        project_name = "STRUCTURES"
    elif "IBKR" in rem_upper:
        project_name = "IBKR"
    elif "IHSG" in rem_upper:
        project_name = "IHSG"
    else:
        # Strip leading Ai / AI prefix
        clean = re.sub(r"^AI[\s_\-]+", "", remainder, flags=re.IGNORECASE).strip()
        words = re.split(r"[\s_\-]+", clean)
        project_name = words[0].upper() if words and words[0] else dirname.upper()

    # Generate workspace_pattern regex
    if num:
        if re.search(r"\bAi\b", dirname, re.IGNORECASE):
            workspace_pattern = f"{num}.*{project_name}|Ai.{project_name}"
        else:
            workspace_pattern = f"{num}.*{project_name}|{project_name}"
    else:
        if re.search(r"\bAi\b", dirname, re.IGNORECASE):
            workspace_pattern = f"Ai.{project_name}|{project_name}"
        else:
            workspace_pattern = project_name

    return project_name, workspace_pattern


def scan_workspace_skills(workspace: Path, global_skill_roots: Set[Path]) -> List[str]:
    """Scan <workspace>/.agents/skills/ for skills containing SKILL.md.

    Skips anything under 'skills_archive/' or '_archived/'.

    Args:
        workspace: Path to the workspace.
        global_skill_roots: Set of resolved global skill roots to prevent recursion.

    Returns:
        Sorted list of absolute paths to SKILL.md files.
    """
    skills_dir = workspace / ".agents" / "skills"
    if not skills_dir.is_dir():
        return []

    # Exclude if this points to the global skill root itself
    try:
        if skills_dir.resolve() in global_skill_roots:
            return []
    except Exception:
        pass

    skill_paths: List[str] = []
    try:
        for child in sorted(skills_dir.iterdir()):
            if child.name in ("skills_archive", "_archived") or not child.is_dir():
                continue
            skill_md = child / "SKILL.md"
            if skill_md.is_file():
                skill_paths.append(str(skill_md.resolve()))
    except Exception as e:
        print(f"[WARN] Error scanning skills in {skills_dir}: {e}", file=sys.stderr)

    return sorted(skill_paths)


def get_git_remote_url(target_dir: Path) -> Optional[str]:
    """Query git remote get-url origin for target directory or its git root.

    Args:
        target_dir: Path to directory or file to check.

    Returns:
        Remote origin URL string if found, None otherwise.
    """
    cwd = target_dir if target_dir.is_dir() else target_dir.parent
    try:
        res_root = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if res_root.returncode != 0:
            return None

        repo_root = res_root.stdout.strip()
        res_remote = subprocess.run(
            ["git", "-C", repo_root, "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if res_remote.returncode == 0 and res_remote.stdout.strip():
            return res_remote.stdout.strip()
    except Exception:
        pass

    return None


def scan_global_skills(
    scan_skills_dirs: List[Path],
    lock_file: Path,
) -> List[Dict[str, Any]]:
    """Scan skill directories for global skills, classifying and deduplicating.

    Classification:
    - 'npx': skill present in .skill-lock.json
    - 'symlink': symlink resolved to a git repo
    - 'system': symlink target located under /usr/share/
    - 'local': everything else

    Deduplication priority: lock file (npx) > symlink/system > local.

    Args:
        scan_skills_dirs: List of directories to scan for global skills.
        lock_file: Path to .skill-lock.json.

    Returns:
        List of global skill dictionaries sorted by name.
    """
    # Priority rank: lock file (npx) > symlink/system > local
    PRIORITIES: Dict[str, int] = {"npx": 3, "symlink": 2, "system": 2, "local": 1}

    locked_skills: Set[str] = set()
    lock_filename = lock_file.name if lock_file else ".skill-lock.json"
    if lock_file and lock_file.is_file():
        try:
            with open(lock_file, "r", encoding="utf-8") as f:
                lock_data = json.load(f)
                locked_skills = set(lock_data.get("skills", {}).keys())
        except Exception as e:
            print(f"[WARN] Error reading lock file {lock_file}: {e}", file=sys.stderr)

    discovered: Dict[str, Tuple[int, Dict[str, Any]]] = {}

    for s_dir in scan_skills_dirs:
        if not s_dir.is_dir():
            continue

        try:
            for item in sorted(s_dir.iterdir()):
                skill_md = item / "SKILL.md"
                if not skill_md.exists():
                    continue

                name = item.name
                is_symlink = item.is_symlink()

                if is_symlink:
                    try:
                        raw_target = os.readlink(item)
                        if os.path.isabs(raw_target):
                            target_path = Path(raw_target)
                        else:
                            target_path = (item.parent / raw_target).resolve()
                    except Exception:
                        target_path = item.resolve()

                    target_str = str(target_path)
                    if target_str.startswith("/usr/share/"):
                        itype = "system"
                        entry: Dict[str, Any] = {
                            "name": name,
                            "install_type": "system",
                            "path": str(item),
                            "target": target_str,
                        }
                    else:
                        git_remote = get_git_remote_url(target_path)
                        if git_remote:
                            itype = "symlink"
                            entry = {
                                "name": name,
                                "install_type": "symlink",
                                "path": str(item),
                                "target": target_str,
                                "git_remote": git_remote,
                            }
                        else:
                            itype = "local"
                            entry = {
                                "name": name,
                                "install_type": "local",
                                "path": str(item),
                            }
                elif name in locked_skills:
                    itype = "npx"
                    entry = {
                        "name": name,
                        "install_type": "npx",
                        "path": str(item),
                        "source_lock": lock_filename,
                    }
                else:
                    itype = "local"
                    entry = {
                        "name": name,
                        "install_type": "local",
                        "path": str(item),
                    }

                priority = PRIORITIES.get(itype, 1)
                if name not in discovered or priority > discovered[name][0]:
                    discovered[name] = (priority, entry)
        except Exception as e:
            print(f"[WARN] Error scanning global skills directory {s_dir}: {e}", file=sys.stderr)

    return [v[1] for k, v in sorted(discovered.items())]


def load_manual_mappings(output_path: Path) -> List[Dict[str, Any]]:
    """Load existing manual mappings from output registry file.

    Preserves any entry with "source": "manual".

    Args:
        output_path: Path to the current project-skill-map.json.

    Returns:
        List of preserved manual mapping dictionaries.
    """
    if not output_path.is_file():
        return []

    try:
        with open(output_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            mappings = data.get("mappings", [])
            return [m for m in mappings if isinstance(m, dict) and m.get("source") == "manual"]
    except Exception as e:
        print(f"[WARN] Error reading existing mappings from {output_path}: {e}", file=sys.stderr)
        return []


def discover_all(
    scan_logs_dir: Path,
    scan_skills_dirs: List[Path],
    lock_file: Path,
    project_dirs: List[Path],
    output_path: Path,
    lazy: bool,
) -> Dict[str, Any]:
    """Execute complete skill discovery pipeline and return registry dict."""
    # Build set of resolved global skill roots
    global_skill_roots: Set[Path] = set()
    for d in scan_skills_dirs:
        try:
            if d.exists():
                global_skill_roots.add(d.resolve())
        except Exception:
            pass

    # 1. Active workspaces from logs
    workspaces_to_scan: Set[Path] = set()
    active_workspaces = extract_active_workspaces(scan_logs_dir)
    workspaces_to_scan.update(active_workspaces)

    # If exhaustive scan requested (--no-lazy), also include project-dirs subdirectories
    if not lazy and project_dirs:
        for p_dir in project_dirs:
            if p_dir.is_dir():
                try:
                    for child in p_dir.iterdir():
                        if child.is_dir() and (child / ".agents" / "skills").is_dir():
                            workspaces_to_scan.add(child.resolve())
                except Exception as e:
                    print(f"[WARN] Error scanning project dir {p_dir}: {e}", file=sys.stderr)

    # 2. Project skill scanning
    auto_mappings: Dict[str, Dict[str, Any]] = {}
    for ws in sorted(workspaces_to_scan):
        if ws == Path.home():
            continue
        skill_paths = scan_workspace_skills(ws, global_skill_roots)
        if not skill_paths:
            continue

        proj_name, pattern = derive_project_scope(ws)
        if proj_name in auto_mappings:
            # Merge paths
            existing = auto_mappings[proj_name]
            combined_paths = sorted(set(existing["skill_paths"] + skill_paths))
            existing["skill_paths"] = combined_paths
        else:
            auto_mappings[proj_name] = {
                "workspace_pattern": pattern,
                "project_name": proj_name,
                "skill_paths": skill_paths,
                "source": "auto:workspace_scan",
            }

    # 3. Preserve manual mappings
    manual_mappings = load_manual_mappings(output_path)
    manual_project_names = {m.get("project_name") for m in manual_mappings if m.get("project_name")}

    final_mappings: List[Dict[str, Any]] = list(manual_mappings)
    for proj_name, mapping in sorted(auto_mappings.items()):
        if proj_name not in manual_project_names:
            final_mappings.append(mapping)

    final_mappings.sort(key=lambda m: m.get("project_name", ""))

    # 4. Global skills scan
    global_skills = scan_global_skills(scan_skills_dirs, lock_file)

    # 5. Build final registry
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    registry: Dict[str, Any] = {
        "auto_discovered_at": now_utc,
        "mappings": final_mappings,
        "global_skills": global_skills,
    }

    return registry


def main() -> int:
    """CLI entry point for discover_skills.py."""
    parser = argparse.ArgumentParser(
        description="Auto-discover project skills and global skills for Agent CI."
    )
    parser.add_argument(
        "--scan-logs",
        type=str,
        default=str(Path.home() / ".agents" / "logs"),
        help="Directory containing session logs (default: ~/.agents/logs)",
    )
    parser.add_argument(
        "--scan-skills",
        type=str,
        nargs="+",
        default=[
            str(Path.home() / ".agents" / "skills"),
            str(Path.home() / ".gemini" / "config" / "skills"),
        ],
        help="Skill directories to scan (default: ~/.agents/skills ~/.gemini/config/skills)",
    )
    parser.add_argument(
        "--lock-file",
        type=str,
        default=str(Path.home() / ".agents" / ".skill-lock.json"),
        help="Path to .skill-lock.json (default: ~/.agents/.skill-lock.json)",
    )
    parser.add_argument(
        "--project-dirs",
        type=str,
        nargs="*",
        default=[],
        help="Additional project root directories to scan (e.g. ~/gdrive/Documents)",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=str(Path.home() / ".agents" / "project-skill-map.json"),
        help="Output path for project-skill-map.json (default: ~/.agents/project-skill-map.json)",
    )
    parser.add_argument(
        "--lazy",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Only include project skills whose workspace appeared in recent logs (default: True).",
    )

    args = parser.parse_args()

    scan_logs_dir = Path(os.path.expanduser(args.scan_logs)).resolve()
    scan_skills_dirs = [Path(os.path.expanduser(d)).resolve() for d in args.scan_skills]
    lock_file = Path(os.path.expanduser(args.lock_file)).resolve()
    project_dirs = [Path(os.path.expanduser(d)).resolve() for d in args.project_dirs]
    output_path = Path(os.path.expanduser(args.output)).resolve()
    lazy = args.lazy

    registry = discover_all(
        scan_logs_dir=scan_logs_dir,
        scan_skills_dirs=scan_skills_dirs,
        lock_file=lock_file,
        project_dirs=project_dirs,
        output_path=output_path,
        lazy=lazy,
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(registry, f, indent=2, ensure_ascii=False)
        f.write("\n")

    print(f"[INFO] Successfully wrote registry to {output_path}")
    print(f"[INFO] Total project mappings: {len(registry.get('mappings', []))}")
    print(f"[INFO] Total global skills: {len(registry.get('global_skills', []))}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
