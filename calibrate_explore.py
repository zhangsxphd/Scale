#!/usr/bin/env python3
"""
MVT-485Pro 校准探索脚本
========================
先全面探测哪些寄存器可写，找到正确的命令寄存器地址，然后执行校准。

已知信息:
  - Lua 代码版寄存器: 0x0000~0x0001=重量, 0x0002=状态, 0x0012=小数点
  - 说明书版寄存器: 0x000B=命令, 0x000C~0x000D=砝码值
  - 之前写 0x000B=5 (校零) 没有生效
  - 寄存器 0x0006=1 可能就是"内码值"的一部分

策略: 利用说明书的提示，但偏移可能不同。
  说明书里 0x0008=小数点 → 实际 0x0012=小数点，偏移了 +0x000A
  那么 0x000B(命令) → 可能是 0x0015 或其它地址
  0x000C~0x000D(砝码值) → 可能是 0x0016~0x0017 或其它

也可能映射完全不同。让我们直接读取 PDF 无法读取的手册(MVT-485协议手册v0.002)
的信息 - 通过尝试提取文本。
"""

import sys
import time
import struct
import subprocess
from typing import Optional, Dict, List
import serial

PORT = "/dev/cu.usbserial-10"
BAUDRATE = 38400
TIMEOUT = 1.0
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


def build_read_request(addr, start_reg, count):
    frame = struct.pack(">BBhH", addr, 0x03, start_reg, count)
    crc = modbus_crc16(frame)
    frame += struct.pack("<H", crc)
    return frame


def build_write_single(addr, reg, value):
    frame = struct.pack(">BbhH", addr, 0x06, reg, value)
    crc = modbus_crc16(frame)
    frame += struct.pack("<H", crc)
    return frame


def build_write_multiple(addr, start_reg, values):
    count = len(values)
    byte_count = count * 2
    frame = struct.pack(">BBhHB", addr, 0x10, start_reg, count, byte_count)
    for v in values:
        frame += struct.pack(">H", v)
    crc = modbus_crc16(frame)
    frame += struct.pack("<H", crc)
    return frame


def send_recv(ser, cmd, expect_len):
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


def read_regs(ser, start, count):
    cmd = build_read_request(MODBUS_ADDR, start, count)
    resp = send_recv(ser, cmd, 5 + count * 2)
    if not resp:
        return None
    vals = []
    for i in range(count):
        vals.append(struct.unpack(">H", resp[3+i*2:5+i*2])[0])
    return vals


def write_single(ser, reg, value):
    cmd = build_write_single(MODBUS_ADDR, reg, value)
    resp = send_recv(ser, cmd, 8)
    return resp is not None


def write_multi(ser, start, values):
    cmd = build_write_multiple(MODBUS_ADDR, start, values)
    resp = send_recv(ser, cmd, 8)
    return resp is not None


def read_weight(ser):
    """读取重量 (Lua 寄存器表)"""
    vals = read_regs(ser, 0x0000, 3)
    if not vals:
        return None
    high, low, status = vals
    raw = (high << 16) | low
    if raw >= 0x80000000:
        raw -= 0x100000000
    dp_vals = read_regs(ser, 0x0012, 1)
    dp = dp_vals[0] if dp_vals and 0 <= dp_vals[0] <= 4 else 1
    weight = raw / (10 ** dp)
    return {"raw": raw, "weight": weight, "dp": dp, "status": status}


def show_weight(ser, label=""):
    r = read_weight(ser)
    if r:
        print(f"  ⚖️  {label}: {r['weight']:.{r['dp']}f} g  (raw={r['raw']}, dp={r['dp']}, status=0x{r['status']:04X})")
    else:
        print(f"  ⚖️  {label}: ❌ 读取失败")
    return r


