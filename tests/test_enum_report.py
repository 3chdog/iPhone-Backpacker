"""列舉可信度（EnumReport / EnumStatus）的單元測試。

★ 這裡測的是**真正的** `core/shell_ns.py`，不是對照實作 ——
  只有 pywin32 的最外層邊界被換成假的（見 `_win_stubs`）。

  測的每一個案例都對應到一個真實的失敗：

    「0 項」分不出真假       → 2026-08-30 民眾B：13 個資料夾各花 8.8 秒
                               回傳 0 項，程式回報「全部已存在」，
                               3649 個檔案一個都沒備份到
    故障後重用 enumerator   → 微軟文件對這件事沒有任何保證
    靠順序去重              → 舊 CLI 版 zip 兩份獨立列舉，複製到錯的檔案
"""

import logging
import sys
import unittest
from pathlib import Path

# 這些測試會**刻意**觸發列舉失敗與複製失敗，log 噪音對測試輸出沒有幫助。
logging.getLogger("iphone_backpacker").setLevel(logging.CRITICAL)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import _win_stubs   # noqa: E402

pythoncom = _win_stubs.install()

from iphone_backpacker.core import shell_ns          # noqa: E402
from iphone_backpacker.core.errors import ShellError  # noqa: E402

#: 測試腳本用的特殊指令。
ERROR = object()      # 這一輪跑到這裡就拋 com_error
NULL = object()       # EnumObjects 回 None（S_FALSE + NULL enumerator）

ROOT = (b"root",)


class _FakeEnumerator:
    """假的 IEnumIDList。`Next(n)` 一次回最多 n 筆，模仿真實的分批行為。"""

    def __init__(self, actions):
        self._actions = list(actions)

    def Next(self, count):
        if count is None:
            count = 1
        chunk = []
        while self._actions and len(chunk) < count:
            action = self._actions.pop(0)
            if action is ERROR:
                raise pythoncom.com_error(
                    -2147024866, "The system cannot read from the specified device.",
                    None, None)
            chunk.append(action)
        return chunk


class _FakeFolder:
    def __init__(self, actions):
        self._actions = actions

    def EnumObjects(self, _hwnd, _flags):
        if self._actions and self._actions[0] is NULL:
            return None
        return _FakeEnumerator(self._actions)

    def GetDisplayNameOf(self, rel_pidl, _flags):
        return rel_pidl.decode()


class _Script:
    """每呼叫一次 bind_folder 就走下一輪腳本。"""

    def __init__(self, attempts):
        self.attempts = attempts
        self.binds = 0

    def bind(self, _abs_pidl):
        index = min(self.binds, len(self.attempts) - 1)
        self.binds += 1
        return _FakeFolder(list(self.attempts[index]))


