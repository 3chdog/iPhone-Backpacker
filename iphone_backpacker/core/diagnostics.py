"""產生診斷報告。

★ 為什麼要有這個：會遇到問題的人，正是最不可能開終端機跑 Python 的人。
  報告必須能從 GUI 按一個按鈕產生，存成一個他們找得到、看得懂、
  可以直接傳給開發者的 .txt 檔。

★ 這個模組沒有 Qt，GUI 與命令列工具共用同一份邏輯。

★ **每一段都獨立包在 try/except 裡。** 報告的目的就是在出問題的時候用，
  所以任何一段失敗都不能讓整份報告產不出來 —— 失敗本身就是有價值的資訊，
  要寫進報告而不是讓它中斷。
"""

import logging
import platform
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

from win32com.shell import shell, shellcon

from . import copysink, device, listing, shell_ns, wpd_probe
from .filters import MEDIA, categorize, extension_of
from .logging_setup import default_log_dir

log = logging.getLogger(__name__)

LOG_TAIL_LINES = 300


def default_report_dir():
    """報告要存在使用者一定找得到的地方 —— 桌面。

    用 CSIDL_DESKTOPDIRECTORY 而不是 `Path.home() / "Desktop"`，
    因為桌面可能被 OneDrive 或群組原則重新導向到別的位置。
    """
    try:
        return Path(shell.SHGetFolderPath(0, shellcon.CSIDL_DESKTOPDIRECTORY, 0, 0))
    except Exception:   # noqa: BLE001 - 取不到就退回家目錄，不能因此產不出報告
        return Path.home()


def default_report_path():
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return default_report_dir() / "iPhoneBackpacker-診斷報告-{}.txt".format(stamp)


class _Report:
    """收集報告內容。每一段都不會因為例外而中斷整份報告。"""

    def __init__(self, progress=None):
        self.lines = []
        self._progress = progress

    def say(self, text=""):
        self.lines.append(text)

    def title(self, text):
        self.say()
        self.say("=" * 64)
        self.say(text)
        self.say("=" * 64)
        if self._progress is not None:
            self._progress(text)

    def section(self, heading, func):
        """跑一段檢查。失敗就把錯誤寫進報告，繼續下一段。"""
        self.title(heading)
        try:
            func()
        except Exception as exc:   # noqa: BLE001 - 報告本來就是給出問題時用的
            self.say("!! 這一段檢查失敗：{}".format(exc))
            self.say("!! （這件事本身就是線索，請連同這份報告一起回報）")
            log.exception("診斷段落失敗：%s", heading)

    def text(self):
        return "\n".join(self.lines) + "\n"


