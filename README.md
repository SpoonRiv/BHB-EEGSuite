# BHB EEGSuite

基于 Windows 11 的 Web 上位机：EEG 采集控制、阻抗检测、离线录制与导出（CSV/EDF）、浏览器端实时可视化。

***

## 1. 快速开始

普通用户请到 GitHub Releases 页面下载 `BHB-EEGSuite-base.zip`，解压到可写目录，双击 `BHB-EEGSuite.exe`。基础包已包含 Python 和运行依赖，无需安装开发环境。首次启动后浏览器会打开 `http://127.0.0.1:8001/`。音乐模块独立放在同一 Release 的 `BHB-EEGSuite-music-V1.zip` 中，可在「模块管理」里按需下载和安装。

### 1.1 运行要求

- Windows 11
- 将 ZIP 完整解压后运行，不要直接在压缩包中双击 exe
- 目录需可写，以便保存本机配置、模块和实验数据

### 1.2 从源码开发

```bash
conda env create -f environment.yml
conda activate BHB
```

### 1.3 从源码启动

```bash
python main.py
```

开发启动后也会自动打开浏览器。仓库页面的「Code → Download ZIP」包含扩展源码，不是面向普通用户的基础包。

> 端口与监听地址由 `configs/config.yaml -> server.host/server.port` 控制；前端为静态资源，无需单独构建。

***

## 2. 使用说明

1. **设备页**：扫描并连接蓝牙设备，选择通道组合后点击「应用到系统」，会自动进入模式页。
2. **模式页**：选择 EEG / 阻抗 / tDCS 模式进入对应页面，点击「开始」运行、「停止」结束。
   - **EEG**：实时波形 + 离线录制，停止后可导出 CSV / EDF（可选带通滤波）。
   - **阻抗**：周期性刷新各通道阻抗与状态颜色。
   - **tDCS**：开启/停止刺激、下发控制指令、监测电流/电压/故障/电量。

文本-音频多模态范式为可选模块，默认不安装，模式页最右侧显示灰色入口。在右上角「模块管理」中从远程模块源下载安装或卸载，卸载时保留实验数据。发布者必须先上传远程索引和安装包；配置与发布方式见 [可选范式模块](docs/optional-modules.md)。

***

## 3. 常用配置

- 页面顶栏版本号：`app.ui_version`
- 后端监听：`server.host` / `server.port`
- 蓝牙：`bluetooth.device_names` / `bluetooth.mac_address`
- EEG：`eeg.n_channels`（8/16 由配置选择）、`eeg.sampling_rate_hz`、`eeg.channel_names`
- 波形展示：`ui.waveform.*`（只影响展示，不影响采集）
- 离线数据：`offline.root_dir`、`offline.export.*`

配置文件：主配置 `configs/config.yaml`（提交入库）；本机覆盖 `configs/config.local.yaml`（不提交）。

***

## 4. 版本号管理

每次提交前更新 `configs/config.yaml -> app.ui_version`（如 `1.0.1` → `1.0.2`），页面顶栏会自动同步。

***

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

3. 重新运行 `BHB-EEGSuite.exe`（源码开发环境用 `python main.py`）。

> 若不想结束进程，也可在 `configs/config.local.yaml` 中修改 `server.port` 换端口，再访问 `http://{host}:{新端口}/`。

### 5.2 无波形 / 扫描不到设备

1. 确认后端在跑：访问 `http://127.0.0.1:8001/api/status`。
2. 清理残留后端进程（同 5.1）。
3. 管理员 PowerShell 重置蓝牙服务：`Restart-Service bthserv`。
4. 仍无波形则给设备断电重启后重试；建议在 `config.local.yaml` 固定 `bluetooth.mac_address`。

### 5.3 页面打不开 / 持续转圈

- 先访问 `http://127.0.0.1:8001/api/status` 确认后端正常。
- 修改过端口则访问地址同步改为 `http://{host}:{port}/`。
- 首次启动可能被防火墙拦截：允许 `BHB-EEGSuite.exe` 本地访问。

### 5.4 pylsl 报错 / 无数据

- 先排查蓝牙采集是否正常（调试事件是否有持续输出）。
- 重启后端，确保无残留进程。
- 若提示缺少 VC 运行库，请安装 Microsoft Visual C++ Redistributable x64。

### 5.5 导出 CSV/EDF 失败

- 确认 `offline.root_dir` 目录可写、磁盘空间充足。
- EDF 失败先试 CSV 确认已录制，再排查 `pyedflib` 依赖。
