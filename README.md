# VMD + Multiwfn 风格脚本生成器

GitHub 仓库：`super3-moon/new_v`

这是一个 Windows 桌面辅助工具，用于生成 VMD + Multiwfn 自动工作流脚本。用户在图形界面中选择 Multiwfn、VMD 路径和可视化风格后，软件会生成可双击运行的 `.cmd` 脚本。

## 当前入口

- `vmd_style_tool_qt6.py`：PySide6 桌面版主程序，也是当前发行版打包入口。
- `vmd_style_tool.py`：风格数据、VMD Save State 解析、配置读写和 `.cmd` 生成核心逻辑。
- `vmd_cube_styles/`：内置风格封面图和 VMD 风格资源。
- `vmd_custom_styles.json`：自定义风格数据。

## 本地依赖

本仓库不跟踪大型第三方运行目录和发行版二进制文件。需要在本机准备：

- `Multiwfn_2026.3.27_bin_Win64/Multiwfn.exe`
- `vmd19.3/vmd.exe`
- Python 3.12+
- PySide6
- PyInstaller

`vmd_style_tool_config.json` 是本机路径配置文件，已设置为 Git 忽略项。程序缺失该文件时会自动扫描或重新保存配置。

## 运行

```powershell
python .\vmd_style_tool_qt6.py
```

自检：

```powershell
python .\vmd_style_tool_qt6.py --self-test
```

## 打包

```powershell
powershell -ExecutionPolicy Bypass -File .\build_release.ps1
```

发行版输出到 `release\YYYY-MM-DD\VMD_Multiwfn_StyleGenerator.exe`。发行策略见 `RELEASE_POLICY.md`。

## 协作规则

修改文件前先向 `THREAD_SYNC_LOG.md` 写入 `START`，完成后写入 `DONE`。新增或删除文件时同步更新 `工程结构.md`。

Git/GitHub 分支、提交、推送和 release 协作流程见 `GIT_GITHUB_WORKFLOW.md`。
