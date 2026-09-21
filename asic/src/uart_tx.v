/*
 * Copyright (c) 2026 sealvlon
 * SPDX-License-Identifier: Apache-2.0
 *
 * Minimal UART transmitter, 8N1, LSB first. One frame per `send` pulse taken
 * while idle; `tx` idles high. DIVISOR = clock cycles per bit.
 */

`default_nettype none

module uart_tx #(
    parameter DIVISOR = 434  // 50 MHz / 434 = 115207 baud (+0.006%)
) (
    input  wire       clk,
    input  wire       rst_n,
    input  wire [7:0] data,
    input  wire       send,  // start a frame (sampled only while idle)
    output wire       tx,
    output wire       busy
);

  reg [$clog2(DIVISOR)-1:0] baud_cnt;
  reg [3:0] bit_idx;  // 9 = start bit ... 0 = stop bit
  reg [9:0] shifter;  // {stop, data[7:0], start}, sent from bit 0
  reg active;

  assign busy = active;
  assign tx   = active ? shifter[0] : 1'b1;

  always @(posedge clk) begin
    if (!rst_n) begin
      active   <= 1'b0;
      baud_cnt <= 0;
      bit_idx  <= 0;
      shifter  <= 10'h3FF;
    end else if (!active) begin
      if (send) begin
        shifter  <= {1'b1, data, 1'b0};
        bit_idx  <= 4'd9;
        baud_cnt <= DIVISOR - 1;
        active   <= 1'b1;
      end
    end else if (baud_cnt != 0) begin
      baud_cnt <= baud_cnt - 1'b1;
    end else if (bit_idx == 0) begin
      active <= 1'b0;  // stop bit done; line stays high
    end else begin
      shifter  <= {1'b1, shifter[9:1]};
      bit_idx  <= bit_idx - 1'b1;
      baud_cnt <= DIVISOR - 1;
    end
  end

endmodule
