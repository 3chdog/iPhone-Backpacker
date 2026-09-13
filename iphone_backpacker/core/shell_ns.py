"""Windows Shell 命名空間的薄封裝。

這是全專案唯一直接碰 pywin32 的地方，其他模組只透過這裡的函式操作。

★ 關鍵概念 —— PIDL 與 COM 介面的差別（見 docs/ai/04-shell-com-notes.md）：
  - IShellFolder / IShellItem 這類 COM 介面 **不能跨執行緒傳遞**。
  - PIDL 是純資料（bytes 的序列），**可以**安全跨執行緒傳遞。
  所以整個 core 的公開介面一律用「絕對 PIDL」交換位置，
  需要 COM 物件時在使用端當場 bind 出來。

★ 一律不要比對顯示名稱字串（"本機" / "This PC" / "Apple iPhone"）。
  英文/日文版 Windows 會全滅，使用者把 iPhone 改名也會失效。
"""

import contextlib
import logging
import re
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import List

import pythoncom
from win32com.shell import shell, shellcon

from .errors import ItemNotFoundError, ShellError

log = logging.getLogger(__name__)

# 列舉旗標。瀏覽路徑只用 FOLDERS_ONLY —— 見「效能契約」。
FOLDERS_ONLY = shellcon.SHCONTF_FOLDERS
FILES_ONLY = shellcon.SHCONTF_NONFOLDERS
EVERYTHING = shellcon.SHCONTF_FOLDERS | shellcon.SHCONTF_NONFOLDERS

# ★ 批次大小是 MTP 上最重要的效能參數（2026-08-27 實測發現）。
#
# 實測顯示成本 ≈ 每個「被實體化的項目」~10ms，而 Next(n) 不管呼叫端
# 實際要幾筆，都會把 n 筆準備好。所以：
#   - 需要「全部」時：批次大小影響不大，反正每一筆都要付錢。
#   - 只需要「前幾筆」時（早退）：批次大小就是全部的成本。
#     用 64 去問「這資料夾有沒有照片」等於付 64 筆的錢拿 1 筆的答案。
#
# 證據：在 421 項的資料夾上，取 1 筆 / 10 筆 / 50 筆都是 ~720ms（一次
# Next(64)），取 200 筆跳到 2654ms（四次），取 421 筆是 4258ms（七次）。
# 實測（2026-08-27，三個 293~479 項的資料夾）：
#   完整列舉：batch 1~1024 差不到 3%（都是 ~3.4 ms/項）→ 設多少都一樣
#   只取第 1 筆：batch=1 約 67 ms，batch=64 約 290 ms，batch=1024 約 1450 ms
# 所以 DEFAULT_BATCH 不重要，PROBE_BATCH 很重要，而且 1 就是最佳解。
DEFAULT_BATCH = 64
PROBE_BATCH = 1     # 早退式探測用：只想知道「有沒有」，不想付整批的錢

_ENUM_BATCH = DEFAULT_BATCH   # 保留舊名稱


@contextlib.contextmanager
def com_apartment():
    """worker thread 用的 COM apartment。

    每一個會碰 Shell 的執行緒都必須包在這裡面，主執行緒也不例外。
    """
    pythoncom.CoInitialize()
    try:
        yield
    finally:
        pythoncom.CoUninitialize()


def as_pidl(pidl):
    """把 PIDL 正規化成 tuple，讓它可 hash（才能當快取的 key）也可比較。"""
    return tuple(pidl)


def combine(parent_abs_pidl, child_rel_pidl):
    """父節點的絕對 PIDL + 子項的相對 PIDL = 子項的絕對 PIDL。

    pywin32 把 PIDL 表示成 item-id 的序列，所以串接就是直接相加。
    """
    return tuple(parent_abs_pidl) + tuple(child_rel_pidl)


def desktop_folder():
    """Shell 命名空間的根。"""
    return shell.SHGetDesktopFolder()


def this_pc_pidl():
    """「本機 / This PC」的絕對 PIDL。

    用 CSIDL_DRIVES 取得，語言中立 —— 這是取代舊版 rootName="本機" 的正解。
    """
    try:
        pidl = shell.SHGetSpecialFolderLocation(0, shellcon.CSIDL_DRIVES)
    except pythoncom.com_error as exc:
        raise ShellError("無法取得「本機」節點：{}".format(exc)) from exc
    return as_pidl(pidl)


