/*
 * Copyright (c) 2026 sealvlon
 * SPDX-License-Identifier: Apache-2.0
 *
 * RCO protocol emulator - Jane Street ASIC competition entry.
 * Current RTL: hello-world stage ("start by getting a UART transmitter out of
 * a pin"). While uio_in[0] is high, the byte on ui_in is sent 8N1 at 115200
 * baud (50 MHz clock), frames back to back. TX on uo_out[4] (TT recommended
 * UART pinout), mirrored on uo_out[0]; busy on uo_out[5].
 */

`default_nettype none

module tt_um_sealvlon_rco_pe (
    input  wire [7:0] ui_in,    // Dedicated inputs
    output wire [7:0] uo_out,   // Dedicated outputs
    input  wire [7:0] uio_in,   // IOs: Input path
    output wire [7:0] uio_out,  // IOs: Output path
    output wire [7:0] uio_oe,   // IOs: Enable path (active high: 0=input, 1=output)
    input  wire       ena,      // always 1 when the design is powered, so you can ignore it
    input  wire       clk,      // clock
    input  wire       rst_n     // reset_n - low to reset
);

  wire tx, busy;

  uart_tx #(
      .DIVISOR(434)  // 50 MHz -> 115207 baud
  ) tx0 (
      .clk  (clk),
      .rst_n(rst_n),
      .data (ui_in),
      .send (uio_in[0] & ~busy),
      .tx   (tx),
      .busy (busy)
  );

  assign uo_out  = {2'b00, busy, tx, 3'b000, tx};
  assign uio_out = 0;
  assign uio_oe  = 0;

  // List all unused inputs to prevent warnings
  wire _unused = &{ena, uio_in[7:1], 1'b0};

endmodule
