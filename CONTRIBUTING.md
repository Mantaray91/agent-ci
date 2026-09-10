# Contributing to AGENT-CI

Thank you for your interest in contributing to **AGENT-CI**! 🚀

AGENT-CI is built to advance autonomous, reliable, and self-healing AI coding agent infrastructure. We welcome bug reports, improvements, documentation enhancements, and feature proposals.

---

## 🧭 Code of Conduct

All contributors are expected to adhere to our [Code of Conduct](CODE_OF_CONDUCT.md). Please read it before participating.

---

## 🛠️ Development Setup & Standards

AGENT-CI is implemented in Python 3.10+ without external third-party dependencies beyond the Python standard library. This ensures maximum portability, zero-dependency installation across diverse agent runtimes, and fast startup times.

### Code Style Guidelines

1. **PEP 8 Compliance**: Follow standard Python conventions (4 spaces indentation, 100-character line length where practical).
2. **Type Annotations**: All function signatures must include comprehensive Python type hints (`from __future__ import annotations`, `typing`).
3. **Docstrings**: Every module, class, and function must have descriptive docstrings adhering to Google or Sphinx docstring format (detailing `Args:`, `Returns:`, and `Raises:`).
4. **Error Handling**: Gracefully handle missing files, corrupted logs, or network errors with informative stderr warnings rather than crashing unhandled.
5. **Cross-Platform Compatibility**: Use `pathlib.Path` for all filesystem operations. Never assume POSIX-only or Windows-only paths. Use `Path.home()` rather than hardcoding user home paths.

---

## 🧪 Testing Your Changes Locally

Before submitting a pull request, run the following verification steps locally:

### 1. Python Syntax & Compilation Verification

Ensure all scripts compile cleanly without any syntax errors:

```bash
python3 -m py_compile scripts/*.py
```

### 2. Dry-Run Execution Test

Run the full pipeline in simulation mode against sample or active logs:

```bash
# Phase 0: Test auto-discovery
python3 scripts/discover_skills.py --output /tmp/test_registry.json

# Phase 1: Test log parsing
python3 scripts/parse_logs.py --batch-size 5 --output /tmp/test_telem.json

# Phase 2: Test score calculation
python3 scripts/score_engine.py \
  --telemetry /tmp/test_telem.json \
  --registry /tmp/test_registry.json \
  --history /tmp/test_history.json \
  --rule-ledger /tmp/test_ledger.json

# Phase 3: Test upstream check
python3 scripts/check_upstream.py \
  --registry /tmp/test_registry.json \
  --output /tmp/test_upstream.json

# Phase 4: Test rule synthesis in dry-run mode
python3 scripts/apply_improvements.py \
  --dry-run \
  --telemetry /tmp/test_telem.json \
  --registry /tmp/test_registry.json \
  --scores /tmp/test_history.json \
  --upstream /tmp/test_upstream.json \
  --rule-ledger /tmp/test_ledger.json

# Phase 5: Test evaluation verification in dry-run mode
python3 scripts/verify_improvements.py \
  --dry-run \
  --history /tmp/test_history.json \
  --rule-ledger /tmp/test_ledger.json

# Phase 6: Test report generation
python3 scripts/generate_report.py \
  --telemetry /tmp/test_telem.json \
  --scores /tmp/test_history.json \
  --upstream /tmp/test_upstream.json \
  --rule-ledger /tmp/test_ledger.json
```

---

## 📝 Commit Conventions

We enforce [Conventional Commits](https://www.conventionalcommits.org/) for all commit messages. This ensures clean changelog generation and clear version history:

- `feat:` A new feature or capability (e.g. `feat: add support for local Ollama agent logs`)
- `fix:` A bug fix (e.g. `fix: prevent divide-by-zero when active rules list is empty`)
- `docs:` Documentation changes only (e.g. `docs: update quickstart instructions in README`)
- `refactor:` Code changes that neither fix a bug nor add a feature
- `perf:` Performance improvements (e.g. `perf: cache git remote hashes per repository`)
- `test:` Adding or correcting tests or evals
- `chore:` Maintenance tasks, metadata, or tooling updates

**Example**:
```
feat(score-engine): add weighted penalty for repeated tool timeouts

Introduces timeout penalty factor to composite scoring formula, ensuring
unstable network tools receive proper triage during CI cycles.
```

---

## 🔄 Pull Request Process

1. **Fork the repository** on GitHub and create your branch from `main`:
   ```bash
   git checkout -b feat/my-new-feature
   ```
2. **Make your changes** following the code style and testing guidelines above.
3. **Commit your work** using conventional commit messages.
4. **Push your branch** to your fork:
   ```bash
   git push origin feat/my-new-feature
   ```
5. **Open a Pull Request** against `Mantaray91/agent-ci:main`.
6. Fill in the Pull Request template, referencing any related issues.
7. Ensure all automated GitHub Actions CI checks pass.

---

## 💬 Getting Help

If you have questions, feedback, or need guidance on an architectural change, please open an Issue on GitHub or reach out to Raymond Alexander Yonathan at `rayonathan@hotmail.com`.