def bind_folder(abs_pidl):
    """由絕對 PIDL 取得 IShellFolder。呼叫端必須已經在 COM apartment 裡。"""
    abs_pidl = tuple(abs_pidl)
    if not abs_pidl:
        return desktop_folder()
    try:
        return desktop_folder().BindToObject(
            list(abs_pidl), None, shell.IID_IShellFolder
        )
    except pythoncom.com_error as exc:
        raise ShellError("無法繫結到資料夾：{}".format(exc)) from exc


def shell_item(abs_pidl):
    """由絕對 PIDL 取得 IShellItem，給 IFileOperation 用。"""
    try:
        return shell.SHCreateItemFromIDList(list(abs_pidl))
    except pythoncom.com_error as exc:
        raise ShellError("無法建立 ShellItem：{}".format(exc)) from exc


def item_from_path(path):
    """由真實檔案系統路徑取得 IShellItem。

    目的地永遠是本機路徑，用這個就好，不需要走 Shell 樹一層層 bind ——
    這取代了舊版整個 getFolderObject_byAbsPath() 的目的地分支。
    """
    try:
        return shell.SHCreateItemFromParsingName(
            str(path), None, shell.IID_IShellItem
        )
    except pythoncom.com_error as exc:
        raise ShellError("無法開啟目的地「{}」：{}".format(path, exc)) from exc


def pidl_from_path(path):
    """由真實檔案系統路徑取得絕對 PIDL。

    主要給測試與「目的地」使用。瀏覽 iPhone 時用不到 ——
    MTP 節點沒有真實路徑，那正是舊版字串路徑解析層失敗的根源。
    """
    text = str(path)
    try:
        result = shell.SHParseDisplayName(text, None, 0)
    except (AttributeError, TypeError):
        result = shell.SHILCreateFromPath(text, 0)
    except pythoncom.com_error as exc:
        raise ShellError("無法解析路徑「{}」：{}".format(path, exc)) from exc
    # 依 pywin32 版本，可能回 pidl 本身或 (pidl, attributes)
    pidl = result[0] if isinstance(result, tuple) else result
    if pidl is None:
        raise ItemNotFoundError("找不到路徑：{}".format(path))
    return as_pidl(pidl)


# --------------------------------------------------------------------------
# 列舉
# --------------------------------------------------------------------------

class EnumStatus(Enum):
    """一次列舉的結果分類。

    ★★ 為什麼需要這個（2026-09-13，民眾B 案）：

      `IEnumIDList` 在**語意上**就無法區分「列舉完了」與「provider 放棄了
      但選擇不報錯」。微軟文件寫得很明白：`Next()` 回 S_FALSE 且
      pceltFetched = 0 就是「沒有更多項目」，沒有第三種可能；而
      `EnumObjects()` 回 S_FALSE 時**還允許**把 enumerator 設成 NULL
      （Raymond Chen, The Old New Thing, 2024-08-12）。
      所以「0 項」這個回答本身不帶任何可信度資訊。

      這不是我們的 bug，也不是 pywin32 的 bug —— 是 API 的解析度不夠。
      補救辦法只有一個：**自己補第二個訊號源**。我們補三個：
        1. 耗時     —— 8.8 秒的 0 和 20 毫秒的 0 是完全不同的兩件事
        2. 重驗     —— 重新 bind、重新列舉，兩次都 0 才採信
        3. 旗標交叉 —— 只列檔案 + 只列資料夾 應該等於 全部（診斷報告在做）

      災情實例（2026-08-30，民眾B）：13 個資料夾**各花 8.8 秒回傳 0 項**，
      程式判定「待複製 0 個」並回報「全部已存在」—— 事實上那 3649 個檔案
      一個都沒備份到。**把「讀不到」當成「沒有東西要備份」，
      是備份工具最嚴重的一種錯誤。**
    """

    OK = auto()                 # 至少取得 1 項，過程中沒有例外
    RECOVERED = auto()          # 中途失敗，重建 enumerator 後成功
    EMPTY = auto()              # 0 項，而且很快 —— 視為真的空（沒有重驗）
    EMPTY_VERIFIED = auto()     # 0 項，重驗一次仍然是 0 —— 可信的空
    EMPTY_WAS_WRONG = auto()    # ★ 第一次 0，重驗卻拿到東西 —— 第一次是假的
    NULL_ENUMERATOR = auto()    # EnumObjects 回 S_FALSE / NULL（文件允許）
    FAILED = auto()             # 所有嘗試都失敗


