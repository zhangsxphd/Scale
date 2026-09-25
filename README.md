# MVT-485Pro 智能称重系统

本项目通过 Modbus RTU 读取 MVT-485Pro 称重变送器，并提供仅限本机访问的 Flask Web 控制台。支持实时重量、状态曲线、日常去皮、增益标定及受控参数配置。

## 安全边界

- 默认仅监听 `127.0.0.1:5050`，避免局域网内其他设备直接调用写寄存器接口。
- 标定前必须由界面明确确认；后端还会检查读数稳定和超载状态。
- 参数值会先校验范围，写入后回读确认；设备未确认时接口不会报告成功。
- “硬件绝对零点”功能已移除。现有资料对 `0x001E` 的含义存在冲突，在取得准确版本的厂家协议前不应写入该寄存器。
- 日常去皮会写寄存器 `0x0006 = 1`，操作前应确认当前负载确实需要作为零点。

## 运行

要求 Python 3.9 或更高版本：

```bash
python3 -m pip install -r requirements.txt
python3 app.py
```

也可双击 macOS 的 `start.command`。浏览器访问 <http://127.0.0.1:5050>。

默认串口为 `/dev/cu.usbserial-10`。可通过环境变量覆盖：

```bash
SCALE_SERIAL_PORT=/dev/cu.usbserial-XX SCALE_BAUDRATE=38400 python3 app.py
```

其他变量：`SCALE_MODBUS_ADDR`、`SCALE_SERIAL_TIMEOUT`、`SCALE_HOST`、`SCALE_PORT`、`LOG_LEVEL`。只有在可信网络和具备访问控制时，才应将 `SCALE_HOST` 改为 `0.0.0.0`。

## 设备协议

- 串口：`38400 8-N-1`，默认站号 `0x01`
- 实时重量：`0x0000–0x0001`，32 位有符号整数，高字在前
- 状态字：`0x0002`，bit0 稳定、bit1 超载、bit2 零位、bit3 负数
- 日常去皮：向 `0x0006` 写入 `1`
- 小数位：`0x0012`
- 最小分度值：`0x0013`
- 最大量程：`0x0014–0x0015`
- 传感器信号：`0x0016–0x0017`
- 增益标定点：`0x0020/0x0022/0x0024/0x0026` 起始的两个寄存器

写寄存器前应核对设备型号和协议版本。项目不会自动扫描或试写未知寄存器。

## API

- `GET /api/status`：实时重量和状态
- `GET /api/params`：读取设备参数
- `POST /api/params`：校验、写入并回读参数
- `POST /api/zero`：日常去皮
- `POST /api/calibrate`：增益标定，要求 `confirm: true`
- `GET /health`：服务存活检查，不访问串口

## 测试

```bash
python3 -m unittest -v
```

自动化测试不会连接或写入真实称重设备。
