"""IFileOperation 的逐檔結果回呼。

★★ 這個模組推翻了決策 D6（見 D19）。

  D6 當時寫「pywin32 對 `IFileOperationProgressSink` 支援不完整，改用
  `FOF_NOERRORUI` + 事後驗證掃描」。**那個前提是錯的。** pywin32 不但支援，
  還自己附了範例（`com/win32comext/shell/demos/IFileOperationProgressSink.py`），
  而 `PostCopyItem` 的簽章帶著**逐檔的 HRESULT**：

      PostCopyItem(Flags, Item, DestinationFolder, NewName, hrCopy, NewlyCreated)

  代價是實打實的：2026-08-30 民眾B 那次 3649 個檔案全數失敗，我們只知道
  「全部失敗」，一個錯誤碼都沒有，到今天都還在猜。有了這個 sink，同樣的
  災情會留下 3649 個 HRESULT 的分布，一次就能定位。

★ 三個地雷，都是外部實測來源，不要自己試：

  1. **`PreCopyItem` 一律回 S_OK。** 從 `PreCopyItem` 回 S_FALSE 會
     **中止整批操作**，不是「跳過這一個」（xplorer² 實測）。
     我們只觀察、不干預，所有 Pre* 一律什麼都不做。

  2. **callback 裡絕不寫 log、絕不呼叫 COM。** 它會在跑 `PerformOperations()`
     的那條執行緒上被呼叫**每個檔案一次**。3649 次 log.warning 是 3649 次
     磁碟寫入；而 `Item.GetDisplayName()` 在 MTP 上是一次來回（~3.4 ms），
     3649 次就是 12 秒純浪費。所以這裡只累加計數，名稱用免費的 `NewName`。

  3. **任何例外都不能逸出 callback。** 它是在 COM 的呼叫堆疊裡執行的，
     讓例外跑出去等於把整個備份賠掉。每一個方法都包 try/except。

★ sink 是**加分項，不是必需品**：建立失敗、Advise 失敗、pywin32 版本不合，
  一律降級成原本的「事後驗證掃描」，備份照跑。診斷能力可以失去，
  備份能力不行。
"""

import logging
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import pythoncom
from win32com.server.policy import DesignatedWrapPolicy
from win32com.shell import shell

log = logging.getLogger(__name__)

#: 失敗樣本最多留幾個。不需要全部 —— 我們要的是「錯誤碼的分布」與
#: 「第幾個開始壞的」，不是 3649 個檔名（那個驗證掃描已經有了）。
MAX_SAMPLES = 20

S_OK = 0


@dataclass
class CopyOutcome:
    """一次 `PerformOperations()` 期間，Shell 逐檔回報了什麼。"""

    #: 有沒有收到 StartOperations / FinishOperations。
    started: bool = False
    finished: bool = False
    #: FinishOperations 帶的整體 HRESULT。
    finish_hresult: Optional[int] = None

    #: 總共收到幾次 PostCopyItem。
    seen: int = 0
    #: hresult -> 次數。**這是這個模組存在的理由。**
    by_hresult: Dict[int, int] = field(default_factory=lambda: Counter())
    #: 第幾個項目開始失敗（0-based）。None 表示沒有任何失敗。
    first_failure_at: Optional[int] = None
    #: 前幾個失敗的樣本：(序號, 名稱, hresult)。
    samples: List[Tuple[int, str, int]] = field(default_factory=list)

    #: sink 有沒有真的接上。False 時上面的數字全部不具意義。
    attached: bool = False

    @property
    def failed(self):
        return sum(n for hr, n in self.by_hresult.items() if hr != S_OK)

    @property
    def succeeded(self):
        return self.by_hresult.get(S_OK, 0)

    def describe(self):
        """給 log 與診斷報告用的多行文字。"""
        if not self.attached:
            return "（逐檔回報沒有接上，只能靠驗證掃描）"
        lines = ["收到 {} 次逐檔回報：成功 {}、失敗 {}".format(
            self.seen, self.succeeded, self.failed)]
        for hr, count in sorted(self.by_hresult.items(),
                                key=lambda kv: -kv[1]):
            lines.append("    {} × {}".format(describe_hresult(hr), count))
        if self.first_failure_at is not None:
            lines.append("    第一個失敗出現在第 {} 個項目".format(
                self.first_failure_at + 1))
        for index, name, hr in self.samples:
            lines.append("    #{} {} → {}".format(
                index + 1, name or "（沒有名稱）", describe_hresult(hr)))
        if self.finished:
            lines.append("    FinishOperations：{}".format(
                describe_hresult(self.finish_hresult or S_OK)))
        else:
            lines.append("    ★ 沒有收到 FinishOperations —— 操作沒有正常收尾")
        return "\n".join(lines)


