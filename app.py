import time
import struct
import threading
import serial
from flask import Flask, render_template, jsonify, request

app = Flask(__name__)

PORT = "/dev/cu.usbserial-10"
BAUDRATE = 38400
MODBUS_ADDR = 0x01

# 全局串口对象和锁，防止并发冲突
ser = None
serial_lock = threading.Lock()

def init_serial():
    global ser
    if ser is None or not ser.is_open:
        try:
            ser = serial.Serial(PORT, BAUDRATE, timeout=0.5)
        except Exception as e:
            print(f"串口打开失败: {e}")

def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            if crc & 1: crc = (crc >> 1) ^ 0xA001
            else: crc >>= 1
    return crc

def transact(cmd, exp_len):
    with serial_lock:
        if not ser or not ser.is_open: return None
        try:
            ser.reset_input_buffer()
            ser.write(cmd)
            ser.flush()
            resp = b""
            end = time.time() + 0.8
            while len(resp) < exp_len and time.time() < end:
                c = ser.read(exp_len - len(resp))
                if c: resp += c
            if len(resp) < exp_len: return None
            if struct.unpack("<H", resp[-2:])[0] != crc16(resp[:-2]): return None
            return resp
        except Exception as e:
            print(f"通信错误: {e}")
            return None

def read_regs(start, count):
    req = struct.pack(">BBhH", MODBUS_ADDR, 0x03, start, count)
    resp = transact(req + struct.pack("<H", crc16(req)), 5 + count*2)
    if not resp: return None
    return [struct.unpack(">H", resp[3+i*2:5+i*2])[0] for i in range(count)]

def write_reg(reg, val):
    req = struct.pack(">BbhH", MODBUS_ADDR, 0x06, reg, val)
    return transact(req + struct.pack("<H", crc16(req)), 8) is not None

def write_32bit(reg, val):
    h = (val >> 16) & 0xFFFF
    l = val & 0xFFFF
    req = struct.pack(">BBhHBHH", MODBUS_ADDR, 0x10, reg, 2, 4, h, l)
    return transact(req + struct.pack("<H", crc16(req)), 8) is not None

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/status")
def api_status():
    init_serial()
    dp_vals = read_regs(0x0012, 1)
    if not dp_vals: return jsonify({"error": "读取参数失败"}), 500
    dp = dp_vals[0]
    
    vals = read_regs(0x0000, 3)
    mv_vals = read_regs(0x0016, 2)
    
    if not vals: return jsonify({"error": "读取重量失败"}), 500
        
    raw = (vals[0] << 16) | vals[1]
    if raw >= 0x80000000: raw -= 0x100000000
    st = vals[2]
    
    mv = 0
    if mv_vals:
        mv_raw = (mv_vals[0] << 16) | mv_vals[1]
        if mv_raw >= 0x80000000: mv_raw -= 0x100000000
        mv = mv_raw / 1000000.0  # 转换为毫伏 (文档中说是微伏值)
    
    return jsonify({
        "weight": round(raw / (10**dp), dp),
        "raw": raw,
        "dp": dp,
        "mv": mv,
        "stable": bool(st & 1),
        "overload": bool(st & 2),
        "zero": bool(st & 4),
        "negative": bool(st & 8)
    })

@app.route("/api/params", methods=["GET", "POST"])
def api_params():
    if request.method == "POST":
        data = request.json
        # 写入参数
        if 'power_on_zero' in data: write_reg(0x0007, int(data['power_on_zero']))
        if 'zero_track' in data: write_reg(0x0008, int(data['zero_track']))
        if 'stable_range' in data: write_reg(0x0009, int(data['stable_range']))
        if 'zero_range' in data: write_reg(0x000A, int(data['zero_range']))
        if 'filter' in data: write_reg(0x000C, int(data['filter']))
        if 'ad_rate' in data: write_reg(0x000D, int(data['ad_rate']))
        if 'min_div' in data: write_reg(0x0013, int(data['min_div']))
        if 'dp' in data: write_reg(0x0012, int(data['dp']))
        return jsonify({"success": True})
        
    # 读取参数
    p1 = read_regs(0x0007, 7) # 7~13
    p2 = read_regs(0x0012, 4) # 18~21
    if not p1 or not p2: return jsonify({"error": "读取失败"}), 500
    
    max_range = (p2[2] << 16) | p2[3]
    return jsonify({
        "power_on_zero": p1[0],
        "zero_track": p1[1],
        "stable_range": p1[2],
        "zero_range": p1[3],
        "filter": p1[5],
        "ad_rate": p1[6],
        "dp": p2[0],
        "min_div": p2[1],
        "max_range_raw": max_range
    })

@app.route("/api/zero", methods=["POST"])
def api_zero():
    if write_reg(0x0006, 1): return jsonify({"success": True})
    return jsonify({"error": "置零失败"}), 500

@app.route("/api/cal_zero", methods=["POST"])
def api_cal_zero():
    if write_32bit(0x001E, 0): return jsonify({"success": True})
    return jsonify({"error": "零点标定失败"}), 500

@app.route("/api/calibrate", methods=["POST"])
def api_calibrate():
    data = request.json
    weight = float(data.get("weight", 0))
    point = int(data.get("point", 1)) # 1-4
    
    dp_vals = read_regs(0x0012, 1)
    if not dp_vals: return jsonify({"error": "读取参数失败"}), 500
    dp = dp_vals[0]
    
    val = int(weight * (10**dp))
    reg = 0x0020 + (point - 1) * 2 # Pt1=0x20, Pt2=0x22, Pt3=0x24, Pt4=0x26
    
    if write_32bit(reg, val): return jsonify({"success": True})
    return jsonify({"error": "标定写入失败"}), 500

if __name__ == "__main__":
    init_serial()
    app.run(host="0.0.0.0", port=5050, debug=False)
