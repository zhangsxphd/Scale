#!/usr/bin/env python3
"""
MVT-485Pro 校准脚本 (基于 MVT-485协议手册v0.002)
=================================================

正确的寄存器映射:
  读取:
    0x0000~0x0001: 实时重量 (32bit补码, 高字在前)
    0x0002:        状态字 (bit0=稳定,bit1=超载,bit2=零位,bit3=负数)
    0x0016~0x0017: 微伏值

  写入:
    0x0006:        置零 (写入非零值执行置零)
    0x0012:        小数点位置
    0x0013:        最小分度值
    0x0014~0x0015: 最大量程 (32bit, 高字在前)
    0x001E~0x001F: 有砝码零点标定 (32bit, 写入砝码重量)
    0x0020~0x0021: 有砝码增益标定第1点 (32bit, 写入砝码重量)

校准步骤:
  1. 空载 → 写 0x0006 = 非零值 → 置零
  2. 放砝码 → 写 0x0020~0x0021 = 砝码重量 → 增益标定
"""

import sys
import time
import struct
from typing import Optional, Dict
import serial

PORT = "/dev/cu.usbserial-10"
BAUDRATE = 38400
TIMEOUT = 1.5
MODBUS_ADDR = 0x01


def modbus_crc16(data):
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 0x0001:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc


def build_read(addr, start, count):
    frame = struct.pack(">BBhH", addr, 0x03, start, count)
    frame += struct.pack("<H", modbus_crc16(frame))
    return frame


def build_write_single(addr, reg, value):
    frame = struct.pack(">BbhH", addr, 0x06, reg, value)
    frame += struct.pack("<H", modbus_crc16(frame))
    return frame


def build_write_multi(addr, start, values):
    count = len(values)
    frame = struct.pack(">BBhHB", addr, 0x10, start, count, count * 2)
    for v in values:
        frame += struct.pack(">H", v)
    frame += struct.pack("<H", modbus_crc16(frame))
    return frame


def transact(ser, cmd, expect_len):
    ser.reset_input_buffer()
    ser.write(cmd)
    ser.flush()
    resp = b""
    deadline = time.time() + TIMEOUT
    while len(resp) < expect_len and time.time() < deadline:
        chunk = ser.read(expect_len - len(resp))
        if chunk:
            resp += chunk
    if len(resp) < expect_len:
        return None
    if struct.unpack("<H", resp[-2:])[0] != modbus_crc16(resp[:-2]):
        return None
    return resp


def read_regs(ser, start, count):
    resp = transact(ser, build_read(MODBUS_ADDR, start, count), 5 + count * 2)
    if not resp:
        return None
    return [struct.unpack(">H", resp[3+i*2:5+i*2])[0] for i in range(count)]


def write_single(ser, reg, value):
    return transact(ser, build_write_single(MODBUS_ADDR, reg, value), 8) is not None


def write_multi(ser, start, values):
    return transact(ser, build_write_multi(MODBUS_ADDR, start, values), 8) is not None


def read_weight(ser):
    vals = read_regs(ser, 0x0000, 3)
    if not vals:
        return None
    high, low, status = vals
    raw = (high << 16) | low
    if raw >= 0x80000000:
        raw -= 0x100000000
    dp = 1
    dp_vals = read_regs(ser, 0x0012, 1)
    if dp_vals and 0 <= dp_vals[0] <= 4:
        dp = dp_vals[0]
    weight = raw / (10 ** dp)
    stable = bool(status & 0x01)
    return {"raw": raw, "weight": weight, "dp": dp, "status": status, "stable": stable}


def show(ser, label=""):
    r = read_weight(ser)
    if r:
        s = "稳" if r["stable"] else "动"
        print(f"  ⚖️  {label}: {r['weight']:.{r['dp']}f} g  [{s}]  (raw={r['raw']}, status=0x{r['status']:04X})")
    else:
        print(f"  ⚖️  {label}: ❌ 读取失败")
    return r


def wait_stable(ser, label="", timeout=10):
    """等待读数稳定"""
    print(f"  ⏳ 等待读数稳定...")
    deadline = time.time() + timeout
    last_raw = None
    stable_count = 0
    while time.time() < deadline:
        r = read_weight(ser)
        if r and r["stable"]:
            if last_raw is not None and abs(r["raw"] - last_raw) <= 1:
                stable_count += 1
                if stable_count >= 3:
                    show(ser, label)
                    return r
            else:
                stable_count = 0
            last_raw = r["raw"]
        time.sleep(0.5)
    return show(ser, label + " (超时)")


