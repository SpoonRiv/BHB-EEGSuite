# BHB EEGSuite

基于 Windows 11 的 Web 上位机：EEG 采集控制、阻抗检测、离线录制与导出（CSV/EDF）、浏览器端实时可视化。

---

## 1. 快速开始

### 1.1 环境要求

- Windows 11
- Python 3.10.20（建议使用 conda）

### 1.2 安装依赖

```bash
conda env create -f environment.yml
conda activate BHB
```

### 1.3 启动

```bash
python main.py
```

启动后会自动打开浏览器，访问 `http://127.0.0.1:8001/`。

> 端口与监听地址由 `configs/config.yaml -> server.host/server.port` 控制；前端为静态资源，无需单独构建。

---

## 2. 使用说明

1. **设备页**：扫描并连接蓝牙设备，选择通道组合后点击「应用到系统」，会自动进入模式页。
2. **模式页**：选择 EEG / 阻抗 / tDCS 模式进入对应页面，点击「开始」运行、「停止」结束。
   - **EEG**：实时波形 + 离线录制，停止后可导出 CSV / EDF（可选带通滤波）。
   - **阻抗**：周期性刷新各通道阻抗与状态颜色。
   - **tDCS**：开启/停止刺激、下发控制指令、监测电流/电压/故障/电量。

---

## 3. 常用配置

- 页面顶栏版本号：`app.ui_version`
- 后端监听：`server.host` / `server.port`
- 蓝牙：`bluetooth.device_names` / `bluetooth.mac_address`
- EEG：`eeg.n_channels`（8/16 由配置选择）、`eeg.sampling_rate_hz`、`eeg.channel_names`
- 波形展示：`ui.waveform.*`（只影响展示，不影响采集）
- 离线数据：`offline.root_dir`、`offline.export.*`

### 3.1 EEG 帧协议（gold 分支）

8 通道 EEG 帧固定为 140 字节：`AA BB` 帧头、1 字节帧序号、120 字节 EEG、
1 字节 Trigger、10 字节 PPG、2 字节预留、2 字节电量、1 字节 SUM 和 `CC` 帧尾。
SUM 为除帧头、SUM 自身及帧尾之外所有字节累加后的低 8 位；因此帧序号也参与校验。
程序同时兼容不计入帧序号的旧版 SUM。实测 MSM008S00 PPG 固件的 SUM 还会漏加
绿光值的最低字节（从 0 开始的偏移 127），并将三路 PPG 高字节的校验项取为
`(value >> 12) & 0xFF`，而不是正确的 `(value >> 16) & 0xFF`。
空载数值小时这一差异不明显，手指接触后数值增大就会导致整帧校验失败、EEG/PPG 一起卡顿。
主配置中的
`eeg.protocol.ch8.frame.allow_ch8_ppg_checksum_quirk: true` 专门兼容这一缺陷，
仅对 CH8 的上述布局生效，仍校验帧头、帧尾和其余参与 SUM 的字节。
兼容算法仅影响校验，不修改 PPG 原始数值。该固件的帧序号、绿光最低字节及三路 PPG
最高 4 bit 不受其 SUM 保护，建议固件修正为标准 SUM 后关闭此选项。
PPG 的首字节 `0x00` 表示数据更新有效，`0x01` 表示未更新，后续三个 24-bit 字段
依次为绿光、红光、近红外原始值。时域页在 EEG 通道上方以蓝色底色显示这三路 PPG，
与 EEG 使用相同的时间窗口，纵轴按各路原始计数独立缩放。仅有效更新帧进入波形，
未更新帧不重复绘制；停止、断开或长时间无有效数据时清空显示。
PPG 通过 `/ws/ppg` 连续推送，不混入 EEG LSL 通道、不经过 EEG 滤波或单位换算，
也不写入现有 EEG 录制文件。`/api/status` 的 `device.ppg` 仍提供最近一帧诊断数据。

配置文件：主配置 `configs/config.yaml`（提交入库）；本机覆盖 `configs/config.local.yaml`（不提交）。

---

## 4. 版本号管理

每次提交前更新 `configs/config.yaml -> app.ui_version`（如 `1.0.1` → `1.0.2`），页面顶栏会自动同步。

---

## 5. 常见问题

### 5.1 端口 8001 被占用（最常见）

启动时报 `error while attempting to bind on address ('127.0.0.1', 8001)`，说明已有残留的后端进程占用了端口：

1. 查看占用端口的进程 PID：

   ```bash
   netstat -ano | findstr :8001
   ```

   输出中 `LISTENING` 行最后一列即为 PID（如 `36048`）。
2. 结束该进程：

   ```bash
   taskkill /F /PID <PID>
   ```
3. 重新运行 `python main.py`。

> 若不想结束进程，也可在 `configs/config.local.yaml` 中修改 `server.port` 换端口，再访问 `http://{host}:{新端口}/`。

### 5.2 无波形 / 扫描不到设备

1. 确认后端在跑：访问 `http://127.0.0.1:8001/api/status`。
2. 清理残留后端进程（同 5.1）。
3. 管理员 PowerShell 重置蓝牙服务：`Restart-Service bthserv`。
4. 仍无波形则给设备断电重启后重试；建议在 `config.local.yaml` 固定 `bluetooth.mac_address`。

### 5.3 页面打不开 / 持续转圈

- 先访问 `http://127.0.0.1:8001/api/status` 确认后端正常。
- 修改过端口则访问地址同步改为 `http://{host}:{port}/`。
- 首次启动可能被防火墙拦截：允许 Python/uvicorn 本地访问。

### 5.4 pylsl 报错 / 无数据

- 先排查蓝牙采集是否正常（调试事件是否有持续输出）。
- 重启后端，确保无残留进程。
- 首次运行若缺运行库（VC 运行时），重新安装依赖或改用 Python 3.10/3.11。

### 5.5 导出 CSV/EDF 失败

- 确认 `offline.root_dir` 目录可写、磁盘空间充足。
- EDF 失败先试 CSV 确认已录制，再排查 `pyedflib` 依赖。
