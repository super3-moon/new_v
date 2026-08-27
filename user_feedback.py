"""Shared conversion from technical failures to actionable user messages."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path


@dataclass(frozen=True)
class UserFacingError:
    title: str
    reason: str
    action: str
    details: str
    severity: str = "error"

    @property
    def message(self) -> str:
        return f"{self.reason}{self.action}"


def _technical_details(
    error: object,
    *,
    stage: str,
    program: str,
    file_path: str | Path | None,
    log_path: str | Path | None,
) -> str:
    lines = [
        f"异常类型：{type(error).__name__}",
        f"原始信息：{str(error).strip() or '(无)'}",
    ]
    if stage:
        lines.append(f"发生阶段：{stage}")
    if program:
        lines.append(f"相关程序：{program}")
    if file_path:
        lines.append(f"相关文件：{file_path}")
    if log_path:
        lines.append(f"完整日志：{log_path}")
    return "\n".join(lines)


def describe_error(
    error: object,
    *,
    title: str = "操作未完成",
    stage: str = "",
    program: str = "",
    file_path: str | Path | None = None,
    log_path: str | Path | None = None,
) -> UserFacingError:
    """Return consistent user copy while retaining complete diagnostics."""

    raw = str(error or "").strip()
    text = raw.casefold()
    program_text = str(program or "").casefold()
    details = _technical_details(
        error,
        stage=stage,
        program=program,
        file_path=file_path,
        log_path=log_path,
    )
    chosen_title = title or "操作未完成"

    if isinstance(error, json.JSONDecodeError) or any(
        marker in text
        for marker in ("jsondecodeerror", "json", "字段缺失", "格式不完整", "流程文件损坏")
    ):
        return UserFacingError(
            chosen_title,
            "流程文件格式不完整或已损坏，无法读取。",
            "请重新导出文件，或选择一份有效的 JSON/流程文件。",
            details,
        )
    if isinstance(error, PermissionError) or any(
        marker in text for marker in ("permissionerror", "access denied", "拒绝访问", "没有权限")
    ):
        return UserFacingError(
            chosen_title,
            "无法写入或读取所选位置。",
            "请选择具有访问权限的目录后重试。",
            details,
        )
    if isinstance(error, TimeoutError) or any(
        marker in text for marker in ("timeout", "timed out", "超时", "规定时间内未完成")
    ):
        return UserFacingError(
            chosen_title,
            "任务在规定时间内未完成，已停止。",
            "已有结果和日志仍然保留，可检查后继续或重试。",
            details,
            "warning",
        )
    if any(
        marker in text
        for marker in (
            "connectionerror",
            "network is unreachable",
            "网络不可用",
            "http 401",
            "http 403",
            "http 429",
            "too many requests",
            "model not found",
        )
    ):
        if any(marker in text for marker in ("401", "403", "凭据", "api key")):
            reason = "AI 服务访问凭据无效或没有权限。"
            action = "请检查访问凭据和服务权限。"
        elif any(marker in text for marker in ("429", "too many requests", "过于频繁")):
            reason = "AI 服务请求过于频繁。"
            action = "请稍后重试。"
        elif "model" in text or "模型" in text:
            reason = "当前选择的 AI 模型不可用。"
            action = "请选择可用模型后重试。"
        else:
            reason = "当前无法连接网络或外部服务。"
            action = "请检查网络连接后重试。"
        return UserFacingError(chosen_title, reason, action, details)

    missing = isinstance(error, FileNotFoundError) or any(
        marker in text
        for marker in (
            "winerror 2",
            "file not found",
            "no such file",
            "路径无效",
            "路径不存在",
            "文件不存在",
            "未找到",
        )
    )
    if missing and ("multiwfn" in text or "multiwfn" in program_text):
        return UserFacingError(
            chosen_title,
            "未找到 Multiwfn。",
            "请在程序路径设置中重新选择 Multiwfn.exe。",
            details,
        )
    if missing and ("vmd" in text or "vmd" in program_text):
        return UserFacingError(
            chosen_title,
            "未找到 VMD。",
            "请在程序路径设置中重新选择 vmd.exe。",
            details,
        )
    if any(
        marker in text
        for marker in ("couldn't open", "could not open", "error opening", "unable to read")
    ) and ("vmd" in text or "vmd" in program_text):
        return UserFacingError(
            chosen_title,
            "VMD 无法读取所需文件。",
            "请确认文件仍存在，并且所在路径可以访问。",
            details,
        )
    if any(marker in text for marker in ("cube", "轨道数据")) and any(
        marker in text for marker in ("missing", "empty", "为空", "未生成", "不存在")
    ):
        return UserFacingError(
            chosen_title,
            "轨道数据未成功生成。",
            "请检查输入文件以及 Multiwfn 日志后重试。",
            details,
        )
    if any(marker in text for marker in ("不匹配", "mismatch", "不兼容", "网格不一致")):
        return UserFacingError(
            chosen_title,
            "所选文件或绘图设置彼此不匹配。",
            "请检查输入配对、轨道范围和绘图方案。",
            details,
            "warning",
        )
    if isinstance(error, ValueError) and raw:
        reason = raw if raw.endswith(("。", "！", "？", ".", "!", "?")) else f"{raw}。"
        return UserFacingError(
            chosen_title,
            reason,
            "请按提示调整输入或设置后重试。",
            details,
            "warning",
        )
    if missing:
        return UserFacingError(
            chosen_title,
            "无法找到操作所需的文件。",
            "请确认文件仍存在，并重新选择后重试。",
            details,
        )
    if "multiwfn" in text or "multiwfn" in program_text:
        return UserFacingError(
            chosen_title,
            "Multiwfn 未能完成当前计算。",
            "请查看运行日志，确认输入文件和操作流程后重试。",
            details,
        )
    if any(marker in text for marker in ("vmd", "tachyon")) or "vmd" in program_text:
        return UserFacingError(
            chosen_title,
            "VMD 未能生成所需图像。",
            "请确认输入文件仍可访问，并查看运行日志。",
            details,
        )
    return UserFacingError(
        chosen_title,
        "任务未能完成。",
        "已有结果不会被删除，请查看详细信息或运行日志后重试。",
        details,
    )


def friendly_error_text(error: object, **context: object) -> str:
    return describe_error(error, **context).message


__all__ = ["UserFacingError", "describe_error", "friendly_error_text"]