#: 已知會遇到的 HRESULT。只用來讓報告好讀，判斷邏輯不依賴這張表。
#: 值來自 WPD 的錯誤碼表與 Win32 系統錯誤碼。
_HRESULT_TEXT = {
    0x00000000: "S_OK 成功",
    0x80070005: "ERROR_ACCESS_DENIED 存取被拒",
    0x8007001E: "ERROR_READ_FAULT 無法從裝置讀取",
    0x8007001F: "ERROR_GEN_FAILURE 裝置沒有回應",
    0x80070021: "ERROR_LOCK_VIOLATION 檔案被鎖住",
    0x80070026: "ERROR_HANDLE_EOF 讀到非預期的結尾",
    0x80070079: "ERROR_SEM_TIMEOUT 裝置逾時",
    0x800700AA: "ERROR_BUSY 裝置忙碌（稍後重試可能會好）",
    0x8007045D: "ERROR_IO_DEVICE 裝置 I/O 錯誤",
    0x8007048F: "ERROR_DEVICE_NOT_CONNECTED 裝置已中斷連線",
    0x800705B4: "ERROR_TIMEOUT 逾時",
    0x80070964: "ERROR_DEVICE_IN_USE 裝置被其他程式佔用",
    0x80270000: "COPYENGINE_E_USER_CANCELLED 使用者取消",
    0x802A0001: "E_WPD_DEVICE_ALREADY_OPENED 裝置已被其他程式開啟",
    0x802A0002: "E_WPD_DEVICE_NOT_OPEN 裝置尚未開啟",
    0x802A0006: "E_WPD_DEVICE_IS_HUNG 裝置沒有回應",
}


def describe_hresult(hresult):
    """把 HRESULT 講成人看得懂的話。不認得就照實印出 16 進位。"""
    if hresult is None:
        return "（沒有值）"
    unsigned = hresult & 0xFFFFFFFF
    known = _HRESULT_TEXT.get(unsigned)
    return "0x{:08X}{}".format(unsigned, "（{}）".format(known) if known else "")


class _Sink(DesignatedWrapPolicy):
    """只觀察、不干預的 progress sink。

    ★ 所有 Pre* 一律什麼都不做直接返回（回 None 即 S_OK）。
      從 PreCopyItem 回 S_FALSE 會中止整批 —— 那是災難，不是過濾。
    """

    _com_interfaces_ = [shell.IID_IFileOperationProgressSink]
    _public_methods_ = [
        "StartOperations", "FinishOperations",
        "PreRenameItem", "PostRenameItem",
        "PreMoveItem", "PostMoveItem",
        "PreCopyItem", "PostCopyItem",
        "PreDeleteItem", "PostDeleteItem",
        "PreNewItem", "PostNewItem",
        "UpdateProgress", "ResetTimer", "PauseTimer", "ResumeTimer",
    ]

    def __init__(self, outcome):
        self._outcome = outcome
        self._wrap_(self)

    # ---- 我們真正在意的兩個 ----

    def StartOperations(self):
        try:
            self._outcome.started = True
        except Exception:       # noqa: BLE001 - 例外絕不能逸出 COM callback
            pass

    def FinishOperations(self, hrResult=S_OK):
        try:
            self._outcome.finished = True
            self._outcome.finish_hresult = hrResult
        except Exception:       # noqa: BLE001
            pass

    def PostCopyItem(self, Flags=0, Item=None, DestinationFolder=None,
                     NewName=None, hrCopy=S_OK, NewlyCreated=None):
        """★ 這一行就是整個模組的目的。

        絕不在這裡呼叫 COM（`Item.GetDisplayName()` 在 MTP 上是一次來回），
        也絕不寫 log。`NewName` 是 Shell 免費給的字串，直接用。
        """
        try:
            out = self._outcome
            index = out.seen
            out.seen += 1
            code = (hrCopy or S_OK) & 0xFFFFFFFF
            out.by_hresult[code] += 1
            if code != S_OK:
                if out.first_failure_at is None:
                    out.first_failure_at = index
                if len(out.samples) < MAX_SAMPLES:
                    out.samples.append((index, NewName or "", code))
        except Exception:       # noqa: BLE001
            pass

    # ---- 其餘一律無動作。Pre* 千萬不要回 S_FALSE。 ----

    def PreRenameItem(self, *args):
        return None

    def PostRenameItem(self, *args):
        return None

    def PreMoveItem(self, *args):
        return None

    def PostMoveItem(self, *args):
        return None

    def PreCopyItem(self, *args):
        return None

    def PreDeleteItem(self, *args):
        return None

    def PostDeleteItem(self, *args):
        return None

    def PreNewItem(self, *args):
        return None

    def PostNewItem(self, *args):
        return None

    def UpdateProgress(self, *args):
        return None

    def ResetTimer(self, *args):
        return None

    def PauseTimer(self, *args):
        return None

    def ResumeTimer(self, *args):
        return None


def attach(pfo):
    """把 sink 接到 IFileOperation 上。回傳 (outcome, detach)。

    ★ **失敗不是錯誤。** 任何一步出問題都降級成「沒有逐檔回報」，
      備份照跑，事後的驗證掃描仍然會產生失敗清單。
      診斷能力可以失去，備份能力不行。
    """
    outcome = CopyOutcome()

    def detach():
        return None

    try:
        wrapped = pythoncom.WrapObject(
            _Sink(outcome), shell.IID_IFileOperationProgressSink)
    except Exception as exc:    # noqa: BLE001
        log.warning("建立逐檔回報 sink 失敗，降級成只用驗證掃描：%s", exc)
        return outcome, detach

    try:
        cookie = pfo.Advise(wrapped)
    except Exception as exc:    # noqa: BLE001
        log.warning("Advise 失敗，降級成只用驗證掃描：%s", exc)
        return outcome, detach

    outcome.attached = True
    log.info("逐檔回報已接上（IFileOperationProgressSink）")

    def detach_real():
        if cookie is None:
            return
        try:
            pfo.Unadvise(cookie)
        except Exception as exc:    # noqa: BLE001
            log.debug("Unadvise 失敗（不影響結果）：%s", exc)

    return outcome, detach_real
