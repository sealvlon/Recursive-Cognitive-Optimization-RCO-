# Getting started

You need Windows x64, an installed Codex desktop app, and the Claude desktop app with a working Claude Code session. Sign in to each app normally. RCO coordinates these sessions; it does not provide their accounts or model access. Desktop model use still requires the connectivity and account access those apps normally need.

## Open the app

Download the Windows x64 ZIP, use **Extract All**, and keep the entire extracted folder in a location where you can save files. Double-click **RCO Middleware.exe**. Python is included. The source ZIP is intended for development and does not contain a built executable.

On first launch, the **Connect your desktop apps** window explains the local setup. Choose **Configure desktop apps**. If a different RCO connection already exists, the button reads **Back up and use this app** so you can deliberately replace it. Changed settings are backed up first.

Setup installs the activation skills and local connection settings. It creates or reuses local RCO access keys, without changing your provider accounts. Restart both desktop apps after setup so they load the connection.

Open the extracted RCO folder as a project in each desktop app. The dashboard's two connection buttons copy these activation messages:

| Desktop session | Message to send once |
|---|---|
| Codex | `$middleware OpenAI` |
| Claude Code | `/middleware Claude` |

Send each message in its matching project chat. Allow the desktop app to connect when it requests permission. The activation skills register, listen for work, return results and continue listening during their bounded activation period. The dashboard distinguishes a registered app from an actively listening agent.

## Run the included task

The ASIC example is queued when the app opens. Before both agents connect, you can adjust its review focus, mode and limits in the dashboard. Human mode is the default. Once both listen, the first turn starts; approve each subsequent handoff to continue.

The example finishes after four successful agent turns and its verification checks. Its retained candidate and evidence are linked from the dashboard. Only the candidate's tile-allocation field is changed. The original example files are preserved.

A round is two completed agent turns. Coordinator operations count claims, submissions and Python verification checks. Work performed internally by a desktop app is not included in that counter. Activation expires after 30 minutes or 200 fetches; reconnect using the same activation message if the dashboard asks you to.

## Pause, stop and reopen

**Pause** holds new work; **Resume** continues a paused run when it is safe to do so. **Stop task** stops new turns and recalls the current claim. It cannot cancel an operation already executing inside Claude Code or Codex. Stop that operation in its desktop app and use the dashboard's confirmation before starting another run.

Double-clicking the executable again reopens the running dashboard. After an interruption, recovery pauses rather than repeating a turn whose result is uncertain. Follow the displayed recovery reason.

## If a connection is waiting

- Keep both desktop apps open. Send each activation message in a project chat that has loaded the local RCO connection.
- After initial setup, fully restart an app if it has not loaded the connection or skill; then open a new project chat.
- A “connected” registration is not yet an active listener. The agent must continue into its waiting step before the task begins.
- If another RCO instance is already using the local ports, close it through its dashboard before launching this copy.
- If the baseline or frozen evaluator changed, the example stops. Use a fresh extraction of the release to restore its reviewed inputs.

Keep the app and its supporting folders together. Moving only the executable will leave the project assets behind. Local configuration, logs and run state are saved in `.rco-desktop/` inside the extracted folder. The local dashboard uses your default browser; its private address should not be shared.
