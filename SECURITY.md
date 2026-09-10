# Security Policy

## Supported Versions

We release security updates and bug fixes for the current major release of **AGENT-CI**:

| Version | Supported          |
| :---    | :---               |
| 2.1.x   | :white_check_mark: |
| < 2.1   | :x:                |

---

## Reporting a Vulnerability

The AGENT-CI team takes the security of autonomous agent infrastructure seriously. If you discover a security vulnerability, please give us the opportunity to fix it before publishing it publicly.

### How to Report

Please report security issues privately by sending an email to:

📧 **rayonathan@hotmail.com**

Please include in your report:
- A clear description of the vulnerability.
- Steps to reproduce the issue (proof-of-concept scripts or sample log files).
- The potential impact of the issue on the host system or agent runtime.
- Any suggested fixes or mitigations.

### Response Timeline

- **Initial Response**: Within 48 hours, confirming receipt of your report.
- **Assessment & Triage**: Within 5 business days, confirming whether the report is accepted as a vulnerability.
- **Fix & Disclosure**: We will collaborate with you to develop and verify a fix before releasing a security advisory.

---

## Security Best Practices for AGENT-CI Users

When using AGENT-CI in production environments:
1. **Repository Permissions**: Ensure that the target repository where `apply_improvements.py` commits has appropriate branch protection rules if running in automated multi-user environments.
2. **Log Privacy**: Session logs may contain sensitive snippets of source code or environment variables. Ensure that log directories (`~/.agents/logs/`) have restricted filesystem permissions (`chmod 700`).
3. **Execution Sandboxing**: Run autonomous cron schedules within sandboxed environments or under dedicated non-root service users.