def _describe_environment(report):
    from .. import __version__

    report.say("產生時間：{}".format(datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    report.say("程式版本：{}".format(__version__))
    report.say("執行方式：{}".format(
        "打包後的 exe" if getattr(sys, "frozen", False) else "從原始碼執行"))
    report.say("作業系統：{}".format(platform.platform()))
    report.say("Python  ：{}".format(sys.version.replace("\n", " ")))


def _describe_this_pc(report):
    """「本機」底下有什麼，以及裝置判斷怎麼決定的。"""
    report.say("這一段用來回答：為什麼顯示「沒有偵測到 iPhone」。")
    report.say()

    this_pc = shell_ns.this_pc_pidl()
    enum_report = shell_ns.EnumReport()
    rows = []
    for _child_abs, name, attrs, parsing in shell_ns.iter_entries(
        this_pc, flags=shell_ns.EVERYTHING,
        want_attributes=True, want_parsing=True, report=enum_report,
    ):
        rows.append(name)
        confidence, reason = device._classify(parsing, attrs)
        marks = {
            device.Confidence.CONFIRMED: "★ 確定是可攜式裝置",
            device.Confidence.LIKELY: "？ 可能是裝置，也可能是其他軟體掛載的",
            device.Confidence.EXCLUDED: "－ 不是裝置",
        }
        report.say("  {}".format(name))
        report.say("      attrs   = {}".format(shell_ns.describe_attributes(attrs)))
        report.say("      parsing = {}".format(
            "None（讀不到）" if parsing is None else (parsing or "（空字串）")))
        report.say("      判定    = {}（{}）".format(marks[confidence], reason))

    # ★ 這一層的列舉品質要記下來。民眾B 15:55 那次 Apple iPhone 從候選裡
    #   整個消失，其中一個假設就是這裡被靜默截斷了 —— 沒有這行永遠證不了。
    report.say()
    report.say("這一層的列舉：{}".format(enum_report.describe()))
    report.say("共 {} 個節點：{}".format(len(rows), "、".join(rows)))


def _describe_api(report):
    """檢查我們依賴的 Shell API 是不是真的存在。

    ★ 這一段是被慘痛教訓逼出來的：`shell.SHBindToParent` 在 pywin32 裡
      根本不存在，於是取解析名稱**每次都失敗**，而那個失敗被誤讀成
      「MTP 裝置的特徵」，導致「本機」底下每個節點都被判成 iPhone。
      主動檢查一遍，這類錯誤就不會再偽裝成裝置行為。
    """
    missing = []
    for label, present, required in shell_ns.api_report():
        report.say("  {:<44} {}{}".format(
            label, "有" if present else "沒有", "" if required else "（選配）"))
        if required and not present:
            missing.append(label)
    report.say()
    if missing:
        report.say("!! 缺少必要的 Shell API：{}".format("、".join(missing)))
        report.say("!! 這幾乎一定是問題的根源，請務必回報。")
    else:
        report.say("必要的 API 都在。")


def _describe_device(report):
    """★ 這裡呼叫 detect()，跟程式實際使用的邏輯完全一樣。

    以前是自己拿 find_portable_devices()[0]，跟 detect() 的挑選規則不同 ——
    報告顯示的裝置可能根本不是程式真正在用的那一個，等於報告在騙人。
    """
    detection = device.detect()

    report.say("候選裝置（{} 個）：".format(len(detection.candidates)))
    for candidate in detection.candidates:
        report.say("  {}  [{}]".format(candidate.name, candidate.confidence.name))
        report.say("      {}".format(candidate.reason))
        report.say("      parsing = {}".format(
            candidate.parsing_name or "（取不到）"))
    if not detection.candidates:
        report.say("  （一個都沒有）")

    report.say()
    report.say("狀態：{}".format(detection.status.name))
    report.say("實際採用：{}".format(
        detection.device.name if detection.device else "（沒有採用任何一個）"))
    report.say()
    for line in device.status_message(detection).splitlines():
        report.say("  " + line.replace("**", ""))

    if detection.status is device.DeviceStatus.AMBIGUOUS:
        report.say()
        report.say("!! 有多個候選而且無法判斷，所以程式不猜。")
        report.say("!! 這通常表示電腦上裝了會掛進「本機」的第三方軟體"
                   "（手機管理工具之類）。")
        report.say("!! 使用者仍然可以在左邊的清單裡自己展開手機來備份。")
    return detection


def _find_richest_level(dev):
    """往下找到子資料夾最多的那一層。對 iPhone 就是 Internal Storage。"""
    node, label = dev.abs_pidl, dev.name
    best = (node, label, [])
    for _ in range(3):
        subs = listing.list_subfolders(node)
        if len(subs) > len(best[2]):
            best = (node, label, subs)
        if not subs:
            break
        node, label = subs[0].abs_pidl, subs[0].name
    return best


def _best_device(detection, candidates=()):
    """挑一台最值得檢查的裝置：優先用實際採用的那台，其次第一個 CONFIRMED。"""
    if detection is not None and detection.device is not None:
        return detection.device
    pool = list(candidates) or list(
        detection.candidates if detection is not None else ())
    for candidate in pool:
        if candidate.confidence is device.Confidence.CONFIRMED:
            return candidate
    return pool[0] if pool else None


def _pick_focus(detection, candidates=()):
    """沒有指定檢查目標時，自己挑一個**在裝置上**的資料夾。

    ★ 取最後一個 —— 資料夾照名稱排序（`202608__` 這種），最後面就是最近的照片。
      使用者會抱怨的幾乎都是最新那幾個資料夾。
    """
    target = _best_device(detection, candidates)
    if target is None:
        return None
    try:
        _pidl, _label, folders = _find_richest_level(target)
    except Exception as exc:   # noqa: BLE001
        log.debug("挑選檢查目標失敗：%s", exc)
        return None
    return folders[-1] if folders else None


def _describe_all_candidates(report, candidates):
    for candidate in candidates:
        report.say()
        report.say("-" * 60)
        report.say("候選：{}  [{}]".format(candidate.name,
                                        candidate.confidence.name))
        report.say("-" * 60)
        try:
            _pidl, label, entries = _find_richest_level(candidate)
        except Exception as exc:      # noqa: BLE001
            report.say("!! 展開失敗：{}".format(exc))
            continue
        if not entries:
            report.say("（底下沒有任何子資料夾 —— "
                       "可能未解鎖／未信任，或這根本不是儲存裝置）")
            continue
        _describe_folders(report, entries, label)


def _describe_folders(report, entries, label):
    report.say("[{}] 底下共 {} 個子資料夾：".format(label, len(entries)))
    report.say()
    for entry in entries:
        report.say("    {}".format(entry.name))

    suffixes = Counter(e.name[-2:] if len(e.name) >= 2 else e.name for e in entries)
    report.say()
    report.say("後綴統計：")
    for suffix, count in sorted(suffixes.items()):
        report.say("    結尾「{}」：{} 個".format(suffix, count))

    double = [e.name for e in entries if e.name.endswith("__")]
    report.say()
    if double:
        report.say("有 {} 個「__」結尾的資料夾，例如：{}".format(
            len(double), "、".join(double[:5])))
    else:
        report.say("!! 一個「__」結尾的資料夾都沒有。")
        report.say("!! 請打開 Windows 檔案總管進到同一層比對：")
        report.say("!!   - 檔案總管也沒有 → 這支手機本來就是這樣命名，不是程式的問題")
        report.say("!!   - 檔案總管有、這裡沒有 → 是程式漏了，請務必回報")


def _is_local_path_folder(folder):
    """這個節點是不是對應到真實檔案系統路徑。

    ★ 用來擋掉 `3D Objects` 那種誤報（2026-09-13）：
      Windows 10 從 21H2 起把 `3D Objects` 從檔案總管隱藏，命名空間項目還在、
      實體資料夾卻常常不存在，於是三種旗標都會秒回 ERROR_PATH_NOT_FOUND。
      舊版據此印出「顯示 0 個檔案很可能是讀取問題」，等於拿一個**必然失敗**
      的目標當健康度指標，還把使用者嚇一跳。
    """
    try:
        parsing = shell_ns.parsing_name(folder.abs_pidl)
    except Exception:   # noqa: BLE001
        return False
    return shell_ns.looks_like_filesystem_path(parsing or "")


def _describe_one_folder(report, folder):
    """某個資料夾到底有沒有東西。用三種旗標交叉比對。"""
    report.say("這一段用來回答：某個資料夾顯示 0 個檔案，是真的空的還是沒讀到。")
    local = _is_local_path_folder(folder)
    if local:
        report.say("（注意：這是**本機**資料夾，不是手機裡的 —— "
                   "它的結果不能拿來判斷裝置狀態。）")
    report.say()

    def count(flags, label):
        """列舉並印出完整的可信度紀錄。

        ★ 「8.8 秒回傳 0 項」和「20 毫秒回傳 0 項」是完全不同的兩件事 ——
          前者是逾時（裝置連線壞掉），後者才是真的空資料夾。
          民眾B 的 log 裡 13 個資料夾都剛好花 8.8 秒回傳 0 項，
          但報告只寫「0 項」，看不出這個關鍵差異。
        """
        enum_report = shell_ns.EnumReport()
        try:
            items = list(shell_ns.iter_entries(folder.abs_pidl, flags=flags,
                                               verify_empty=True,
                                               report=enum_report))
        except Exception as exc:   # noqa: BLE001
            report.say("  {:<12} 讀取失敗：{}".format(label, exc))
            report.say("      {}".format(enum_report.describe()))
            return None
        report.say("  {:<12} {}".format(label, enum_report.describe()))
        if not items and enum_report.suspicious_zero:
            report.say("      !! 這個 0 不可信 —— 見上面的耗時與嘗試次數。")
        return len(items)

    counts = {}
    for flags, label in ((shell_ns.FILES_ONLY, "只列檔案"),
                         (shell_ns.FOLDERS_ONLY, "只列資料夾"),
                         (shell_ns.EVERYTHING, "全部")):
        counts[label] = count(flags, label)

    report.say()
    if None in counts.values():
        if local:
            # ★ 本機資料夾讀不到，多半是那個資料夾根本不存在（例如被
            #   Windows 隱藏起來的 3D Objects），跟 iPhone 一點關係都沒有。
            report.say("這是本機資料夾而且讀不到 —— "
                       "多半是這個資料夾在你的電腦上根本不存在"
                       "（Windows 會保留一些看不到的項目）。")
            report.say("**這跟 iPhone 無關，不是問題。**")
            report.say("若要檢查手機裡的資料夾，請在左邊清單裡勾選它，"
                       "再按一次「產生診斷報告」。")
            return
        report.say("!! 有列舉失敗 —— 顯示 0 個檔案很可能是讀取問題，不是真的空的。")
        return

    if counts["只列檔案"] + counts["只列資料夾"] != counts["全部"]:
        report.say("!! 數字對不起來：{} + {} != {}".format(
            counts["只列檔案"], counts["只列資料夾"], counts["全部"]))
        report.say("!! 這代表列舉不穩定，顯示的數量不可信。")

    if counts["全部"] == 0:
        report.say("!! 這個資料夾三種列舉都是 0 項，看起來是真的空的。")
        report.say("!! 如果用檔案總管進去看得到照片，請務必回報。")
        return

    names = [n for _, n, _ in shell_ns.iter_entries(
        folder.abs_pidl, flags=shell_ns.EVERYTHING)]
    report.say()
    report.say("前 30 個項目與分類：")
    for name in names[:30]:
        report.say("    {:<34} {}".format(name, categorize(name).name))
    if len(names) > 30:
        report.say("    …（其餘 {} 項省略）".format(len(names) - 30))

    media = [n for n in names if categorize(n) & MEDIA]
    report.say()
    report.say("符合「照片 + 影片」的：{} 個（程式會備份的就是這些）".format(len(media)))
    if names and not media:
        report.say("!! 有檔案但沒有一個算照片或影片 —— "
                   "副檔名可能是程式沒涵蓋的，請回報上面的清單。")


#: 判斷傳輸模式時最多取樣幾個資料夾 / 幾個檔案。
#: 這一段是診斷用的，不能把它變成「掃描整支手機」（D11 的教訓）。
_MODE_SAMPLE_FOLDERS = 6
_MODE_SAMPLE_FILES = 400


def _sample_device_files(detection):
    """從裝置上抓一小撮檔名，用來推斷傳輸模式。

    ★ 從**最後面**開始取 —— 資料夾是照名稱排序的（`202608__` 這種），
      最後面就是最近的照片，最能反映使用者現在的設定。
    """
    target = _best_device(detection)
    if target is None:
        return [], ""

    _pidl, label, folders = _find_richest_level(target)
    if not folders:
        return [], label

    names = []
    for entry in reversed(folders[-_MODE_SAMPLE_FOLDERS:]):
        try:
            for _abs, name, _attrs in shell_ns.iter_entries(
                entry.abs_pidl, flags=shell_ns.FILES_ONLY
            ):
                names.append(name)
                if len(names) >= _MODE_SAMPLE_FILES:
                    return names, label
        except Exception as exc:   # noqa: BLE001
            log.debug("取樣資料夾「%s」失敗：%s", entry.name, exc)
    return names, label


def _describe_transfer_mode(report, detection):
    """判斷 iPhone 的「傳送到 Mac 或 PC」是「自動」還是「保留原始檔」。

    ★★ 為什麼要放進報告（2026-09-13）：兩種模式的失敗成因完全不同，
      而使用者幾乎不會主動講自己設了哪一種。之前為了問這一題，
      要多來回一輪 issue。

    ★ 判斷依據與它的極限，都要老實寫進報告：
      「自動」模式**只有在 iOS 認為這台 PC 讀不懂 HEIC 時**才會即時轉檔。
      若電腦上裝了 HEIC 解碼器（例如 CopyTrans HEIC 或微軟的 HEIF 擴充功能），
      iOS 可能判定「這台讀得懂」而直接給 HEIC —— 也就是**看到 .heic
      並不能 100% 斷定是「保留原始檔」**。所以這裡輸出的是證據 + 傾向，
      不是斬釘截鐵的結論。
    """
    names, label = _sample_device_files(detection)
    if not names:
        report.say("取不到任何檔名樣本，無法判斷傳輸模式。")
        report.say("（可能是裝置讀不到，或取樣到的資料夾剛好都沒有檔案。）")
        return

    report.say("取樣位置：[{}] 最後 {} 個資料夾，共 {} 個檔名".format(
        label, _MODE_SAMPLE_FOLDERS, len(names)))
    report.say()

    exts = Counter(extension_of(n) or "（沒有副檔名）" for n in names)
    report.say("副檔名分布：")
    for ext, count in exts.most_common():
        report.say("    .{:<10} {} 個".format(ext, count))

    heic = sum(exts.get(e, 0) for e in ("heic", "heif"))
    jpeg = sum(exts.get(e, 0) for e in ("jpg", "jpeg"))

    # 同一張照片同時有 HEIC 與 JPEG 版本？
    stems = {}
    for name in names:
        stem, _, ext = name.rpartition(".")
        stems.setdefault(stem.lower(), set()).add(ext.lower())
    both = [s for s, e in stems.items()
            if e & {"heic", "heif"} and e & {"jpg", "jpeg"}]
    edited = [n for n in names if n.upper().startswith("IMG_E")]

    report.say()
    report.say("HEIC/HEIF：{} 個　JPG/JPEG：{} 個".format(heic, jpeg))
    report.say("同一張同時有 HEIC 與 JPEG 版本：{} 組".format(len(both)))
    report.say("編輯後版本（IMG_E****）：{} 個".format(len(edited)))
    report.say()

    if heic:
        report.say(">> 傾向：**保留原始檔**（清單裡直接看得到 .heic/.heif）")
        report.say("   但這不是 100% —— 如果這台電腦裝了 HEIC 解碼器，")
        report.say("   iOS 可能認為它讀得懂，即使設定是「自動」也直接給原始檔。")
    elif jpeg:
        report.say(">> 傾向：**自動**（完全沒有 .heic/.heif，照片都是 JPG）")
        report.say("   也可能是使用者在「設定 → 相機 → 格式」選了「最相容」，")
        report.say("   那樣拍出來本來就是 JPG。兩者從檔名分不出來。")
    else:
        report.say(">> 無法判斷：樣本裡既沒有 HEIC 也沒有 JPEG。")


def _describe_wpd(report):
    """WPD 探針 —— Shell 路徑分不出來的裝置狀態，這裡問得到（決策 D21）。

    ★ 這一段失敗完全不影響程式，它只是報告的一段。
    """
    report.say("（這一段會實際跟裝置要一次連線，可能要等幾秒。）")
    report.say()
    result = wpd_probe.probe()

    if not result.available:
        report.say("（這台電腦上的 WPD 探針不可用：{}）".format(
            result.unavailable_reason))
        report.say("這不影響備份功能，只是少一份判斷裝置狀態的資訊。")
        return

    if result.error:
        report.say("!! 探針中途失敗：{}".format(result.error))

    if not result.devices:
        report.say("WPD 說：**一台可攜式裝置都沒有。**")
        report.say("這是獨立於上面那些 Shell 判斷的第二個答案 ——")
        report.say("如果上面看得到 iPhone 而這裡看不到，請務必回報。")
        return

    report.say("WPD 看到 {} 台裝置："
               "（這是獨立於 Shell 列舉的第二個答案）".format(len(result.devices)))
    for entry in result.devices:
        report.say()
        report.say("  {}".format(entry.friendly_name or "（沒有名稱）"))
        report.say("      製造商    = {}".format(entry.manufacturer or "（取不到）"))
        report.say("      描述      = {}".format(entry.description or "（取不到）"))
        if entry.opened:
            report.say("      連線測試  = ★ 可以開啟 —— **裝置本身是好的**")
        else:
            report.say("      連線測試  = !! 開不起來：{}".format(
                copysink.describe_hresult(entry.open_hresult)))
            if entry.open_error:
                report.say("                  {}".format(entry.open_error))
            report.say("      ↑ 這一行是 Shell 路徑給不出來的資訊。"
                       "「被其他程式佔用」「裝置當掉」「已拔除」")
            report.say("        在 Shell 那邊全都長成同一個 "
                       "0x8007001E，只有這裡分得出來。")


def _describe_log_tail(report):
    log_path = default_log_dir() / "backpacker.log"
    report.say("紀錄檔：{}".format(log_path))
    report.say()
    if not log_path.exists():
        report.say("（紀錄檔還不存在）")
        return
    with log_path.open(encoding="utf-8", errors="replace") as handle:
        tail = handle.readlines()[-LOG_TAIL_LINES:]
    report.say("最後 {} 行：".format(len(tail)))
    report.say()
    for line in tail:
        report.say("  " + line.rstrip())


def collect_report(focus_folder=None, progress=None):
    """跑完整套診斷，回傳報告全文。

    focus_folder: 可選的 FileEntry，會針對它做逐項檢查。
                  GUI 會把使用者目前選取的資料夾傳進來 ——
                  這樣「看某個可疑資料夾」不需要命令列參數。
    progress:     可選的 callable，收一段文字，用來更新 UI 狀態。
    """
    started = time.perf_counter()
    report = _Report(progress)

    report.title("iPhone Backpacker 診斷報告")
    report.section("執行環境", lambda: _describe_environment(report))
    report.section("零、Shell API 檢查", lambda: _describe_api(report))
    report.section("一、「本機」底下有什麼", lambda: _describe_this_pc(report))

    holder = {}
    report.section("二、裝置偵測",
                   lambda: holder.update(detection=_describe_device(report)))
    detection = holder.get("detection")

    # ★ 第二段失敗時（例如 probe 拿到 0x8007001E），舊版 detection 會是 None，
    #   於是 candidates 變成空的，**第三段整段消失** —— 偏偏那是最有價值的一段，
    #   而且它消失的時機正好是出事的時候。改成自己再找一次候選清單。
    candidates = detection.candidates if detection is not None else ()
    if not candidates:
        try:
            candidates = tuple(device.find_portable_devices())
            if candidates:
                report.say()
                report.say("（上一段失敗，改用備援方式重新列出候選裝置：{} 個）"
                           .format(len(candidates)))
        except Exception as exc:   # noqa: BLE001
            log.debug("備援列出候選裝置也失敗：%s", exc)

    if candidates:
        # ★ 對**每一個**候選都走一次，不是只走選中的那個。
        #   上一版只走選中的，結果在民眾B 的機器上走進了 CopyTrans Studio，
        #   然後對著它的資料夾名稱抱怨「一個 __ 結尾的資料夾都沒有」——
        #   完全是假警報，因為看錯裝置了。
        report.section(
            "三、每個候選裝置底下的資料夾",
            lambda: _describe_all_candidates(report, candidates))

    # ★★ 逐項檢查的目標絕不能預設成「本機」底下的第一個節點。
    #   排序後 "3d objects" 的 '3' 在 ASCII 排在所有字母前面，於是它
    #   一直是樹狀清單的第一個、也就是預設選取的那個。而 Windows 10 21H2
    #   之後 3D Objects 的實體資料夾常常不存在，三種旗標必定失敗，
    #   報告就印出「顯示 0 個檔案很可能是讀取問題」嚇使用者 ——
    #   拿一個必然失敗的目標當健康度指標。
    focus = focus_folder or _pick_focus(detection, candidates)
    if focus is not None:
        report.section("四、[{}] 逐項檢查".format(focus.name),
                       lambda: _describe_one_folder(report, focus))
    else:
        report.title("四、逐項檢查")
        report.say("（找不到可以檢查的資料夾。若某個資料夾的檔案數看起來不對，")
        report.say("　請在程式裡勾選那個資料夾，再按一次「產生診斷報告」。）")

    report.section("五、傳輸模式判定",
                   lambda: _describe_transfer_mode(report, detection))
    report.section("六、WPD 探針（裝置狀態）", lambda: _describe_wpd(report))
    report.section("七、紀錄檔內容", lambda: _describe_log_tail(report))

    report.title("報告結束")
    report.say("耗時 {:.1f} 秒。".format(time.perf_counter() - started))
    report.say("請把這個檔案整份傳給開發者。")
    return report.text()


def write_report(text, path=None):
    """把報告寫成 .txt。刻意不用 .log —— 使用者要找得到、打得開、傳得出去。"""
    path = Path(path) if path else default_report_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Windows 記事本對沒有 BOM 的 UTF-8 中文會顯示成亂碼，所以用 utf-8-sig。
    path.write_text(text, encoding="utf-8-sig")
    log.info("診斷報告已寫入：%s", path)
    return path
