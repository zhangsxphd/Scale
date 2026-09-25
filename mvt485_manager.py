#!/usr/bin/env python3
"""
MVT-485Pro 全功能管理与标定工具 (Mac 适用版)
替代官方 Windows 版上位机，提供命令行交互式菜单。
"""

import sys
import time
import struct
import serial

PORT = "/dev/cu.usbserial-10"
BAUDRATE = 38400
TIMEOUT = 1.0
MODBUS_ADDR = 0x01

# --- Modbus 基础函数 ---
def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc

def transact(ser, cmd, exp_len):
    ser.reset_input_buffer()
    ser.write(cmd)
    ser.flush()
    resp = b""
    end = time.time() + TIMEOUT
    while len(resp) < exp_len and time.time() < end:
        c = ser.read(exp_len - len(resp))
        if c: resp += c
    if len(resp) < exp_len: return None
    if struct.unpack("<H", resp[-2:])[0] != crc16(resp[:-2]): return None
    return resp

def read_regs(ser, start, count):
    req = struct.pack(">BBhH", MODBUS_ADDR, 0x03, start, count)
    resp = transact(ser, req + struct.pack("<H", crc16(req)), 5 + count*2)
    if not resp: return None
    return [struct.unpack(">H", resp[3+i*2:5+i*2])[0] for i in range(count)]

def write_reg(ser, reg, val):
    req = struct.pack(">BbhH", MODBUS_ADDR, 0x06, reg, val)
    return transact(ser, req + struct.pack("<H", crc16(req)), 8) is not None

def write_regs(ser, start, vals):
    cnt = len(vals)
    req = struct.pack(">BBhHB", MODBUS_ADDR, 0x10, start, cnt, cnt*2)
    for v in vals: req += struct.pack(">H", v)
    return transact(ser, req + struct.pack("<H", crc16(req)), 8) is not None

def write_32bit(ser, reg, val):
    # 32位无符号，高字在前
    h = (val >> 16) & 0xFFFF
    l = val & 0xFFFF
    return write_regs(ser, reg, [h, l])

# --- 业务逻辑 ---
def get_sys_info(ser):
    dp_vals = read_regs(ser, 0x0012, 1)
    if not dp_vals: return None
    dp = dp_vals[0]
    
    # 读取常用参数
    # 0x08=零点跟踪, 0x09=判稳, 0x0C=稳态滤波, 0x0D=AD速率
    params = read_regs(ser, 0x0008, 6) 
    
    return {
        "dp": dp,
        "zero_track": params[0] if params else -1,
        "stable_range": params[1] if params else -1,
        "filter": params[4] if params else -1,
        "ad_rate": params[5] if params else -1
    }

def get_weight(ser, dp):
    vals = read_regs(ser, 0x0000, 3)
    if not vals: return None
    raw = (vals[0] << 16) | vals[1]
    if raw >= 0x80000000: raw -= 0x100000000
    st = vals[2]
    return {
        "val": raw / (10**dp),
        "raw": raw,
        "stable": bool(st & 1),
        "overload": bool(st & 2),
        "zero": bool(st & 4),
        "status": st
    }

# --- 界面交互 ---
def print_header(title):
    print(f"\n{'='*50}\n  {title}\n{'='*50}")

def monitor_mode(ser):
    info = get_sys_info(ser)
    if not info:
        print("通信失败！")
        return
    dp = info['dp']
    print_header("实时监控 (按 Ctrl+C 退出)")
    try:
        while True:
            w = get_weight(ser, dp)
            if w:
                s = "稳" if w['stable'] else "动"
                z = " 零" if w['zero'] else ""
                o = " [超载!]" if w['overload'] else ""
                print(f" \r  重量: {w['val']:>8.{dp}f} g  [{s}{z}]{o}  (内码: {w['raw']:>6})   ", end="")
            else:
                print(" \r  通信异常...                                   ", end="")
            time.sleep(0.2)
    except KeyboardInterrupt:
        print("\n  退出监控。")