def main():
    ser = serial.Serial(
        port=PORT, baudrate=BAUDRATE,
        bytesize=serial.EIGHTBITS, parity=serial.PARITY_NONE,
        stopbits=serial.STOPBITS_ONE, timeout=TIMEOUT
    )
    ser.reset_input_buffer()

    print("=" * 60)
    print("  MVT-485Pro 校准探索工具")
    print("=" * 60)

    # 首先尝试用 pdftotext 提取 PDF 内容
    print("\n📖 尝试提取 MVT-485 协议手册...")
    try:
        result = subprocess.run(
            ["pdftotext", "-layout",
             "/Users/zhangshuxuan/本地文稿/工程化育秧/称重计说明书/协议说明书/MVT-485协议手册v0.002.pdf",
             "-"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0 and result.stdout.strip():
            print("  ✅ 成功提取! 查找校准相关内容...")
            text = result.stdout
            # 查找包含 "校" "标定" "calibrat" 等关键词的行
            lines = text.split('\n')
            for i, line in enumerate(lines):
                if any(kw in line.lower() for kw in ['校', '标定', 'calibr', '砝码', '命令', '0x000b',
                                                       '清零', '置零', 'command', 'zero']):
                    # 打印这行及上下文
                    start = max(0, i-1)
                    end = min(len(lines), i+2)
                    for j in range(start, end):
                        print(f"    [{j+1}] {lines[j]}")
                    print()
        else:
            print("  ⚠ pdftotext 未输出内容")
    except FileNotFoundError:
        print("  ⚠ pdftotext 未安装，跳过")
    except Exception as e:
        print(f"  ⚠ 提取失败: {e}")

    # 用 python 尝试 pdfminer
    try:
        result = subprocess.run(
            ["python3", "-c", """
import sys
try:
    from pdfminer.high_level import extract_text
    text = extract_text(sys.argv[1])
    print(text[:15000])
except ImportError:
    print("NO_PDFMINER")
except Exception as e:
    print(f"ERROR: {e}")
""",
             "/Users/zhangshuxuan/本地文稿/工程化育秧/称重计说明书/协议说明书/MVT-485协议手册v0.002.pdf"],
            capture_output=True, text=True, timeout=15
        )
        if "NO_PDFMINER" not in result.stdout and result.stdout.strip():
            text = result.stdout
            if len(text) > 100:
                print("\n📖 PDF 内容 (pdfminer):")
                lines = text.split('\n')
                for i, line in enumerate(lines):
                    if line.strip():
                        print(f"  {line}")
    except Exception:
        pass

    # ─── 读取当前所有有效寄存器 ───────────────────────────────
    print("\n" + "=" * 60)
    print("  📋 当前寄存器状态")
    print("=" * 60)

    show_weight(ser, "当前重量 (空载)")

    # 已知寄存器含义 (根据 singleVariable_Modbus.xml)
    reg_names = {
        0x0000: "总重(高16位)",
        0x0001: "总重(低16位)",
        0x0002: "状态字(Lua)", 
        0x0003: "?",
        0x0004: "?",
        0x0005: "?",
        0x0006: "?/上电清零开关(xml:addr7)",
        0x0007: "?/零点跟踪(xml:addr8)",
        0x0008: "小数点(说明书)/判稳范围(xml:addr9)",
        0x0009: "单位(说明书)/清零范围(xml:addr10)",
        0x000A: "状态(说明书)/数字滤波(xml:addr11)",
        0x000B: "命令(说明书)/稳态滤波(xml:addr12)",
        0x000C: "砝码值低(说明书)/AD采样率(xml:addr13)",
        0x000D: "砝码值高(说明书)",
        0x000E: "蠕变追踪(说明书)",
        0x000F: "显示归零(说明书)",
        0x0010: "动态追踪范围",
        0x0011: "动态追踪更新时间",
        0x0012: "小数点(Lua)/稳定重量开关(说明书addr18)",
        0x0013: "最小分度值(xml:addr19)/置零范围(说明书addr19)",
        0x0014: "最大量程低(xml:addr20)/上电置零使能",
        0x0015: "最大量程高(xml:addr21)/上电置零时间",
    }

    print("\n  有效寄存器 0x0000 ~ 0x0025:")
    for reg in range(0x0000, 0x0026):
        vals = read_regs(ser, reg, 1)
        name = reg_names.get(reg, "?")
        if vals is not None:
            v = vals[0]
            print(f"    0x{reg:04X}  {name:40s} = {v:6d} (0x{v:04X})")
        time.sleep(0.03)

    # ─── 测试写入可写性 ──────────────────────────────────────
    print("\n" + "=" * 60)
    print("  🔍 探测可写寄存器 (写回原值测试)")
    print("=" * 60)

    # 安全地测试：读出值 → 写回原值 → 看是否成功
    writable = []
    for reg in range(0x0000, 0x0026):
        vals = read_regs(ser, reg, 1)
        if vals is None:
            continue
        orig = vals[0]
        # 尝试写回原值
        ok = write_single(ser, reg, orig)
        status = "✅可写" if ok else "🔒只读"
        if ok:
            writable.append(reg)
        name = reg_names.get(reg, "?")
        print(f"    0x{reg:04X}  {status}  (值={orig})")
        time.sleep(0.05)

    print(f"\n  可写寄存器: {['0x{:04X}'.format(r) for r in writable]}")

    # ─── 根据说明书的寄存器对应关系，推断正确的命令地址 ────────
    # 说明书里: 小数点=0x0008(addr8), 实际小数点在 0x0012(addr18)
    # 偏移 = 0x0012 - 0x0008 = 0x000A = 10
    # 但也可能不是简单偏移，让我们检查另一种映射:
    # singleVariable_Modbus.xml 用的 Address 字段是 PLC 地址(从0开始)
    # xml: WEIGHT addr=0, 上电清零 addr=7, 零点跟踪 addr=8, 判稳范围 addr=9
    #      清零范围 addr=10, 数字滤波 addr=11, 稳态滤波 addr=12, AD采样率 addr=13
    #      小数点 addr=18, 最小分度值 addr=19, 最大量程 addr=20(Double=2 regs)
    # WEIGHT 占 Float(2 regs) 所以 addr 0~1
    # 状态 addr=2 (unsigned, 1 reg) -- 这正好是 Lua 的 0x0002

    # 对比说明书寄存器表:
    # 说明书: 0x0000~0x0001=总重, 0x0002~0x0003=净重, 0x0004~0x0005=峰值
    #         0x0006~0x0007=内码值, 0x0008=小数点, 0x0009=单位
    #         0x000A=状态, 0x000B=命令, 0x000C~0x000D=砝码值
    # 
    # Lua/xml: 0x0000~0x0001=总重, 0x0002=状态
    #          0x0012=小数点
    # 
    # 很可能 Lua 代码的模块精简了寄存器，去掉了净重/峰值/内码，
    # 但保留了功能寄存器在不同的偏移位置。

    # 让我们直接在可写寄存器中尝试命令操作
    print("\n" + "=" * 60)
    print("  🔧 校准操作")
    print("=" * 60)

    print("\n  当前秤台应该是空载状态。")
    show_weight(ser, "校零前")

    # 策略1: 说明书地址 0x000B 写5 (已失败)
    # 策略2: 尝试所有可写寄存器写入5
    # 但这太危险了，先试几个最可能的地址

    # 看看说明书中 0x000B 的位置：
    # 如果 xml addr 是从 0 开始编号（与 Modbus 寄存器地址一致），
    # 而 xml 没有列出命令寄存器，那可能是 Lua/xml 固件确实用 0x000B

    # 让我尝试用功能码 16 写入命令（可能只支持 0x10 写入）
    print("\n  尝试多种方式发送校零命令 (命令=5):")

    # 方法1: FC06 写 0x000B
    print("    方法1: FC06 → 0x000B = 5 ...", end=" ")
    ok = write_single(ser, 0x000B, 5)
    print("✅" if ok else "❌")
    time.sleep(1)
    show_weight(ser, "方法1后")

    # 方法2: FC16 写 0x000B
    print("    方法2: FC16 → 0x000B = 5 ...", end=" ")
    ok = write_multi(ser, 0x000B, [5])
    print("✅" if ok else "❌")
    time.sleep(1)
    show_weight(ser, "方法2后")

    # 方法3: 也许命令是 "4" (置零不保存) 而不是 "5"
    print("    方法3: FC06 → 0x000B = 4 (置零不保存) ...", end=" ")
    ok = write_single(ser, 0x000B, 4)
    print("✅" if ok else "❌")
    time.sleep(1)
    show_weight(ser, "方法3后")

    # 方法4: 用 FC06 在地址 0x0003 写 (有些模块精简后命令在状态后面)
    print("    方法4: FC06 → 0x0003 = 5 ...", end=" ")
    ok = write_single(ser, 0x0003, 5)
    print("✅" if ok else "❌")
    time.sleep(1)
    show_weight(ser, "方法4后")

    # 方法5: 也许命令直接写 0x0002 (状态/命令复用)
    print("    方法5: FC06 → 0x0002 = 5 ...", end=" ")
    ok = write_single(ser, 0x0002, 5)
    print("✅" if ok else "❌")
    time.sleep(1)
    show_weight(ser, "方法5后")

    print("\n" + "=" * 60)
    print("  完成探索。请查看哪种方法使读数归零。")
    print("=" * 60)

    ser.close()
    print("\n🔌 串口已关闭.")


if __name__ == "__main__":
    main()