class EnumReportTests(unittest.TestCase):

    def setUp(self):
        self._real_bind = shell_ns.bind_folder
        self._real_backoff = shell_ns.ENUM_BACKOFF_SECONDS
        # 測試不需要真的等 —— 退避的「有沒有等」不是這裡要驗的東西。
        shell_ns.ENUM_BACKOFF_SECONDS = (0.0, 0.0, 0.0)

    def tearDown(self):
        shell_ns.bind_folder = self._real_bind
        shell_ns.ENUM_BACKOFF_SECONDS = self._real_backoff

    def run_enum(self, attempts, **kwargs):
        script = _Script(attempts)
        shell_ns.bind_folder = script.bind
        report = shell_ns.EnumReport()
        names = [
            name for _abs, name, _attrs in shell_ns.iter_entries(
                ROOT, flags=shell_ns.EVERYTHING, batch=64,
                report=report, **kwargs)
        ]
        return names, report, script

    # ---- 正常路徑 ----

    def test_normal_enumeration(self):
        names, report, script = self.run_enum([[b"a", b"b", b"c"]])
        self.assertEqual(names, ["a", "b", "c"])
        self.assertIs(report.status, shell_ns.EnumStatus.OK)
        self.assertEqual(report.count, 3)
        self.assertEqual(report.attempts, 1)
        self.assertEqual(script.binds, 1)
        self.assertTrue(report.trustworthy)
        self.assertFalse(report.suspicious_zero)

    # ---- 快速的 0：瀏覽路徑不該為了它多跑一次（效能契約 D8）----

    def test_fast_empty_is_not_reverified(self):
        names, report, script = self.run_enum([[], [b"a"]], verify_empty=None)
        self.assertEqual(names, [])
        self.assertIs(report.status, shell_ns.EnumStatus.EMPTY)
        self.assertEqual(script.binds, 1, "快速的 0 不應該重驗")
        self.assertTrue(report.trustworthy)

    def test_verify_empty_true_reverifies(self):
        names, report, script = self.run_enum([[], []], verify_empty=True)
        self.assertEqual(names, [])
        self.assertIs(report.status, shell_ns.EnumStatus.EMPTY_VERIFIED)
        self.assertEqual(script.binds, 2)
        self.assertTrue(report.trustworthy)
        self.assertFalse(report.suspicious_zero)

    # ---- ★ 最重要的一個：第一次的 0 是假的 ----

    def test_empty_that_was_wrong_is_caught(self):
        """第一次回 0、重驗卻拿到東西 —— 這正是 8/30 災情的形狀。"""
        names, report, script = self.run_enum([[], [b"x", b"y"]],
                                              verify_empty=True)
        self.assertEqual(names, ["x", "y"])
        self.assertIs(report.status, shell_ns.EnumStatus.EMPTY_WAS_WRONG)
        self.assertEqual(report.count, 2)
        self.assertEqual(script.binds, 2)
        self.assertFalse(report.trustworthy,
                         "騙過我們一次的列舉不能算可信")

    # ---- 失敗與恢復 ----

    def test_error_then_success_recovers(self):
        names, report, script = self.run_enum([[b"a", b"b", ERROR],
                                               [b"a", b"b", b"c"]])
        self.assertIs(report.status, shell_ns.EnumStatus.RECOVERED)
        self.assertEqual(script.binds, 2, "重試必須重新 bind，不能重用 enumerator")
        self.assertEqual(report.count, 3)
        self.assertEqual(len(report.errors), 1)

    def test_retry_does_not_duplicate_items(self):
        """★ 用 PIDL 去重，不是「跳過前 N 筆」。

        Shell 不保證兩次列舉的順序一致，所以第二輪即使把順序整個打亂，
        呼叫端拿到的每一項也只能出現一次。
        """
        names, report, _script = self.run_enum([[b"a", b"b", ERROR],
                                                [b"c", b"b", b"a", b"d"]])
        self.assertEqual(sorted(names), ["a", "b", "c", "d"])
        self.assertEqual(len(names), len(set(names)), "不能有重複")
        self.assertEqual(report.count, 4)

    def test_all_attempts_fail_raises(self):
        script = _Script([[ERROR]])
        shell_ns.bind_folder = script.bind
        with self.assertRaises(ShellError) as caught:
            list(shell_ns.iter_entries(ROOT, flags=shell_ns.EVERYTHING))
        self.assertEqual(script.binds, shell_ns.ENUM_MAX_ATTEMPTS)
        self.assertIn("列舉失敗", str(caught.exception))

    def test_partial_then_total_failure_still_raises(self):
        """拿到一半就掛掉，仍然必須 raise —— 絕不能默默回傳半份清單。"""
        script = _Script([[b"a", b"b", ERROR]])
        shell_ns.bind_folder = script.bind
        with self.assertRaises(ShellError):
            list(shell_ns.iter_entries(ROOT, flags=shell_ns.EVERYTHING))

    # ---- EnumObjects 回 NULL（文件允許）----

    def test_null_enumerator_is_recorded(self):
        names, report, script = self.run_enum([[NULL]], verify_empty=False)
        self.assertEqual(names, [])
        self.assertIs(report.status, shell_ns.EnumStatus.NULL_ENUMERATOR)
        self.assertTrue(report.null_enumerator)
        self.assertEqual(script.binds, 1)

    def test_null_enumerator_can_be_reverified(self):
        names, report, _script = self.run_enum([[NULL], [b"a"]],
                                               verify_empty=True)
        self.assertEqual(names, ["a"])
        self.assertIs(report.status, shell_ns.EnumStatus.EMPTY_WAS_WRONG)
        self.assertTrue(report.null_enumerator)

    # ---- describe() 要讓人看得出是哪一種 0 ----

    def test_describe_mentions_count_and_time(self):
        _names, report, _script = self.run_enum([[b"a"]])
        text = report.describe()
        self.assertIn("1 項", text)
        self.assertIn("ms", text)

    def test_suspicious_zero_when_errors_happened(self):
        """出過錯、最後仍然是 0 —— 這個 0 不能信。"""
        _names, report, _script = self.run_enum([[ERROR], [], []],
                                                verify_empty=True)
        self.assertEqual(report.count, 0)
        self.assertTrue(report.errors)
        self.assertTrue(report.suspicious_zero)


class CostShapeTests(unittest.TestCase):
    """確認重驗策略沒有偷偷讓瀏覽路徑變慢（效能契約 D8）。"""

    def setUp(self):
        self._real_bind = shell_ns.bind_folder
        shell_ns.ENUM_BACKOFF_SECONDS = (0.0, 0.0, 0.0)

    def tearDown(self):
        shell_ns.bind_folder = self._real_bind

    def test_non_empty_never_reverifies(self):
        """有東西就不該重驗 —— 展開 344 個資料夾不能付兩倍的錢。"""
        script = _Script([[b"a"], [b"a"]])
        shell_ns.bind_folder = script.bind
        list(shell_ns.iter_entries(ROOT, flags=shell_ns.FOLDERS_ONLY,
                                   verify_empty=True))
        self.assertEqual(script.binds, 1)


if __name__ == "__main__":
    unittest.main()
