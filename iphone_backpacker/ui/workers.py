"""背景執行緒。

★★ 為什麼所有 Shell 操作都必須在這裡（決策 D10）：
  實測裝置偵測總成本約 1.9 秒、展開 Internal Storage（184 個資料夾）
  首次要 2.6 秒。這些在主執行緒跑會直接凍住視窗。

★ COM 的規矩（見 docs/ai/04-shell-com-notes.md）：
  - 每個碰 Shell 的執行緒開頭要 CoInitialize()、結束要 CoUninitialize()
  - IShellFolder / IShellItem **不能跨執行緒傳遞**
  - PIDL 是純資料，**可以**跨執行緒傳遞
  所以主執行緒與 worker 之間只傳 PIDL 與 FileEntry，絕不傳 COM 介面。

★ 只用**一條** worker 執行緒，任務序列執行。
  MTP 本來就不適合並行存取，多開執行緒不會比較快，只會讓錯誤更難查。
"""

import logging
import threading

import pythoncom
from PySide6.QtCore import QObject, Signal, Slot

from ..core import copier, device, diagnostics, listing, shell_ns
from ..core.errors import BackpackerError

log = logging.getLogger(__name__)


class ShellWorker(QObject):
    """所有 Shell 操作的執行者。由 MainWindow 移到獨立的 QThread 裡。"""

    # 裝置偵測
    device_detected = Signal(object)                # core.device.Detection

    # 樹狀展開
    roots_ready = Signal(object)                    # list[FileEntry]（「本機」底下）
    subfolders_ready = Signal(object, object)       # parent_pidl, list[FileEntry]
    subfolders_failed = Signal(object, str)         # parent_pidl, message

    # 資料夾摘要（背景算，永不阻塞選取）
    file_count_ready = Signal(object, int)          # folder_pidl, count
    file_count_failed = Signal(object, str)         # folder_pidl, 錯誤訊息
    count_batch_progress = Signal(int, int)         # 已完成, 總數

    # 診斷報告
    report_progress = Signal(str)                   # 目前在跑哪一段
    report_ready = Signal(str)                      # 報告檔的完整路徑
    report_failed = Signal(str)

    # 複製
    copy_progress = Signal(object)                  # CopyProgress
    copy_finished = Signal(object)                  # CopyReport
    copy_failed = Signal(str)

    def __init__(self):
        super().__init__()
        self.cache = listing.NamespaceCache()
        self._cancel = threading.Event()
        self._cancel_count = threading.Event()

    # ---- 執行緒生命週期 ----

    @Slot()
    def start_up(self):
        pythoncom.CoInitialize()
        log.info("worker 執行緒已進入 COM apartment")
        # 啟動就檢查一次我們依賴的 Shell API 是否存在。
        # 「某個 API 其實不存在」曾經偽裝成裝置行為騙過我們一次。
        shell_ns.log_api_report()

    @Slot()
    def shut_down(self):
        pythoncom.CoUninitialize()
        log.info("worker 執行緒已離開 COM apartment")

    # ★ 這兩個是**唯一**可以由主執行緒直接呼叫的方法。
    #   threading.Event 本身是執行緒安全的，而且必須立刻生效 ——
    #   走 signal/slot 的話會排在正在執行的任務後面，等於沒有取消功能。

    def request_cancel(self):
        self._cancel.set()

    def request_cancel_count(self):
        self._cancel_count.set()

    # ---- 任務 ----

    @Slot()
    def detect_device(self):
        try:
            detection = device.detect()
        except BackpackerError as exc:
            log.exception("裝置偵測失敗")
            detection = self._detection_after_failure(exc)
        self.device_detected.emit(detection)

    @staticmethod
    def _detection_after_failure(exc):
        """偵測炸掉之後，盡量別給出誤導的結論。

        ★ 舊版一律回 NOT_FOUND，於是畫面顯示「沒有自動偵測到 iPhone，
          請確認 USB 線接好了、手機已解鎖」—— 但 2026-09-12 16:49 那次
          明明**已經找到 iPhone 而且確認了 WPD 證據**，只是 probe 拿到
          0x8007001E。把使用者推去檢查早就沒問題的線材和解鎖狀態，
          比什麼都不說還糟。

          所以這裡再試一次「只列候選、不 probe」：列得出來就是 READ_FAILED
          （找到了但讀不到），真的列不出來才是 NOT_FOUND。
        """
        log.error("%s", exc)
        try:
            candidates = tuple(device.find_portable_devices())
        except BackpackerError as inner:
            log.warning("連候選清單都列不出來：%s", inner)
            return device.Detection(device.DeviceStatus.NOT_FOUND)

        if not candidates:
            return device.Detection(device.DeviceStatus.NOT_FOUND)

        confirmed = [d for d in candidates
                     if d.confidence is device.Confidence.CONFIRMED]
        # 只有在「剛好一台確定是可攜式裝置」時才敢指名道姓（決策 D17）。
        named = confirmed[0] if len(confirmed) == 1 else None
        log.warning("裝置在（%d 個候選），但讀不到內容", len(candidates))
        return device.Detection(device.DeviceStatus.READ_FAILED, named, candidates)

    @Slot()
    def load_roots(self):
        """載入「本機」底下的節點。

        磁碟機與 iPhone 都在這一層，所以樹只要一個根就同時涵蓋兩者，
        不需要為裝置另外做一棵樹。

        實測這一步要 300~800 ms（跟有沒有插 iPhone 無關，是本機列舉
        本身的成本），所以一定要在這裡而不是主執行緒。
        """
        try:
            entries = listing.list_subfolders(shell_ns.this_pc_pidl(), self.cache)
        except BackpackerError as exc:
            log.exception("載入「本機」失敗")
            self.subfolders_failed.emit(None, str(exc))
            return
        self.roots_ready.emit(entries)

    @Slot(object)
    def load_subfolders(self, parent_pidl):
        """展開一層。★ 只列資料夾，不取 details、不取縮圖。"""
        try:
            entries = listing.list_subfolders(parent_pidl, self.cache)
        except BackpackerError as exc:
            log.warning("展開失敗：%s", exc)
            self.subfolders_failed.emit(parent_pidl, str(exc))
            return
        self.subfolders_ready.emit(parent_pidl, entries)

    @Slot(object, object)
    def count_files(self, folder_pidl, categories):
        """算單一資料夾裡的檔案數。

        ★ 這是**慢**操作（~3.4 ms/項），只在使用者停在某個節點時才做，
          而且算不完也不影響勾選與備份（效能契約 D8）。
        """
        count = self._count_one(folder_pidl, categories)
        if count is not None:
            self.file_count_ready.emit(folder_pidl, count)

    @Slot(object, object)
    def count_files_batch(self, folder_pidls, categories):
        """一次算多個資料夾。

        使用者的 iPhone 有 184 個日期資料夾，一個一個點著等太慢；
        框選一批再一次算才符合實際用法。

        序列執行 —— MTP 不適合並行存取。每算完一個就 emit，
        使用者會看到數字一個一個填上去，而不是等到全部算完。
        """
        self._cancel_count.clear()
        total = len(folder_pidls)
        log.info("開始批次計算 %d 個資料夾的檔案數", total)
        for index, pidl in enumerate(folder_pidls, 1):
            if self._cancel_count.is_set():
                log.info("批次計算已取消（完成 %d / %d）", index - 1, total)
                break
            count = self._count_one(pidl, categories)
            if count is not None:
                self.file_count_ready.emit(pidl, count)
            self.count_batch_progress.emit(index, total)
        else:
            log.info("批次計算完成")

    def _count_one(self, folder_pidl, categories):
        """算一個資料夾的檔案數。讀不到時回 None 並發出 file_count_failed。

        ★ 讀取失敗**絕對不能顯示成 0**。使用者看到 0 會以為那個資料夾是空的，
          於是不去備份它 —— 但實際上是我們沒讀到。這正是災情回報裡
          「最新的 202608_a 顯示 0 個檔案」的可疑之處。
        """
        try:
            return sum(1 for _ in listing.iter_files(folder_pidl, categories))
        except BackpackerError as exc:
            log.warning("計算檔案數失敗：%s", exc)
            self.file_count_failed.emit(folder_pidl, str(exc))
            return None

    @Slot(object, str, object, int)
    def start_copy(self, sources, dest_dir, categories, owner_hwnd):
        self._cancel.clear()
        try:
            plan = copier.plan_copy(sources, dest_dir)
            report = copier.run_copy(
                plan, categories,
                owner_hwnd=owner_hwnd or None,
                progress=self.copy_progress.emit,
                cancel=self._cancel.is_set,
            )
        except BackpackerError as exc:
            log.exception("備份失敗")
            self.copy_failed.emit(str(exc))
            return
        self.copy_finished.emit(report)

    @Slot(object)
    def build_report(self, focus_folder):
        """產生診斷報告並存成 .txt。

        ★ 這件事一定要在 worker 執行緒做 —— 它要碰 Shell COM，
          而且會跑好幾秒（要列舉「本機」與裝置的資料夾）。

        focus_folder 是使用者目前選取的資料夾，可以是 None。
        """
        try:
            text = diagnostics.collect_report(
                focus_folder=focus_folder,
                progress=self.report_progress.emit,
            )
            path = diagnostics.write_report(text)
        except Exception as exc:   # noqa: BLE001
            # 診斷報告是「出問題的時候」用的，所以它自己絕對不能因為
            # 未預期的例外而失敗得無聲無息。這裡刻意攔下所有例外。
            log.exception("產生診斷報告失敗")
            self.report_failed.emit(str(exc))
            return
        self.report_ready.emit(str(path))

    @Slot()
    def clear_cache(self):
        """重新整理裝置時呼叫。MTP 偶爾回傳不完整清單，重列一次就好。"""
        self.cache.invalidate()
        log.info("命名空間快取已清空")
