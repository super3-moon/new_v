# 内置 VMD 风格参数核验（2026-07-20）

## 核验边界

- 本轮只核验，不修改任何 `BASE_VIEW`、`RAW_STYLES`、材质、颜色、灯光或视图参数。
- 改动前备份与当前源码的风格定义块逐字一致，SHA-256 均为 `062ee0aaf49517f4762ae1a8f653bdcbf47058e3f6e9afc587a2a3ce40d0242a`。
- 对照来源包括 [447](http://sobereva.com/447)、[483](http://sobereva.com/483)、[449](http://sobereva.com/449)、[443](http://sobereva.com/443)、[291](http://sobereva.com/291)，以及本机 Multiwfn 2026.3.27 附带的 `showorb.vmd`、`showcub.vmd`、`VMDrender.txt` 和 `RDGfill*.vmd`。
- 源码有 11 个原始套装；`classic_glossy_483` 因当前参数与 `classic_glossy_447` 完全相同而被去重，所以界面实际显示 10 个内置套装。

## 总表

| 套装 | 核验结论 | 关键事实 |
|---|---|---|
| `classic_glossy_447` | 部分一致 | 材质 Glossy、正红 `1`、负蓝 `0`、白背景、关闭 depth cue、GLSL、隐藏坐标轴与 447 原文/`showorb.vmd` 一致；但当前套装为 `light 3 off`，2026.3.27 的 `showorb.vmd` 明确为 `light 3 on`。套装还额外显式设置 0–2 号灯为 on。 |
| `classic_glossy_483`（被去重的别名） | 明确不一致 | 当前原始定义沿用了 447 的红 `1` / 蓝 `0` 和 `light 3 off`；483 原文及 `showcub.vmd` 使用浅绿 `12` / 浅蓝 `22`，且脚本为 `light 3 on`，并带有一组材质调整。当前“与 447 完全相同而去重”的事实本身就是来源错配。 |
| `soft_glossy_449` | 文章一致、当前脚本不完全一致 | 449 原文逐条列出的 C=tan、RGB、Opaque mirror/outline/outlinewidth、Glossy ambient/diffuse/opacity/shininess、颜色 `12/22`、`light 3 on` 均已匹配。当前 2026.3.27 `VMDrender.txt` 又增加 `ambient Opaque 0.08`、后置 `mirror Opaque 0.0`（覆盖前面的 0.15）、`display distance -7.0`、`display height 10`，套装未包含这些后续变化。 |
| `edgyglass_overlap_483` | 基本一致但非逐字一致 | 483 明确建议空穴/电子交叠图改为 EdgyGlass；颜色 `12/22` 与 `showcub.vmd` 一致。套装显式打开 0–3 号灯，而脚本只写 `light 3 on`，在 VMD 默认灯光状态下效果等价，但命令并非逐字一致。 |
| `bright_bule_yellow_userpack` | 无法核验 | 来源只标为 `user-upload:param-pack`，项目及备份中没有原始 `Bright_Bule+Yellow.txt`，目前无法证明每个 RGB/材质数值与原始参数包一致。 |
| `modern_cool_palette_userpack` | 无法核验 | 来源只标为 `user-upload:param-pack`，项目及备份中没有原始 `Modern_cool palette.txt`，目前无法独立核验。 |
| `edgyglass_tuned_443` | 材质数值一致，但不是完整的 443 命令组 | `display projection Orthographic` 及 EdgyGlass 的 outline `0.59`、outlinewidth `0.34`、opacity `0.73`、shininess `0.8`、diffuse `0.8`、specular `0.25` 与 443 原文完全一致。原文同组还包含 `color scale method turbo` 和 `mol scaleminmax 0 1 -0.06 0.06`，套装没有收录；`12/22` 双相颜色也不是该段原文参数。 |
| `goodsell_58009` | 无法核验 | 所标论坛页当前未能返回正文，项目内也没有原帖附件或原始参数文件；现有 8 个 Goodsell 材质数值和灯光组合缺少可复核证据。 |
| `edgy_58009` | 无法核验 | 同上，现有 8 个 Edgy 材质数值和灯光组合不能据现存材料确认。 |
| `translucent_clean_447` | 只有概念来源，非原文完整套装 | 447 确实推荐将 Glossy 替换为 EdgyGlass 或 Translucent；483 重点示例是 EdgyGlass。当前 `12/22` 和全灯光组合是混合选择，没有一段对应的原文/脚本可证明整套参数。 |
| `rdg_clarity_291` | 显示设置一致，但不等同 RDG 绘图脚本 | 白背景、关闭 depth cue、开启 3 号灯与 291 原文及 `RDGfill*.vmd` 一致。真正的 RDG 脚本还包含 Volume 着色、BGR 色标、`scaleminmax`、等值面和 CPK 参数；当前套装是普通双颜色单 cube 风格，不能视为完整复刻 RDG 脚本。 |

## 建议等待确认的项目

1. 将 `classic_glossy_483` 从 447 风格中拆开，按 `showcub.vmd` 恢复为颜色 `12/22`、`light 3 on` 及相应材质命令。
2. 决定 `classic_glossy_447` 是严格跟随 447 发表时效果，还是跟随当前 `showorb.vmd` 的 `light 3 on`。
3. 决定 `soft_glossy_449` 固定复刻文章列出的旧参数，还是同步 2026.3.27 `VMDrender.txt` 的最终有效参数；两者不能同时称为“完全一致”。
4. 对 443、291 等“只抽取部分显示参数”的套装，选择补全、改名为“灵感/局部参数”，或维持现状但在界面明确标注不是完整复刻。
5. 若要核验两个 userpack 和论坛 58009 套装，需要补回原始 `.txt`/帖子附件或提供可访问的原始内容。

在用户确认上述取舍前，不修改任何风格参数。