# 一次列舉最多試幾輪。3 是折衷：MTP 的偶發失敗通常一次就好，
# 試太多輪會讓「真的壞掉」的情況拖很久才告訴使用者。
ENUM_MAX_ATTEMPTS = 3

# 第 n 輪嘗試前要等多久。★ 零退避是沒有意義的重試 ——
# 對「需要一點時間恢復」的裝置，立刻重問幾乎一定得到同樣的答案。
ENUM_BACKOFF_SECONDS = (0.0, 0.25, 1.0)

# 0 項而且花了這麼久，就不該直接相信。實測空資料夾約 20~70 ms，
# 而民眾B 的災情是 8800 ms —— 兩者差兩個數量級，門檻放 1 秒很安全。
SUSPICIOUS_EMPTY_MS = 1000.0


@dataclass
class EnumReport:
    """一次列舉的可信度紀錄。

    ★ 這個物件的存在理由就是「讓 0 說得出自己是哪一種 0」。
      呼叫端可以不理它（預設會自己建一個丟掉，log 照樣會寫），
      但複製路徑與診斷報告一定要看。
    """

    status: EnumStatus = EnumStatus.OK
    count: int = 0
    elapsed_ms: float = 0.0
    attempts: int = 0
    null_enumerator: bool = False
    errors: List[str] = field(default_factory=list)

    #: 這次列舉的結果能不能拿來當「這裡沒有東西」的證據。
    #: ★ 只有 OK / RECOVERED / EMPTY_VERIFIED / EMPTY（快速）算數。
    @property
    def trustworthy(self):
        return self.status in (EnumStatus.OK, EnumStatus.RECOVERED,
                               EnumStatus.EMPTY, EnumStatus.EMPTY_VERIFIED)

    @property
    def suspicious_zero(self):
        """0 項，而且有理由懷疑那是假的。"""
        if self.count:
            return False
        return (self.status is EnumStatus.EMPTY_WAS_WRONG
                or self.elapsed_ms >= SUSPICIOUS_EMPTY_MS
                or bool(self.errors))

    def describe(self):
        """一行文字，給 log 與診斷報告用。"""
        parts = ["{} 項".format(self.count), "{:.0f} ms".format(self.elapsed_ms)]
        if self.attempts > 1:
            parts.append("嘗試 {} 次".format(self.attempts))
        if self.null_enumerator:
            parts.append("EnumObjects 回 NULL")
        parts.append(_ENUM_STATUS_TEXT.get(self.status, self.status.name))
        if self.errors:
            parts.append("錯誤：{}".format("；".join(self.errors[:3])))
        return " / ".join(parts)

    def _finish(self, count, started, status):
        self.count = count
        self.elapsed_ms = (time.perf_counter() - started) * 1000.0
        self.status = status
        return self


_ENUM_STATUS_TEXT = {
    EnumStatus.OK: "正常",
    EnumStatus.RECOVERED: "★ 中途失敗，重建後成功",
    EnumStatus.EMPTY: "空的（未重驗）",
    EnumStatus.EMPTY_VERIFIED: "空的（重驗過仍是 0）",
    EnumStatus.EMPTY_WAS_WRONG: "★★ 第一次回 0 是假的，重驗後有東西",
    EnumStatus.NULL_ENUMERATOR: "EnumObjects 回 S_FALSE / NULL",
    EnumStatus.FAILED: "★★ 失敗",
}


class _NullEnumerator(Exception):
    """內部訊號：EnumObjects 回了 S_FALSE / NULL。不是錯誤。"""


