<!---

This file is used to generate your project datasheet. Please fill in the information below and delete any unused
sections.

You can also include images in this folder and reference them in the markdown. Each image must be less than
512 kb in size, and the combined size of all images must be less than 1 MB.
-->

## How it works

Entry for the Jane Street protocol emulator ASIC competition: a programmable
protocol-emulation chip on IHP 130 nm CMOS5L, 8x4 Tiny Tapeout tiles.

Current RTL is the hello-world stage ("start by getting a UART transmitter out
of a pin"): while the send enable is high, the byte on `ui_in[7:0]` is
transmitted 8N1, LSB first, at 115200 baud from a 50 MHz clock (divisor 434),
frames back to back. The line idles high. The programmable engine that
replaces this fixed block is being designed; this stage exists to validate the
full RTL-to-GDS flow first.

## How to test

Run the cocotb testbench in `test/` (`make -B`). It checks idle level, framing,
two back-to-back frames with a data change in between, and return to idle.

On the demo board: drive a byte on `ui_in[7:0]`, set `uio[0]` high, and watch
115200-8N1 frames on `uo[4]` (mirrored on `uo[0]`) with a UART adapter or
logic analyzer. `uo[5]` is high while a frame is in flight.

## External hardware

None for the current stage. A 3.3 V USB-UART adapter or logic analyzer on
`uo[4]` is convenient for watching the output.
