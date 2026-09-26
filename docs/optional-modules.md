# 远程可选模块

文本-音频多模态范式作为独立扩展发布。基础版不携带扩展代码和音频。模式页保留入口，在「模块管理」中从远程仓库下载并安装。安装状态在重启后保留；卸载不会删除离线采集或导出的实验数据。

用户下载的是 Releases 页面中的 `BHB-EEGSuite-base.zip`。GitHub 仓库的「Code → Download ZIP」会包含开发用扩展源码，不是面向用户的基础包。

## 发布流程

项目目录分工：

- `extensions/music/`：扩展源码与素材，仅供构建发布资产，不参与运行时安装。
- `core/module_store.py`：远程索引、HTTPS 下载、SHA-256 校验和安全解包。
- `core/modules.py`：安装切换、加载、卸载与接口隔离。
- `module_data/music/`：本机已安装的运行副本；`module_data/cache/`：已校验的下载缓存，两者都不入库。

发布工具只保留在发布者本机的 `tools/`，不提交到 Git。Windows 本机使用项目的 `BHB` Conda 环境构建；首次使用时安装 PyInstaller：

```powershell
conda env create -f environment.yml
conda run -n BHB python -m pip install pyinstaller==6.16.0
```

每次发布先确定一个新标签，例如 `v3.5.7`，再在项目根目录运行：

```powershell
$releaseTag = "v3.5.7"
conda run -n BHB python tools/build_windows.py --asset-base-url "https://github.com/SpoonRiv/BHB-EEGSuite/releases/download/$releaseTag"
```

在 GitHub Releases 页面手动创建同名标签的 Release，从本机 `dist/` 上传 `BHB-EEGSuite-base.zip`、`BHB-EEGSuite-music-V1.zip` 和 `modules.json`，然后发布。基础 ZIP 包含 Windows exe、Python 运行时、依赖、页面和默认配置，不含项目文档；音乐 ZIP 是可选附件，不进入基础包。`modules.json` 中的 ZIP URL 必须与实际 Release 标签一致。不要覆盖已经发布的标签或包；只有音乐内容变化时才将 `manifest.json` 的版本改为 `V2`。项目默认从 `releases/latest/download/modules.json` 获取索引，因此后续公开 Release 也必须包含该索引。`tools/` 和 `tests/` 都只保留在本机，换电脑构建前需要自行备份这些工具。如果仓库使用其他托管位置，在 `configs/config.local.yaml` 中覆盖：

```yaml
modules:
  catalog_url: "https://your-host.example/modules.json"
```

远程索引格式由构建脚本生成，包含 `schema`、模块 `id`、`version`、`host_api`、ZIP 的 HTTPS `url`、`sha256` 和 `size`。客户端仅接受 HTTPS，限制下载和解压大小，校验摘要与包内 `manifest.json`，拒绝路径穿越和符号链接，再切换运行目录。下载失败、校验失败或加载失败时，不启用扩展，旧安装目录会恢复。

## 用户操作

打开「模块管理」，等待远程版本信息，然后点击「下载并安装」。有新版本时可直接点击更新。断网或索引未发布时会显示具体错误。安装期间显示下载进度；安装完成后模式入口可用。实验运行或导出期间不能更新或卸载。卸载仅删除 `module_data/music/`；已采集和导出的文件仍位于配置的离线数据目录。

主程序不直接从扩展源码加载代码。只有安装成功后才挂载扩展 API 和页面资源，未安装时返回 404。模块仍通过主程序的采集接口使用当前蓝牙连接。

## 验收

1. 在 GitHub Releases 页面确认新标签下有上述三个附件，并检查 `modules.json` 内的下载 URL 使用同一个标签。
2. 在项目根目录运行下列公网校验命令，它会在系统临时目录下载并核对 ZIP，不修改已有 `module_data`：

```powershell
python -m tools.verify_remote_release https://github.com/SpoonRiv/BHB-EEGSuite/releases/latest/download/modules.json
```

3. 将 Release 中的 `BHB-EEGSuite-base.zip` 解压到一个**全新目录**。双击 `BHB-EEGSuite.exe`，无需安装 Python。打开模式页的「模块管理」，应显示 `可安装 V1`，点击「下载并安装」，进度结束后应显示 `已安装 V1`，音乐模式入口变为可用。
4. 重启应用，确认仍显示已安装且能进入音乐模式。完成一条测试实验并导出数据，再从模块管理卸载，确认入口恢复未安装，同时实验数据仍在 `offline.root_dir`。

不要在已有实验数据的工作目录执行“全新安装”测试；这个验收流程不要求清理当前 `module_data`。