def main():
    cal_weight = 500  # 默认砝码重量 (g)

    for a in sys.argv[1:]:
        if a.startswith("--weight="):
            cal_weight = int(a.split("=")[1])

    print("=" * 60)
    print("  MVT-485Pro 校准工具 v2")
    print("  (基于 MVT-485协议手册v0.002)")
    print("=" * 60)
    print(f"  串口:     {PORT}")
    print(f"  砝码重量: {cal_weight} g")
    print()

    ser = serial.Serial(
        port=PORT, baudrate=BAUDRATE,
        bytesize=serial.EIGHTBITS, parity=serial.PARITY_NONE,
        stopbits=serial.STOPBITS_ONE, timeout=TIMEOUT
    )
    ser.reset_input_buffer()
    print("✅ 串口已打开")

    # 读取基本参数
    dp_vals = read_regs(ser, 0x0012, 2)
    dp = dp_vals[0] if dp_vals else 1
    min_div = dp_vals[1] if dp_vals else 1
    range_vals = read_regs(ser, 0x0014, 2)
    max_range_raw = (range_vals[0] << 16 | range_vals[1]) if range_vals else 0
    max_range = max_range_raw / (10 ** dp) if max_range_raw else 0

    print(f"\n  📋 模块参数:")
    print(f"     小数点位数:   {dp}")
    print(f"     最小分度值:   {min_div}")
    print(f"     最大量程:     {max_range} g (raw={max_range_raw})")

    # 砝码值换算 (按小数点位数)
    cal_value = int(cal_weight * (10 ** dp))
    cal_high = (cal_value >> 16) & 0xFFFF
    cal_low = cal_value & 0xFFFF
    print(f"     砝码写入值:   {cal_value} (高=0x{cal_high:04X}, 低=0x{cal_low:04X})")

    show(ser, "当前读数")

    # ═══════════════════════════════════════════════════════════
    # 第一步: 置零
    # ═══════════════════════════════════════════════════════════
    print()
    print("━" * 60)
    print("  📌 第一步: 置零 (空载)")
    print("━" * 60)
    print("  请确认秤台上没有任何东西。")
    input("  👉 按 Enter 开始置零...")
    print()

    wait_stable(ser, "置零前")

    # 写入 0x0006 = 1 (非零值即执行置零)
    print("\n  🔧 发送置零命令 (0x0006 = 1)...")
    ok = write_single(ser, 0x0006, 1)
    if ok:
        print("  ✅ 置零命令发送成功")
    else:
        print("  ❌ 置零命令失败!")
        ser.close()
        return

    time.sleep(3)
    r = wait_stable(ser, "置零后")

    if r and abs(r["weight"]) < 1.0:
        print("  ✅ 置零成功!")
    else:
        print("  ⚠ 置零后读数不为零，但继续进行标定...")

    # ═══════════════════════════════════════════════════════════
    # 第二步: 增益标定
    # ═══════════════════════════════════════════════════════════
    print()
    print("━" * 60)
    print(f"  📌 第二步: 增益标定 (放上 {cal_weight}g 砝码)")
    print("━" * 60)
    input(f"  👉 请放上 {cal_weight}g 标准砝码，然后按 Enter...")
    print()

    print("  ⏳ 等待 5 秒让读数稳定...")
    time.sleep(5)
    wait_stable(ser, "标定前")

    # 写入砝码重量到 0x0020~0x0021 (有砝码增益标定第一点)
    # 按协议: 32bit, 高字在前
    print(f"\n  🔧 写入砝码值 {cal_value} 到 0x0020~0x0021 (增益标定第1点)...")
    ok = write_multi(ser, 0x0020, [cal_high, cal_low])
    if ok:
        print("  ✅ 砝码值写入成功, 增益标定完成!")
    else:
        print("  ⚠ FC16写入失败, 尝试逐个写入...")
        ok1 = write_single(ser, 0x0020, cal_high)
        ok2 = write_single(ser, 0x0021, cal_low)
        print(f"     0x0020={ok1}, 0x0021={ok2}")

    print("  ⏳ 等待 5 秒...")
    time.sleep(5)

    # ═══════════════════════════════════════════════════════════
    # 验证
    # ═══════════════════════════════════════════════════════════
    print()
    print("━" * 60)
    print("  📊 校准结果验证")
    print("━" * 60)

    r = wait_stable(ser, f"校准后 (应为 {cal_weight}.0 g)")

    if r:
        err = abs(r["weight"] - cal_weight)
        pct = err / cal_weight * 100
        print(f"\n  误差: {err:.{r['dp']}f} g ({pct:.2f}%)")
        if pct < 1.0:
            print("  🎉 校准优秀! 误差 < 1%")
        elif pct < 5.0:
            print("  ✅ 校准良好, 误差 < 5%")
        else:
            print("  ⚠ 校准偏差较大, 建议检查传感器")

    print(f"\n  📊 连续验证 10 次 (1秒间隔):")
    print("  " + "-" * 50)
    for i in range(10):
        time.sleep(1)
        r = read_weight(ser)
        if r:
            ts = time.strftime("%H:%M:%S")
            s = "稳" if r["stable"] else "动"
            print(f"    [{ts}] #{i+1:02d}  {r['weight']:>10.{r['dp']}f} g  [{s}]  raw={r['raw']}")
        else:
            print(f"    #{i+1:02d}  ❌ 读取失败")

    # 取下砝码验证零点
    print()
    input("  👉 请取下砝码验证零点，按 Enter...")
    time.sleep(3)

    r = wait_stable(ser, "零点验证 (应为 0.0 g)")

    print()
    ser.close()
    print("🔌 串口已关闭.")
    print("✅ 校准完成!")


if __name__ == "__main__":
    main()
