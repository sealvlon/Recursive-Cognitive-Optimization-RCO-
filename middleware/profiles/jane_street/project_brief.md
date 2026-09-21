# Bundled ASIC configuration example

This example demonstrates a bounded desktop-agent review loop. It copies the
bundled `asic/` fixture into an isolated candidate, changes one configuration
field after the first reviewer approval, and checks the result against frozen
file hashes. The source fixture remains unchanged.

The task is to change `info.yaml` -> `project.tiles` from `8x4` to `6x4`.
Everything else must remain byte-for-byte identical, including historical
comments, RTL, tests, documentation, and the file inventory. The preserved
historical comments still mention `8x4`; they are deliberately outside this
example's one-field scope.

The desktop agents propose and review in text. Python owns the candidate copy,
the permitted edit, and the evaluator. A reviewer must return an explicit JSON
approval with no objections before the candidate is edited or retained.

## Included files

- `asic/`: a frozen 23-file Tiny Tapeout CMOS5L example with an existing UART
  transmitter and cocotb test. This is a fixture, not a finished programmable
  protocol emulator.
- `middleware/evidence/asic/baseline_manifest.json`: the complete fixture file
  inventory and hashes, plus the frozen evaluator and requirements hashes.
- `middleware/profiles/jane_street/requirements.json`: a dated requirements
  snapshot checked on September 18, 2026 local time (September 19 UTC).
- `middleware/profiles/jane_street/verify_template_candidate.py`: the independent
  static evaluator.

Generated candidates and run history are local runtime output and are excluded
from the published source package. The release contains no prior agent
conversation or completed-run evidence.

## Dated competition context

The included requirements snapshot cites the
[Jane Street announcement](https://blog.janestreet.com/protocol-emulator-asic-competition/),
the [Tiny Tapeout CMOS5L template](https://github.com/TinyTapeout/ttihp-verilog-template/tree/cmos5l),
and the CMOS5L support table. The snapshot records a `6x4` allocation and IHP
130 nm CMOS5L. Treat it as the fixed input for this example, not a promise that
competition requirements will remain unchanged. Review current official rules
before starting a real competition submission.

## What passing means

A pass means the candidate has the expected files, all protected bytes are
unchanged, and the tile field is exactly the permitted `6x4` value. An altered
RTL file, an extra file, or an unrelated configuration edit must fail.

This release does not establish RTL simulation, formal verification, synthesis,
timing, area fit, place and route, DRC/LVS, FPGA behavior, or silicon behavior.
Those checks are unrun by this static example. It also does not submit or
publish a competition entry.

## Fixture attribution

The `asic/` template and retained source notices carry Apache-2.0 licensing;
see `asic/LICENSE`. The existing UART implementation retains its author and
copyright attribution. No author names or top-module identifiers were changed
while packaging, so all original frozen fixture hashes are preserved. The
fixture's nested `.github/workflows/` files are preserved sample files and are
not repository-root release workflows.
