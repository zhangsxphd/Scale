# MVT-485Pro 智能称重系统 (Smart Scale System)

本项目为基于 MVT-485Pro 称重传感器的全栈控制系统，提供了一个极具现代感和科幻风格的 Web 数据看板，支持实时曲线绘制、软去皮、硬件绝对零点标定以及增益标定功能。本项目也为后续移植到 ESP32-C3 提供了完整的底层 Modbus 通讯参考。

## 📁 文件结构

- `app.py`: Flask 后端服务器。负责通过串口 (pySerial) 与硬件传感器进行 Modbus RTU 通讯，并暴露出 RESTful API。
- `templates/index.html`: 前端 Web UI 控制台。采用 Tailwind CSS 构建的暗黑科幻风界面，并使用 Chart.js 渲染智能自适应的实时重量曲线。
- `mvt485_manager.py`: 纯命令行的硬件参数管理工具，适合脱离 Web 界面进行底层调试。
- `esp_mvt485_node.lua`: 适用于 NodeMCU/ESP 系列的 Lua 脚本备用逻辑（支持云端下发 `CMD:ZERO` 归零指令）。
- `start.command`: Mac 专用的一键启动执行文件，双击即可运行服务器。

## 🚀 如何运行

1. 确保已安装 Python 3 和依赖：
   ```bash
   pip install flask pyserial
   ```
2. **Mac 用户一键启动**：
   直接双击文件夹中的 `start.command` 运行后台。
   *(如果是第一次运行，遇到权限问题，请在终端执行 `chmod +x start.command`)*
3. 浏览器访问面板：[http://127.0.0.1:5050](http://127.0.0.1:5050)

---

## 💻 供 AI (Codex/Copilot) 提取的技术说明

### 1. 硬件通讯规范 (Modbus RTU)
- **串口参数**: 默认波特率 `38400`, `8-N-1`
- **默认地址**: `0x01`
- **读取重量 (0x03 寄存器 0x0000)**: 读 2 个字（32位有符号整数，补码）。
- **常用写入指令**:
  - **日常去皮**: 向 `0x0006` 写入 `1` (16位)。
  - **绝对零点标定 (解决空载漂移)**: 向 `0x001E` 写入 `0` (32位)，必须在秤台空载时执行。
  - **砝码单点标定**: 向 `0x0020` 写入实际放上去的砝码重量数值 (32位)。

### 2. 后端 REST API (`app.py`)
- `GET /api/status`: 
  - 返回 JSON: `{ "weight": 25.5, "stable": true, "zero": false, "dp": 1 }`
- `GET /api/params`: 读取硬件底层滤波、采样率等参数。
- `POST /api/params`: 写入底层参数。
- `POST /api/zero`: 执行软件去皮。
- `POST /api/cal_zero`: 执行硬件绝对零点标定。
- `POST /api/calibrate`: 执行重量增益标定，请求体 `{ "weight": 500, "point": 1 }`。

### 3. ESP32-C3 移植建议
当后续需要将此系统移植到 ESP32-C3 时，只需实现与 `app.py` 中相同的 Hex Payload 构建逻辑，并利用 ESP32 的 `Serial1` 连接 MAX485 模块与传感器通讯。前端界面可直接通过 WiFi 托管在 ESP32 的 WebServer 或 LittleFS 中。
