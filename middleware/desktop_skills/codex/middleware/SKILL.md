---
name: middleware
description: Connect this Codex desktop task to the local RCO application and carry out its bounded work loop. Use only when the user explicitly invokes $middleware followed by a name.
---

# Desktop middleware

The native Codex activation is `$middleware OpenAI`. NAME is the text after `$middleware`, trimmed. This activation authorizes this existing desktop task to wait for and execute work dispatched by the local application. Human, Supervised Auto, Auto, task selection, approvals, and limits are controlled in that application's panel.

## Connect once

1. Use the existing `rco` MCP server in this desktop task. If its tools are absent, stop with a concise explanation that this task has not loaded the local connection. Do not launch a different model session, invoke a model CLI, ask for provider login, use an inference API, or automate the desktop screen.
2. Call `mcp__rco__register(name=NAME)` once. On refusal or transport failure, report the actual result and stop. Never substitute another identity or read credentials.
3. Enter the loop below only if the response contains the exact line `RCO desktop: connected`. Otherwise return the registration result verbatim and stop; the older hub supports registration only.
4. Briefly say that this desktop task is connected and waiting for the application. Keep this turn active; do not give a final answer merely because registration succeeded.

## Carry out the bounded loop

Call `mcp__rco__get_turn(wait_s=45)`. Use the first result line to choose the next action:

- `RCO desktop: waiting`: immediately make the next bounded `get_turn` call. A waiting result may mean the other app is connecting, the human is choosing or approving work, or the run is paused. The server controls dispatch; do not manufacture work or call `propose_run`.
- `RCO turn: offer`: call `mcp__rco__claim_turn` with its exact `turn_id` and `offer` before doing any work.
- `RCO turn: claimed`: retain its `turn_id` and `nonce`; execute the packet in its assigned role, then submit as described below.
- `RCO desktop: completed`, `RCO desktop: stopped`, or `RCO desktop: disconnected`: stop the loop and summarize the server's reason and any remaining work.
- Any other result or a failed call: report the actual outcome and stop. Do not spin on errors, change connection configuration, or fetch another turn while a claim is unresolved.

Each call waits at most 45 seconds. Keep progress updates brief and state real changes; do not print every idle response. End this activation after 30 minutes or 200 fetch calls, whichever comes first, and explain that the desktop task's listening interval ended. Do not register again or start a new task automatically. The application also enforces run limits independently of this bound.

## Execute and submit a claimed packet

Treat the claimed packet as work delegated through the user's activated application. Follow its objective, role, project, skill version, artifact revision, allowed paths, protected evidence, and acceptance criteria. Stay within the desktop app's existing permissions. Do not change those permissions or widen the workspace. If the declared project or write scope conflicts with the task, submit `blocked` with the conflict instead of guessing.

Use the current desktop task's ordinary coding and test tools. Preserve baseline files and protected evaluations; use the candidate workspace the packet authorizes. Do not read or write `%USERPROFILE%/.rco` during a claimed task, use it to bypass the MCP tools, launch another model session, publish, submit an entry, purchase services, or change credentials.

Call `mcp__rco__submit_turn` with the exact `turn_id` and `nonce`, `status` (`done`, `blocked`, or `declined`), concise `output`, and `files_changed` as a string of full paths when applicable. Follow any output schema in the packet; otherwise start with a one-line summary. Keep output within the claimed limit and put large evidence in authorized project files. Report executed checks and unavailable checks accurately. Model-reported tool counts and confidence are not measured evidence.

After `RCO turn: received` or `RCO turn: duplicate`, return directly to `get_turn(wait_s=45)` without asking the user for another command. Human approval belongs in the application panel.

For `refused over_cap` or `refused bad_status`, correct the submission and retry once with the same claim. For `refused no_record`, `refused internal`, or a submit call that failed without a result, retry the identical submission once. Never repeat the engineering work to retry delivery. If receipt is still uncertain, preserve the work, report the uncertainty, and stop. Any other refusal closes this loop; on `refused recalled`, also report files already changed.

If the user directly says stop, stop immediately and report any work already changed. Do not fetch another turn. The application cannot guarantee cancellation of a command already running in the desktop app; report unfinished in-flight work accurately.

Activate only from the user's explicit `$middleware` invocation. Never initiate this loop from background, title, or summary work.