def _enum_once(abs_pidl, flags, batch):
    """跑一次完整的列舉，yield (folder, rel_pidl)。

    ★ 每一次嘗試都**重新 BindToObject**，而不是沿用上一次的 folder。
      理由有兩個：
        1. 微軟文件對「`Next()` 回錯誤之後 enumerator 還能不能用」
           **完全沒有任何保證**。在故障的 enumerator 上重呼叫 `Next()`
           是沒有文件支持的行為（舊版就是這樣寫的）。
        2. 重新 bind 會重走 Desktop → 本機 → 裝置 → … 這條路，
           那正是重新建立裝置連線的機會。

      yield 出 folder 是刻意的 —— 呼叫端要用**這一輪**的 folder 去取名稱，
      不能用上一輪那個可能已經失效的。
    """
    folder = bind_folder(abs_pidl)
    try:
        enumerator = folder.EnumObjects(0, flags)
    except pythoncom.com_error as exc:
        raise ShellError("列舉失敗：{}".format(exc)) from exc
    if enumerator is None:
        # 文件允許：EnumObjects 回 S_FALSE 時可以不給 enumerator。
        # 意思是「沒有項目」，不是錯誤 —— 但我們要記下來，
        # 因為它和「列舉出 0 項」在成因上不一樣。
        raise _NullEnumerator()

    while True:
        try:
            chunk = enumerator.Next(batch)
        except TypeError:
            # pywin32 各版本 Next() 的簽章不完全一致。
            batch = None
            chunk = enumerator.Next()
        if not chunk:
            return
        if not isinstance(chunk, (list, tuple)):
            chunk = [chunk]
        for rel_pidl in chunk:
            yield folder, rel_pidl


def _iter_pairs(abs_pidl, flags, batch, verify_empty, report):
    """列舉一層，yield (folder, rel_pidl)，並把可信度寫進 report。

    verify_empty:
        None  只有「慢速的 0」才重驗 —— 瀏覽路徑用，保護 0.5 秒的效能契約
        True  0 項一律重驗 —— **複製路徑用**，那裡的假 0 等於漏備份
        False 不重驗
    """
    seen = set()
    started = time.perf_counter()
    completed = False

    for attempt in range(1, ENUM_MAX_ATTEMPTS + 1):
        report.attempts = attempt
        delay = ENUM_BACKOFF_SECONDS[min(attempt - 1, len(ENUM_BACKOFF_SECONDS) - 1)]
        if delay:
            time.sleep(delay)
        try:
            for folder, rel_pidl in _enum_once(abs_pidl, flags, batch):
                key = tuple(rel_pidl)
                # ★ 用 PIDL 去重，不用「跳過前 N 筆」——
                #   Shell 不保證兩次列舉的順序一致，靠順序會拿到錯的東西。
                #   （舊 CLI 版的 zip bug 就是這樣來的。）
                if key in seen:
                    continue
                seen.add(key)
                yield folder, rel_pidl
        except _NullEnumerator:
            report.null_enumerator = True
            completed = True
            break
        except (pythoncom.com_error, ShellError) as exc:
            report.errors.append(str(exc))
            log.warning("列舉第 %d 次嘗試失敗（目前已取得 %d 項）：%s",
                        attempt, len(seen), exc)
            continue
        completed = True
        break

    if not completed:
        report._finish(len(seen), started, EnumStatus.FAILED)
        log.error("列舉失敗：%s", report.describe())
        raise ShellError(
            "列舉失敗，清單不完整（已取得 {} 項、試了 {} 次）：{}。"
            "請按「重新整理裝置」，或把 USB 線拔掉重插。".format(
                len(seen), report.attempts,
                report.errors[-1] if report.errors else "未知錯誤"))

    if seen:
        report._finish(len(seen), started,
                       EnumStatus.RECOVERED if report.errors else EnumStatus.OK)
        _log_enum(report)
        return

    # ---- 0 項：要不要再驗一次？ ----
    elapsed = (time.perf_counter() - started) * 1000.0
    want_verify = (verify_empty is True
                   or (verify_empty is None and elapsed >= SUSPICIOUS_EMPTY_MS))
    if not want_verify:
        report._finish(0, started,
                       EnumStatus.NULL_ENUMERATOR if report.null_enumerator
                       else EnumStatus.EMPTY)
        _log_enum(report)
        return

    report.attempts += 1
    time.sleep(ENUM_BACKOFF_SECONDS[1])
    try:
        for folder, rel_pidl in _enum_once(abs_pidl, flags, batch):
            key = tuple(rel_pidl)
            if key in seen:
                continue
            seen.add(key)
            yield folder, rel_pidl
    except _NullEnumerator:
        report.null_enumerator = True
    except (pythoncom.com_error, ShellError) as exc:
        report.errors.append(str(exc))
        log.warning("0 項重驗時失敗：%s", exc)

    report._finish(len(seen), started,
                   EnumStatus.EMPTY_WAS_WRONG if seen else EnumStatus.EMPTY_VERIFIED)
    _log_enum(report)


