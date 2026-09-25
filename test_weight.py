#!/usr/bin/env python3
"""
MVT-485Pro 称重模块 Modbus RTU 调试脚本
========================================

RS485 参数:
  - 波特率: 38400
  - 数据位: 8
  - 校验:   无
  - 停止位: 1
  - Modbus 地址: 0x01

寄存器映射:
  0x0000~0x0001: 实时重量 (32bit 补码, 高字在前)
  0x0002:        状态字
  0x0012:        小数点位数

用法:
  python3 test_weight.py                         # 默认 /dev/cu.usbserial-10
  python3 test_weight.py /dev/cu.usbserial-XX    # 指定串口
  python3 test_weight.py --loop                  # 持续读取 (2秒间隔)
  python3 test_weight.py --loop --interval 0.5   # 持续读取 (0.5秒间隔)
"""

import sys
import time
import struct
from typing import Optional, Dict
import serial

# ─── 配置 ───────────────────────────────────────────────────────
DEFAULT_PORT = "/dev/cu.usbserial-10"
BAUDRATE = 38400
TIMEOUT = 1.5          # 串口读超时 (秒)
MODBUS_ADDR = 0x01

# ─── Modbus CRC16 ──────────────────────────────────────────────
def modbus_crc16(data: bytes) -> int:
    """计算 Modbus RTU CRC16"""
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
    """构建 Modbus 功能码 03 (Read Holding Registers) 请求帧"""
    frame = struct.pack(">BBhH", addr, 0x03, start_reg, count)
    crc = modbus_crc16(frame)
    # CRC 低字节在前
    frame += struct.pack("<H", crc)
    return frame

# ─── 预构建的命令帧 ───────────────────────────────────────────
# 读 0x0000~0x0002 (重量高16位 + 低16位 + 状态字), 共3个寄存器
CMD_WEIGHT = build_read_request(MODBUS_ADDR, 0x0000, 3)
# 读 0x0012 (小数点位数), 1个寄存器
CMD_DECIMAL = build_read_request(MODBUS_ADDR, 0x0012, 1)

# 验证与 Lua 代码中硬编码的命令一致
assert CMD_WEIGHT.hex().upper() == "01030000000305CB", \
    f"CMD_WEIGHT mismatch: {CMD_WEIGHT.hex().upper()}"
assert CMD_DECIMAL.hex().upper() == "010300120001240F", \
    f"CMD_DECIMAL mismatch: {CMD_DECIMAL.hex().upper()}"


def open_serial(port: str) -> serial.Serial:
    """打开串口"""
    ser = serial.Serial(
        port=port,
        baudrate=BAUDRATE,
        bytesize=serial.EIGHTBITS,
        parity=serial.PARITY_NONE,
        stopbits=serial.STOPBITS_ONE,
        timeout=TIMEOUT,
    )
    # 清空缓冲区
    ser.reset_input_buffer()
    ser.reset_output_buffer()
    return ser


def send_and_receive(ser: serial.Serial, cmd: bytes, expect_len: int) -> Optional[bytes]:
    """
    发送命令并等待响应。
    expect_len: 期望的响应帧总长度 (含地址+功能码+字节数+数据+CRC)
    """
    ser.reset_input_buffer()
    ser.write(cmd)
    ser.flush()

    # 等待响应
    response = b""
    deadline = time.time() + TIMEOUT
    while len(response) < expect_len and time.time() < deadline:
        remaining = expect_len - len(response)
        chunk = ser.read(remaining)
        if chunk:
            response += chunk

    if len(response) < expect_len:
        return None

    # 校验 CRC
    received_crc = struct.unpack("<H", response[-2:])[0]
    calc_crc = modbus_crc16(response[:-2])
    if received_crc != calc_crc:
        print(f"  ⚠ CRC 校验失败: 收到 0x{received_crc:04X}, 计算 0x{calc_crc:04X}")
        return None

    return response


def read_decimal_places(ser: serial.Serial) -> Optional[int]:
    """读取小数点位数 (寄存器 0x0012)"""
    # 响应: 地址(1) + 功能码(1) + 字节数(1) + 数据(2) + CRC(2) = 7
    resp = send_and_receive(ser, CMD_DECIMAL, 7)
    if resp is None:
        return None

    value = struct.unpack(">H", resp[3:5])[0]
    if 0 <= value <= 4:
        return value
    return None


