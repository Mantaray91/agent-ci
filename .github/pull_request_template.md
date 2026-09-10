## Summary
<!-- Brief summary of what this pull request does and the motivation behind the change. -->

## Changes Made
<!-- List key changes introduced by this PR: -->
- 
- 

## Testing & Verification
<!-- Describe the tests and verification steps you performed. Include command invocations and results. -->
```bash
# Example verification commands:
python -m py_compile scripts/*.py
python scripts/<script_name>.py --help
```

## Checklist
- [ ] Tested with `python -m py_compile scripts/*.py` (no syntax errors).
- [ ] Verified CLI flags with `--help` on modified scripts.
- [ ] No hardcoded personal paths (e.g., no `/home/<user>/...` or hardcoded usernames; use `Path.home()`, `~`, or relative paths).
- [ ] Standard library only (no external PyPI dependencies added without discussion).
- [ ] Documentation updated (`README.md`, `ARCHITECTURE.md`, or docstrings if applicable).
- [ ] Adheres to existing code style and formatting standards.
