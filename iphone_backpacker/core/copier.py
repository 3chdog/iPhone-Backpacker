"""複製作業。

★ 排程單位永遠是「檔案」，永遠不是「資料夾」（決策 D9）。
  整包丟給 shell 遞迴雖然快一點，但失敗時完全拿不到「是哪個檔案失敗」——
  那正是使用者用檔案總管複製整個資料夾時遇到的「不穩定」。
  逐檔排程才換得到失敗清單、重試能力與增量去重。

★★ 一整個備份任務 = 一次 IFileOperation（決策 D12，2026-08-27 修訂）。
  使用者實測過：用 copyShellItem() 逐檔各開一次操作「超級慢」，
  而且 **Windows 的原生進度視窗會反覆彈出**。切成小批次是同一個問題的
  縮小版 —— 每次 PerformOperations() 有約 600 ms 固定開銷，一個 5000 張
  的資料夾切 200 一批就是 25 次視窗 + 15 秒純浪費。

  最初改成「一個資料夾一次操作」，但實測選 5 個資料夾就跳 5 次視窗。
  IFileOperation 允許同一次操作裡每個項目有**不同的目的地**，
  所以整個任務不管幾個資料夾都只需要一次 PerformOperations()。

  流程：
    第 1 段 走過所有來源資料夾，列舉 + 過濾 + 去重
            → 這段由我們自己回報進度（「已找到 N 個」）
            → ★ 單一資料夾讀不到只影響它自己，其餘照備份
    第 2 段 把全部檔案排程進**單一** IFileOperation（每個檔案帶自己的目的地）
            → 原生進度視窗只出現一次
            → ★ 掛上 IFileOperationProgressSink 取得**逐檔 HRESULT**（D19）
    第 3 段 逐資料夾驗證掃描（比對檔名**與大小**）→ 失敗清單

  第 2 段不需要我們畫進度：IFileOperation 的原生視窗本來就有
  逐檔進度、剩餘時間與取消鈕，品質比自己畫的高。

★★ 第 2 段的 sink 與第 3 段的驗證掃描是**兩個獨立的證據來源**，刻意都留著：
  sink 說「Shell 認為這個檔案複製成功了」，驗證掃描說「檔案真的在目的地、
  而且不是 0 byte」。兩者不一致本身就是最有價值的訊息。
"""

import logging
import os
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Dict, List

import pythoncom
from win32com.shell import shell, shellcon

from . import copysink, shell_ns
from .errors import (BackpackerError, DestinationError, OperationCancelled,
                     ShellError)
from .filters import MEDIA, describe
from .listing import FileEntry, iter_files
from .naming import safe_folder_name

log = logging.getLogger(__name__)

# FOF_NOCONFIRMATION  所有確認對話框一律回「全部是」
# FOF_NOERRORUI       ★ 關掉「這個檔案無法複製，是否略過？」小視窗，
#                       失敗項目靜默跳過，改由事後的驗證掃描產生失敗清單。
#                       這正是使用者回報「略過後沒有任何 log」的解法。
# 刻意不加 FOF_SILENT —— 那會關掉 Windows 原生的複製進度視窗，
# 而那個視窗的品質比我們自己畫的高。
_OPERATION_FLAGS = shellcon.FOF_NOCONFIRMATION | shellcon.FOF_NOERRORUI

# PerformOperations() 在使用者按下取消（或關掉進度視窗）時回這個 HRESULT。
# 0x80270000 = COPYENGINE_E_USER_CANCELLED。
# ★ 這是正常結果，不是錯誤 —— 不能讓它變成 traceback 嚇到使用者。
COPYENGINE_E_USER_CANCELLED = -2144927744


class CopyPhase(Enum):
    LISTING = auto()    # 正在列舉來源檔案（我們自己回報進度）
    COPYING = auto()    # 交給 IFileOperation，原生進度視窗接手
    VERIFYING = auto()  # 驗證掃描


@dataclass
class CopyProgress:
    """回報給 UI 的進度快照。"""

    phase: CopyPhase = CopyPhase.LISTING
    current_folder: str = ""
    listed: int = 0             # 列舉階段已找到、待複製的檔案數
    copied: int = 0             # 驗證確認已落地的檔案數
    skipped_existing: int = 0   # 增量備份跳過的
    failed: int = 0


