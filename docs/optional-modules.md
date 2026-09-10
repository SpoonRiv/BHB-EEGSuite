# 可选范式模块

文本-音频多模态范式作为独立模块提供。首次启动时不安装、不加载模块后端，模式页最右侧保留灰色入口。

## 安装与卸载

1. 在模式页打开「模块管理」，点击蓝色「安装」按钮。
2. 安装期间按钮显示转圈动画，完成后模式入口恢复彩色，点击卡片进入范式。
3. 重启上位机后保留安装状态。在模块管理中点击「卸载」可移除模块；实验运行或导出期间不可卸载。

当前采用本地安装包，不会联网下载。源码目录已附带安装包；基础版用户需先将模块 ZIP 解压到上位机根目录，确认存在 `optional_modules/music/manifest.json`，再打开模块管理直接安装。

卸载仅移除 `module_data/music`，已采集和导出的文件仍保留在配置的离线数据目录中。重新安装不会删除这些文件。

## 发布

在项目根目录运行：

```powershell
python checks/build_release.py
```

生成两个独立的源码发布包，均使用现有 `environment.yml` 安装依赖、通过 `python main.py` 启动：

- `dist/BHB-EEGSuite-base.zip`：基础上位机，不包含范式代码、页面、音频、歌词及本机安装状态。
- `dist/BHB-EEGSuite-music-1.0.0.zip`：模块代码、页面和 10 组音频与歌词，解压到基础上位机根目录后点击安装。

发布脚本不打包本机配置覆盖、实验数据和模块安装目录。手工发布时也必须排除 `module_data`；发布基础版时还应排除 `optional_modules`。

## 目录与接口

- `optional_modules/music` 是安装包来源，包含 `manifest.json`、后端、`web` 页面和 `resources` 实验素材。
- `module_data/music` 是点击安装后生成的运行副本，已加入 Git 忽略。安装后删除来源包不影响当前安装。
- `core/modules.py` 管理安装、恢复、卸载和资源访问。未安装时，范式接口和资源返回 404。
- 模块通过主程序提供的采集与释放接口使用当前蓝牙连接，并注册脑电数据回调，不建立独立设备连接。
- 模式卡片始终位于最右侧，未安装时点击会打开模块管理；安装后点击进入范式。卸载后刷新页面以清除模块事件处理器，卡片恢复灰色。

## 验证

```powershell
python -m unittest discover -s checks -p 'test_*.py' -v
```

`checks/verify_modules_ui.cjs` 可对运行中的开发服务器执行浏览器检查。需要可用的 Playwright 和 Microsoft Edge，默认访问 `http://127.0.0.1:8015`，可通过 `MODULE_TEST_URL` 覆盖。执行前模块应处于未安装状态，检查完成后会卸载模块。
