import logging
import math
import os
import struct
import threading
import time

import serial
from serial.tools import list_ports
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("scale")

PORT = os.getenv("SCALE_SERIAL_PORT", "/dev/cu.usbserial-10")
BAUDRATE = int(os.getenv("SCALE_BAUDRATE", "38400"))
MODBUS_ADDR = int(os.getenv("SCALE_MODBUS_ADDR", "1"), 0)
SERIAL_TIMEOUT = float(os.getenv("SCALE_SERIAL_TIMEOUT", "0.5"))


def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


class ModbusError(RuntimeError):
    pass


class ScaleDevice:
    def __init__(self, port=PORT, baudrate=BAUDRATE, address=MODBUS_ADDR):
        self.port = port
        self.baudrate = baudrate
        self.address = address
        self._serial = None
        self._lock = threading.Lock()

    def _open(self):
        if self._serial and self._serial.is_open:
            return
        self.close()
        self._serial = serial.Serial(
            self.port, self.baudrate, bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE, stopbits=serial.STOPBITS_ONE,
            timeout=SERIAL_TIMEOUT, write_timeout=SERIAL_TIMEOUT,
        )
        self._serial.reset_input_buffer()
        self._serial.reset_output_buffer()
        logger.info("串口已连接: %s @ %s", self.port, self.baudrate)

    def close(self):
        if self._serial:
            try:
                self._serial.close()
            except serial.SerialException:
                pass
        self._serial = None

    def configure(self, port, baudrate, address):
        with self._lock:
            self.close()
            self.port, self.baudrate, self.address = port, baudrate, address

    def _transact(self, pdu: bytes, expected_length: int) -> bytes:
        frame = bytes([self.address]) + pdu
        frame += struct.pack("<H", crc16(frame))
        last_error = None
        with self._lock:
            for attempt in range(2):
                try:
                    self._open()
                    self._serial.reset_input_buffer()
                    self._serial.write(frame)
                    self._serial.flush()
                    response = self._serial.read(expected_length)
                    if len(response) != expected_length:
                        raise ModbusError(f"响应不完整: {len(response)}/{expected_length} 字节")
                    if crc16(response[:-2]) != struct.unpack("<H", response[-2:])[0]:
                        raise ModbusError("响应 CRC 校验失败")
                    if response[0] != self.address:
                        raise ModbusError(f"响应地址错误: {response[0]}")
                    if response[1] == (pdu[0] | 0x80):
                        raise ModbusError(f"设备异常码: 0x{response[2]:02X}")
                    if response[1] != pdu[0]:
                        raise ModbusError(f"响应功能码错误: 0x{response[1]:02X}")
                    return response
                except (serial.SerialException, serial.SerialTimeoutException, OSError, ModbusError) as exc:
                    last_error = exc
                    self.close()
                    if attempt == 0:
                        time.sleep(0.05)
            raise ModbusError(str(last_error))

    def read_registers(self, start: int, count: int):
        pdu = struct.pack(">BHH", 0x03, start, count)
        response = self._transact(pdu, 5 + count * 2)
        if response[2] != count * 2:
            raise ModbusError(f"响应数据长度错误: {response[2]}")
        return [struct.unpack(">H", response[3 + i * 2:5 + i * 2])[0] for i in range(count)]

    def write_register(self, register: int, value: int):
        pdu = struct.pack(">BHH", 0x06, register, value)
        response = self._transact(pdu, 8)
        if response[1:6] != pdu:
            raise ModbusError("写单寄存器回显不一致")

    def write_u32(self, register: int, value: int):
        pdu = struct.pack(">BHHB", 0x10, register, 2, 4) + struct.pack(">I", value)
        response = self._transact(pdu, 8)
        if response[2:6] != struct.pack(">HH", register, 2):
            raise ModbusError("写多寄存器回显不一致")


device = ScaleDevice()