@dataclass
class CopyPlan:
    """複製計畫。

    ★ 只記「被勾選的資料夾」。檔案清單要到 run_copy() 執行時才展開，
      這樣使用者按下備份之前完全不需要等待列舉（效能契約 D8）。
    """

    dest_dir: Path
    sources: List[FileEntry]

    def __post_init__(self):
        self.dest_dir = Path(self.dest_dir)


@dataclass
class CopyReport:
    copied: List[str] = field(default_factory=list)
    failed: List[str] = field(default_factory=list)
    skipped_existing: List[str] = field(default_factory=list)
    cancelled: List[str] = field(default_factory=list)
    aborted: bool = False
    #: 來源一個檔案都讀不到（而且目的地也沒有既有檔案可以跳過）。
    #: 這通常代表裝置連線出問題，不是「已經備份好了」。
    source_empty: bool = False

    #: ★ 讀不到、被整個略過的來源資料夾：[(名稱, 錯誤訊息), ...]
    #:   舊版任何一個資料夾列舉失敗會讓**整個備份任務**陣亡
    #:   （run_copy 只 catch OperationCancelled），前面列好的全部作廢。
    #:   現在一個資料夾壞掉只影響它自己，其餘照備份。
    unreadable: List[tuple] = field(default_factory=list)

    #: ★ 回報 0 個檔案、但那個 0 很可疑的來源資料夾（慢速 0、重驗結果不一致…）。
    #:   這些**不算成功**，要明確告訴使用者「這幾個沒讀到，不是沒東西」。
    suspicious_empty: List[str] = field(default_factory=list)

    #: ★ 檔案有落地但大小是 0 —— 名稱在、內容不在。
    #:   舊版只比對檔名，這種殘缺檔會被算成「複製成功」。
    zero_byte: List[str] = field(default_factory=list)

    #: `GetAnyOperationsAborted()` 的值。微軟文件：操作可能被使用者中止，
    #: **也可能被系統靜默中止**。舊版算出來就丟掉，於是 8/30 那次 3649 全失敗
    #: 到今天都還不知道 Shell 到底有沒有自認中止過。
    any_aborted: bool = False

    #: 逐檔 HRESULT 的彙整（core.copysink.CopyOutcome），可能是 None。
    outcome: object = None

    #: 每個來源資料夾的列舉可信度，給診斷報告用：[(名稱, 說明), ...]
    enum_notes: List[tuple] = field(default_factory=list)

    @property
    def ok(self):
        return not (self.failed or self.aborted or self.unreadable
                    or self.suspicious_empty or self.zero_byte)

    def summary(self):
        parts = ["複製 {} 個".format(len(self.copied))]
        if self.skipped_existing:
            parts.append("跳過 {} 個（已存在）".format(len(self.skipped_existing)))
        if self.failed:
            parts.append("失敗 {} 個".format(len(self.failed)))
        if self.zero_byte:
            parts.append("檔案是空的 {} 個".format(len(self.zero_byte)))
        if self.unreadable:
            parts.append("讀不到的資料夾 {} 個".format(len(self.unreadable)))
        if self.suspicious_empty:
            parts.append("可疑的空資料夾 {} 個".format(len(self.suspicious_empty)))
        if self.source_empty:
            parts.append("來源讀不到任何檔案")
        if self.cancelled:
            # ★ 取消時「還沒輪到的檔案」不是失敗，要分開講。
            #   混在一起會讓使用者看到「失敗 243 個」而以為出大事。
            parts.append("尚未複製 {} 個（已取消）".format(len(self.cancelled)))
        return "、".join(parts)


def plan_copy(sources, dest_dir):
    """建立計畫。只做便宜的事：檢查目的地。

    ★ 刻意不在這裡展開來源檔案清單 —— 那是 run_copy() 在背景執行緒才做的事。
    """
    dest_dir = Path(dest_dir)
    if dest_dir.exists() and not dest_dir.is_dir():
        raise DestinationError("目的地不是資料夾：{}".format(dest_dir))
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise DestinationError("無法建立目的地「{}」：{}".format(dest_dir, exc)) from exc
    if not os.access(dest_dir, os.W_OK):
        raise DestinationError("目的地無法寫入：{}".format(dest_dir))

    sources = [s for s in sources if s.is_dir]
    log.info("計畫：%d 個來源資料夾 → %s", len(sources), dest_dir)
    return CopyPlan(dest_dir=dest_dir, sources=sources)