def read_weight(ser: serial.Serial, decimals: int = 1) -> Optional[Dict]:
    """
    读取重量和状态字 (寄存器 0x0000~0x0002)

    返回 dict:
      raw:      原始值 (32bit 补码整数)
      weight:   实际重量 (g)
      stable:   是否稳定
      overload: 是否超载
      zero:     是否零位
      negative: 是否负数
    """
    # 响应: 地址(1) + 功能码(1) + 字节数(1) + 数据(6) + CRC(2) = 11
    resp = send_and_receive(ser, CMD_WEIGHT, 11)
    if resp is None:
        return None

    # 解析3个寄存器 (各16bit, big-endian)
    high, low, status = struct.unpack(">HHH", resp[3:9])

    # 合成 32bit 补码
    raw = (high << 16) | low
    if raw >= 0x80000000:
        raw -= 0x100000000

    # 实际重量
    weight = raw / (10 ** decimals)

    # 状态字解析
    stable   = bool(status & 0x01)
    overload = bool(status & 0x02)
    zero     = bool(status & 0x04)
    negative = bool(status & 0x08)

    return {
        "raw": raw,
        "weight": weight,
        "stable": stable,
        "overload": overload,
        "zero": zero,
        "negative": negative,
        "status_hex": f"0x{status:04X}",
    }


def print_result(result: dict, decimals: int):
    """打印称重结果"""
    unit = "g"
    w = result["weight"]

    flags = []
    if result["stable"]:   flags.append("✅稳定")
    else:                  flags.append("⏳不稳定")
    if result["zero"]:     flags.append("⚖️零位")
    if result["negative"]: flags.append("➖负数")
    if result["overload"]: flags.append("🔴超载")

    print(f"  重量: {w:.{decimals}f} {unit}  (原始值: {result['raw']})")
    print(f"  状态: {' | '.join(flags)}  [状态字: {result['status_hex']}]")


def main():
    # 解析参数
    port = DEFAULT_PORT
    loop = False
    interval = 2.0

    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--loop":
            loop = True
        elif args[i] == "--interval" and i + 1 < len(args):
            i += 1
            interval = float(args[i])
        elif not args[i].startswith("--"):
            port = args[i]
        i += 1

    print("=" * 56)
    print("  MVT-485Pro 称重模块调试工具")
    print("=" * 56)
    print(f"  串口:   {port}")
    print(f"  波特率: {BAUDRATE}")
    print(f"  Modbus: 地址 0x{MODBUS_ADDR:02X}, 功能码 03")
    print()

    try:
        ser = open_serial(port)
    except serial.SerialException as e:
        print(f"❌ 打开串口失败: {e}")
        sys.exit(1)

    print(f"✅ 串口已打开: {ser.name}")
    print()

    # ─── 读取小数点位数 ───────────────────────────────────────
    print("📐 读取小数点位数 (寄存器 0x0012)...")
    dp = read_decimal_places(ser)
    if dp is not None:
        print(f"  小数点位数: {dp}")
    else:
        print("  ⚠ 读取失败, 使用默认值 1 (与 Lua 代码一致)")
        dp = 1
    print()

    # ─── 读取重量 ─────────────────────────────────────────────
    if loop:
        print(f"📊 持续读取模式 (间隔 {interval}s, Ctrl+C 退出)")
        print("-" * 56)
        count = 0
        try:
            while True:
                count += 1
                ts = time.strftime("%H:%M:%S")
                result = read_weight(ser, dp)
                if result:
                    w = result["weight"]
                    flags = "稳" if result["stable"] else "动"
                    if result["overload"]: flags = "超载"
                    if result["zero"]: flags += "+零"
                    print(f"  [{ts}] #{count:04d}  {w:>10.{dp}f} g  [{flags}]  raw={result['raw']}")
                else:
                    print(f"  [{ts}] #{count:04d}  ❌ 通讯超时")
                time.sleep(interval)
        except KeyboardInterrupt:
            print(f"\n\n🛑 停止. 共读取 {count} 次.")
    else:
        print("⚖️  读取重量 (寄存器 0x0000~0x0002)...")
        result = read_weight(ser, dp)
        if result:
            print_result(result, dp)
        else:
            print("  ❌ 读取失败 (通讯超时)")

        # 多读几次看看稳定性
        print()
        print("📊 连续读取 5 次 (间隔 1s):")
        print("-" * 56)
        for i in range(5):
            time.sleep(1)
            result = read_weight(ser, dp)
            ts = time.strftime("%H:%M:%S")
            if result:
                w = result["weight"]
                flags = "稳" if result["stable"] else "动"
                if result["overload"]: flags = "超载"
                print(f"  [{ts}] #{i+1}  {w:>10.{dp}f} g  [{flags}]  raw={result['raw']}")
            else:
                print(f"  [{ts}] #{i+1}  ❌ 通讯超时")

    print()
    ser.close()
    print("🔌 串口已关闭.")


if __name__ == "__main__":
    main()
