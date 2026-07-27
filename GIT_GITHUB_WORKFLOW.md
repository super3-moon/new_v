# Git / GitHub 协作流程

## 仓库

- 本地目录：当前 Git 工作区（下文记作 `<项目目录>`）
- GitHub 远端：`origin` -> `https://github.com/super3-moon/new_v.git`
- 默认分支：`main`

## 总原则

1. `main` 始终保持可运行、可打包的稳定状态。
2. 每次改动前先同步远端并检查工作区状态。
3. 业务源码修改和发行打包分开做，不在同一次提交里混杂。
4. 大型第三方程序目录、发行版 exe、临时计算文件、本机路径配置和用户自定义风格不进 Git。
5. 每个改动线程必须维护 `THREAD_SYNC_LOG.md`；新增或删除文件必须同步 `工程结构.md`。
6. GitHub 的代码页只保存当前稳定源码、测试、资源和文档；二进制只放在 GitHub Releases。
7. GitHub Releases 只保留当前最新发行版。

## 开始任意工作前

```powershell
git fetch origin
git status --short --branch
```

如果当前分支落后于远端：

```powershell
git pull --ff-only
```

如果工作区已有未提交改动，先确认这些改动属于谁、属于哪个线程，再决定继续、提交或暂存。

## 源码开发线程

用于修改 `vmd_style_tool.py`、`vmd_style_tool_qt6.py`、资源、文档或配置逻辑。

推荐分支命名：

- `feature/<短描述>`
- `fix/<短描述>`
- `hotfix/<短描述>`

流程：

```powershell
git switch main
git pull --ff-only
git switch -c feature/<短描述>
```

然后：

1. 写 `THREAD_SYNC_LOG.md` 的 `START`。
2. 修改源码或资源。
3. 运行必要检查，例如 `python .\vmd_style_tool_qt6.py --self-test`。
4. 写 `THREAD_SYNC_LOG.md` 的 `DONE`。
5. 提交并推送分支。
6. 在 GitHub 上开 PR 合入 `main`。

## 发行打包线程

发行打包线程只做打包和发布，不夹带业务逻辑修改。

推荐分支命名：

- `release/YYYY-MM-DD`
- 同一天多次发布时使用 `release/YYYY-MM-DD-v2`

流程：

```powershell
git switch main
git pull --ff-only
git switch -c release/YYYY-MM-DD
```

然后：

1. 读取 `THREAD_SYNC_LOG.md` 最近的源码 `DONE` 记录。
2. 写 packaging `START`。
3. 执行 `powershell -ExecutionPolicy Bypass -File .\build_release.ps1`。
4. 验证生成的 exe 能启动或至少通过自检。
5. 写 packaging `DONE`。
6. 提交日志或文档变化。
7. release 二进制不提交到 Git；需要公开发布时，用 GitHub Releases 上传。
8. 新 Release 上传并验证完成后，删除更旧的 GitHub Release 和对应标签。

推荐标签：

- `vYYYY.MM.DD`
- 同日多版：`vYYYY.MM.DD-2`

## 本地发行版保留规则

- `release\` 按日期保留历史。
- 同一天出现多个版本时，只保留序号最高的目录。
- 清理前先运行 `cleanup_project.ps1` 预览，确认后使用 `-Apply`。
- 清理脚本会先在项目外建立恢复压缩包，再删除同日中间版本和构建缓存。

## 禁止事项

- 不直接把 `release/`、`vmd19.3/`、`Multiwfn_*_bin_Win64/` 加入 Git。
- 不提交 `vmd_custom_styles.json`、自定义封面、本机配置和自动生成的工作流脚本。
- 不在未确认的情况下 `git push --force`。
- 不在 release 分支里顺手改业务功能。
- 不在源码分支里提交本机路径配置 `vmd_style_tool_config.json`。

## 收尾检查

每次提交前至少检查：

```powershell
git status --short
git diff --check
```

每次推送后至少检查：

```powershell
git status --short --branch
git log --oneline --decorate -5
git remote -v
```
