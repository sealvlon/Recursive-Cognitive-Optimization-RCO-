# Version 0.1.0 validation

Prepared on Windows x64 with Python 3.14.3. This is a prototype release.

- **30 automated source tests passed:** 12 desktop transport/workflow checks, 12 connection setup checks, and 6 frozen fixture checks. Transport clients are scripted test clients. Setup tests use isolated temporary homes and do not modify installed app settings.
- The windowless Windows executable was built successfully. The ZIPs include a file-hash manifest, and the build metadata binds the executable to the source packaged with it.
- **Six offline checks passed from the extracted executable:** bundled runtime imports, bundled Tcl, required assets, frozen fixture integrity, the four-step static example, and candidate tamper rejection. This check opens no dashboard and connects no desktop agents. See the attached `VALIDATION.json` for the executable hash and result from the build host.
- An authenticated exchange between the installed Codex and Claude desktop apps was not retested for this packaged version. GitHub Actions has not yet run on a published repository.
- The example checks static configuration only. RTL simulation, synthesis, physical design, timing, and silicon behavior are untested by this release.

The Windows application is unsigned. A machine enforcing application-control policy may require a signed or policy-approved build from its owner or administrator. The source and automated build workflow are included for maintainers.

Local credentials, existing desktop configuration, prior task history, and build logs are excluded from the release archives. Third-party license notices are retained. A license for the original application code remains unselected.
