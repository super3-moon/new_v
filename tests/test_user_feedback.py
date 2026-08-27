from __future__ import annotations

import json
import unittest

import user_feedback


class UserFeedbackTests(unittest.TestCase):
    def test_missing_program_has_actionable_copy_and_keeps_raw_details(self) -> None:
        error = FileNotFoundError(2, "The system cannot find the file specified", "VMD.exe")
        info = user_feedback.describe_error(
            error,
            title="无法启动 VMD",
            stage="启动 VMD",
            program="VMD",
            file_path="E:/missing/vmd.exe",
        )
        self.assertEqual(info.reason, "未找到 VMD。")
        self.assertIn("重新选择 vmd.exe", info.action)
        self.assertNotIn("WinError", info.message)
        self.assertIn("FileNotFoundError", info.details)
        self.assertIn("E:/missing/vmd.exe", info.details)

    def test_json_failure_explains_recovery(self) -> None:
        try:
            json.loads("{")
        except json.JSONDecodeError as error:
            info = user_feedback.describe_error(error, stage="导入流程")
        self.assertIn("格式不完整", info.reason)
        self.assertIn("重新导出", info.action)
        self.assertIn("JSONDecodeError", info.details)

    def test_timeout_preserves_existing_result_guidance(self) -> None:
        info = user_feedback.describe_error(TimeoutError("VMD timed out"), program="VMD")
        self.assertEqual(info.severity, "warning")
        self.assertIn("已有结果和日志仍然保留", info.action)

    def test_unknown_failure_does_not_expose_raw_text_in_primary_message(self) -> None:
        raw = "opaque internal implementation traceback token"
        info = user_feedback.describe_error(RuntimeError(raw), stage="生成结果")
        self.assertNotIn(raw, info.message)
        self.assertIn(raw, info.details)

    def test_controlled_validation_message_remains_visible(self) -> None:
        info = user_feedback.describe_error(ValueError("请先添加一个输入文件"))
        self.assertIn("请先添加一个输入文件", info.reason)
        self.assertEqual(info.severity, "warning")


if __name__ == "__main__":
    unittest.main()
