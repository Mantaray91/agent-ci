#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
parse_logs.py - High-density JSONL & Markdown log parser and failure signature extractor for agent-ci.
Analyzes structured JSONL logs (*.jsonl) and legacy Markdown logs (*.md),
identifies Level A/B/C failure patterns, and outputs a Graphify-compatible knowledge graph JSON.
"""

import os
import sys
import re
import json
import time
import shutil
import argparse
from pathlib import Path
from collections import defaultdict, Counter

# Explicit error patterns
RE_ERROR_PATTERNS = [
    (r"Encountered error in tool execution[:\s]*(.*)", "ToolExecutionError", "B"),
    (r"Traceback \(most recent call last\):[\s\S]*?([A-Za-z0-9_]+Error|Exception): (.*)", "PythonTraceback", "B"),
    (r"(System\.Exception|Autodesk\.Revit\.Exceptions\.[A-Za-z0-9_]+): (.*)", "RevitAPIException", "B"),
    (r"The command exited with code ([1-9][0-9]*)", "NonZeroExitCode", "B"),
    (r"ParserError|UnexpectedToken|Cannot find path", "PowerShellSyntaxError", "B"),
    (r"FileNotFoundError: \[Errno 2\] No such file or directory: (.*)", "FileNotFound", "B"),
    (r"PermissionError: \[Errno 13\] Permission denied: (.*)", "PermissionDenied", "B"),
    (r"timeout|timed out after [0-9]+ms", "ToolTimeout", "A"),
    (r"Failed to connect|Connection refused|Server not responding", "MCPConnectionError", "B")
]

# Regex patterns for user correction / retry (Level C)
RE_USER_CORRECTION = re.compile(
    r"(?i)\b(salah|bukan|ulangi|ulang|kenapa gagal|tidak bisa|error lagi|keliru|re-run|retry|fix this|failed to)\b"
)

def is_audited_file(path: Path) -> bool:
    """Check if a file matches *.audited or .audited.* or has .audited in name."""
    name = path.name
    return name.endswith(".audited") or ".audited." in name or name.startswith(".audited.")

def determine_project_tag(
    workspace: str,
    filename: str = "",
    registry_mappings: list[dict] | None = None
) -> str:
    """Extract project tag from workspace path or filename using registry and regex rules."""
    ws_str = str(workspace or "").strip()
    fn_str = str(filename or "").strip()

    # 1. Match against registry mappings if available
    if registry_mappings:
        for mapping in registry_mappings:
            pattern = mapping.get("workspace_pattern", "")
            if pattern:
                try:
                    if ws_str and re.search(pattern, ws_str, re.IGNORECASE):
                        return mapping.get("project_name", "global")
                    if fn_str and re.search(pattern, fn_str, re.IGNORECASE):
                        return mapping.get("project_name", "global")
                except re.error:
                    pass

    # 2. Dynamic extraction from filename convention: agy_Session_<TAG>_...
    m_fn = re.search(r'agy_Session_([A-Za-z0-9_\-]+?)_\d{4}-\d{2}-\d{2}', fn_str)
    if m_fn:
        tag_candidate = m_fn.group(1)
        tag_candidate = re.sub(r'^[0-9]+[\.\-_ ]*', '', tag_candidate).strip()
        if tag_candidate and tag_candidate.lower() not in ("session", "work", "home", "root"):
            return tag_candidate

    # 3. Dynamic extraction from workspace directory name
    if ws_str and ws_str not in ("/", "\\", ".", "~"):
        try:
            p_name = Path(ws_str).name
            if p_name and p_name.lower() not in ("home", "users", "documents", "desktop", "work", "root"):
                clean_name = re.sub(r'^[0-9]+[\.\-_ ]*', '', p_name).strip()
                if clean_name:
                    return clean_name
        except Exception:
            pass

    return "global"

def parse_jsonl_file(file_path: Path, registry_mappings: list[dict] | None = None):
    """Parse structured JSONL session log file."""
    sessions = []
    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            for line_idx, line in enumerate(f):
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line)
                except Exception:
                    continue

                session_id = raw.get("id", "")
                short_id = raw.get("short_id", "") or (session_id[:8] if session_id else f"turn_{line_idx+1}")
                role = raw.get("role", "Main Orchestrator")
                timestamp = raw.get("ts", "")
                source = raw.get("src", "Unknown")
                workspace = raw.get("workspace", "")
                term_reason = raw.get("term_reason", "NO_TOOL_CALL")
                prompts = raw.get("prompts", [])
                if isinstance(prompts, dict):
                    prompts = list(prompts.values())
                elif isinstance(prompts, str):
                    prompts = [prompts]
                
                context = raw.get("context", [])
                if isinstance(context, dict):
                    context = list(context.values())
                elif isinstance(context, str):
                    context = [context]

                tools_raw = raw.get("tools", [])
                if isinstance(tools_raw, dict):
                    tools_raw = list(tools_raw.values())

                tools_called = []
                tool_details = []
                tool_sequence = []

                for t in tools_raw:
                    t_name = t.get("name", "unknown_tool") if isinstance(t, dict) else str(t)
                    summary_arg = ""
                    if isinstance(t, dict):
                        if t.get("cmd"):
                            summary_arg = t["cmd"]
                        elif t.get("target"):
                            summary_arg = t["target"]
                        elif t.get("path"):
                            summary_arg = os.path.basename(t["path"])
                        elif t.get("server") or t.get("tool"):
                            summary_arg = f"{t.get('server','')}:{t.get('tool','')}"
                    
                    summary_arg_clean = summary_arg[:120].strip()
                    tools_called.append(t_name)
                    tool_details.append({
                        "name": t_name,
                        "summary_arg": summary_arg_clean
                    })
                    tool_sequence.append((t_name, summary_arg_clean))

                # Detect Looping (Level B)
                loops = []
                call_counts = Counter(tool_sequence)
                for (t_name, s_arg), count in call_counts.items():
                    if count >= 3 and s_arg:
                        loops.append({
                            "tool": t_name,
                            "target": s_arg,
                            "count": count
                        })

                # Detect Errors (Level A, B, C)
                errors = []
                # 1. Level A: Abnormal Termination
                if term_reason not in ["NO_TOOL_CALL", "USER_EXIT", ""]:
                    errors.append({
                        "level": "A",
                        "type": "AbnormalTermination",
                        "message": f"Termination reason: {term_reason}",
                        "tool": "session_lifecycle"
                    })

                # 2. Level B: Text errors in response or tools
                final_resp = raw.get("final_response", "")
                full_text = final_resp + "\n" + "\n".join(str(t) for t in tools_raw)
                seen_err_msgs = set()
                for pat, err_type, err_lvl in RE_ERROR_PATTERNS:
                    matches = re.findall(pat, full_text, re.IGNORECASE)
                    for m in matches:
                        msg = m if isinstance(m, str) else " | ".join([x for x in m if x])
                        msg = msg.strip()[:200]
                        if msg and msg not in seen_err_msgs:
                            seen_err_msgs.add(msg)
                            errors.append({
                                "level": err_lvl,
                                "type": err_type,
                                "message": msg,
                                "tool": tools_called[-1] if tools_called else "general"
                            })

                # 3. Level C: User Corrections
                user_corrections = []
                for p_text in prompts:
                    if RE_USER_CORRECTION.search(p_text):
                        matched = list(set(RE_USER_CORRECTION.findall(p_text)))
                        user_corrections.append({
                            "keywords": matched,
                            "snippet": p_text[:200]
                        })
                        errors.append({
                            "level": "C",
                            "type": "UserCorrectionSignal",
                            "message": f"Prompt correction detected ({', '.join(matched)}): {p_text[:100]}",
                            "tool": "orchestration"
                        })

                # Health
                if any(e["level"] == "A" for e in errors) or len(errors) >= 3:
                    health = "FAIL"
                elif errors or loops:
                    health = "WARN"
                else:
                    health = "HEALTHY"

                sessions.append({
                    "filename": file_path.name,
                    "format": "JSONL",
                    "role": role,
                    "timestamp": timestamp,
                    "source": source,
                    "conversation_id": session_id,
                    "workspace": workspace,
                    "project_tag": determine_project_tag(workspace, file_path.name, registry_mappings),
                    "termination_reason": term_reason,
                    "prompts": prompts,
                    "context_loaded": context,
                    "tools_called": tools_called,
                    "tool_details": tool_details,
                    "errors": errors,
                    "loops": loops,
                    "user_corrections": user_corrections,
                    "health": health
                })
    except Exception as e:
        print(f"Warning: Failed to parse JSONL file {file_path.name}: {e}", file=sys.stderr)
    return sessions

def parse_markdown_blocks(content: str):
    """Split legacy Markdown log file into individual session turns."""
    raw_blocks = re.split(r'(?m)^## 🏷️\s*\[', content)
    blocks = []
    for b in raw_blocks:
        if not b.strip():
            continue
        blocks.append('## 🏷️ [' + b)
    return blocks

def parse_single_markdown_session(block_text: str, filename: str, registry_mappings: list[dict] | None = None):
    """Extract metadata from a legacy Markdown session block."""
    session_data = {
        "filename": filename,
        "format": "MD",
        "role": "Main Orchestrator",
        "timestamp": "",
        "source": "Unknown",
        "conversation_id": "",
        "workspace": "",
        "project_tag": "global",
        "termination_reason": "NO_TOOL_CALL",
        "prompts": [],
        "context_loaded": [],
        "tools_called": [],
        "tool_details": [],
        "errors": [],
        "loops": [],
        "user_corrections": [],
        "health": "HEALTHY"
    }

    lines = block_text.splitlines()
    header_line = lines[0] if lines else ""
    
    m_head = re.search(r'## 🏷️\s*\[(.*?)\]\s*•\s*([0-9\-:\s]+)', header_line)
    if m_head:
        session_data["role"] = m_head.group(1).strip()
        session_data["timestamp"] = m_head.group(2).strip()

    for line in lines[1:20]:
        if "- **Source**:" in line:
            session_data["source"] = re.sub(r'[\*\`\-]', '', line.split(":", 1)[1]).strip()
        elif "- **Conversation ID**:" in line:
            session_data["conversation_id"] = re.sub(r'[\*\`\-]', '', line.split(":", 1)[1]).strip()
        elif "- **Workspace**:" in line:
            session_data["workspace"] = re.sub(r'[\*\`\-]', '', line.split(":", 1)[1]).strip()
        elif "- **Termination Reason**:" in line:
            session_data["termination_reason"] = line.split(":", 1)[1].strip()

    if session_data["source"] == "Unknown":
        if "antigravity-cli" in block_text or "agy_Session" in filename:
            session_data["source"] = "CLI"
        else:
            session_data["source"] = "Chat-Agentic"

    prompts_match = re.findall(r'### 👤 User Prompt / Task Scope\s*\n([\s\S]*?)(?=\n###|\n---|\Z)', block_text)
    for p in prompts_match:
        p_clean = p.strip()
        session_data["prompts"].append(p_clean)
        if RE_USER_CORRECTION.search(p_clean):
            matched_words = RE_USER_CORRECTION.findall(p_clean)
            session_data["user_corrections"].append({
                "keywords": list(set(matched_words)),
                "snippet": p_clean[:200]
            })

    context_match = re.findall(r'- 📖 Loaded skill/context:\s*`([^`]+)`', block_text)
    session_data["context_loaded"] = list(set(context_match))

    tool_sections = re.findall(r'####\s*([^\n]+)\n```(?:json)?\s*([\s\S]*?)```', block_text)
    tool_sequence = []
    for raw_header, tool_arg_raw in tool_sections:
        raw_header = raw_header.strip()
        tool_name = "unknown_tool"
        summary_arg = ""
        args = {}
        try:
            args = json.loads(tool_arg_raw.strip())
        except Exception:
            pass

        if "Shell:" in raw_header:
            tool_name = "run_command"
            summary_arg = args.get("CommandLine") or args.get("command") or ""
        elif "Edit/Write:" in raw_header:
            m_tw = re.search(r'`([^`]+)`', raw_header)
            tool_name = m_tw.group(1) if m_tw else "write_to_file"
            summary_arg = args.get("TargetFile") or ""
        elif "MCP Tool:" in raw_header:
            tool_name = "call_mcp_tool"
            server = args.get("ServerName", "")
            tname = args.get("ToolName", "")
            summary_arg = f"{server}:{tname}" if server or tname else ""
        elif "Dispatch Subagents" in raw_header:
            tool_name = "invoke_subagent"
            summary_arg = str(args.get("Subagents", ""))[:100]
        else:
            m_t = re.search(r'`([^`]+)`', raw_header)
            tool_name = m_t.group(1) if m_t else "tool"
            if "AbsolutePath" in args:
                summary_arg = os.path.basename(args["AbsolutePath"])
            elif "DirectoryPath" in args:
                summary_arg = args["DirectoryPath"]

        if not summary_arg and "`" in raw_header:
            m_arg = re.search(r'`([^`]+)`', raw_header)
            if m_arg and m_arg.group(1) != tool_name:
                summary_arg = m_arg.group(1)

        summary_arg_clean = summary_arg[:120].strip()
        session_data["tools_called"].append(tool_name)
        session_data["tool_details"].append({
            "name": tool_name,
            "summary_arg": summary_arg_clean
        })
        tool_sequence.append((tool_name, summary_arg_clean))

    call_counts = Counter(tool_sequence)
    for (t_name, s_arg), count in call_counts.items():
        if count >= 3 and s_arg:
            session_data["loops"].append({
                "tool": t_name,
                "target": s_arg,
                "count": count
            })

    non_arg_text = re.sub(r'```(?:json)?\s*[\s\S]*?```', '', block_text)
    if session_data["termination_reason"] not in ["NO_TOOL_CALL", "USER_EXIT", ""]:
        session_data["errors"].append({
            "level": "A",
            "type": "AbnormalTermination",
            "message": f"Termination reason: {session_data['termination_reason']}",
            "tool": "session_lifecycle"
        })

    seen_err_msgs = set()
    for pat, err_type, err_lvl in RE_ERROR_PATTERNS:
        matches = re.findall(pat, non_arg_text, re.IGNORECASE)
        for m in matches:
            msg = m if isinstance(m, str) else " | ".join([x for x in m if x])
            msg = msg.strip()[:200]
            if msg and msg not in seen_err_msgs:
                seen_err_msgs.add(msg)
                session_data["errors"].append({
                    "level": err_lvl,
                    "type": err_type,
                    "message": msg,
                    "tool": session_data["tools_called"][-1] if session_data["tools_called"] else "general"
                })

    if session_data["user_corrections"]:
        for uc in session_data["user_corrections"]:
            session_data["errors"].append({
                "level": "C",
                "type": "UserCorrectionSignal",
                "message": f"Prompt correction detected ({', '.join(uc['keywords'])}): {uc['snippet'][:100]}",
                "tool": "orchestration"
            })

    if any(e["level"] == "A" for e in session_data["errors"]) or len(session_data["errors"]) >= 3:
        session_data["health"] = "FAIL"
    elif session_data["errors"] or session_data["loops"]:
        session_data["health"] = "WARN"
    else:
        session_data["health"] = "HEALTHY"

    session_data["project_tag"] = determine_project_tag(session_data["workspace"], filename, registry_mappings)
    return session_data

def build_knowledge_graph(sessions):
    """Transform extracted sessions into Graphify nodes and edges."""
    nodes = []
    edges = []
    node_ids = set()

    def add_node(node_id, label, n_type, properties=None):
        if node_id not in node_ids:
            node_ids.add(node_id)
            nodes.append({
                "id": node_id,
                "label": label,
                "type": n_type,
                "properties": properties or {}
            })

    def add_edge(src, tgt, rel, weight=1, details=""):
        edges.append({
            "source": src,
            "target": tgt,
            "relation": rel,
            "weight": weight,
            "details": details
        })

    stats = {
        "total_sessions": len(sessions),
        "healthy_sessions": sum(1 for s in sessions if s["health"] == "HEALTHY"),
        "warning_sessions": sum(1 for s in sessions if s["health"] == "WARN"),
        "failed_sessions": sum(1 for s in sessions if s["health"] == "FAIL"),
        "project_tags": dict(Counter(s.get("project_tag", "global") for s in sessions)),
        "sources": Counter(s["source"] for s in sessions),
        "roles": Counter(s["role"] for s in sessions),
        "top_tools": Counter(t for s in sessions for t in s["tools_called"]),
        "error_types": Counter(e["type"] for s in sessions for e in s["errors"]),
        "loops_detected": sum(len(s["loops"]) for s in sessions)
    }

    for i, s in enumerate(sessions):
        short_id = s["conversation_id"][:8] if s["conversation_id"] else f"sess_{i+1}"
        session_node_id = f"Session:{short_id}"
        proj_tag = s.get("project_tag", "global")
        
        add_node(
            session_node_id,
            f"Session {short_id} ({s['source']})",
            "session",
            {
                "timestamp": s["timestamp"],
                "source": s["source"],
                "health": s["health"],
                "role": s["role"],
                "project_tag": proj_tag,
                "error_count": len(s["errors"]),
                "loop_count": len(s["loops"]),
                "format": s.get("format", "JSONL")
            }
        )

        source_node_id = f"Source:{s['source']}"
        add_node(source_node_id, f"Source: {s['source']}", "source")
        add_edge(session_node_id, source_node_id, "ORIGINATED_FROM")

        proj_node_id = f"Project:{proj_tag}"
        add_node(proj_node_id, f"Project: {proj_tag}", "project_scope")
        add_edge(session_node_id, proj_node_id, "BELONGS_TO_PROJECT")

        role_node_id = f"Role:{s['role']}"
        add_node(role_node_id, f"Role: {s['role']}", "agent_role")
        add_edge(session_node_id, role_node_id, "EXECUTED_BY")

        for ctx in s["context_loaded"]:
            ctx_node_id = f"SkillContext:{ctx}"
            add_node(ctx_node_id, f"Skill: {ctx}", "skill")
            add_edge(session_node_id, ctx_node_id, "LOADED_CONTEXT")

        tool_counts = Counter(s["tools_called"])
        for tool_name, count in tool_counts.items():
            tool_node_id = f"Tool:{tool_name}"
            add_node(tool_node_id, f"Tool: {tool_name}", "tool")
            add_edge(session_node_id, tool_node_id, "USED_TOOL", weight=count)

        for err in s["errors"]:
            err_node_id = f"Error:{err['type']}"
            add_node(
                err_node_id,
                f"Error: {err['type']} (Level {err['level']})",
                "error_signature",
                {"level": err["level"]}
            )
            add_edge(session_node_id, err_node_id, "ENCOUNTERED_ERROR", details=err["message"])
            
            if err.get("tool") and err["tool"] != "general":
                tool_target = f"Tool:{err['tool']}"
                add_node(tool_target, f"Tool: {err['tool']}", "tool")
                add_edge(err_node_id, tool_target, "OCCURRED_IN_TOOL")

        for lp in s["loops"]:
            clean_target = re.sub(r'[^a-zA-Z0-9_\-]', '_', lp['target'][:30])
            loop_node_id = f"Loop:{lp['tool']}_{clean_target}"
            add_node(
                loop_node_id,
                f"Loop: {lp['tool']} ({lp['count']}x)",
                "failure_pattern",
                {"target": lp["target"], "count": lp["count"]}
            )
            add_edge(session_node_id, loop_node_id, "EXHIBITED_LOOP")
            add_edge(loop_node_id, f"Tool:{lp['tool']}", "LOOPED_ON_TOOL")

    return {
        "stats": {
            "total_sessions": stats["total_sessions"],
            "healthy_sessions": stats["healthy_sessions"],
            "warning_sessions": stats["warning_sessions"],
            "failed_sessions": stats["failed_sessions"],
            "sources": dict(stats["sources"]),
            "roles": dict(stats["roles"]),
            "project_tags": dict(stats["project_tags"]),
            "top_tools": dict(stats["top_tools"].most_common(10)),
            "error_types": dict(stats["error_types"].most_common(10)),
            "loops_detected": stats["loops_detected"]
        },
        "sessions": sessions,
        "nodes": nodes,
        "edges": edges
    }

def main():
    parser = argparse.ArgumentParser(description="Parse agent JSONL and Markdown logs into Graphify-compatible telemetry.")
    parser.add_argument("--logs-dir", type=str, default="", help="Directory containing session logs (default: ./logs or ~/.agents/logs).")
    parser.add_argument("--filter", type=str, default="", help="Optional substring to filter log filenames by project or keyword.")
    parser.add_argument("--output", type=str, default="", help="Output JSON path (default: graphify-out/.agent_ci_extract.json)")
    parser.add_argument("--batch-size", type=int, default=10, help="Number of files to process per batch (default: 10, max: 20).")
    parser.add_argument("--skip-audited", action="store_true", help="Skip any files matching *.audited or .audited.*.")
    parser.add_argument("--mark-audited", action="store_true", help="Append .audited to processed log files after extraction.")
    parser.add_argument("--archive-dir", type=str, default=None, help="Directory to move audited log files into (e.g. ~/.agents/logs/archived/).")
    parser.add_argument("--registry", type=str, default=str(Path.home() / ".agents" / "project-skill-map.json"), help="Path to project-skill mapping registry (optional).")
    args = parser.parse_args()

    if args.logs_dir:
        logs_dir = Path(os.path.expanduser(args.logs_dir))
    elif Path("./logs").exists():
        logs_dir = Path("./logs")
    else:
        logs_dir = Path(os.path.expanduser("~/.agents/logs"))

    if not logs_dir.exists():
        print(f"Error: Log directory '{logs_dir}' not found.", file=sys.stderr)
        sys.exit(1)

    # Load registry if available
    registry_mappings = []
    if args.registry:
        reg_path = Path(os.path.expanduser(args.registry))
        if reg_path.exists() and reg_path.is_file():
            try:
                reg_data = json.loads(reg_path.read_text(encoding="utf-8"))
                if isinstance(reg_data, dict):
                    registry_mappings = reg_data.get("mappings", [])
            except Exception as e:
                print(f"Warning: Failed to load registry '{reg_path}': {e}", file=sys.stderr)

    archive_dir = Path(os.path.expanduser(args.archive_dir)).resolve() if args.archive_dir else (logs_dir / "archived" if args.mark_audited else None)

    # Collect candidate files from logs_dir
    candidate_files = []
    for p in logs_dir.iterdir():
        if not p.is_file():
            continue
        # Avoid picking up files from inside archive_dir if archive_dir is a subfolder of logs_dir
        if archive_dir and (p.resolve() == archive_dir or p.parent.resolve() == archive_dir):
            continue

        # Check if file is audited
        if is_audited_file(p):
            if args.skip_audited:
                continue

        name_lower = p.name.lower()
        # Must be jsonl or md log file
        if not (".jsonl" in name_lower or ".md" in name_lower):
            continue

        # Filter by keyword if provided
        if args.filter and args.filter.lower() not in name_lower:
            continue

        candidate_files.append(p)

    # Sort files by mtime ascending (oldest to newest)
    candidate_files.sort(key=lambda p: p.stat().st_mtime)

    # Apply batch size: pick the latest batch (newest files)
    if args.batch_size and args.batch_size > 0:
        effective_batch_size = min(args.batch_size, 20)
        if len(candidate_files) > effective_batch_size:
            candidate_files = candidate_files[-effective_batch_size:]

    total_files = len(candidate_files)
    if total_files == 0:
        filter_msg = f" matching filter '{args.filter}'" if args.filter else ""
        audited_msg = " (unaudited)" if args.skip_audited else ""
        print(f"No session logs (*.jsonl or *.md){audited_msg}{filter_msg} found in '{logs_dir}'.")
        sys.exit(0)

    # Separate into jsonl and md
    jsonl_files = [p for p in candidate_files if ".jsonl" in p.name.lower()]
    md_files = [p for p in candidate_files if ".md" in p.name.lower() and p not in jsonl_files]

    filter_desc = f" [filter: '{args.filter}']" if args.filter else ""
    batch_desc = f" [batch: {len(candidate_files)}]" if args.batch_size else ""
    print(f"Scanning '{logs_dir}' ({len(jsonl_files)} JSONL files, {len(md_files)} Markdown files){filter_desc}{batch_desc}...")

    all_sessions = []
    processed_files = []

    # 1. Parse JSONL files (native high-speed streaming)
    for jf in jsonl_files:
        sess_list = parse_jsonl_file(jf, registry_mappings=registry_mappings)
        all_sessions.extend(sess_list)
        processed_files.append(jf)

    # 2. Parse Markdown files (legacy blocks)
    for mf in md_files:
        try:
            content = mf.read_text(encoding="utf-8", errors="ignore")
            blocks = parse_markdown_blocks(content)
            for blk in blocks:
                sess = parse_single_markdown_session(blk, mf.name, registry_mappings=registry_mappings)
                all_sessions.append(sess)
            processed_files.append(mf)
        except Exception as e:
            print(f"Warning: Failed to parse {mf.name}: {e}", file=sys.stderr)

    print(f"Parsed {len(all_sessions)} total session execution turns.")

    graph_data = build_knowledge_graph(all_sessions)

    out_path = Path(args.output) if args.output else logs_dir.parent / "graphify-out" / ".agent_ci_extract.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(graph_data, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"Extracted {len(graph_data['nodes'])} nodes and {len(graph_data['edges'])} edges.")
    print(f"Saved telemetry to: {out_path}")

    # Mark audited / Archive / Auto-purge
    if args.mark_audited or archive_dir:
        if archive_dir:
            archive_dir.mkdir(parents=True, exist_ok=True)

        if args.mark_audited:
            renamed_or_moved = []
            for pf in processed_files:
                if not pf.exists():
                    continue
                new_name = pf.name if pf.name.endswith(".audited") else f"{pf.name}.audited"
                if archive_dir:
                    dest = archive_dir / new_name
                    shutil.move(str(pf), str(dest))
                    renamed_or_moved.append(dest)
                else:
                    dest = pf.with_name(new_name)
                    pf.rename(dest)
                    renamed_or_moved.append(dest)
            action_desc = f"and moved to '{archive_dir}'" if archive_dir else "in place"
            print(f"Marked {len(renamed_or_moved)} files as audited ({action_desc}).")

        if archive_dir:
            # Move any other existing audited files in logs_dir to archive_dir
            archived_count = 0
            for p in logs_dir.iterdir():
                if p.is_file() and is_audited_file(p) and p.parent.resolve() != archive_dir:
                    try:
                        shutil.move(str(p), str(archive_dir / p.name))
                        archived_count += 1
                    except Exception as e:
                        print(f"Warning: Failed to archive {p.name}: {e}", file=sys.stderr)
            if archived_count > 0:
                print(f"Moved {archived_count} existing audited log files to '{archive_dir}'.")

            # Auto-purge: files older than 30 days in archive_dir
            now_ts = time.time()
            cutoff_ts = now_ts - (30 * 86400)
            purged_count = 0
            for arch_file in archive_dir.iterdir():
                if arch_file.is_file():
                    try:
                        if arch_file.stat().st_mtime < cutoff_ts:
                            arch_file.unlink()
                            purged_count += 1
                    except Exception as e:
                        print(f"Warning: Failed to purge {arch_file.name}: {e}", file=sys.stderr)
            if purged_count > 0:
                print(f"Purged {purged_count} archived log files older than 30 days from '{archive_dir}'.")

if __name__ == "__main__":
    main()
