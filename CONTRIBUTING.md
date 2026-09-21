# Contributing

Start with [development and builds](docs/DEVELOPMENT.md). Describe the behavior being changed and keep each contribution focused. A change to orchestration should include a meaningful test for its success or failure case; document changes to user-visible connection behavior.

Preserve the desktop-first experience: users open the executable, connect their existing desktop sessions and work through the dashboard. Keep provider identity separate from workflow roles. Do not add a silent CLI inference or API fallback.

Respect the declared profile boundaries, frozen evaluator and isolated candidates. Do not equate static configuration checks, scripted transport tests or browser checks with live desktop inference or hardware verification.

Run the automated checks before proposing changes. For a build or setup change, also test an extracted Windows package in a fresh folder. Describe exactly what was tested and any remaining limitations in the pull request.

Never submit local credentials, generated client configuration, run history, dashboard tokens, private model responses or a populated working installation. The repository has no first-party license decision yet; agree on contribution and licensing terms with the maintainer before adding work intended for redistribution.
