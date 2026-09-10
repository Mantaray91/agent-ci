#!/usr/bin/env python3
"""
session-logger.py - Telemetry Hook for AI Coding Agents.

Captures completed agent execution transcripts, tool calls, error metrics,
and session contexts, appending structured telemetry records to ~/.agents/logs/*.jsonl.
Triggered on session completion (Stop hook event).
"""

import sys
import os
import json
import re
import glob
from datetime import datetime

def clean_text(s):
    if not s:
        return ""
    return str(s).strip(" \"'\\`")

def main():
    try:
        raw_input = sys.stdin.read()
        if not raw_input or not raw_input.strip():
            print(json.dumps({"decision": "allow"}))
            return

        data = json.loads(raw_input)
        transcript = data.get("transcriptPath", "")
        artifact_dir = data.get("artifactDirectoryPath", "")
        is_cli = ("antigravity-cli" in transcript) or ("antigravity-cli" in artifact_dir)
        source_type = "CLI" if is_cli else "Chat-Agentic"

        workspace = None
        ws_paths = data.get("workspacePaths")
        if ws_paths and len(ws_paths) > 0:
            workspace = ws_paths[0]
        if not workspace or not os.path.exists(workspace):
            workspace = os.getcwd()

        conv_id = str(data.get("conversationId", ""))
        short_id = conv_id[:8] if len(conv_id) >= 8 else conv_id
        reason = str(data.get("terminationReason", ""))
        now = datetime.now()
        timestamp = now.strftime("%Y-%m-%d %H:%M:%S")
        file_time = now.strftime("%Y-%m-%d_%H-%M")

        if workspace and short_id:
            # Centralized logs directory under ~/.agents/logs
            logs_dir = os.path.expanduser(os.environ.get("AGENT_CI_LOGS_DIR", "~/.agents/logs"))
            os.makedirs(logs_dir, exist_ok=True)

            # Determine clean active directory / workspace tag
            home_dir = os.path.expanduser("~")
            norm_ws = os.path.abspath(workspace)
            norm_home = os.path.abspath(home_dir)

            if norm_ws == norm_home:
                ws_tag = "home"
            elif norm_ws.startswith(norm_home + os.sep):
                rel_ws = os.path.relpath(norm_ws, norm_home)
                ws_tag = re.sub(r'[^a-zA-Z0-9_\-\.]', '_', rel_ws)
            else:
                ws_tag = re.sub(r'[^a-zA-Z0-9_\-\.]', '_', os.path.basename(norm_ws) or "root")

            existing_logs = glob.glob(os.path.join(logs_dir, f"*_{short_id}.jsonl"))
            if existing_logs:
                target_log = existing_logs[0]
            else:
                target_log = os.path.join(logs_dir, f"agy_Session_{ws_tag}_{file_time}_{short_id}.jsonl")

            detected_role = "Main Orchestrator"
            prompts = []
            final_response = ""
            tools_called = []
            context_loaded = []
            tool_distribution = {}

            if transcript and os.path.exists(transcript):
                try:
                    with open(transcript, "r", encoding="utf-8", errors="ignore") as f:
                        for line in f:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                step = json.loads(line)
                            except Exception:
                                continue

                            step_type = step.get("type")
                            content = step.get("content", "")

                            if step_type == "USER_INPUT" and content:
                                p_text = str(content)
                                prompts.append(p_text)

                                m = re.search(r'(?i)Role:\s*([^\r\n]+)', p_text)
                                if m:
                                    detected_role = m.group(1).strip()
                                else:
                                    m = re.search(r'(?i)You are the\s+([^\r\n(]+)', p_text)
                                    if m:
                                        detected_role = m.group(1).strip()
                                    else:
                                        m = re.search(r'(?i)(?:sub-agent spesialis yang mengeksekusi|specialized subagent executing)\s+([^\r\n:]+)', p_text)
                                        if m:
                                            detected_role = "Subagent " + m.group(1).strip()
                                        else:
                                            m = re.search(r'(?i)skill:\s*([a-zA-Z0-9_]+)', p_text)
                                            if m:
                                                detected_role = f"Subagent ({m.group(1).strip()})"

                            tool_calls = step.get("tool_calls")
                            if tool_calls and isinstance(tool_calls, list):
                                for tc in tool_calls:
                                    tool_name = str(tc.get("name", ""))
                                    args = tc.get("args", {}) or {}

                                    clean_path = clean_text(args.get("AbsolutePath", ""))
                                    clean_target = clean_text(args.get("TargetFile", ""))
                                    clean_cmd = clean_text(args.get("CommandLine", "") or args.get("command", ""))

                                    if tool_name == "view_file" and re.search(r'skills[/\\]|SKILL\.md|transcript', clean_path):
                                        file_name = os.path.basename(clean_path) or clean_path
                                        context_loaded.append(file_name)

                                    tool_record = {
                                        "name": tool_name,
                                        "target": clean_target,
                                        "cmd": clean_cmd,
                                        "path": clean_path,
                                    }
                                    if re.search(r'^mcp_|^call_mcp_tool', tool_name):
                                        if args.get("ServerName"):
                                            tool_record["server"] = str(args.get("ServerName"))
                                        if args.get("ToolName"):
                                            tool_record["tool"] = str(args.get("ToolName"))

                                    if args:
                                        tool_record["args"] = args

                                    tools_called.append(tool_record)
                                    tool_distribution[tool_name] = tool_distribution.get(tool_name, 0) + 1

                            if step_type == "PLANNER_RESPONSE" and content:
                                final_response = str(content)
                except Exception:
                    pass

            def unique_list(seq):
                seen = set()
                out = []
                for x in seq:
                    if x not in seen:
                        seen.add(x)
                        out.append(x)
                return out

            session_turn = {
                "ts": timestamp,
                "src": source_type,
                "id": conv_id,
                "short_id": short_id,
                "role": detected_role,
                "workspace": workspace,
                "term_reason": reason,
                "prompts": unique_list(prompts),
                "context": unique_list(context_loaded),
                "tools": tools_called,
                "metrics": {
                    "total_tools": len(tools_called),
                    "tool_distribution": tool_distribution
                },
                "final_response": final_response
            }

            with open(target_log, "a", encoding="utf-8") as out_f:
                out_f.write(json.dumps(session_turn, ensure_ascii=False) + "\n")
    except Exception:
        pass

    print(json.dumps({"decision": "allow"}))

if __name__ == "__main__":
    main()
