# 远程可选模块

文本-音频多模态范式作为独立扩展发布。基础版不携带扩展代码和音频。模式页保留入口，在「模块管理」中从远程仓库下载并安装。安装状态在重启后保留；卸载不会删除离线采集或导出的实验数据。

用户下载的是 Releases 页面中的 `BHB-EEGSuite-base.zip`。GitHub 仓库的「Code → Download ZIP」会包含开发用扩展源码，不是面向用户的基础包。

## 发布流程

项目目录分工：

- `extensions/music/`：扩展源码与素材，仅供构建发布资产，不参与运行时安装。
- `core/module_store.py`：远程索引、HTTPS 下载、SHA-256 校验和安全解包。
- `core/modules.py`：安装切换、加载、卸载与接口隔离。
- `module_data/music/`：本机已安装的运行副本；`module_data/cache/`：已校验的下载缓存，两者都不入库。

发布由 `.github/workflows/release-music.yml` 自动完成。在 `main` 提交扩展源码、安装器和工作流后，确认 `extensions/music/manifest.json` 中的 `version`，再推送对应标签。当前版本的命令是：

```powershell
git tag music-V1
git push origin main
git push origin music-V1
```

Actions 会核对标签和模块版本，生成并发布 `BHB-EEGSuite-base.zip`、`BHB-EEGSuite-music-V1.zip` 和 `modules.json`。本机测试文件保留在 `tests/`，不提交到 GitHub。`modules.json` 中的 ZIP URL 指向带版本号的 Release 资产；不要覆盖已经发布的标签或包。发布新版本时将 `manifest.json` 的版本改为 `V2`，提交后推送 `music-V2` 标签。项目默认从 `releases/latest/download/modules.json` 获取索引，因此后续公开 Release 也必须包含该索引。如果仓库使用其他托管位置，在 `configs/config.local.yaml` 中覆盖：

```yaml
modules:
  catalog_url: "https://your-host.example/modules.json"
```

远程索引格式由构建脚本生成，包含 `schema`、模块 `id`、`version`、`host_api`、ZIP 的 HTTPS `url`、`sha256` 和 `size`。客户端仅接受 HTTPS，限制下载和解压大小，校验摘要与包内 `manifest.json`，拒绝路径穿越和符号链接，再切换运行目录。下载失败、校验失败或加载失败时，不启用扩展，旧安装目录会恢复。

## 用户操作

打开「模块管理」，等待远程版本信息，然后点击「下载并安装」。有新版本时可直接点击更新。断网或索引未发布时会显示具体错误。安装期间显示下载进度；安装完成后模式入口可用。实验运行或导出期间不能更新或卸载。卸载仅删除 `module_data/music/`；已采集和导出的文件仍位于配置的离线数据目录。

主程序不直接从扩展源码加载代码。只有安装成功后才挂载扩展 API 和页面资源，未安装时返回 404。模块仍通过主程序的采集接口使用当前蓝牙连接。

## 验收

1. 在 GitHub 仓库 Actions 页面确认 `Release music extension` 运行成功，并在 Releases 页面看到 `music-V1` 和三个资产。
2. 在项目根目录运行下列公网校验命令，它会在系统临时目录下载并核对 ZIP，不修改已有 `module_data`：

```powershell
python -m tools.verify_remote_release https://github.com/SpoonRiv/BHB-EEGSuite/releases/latest/download/modules.json
```

3. 将 Release 中的 `BHB-EEGSuite-base.zip` 解压到一个**全新目录**。在该目录按 README 安装依赖并运行 `python main.py`。打开模式页的「模块管理」，应显示 `可安装 V1`，点击「下载并安装」，进度结束后应显示 `已安装 V1`，音乐模式入口变为可用。
4. 重启应用，确认仍显示已安装且能进入音乐模式。完成一条测试实验并导出数据，再从模块管理卸载，确认入口恢复未安装，同时实验数据仍在 `offline.root_dir`。

不要在已有实验数据的工作目录执行“全新安装”测试；这个验收流程不要求清理当前 `module_data`。