def _local_index(directory):
    """把本機資料夾建成 {小寫檔名: 大小} 索引。

    目的地是真實檔案系統，用 os.scandir 很便宜 ——
    跟在 MTP 上取來源大小的成本完全不是一個量級。
    """
    index: Dict[str, int] = {}
    if not directory.is_dir():
        return index
    with os.scandir(directory) as it:
        for entry in it:
            if entry.is_file():
                try:
                    index[entry.name.lower()] = entry.stat().st_size
                except OSError:
                    index[entry.name.lower()] = -1
    return index


def _new_operation(owner_hwnd):
    try:
        pfo = pythoncom.CoCreateInstance(
            shell.CLSID_FileOperation, None,
            pythoncom.CLSCTX_ALL, shell.IID_IFileOperation,
        )
    except pythoncom.com_error as exc:
        raise ShellError("無法建立 IFileOperation：{}".format(exc)) from exc

    pfo.SetOperationFlags(_OPERATION_FLAGS)
    if owner_hwnd:
        # 把 Windows 原生進度視窗掛在主視窗底下，否則它會變成孤兒視窗。
        pfo.SetOwnerWindow(owner_hwnd)
    return pfo


def _perform(pfo):
    """執行已排程的操作。回傳 (使用者取消, 有項目被中止)。

    ★★ 這兩者必須分開，實測證據（2026-08-28）：

      情況 A：使用者按取消 / 關掉進度視窗
        → PerformOperations() **拋** COPYENGINE_E_USER_CANCELLED
        → log：「開始複製：105 個」…「使用者取消了複製」
                「複製 58 個、尚未複製 47 個」
        沒複製到的是「還沒輪到」。

      情況 B：有檔案複製不了，被 FOF_NOERRORUI 靜默略過
        → PerformOperations() **正常返回**，但 GetAnyOperationsAborted() 為真
        → log：「開始複製：160 個」…「複製 158 個、尚未複製 2 個」
                （**前面沒有取消那一行**）
        那 2 個是**真的失敗**，正是使用者最初回報的
        「有些檔案 Windows 無法複製，略過後沒有任何 log」。

      只看 GetAnyOperationsAborted() 會把 B 誤判成 A，
      等於把真正的失敗藏進「已取消」裡 —— 那是本專案存在的理由之一。
    """
    try:
        pfo.PerformOperations()
    except pythoncom.com_error as exc:
        hresult = exc.args[0] if exc.args else None
        if hresult == COPYENGINE_E_USER_CANCELLED:
            log.info("使用者取消了複製（COPYENGINE_E_USER_CANCELLED）")
            return True, True
        raise ShellError("複製作業失敗：{}".format(exc)) from exc

    any_aborted = bool(pfo.GetAnyOperationsAborted())
    if any_aborted:
        log.info("PerformOperations 正常返回但 GetAnyOperationsAborted 為真"
                 " —— 有項目被略過（多半是檔案本身有問題）")
    return False, any_aborted


