"""逐檔回報 sink 的單元測試（core/copysink.py）。

★ 這個模組沒辦法在 Linux 上真的接到 IFileOperation 上，但它**大部分的
  風險不在 COM，在我們自己寫的那幾行**：

    - `PreCopyItem` 絕對不能回 S_FALSE（會中止整批操作，不是跳過一個）
    - callback 裡的任何例外都不能逸出（它在 COM 的呼叫堆疊裡執行）
    - 樣本數要有上限（3649 個失敗不能變成 3649 筆記憶體資料）

  這三件事都是純 Python，可以測，而且測起來很便宜。
"""

import logging
import sys
import unittest
from pathlib import Path

# 這些測試會**刻意**觸發列舉失敗與複製失敗，log 噪音對測試輸出沒有幫助。
logging.getLogger("iphone_backpacker").setLevel(logging.CRITICAL)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import _win_stubs   # noqa: E402

_win_stubs.install()

from iphone_backpacker.core import copysink   # noqa: E402

S_OK = 0
READ_FAULT = 0x8007001E


def _sink():
    outcome = copysink.CopyOutcome()
    outcome.attached = True
    return copysink._Sink(outcome), outcome


class DescribeHresultTests(unittest.TestCase):

    def test_known_code_is_explained(self):
        text = copysink.describe_hresult(READ_FAULT)
        self.assertIn("0x8007001E", text)
        self.assertIn("ERROR_READ_FAULT", text)

    def test_wpd_codes_are_explained(self):
        """★ 這幾個正是 Shell 路徑給不出來、要靠 sink/WPD 才看得到的狀態。"""
        self.assertIn("E_WPD_DEVICE_IS_HUNG",
                      copysink.describe_hresult(0x802A0006))
        self.assertIn("ERROR_DEVICE_IN_USE",
                      copysink.describe_hresult(0x80070964))

    def test_unknown_code_still_prints_hex(self):
        text = copysink.describe_hresult(0x8007ABCD)
        self.assertIn("0x8007ABCD", text)

    def test_negative_hresult_is_normalised(self):
        """pywin32 給的是有號整數，報告上要看得到一般的 8 位 16 進位。"""
        self.assertIn("0x8007001E", copysink.describe_hresult(-2147024866))

    def test_none_is_handled(self):
        self.assertIsInstance(copysink.describe_hresult(None), str)


class SinkBehaviourTests(unittest.TestCase):

    def test_counts_successes_and_failures(self):
        sink, outcome = _sink()
        for _ in range(3):
            sink.PostCopyItem(hrCopy=S_OK, NewName="ok.jpg")
        for _ in range(2):
            sink.PostCopyItem(hrCopy=READ_FAULT, NewName="bad.jpg")
        self.assertEqual(outcome.seen, 5)
        self.assertEqual(outcome.succeeded, 3)
        self.assertEqual(outcome.failed, 2)

    def test_records_where_failures_started(self):
        """★ 「前 N 個成功之後全垮」和「一開始就全垮」是不同的病。"""
        sink, outcome = _sink()
        for _ in range(4):
            sink.PostCopyItem(hrCopy=S_OK)
        sink.PostCopyItem(hrCopy=READ_FAULT)
        self.assertEqual(outcome.first_failure_at, 4)

    def test_no_failure_leaves_index_none(self):
        sink, outcome = _sink()
        sink.PostCopyItem(hrCopy=S_OK)
        self.assertIsNone(outcome.first_failure_at)

    def test_samples_are_capped(self):
        """3649 個失敗不能變成 3649 筆樣本。"""
        sink, outcome = _sink()
        for index in range(copysink.MAX_SAMPLES * 5):
            sink.PostCopyItem(hrCopy=READ_FAULT, NewName="f{}.jpg".format(index))
        self.assertEqual(len(outcome.samples), copysink.MAX_SAMPLES)
        self.assertEqual(outcome.failed, copysink.MAX_SAMPLES * 5)

    def test_negative_hresult_is_normalised(self):
        sink, outcome = _sink()
        sink.PostCopyItem(hrCopy=-2147024866)
        self.assertEqual(list(outcome.by_hresult), [READ_FAULT])

    def test_callback_never_raises(self):
        """★ 例外逸出 COM callback 等於把整個備份賠掉。"""
        sink, outcome = _sink()
        outcome.by_hresult = None      # 故意讓內部運算爆炸
        sink.PostCopyItem(hrCopy=S_OK)  # 不應該拋出任何東西
        sink.StartOperations()
        sink.FinishOperations(S_OK)

    def test_pre_callbacks_return_none(self):
        """★★ `PreCopyItem` 回 S_FALSE 會中止整批操作，不是跳過單一項目。

        這個 sink 只觀察、不干預，所有 Pre* 一律回 None（即 S_OK）。
        """
        sink, _outcome = _sink()
        for name in ("PreCopyItem", "PreMoveItem", "PreRenameItem",
                     "PreDeleteItem", "PreNewItem"):
            self.assertIsNone(getattr(sink, name)(0, None, None, None),
                              "{} 不能回傳非 None 的值".format(name))

    def test_finish_operations_is_recorded(self):
        sink, outcome = _sink()
        sink.StartOperations()
        sink.FinishOperations(READ_FAULT)
        self.assertTrue(outcome.started)
        self.assertTrue(outcome.finished)
        self.assertEqual(outcome.finish_hresult, READ_FAULT)


class DescribeTests(unittest.TestCase):

    def test_not_attached_says_so(self):
        outcome = copysink.CopyOutcome()
        self.assertIn("沒有接上", outcome.describe())

    def test_missing_finish_is_flagged(self):
        """沒有收到 FinishOperations 代表操作沒有正常收尾，要講出來。"""
        sink, outcome = _sink()
        sink.PostCopyItem(hrCopy=S_OK)
        self.assertIn("沒有收到 FinishOperations", outcome.describe())

    def test_describe_lists_hresult_distribution(self):
        """★ 這就是 8/30 那次我們最想要、卻完全沒有的東西。"""
        sink, outcome = _sink()
        for _ in range(3649):
            sink.PostCopyItem(hrCopy=READ_FAULT, NewName="x.jpg")
        sink.FinishOperations(S_OK)
        text = outcome.describe()
        self.assertIn("3649", text)
        self.assertIn("ERROR_READ_FAULT", text)


if __name__ == "__main__":
    unittest.main()