def signed_u32(high, low):
    value = (high << 16) | low
    return value - 0x100000000 if value & 0x80000000 else value


def read_status():
    values = device.read_registers(0x0000, 3)
    dp = device.read_registers(0x0012, 1)[0]
    if dp > 4:
        raise ModbusError(f"设备小数位参数异常: {dp}")
    raw = signed_u32(values[0], values[1])
    status = values[2]
    result = {
        "weight": raw / (10 ** dp), "raw": raw, "dp": dp,
        "stable": bool(status & 1), "overload": bool(status & 2),
        "zero": bool(status & 4), "negative": bool(status & 8),
    }
    try:
        mv = device.read_registers(0x0016, 2)
        result["mv"] = signed_u32(mv[0], mv[1]) / 1_000_000
    except ModbusError:
        result["mv"] = None
    return result


def api_error(message, status=502):
    return jsonify({"success": False, "error": message}), status


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/status")
def api_status():
    try:
        return jsonify(read_status())
    except ModbusError as exc:
        logger.warning("读取状态失败: %s", exc)
        return api_error("称重设备通信失败")


PARAMETERS = {
    "power_on_zero": (0x0007, {0, 1}),
    "zero_track": (0x0008, range(0, 10)),
    "stable_range": (0x0009, range(1, 100)),
    "zero_range": (0x000A, range(0, 100)),
    "filter": (0x000C, range(0, 10)),
    "ad_rate": (0x000D, {0, 1, 2}),
    "min_div": (0x0013, {1, 2, 5, 10, 20, 50}),
    "dp": (0x0012, range(0, 5)),
}


def read_params():
    p1 = device.read_registers(0x0007, 7)
    p2 = device.read_registers(0x0012, 4)
    result = {
        "power_on_zero": p1[0], "zero_track": p1[1],
        "stable_range": p1[2], "zero_range": p1[3],
        "filter": p1[5], "ad_rate": p1[6], "dp": p2[0],
        "min_div": p2[1], "max_range_raw": (p2[2] << 16) | p2[3],
    }
    result["max_range_g"] = result["max_range_raw"] / (10 ** result["dp"])
    return result


@app.route("/api/params", methods=["GET", "POST"])
def api_params():
    try:
        if request.method == "GET":
            return jsonify(read_params())
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or not data:
            return api_error("请求必须包含 JSON 参数", 400)
        unknown = set(data) - set(PARAMETERS) - {"max_range_g"}
        if unknown:
            return api_error(f"未知参数: {', '.join(sorted(unknown))}", 400)
        parsed = {}
        for name, value in data.items():
            if name == "max_range_g":
                try:
                    value = float(value)
                except (TypeError, ValueError):
                    return api_error("最大量程必须是数字", 400)
                if not math.isfinite(value) or value <= 0 or value > 3000:
                    return api_error("最大量程必须在 0–3000 g 之间", 400)
                parsed[name] = value
                continue
            try:
                parsed[name] = int(value)
            except (TypeError, ValueError):
                return api_error(f"参数 {name} 必须是整数", 400)
            if parsed[name] not in PARAMETERS[name][1]:
                return api_error(f"参数 {name} 超出允许范围", 400)
        current = read_params()
        changed = []
        for name, value in parsed.items():
            if name == "max_range_g":
                raw_range = round(value * (10 ** current["dp"]))
                if raw_range != current["max_range_raw"]:
                    device.write_u32(0x0014, raw_range)
                    verify = device.read_registers(0x0014, 2)
                    if ((verify[0] << 16) | verify[1]) != raw_range:
                        raise ModbusError("最大量程写入后校验失败")
                    changed.append(name)
                continue
            if current[name] == value:
                continue
            register = PARAMETERS[name][0]
            device.write_register(register, value)
            actual = device.read_registers(register, 1)[0]
            if actual != value:
                raise ModbusError(f"参数 {name} 写入后校验失败")
            changed.append(name)
        return jsonify({"success": True, "changed": changed, "params": read_params()})
    except ModbusError as exc:
        logger.warning("参数操作失败: %s", exc)
        return api_error("参数操作失败，设备未确认写入")


