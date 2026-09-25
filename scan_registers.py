#!/usr/bin/env python3
"""
MVT-485Pro 寄存器扫描 + 诊断工具
扫描所有可能的 Modbus 寄存器，找出校准相关参数
"""

import sys
import time
import struct
from typing import Optional
import serial

PORT = "/dev/cu.usbserial-10"
BAUDRATE = 38400
TIMEOUT = 0.5
MODBUS_ADDR = 0x01


def modbus_crc16(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 0x0001:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc


def build_read_request(addr: int, start_reg: int, count: int) -> bytes:
    frame = struct.pack(">BBhH", addr, 0x03, start_reg, count)
    crc = modbus_crc16(frame)
    frame += struct.pack("<H", crc)
    return frame


def send_and_receive(ser, cmd, expect_len):
    ser.reset_input_buffer()
    ser.write(cmd)
    ser.flush()

    response = b""
    deadline = time.time() + TIMEOUT
    while len(response) < expect_len and time.time() < deadline:
        chunk = ser.read(expect_len - len(response))
        if chunk:
            response += chunk

    if len(response) < expect_len:
        return None

    received_crc = struct.unpack("<H", response[-2:])[0]
    calc_crc = modbus_crc16(response[:-2])
    if received_crc != calc_crc:
        return None

    return response


def read_registers(ser, start_reg, count):
    """读取指定寄存器，返回寄存器值列表"""
    cmd = build_read_request(MODBUS_ADDR, start_reg, count)
    # 响应长度: 1(addr) + 1(func) + 1(byte_count) + count*2(data) + 2(crc)
    expect_len = 5 + count * 2
    resp = send_and_receive(ser, cmd, expect_len)
    if resp is None:
        return None

    values = []
    for i in range(count):
        val = struct.unpack(">H", resp[3 + i*2 : 5 + i*2])[0]
        values.append(val)
    return values


def main():
    ser = serial.Serial(
        port=PORT, baudrate=BAUDRATE,
        bytesize=serial.EIGHTBITS, parity=serial.PARITY_NONE,
        stopbits=serial.STOPBITS_ONE, timeout=TIMEOUT
    )
    ser.reset_input_buffer()

    print("=" * 60)
    print("  MVT-485Pro 寄存器扫描诊断")
    print("=" * 60)
    print()

    # ─── 逐个/逐段读取已知和可能的寄存器 ────────────────────
    # 已知寄存器
    known_regs = {
        0x0000: "重量高16位",
        0x0001: "重量低16位",
        0x0002: "状态字",
        0x0003: "?",
        0x0004: "?",
        0x0005: "?",
        0x0006: "?",
        0x0007: "?",
        0x0008: "?",
        0x0009: "?",
        0x000A: "?",
        0x000B: "?",
        0x000C: "?",
        0x000D: "?",
        0x000E: "?",
        0x000F: "?",
        0x0010: "?",
        0x0011: "?",
        0x0012: "小数点位数",
        0x0013: "?",
        0x0014: "?",
        0x0015: "?",
        0x0016: "?",
        0x0017: "?",
        0x0018: "?",
        0x0019: "?",
        0x001A: "?",
        0x001B: "?",
        0x001C: "?",
        0x001D: "?",
        0x001E: "?",
        0x001F: "?",
    }

    # 扫描 0x0000 ~ 0x001F (每次读1个，避免越界)
    print("📋 扫描寄存器 0x0000 ~ 0x001F:")
    print("-" * 60)
    for reg in range(0x0000, 0x0020):
        vals = read_registers(ser, reg, 1)
        label = known_regs.get(reg, "?")
        if vals is not None:
            v = vals[0]
            # 也显示有符号解释
            signed_v = v if v < 0x8000 else v - 0x10000
            print(f"  0x{reg:04X}  {label:12s}  = {v:5d} (0x{v:04X})  signed={signed_v}")
        else:
            print(f"  0x{reg:04X}  {label:12s}  = [无响应]")
        time.sleep(0.05)

    # 扫描更高的地址段 (校准参数可能在这里)
    print()
    print("📋 扫描寄存器 0x0020 ~ 0x003F:")
    print("-" * 60)
    for reg in range(0x0020, 0x0040):
        vals = read_registers(ser, reg, 1)
        if vals is not None:
            v = vals[0]
            signed_v = v if v < 0x8000 else v - 0x10000
            print(f"  0x{reg:04X}  = {v:5d} (0x{v:04X})  signed={signed_v}")
        time.sleep(0.05)

    # 扫描 0x0040 ~ 0x005F
    print()
    print("📋 扫描寄存器 0x0040 ~ 0x005F:")
    print("-" * 60)
    for reg in range(0x0040, 0x0060):
        vals = read_registers(ser, reg, 1)
        if vals is not None:
            v = vals[0]
            signed_v = v if v < 0x8000 else v - 0x10000
            print(f"  0x{reg:04X}  = {v:5d} (0x{v:04X})  signed={signed_v}")
        time.sleep(0.05)

    # 扫描更高地址 0x0100 ~ 0x011F (有些模块校准参数在高地址)
    print()
    print("📋 扫描寄存器 0x0100 ~ 0x011F:")
    print("-" * 60)
    for reg in range(0x0100, 0x0120):
        vals = read_registers(ser, reg, 1)
        if vals is not None:
            v = vals[0]
            signed_v = v if v < 0x8000 else v - 0x10000
            print(f"  0x{reg:04X}  = {v:5d} (0x{v:04X})  signed={signed_v}")
        time.sleep(0.05)

    print()

    # ─── 重新读一次重量做对比 ─────────────────────────────────
    print("⚖️  当前称重读数 (100g 标准砝码):")
    print("-" * 60)
    vals = read_registers(ser, 0x0000, 3)
    if vals:
        high, low, status = vals
        raw = (high << 16) | low
        if raw >= 0x80000000:
            raw -= 0x100000000

        dp_vals = read_registers(ser, 0x0012, 1)
        dp = dp_vals[0] if dp_vals else 1
        weight = raw / (10 ** dp)

        print(f"  高16位:     0x{high:04X} ({high})")
        print(f"  低16位:     0x{low:04X} ({low})")
        print(f"  原始32位:   {raw}")
        print(f"  小数点位数: {dp}")
        print(f"  换算重量:   {weight} g")
        print(f"  状态字:     0x{status:04X}")
        print(f"  期望重量:   100.0 g")
        print(f"  误差:       {weight - 100.0:.1f} g ({(weight/100.0*100):.1f}%)")
    else:
        print("  ❌ 读取失败")

    ser.close()
    print()
    print("🔌 串口已关闭.")


if __name__ == "__main__":
    main()
