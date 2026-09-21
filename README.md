# Recursive Cognitive Optimization (RCO)
Recursive Cognitive Optimization (RCO) is local, MCP-based orchestration middleware, a coordination layer designed to connect AI desktop apps such as Claude Code and Codex and make them work as a human-directed reasoning team. One model proposes, another challenges, tools and evidence verify, and the work is refined through repeated rounds while the user controls the goal, boundaries, and final decision. By working through desktop apps, RCO lets people use subscriptions they already pay for, potentially reducing the need for separate API keys and additional pay-as-you-go model costs. Auto-modo available. 

# RCO Desktop

A visual Python coordinator for **Claude Code in the Claude desktop app** and **Codex desktop**. Open the Windows app, connect the two desktop sessions, and follow their work in one local dashboard.

RCO supplies the shared queue, handoffs, checkpoints and evidence record. The desktop apps supply the AI. You use their existing accounts and permissions; the Windows release includes Python and needs no terminal to run.

## Start with the Windows release

1. Download the **Windows x64 ZIP** from the repository's Releases page and choose **Extract All**.
2. Open the extracted folder and double-click **RCO Middleware.exe**. Keep the folders next to it.
3. Complete the visual connection setup, then open this extracted folder as a project in each desktop app.
4. Use the dashboard's connection buttons to copy each activation message into the matching app: `$middleware OpenAI` for Codex and `/middleware Claude` for Claude Code.

The dashboard waits until both agents are listening. The queued example then starts. Desktop apps may ask you to approve their connection or permissions, and must remain open while working. Setup or a changed connection can require restarting those apps.

[Getting started and troubleshooting](docs/GETTING_STARTED.md)

Download the source code and Windows app at alvlon.com:

http://www.alvlon.com/rco-desktop-0.1.0-source.zip — Source code.

http://www.alvlon.com/rco-desktop-0.1.0-windows-x64.zip — Windows app. Extract the ZIP and double-click RCO Middleware.exe.

## What you can control

| Mode or control | Behavior |
|---|---|
| Human | Starts the first turn once both agents listen; asks you to approve each handoff. |
| Supervised | Continues between the configured review checkpoints. |
| Auto | Continues within the visible round, turn, operation and time limits. |
| Pause / Resume | Controls when new work can start. |
| Stop task | Recalls the active claim and prevents new work. |
| Close RCO | Stops the coordinator. |

Agent replies, approval requests, evidence and stop reasons stay visible. If a command is already running in a desktop app, stop it there too; RCO cannot forcibly cancel that app's work. Late results are quarantined.

## Included example: a bounded ASIC review

This prototype includes a four-turn builder/reviewer workflow. It proposes a change to the example template's tile allocation from `8x4` to `6x4`, reviews it, applies that single change in an isolated candidate, and reviews the resulting evidence. Python checks the allowed change against a frozen baseline; the agents return text. The baseline remains unchanged.

The check establishes **static configuration only**. It does not simulate RTL, synthesize a chip, measure area or timing, or prove physical feasibility. The dated requirements snapshot is in [the profile](middleware/profiles/jane_street/requirements.json). Editing the task text does not expand the profile's permitted edits.

Project profiles can be extended in Python. The included profile is the current working example, not a general ASIC implementation engine. This is an independent project; the product and organization names identify integrations and example context, not endorsement.

## Source and validation

The **source ZIP** is the repository package. The **Windows x64 ZIP** contains the executable and its supporting files. No sibling checkout is required. Local credentials, app configuration, run history, build caches and generated candidates are excluded from distribution.

The automated tests use real local MCP transport with scripted clients, plus setup checks. Passing those tests does not establish an authenticated end-to-end exchange between the two desktop AI apps. A release should report that integration check separately.

[Publishing with clicks](docs/PUBLISHING.md) · [Release validation](docs/RELEASE_STATUS.md) · [Development and builds](docs/DEVELOPMENT.md) · [Contributing](CONTRIBUTING.md) · [Security](SECURITY.md) · [Third-party notices](THIRD_PARTY_NOTICES.md)

**License status:** a license for the original RCO and desktop application code has not yet been selected. This package does not grant a new license for that code. Included third-party material retains its existing notices and licenses.
