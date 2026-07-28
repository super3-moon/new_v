# VMD + Multiwfn 绘图工作台

GitHub 仓库：`super3-moon/new_v`

这是一个 Windows 桌面辅助工具，用于直接编排 Multiwfn + VMD 绘图流程，同时保留工作流脚本导出和可自定义的 Multiwfn 批处理工作台。用户可以选择视觉风格后拖入本地文件直接绘图，也可以按需导出可双击运行的 `.cmd` 脚本。

## 功能亮点

- 套装模式与骨架/等值面拆分模式。
- “查看风格参数 / 导出脚本 / 直接绘图”均为风格页底部的明确可见操作。
- 直接绘图支持点击选择或拖入单个本地文件；Cube 文件直接进入 VMD，其他输入文件由软件打开可交互的 Multiwfn。
- 直接流程自动检测本次新增或更新的 Cube；检测到多个时由用户选择，没有检测到时可重新运行或手动指定。
- VMD 图片默认保存到输入文件所在目录，也可以在运行前改为自定义结果目录。
- 按名称、说明、来源搜索，并支持材质筛选和多种排序。
- 默认窗口采用接近 `1320 × 718` 的逻辑尺寸（Windows 125% 缩放下含外框截图约为 `1650 × 938`），与常用桌面比例保持平衡；套装模式保持三列浏览，AI 识别和批量文件页会随内容宽度自动重排，避免在非全屏窗口中截断按钮。
- 拆分模式始终保持骨架与等值面左右等宽展示，并使用一致的双列紧凑卡片；程序路径与活动记录常驻左侧，风格参数首先以只读摘要展示，点击“编辑参数”后才进入完整表单。
- 浅色/深色主题，可用 `Ctrl+T` 快速切换。
- `Ctrl+F` 聚焦搜索，`Ctrl+Enter` 进入直接绘图，`Ctrl+G` 导出脚本。
- 从 VMD 状态文件（Save State）导入自定义风格。
- OpenAI / Gemini 图片风格识别、本地测色、裁剪和参数微调。
- AI 网络识别在后台执行，识别过程中界面仍可浏览和调整。
- 损坏或缺失的封面自动显示统一占位预览。
- 独立的“批量 Multiwfn”工作台，支持直接粘贴命令、导入 TXT，以及在软件内操作一次并自动记录完整流程。
- 批量流程按“获取操作流程 → 核对命令 → 勾选结果类型 → 命名保存”组织；不常用的手动匹配规则、命令行参数和变量默认折叠。
- 统一的“绘图方案 / 自定义 / 批量任务”工作区导航与中性色视觉系统；套装模式和拆分模式仅在绘图方案中显示，三个批量页面均可独立滚动，并提供清晰空状态、深浅主题和克制的页面/进度动画。
- 每个批处理任务使用独立工作目录，避免 `density.cub`、`totesp.cub` 等固定输出名互相覆盖。
- 实时日志、超时/取消、输出检查和重命名；即使未配置生成文件，也会自动汇总完整 Multiwfn 日志，并生成 `manifest.json` 与 `summary.csv`。
- 内置“导出 XYZ”“ESP + 电子密度 Cube”“分子表面 ESP 描述符”三个已验证模板。

## 当前入口

- `vmd_style_tool_qt6.py`：PySide6 桌面版主程序，也是当前发行版打包入口。
- `vmd_style_tool.py`：风格数据、VMD Save State 解析、共用 VMD Tcl 与 `.cmd` 生成核心逻辑。
- `direct_workflow_qt6.py`：文件拖放、直接调用 Multiwfn、Cube 检测、VMD 启动和临时文件清理。
- `multiwfn_batch.py`：批处理模板、任务规划、Multiwfn 执行、输出归档和汇总核心。
- `multiwfn_batch_qt6.py`：批量工作台、流程编辑器、文件队列和实时日志界面。
- `multiwfn_recorder_qt6.py`：内嵌 Multiwfn 试运行终端、逐步输入记录和结果文件识别。
- `vmd_cube_styles/`：内置风格封面图和 VMD 风格资源。
- `vmd_custom_styles.default.json`：发行版首次启动时使用的空白自定义风格模板。
- `vmd_custom_styles.json`：用户自己的风格数据，仅保存在本机，不提交到 Git。

## 本地依赖

本仓库不跟踪大型第三方运行目录和发行版二进制文件。需要在本机准备：

- `Multiwfn_2026.7.11_bin_Win64/Multiwfn.exe`
- `vmd19.3/vmd.exe`
- Python 3.12+
- PySide6 6.11.0
- PyInstaller 6.19.0（仅打包需要）

安装运行依赖：

```powershell
python -m pip install -r .\requirements.txt
```

安装打包依赖：

```powershell
python -m pip install -r .\requirements-build.txt
```

`vmd_style_tool_config.json` 是本机路径配置文件，已设置为 Git 忽略项。程序缺失该文件时会自动扫描或重新保存配置。

`multiwfn_batch_presets.json` 保存用户自定义的批量流程，同样只保存在本机。

自定义风格封面使用 `vmd_cube_styles/custom_*_cover.*` 命名，也只保存在本机。打包时不会把个人自定义风格和封面带入公开发行版。

