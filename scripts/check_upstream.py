#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
check_upstream.py - Check if locally installed skills have upstream updates available.

Compares skill hashes and remote repository HEAD commits based on .skill-lock.json
and project-skill-map.json registry. Tracks remote commit hashes across execution cycles
to identify available updates, supports symlink-installed skills with repository-level caching,
and optionally fetches sparse diffs for updated skills.
"""

from __future__ import annotations

import os
import sys
import json
import argparse
import subprocess
import tempfile
from pathlib import Path
from datetime import datetime, timezone

# Module-level cache for remote repository HEAD queries: remote_url -> (remote_head, error_msg)
_REPO_URL_CACHE: dict[str, tuple[str | None, str | None]] = {}


def resolve_local_skill_path(name: str) -> Path | None:
    """Resolve the local filesystem directory for a skill.

    Checks ~/.gemini/config/skills/<name>/ first, then ~/.agents/skills/<name>/.

    Args:
        name: Name of the skill.

    Returns:
        Path if found and is a directory, otherwise None.
    """
    gemini_path = Path(os.path.expanduser(f"~/.gemini/config/skills/{name}"))
    if gemini_path.exists() and gemini_path.is_dir():
        return gemini_path

    agents_path = Path(os.path.expanduser(f"~/.agents/skills/{name}"))
    if agents_path.exists() and agents_path.is_dir():
        return agents_path

    return None


def _format_symlink_source(remote_url: str | None, repo_name: str) -> str:
    """Format symlink source identifier from git remote URL or repo name.

    Example:
        https://github.com/Egonex-AI/Understand-Anything.git -> symlink:Egonex-AI/Understand-Anything

    Args:
        remote_url: Git remote URL string.
        repo_name: Fallback repo name if remote URL cannot be parsed.

    Returns:
        Formatted source string prefixed with 'symlink:'.
    """
    if not remote_url:
        return f"symlink:{repo_name}"

    url = remote_url.strip().rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]

    for prefix in ("https://github.com/", "http://github.com/", "git@github.com:"):
        if url.startswith(prefix):
            return f"symlink:{url[len(prefix):]}"

    parts = url.replace(":", "/").split("/")
    if len(parts) >= 2 and parts[-2] and parts[-1]:
        return f"symlink:{parts[-2]}/{parts[-1]}"

    return f"symlink:{repo_name}"


def is_valid_git_url(url: str | None) -> bool:
    """Validate that a git URL is safe against option injection and uses an allowed protocol."""
    if not url or not isinstance(url, str):
        return False
    u = url.strip()
    if u.startswith("-"):
        return False
    valid_prefixes = ("https://", "http://", "git://", "ssh://", "git@")
    return any(u.startswith(p) for p in valid_prefixes)


def fetch_skill_diff(name: str, skill_info: dict, local_path: Path) -> str:
    """Fetch git diff between local skill folder and upstream repository.

    Clones the remote repository to a temporary directory with a shallow sparse checkout
    of the skill folder only, runs `diff -r` against the local skill directory,
    truncates output to 2000 characters if longer, and cleans up the temporary directory.

    Args:
        name: Skill name.
        skill_info: Skill metadata dictionary from .skill-lock.json.
        local_path: Path to local skill folder.

    Returns:
        Diff text string (max 2000 characters).
    """
    source_url = skill_info.get("sourceUrl", "")
    if not source_url or source_url == "local":
        return f"Cannot fetch diff: Invalid or local source URL for skill '{name}'."

    if not is_valid_git_url(source_url):
        return f"Cannot fetch diff: Untrusted git source URL '{source_url}' for skill '{name}'."

    skill_path_raw = skill_info.get("skillPath", f"skills/{name}/SKILL.md")
    skill_p = Path(skill_path_raw)
    if skill_p.suffix == ".md":
        folder_rel = str(skill_p.parent)
    else:
        folder_rel = str(skill_p)
    if not folder_rel or folder_rel == ".":
        folder_rel = f"skills/{name}"

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            # 1. Shallow clone with sparse checkout enabled (using '--' to prevent option injection)
            clone_cmd = [
                "git", "clone", "--depth", "1",
                "--filter=blob:none", "--sparse",
                "--", source_url, tmpdir
            ]
            clone_res = subprocess.run(
                clone_cmd,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if clone_res.returncode != 0:
                err = clone_res.stderr.strip() or f"git clone failed with code {clone_res.returncode}"
                return f"Error cloning upstream repo: {err}"

            # 2. Sparse checkout set folder
            sparse_cmd = ["git", "-C", tmpdir, "sparse-checkout", "set", folder_rel]
            sparse_res = subprocess.run(
                sparse_cmd,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            if sparse_res.returncode != 0:
                err = sparse_res.stderr.strip() or f"sparse-checkout failed with code {sparse_res.returncode}"
                return f"Error configuring sparse-checkout: {err}"

            upstream_folder = Path(tmpdir) / folder_rel
            if not upstream_folder.exists():
                alt_folder = Path(tmpdir) / f"skills/{name}"
                if alt_folder.exists():
                    upstream_folder = alt_folder
                else:
                    return f"Upstream skill folder not found at '{folder_rel}'."

            # 3. Run diff -r between local and upstream
            diff_proc = subprocess.run(
                ["diff", "-r", str(local_path), str(upstream_folder)],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            diff_text = diff_proc.stdout
            if diff_proc.returncode > 1 and diff_proc.stderr:
                diff_text = f"diff error: {diff_proc.stderr.strip()}"

            if len(diff_text) > 2000:
                diff_text = diff_text[:2000]

            return diff_text

    except subprocess.TimeoutExpired:
        return f"Timeout fetching diff from {source_url}"
    except Exception as e:
        return f"Error fetching diff: {e}"


def check_single_skill(
    name: str,
    skill_info: dict,
    timeout: int = 10,
    previous_remote_hash: str | None = None,
    url_cache: dict | None = None,
    is_first_run: bool | None = None,
) -> dict:
    """Check upstream status for a single skill.

    Args:
        name: Name of the skill.
        skill_info: Skill metadata dictionary from .skill-lock.json.
        timeout: Subprocess timeout in seconds for git ls-remote.
        previous_remote_hash: Last seen remote HEAD commit hash if available.
        url_cache: Optional cache mapping sourceUrl to (remote_head, error_msg).
        is_first_run: If True, indicates first check cycle (stores baseline as CURRENT).
                      If None and no previous_remote_hash is given, compares against
                      skillFolderHash or establishes baseline.

    Returns:
        Dictionary containing status and skill metadata.
    """
    source_type = str(skill_info.get("sourceType", "")).lower()
    source_url = skill_info.get("sourceUrl", "")
    source = skill_info.get("source", "")
    local_hash = skill_info.get("skillFolderHash", "")

    # Handle local-only skills
    if source_type != "github" or not source_url or source_url == "local":
        return {
            "name": name,
            "status": "LOCAL_ONLY",
            "local_hash": local_hash,
            "remote_hash": None,
            "source_url": source_url,
            "source": source,
        }

    if not is_valid_git_url(source_url):
        return {
            "name": name,
            "status": "CHECK_FAILED",
            "local_hash": local_hash,
            "remote_hash": None,
            "source_url": source_url,
            "source": source,
            "error": f"Untrusted or invalid git source URL: {source_url}",
        }

    # Query remote HEAD commit
    remote_hash = None
    error_msg = None

    if url_cache is not None and source_url in url_cache:
        remote_hash, error_msg = url_cache[source_url]
    else:
        try:
            cmd = ["git", "ls-remote", "--", source_url, "HEAD"]
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            if proc.returncode != 0:
                error_msg = proc.stderr.strip() or f"git ls-remote failed with code {proc.returncode}"
            else:
                lines = proc.stdout.strip().splitlines()
                if not lines or not lines[0].strip():
                    error_msg = "Empty output from git ls-remote"
                else:
                    remote_hash = lines[0].split()[0].strip()
        except subprocess.TimeoutExpired:
            error_msg = f"Timeout ({timeout}s) querying {source_url}"
        except Exception as e:
            error_msg = str(e)

        if url_cache is not None:
            url_cache[source_url] = (remote_hash, error_msg)

    if error_msg or not remote_hash:
        return {
            "name": name,
            "status": "CHECK_FAILED",
            "local_hash": local_hash,
            "remote_hash": None,
            "source_url": source_url,
            "source": source,
            "error": error_msg or "Unknown error querying remote",
        }

    # Determine status:
    prev_hash = previous_remote_hash or skill_info.get("previous_remote_hash") or skill_info.get("last_remote_hash")

    if prev_hash is not None:
        # Subsequent cycle: compare current remote HEAD with last recorded value
        status = "CURRENT" if remote_hash == prev_hash else "UPDATE_AVAILABLE"
    elif is_first_run is True:
        # First cycle baseline established as CURRENT per spec note
        status = "CURRENT"
    elif local_hash and local_hash != remote_hash:
        # Standalone comparison with no prior history and mismatched hash
        status = "UPDATE_AVAILABLE"
    else:
        status = "CURRENT"

    return {
        "name": name,
        "status": status,
        "local_hash": local_hash,
        "remote_hash": remote_hash,
        "source_url": source_url,
        "source": source,
    }


def check_symlink_skill(
    name: str,
    skill_path: Path | str,
    timeout: int = 10,
    repo_cache: dict[str, tuple[str | None, str | None]] | None = None,
) -> dict:
    """Check upstream status for a symlink-installed skill.

    Resolves the symlink target, finds the git repository root, retrieves the
    origin remote URL and local HEAD commit, queries the remote HEAD commit
    (with repo-level caching), and compares local vs remote HEAD.

    Args:
        name: Name of the skill.
        skill_path: Path to the skill symlink or directory.
        timeout: Subprocess timeout in seconds for git commands (default: 10).
        repo_cache: Optional cache mapping remote_url to (remote_head, error_msg).
                    If None, uses module-level cache _REPO_URL_CACHE.

    Returns:
        Dictionary containing status and skill metadata matching check_single_skill format.
    """
    if repo_cache is None:
        repo_cache = _REPO_URL_CACHE

    p = Path(os.path.expanduser(str(skill_path)))

    # Step 1: Resolve symlink to real target
    try:
        resolved_path = p.resolve()
    except Exception as e:
        return {
            "name": name,
            "status": "CHECK_FAILED",
            "local_hash": None,
            "remote_hash": None,
            "source_url": None,
            "source": f"symlink:{name}",
            "install_type": "symlink",
            "error": f"Failed to resolve symlink '{p}': {e}",
        }

    # Edge case: Symlink target directory no longer exists
    if not resolved_path.exists():
        return {
            "name": name,
            "status": "CHECK_FAILED",
            "local_hash": None,
            "remote_hash": None,
            "source_url": None,
            "source": f"symlink:{name}",
            "install_type": "symlink",
            "error": f"Symlink target does not exist: {resolved_path}",
        }

    resolved_dir = resolved_path.parent if resolved_path.is_file() else resolved_path

    # Step 2: Find git repo root
    try:
        proc = subprocess.run(
            ["git", "-C", str(resolved_dir), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if proc.returncode != 0:
            return {
                "name": name,
                "status": "CHECK_FAILED",
                "local_hash": None,
                "remote_hash": None,
                "source_url": None,
                "source": f"symlink:{name}",
                "install_type": "symlink",
                "error": f"Target is not inside a git repository: {proc.stderr.strip() or resolved_dir}",
            }
        repo_root = Path(proc.stdout.strip())
    except subprocess.TimeoutExpired:
        return {
            "name": name,
            "status": "CHECK_FAILED",
            "local_hash": None,
            "remote_hash": None,
            "source_url": None,
            "source": f"symlink:{name}",
            "install_type": "symlink",
            "error": f"Timeout ({timeout}s) finding git repo root for {resolved_dir}",
        }
    except Exception as e:
        return {
            "name": name,
            "status": "CHECK_FAILED",
            "local_hash": None,
            "remote_hash": None,
            "source_url": None,
            "source": f"symlink:{name}",
            "install_type": "symlink",
            "error": str(e),
        }

    # Step 3: Get remote URL
    remote_url = None
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            remote_url = proc.stdout.strip()
    except subprocess.TimeoutExpired:
        return {
            "name": name,
            "status": "CHECK_FAILED",
            "local_hash": None,
            "remote_hash": None,
            "source_url": None,
            "source": f"symlink:{name}",
            "install_type": "symlink",
            "error": f"Timeout ({timeout}s) getting remote URL for {repo_root}",
        }
    except Exception as e:
        return {
            "name": name,
            "status": "CHECK_FAILED",
            "local_hash": None,
            "remote_hash": None,
            "source_url": None,
            "source": f"symlink:{name}",
            "install_type": "symlink",
            "error": str(e),
        }

    # Step 4: Get local HEAD commit
    local_hash = None
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            local_hash = proc.stdout.strip()
    except subprocess.TimeoutExpired:
        return {
            "name": name,
            "status": "CHECK_FAILED",
            "local_hash": None,
            "remote_hash": None,
            "source_url": remote_url,
            "source": _format_symlink_source(remote_url, repo_root.name),
            "install_type": "symlink",
            "error": f"Timeout ({timeout}s) getting local HEAD for {repo_root}",
        }
    except Exception as e:
        return {
            "name": name,
            "status": "CHECK_FAILED",
            "local_hash": None,
            "remote_hash": None,
            "source_url": remote_url,
            "source": _format_symlink_source(remote_url, repo_root.name),
            "install_type": "symlink",
            "error": str(e),
        }

    # Edge case: Git repo has no remote -> LOCAL_ONLY
    if not remote_url:
        return {
            "name": name,
            "status": "LOCAL_ONLY",
            "local_hash": local_hash,
            "remote_hash": None,
            "source_url": None,
            "source": f"symlink:{repo_root.name}",
            "install_type": "symlink",
        }

    source_str = _format_symlink_source(remote_url, repo_root.name)

    # Validate remote URL safety
    if not is_valid_git_url(remote_url):
        return {
            "name": name,
            "status": "CHECK_FAILED",
            "local_hash": local_hash,
            "remote_hash": None,
            "source_url": remote_url,
            "source": source_str,
            "install_type": "symlink",
            "error": f"Untrusted or invalid git remote URL: {remote_url}",
        }

    # Step 5 & 8: Get remote HEAD commit (with cache by remote_url)
    remote_hash = None
    error_msg = None

    if remote_url in repo_cache:
        remote_hash, error_msg = repo_cache[remote_url]
    else:
        try:
            cmd = ["git", "ls-remote", "--", remote_url, "HEAD"]
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            if proc.returncode != 0:
                error_msg = proc.stderr.strip() or f"git ls-remote failed with code {proc.returncode}"
            else:
                lines = proc.stdout.strip().splitlines()
                if not lines or not lines[0].strip():
                    error_msg = "Empty output from git ls-remote"
                else:
                    remote_hash = lines[0].split()[0].strip()
        except subprocess.TimeoutExpired:
            error_msg = f"Timeout ({timeout}s) querying {remote_url}"
        except Exception as e:
            error_msg = str(e)

        repo_cache[remote_url] = (remote_hash, error_msg)

    if error_msg or not remote_hash:
        return {
            "name": name,
            "status": "CHECK_FAILED",
            "local_hash": local_hash,
            "remote_hash": None,
            "source_url": remote_url,
            "source": source_str,
            "install_type": "symlink",
            "error": error_msg or "Unknown error querying remote",
        }

    # Step 6: Compare local vs remote HEAD commit
    status = "CURRENT" if local_hash == remote_hash else "UPDATE_AVAILABLE"

    # Step 7: Return status dictionary
    return {
        "name": name,
        "status": status,
        "local_hash": local_hash,
        "remote_hash": remote_hash,
        "source_url": remote_url,
        "source": source_str,
        "install_type": "symlink",
    }


def check_upstream(
    lock_file: str | Path,
    output_path: str | Path,
    fetch_diffs: bool = False,
    timeout: int = 10,
    registry_path: str | Path | None = None,
) -> dict:
    """Check all skills in lock_file and registry against remote repositories and write output JSON.

    Args:
        lock_file: Path to .skill-lock.json.
        output_path: Path to write upstream status JSON.
        fetch_diffs: Whether to fetch diffs for UPDATE_AVAILABLE skills.
        timeout: Subprocess timeout in seconds for remote queries.
        registry_path: Optional path to project-skill-map.json registry.

    Returns:
        Summary dictionary of upstream check results.
    """
    lock_p = Path(os.path.expanduser(str(lock_file)))
    out_p = Path(os.path.expanduser(str(output_path)))

    if not lock_p.exists():
        raise FileNotFoundError(f"Lock file not found: '{lock_p}'")

    try:
        with open(lock_p, "r", encoding="utf-8") as f:
            lock_data = json.load(f)
    except Exception as e:
        raise ValueError(f"Failed to parse lock file JSON from '{lock_p}': {e}") from e

    skills_dict = lock_data.get("skills", {})
    if not isinstance(skills_dict, dict):
        raise ValueError(f"Invalid 'skills' entry in lock file '{lock_p}': expected dictionary.")

    # Check for previous cycle results in output_path to track remote HEAD changes
    previous_remote_hashes: dict[str, str] = {}
    is_first_run = True

    if out_p.exists():
        try:
            with open(out_p, "r", encoding="utf-8") as f:
                prev_data = json.load(f)
            if isinstance(prev_data, dict) and "skills" in prev_data:
                for item in prev_data["skills"]:
                    if isinstance(item, dict) and item.get("name") and item.get("remote_hash"):
                        previous_remote_hashes[item["name"]] = item["remote_hash"]
                if previous_remote_hashes:
                    is_first_run = False
        except Exception:
            # If previous output file is corrupt or unreadable, treat as first run
            is_first_run = True

    url_cache: dict[str, tuple[str | None, str | None]] = {}
    skills_results: list[dict] = []
    seen_skills: set[str] = set()

    for name, skill_info in skills_dict.items():
        if not isinstance(skill_info, dict):
            continue

        prev_hash = previous_remote_hashes.get(name)
        res = check_single_skill(
            name=name,
            skill_info=skill_info,
            timeout=timeout,
            previous_remote_hash=prev_hash,
            url_cache=url_cache,
            is_first_run=is_first_run,
        )

        # Optionally fetch diffs if update is available
        if fetch_diffs and res.get("status") == "UPDATE_AVAILABLE":
            local_path = resolve_local_skill_path(name)
            if local_path:
                diff_text = fetch_skill_diff(name, skill_info, local_path)
                res["diff"] = diff_text
            else:
                res["diff"] = f"Local skill directory not found for '{name}'."

        skills_results.append(res)
        seen_skills.add(name)

    # Process global_skills from registry (if provided)
    if registry_path:
        reg_p = Path(os.path.expanduser(str(registry_path)))
        if reg_p.exists() and reg_p.is_file():
            try:
                with open(reg_p, "r", encoding="utf-8") as f:
                    reg_data = json.load(f)
                global_skills = reg_data.get("global_skills", [])
                if isinstance(global_skills, list):
                    for item in global_skills:
                        if not isinstance(item, dict):
                            continue
                        skill_name = item.get("name")
                        if not skill_name or skill_name in seen_skills:
                            continue

                        install_type = item.get("install_type", "")
                        git_remote = item.get("git_remote")

                        if install_type == "symlink" and git_remote:
                            # Symlink skill with git_remote -> check_symlink_skill
                            skill_path_str = item.get("path")
                            skill_p = (
                                Path(os.path.expanduser(skill_path_str))
                                if skill_path_str
                                else resolve_local_skill_path(skill_name)
                            )
                            if not skill_p or (not skill_p.exists() and not skill_p.is_symlink()):
                                target_str = item.get("target")
                                if target_str and Path(os.path.expanduser(target_str)).exists():
                                    skill_p = Path(os.path.expanduser(target_str))
                                else:
                                    skill_p = Path(os.path.expanduser(f"~/.agents/skills/{skill_name}"))

                            res = check_symlink_skill(
                                name=skill_name,
                                skill_path=skill_p,
                                timeout=timeout,
                                repo_cache=url_cache,
                            )
                            seen_skills.add(skill_name)
                            skills_results.append(res)
                        elif install_type == "system":
                            # System skill -> skip remote check, mark SYSTEM_MANAGED
                            res = {
                                "name": skill_name,
                                "status": "SYSTEM_MANAGED",
                                "local_hash": None,
                                "remote_hash": None,
                                "source_url": None,
                                "source": f"system:{skill_name}",
                                "install_type": "system",
                            }
                            seen_skills.add(skill_name)
                            skills_results.append(res)
            except Exception as e:
                print(f"Warning: Failed to load skills from registry '{reg_p}': {e}", file=sys.stderr)

    # Tally counts
    total_skills = len(skills_results)
    current_count = sum(1 for s in skills_results if s.get("status") == "CURRENT")
    update_count = sum(1 for s in skills_results if s.get("status") == "UPDATE_AVAILABLE")
    local_count = sum(1 for s in skills_results if s.get("status") == "LOCAL_ONLY")
    failed_count = sum(1 for s in skills_results if s.get("status") == "CHECK_FAILED")
    system_count = sum(1 for s in skills_results if s.get("status") == "SYSTEM_MANAGED")

    # Upstream freshness score including symlink skills in denominator.
    # Excludes SYSTEM_MANAGED skills which do not have upstream git tracking.
    freshness_denom = total_skills - system_count
    freshness = round((current_count + local_count) / freshness_denom, 4) if freshness_denom > 0 else 1.0

    output_data = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "total_skills": total_skills,
        "current": current_count,
        "update_available": update_count,
        "local_only": local_count,
        "check_failed": failed_count,
        "system_managed": system_count,
        "freshness": freshness,
        "skills": skills_results,
    }

    # Ensure target directory exists and write output JSON
    out_p.parent.mkdir(parents=True, exist_ok=True)
    with open(out_p, "w", encoding="utf-8") as f:
        json.dump(output_data, f, indent=2, ensure_ascii=False)

    return output_data


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for check_upstream."""
    parser = argparse.ArgumentParser(
        description="Check if locally installed skills have upstream updates available."
    )
    parser.add_argument(
        "--lock-file",
        default="~/.agents/.skill-lock.json",
        help="Path to .skill-lock.json file (default: ~/.agents/.skill-lock.json)",
    )
    parser.add_argument(
        "--registry",
        default="~/.agents/project-skill-map.json",
        help="Path to project-skill-map.json registry file (default: ~/.agents/project-skill-map.json)",
    )
    parser.add_argument(
        "--output",
        required=True,
        help="Path to write upstream status JSON (required)",
    )
    parser.add_argument(
        "--fetch-diffs",
        action="store_true",
        help="Fetch git diffs for skills with updates available",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=10,
        help="Per-repo timeout in seconds for git ls-remote (default: 10)",
    )

    args = parser.parse_args(argv)

    try:
        res = check_upstream(
            lock_file=args.lock_file,
            output_path=args.output,
            fetch_diffs=args.fetch_diffs,
            timeout=args.timeout,
            registry_path=args.registry,
        )
        sys_managed = res.get("system_managed", 0)
        sys_str = f", {sys_managed} SYSTEM_MANAGED" if sys_managed > 0 else ""
        print(
            f"Checked {res['total_skills']} skills: "
            f"{res['current']} CURRENT, "
            f"{res['update_available']} UPDATE_AVAILABLE, "
            f"{res['local_only']} LOCAL_ONLY, "
            f"{res['check_failed']} CHECK_FAILED"
            f"{sys_str} "
            f"(freshness: {res['freshness']:.2f})"
        )
        print(f"Upstream status written to: {args.output}")
        return 0
    except (FileNotFoundError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Unexpected error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