def zero_scale(ser):
    print("\n[一键置零] 确保秤台无杂物...")
    if write_reg(ser, 0x0006, 1):
        print("✅ 置零命令已发送。")
    else:
        print("❌ 置零失败，请检查通信。")
    time.sleep(1)

def config_menu(ser):
    while True:
        info = get_sys_info(ser)
        if not info:
            print("通信失败！")
            return
        print_header("参数配置")
        print(f" 1. 小数点位数    : {info['dp']}")
        print(f" 2. 零点跟踪范围  : {info['zero_track']} d  (0=关, 1-9d)")
        print(f" 3. 稳态滤波级数  : {info['filter']}    (0-9，越大越稳但越慢)")
        print(f" 4. 判稳范围      : {info['stable_range']} d")
        print(" 0. 返回主菜单")
        
        c = input("\n请选择要修改的项: ").strip()
        if c == '0': break
        elif c == '1':
            v = int(input("输入小数点位数(0-4): "))
            if 0<=v<=4: write_reg(ser, 0x0012, v)
        elif c == '2':
            v = int(input("输入零点跟踪范围(0-9): "))
            if 0<=v<=9: write_reg(ser, 0x0008, v)
        elif c == '3':
            v = int(input("输入滤波级数(0-9): "))
            if 0<=v<=9: write_reg(ser, 0x000C, v)
        elif c == '4':
            v = int(input("输入判稳范围(1-9): "))
            if 1<=v<=9: write_reg(ser, 0x0009, v)

def multi_point_cal(ser):
    print_header("多点标定")
    print("本功能支持最多4点线性标定。")
    print("⚠️  请严格按提示操作！\n")
    
    info = get_sys_info(ser)
    if not info: return
    dp = info['dp']
    
    # 0. 零点
    input("👉 [准备] 请清空秤台，按 Enter 开始标定零点...")
    if write_reg(ser, 0x0006, 1):
        print("✅ 零点已重置。")
    else:
        print("❌ 通信失败。")
        return
    time.sleep(2)
    
    # 增益点寄存器: Pt1=0x0020, Pt2=0x0022, Pt3=0x0024, Pt4=0x0026
    pt_regs = [0x0020, 0x0022, 0x0024, 0x0026]
    
    for i, reg in enumerate(pt_regs):
        ans = input(f"\n👉 是否进行第 {i+1} 点标定？(y/n): ").strip().lower()
        if ans != 'y':
            break
            
        weight_str = input(f"请输入即将放上的砝码重量(克): ").strip()
        try:
            w_float = float(weight_str)
            w_val = int(w_float * (10**dp))
        except ValueError:
            print("输入无效！标定终止。")
            break
            
        input(f"👉 请放上 {weight_str}g 砝码，等读数稳定后，按 Enter 确认...")
        print("⏳ 正在写入标定数据...")
        if write_32bit(ser, reg, w_val):
            print(f"✅ 第 {i+1} 点标定成功！")
        else:
            print("❌ 写入失败，标定终止。")
            break
        time.sleep(1)
        
    print("\n🎉 标定流程结束！")

def main():
    try:
        ser = serial.Serial(PORT, BAUDRATE, timeout=TIMEOUT)
    except Exception as e:
        print(f"无法打开串口 {PORT}: {e}")
        return

    while True:
        print_header("MVT-485Pro 管理器")
        print(" 1. 实时监控")
        print(" 2. 一键置零 (空载清零)")
        print(" 3. 执行标定 (单点/多点)")
        print(" 4. 参数配置 (滤波/零点跟踪等)")
        print(" 0. 退出")
        
        choice = input("\n👉 请选择操作 (0-4): ").strip()
        
        if choice == '1': monitor_mode(ser)
        elif choice == '2': zero_scale(ser)
        elif choice == '3': multi_point_cal(ser)
        elif choice == '4': config_menu(ser)
        elif choice == '0': break
        else: print("无效选择。")
        
    ser.close()
    print("👋 拜拜！")

if __name__ == "__main__":
    main()
