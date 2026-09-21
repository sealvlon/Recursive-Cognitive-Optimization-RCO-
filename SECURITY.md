# Security

RCO Desktop is a local coordinator prototype. Its MCP endpoint and dashboard listen on loopback. The two desktop clients use separate local bearer credentials. The dashboard uses a private access token. Do not expose either port through a public tunnel or share a dashboard URL.

The desktop apps retain their own accounts, permissions and model access. The coordinator cannot forcibly cancel a command already executing in an app; a stopped run rejects late results. The included ASIC profile accepts text from the agents and performs its one allowed candidate edit in Python.

Connection setup updates local desktop configuration and activation skills only after the visual setup action, with backups of changed files under the user's `.rco/backups/` folder. It creates or reuses local RCO access keys in the Windows user environment. It does not replace the desktop apps' provider credentials. Runtime state is stored in `.rco-desktop/` beside the executable. Tokens, backup files, generated configuration, run journals and candidate output belong to the local installation and must not be committed. Use the release packaging script instead of archiving a running installation.

If you find a vulnerability, use the repository's **Security → Report a vulnerability** option when enabled. Otherwise ask the maintainer for a private reporting route without including exploit details or credentials in a public issue. Provide the affected version, a minimal reproduction and the observed impact. Redact personal paths, bearer tokens and conversation content.

There is no declared support or security-update schedule yet. Report the exact release version when requesting help.
