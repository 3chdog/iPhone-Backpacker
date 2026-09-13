"""進入點。

負責把 MainWindow 與 ShellWorker 接起來，並管理 worker 執行緒的生命週期。

★ 啟動時**不同步等裝置偵測**（實測總成本約 1.9 秒）。
  視窗先出來，偵測與「本機」列舉都在 worker 執行緒跑，回來再填（決策 D10）。
"""

import logging
import sys
import time

_T0 = time.perf_counter()

from PySide6.QtCore import QThread
from PySide6.QtWidgets import QApplication

from .core.logging_setup import setup_logging
from .ui import dialogs
from .ui.main_window import MainWindow
from .ui.workers import ShellWorker

log = logging.getLogger(__name__)


def _connect(window, worker):
    # 主執行緒 → worker（跨執行緒，Qt 自動用 queued connection）
    window.request_detect.connect(worker.detect_device)
    window.request_roots.connect(worker.load_roots)
    window.request_subfolders.connect(worker.load_subfolders)
    window.request_count.connect(worker.count_files)
    window.request_count_batch.connect(worker.count_files_batch)
    window.request_copy.connect(worker.start_copy)
    window.request_clear_cache.connect(worker.clear_cache)
    window.request_report.connect(worker.build_report)

    # worker → 主執行緒
    worker.device_detected.connect(window.on_device_detected)
    worker.roots_ready.connect(window.populate_roots)
    worker.subfolders_ready.connect(window.on_subfolders_ready)
    worker.subfolders_failed.connect(window.on_subfolders_failed)
    worker.file_count_ready.connect(window.on_file_count_ready)
    worker.file_count_failed.connect(window.on_file_count_failed)
    worker.count_batch_progress.connect(window.on_count_batch_progress)
    worker.copy_progress.connect(window.on_copy_progress)
    worker.copy_finished.connect(window.on_copy_finished)
    worker.copy_failed.connect(window.on_copy_failed)
    worker.report_progress.connect(window.on_report_progress)
    worker.report_ready.connect(window.on_report_ready)
    worker.report_failed.connect(window.on_report_failed)


def _disconnect(window, worker):
    """拆掉 window 與 worker 之間所有的連線。

    不帶參數的 disconnect() 會拆掉該 signal 的**全部**接收者 ——
    這些 signal 只接 worker 一個人，所以是安全的。
    """
    signals = [
        window.request_detect, window.request_roots, window.request_subfolders,
        window.request_count, window.request_count_batch, window.request_copy,
        window.request_clear_cache, window.request_report,
        worker.device_detected, worker.roots_ready, worker.subfolders_ready,
        worker.subfolders_failed, worker.file_count_ready,
        worker.file_count_failed, worker.count_batch_progress,
        worker.copy_progress, worker.copy_finished, worker.copy_failed,
        worker.report_progress, worker.report_ready, worker.report_failed,
    ]
    for signal in signals:
        try:
            signal.disconnect()
        except (RuntimeError, TypeError):
            # 本來就沒接東西。不是問題。
            pass