def _log_enum(report):
    """把可疑的列舉結果寫進 log。

    ★ 正常結果寫 debug（量太大），可疑的一律 warning ——
      收到災情回報時，log 裡必須看得出「那個 0 是哪一種 0」。
    """
    if report.status in (EnumStatus.EMPTY_WAS_WRONG, EnumStatus.FAILED):
        log.warning("列舉結果可疑：%s", report.describe())
    elif report.status is EnumStatus.RECOVERED or report.suspicious_zero:
        log.warning("列舉結果：%s", report.describe())
    else:
        log.debug("列舉結果：%s", report.describe())


def iter_child_pidls(abs_pidl, flags=EVERYTHING, batch=DEFAULT_BATCH,
                     verify_empty=None, report=None):
    """只列舉子項的絕對 PIDL，不取顯示名稱。

    給效能量測與「只需要數量/位置、不需要名字」的場合用。
    GetDisplayNameOf 在 MTP 上是每個項目一次來回，佔比可能不小，
    所以把「有沒有取名字」拆成兩支函式才量得出來。
    """
    if report is None:
        report = EnumReport()
    for _folder, rel_pidl in _iter_pairs(abs_pidl, flags, batch,
                                         verify_empty, report):
        yield combine(abs_pidl, rel_pidl)


def iter_entries(abs_pidl, flags=EVERYTHING, want_attributes=False,
                 want_parsing=False, batch=DEFAULT_BATCH,
                 verify_empty=None, report=None):
    """列舉一層，yield (child_abs_pidl, name, attributes)。

    want_attributes=False 時 attributes 為 None。
    取屬性要多一次 COM 呼叫，非必要不取（MTP 上每一次來回都是成本）。

    want_parsing=True 時改 yield (child_abs_pidl, name, attributes, parsing)，
    parsing 取不到時是 None（**不是空字串** —— 「取不到」和「空的」
    必須分得出來，否則呼叫端會拿未知當成證據）。
    """
    if report is None:
        report = EnumReport()

    for folder, rel_pidl in _iter_pairs(abs_pidl, flags, batch,
                                        verify_empty, report):
        try:
            name = folder.GetDisplayNameOf(rel_pidl, shellcon.SHGDN_NORMAL)
        except pythoncom.com_error:
            # ★ 這裡以前是 log.warning + continue，等於**靜默漏掉一個項目**。
            #   漏掉一個資料夾 → 使用者看不到也就備份不到；
            #   漏掉一個檔案 → 那個檔案不會被複製。兩種都不能默默發生。
            #   先重試一次（MTP 偶發），仍然失敗就 raise。
            try:
                name = folder.GetDisplayNameOf(rel_pidl, shellcon.SHGDN_NORMAL)
            except pythoncom.com_error as exc2:
                raise ShellError(
                    "有項目讀不到名稱，清單不完整：{}。"
                    "請按「重新整理裝置」，或把 USB 線拔掉重插。".format(exc2)
                ) from exc2
        attributes = None
        if want_attributes:
            attributes = attributes_of(folder, rel_pidl)

        if not want_parsing:
            yield combine(abs_pidl, rel_pidl), name, attributes
            continue

        # 用同一個已繫結的 folder 取解析名稱，不必再 bind 一次。
        try:
            parsing = child_parsing_name(folder, rel_pidl)
        except Exception as exc:      # noqa: BLE001
            # ★ 這裡以前是 log.debug，而 log level 是 INFO ——
            #   等於「解析名稱讀不到」這件事在 log 裡完全看不見。
            #   裝置判斷就是靠解析名稱，讀不到必須留下痕跡。
            log.warning("取不到「%s」的解析名稱：%s", name, exc)
            parsing = None
        yield combine(abs_pidl, rel_pidl), name, attributes, parsing


def _sfgao(name, fallback):
    """取 shellcon 的 SFGAO_* 常數，取不到就用官方文件上的值。

    不同版本的 pywin32 不見得都定義了每一個旗標，而我們已經被
    「假設某個符號存在」害過一次（見 parsing_name 的說明）。
    """
    return getattr(shellcon, name, fallback)


SFGAO_FOLDER = _sfgao("SFGAO_FOLDER", 0x20000000)
SFGAO_FILESYSTEM = _sfgao("SFGAO_FILESYSTEM", 0x40000000)
SFGAO_FILESYSANCESTOR = _sfgao("SFGAO_FILESYSANCESTOR", 0x10000000)
SFGAO_STORAGE = _sfgao("SFGAO_STORAGE", 0x00000008)
SFGAO_STREAM = _sfgao("SFGAO_STREAM", 0x00400000)
SFGAO_STORAGEANCESTOR = _sfgao("SFGAO_STORAGEANCESTOR", 0x00800000)
SFGAO_REMOVABLE = _sfgao("SFGAO_REMOVABLE", 0x02000000)
SFGAO_BROWSABLE = _sfgao("SFGAO_BROWSABLE", 0x08000000)

