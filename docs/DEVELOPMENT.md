# Development and builds

RCO Desktop is a Windows Python application. The repository includes the RCO hub source in `rco/` and its skill adapter in `rco_skills/`; it does not need a separate RCO checkout. Desktop activation skills are under `middleware/desktop_skills/`.

Use Windows x64 and Python 3.14 for the tested build target. The following commands are for contributors; end users open the Windows release.

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.venv\Scripts\python.exe -m unittest discover -s tests -v
.venv\Scripts\python.exe -m unittest middleware.desktop.test_desktop middleware.tests.test_desktop_setup middleware.desktop.test_setup_wizard
```

Run the source launcher with `.venv\Scripts\python.exe -m middleware.desktop.launcher`. Like the executable, it opens the visual setup and dashboard. Do this only when you intend to configure your desktop connections; the test suites use temporary homes and scripted clients.

## Build the distributable packages

```powershell
.venv\Scripts\python.exe scripts/build_release.py --version 0.1.0
```

The default version comes from `VERSION`. The build script runs validation and creates a windowless executable with PyInstaller, then writes:

- `dist/rco-desktop-0.1.0-source.zip`
- `dist/rco-desktop-0.1.0-windows-x64.zip`
- `dist/SHA256SUMS.txt`
- `dist/VALIDATION.json` with the extracted executable check result, including any host-policy block.

Keep the package's required data files beside the executable. A one-file executable contains the Python runtime, but the project profile and example baseline are external package assets. Building does not sign the Windows executable.

The **Checks** GitHub Actions workflow runs the automated checks on pushes and pull requests. **Build Windows packages** is a manually started workflow that produces downloadable workflow artifacts. It does not publish a GitHub Release or upload credentials. The workflow versions follow the official [checkout](https://github.com/actions/checkout), [setup-python](https://github.com/actions/setup-python) and [upload-artifact](https://github.com/actions/upload-artifact) documentation.

## Repository layout

| Location | Responsibility |
|---|---|
| `middleware/desktop/launcher.py` | Windowless launch and single-instance behavior. |
| `middleware/desktop/server.py` | Dashboard, desktop listeners and bounded workflow control. |
| `middleware/desktop/profile.py` | The included four-step ASIC profile. |
| `middleware/desktop/project.json` | Profile selection. |
| `middleware/rco_bridge/` | Local RCO import and desktop setup helpers. |
| `middleware/profiles/jane_street/` | Dated requirements and protected static evaluator. |
| `middleware/evidence/asic/baseline_manifest.json` | Frozen example-file and evaluator hashes. |
| `asic/` | Example template and RTL baseline, with original license notices. |
| `rco/`, `rco_skills/`, `registry.toml` | RCO hub, adapter and desktop client declarations. |
| `scripts/build_release.py` | Tested release packaging entry point. |

## Extend a profile

The coordinator loads the class declared by `middleware/desktop/project.json`. A profile declares ordered roles, steps and operation costs; prepares an isolated candidate; supplies the brief and evidence; and verifies approved results. Roles are separate from provider identity.

Keep acceptance criteria and evaluator inputs fixed during a run. Validate a new profile against explicit allowed edits and meaningful failure cases before enabling it. Changing the task's prose must not grant extra file permissions or change the evaluator. The shipped profile intentionally permits one configuration-field correction only.

## Validation boundaries

`middleware/desktop/test_desktop.py` exercises the running local MCP server using scripted clients. It covers registration versus listening, approvals, automatic progress, limits, pause/resume, stop and late results, duplicate handling, rejected candidates and interrupted recovery. Skill setup tests and `middleware/desktop/test_setup_wizard.py` use temporary configuration homes.

These checks do not run authenticated desktop model inference or hardware tools. Before describing a release as tested end to end, separately exercise both installed desktop apps through registration, a completed run, checkpoint approval and stop. Report the app versions and result without publishing tokens or private conversation history.

The ASIC evaluator hashes bytes. `.gitattributes` disables line-ending conversion for the frozen example and evaluator. Do not regenerate hashes merely to make a failing change pass; review a deliberate baseline update and its scope first.

## Publish from GitHub's interface

[Publishing with clicks](PUBLISHING.md) covers uploading the source and attaching a Windows release. Upload the prepared distribution, not your running app folder, which may contain local configuration or task history.