@app.post("/api/zero")
def api_zero():
    try:
        if read_status()["overload"]:
            return api_error("设备处于超载状态，禁止去皮", 409)
        device.write_register(0x0006, 1)
        return jsonify({"success": True})
    except ModbusError as exc:
        logger.warning("去皮失败: %s", exc)
        return api_error("去皮失败，设备未确认命令")


@app.post("/api/cal_zero")
def api_cal_zero():
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or data.get("confirm") is not True:
        return api_error("零点标定需要明确确认", 400)
    try:
        status = read_status()
        if status["overload"]:
            return api_error("当前处于超载状态，不能进行零点标定", 409)
        if not status["stable"]:
            return api_error("读数尚未稳定，请等待空载稳定后再标定", 409)
        # 厂家协议：十进制地址 22（0x0016），写入任意非零 32 位值。
        device.write_u32(0x0016, 1)
        return jsonify({"success": True})
    except ModbusError as exc:
        logger.warning("零点标定失败: %s", exc)
        return api_error("零点标定失败，设备未确认写入")


@app.post("/api/calibrate")
def api_calibrate():
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or data.get("confirm") is not True:
        return api_error("标定需要明确确认", 400)
    try:
        weight = float(data.get("weight"))
        point = int(data.get("point", 1))
    except (TypeError, ValueError):
        return api_error("标定重量或标定点无效", 400)
    if not math.isfinite(weight) or weight <= 0 or point not in range(1, 5):
        return api_error("标定重量必须为正数，标定点必须为 1-4", 400)
    try:
        status = read_status()
        if status["overload"]:
            max_range = read_params()["max_range_g"]
            return api_error(f"设备已超载：当前最大量程为 {max_range:g} g，请先调整量程", 409)
        if not status["stable"]:
            return api_error("读数尚未稳定，请等待稳定标志后再标定", 409)
        value = round(weight * (10 ** status["dp"]))
        if value > 0xFFFFFFFF:
            return api_error("标定重量超出设备数值范围", 400)
        # 厂家协议地址为十进制 30/32/34/36，即 0x001E/0x0020/0x0022/0x0024。
        device.write_u32(0x001E + (point - 1) * 2, value)
        return jsonify({"success": True, "point": point, "weight": weight})
    except ModbusError as exc:
        logger.warning("标定失败: %s", exc)
        return api_error("标定失败，设备未确认写入")


@app.get("/health")
def health():
    return jsonify({"status": "ok", "serial_port": PORT})


@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    if request.method == "GET":
        ports = sorted({p.device for p in list_ports.comports()} | {device.port})
        return jsonify({"port": device.port, "baudrate": device.baudrate, "address": device.address, "ports": ports})
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return api_error("请求必须包含 JSON 参数", 400)
    port = str(data.get("port", device.port))
    try:
        baudrate = int(data.get("baudrate", device.baudrate))
        address = int(data.get("address", device.address), 0) if isinstance(data.get("address", device.address), str) else int(data.get("address", device.address))
    except (TypeError, ValueError):
        return api_error("端口、波特率或地址无效", 400)
    if baudrate not in {9600, 19200, 38400, 57600, 115200} or not 1 <= address <= 247:
        return api_error("波特率或 Modbus 地址超出允许范围", 400)
    known_ports = {p.device for p in list_ports.comports()}
    if port != device.port and port not in known_ports:
        return api_error("指定串口当前不可用", 400)
    device.configure(port, baudrate, address)
    return jsonify({"success": True, "port": port, "baudrate": baudrate, "address": address})


if __name__ == "__main__":
    app.run(host=os.getenv("SCALE_HOST", "127.0.0.1"), port=int(os.getenv("SCALE_PORT", "5050")), debug=False)