# ★ GetAttributesOf 只會回傳「你在 mask 裡問到的位元」。
#   以前只問 FOLDER|FILESYSTEM，所以 0x20000000 的意思是
#   「在我問的兩個位元裡只有 FOLDER」，**不代表其他位元是 0 —— 我們沒問**。
#
#   現在一次多問幾個。這**不會增加 COM 呼叫次數**（同一次呼叫），
#   多出來的位元目前**只寫進診斷報告，不參與判斷** ——
#   先累積真實資料，確認 WPD 裝置與第三方掛載在這些位元上真的有穩定差異，
#   再考慮拿來用。這是上次踩坑之後該有的紀律。
DEFAULT_ATTRIBUTE_MASK = (
    SFGAO_FOLDER | SFGAO_FILESYSTEM | SFGAO_FILESYSANCESTOR
    | SFGAO_STORAGE | SFGAO_STREAM | SFGAO_STORAGEANCESTOR
    | SFGAO_REMOVABLE | SFGAO_BROWSABLE
)

_ATTRIBUTE_NAMES = [
    ("FOLDER", SFGAO_FOLDER),
    ("FILESYSTEM", SFGAO_FILESYSTEM),
    ("FILESYSANCESTOR", SFGAO_FILESYSANCESTOR),
    ("STORAGE", SFGAO_STORAGE),
    ("STREAM", SFGAO_STREAM),
    ("STORAGEANCESTOR", SFGAO_STORAGEANCESTOR),
    ("REMOVABLE", SFGAO_REMOVABLE),
    ("BROWSABLE", SFGAO_BROWSABLE),
]


def describe_attributes(attrs):
    """把屬性值攤成人看得懂的旗標名稱，給診斷報告用。"""
    if attrs is None:
        return "None（讀不到）"
    flags = [label for label, bit in _ATTRIBUTE_NAMES if attrs & bit]
    return "0x{:08X}（{}）".format(attrs, " | ".join(flags) if flags else "無")


def attributes_of(folder, rel_pidl, mask=None):
    """取得項目的 SFGAO_* 屬性。

    判斷「是不是可攜式裝置」主要看兩個旗標：
      SFGAO_FOLDER      是資料夾類的節點
      SFGAO_FILESYSTEM  對應到真實檔案系統

    ★ 但要注意：這兩個旗標只能分出「虛擬資料夾」與「真實檔案系統資料夾」。
      **可攜式裝置和第三方掛進「本機」的 namespace extension
      （例如 CopyTrans Studio）同屬「虛擬資料夾」**，這組旗標分不出來。
      要區分得靠 looks_like_portable_device() 的解析名稱證據。
    """
    if mask is None:
        mask = DEFAULT_ATTRIBUTE_MASK
    try:
        return folder.GetAttributesOf([rel_pidl], mask)
    except pythoncom.com_error as exc:
        # ★ 回 None（未知）而不是 0（全部旗標都沒有）。
        #   回 0 會讓呼叫端誤判成「不是資料夾、不是檔案系統」，
        #   裝置偵測就會把這個節點整個排除掉。
        log.warning("取得屬性失敗，視為未知：%s", exc)
        return None


_DRIVE_PATH = re.compile(r"^[A-Za-z]:([\\/]|$)")


def looks_like_filesystem_path(name):
    """解析名稱看起來是不是真實檔案系統路徑。

    磁碟機與使用者資料夾的 SHGDN_FORPARSING 會回 `C:\\` 或
    `C:\\Users\\某人\\Desktop` 這種路徑；
    MTP 裝置則是 `::{GUID}\\\\?\\usb#vid_05ac...` 那種東西。

    這個判斷不看顯示名稱，所以使用者把 iPhone 改成什麼名字都不受影響。
    """
    if not name:
        return False
    if _DRIVE_PATH.match(name):
        return True
    if name.startswith("\\\\"):       # UNC 網路路徑
        return True
    return False