def run_copy(plan, categories=MEDIA, *, owner_hwnd=None,
             progress=None, cancel=None):
    """執行備份。**必須在 worker thread 且已進入 COM apartment。**

    progress: 可選的 callable，收一個 CopyProgress。
    cancel:   可選的 callable，回傳 True 表示使用者要求取消。
              只在「列舉」階段有效；一旦進入 PerformOperations()，
              取消要靠原生進度視窗上的按鈕。
    """
    report = CopyReport()
    state = CopyProgress()

    def notify():
        if progress is not None:
            progress(state)

    def check_cancel():
        if cancel is not None and cancel():
            raise OperationCancelled("使用者取消備份")

    log.info("開始備份：%d 個資料夾 → %s（%s）",
             len(plan.sources), plan.dest_dir, describe(categories))

    # ---- 第 1 段：列舉 + 過濾 + 增量去重 ----
    # 這段是我們自己回報進度的地方。MTP 列舉每項約 3.4 ms（跨 session 浮動
    # 可達 3 倍），幾百張就要好幾秒，不回報進度使用者會以為當掉。
    state.phase = CopyPhase.LISTING
    jobs = []   # [(source, dest_sub, [FileEntry, ...]), ...]

    try:
        for source in plan.sources:
            check_cancel()
            dest_sub = plan.dest_dir / safe_folder_name(source.name)

            state.current_folder = source.name
            notify()

            # ★ 先不要 mkdir。確認這個資料夾真的有東西要複製才建 ——
            #   8/30 那次在使用者桌面留下 13 個空資料夾，
            #   看起來像「備份了一部分」，其實一個檔案都沒有。
            existing = _local_index(dest_sub)
            enum_report = shell_ns.EnumReport()
            pending = []
            skipped_here = 0
            try:
                for entry in iter_files(source.abs_pidl, categories,
                                        report=enum_report):
                    check_cancel()
                    # 增量去重以檔名為準，刻意不比對來源大小 ——
                    # 取來源大小在 MTP 上要每個項目一次 GetDetailsOf 來回，
                    # 成本會抵銷掉增量備份省下的時間。
                    # iPhone 的 IMG_xxxx 檔名在同一資料夾內本來就唯一。
                    #
                    # ★ 但目的地大小要看：既有檔案是 0 byte 就**不算已存在**，
                    #   否則一個殘缺檔會永遠擋住自己的重試。
                    if existing.get(entry.key):
                        report.skipped_existing.append(entry.name)
                        skipped_here += 1
                        state.skipped_existing = len(report.skipped_existing)
                        continue
                    pending.append(entry)
                    state.listed += 1
                    if state.listed % 50 == 0:
                        notify()
            except OperationCancelled:
                raise
            except BackpackerError as exc:
                # ★★ 一個資料夾讀不到，不該讓整批備份陣亡（2026-09-13）。
                #   舊版這裡只 catch OperationCancelled，於是 ShellError 會一路
                #   逸出 run_copy → copy_failed，前面所有資料夾列好的清單全部作廢，
                #   而使用者只看到一句「備份失敗」。
                log.warning("「%s」讀取失敗，略過這個資料夾，其餘繼續：%s",
                            source.name, exc)
                report.unreadable.append((source.name, str(exc)))
                continue

            report.enum_notes.append((source.name, enum_report.describe()))
            # ★ 「跳過」以前印的是 len(existing)（整個目的地資料夾的檔案數），
            #   那在目的地有舊檔案時會虛報。改成這一輪真的跳過的數量。
            log.info("「%s」：待複製 %d 個、跳過 %d 個（列舉：%s）",
                     source.name, len(pending), skipped_here,
                     enum_report.describe())

            if pending:
                try:
                    dest_sub.mkdir(parents=True, exist_ok=True)
                except OSError as exc:
                    raise DestinationError(
                        "無法建立「{}」：{}".format(dest_sub, exc)) from exc
                jobs.append((source, dest_sub, pending))
            elif enum_report.suspicious_zero:
                # ★ 0 個檔案，而且那個 0 不可信 —— 絕不能當成「沒東西要備份」。
                log.warning("「%s」回報 0 個檔案，但那個 0 很可疑：%s",
                            source.name, enum_report.describe())
                report.suspicious_empty.append(source.name)
        notify()
    except OperationCancelled:
        report.aborted = True
        log.info("備份在列舉階段被取消")
        return report

    if not jobs:
        # ★ 這裡以前一律寫「全部已存在」，但那在 skipped 也是 0 的時候是**錯的**。
        #   實測（2026-08-30，民眾B）：13 個資料夾全部「待複製 0 個、跳過 0 個」，
        #   而程式回報「全部已存在」—— 事實上目的地什麼都沒有，是來源讀不到。
        #   把情況分開講，不要讓使用者以為備份成功了。
        if report.unreadable or report.suspicious_empty:
            log.warning("沒有任何檔案可以複製，而且有 %d 個資料夾讀不到、"
                        "%d 個資料夾回報了可疑的 0 —— 這不是「已經備份好了」",
                        len(report.unreadable), len(report.suspicious_empty))
        elif report.skipped_existing:
            log.info("沒有需要複製的檔案（%d 個全部已存在）",
                     len(report.skipped_existing))
        else:
            log.warning("來源資料夾裡讀不到任何符合條件的檔案 —— "
                        "如果你確定裡面有照片，很可能是裝置連線異常，"
                        "請把 USB 線拔掉重插後再試一次")
            report.source_empty = True
        return report

    # ---- 第 2 段：全部排程進單一 IFileOperation ----
    # ★ 每個 CopyItem 可以帶自己的目的地，所以不管幾個資料夾都只要一次操作，
    #   原生進度視窗只彈一次（決策 D12）。
    state.phase = CopyPhase.COPYING
    state.current_folder = "全部"
    notify()

    pfo = _new_operation(owner_hwnd)
    scheduled = 0
    for source, dest_sub, pending in jobs:
        dest_item = shell_ns.item_from_path(dest_sub)
        for entry in pending:
            try:
                pfo.CopyItem(shell_ns.shell_item(entry.abs_pidl), dest_item, None)
                scheduled += 1
            except (pythoncom.com_error, ShellError) as exc:
                # 排程階段就失敗的個別項目不該拖垮整批，
                # 交給第 3 段的驗證掃描去記錄。
                log.warning("排程失敗，略過：%s\\%s（%s）",
                            source.name, entry.name, exc)

    if scheduled == 0:
        # PerformOperations() 在零排程時會回 0x8000FFFF (E_UNEXPECTED)，
        # 也就是舊版使用者遇到的 -2147418113「災難性的失敗」。
        log.warning("沒有任何項目排程成功")
        report.failed.extend(e.name for _, _, p in jobs for e in p)
        return report

    # ★ 接上逐檔回報（決策 D19，推翻 D6）。接不上就降級，備份照跑。
    outcome, detach = copysink.attach(pfo)
    report.outcome = outcome

    log.info("開始複製：%d 個檔案，來自 %d 個資料夾", scheduled, len(jobs))
    try:
        user_cancelled, any_aborted = _perform(pfo)
    finally:
        detach()

    # ★ 這個值以前算出來就丟掉。微軟文件寫得很清楚：操作可能被使用者中止，
    #   **也可能被系統靜默中止**。8/30 那次 3649 全失敗到今天還不知道
    #   Shell 有沒有自認中止過 —— 就是因為這一行沒有被留下來。
    report.any_aborted = any_aborted

    if outcome.attached:
        log.info("逐檔回報結果：\n%s", outcome.describe())
    else:
        log.info("這次沒有逐檔回報，失敗清單只能靠驗證掃描產生")

    # ---- 第 3 段：逐資料夾驗證掃描 ----
    # 取代 IFileOperationProgressSink（決策 D6）。搭配 FOF_NOERRORUI，
    # 失敗的檔案不會跳「是否略過」小視窗，改由這裡比對出來，
    # 使用者才拿得到失敗清單。
    state.phase = CopyPhase.VERIFYING
    notify()
    for source, dest_sub, pending in jobs:
        landed = _local_index(dest_sub)
        for entry in pending:
            label = "{}\\{}".format(source.name, entry.name)
            size = landed.get(entry.key)
            if size == 0:
                # ★ 名稱在、內容不在。舊版只比對檔名，這種殘缺檔會被算成成功，
                #   而且因為去重也只看檔名，下次備份還會直接跳過它 ——
                #   等於使用者永遠拿不回那張照片卻以為備份好了。
                report.zero_byte.append(label)
                report.failed.append(label)
            elif size is not None:
                report.copied.append(entry.name)
            elif user_cancelled:
                # 使用者主動取消：沒落地的多半是「還沒輪到」，不是失敗。
                # 我們無法從 IFileOperation 得知取消當下處理到第幾個，
                # 一律當成還沒輪到 —— 這些檔案下次備份會自動重試。
                report.cancelled.append(label)
            else:
                # 沒有取消卻沒落地 = 真的失敗（被 FOF_NOERRORUI 靜默略過）。
                report.failed.append(label)
    state.copied = len(report.copied)
    state.failed = len(report.failed)
    report.aborted = user_cancelled
    notify()

    for label in report.failed:
        log.warning("複製失敗：%s", label)
    for name, message in report.unreadable:
        log.warning("整個資料夾讀不到，沒有備份到：%s（%s）", name, message)
    for name in report.suspicious_empty:
        log.warning("資料夾回報 0 個檔案但不可信，沒有備份到：%s", name)

    log.info("%s：%s", "備份已取消" if user_cancelled else "備份完成",
             report.summary())
    return report