## 直接绘图使用

1. 在绘图方案中选择套装风格，或在拆分模式分别选择骨架和等值面风格。
2. 点击底部“直接绘图”，将一个本地文件拖入添加区域，或点击该区域选择文件。
3. 结果目录默认使用输入文件所在目录；如需更改，在运行设置中选择自定义目录。VMD Render 产生的图片会被定向到这里。
4. 填写适合当前分析的正等值面数值。程序不会擅自设置科学默认值，负等值面会自动取相反数。
5. Cube 文件会直接打开 VMD。其他文件会打开 Multiwfn，请在其窗口中生成 Cube 并正常输入 `q` 退出，软件随后自动继续到 VMD。

直接绘图不会在项目目录生成 `.cmd`；VMD 所需 Tcl 只临时存在于系统临时目录，并在 VMD 退出后清理。Cube、dat 和渲染图片作为结果保留。

## 批量 Multiwfn 使用

1. 在左侧设置正确的 `Multiwfn.exe`，打开“批量 Multiwfn”。
2. 添加输入文件、按当前流程支持的扩展名扫描文件夹，或将文件和文件夹直接拖入列表。已取消勾选的文件在继续添加文件时不会被重新启用。
3. 选择内置流程，或点击 `+` 新建空白流程。新建和复制会明确显示为“未保存草稿”；修改已有流程后会显示“有未保存修改”，切换流程前可以选择保存、放弃或取消。命令序列可以直接粘贴、从 `.txt` 导入，也可以选择一个示例文件，在内嵌 Multiwfn 窗口中操作一次并自动记录。录制时应完成全部菜单操作并让 Multiwfn 正常退出；尚在等待输入或异常退出的记录不能采用。
4. 核对命令序列：每行代表一次输入，只有单独的空行才代表按回车；随后勾选 Cube、文本、数据表、结构、波函数或图片等结果类型。手动文件匹配、命令行参数、变量和超时位于折叠的高级设置中。
5. 先使用“预检/预览”检查首个任务；对于新流程，建议执行“首文件试运行”。试运行成功后可在结果页直接开始完整批次。
6. 确认输出正确后开始批处理。运行区会明确说明本次使用的是已保存流程还是未保存编辑内容；任务完成后可打开运行目录查看结果、日志和 CSV 汇总。

流程中的 `${input}`、`${stem}`、`${index}`、`${job_dir}`、`${output_dir}` 等变量会按文件自动替换。需要时可在高级设置中添加用户参数，例如 `${rho_iso}`。

默认结果结构：

```text
batch_runs\batch_YYYYMMDD_HHMMSS_xxxxxx\
├─ manifest.json                 # 完整运行状态与模板快照
├─ summary.csv                   # 成功、失败、耗时和输出汇总
├─ results\                     # 已检查并重命名的最终结果
└─ jobs\0001_文件名\
   ├─ stdin.txt                  # 实际发送给 Multiwfn 的输入
   ├─ stdout.log                 # 完整控制台输出
   ├─ job.json / result.json     # 单任务配置与结果
   └─ Multiwfn 原始输出文件
```

## 运行

```powershell
python .\vmd_style_tool_qt6.py
```

自检：

```powershell
python .\vmd_style_tool_qt6.py --self-test
```

完整自动测试：

```powershell
python -m unittest discover -s tests -v
```

## 打包

```powershell
powershell -ExecutionPolicy Bypass -File .\build_release.ps1
```

发行版输出到 `release\YYYY-MM-DD\VMD_Multiwfn_StyleGenerator.exe`。发行策略见 `RELEASE_POLICY.md`。

同一天重复打包时会自动使用 `_v2`、`_v3` 等目录，避免覆盖已有发行版。完成验证后使用工程清理脚本，只保留同一日期中序号最高的发行目录。

生成脚本会用运行标记只选择本次 Multiwfn 新建或更新的 `.cub` 文件。该查找逻辑不再在 `cmd.exe` 的 `for /f` 子命令中嵌套 PowerShell 管道，避免管道转义导致找不到 cube、VMD 未启动的问题。

## 工程清理

先预览，不会删除任何内容：

```powershell
powershell -ExecutionPolicy Bypass -File .\cleanup_project.ps1
```

确认清单后执行：

```powershell
powershell -ExecutionPolicy Bypass -File .\cleanup_project.ps1 -Apply
```

脚本会：

- 每个日期只保留序号最高的发行目录。
- 删除 PyInstaller 工作目录、Python 缓存和项目根目录中自动生成的工作流脚本。
- 在删除旧发行目录前，于项目外创建恢复压缩包。
- 保留 Multiwfn、VMD、本机配置、批处理结果和用户自定义风格。

Git 仓库只保存当前源码、测试、资源和文档。发行版二进制通过 GitHub Releases 发布，GitHub 只保留当前最新 Release。

## 协作规则

修改文件前先向 `THREAD_SYNC_LOG.md` 写入 `START`，完成后写入 `DONE`。新增或删除文件时同步更新 `工程结构.md`。

Git/GitHub 分支、提交、推送和 release 协作流程见 `GIT_GITHUB_WORKFLOW.md`。
