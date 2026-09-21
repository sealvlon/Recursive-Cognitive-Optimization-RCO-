# SPDX-FileCopyrightText: © 2026 sealvlon
# SPDX-License-Identifier: Apache-2.0

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles

DIVISOR = 434  # clock cycles per bit, matches uart_tx DIVISOR in project.v

TX = 1 << 4  # uo_out[4]
TX_MIRROR = 1 << 0  # uo_out[0]
BUSY = 1 << 5  # uo_out[5]


def uo(dut) -> int:
    return int(dut.uo_out.value)


async def rx_frame(dut) -> int:
    """Wait for a start bit, sample 8 data bits LSB-first and the stop bit at
    bit centers, return the byte."""
    for _ in range(20 * DIVISOR):
        if not uo(dut) & TX:
            break
        await ClockCycles(dut.clk, 1)
    assert not uo(dut) & TX, "no start bit seen"
    # Move to the middle of the start bit, then step one bit at a time.
    await ClockCycles(dut.clk, DIVISOR // 2)
    assert not uo(dut) & TX, "start bit did not hold"
    byte = 0
    for i in range(8):
        await ClockCycles(dut.clk, DIVISOR)
        byte |= (1 if uo(dut) & TX else 0) << i
    await ClockCycles(dut.clk, DIVISOR)
    assert uo(dut) & TX, "stop bit missing"
    return byte


@cocotb.test()
async def test_uart_tx(dut):
    clock = Clock(dut.clk, 20, unit="ns")  # 50 MHz
    cocotb.start_soon(clock.start())

    # Reset with the transmitter disabled.
    dut.ena.value = 1
    dut.ui_in.value = 0
    dut.uio_in.value = 0
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 10)
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 5)

    # Idle: line high, not busy, mirror matches.
    assert uo(dut) & TX, "TX must idle high"
    assert not uo(dut) & BUSY, "must not be busy while disabled"
    assert bool(uo(dut) & TX_MIRROR) == bool(uo(dut) & TX)

    # Send 0xA5, then change the input and read the next frame.
    dut.ui_in.value = 0xA5
    dut.uio_in.value = 1
    assert await rx_frame(dut) == 0xA5
    dut.ui_in.value = 0x52  # "R"
    assert await rx_frame(dut) == 0x52

    # Release send; after the frame in flight, the line stays idle.
    dut.uio_in.value = 0
    await ClockCycles(dut.clk, 12 * DIVISOR)
    assert uo(dut) & TX, "TX must return to idle"
    assert not uo(dut) & BUSY, "busy must clear"
    assert bool(uo(dut) & TX_MIRROR) == bool(uo(dut) & TX)
