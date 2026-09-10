# Changelog

All notable changes to the **AGENT-CI** project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [2.1.1] - 2026-09-10

### 🛡️ Security Hardening & Runtime Defenses

- **Safe Eval Sandbox (`scripts/verify_improvements.py`)**:
  - Prevent Arbitrary Code Execution (RCE) during automated skill verification by defaulting to static AST inspection (`ast.parse`) instead of executing arbitrary third-party test subprocesses.
  - Added `--allow-code-eval` opt-in flag for explicit runtime test execution.
  - Guard JSON evaluation specs against path traversal attacks (`../`) in required files and assertion targets via `is_relative_to()`.
  - Guard `auto_revert` against reverting manual user commits by verifying commit message subject headers before execution.

- **Git Option Injection Mitigation (`scripts/check_upstream.py`)**:
  - Added `is_valid_git_url()` to reject option flags (`--upload-pack`, `-u`) and enforce safe protocol schemes (`https`, `http`, `git`, `ssh`).
  - Added `--` argument separators across all git subprocess commands (`git clone`, `git ls-remote`).

- **Prompt Injection & Path Traversal Guards (`scripts/apply_improvements.py`)**:
  - Added `_sanitize_token()` to strip newlines, carriage returns, markdown backticks, and HTML tags from error tokens and telemetry before rule synthesis.
  - Added `allowed_roots` validation in `apply_patch_to_file()` to prevent writing patches outside designated skill boundaries.
  - Replaced blind `git add -A` with explicit file staging (`files_to_stage`) to prevent committing untracked files or secrets.

### 🧪 Testing & Verification
- Added automated security test suite [`tests/test_security_hardening.py`](tests/test_security_hardening.py) verifying URL sanitization, prompt injection defenses, path traversal guards, static eval behavior, and auto-revert safety.

---

## [2.1.0] - 2026-09-10

### 🚀 Initial Public Open-Source Release

This is the premier public open-source release of **AGENT-CI**, introducing a complete 7-phase autonomous continuous improvement pipeline for AI coding agents.

### ✨ Key Capabilities & Highlights

#### 1. 7-Phase Closed-Loop Architecture
- **Phase 0 (`discover_skills.py`)**: Auto-discovery of active project workspaces from execution logs. Automatic classification of skills across `symlink`, `npx`, `system`, and `local` installations. Dynamic registry generation (`project-skill-map.json`) preserving manual user mappings.
- **Phase 1 (`parse_logs.py`)**: High-throughput log parser supporting both JSON Lines (`*.jsonl`) and Markdown (`*.md`) logs. Adaptive batching (5–20 sessions per run) with `.audited` state tracking and automatic archival. Comprehensive taxonomy for Level A (fatal crashes/timeouts), Level B (tool friction/loops), and Level C (user corrections).
- **Phase 2 (`score_engine.py`)**: Scope-routed composite health scoring ($S \in [0.0, 1.0]$) incorporating Health Rate ($30\%$), Loop Rate Inversion ($25\%$), Correction Rate Inversion ($15\%$), Upstream Freshness ($10\%$), and dynamic Learning Rate ($20\%$). Chronological delta tracking against preceding cycles in `score_history.json`.
- **Phase 3 (`check_upstream.py`)**: Remote Git status auditor using `git ls-remote` with hash caching to prevent duplicate network calls across monorepos. Full support for symlinked skills and NPX package tracking with non-blocking timeouts.
- **Phase 4 (`apply_improvements.py`)**: Autonomous upstream synchronization, targeted rule synthesis (tool loop guards, timeout pre-flight checks, interactive `grill-me` alignment gates), rule pruning, and rule evolution. Atomic git commits with annotated tags (`ci/YYYY-MM-DD`).
- **Phase 5 (`verify_improvements.py`)**: Automated execution of skill evaluation suites (`evals/evals.json`, unit tests). Automated regression guard that triggers `git revert --no-edit HEAD` and records `ci/YYYY-MM-DD-reverted` if aggregate score drops by $> 5\%$.
- **Phase 6 (`generate_report.py`)**: Publication-grade executive audit reports (`REPORT_YYYY-MM-DD_HH-mm.md`) and real-time Obsidian dashboard mirroring (`OBSIDIAN_DASHBOARD.md`) complete with status callouts, progress meters, and historical wikilinks.

#### 2. Rule Efficacy Lifecycle Engine
- Every synthesized guardrail is recorded in `rule_ledger.json` with a unique ID and baseline error counts.
- **Efficacy Tracking**: Measured over a 7-day observation window.
- **Automated Pruning**: Rules with $< 20\%$ error reduction after 7 days are automatically excised from prompt configuration files (`GEMINI.md`, `SKILL.md`) to prevent prompt bloat.
- **Rule Evolution**: Persistent error patterns trigger synthesis of evolved alternative rules with stricter preconditions.

#### 3. Safety & Developer Experience
- Safe `--dry-run` simulation mode across all pipeline scripts.
- Cross-platform support for Linux, macOS, and POSIX runtimes with clean home-directory expansion.
- Modern vector visual assets (`assets/hero-banner.svg`, `assets/pipeline-diagram.svg`).
- Seamless background scheduling integration via `/schedule`.

---

[2.1.1]: https://github.com/Mantaray91/agent-ci/releases/tag/v2.1.1
[2.1.0]: https://github.com/Mantaray91/agent-ci/releases/tag/v2.1.0