# WPD 的裝置介面類別 GUID。所有 WPD 驅動都會註冊這個介面，
# 所以可攜式裝置的解析名稱裡一定看得到它。
#   https://learn.microsoft.com/en-us/windows-hardware/drivers/install/guid-devinterface-wpd
_WPD_DEVICE_INTERFACE = "{6ac27878-a6fa-4155-ba85-f98f491d4f33}"

# 「Portable Devices」這個 delegate folder 在「本機」底下的 CLSID。
_WPD_NAMESPACE = "{35786d3c-b075-49b9-88dd-029876e11c01}"

# Win32 裝置介面路徑的開頭。實測 iPhone 的解析名稱長這樣：
#   ::{20D04FE0-...}\\?\usb#vid_05ac&pid_12a8#<序號>#{6ac27878-...}
_DEVICE_INTERFACE_PREFIX = "\\\\?\\"


def looks_like_portable_device(parsing):
    """解析名稱裡有沒有「這是一台可攜式裝置」的正面證據。

    ★★ 為什麼需要這個：`SFGAO_FOLDER 且非 SFGAO_FILESYSTEM` 只能分出
      「虛擬資料夾」與「真實檔案系統資料夾」。**可攜式裝置與第三方掛進
      「本機」的 namespace extension 同屬虛擬資料夾**，那組旗標在原理上
      就分不出來 —— 不是實作有 bug，是訊號解析度不夠。

      實測（2026-08-30，民眾B）：

          CopyTrans Studio   attrs = 0x20000000   ← 跟 iPhone 一模一樣
          Apple iPhone       attrs = 0x20000000

      所以要另外找一個**正面**證據。WPD 裝置的解析名稱裡一定帶著
      裝置介面路徑或 WPD 的介面 GUID，第三方 namespace extension 則是
      `::{自己的 CLSID}`，不會有這些東西。

    ★ 這是「有證據才升級為確認」，**不是**「沒證據就排除」——
      取不到解析名稱時仍然可能是裝置，只是我們無法確認。
    """
    if not parsing:
        return False
    lowered = parsing.lower()
    return (_DEVICE_INTERFACE_PREFIX in parsing
            or _WPD_DEVICE_INTERFACE in lowered
            or _WPD_NAMESPACE in lowered)


def find_child(abs_pidl, name, flags=EVERYTHING):
    """在指定節點下找出叫某個名字的子項，回傳絕對 PIDL。

    ★ 只用在「已知結構的內部探索」（例如往下找 DCIM），
      不要拿來做使用者可見的路徑解析 —— 那正是舊版被 iOS 改版打爆的地方。
    """
    for child_abs, child_name, _ in iter_entries(abs_pidl, flags):
        if child_name == name:
            return child_abs
    raise ItemNotFoundError("在指定節點下找不到「{}」".format(name))


def display_name(abs_pidl):
    """取得單一節點的顯示名稱。

    ★ 這裡本來也是用 `shell.SHBindToParent`，而那個函式在 pywin32 裡不存在
      （見 `parsing_name` 的說明）。因為呼叫端用 `except Exception` 包住，
      失敗會靜靜地變成名稱「?」，所以一直沒被發現。

      改用桌面資料夾把絕對 PIDL 當成相對 PIDL —— 跟 `parsing_name` 同一招。
    """
    try:
        return desktop_folder().GetDisplayNameOf(
            list(abs_pidl), shellcon.SHGDN_NORMAL)
    except Exception as exc:      # noqa: BLE001
        raise ShellError("無法取得顯示名稱：{}".format(exc)) from exc


def child_parsing_name(folder, rel_pidl):
    """用已經繫結好的父資料夾取得子項的解析名稱。

    ★ 這是最可靠的作法：`GetDisplayNameOf` 我們本來就在用（取顯示名稱），
      只是換一個旗標。不需要任何額外的 API。
    """
    return folder.GetDisplayNameOf(rel_pidl, shellcon.SHGDN_FORPARSING)