class _WorkerHost:
    """管理 worker 執行緒的生命週期，並支援**整個重建**。

    ★★ 為什麼需要「重建」（2026-09-13，民眾B 案）：

      裝置連線一旦壞掉，同一個 process 內不會自己恢復 —— 實測 32 分鐘
      都還是壞的，關掉程式重開卻立刻正常。重啟 process 相對於原本那個，
      最明顯的差別就是拿到一個**全新的 COM apartment**。

      所以「重新整理裝置」現在做的是「不用關程式的重啟」：
      結束舊執行緒（CoUninitialize）→ 建新的（CoInitialize）。

    ★ 舊執行緒若卡在一個沒有回應的 COM 呼叫裡，`quit()` 是叫不動它的
      （quit 只是請事件迴圈結束，正在執行的 slot 會繼續跑）。
      這種情況我們**放棄它、另外開一條新的** —— 留著一條卡死的執行緒
      比讓使用者對著凍住的視窗等好，而且卡住的那條正好帶著壞掉的 apartment
      一起離開我們的使用範圍。被放棄的物件要留著參考，
      否則 Qt 會因為「QThread 還在跑就被銷毀」而當掉。
    """

    #: 等舊執行緒收工的上限。閒置時是瞬間完成；卡住時不值得讓使用者多等。
    STOP_TIMEOUT_MS = 2000

    def __init__(self, window):
        self.window = window
        self.thread = None
        self.worker = None
        self._abandoned = []

    def start(self):
        self.thread = QThread()
        self.worker = ShellWorker()
        self.worker.moveToThread(self.thread)
        # CoInitialize 必須在 worker 自己的執行緒裡呼叫，所以掛在 started 上。
        self.thread.started.connect(self.worker.start_up)
        self.thread.finished.connect(self.worker.shut_down)
        _connect(self.window, self.worker)
        # 取消必須立刻生效，不能走 signal/slot（會排在正在執行的任務後面）。
        self.window.set_cancel_hooks(self.worker.request_cancel,
                                     self.worker.request_cancel_count)
        self.thread.start()

    def stop(self):
        if self.thread is None:
            return
        thread, worker = self.thread, self.worker
        self.thread = self.worker = None

        worker.request_cancel()
        worker.request_cancel_count()
        _disconnect(self.window, worker)
        thread.quit()
        if not thread.wait(self.STOP_TIMEOUT_MS):
            log.warning("舊的 worker 執行緒沒有在 %d ms 內結束"
                        "（多半卡在沒有回應的裝置呼叫裡），放棄它並另開一條",
                        self.STOP_TIMEOUT_MS)
            self._abandoned.append((thread, worker))

    def restart(self):
        log.info("★ 重建 worker 執行緒 —— 取得全新的 COM apartment")
        self.stop()
        self.start()

    def shutdown(self):
        if self.worker is not None:
            self.worker.request_cancel()
            self.worker.request_cancel_count()
        if self.thread is not None:
            self.thread.quit()
            self.thread.wait(5000)


def main():
    log_path = setup_logging()

    app = QApplication(sys.argv)
    app.setApplicationName("iPhone Backpacker")

    window = MainWindow()
    host = _WorkerHost(window)
    # 同一條執行緒之間是 direct connection，emit 當下就同步做完 ——
    # _on_refresh 依賴這一點：重建完成之後才輪到後面的 detect / roots。
    window.request_restart_worker.connect(host.restart)

    app.aboutToQuit.connect(host.shutdown)

    host.start()
    window.show()

    # ★★ 啟動耗時一定要在**任何互動之前**量完（2026-09-14 修）。
    #   實測回報：首次啟動會跳出使用說明，而它是 modal 的；舊版把量測放在
    #   它後面，於是 log 寫出「我們的初始化 39556 ms」—— 那 39 秒其實是
    #   使用者在讀說明。每一份首次使用者的災情回報都會被這個數字誤導。
    import_ms = getattr(sys.modules["__main__"], "_IMPORT_MS", None)
    if import_ms is not None:
        # 實測（2026-08-27）：import 約 2100~2400 ms，之後的初始化只有約 80 ms。
        # 也就是使用者感受到的啟動時間**幾乎全部**是 Python 直譯器啟動 +
        # PySide6 import，發生在我們任何程式碼之前，開發模式下無法改善。
        # PyInstaller --onedir 打包後會明顯變快（階段 6）。
        total_ms = (time.perf_counter() - _T0) * 1000
        log.info("啟動耗時：import %.0f ms + 我們的初始化 %.0f ms（合計 %.0f ms）",
                 import_ms, max(total_ms - import_ms, 0.0), total_ms)
    log.info("啟動完成，log 檔：%s", log_path)

    # 首次啟動的引導。放在 show() 之後 —— 主視窗要先出現在後面，
    # 使用者關掉說明就能直接操作。
    # ★ 這是 modal 的，會一直卡到使用者關掉為止，所以務必在量測之後。
    dialogs.maybe_show_guide(window)

    # 視窗已經顯示之後才發請求 —— 使用者看到的是「立刻開啟」而不是「卡兩秒」。
    window.request_detect.emit()
    window.request_roots.emit()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
