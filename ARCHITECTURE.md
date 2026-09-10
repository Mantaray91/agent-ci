# AGENT-CI Technical Architecture Specification

This document provides a deep-dive technical specification of the **AGENT-CI** system architecture, data models, state management, mathematical scoring formulation, rule efficacy lifecycle engine, and regression recovery mechanisms.

---

## 📑 Table of Contents

1. [Architectural Overview](#1-architectural-overview)
2. [End-to-End 7-Phase Execution Pipeline](#2-end-to-end-7-phase-execution-pipeline)
3. [Telemetry & State File Schemas](#3-telemetry--state-file-schemas)
   - [3.1 `project-skill-map.json` (Discovery Registry)](#31-project-skill-mapjson-discovery-registry)
   - [3.2 `rule_ledger.json` (Rule Efficacy Ledger)](#32-rule_ledgerjson-rule-efficacy-ledger)
   - [3.3 `score_history.json` (Historical Score Ledger)](#33-score_historyjson-historical-score-ledger)
   - [3.4 Telemetry Extraction Schema (`parse_logs.py`)](#34-telemetry-extraction-schema-parse_logspy)
4. [Mathematical Formulation](#4-mathematical-formulation)
   - [4.1 Composite Health Score ($Score$)](#41-composite-health-score-score)
   - [4.2 Learning Rate Metric ($R$)](#42-learning-rate-metric-r)
   - [4.3 Efficacy Measurement ($\text{Efficacy}$)](#43-efficacy-measurement-textefficacy)
5. [Rule Efficacy Lifecycle State Machine](#5-rule-efficacy-lifecycle-state-machine)
   - [5.1 State Machine Transitions](#51-state-machine-transitions)
   - [5.2 Rule Pruning & Clean Removal](#52-rule-pruning--clean-removal)
   - [5.3 Evolved Rule Synthesis](#53-evolved-rule-synthesis)
6. [Regression Guard & Automated Rollback](#6-regression-guard--automated-rollback)
   - [6.1 Continuous Eval Verification](#61-continuous-eval-verification)
   - [6.2 Automated Revert Trigger](#62-automated-revert-trigger)
7. [Obsidian Vault Mirroring Architecture](#7-obsidian-vault-mirroring-architecture)

---

## 1. Architectural Overview

Traditional continuous integration systems validate deterministic software builds through compilers, linters, and unit tests. In contrast, modern AI coding agents operate non-deterministically across multi-turn interactions. Failure modes manifest as infinite tool loops, repeated runtime exceptions, unhandled tool timeouts, and semantic misalignment requiring user corrections.

**AGENT-CI** closes the feedback loop between agent execution and agent behavior:

```
+------------------------------------------------------------------------------------+
|                                    AGENT RUNTIME                                   |
|   (Interactive Coding Sessions, Multi-turn Tool Invocations, File Edits, Bash)      |
+------------------------------------------------------------------------------------+
                                           |
                                           v
                              Session Logs (*.jsonl, *.md)
                                           |
    +--------------------------------------+------------------------------------+
    |                         AGENT-CI 7-PHASE PIPELINE                         |
    |                                                                           |
    |  [Phase 0: discover_skills]   --> Auto-discovers workspaces, tools, repos |
    |  [Phase 1: parse_logs]        --> Extracts Level A, B, C failure signatures|
    |  [Phase 2: score_engine]      --> Computes Composite Score & Learning Rate|
    |  [Phase 3: check_upstream]    --> Audits Git/NPX/Symlink freshness        |
    |  [Phase 4: apply_improvements]--> Injects rules, prunes stale, evolves     |
    |  [Phase 5: verify_improvements]--> Executes evals; Auto-reverts on regress |
    |  [Phase 6: generate_report]   --> Writes Markdown & Mirrors to Obsidian   |
    +--------------------------------------+------------------------------------+
                                           |
                                           v
                           Updated Rules, Skills & Dashboards
```

---

## 2. End-to-End 7-Phase Execution Pipeline

The continuous improvement engine executes through seven discrete, decoupled phases:

### Phase 0: Auto-Discovery (`scripts/discover_skills.py`)
- **Objective**: Dynamically discover active project workspaces and installed skills without requiring manual user registration.
- **Mechanism**:
  - Scans session log headers and tool execution arguments for workspace paths.
  - Normalizes path structures across POSIX environments and directory mounts.
  - Classifies installed skills into four operational install types:
    - `symlink`: Filesystem symlink pointing to an external Git repository. Resolves target to find root Git remote.
    - `npx`: Node.js / NPX package execution wrapper.
    - `system`: Globally installed executable or system-level utility.
    - `local`: Project-specific custom skill or standalone script.
  - Produces or updates `project-skill-map.json` while strictly preserving manual mapping entries (`"source": "manual"`).

### Phase 1: Log Audit & Telemetry Extraction (`scripts/parse_logs.py`)
- **Objective**: Ingest structured and semi-structured session logs and extract failure patterns into normalized telemetry.
- **Adaptive Batching**: Processes logs in configurable batch windows (default: 10, max: 20 sessions) to maintain minimal memory overhead.
- **Audited State Tracking**: Appends `.audited` to processed files or moves them to `logs/archived/` to ensure idempotency.
- **Failure Taxonomy**:
  - **Level A (Fatal Crashes & Timeouts)**: Unhandled process terminations, syntax aborts, socket dropouts, and tool timeouts (`AbnormalTermination`, `ToolTimeout`).
  - **Level B (Friction & Loops)**: Repetitive identical tool calls ($\ge 3$ consecutive calls on same arguments), missing files (`FileNotFoundError`), and recurring tool exceptions.
  - **Level C (Outcome Misalignment)**: Explicit user correction indicators in natural language (*"salah"*, *"bukan itu"*, *"ulangi"*, *"error lagi"*, *"retry"*).

### Phase 2: Score Engine & Delta Tracking (`scripts/score_engine.py`)
- **Objective**: Route telemetry to individual project scopes and compute multi-dimensional health metrics.
- **Scope Routing**: Partitions session telemetry using regex patterns defined in the discovery registry.
- **Historical Deltas**: Compares current scores against the preceding cycle recorded in `score_history.json` (tracking $\Delta Score$).
- **Rule Efficacy Integration**: Analyzes active rules from `rule_ledger.json` to compute the Learning Rate metric.

### Phase 3: Upstream Dependency Check (`scripts/check_upstream.py`)
- **Objective**: Audit remote Git repositories, NPX packages, and symlinked skills for upstream changes.
- **Mechanism**:
  - Compares local commit hashes in `.skill-lock.json` against remote repository heads using `git ls-remote`.
  - Employs per-repository hash caching to prevent redundant network requests when multiple skills share a monorepo.
  - Classifies status as `CURRENT`, `UPDATE_AVAILABLE`, `LOCAL_ONLY`, `SYSTEM_MANAGED`, or `CHECK_FAILED`.

### Phase 4: Apply Improvements & Rule Lifecycle (`scripts/apply_improvements.py`)
- **Objective**: Autonomous synchronization of upstream skills, synthesis of targeted behavioral guardrails, pruning of ineffective rules, and rule evolution.
- **Automated Hardening Generators**:
  - `LOOP_GUARDS`: Injects backoff limits and anti-thrashing rules when tool loop frequencies exceed thresholds.
  - `PRE_FLIGHT_CHECKS`: Injects verification gates before invoking tools prone to timeouts or fatal terminations.
  - `SCOPE_ALIGNMENT`: Injects interactive clarification protocols (`grill-me`) when Level C user corrections spike.
- **Lifecycle Engine**:
  - Records newly injected rules into `rule_ledger.json` with unique rule IDs and baseline error frequencies.
  - Evaluates rules over a 7-day window. Rules achieving $\ge 30\%$ error reduction are marked `EFFECTIVE`. Rules failing to reach $20\%$ reduction are excised from configuration files (`GEMINI.md`, `SKILL.md`) and marked `PRUNED`.
  - For pruned rules where the target error persists, synthesizes an `EVOLVED` alternative rule with stricter constraints.
- **Atomic Git Commit**: Commits all file modifications in a single git commit tagged `ci/YYYY-MM-DD`.

### Phase 5: Verification & Automated Rollback (`scripts/verify_improvements.py`)
- **Objective**: Validate patched skills and rules against evaluation suites to prevent performance regressions.
- **Execution**:
  - Locates and executes eval suites (e.g. `evals/evals.json` or Python test scripts) for each modified component.
  - Compares test pass rates against baseline scores.
- **Regression Guard**:
  - If aggregate score or test pass rate regresses by more than the allowable tolerance threshold (default: $5\%$), the engine halts execution, runs `git revert --no-edit HEAD`, tags the rollback as `ci/YYYY-MM-DD-reverted`, and outputs a critical incident alert.

### Phase 6: Report Generation & Obsidian Mirroring (`scripts/generate_report.py`)
- **Objective**: Aggregate all phase outputs into a comprehensive audit report and maintain a real-time Obsidian dashboard.
- **Artifacts**:
  - Historical Markdown report: `agent-ci-reports/REPORT_YYYY-MM-DD_HH-mm.md`.
  - Live Obsidian dashboard: `agent-ci-reports/OBSIDIAN_DASHBOARD.md` (featuring callouts, progress bars, and bidirectional navigation links).
  - Archival: Moves audited session logs into `logs/archived/`.

---

## 3. Telemetry & State File Schemas

### 3.1 `project-skill-map.json` (Discovery Registry)

Maps workspace folder regex patterns to project scope names and associated skill directories:

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "ProjectSkillMap",
  "type": "object",
  "required": ["auto_discovered_at", "mappings", "global_skills"],
  "properties": {
    "auto_discovered_at": {
      "type": "string",
      "format": "date-time",
      "description": "ISO 8601 UTC timestamp of discovery execution"
    },
    "mappings": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["project_name", "workspace_pattern", "skill_paths", "source"],
        "properties": {
          "project_name": { "type": "string", "example": "web-backend" },
          "workspace_pattern": { "type": "string", "example": "web-backend|api-service" },
          "skill_paths": {
            "type": "array",
            "items": { "type": "string" }
          },
          "source": {
            "type": "string",
            "enum": ["auto:workspace_scan", "manual"],
            "description": "Origin of mapping. 'manual' entries are preserved across discovery runs."
          }
        }
      }
    },
    "global_skills": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["name", "path", "install_type"],
        "properties": {
          "name": { "type": "string" },
          "path": { "type": "string" },
          "install_type": {
            "type": "string",
            "enum": ["symlink", "npx", "system", "local"]
          },
          "remote_url": { "type": ["string", "null"] }
        }
      }
    }
  }
}
```

---

### 3.2 `rule_ledger.json` (Rule Efficacy Ledger)

Tracks behavioral guardrail rules through their lifecycle from injection to measurement, pruning, and evolution:

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "RuleLedger",
  "type": "object",
  "required": ["rules", "pruned", "evolved"],
  "properties": {
    "rules": {
      "type": "array",
      "items": {
        "type": "object",
        "required": [
          "id",
          "section",
          "target_file",
          "target_error_type",
          "rule_text",
          "rule_text_hash",
          "applied_at",
          "baseline_count",
          "baseline_window_days",
          "measurements",
          "status",
          "lifecycle"
        ],
        "properties": {
          "id": { "type": "string", "example": "RULE-LOOP-run_command-001" },
          "section": { "type": "string", "example": "HARD_GATES" },
          "target_file": { "type": "string", "example": "~/.agents/GEMINI.md" },
          "target_error_type": { "type": "string", "example": "loop:run_command" },
          "rule_text": { "type": "string" },
          "rule_text_hash": { "type": "string", "example": "a3f89b12c0de" },
          "rule_text_preview": { "type": "string" },
          "applied_at": { "type": "string", "format": "date-time" },
          "baseline_count": { "type": "integer", "minimum": 0 },
          "baseline_window_days": { "type": "integer", "default": 7 },
          "measurements": {
            "type": "array",
            "items": {
              "type": "object",
              "required": ["timestamp", "occurrence_count", "efficacy_percent"],
              "properties": {
                "timestamp": { "type": "string", "format": "date-time" },
                "occurrence_count": { "type": "integer" },
                "efficacy_percent": { "type": "number" }
              }
            }
          },
          "status": {
            "type": "string",
            "enum": ["ACTIVE", "EFFECTIVE", "PRUNED", "REMOVED"]
          },
          "lifecycle": {
            "type": "string",
            "enum": ["MEASURING", "CONFIRMED", "PRUNED", "EVOLVED"]
          }
        }
      }
    },
    "pruned": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["id", "pruned_at", "reason", "target_file"],
        "properties": {
          "id": { "type": "string" },
          "pruned_at": { "type": "string", "format": "date-time" },
          "reason": { "type": "string" },
          "target_file": { "type": "string" },
          "final_efficacy_percent": { "type": "number" }
        }
      }
    },
    "evolved": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["original_rule_id", "evolved_rule_id", "evolved_at", "rationale"],
        "properties": {
          "original_rule_id": { "type": "string" },
          "evolved_rule_id": { "type": "string" },
          "evolved_at": { "type": "string", "format": "date-time" },
          "rationale": { "type": "string" }
        }
      }
    }
  }
}
```

---

### 3.3 `score_history.json` (Historical Score Ledger)

Maintains chronological execution records (up to 30 cycles FIFO) for trend analysis:

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "ScoreHistory",
  "type": "object",
  "required": ["cycles"],
  "properties": {
    "cycles": {
      "type": "array",
      "maxItems": 30,
      "items": {
        "type": "object",
        "required": ["timestamp", "scores"],
        "properties": {
          "timestamp": { "type": "string", "format": "date-time" },
          "scores": {
            "type": "object",
            "additionalProperties": {
              "type": "object",
              "required": ["score", "delta"],
              "properties": {
                "score": { "type": "number", "minimum": 0.0, "maximum": 1.0 },
                "delta": { "type": "number" }
              }
            }
          }
        }
      }
    }
  }
}
```

---

### 3.4 Telemetry Extraction Schema (`parse_logs.py`)

Outputs graph-compatible telemetry containing raw sessions, aggregate statistics, and failure signatures:

```json
{
  "stats": {
    "total_sessions": 142,
    "healthy_sessions": 128,
    "warning_sessions": 11,
    "failed_sessions": 3,
    "sources": { "antigravity": 98, "claude": 44 },
    "project_tags": { "web-backend": 64, "data-service": 40, "global": 38 },
    "top_tools": { "run_command": 512, "view_file": 320, "replace_file_content": 140 },
    "error_types": { "ToolExecutionError": 12, "ToolTimeout": 2 },
    "loops_detected": 4
  },
  "sessions": [
    {
      "id": "session_2026-09-10_01",
      "timestamp": "2026-09-10T08:30:00Z",
      "workspace": "/home/user/projects/web-backend",
      "project_tag": "web-backend",
      "health": "HEALTHY",
      "tools_used": { "run_command": 8, "view_file": 4 },
      "errors": [],
      "loops": [],
      "user_corrections": false
    }
  ]
}
```

---

## 4. Mathematical Formulation

### 4.1 Composite Health Score ($Score$)

For any project scope $p$, the Composite Health Score $S_p \in [0.0, 1.0]$ is computed as:

$$S_p = 0.30H_p + 0.25(1 - L_p) + 0.15(1 - C_p) + 0.10F + 0.20R$$

Where:
- **$H_p$ (Health Rate)**:
  $$H_p = \frac{|\{s \in \text{Sessions}_p : \text{Health}(s) = \text{HEALTHY}\}|}{|\text{Sessions}_p|}$$
  *Evaluates the fraction of sessions concluding without fatal crashes or recurring errors.*

- **$L_p$ (Loop Rate)**:
  $$L_p = \frac{|\{s \in \text{Sessions}_p : \text{Loops}(s) > 0\}|}{|\text{Sessions}_p|}$$
  *Penalizes non-terminating tool iterations ($\ge 3$ consecutive repeated invocations on the same arguments).*

- **$C_p$ (Correction Rate)**:
  $$C_p = \frac{|\{s \in \text{Sessions}_p : \text{UserCorrections}(s) = \text{True}\}|}{|\text{Sessions}_p|}$$
  *Reflects the frequency of human interventions to correct misguided agent actions.*

- **$F$ (Upstream Freshness)**:
  $$F = \frac{|\{k \in \text{TrackedSkills} : \text{Status}(k) = \text{CURRENT}\}|}{|\text{TrackedSkills}|}$$
  *Measures alignment with remote repository heads ($F = 1.0$ if no external skills are tracked).*

- **$R$ (Learning Rate)**:
  $$R = \begin{cases} 
  \frac{|\text{EffectiveRules}|}{|\text{ActiveRules}|} & \text{if } |\text{ActiveRules}| > 0 \\ 
  1.0 & \text{otherwise} 
  \end{cases}$$
  *Measures the effectiveness of autonomous guardrails.*

---

### 4.2 Learning Rate Metric ($R$)

The Learning Rate directly penalizes speculative, ineffective prompt bloat:
- If a CI system indiscriminately injects 10 rules, but only 2 reduce errors while 8 produce zero measurable impact, $R = \frac{2}{10} = 0.20$.
- Because $R$ holds a $20\%$ weight in the composite score, unchecked rule proliferation severely degrades the system score.
- This creates mathematical pressure for the system to **prune** non-performing rules.

---

### 4.3 Efficacy Measurement ($\text{Efficacy}$)

For an active rule $r$ targeting error type $E$ with baseline occurrence count $B_r$ over a 7-day evaluation window:

$$\text{Efficacy}(r) = \frac{B_r - C_{r,\text{current}}}{B_r}$$

Where $C_{r,\text{current}}$ is the observed frequency of error type $E$ normalized to the current 7-day session volume.

---

## 5. Rule Efficacy Lifecycle State Machine

### 5.1 State Machine Transitions

```
                    +-----------------------------+
                    |                             |
                    v                             |
              [ 1. ACTIVE ]                       |
             (Applied by CI)                      |
                    |                             |
     +--------------+--------------+              |
     | (>= 30% reduction)          | (< 20% reduction after 7d)
     v                             v              |
[ 2. EFFECTIVE ]            [ 3. PRUNED ]         |
(Maintained & Reinforced)   (Excised from file)   |
                                   |              |
                    +--------------+--------------+
                    | (Error persists in telemetry)
                    v
              [ 4. EVOLVED ]
        (Synthesize stricter rule)
                    |
                    +-----------------------------+
```

1. **ACTIVE**: Rule is injected into the target prompt file (`GEMINI.md` or `SKILL.md`) inside designated markers (`<!-- AGENT-CI:RULE-START -->`) and recorded in `rule_ledger.json`.
2. **EFFECTIVE**: Error frequency drops by $\ge 30\%$ relative to baseline. The rule is verified as beneficial and maintained.
3. **PRUNED**: If after 7 days error reduction is $< 20\%$, the rule is classified as ineffective. The rule string is excised from the target markdown file using regex/AST manipulation, and status is marked `PRUNED`.
4. **EVOLVED**: If an error continues to appear after rule pruning, AGENT-CI synthesizes an evolved variant (e.g. converting a general warning into a strict pre-flight tool verification gate).

---

### 5.2 Rule Pruning & Clean Removal

When a rule is pruned, `apply_improvements.py` performs atomic file surgery:
1. Locates the exact rule block via rule ID or SHA-256 text hash.
2. Removes the block without disturbing adjacent manual instructions or other active rules.
3. Records the pruning event with timestamp and rationale in the `pruned` array of `rule_ledger.json`.
4. Stages and commits the pruning operation in Git.

---

### 5.3 Evolved Rule Synthesis

Rule evolution handles stubborn agent failures where simple negative constraints failed:
- **Generation Strategy**: If a negative constraint (e.g. *"Do not run command X repeatedly"*) fails, the engine evolves the rule into an affirmative pre-flight requirement (e.g. *"Before running command X, execute check Y and inspect return code"*).
- **ID Tracking**: Evolved rules receive lineage identifiers (e.g., `RULE-LOOP-run_command-001` evolves to `RULE-LOOP-run_command-001-EVO1`).

---

## 6. Regression Guard & Automated Rollback

### 6.1 Continuous Eval Verification

During Phase 5 (`verify_improvements.py`), modified components are tested against evaluation suites:
- Test suites are loaded from `evals/evals.json` or discovered `test_*.py` files.
- Each evaluation case verifies tool call correctness, regex output constraints, or return codes.
- An aggregate pass rate $P_{\text{post}} \in [0.0, 1.0]$ is computed and compared against $P_{\text{pre}}$.

---

### 6.2 Automated Revert Trigger

If performance degradation exceeds the regression threshold $\theta$ (default: $\theta = 0.05$ or $5\%$):

$$\Delta P = P_{\text{pre}} - P_{\text{post}} > \theta$$

1. **Immediate Execution**:
   ```bash
   git revert --no-edit HEAD
   ```
2. **Incident Tagging**:
   ```bash
   git tag "ci/$(date +%Y-%m-%d)-reverted"
   ```
3. **Audit Documentation**:
   - Logs an alert in `agent_ci_verification.json`.
   - Embeds a `[!CAUTION]` block in the executive report highlighting the reverted commit and regression delta.

---

## 7. Obsidian Vault Mirroring Architecture

To enable seamless human oversight within knowledge management systems, AGENT-CI maintains a mirrored dashboard in Obsidian:

```
agent-ci-reports/
├── OBSIDIAN_DASHBOARD.md           <-- Central live dashboard (auto-updated)
├── REPORT_2026-09-08_06-00.md      <-- Historical cycle report
├── REPORT_2026-09-09_06-00.md      <-- Historical cycle report
└── REPORT_2026-09-10_06-00.md      <-- Historical cycle report
```

### Dashboard Design Principles
- **Native Obsidian Callouts**: Renders `[!SUCCESS]`, `[!WARNING]`, `[!DANGER]`, and `[!INFO]` blocks for instant visual triaging.
- **ASCII Progress Gauges**: Displays scores using visual UTF-8 bar charts (`████████░░ 82%`).
- **Internal Wikilinks**: Links executive summaries to detailed historical cycle reports using Obsidian `[[REPORT_YYYY-MM-DD_HH-mm|Detailed Report]]` wikilink syntax.
- **Rule Lifecycle Summaries**: Displays tables of currently active rules, effective rates, and recently pruned/evolved guardrails.