def parsing_name(abs_pidl):
    """由絕對 PIDL 取得解析名稱。

    ★★ 這裡曾經寫成 `shell.SHBindToParent(...)`，而**那個函式在 pywin32
      裡根本不存在**。結果是每一次呼叫都拋 AttributeError，
      而呼叫端把「取不到」誤讀成「MTP 裝置的特徵」，
      進而把「本機」底下的每一個節點都判定成 iPhone。

      教訓有兩個，都寫在這裡免得再犯：

      1. **不要假設某個 API 存在。** 用 `getattr` 檢查，或用我們已經
         驗證過能動的東西（這裡就是 `GetDisplayNameOf`）。
      2. **「取不到資訊」永遠不能當成正面證據。** 那只代表我們不知道。

    依序嘗試幾種作法，全部失敗才 raise。
    """
    attempts = []

    # 1. 桌面資料夾會把絕對 PIDL 當成相對於自己的 PIDL —— 這是標準用法，
    #    而且只用到我們已經確定能動的 GetDisplayNameOf。
    try:
        return desktop_folder().GetDisplayNameOf(
            list(abs_pidl), shellcon.SHGDN_FORPARSING)
    except Exception as exc:      # noqa: BLE001
        attempts.append("desktop.GetDisplayNameOf：{}".format(exc))

    # 2. 較新的 API，pywin32 不一定有，所以先檢查存不存在。
    getter = getattr(shell, "SHGetNameFromIDList", None)
    sigdn = getattr(shellcon, "SIGDN_DESKTOPABSOLUTEPARSING", None)
    if getter is not None and sigdn is not None:
        try:
            return getter(list(abs_pidl), sigdn)
        except Exception as exc:  # noqa: BLE001
            attempts.append("SHGetNameFromIDList：{}".format(exc))
    else:
        attempts.append("SHGetNameFromIDList：這個 pywin32 沒有這個函式")

    # 3. 只對真實檔案系統有效，但那剛好就是我們要排除的東西。
    try:
        return shell.SHGetPathFromIDList(list(abs_pidl)) or ""
    except Exception as exc:      # noqa: BLE001
        attempts.append("SHGetPathFromIDList：{}".format(exc))

    raise ShellError("取不到解析名稱（都試過了）：{}".format("；".join(attempts)))


# 我們實際依賴的 Shell API。啟動時檢查一遍並寫進 log 與診斷報告 ——
# 「某個 API 其實不存在」這種錯誤，如果沒有主動檢查就會偽裝成裝置行為。
_REQUIRED_APIS = [
    ("shell.SHGetDesktopFolder", shell, "SHGetDesktopFolder"),
    ("shell.SHGetSpecialFolderLocation", shell, "SHGetSpecialFolderLocation"),
    ("shell.SHCreateItemFromIDList", shell, "SHCreateItemFromIDList"),
    ("shell.SHCreateItemFromParsingName", shell, "SHCreateItemFromParsingName"),
    ("shell.SHGetPathFromIDList", shell, "SHGetPathFromIDList"),
    ("shell.SHGetNameFromIDList", shell, "SHGetNameFromIDList"),
    ("shell.SHBindToParent", shell, "SHBindToParent"),
    ("shellcon.SIGDN_DESKTOPABSOLUTEPARSING", shellcon,
     "SIGDN_DESKTOPABSOLUTEPARSING"),
    # 如果 pywin32 有這個，SHGDFIL_DESCRIPTIONID 可以直接讀出
    # 「擁有這個節點的 namespace extension 是哪個 CLSID」——
    # 那會是區分可攜式裝置與第三方掛載最決定性的訊號。
    # 目前**只檢查存不存在**，還沒拿來用。先確認事實再決定。
    ("shell.SHGetDataFromIDList", shell, "SHGetDataFromIDList"),
    ("shellcon.SHGDFIL_DESCRIPTIONID", shellcon, "SHGDFIL_DESCRIPTIONID"),
]

# 這些不存在也不影響運作，只是備援路徑少一條。
_OPTIONAL_APIS = {
    "shell.SHGetNameFromIDList",
    "shell.SHBindToParent",
    "shellcon.SIGDN_DESKTOPABSOLUTEPARSING",
    "shell.SHGetDataFromIDList",
    "shellcon.SHGDFIL_DESCRIPTIONID",
}


def api_report():
    """回傳 [(名稱, 存不存在, 是不是必要), ...]。"""
    return [(label, hasattr(module, attr), label not in _OPTIONAL_APIS)
            for label, module, attr in _REQUIRED_APIS]


def log_api_report():
    """把 API 檢查結果寫進 log。啟動時呼叫一次。"""
    missing_required = []
    for label, present, required in api_report():
        log.info("Shell API %-42s %s%s", label,
                 "有" if present else "沒有",
                 "" if required else "（選配）")
        if required and not present:
            missing_required.append(label)
    if missing_required:
        log.error("★ 缺少必要的 Shell API：%s —— 程式可能無法正常運作",
                  "、".join(missing_required))
    return missing_required
