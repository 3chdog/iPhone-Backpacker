"""可攜式裝置（iPhone）偵測。

★ 絕對不要比對顯示名稱。
  使用者可能把手機改名成「小明的 iPhone」，英文版 Windows 顯示也不同。
  判斷依據是 Shell 屬性：**可攜式裝置是 SFGAO_FOLDER 但不是 SFGAO_FILESYSTEM**，
  而磁碟機與使用者資料夾兩者皆是。這個判斷語言中立、不受改名影響。

★ 未解鎖 / 未點「信任這部電腦」時，Shell 會把裝置內容列舉成「空的」
  而不是回報錯誤。使用者只會看到一片空白然後以為程式壞了。
  probe() 就是為了主動抓出這個狀況 —— 這是投報率最高的單一功能。
"""

import logging
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import List, Optional, Tuple

from win32com.shell import shellcon

from . import shell_ns
from .errors import BackpackerError, OperationCancelled
from .filters import MEDIA
from .listing import FileEntry, folder_has_media, is_empty, list_subfolders

log = logging.getLogger(__name__)

# iPhone 的結構是 裝置 / Internal Storage / <照片資料夾>，深度 2 就夠。
# 舊版預設 3 會多掃一整層，在 50~100 個資料夾的裝置上是數倍的代價。
DEFAULT_SCAN_DEPTH = 2


class DeviceStatus(Enum):
    OK = auto()
    NOT_FOUND = auto()
    LOCKED_OR_UNTRUSTED = auto()
    #: 有多個候選但無法確定哪一個是使用者的手機。
    #: 這時候**不猜** —— 請使用者自己在樹狀清單裡選（決策 D17）。
    AMBIGUOUS = auto()
    #: ★ 裝置找到了、而且確定是可攜式裝置，但**讀不到內容**。
    #:
    #: 為什麼要跟 NOT_FOUND 分開（2026-09-13，民眾B 案）：
    #: 16:49 那次 probe() 拿到 0x8007001E，而 worker 把它轉成 NOT_FOUND，
    #: 於是橫幅顯示「沒有自動偵測到 iPhone，請確認 USB 線接好了、手機已解鎖」——
    #: 但事實是我們**確實找到了 iPhone，還確認了它有 WPD 證據**，
    #: 只是讀不到。叫使用者去檢查線和解鎖是把他推往錯誤的方向。
    READ_FAILED = auto()


class Confidence(Enum):
    """對「這個節點是不是可攜式裝置」的把握程度。

    ★ 為什麼需要三值：`SFGAO_FOLDER 且非 SFGAO_FILESYSTEM` 只能分出
      「虛擬資料夾」與「真實檔案系統資料夾」，而可攜式裝置與第三方掛進
      「本機」的 namespace extension（CopyTrans Studio 等）同屬虛擬資料夾。
      二值判斷在原理上就分不出這兩者，只能誠實地表達「不確定」。
    """

    EXCLUDED = auto()    # 確定不是（磁碟機、使用者資料夾…）
    LIKELY = auto()      # 是虛擬資料夾，但沒有 WPD 的正面證據
    CONFIRMED = auto()   # 解析名稱裡有 WPD 的正面證據


@dataclass(frozen=True)
class Device:
    entry: FileEntry
    parsing_name: str = ""
    confidence: Confidence = Confidence.LIKELY
    reason: str = ""

    @property
    def name(self):
        return self.entry.name

    @property
    def abs_pidl(self):
        return self.entry.abs_pidl


@dataclass(frozen=True)
class Detection:
    """一次偵測的完整結果。

    ★ 刻意把 candidates 一起帶出來：偵測不確定時，UI 要能把候選清單
      列給使用者看，而不是硬挑一個然後宣稱「已連接：CopyTrans Studio」。
    """

    status: DeviceStatus
    device: Optional[Device] = None
    candidates: Tuple[Device, ...] = ()


def status_message(detection):
    """給 UI 直接顯示的訊息。core 不碰 UI，但這段文字的正確性屬於領域知識。

    ★ LOCKED_OR_UNTRUSTED 的措辭很重要。實測的狀態變化是：

      1. 沒插手機          → 「本機」底下沒有 Apple iPhone
      2. 插入手機          → 出現 Apple iPhone 與 Internal Storage，但裡面是空的
      3. 不理會 / 點不允許 → 同 2
      4. **點了允許之後，仍然會維持 1~2 分鐘的空狀態**
      5. 再過一陣子        → Internal Storage 底下才出現資料夾

      第 4 步是關鍵：使用者已經按了「信任」，畫面卻還是叫他去按「信任」，
      會讓人以為程式壞了或自己按錯。訊息一定要涵蓋「已經按過了，請再等一下」。
      這段期間連檔案總管也看不到資料夾，所以不是本程式的問題。

    ★ 每一種狀態都要讓使用者知道「還是可以自己在左邊展開手機來備份」——
      自動偵測只是輔助，失敗或不確定都不該擋住他。
    """
    status = detection.status
    name = detection.device.name if detection.device is not None else None

    if status is DeviceStatus.OK:
        if detection.device is not None and \
                detection.device.confidence is Confidence.LIKELY:
            return ("已連接：{}\n"
                    "（無法百分之百確定這是手機。如果左邊清單裡有別的裝置才是你的手機，"
                    "直接展開那一個就好。）".format(name))
        return "已連接：{}".format(name)

    if status is DeviceStatus.AMBIGUOUS:
        listed = "、".join("**{}**".format(d.name) for d in detection.candidates)
        return ("偵測到多個可能的裝置：{}\n"
                "\n"
                "無法判斷哪一個是你的手機，所以不亂猜。\n"
                "**請直接在左邊的清單裡展開你的手機** —— 自動偵測只是輔助，"
                "不影響瀏覽與備份。\n"
                "\n"
                "（其他項目可能是別的軟體掛在「本機」底下的，例如手機管理工具。）"
                .format(listed))

    if status is DeviceStatus.READ_FAILED:
        # ★ 這段訊息刻意**不提**「請確認 USB 線接好、手機已解鎖」——
        #   我們明明已經找到裝置了，叫使用者去檢查那些只會讓他白忙。
        #   真正有效的動作是「重新整理裝置」（會重建連線），其次才是重插。
        return ("找到「{}」了，但**讀不到裡面的內容**。\n"
                "\n"
                "這通常是裝置連線進入了異常狀態，**不是**你的手機或線材壞了。\n"
                "\n"
                "請依序試：\n"
                "1. 按一次 **「重新整理裝置」**（會重新建立連線，多半這樣就好）\n"
                "2. 還是不行 → 把 USB 線拔掉重插，等十幾秒再按一次\n"
                "3. 如果電腦上裝了會存取 iPhone 的其他軟體"
                "（手機管理／備份工具之類），請先把它完全關閉\n"
                "\n"
                "如果一直是這樣，請按「產生診斷報告」並把檔案回報給開發者。"
                .format(name or "裝置"))

    if status is DeviceStatus.NOT_FOUND:
        # ★ 措辭很重要：自動偵測失敗**不代表不能用**。
        #   左邊的樹狀清單走的是另一條路（純列舉，不看屬性），
        #   就算偵測失敗也照樣展得開、備份得了。
        #   訊息絕對不能讓使用者以為「偵測不到 = 沒救了」。
        return ("沒有自動偵測到 iPhone。\n"
                "\n"
                "請確認：USB 線接好了、手機已解鎖、並且在手機上點過"
                "「信任這部電腦」，然後按「重新整理裝置」。\n"
                "\n"
                "**如果左邊的清單裡看得到你的手機，可以直接展開它使用** ——"
                "自動偵測只是輔助，失敗不影響瀏覽與備份。")

    return ("偵測到「{}」，但還讀不到裡面的內容。可能是下列其中一種情況：\n"
            "\n"
            "1. iPhone 還沒解鎖 → 請解鎖手機。\n"
            "2. 還沒點「信任這部電腦」→ 請看手機畫面並點「信任」。\n"
            "3. **已經點過「信任」了** → 手機準備資料還需要一到兩分鐘，"
            "有時候更久。請稍等一下再按「重新整理裝置」。\n"
            "\n"
            "（第 3 種情況下，用 Windows 檔案總管進去看也是空的，"
            "這是正常現象，不是程式出問題。）".format(name or "裝置"))


def find_portable_devices():
    """列出「本機」底下所有**可能是**可攜式裝置的節點。

    ★★ 兩層判斷（見 `_classify`）：

      `CONFIRMED` —— 解析名稱裡有 WPD 的正面證據（裝置介面路徑或 WPD 介面 GUID）
      `LIKELY`    —— 是虛擬資料夾，但拿不到正面證據

      **為什麼不能只用 `SFGAO_FOLDER 且非 SFGAO_FILESYSTEM`**：那組旗標只能分出
      「虛擬資料夾」與「真實檔案系統資料夾」，而可攜式裝置與第三方掛進「本機」的
      namespace extension **同屬虛擬資料夾**。實測（2026-08-30，民眾B）：

          CopyTrans Studio   attrs = 0x20000000   ← 與 iPhone 完全相同
          Apple iPhone       attrs = 0x20000000

      舊版因此把 CopyTrans Studio 當成手機，橫幅顯示「已連接：CopyTrans Studio」。

    ★ 全程不看顯示名稱。使用者可以把手機改成任何名字。

    回傳的順序是 Shell 的列舉順序，**沒有排序**，呼叫端不應該依賴它。
    """
    this_pc = shell_ns.this_pc_pidl()
    devices: List[Device] = []
    diagnostics = []

    # ★ 「本機」這一層也要記可信度。民眾B 15:55 那次 Apple iPhone 沒出現在
    #   候選裡，其中一個假設就是這一層的列舉被靜默截斷了（CopyTrans 是第 5 項、
    #   Apple iPhone 是第 9 項）。沒有這份紀錄就永遠無法證實或推翻。
    enum_report = shell_ns.EnumReport()
    for child_abs, name, attrs, parsing in shell_ns.iter_entries(
        this_pc, flags=shell_ns.EVERYTHING,
        want_attributes=True, want_parsing=True, report=enum_report,
    ):
        confidence, reason = _classify(parsing, attrs)
        diagnostics.append((name, parsing, attrs, confidence, reason))

        if confidence is Confidence.EXCLUDED:
            continue

        entry = FileEntry(name=name, is_dir=True, abs_pidl=child_abs)
        devices.append(Device(entry=entry, parsing_name=parsing or "",
                              confidence=confidence, reason=reason))
        log.info("候選裝置：%s（%s：%s）", name, confidence.name, reason)

    # ★★ 一律把每一個節點的判定寫進 log，不再只在「零候選」時才寫
    #   （2026-09-13）。舊版只在 `not devices` 時 dump，結果民眾B 的
    #   15:55 那次**只看到 CopyTrans Studio、Apple iPhone 整個消失**，
    #   而因為候選數不是 0，我們完全沒有留下任何可以回答「iPhone 去哪了」
    #   的紀錄。被排除的節點本身就是證據，不能只在全軍覆沒時才記。
    log.info("「本機」底下共 %d 個節點，其中 %d 個是候選裝置（列舉：%s）：",
             len(diagnostics), len(devices), enum_report.describe())
    for name, parsing, attrs, confidence, reason in diagnostics:
        log.info("    %-24s %-9s attrs=%s parsing=%s（%s）",
                 name, confidence.name, shell_ns.describe_attributes(attrs),
                 "None（讀不到）" if parsing is None
                 else (parsing or "（空字串）"),
                 reason)
    if not devices:
        log.warning("沒有偵測到可攜式裝置 —— 判斷依據見上面那份清單。")

    return devices


def _classify(parsing, attrs):
    r"""判斷一個「本機」底下的節點是不是可攜式裝置。

    ★ 這個 docstring 前面有 r 前綴（raw string），因為內文寫到了磁碟機路徑。
      非 raw 字串裡「反斜線 + 空白」是無效的跳脫序列：Python 3.12 只發
      SyntaxWarning（打包 log 裡看得到），但未來版本會直接變成 SyntaxError。

    回傳 (Confidence, 判斷依據的文字說明)。文字會寫進 log 與診斷報告，
    收到災情回報時才知道是哪一條規則做的決定。

    參數的 None 代表**取不到**，跟空字串或 0 不一樣，不可混用。

    判斷順序（由可靠到次要）：
      ① 解析名稱是 C:\ 或 UNC          → EXCLUDED
      ② 屬性有 SFGAO_FILESYSTEM        → EXCLUDED
      ③ 屬性明確沒有 SFGAO_FOLDER      → EXCLUDED
      ④ 解析名稱有 WPD 正面證據         → CONFIRMED
      ⑤ 是虛擬資料夾但沒有正面證據       → LIKELY
      ⑥ 兩個訊號都取不到                → EXCLUDED（不知道不等於是裝置）
    """
    # ① 最可靠的排除依據。
    if parsing and shell_ns.looks_like_filesystem_path(parsing):
        return Confidence.EXCLUDED, "解析名稱是檔案系統路徑"

    if attrs is not None:
        # ② 對應到真實檔案系統 → 磁碟機或使用者資料夾。
        if attrs & shell_ns.SFGAO_FILESYSTEM:
            return Confidence.EXCLUDED, "SFGAO_FILESYSTEM 已設定（對應到真實檔案系統）"
        # ③ 連資料夾都不是。
        if not (attrs & shell_ns.SFGAO_FOLDER):
            return Confidence.EXCLUDED, "不是資料夾節點"

    # ④ 正面證據 —— 這是唯一能把可攜式裝置跟第三方掛載分開的訊號。
    if shell_ns.looks_like_portable_device(parsing):
        return Confidence.CONFIRMED, "解析名稱含 WPD 裝置介面（確定是可攜式裝置）"

    # ⑤ 是虛擬資料夾，但可能是第三方掛進「本機」的東西。
    if attrs is not None:
        return Confidence.LIKELY, "是虛擬資料夾，但沒有 WPD 證據（也可能是其他軟體掛載的）"
    if parsing:
        return Confidence.LIKELY, "屬性讀不到，解析名稱不是檔案系統路徑，也沒有 WPD 證據"

    # ⑥ 兩個訊號都沒有 → 我們就是不知道。不知道不等於是裝置。
    return Confidence.EXCLUDED, "屬性與解析名稱都取不到，無法判斷"


def probe(device):
    """裝置內容讀不讀得到。

    iPhone 鎖定或未信任時，Shell 會把 Internal Storage 列成空的而不報錯，
    所以要實際往下探一層才知道。

    ★★ 「空的」有兩種，訊息完全不同，絕不能混（2026-09-13）：

        真的空 → 手機沒解鎖 / 沒點信任 → LOCKED_OR_UNTRUSTED
        讀不到 → 裝置連線出問題       → READ_FAILED

      舊版兩種都回 LOCKED_OR_UNTRUSTED，於是連線壞掉時會叫使用者
      去按一個他早就按過的「信任」。現在靠 EnumReport 分開。
    """
    report = shell_ns.EnumReport()
    try:
        # ★ 這裡用 verify_empty=True，跟一般的瀏覽路徑不同。
        #   成本是「只有在結果為 0 時才多列舉一次、而且只有一個節點」，
        #   可以忽略；但這個 0 決定的是要不要叫使用者「去手機上按信任」。
        #   叫一個早就按過信任的人再去按一次，比不講話還糟。
        storages = list_subfolders(device.abs_pidl, report=report,
                                   verify_empty=True)
    except BackpackerError as exc:
        log.warning("裝置「%s」的儲存區列不出來：%s", device.name, exc)
        return DeviceStatus.READ_FAILED

    if not storages:
        if report.suspicious_zero:
            log.warning("裝置「%s」的儲存區清單可疑，不採信這個 0：%s",
                        device.name, report.describe())
            return DeviceStatus.READ_FAILED
        log.warning("裝置「%s」底下沒有任何儲存區 —— 可能未解鎖或未信任（%s）",
                    device.name, report.describe())
        return DeviceStatus.LOCKED_OR_UNTRUSTED

    suspicious = False
    for storage in storages:
        sub = shell_ns.EnumReport()
        try:
            empty = is_empty(storage.abs_pidl, report=sub)
        except BackpackerError as exc:
            log.warning("裝置「%s」的儲存區「%s」讀取失敗：%s",
                        device.name, storage.name, exc)
            return DeviceStatus.READ_FAILED
        if not empty:
            return DeviceStatus.OK
        if sub.suspicious_zero:
            suspicious = True
            log.warning("儲存區「%s」的 0 很可疑：%s", storage.name, sub.describe())

    if suspicious:
        return DeviceStatus.READ_FAILED
    log.warning("裝置「%s」的儲存區全部是空的 —— 可能未解鎖或未信任", device.name)
    return DeviceStatus.LOCKED_OR_UNTRUSTED


def detect():
    """找裝置並判斷狀態。回傳 `Detection`。

    ★★ 挑選規則（決策 D17）：

        有 CONFIRMED → 只在 CONFIRMED 裡挑
                         1 個 → 用它
                        >1 個 → AMBIGUOUS，請使用者自己選
        只有 LIKELY  → probe 一遍
                        剛好 1 個讀得到內容 → 用它
                        其他情況            → AMBIGUOUS

      **分不出來的時候不猜。** 舊版「挑第一個 probe 成功的」在民眾B 的機器上
      挑到了 CopyTrans Studio —— 它不是空殼，真的有內容，所以 probe 擋不住。
      硬挑一個然後宣稱「已連接：CopyTrans Studio」比誠實說「有這幾個，請你選」
      糟糕得多；何況樹狀清單本來就全部列出來，使用者自己展開就能備份。
    """
    candidates = tuple(find_portable_devices())
    if not candidates:
        return Detection(DeviceStatus.NOT_FOUND, None, ())

    confirmed = [d for d in candidates if d.confidence is Confidence.CONFIRMED]

    if len(confirmed) == 1:
        device = confirmed[0]
        log.info("採用「%s」：有 WPD 正面證據", device.name)
        return Detection(probe(device), device, candidates)

    if len(confirmed) > 1:
        log.info("有 %d 個確定的可攜式裝置，無法判斷哪一個是使用者要的",
                 len(confirmed))
        return Detection(DeviceStatus.AMBIGUOUS, None, candidates)

    # 沒有任何一個拿得到正面證據 —— 可能是解析名稱讀不到，
    # 也可能全都是第三方掛載。用「讀不讀得到內容」再篩一次。
    if len(candidates) == 1:
        device = candidates[0]
        log.info("只有一個候選「%s」，採用（未確認是可攜式裝置）", device.name)
        return Detection(probe(device), device, candidates)

    readable = [d for d in candidates if probe(d) is DeviceStatus.OK]
    if len(readable) == 1:
        device = readable[0]
        log.info("採用「%s」：候選中只有它讀得到內容（未確認）", device.name)
        return Detection(DeviceStatus.OK, device, candidates)

    log.info("有 %d 個候選、其中 %d 個讀得到內容，無法判斷哪一個是使用者要的",
             len(candidates), len(readable))
    return Detection(DeviceStatus.AMBIGUOUS, None, candidates)


def find_photo_folders(root, categories=MEDIA, *,
                       max_depth=DEFAULT_SCAN_DEPTH, max_folders=None,
                       cancel=None, cache=None, progress=None):
    """從指定節點往下遞迴，收集「含有目標類型檔案的資料夾」。

    ★ 這是取代舊版 100APPLE ~ 105APPLE 序號展開的功能。
      iOS 改版曾把結構從 DCIM\\100APPLE 改成 DCIM\\202510_a 再改成直接放在
      Internal Storage 底下 —— 遞迴掃描對這三種都有效，不必跟著改 code。

    ★★ 這是**慢**操作，不能當成預設動作。
      實測（2026-08-27）：MTP 每次列舉有 ~45ms 固定開銷，而一支有 50~100 個
      資料夾的 iPhone 光是展開 Internal Storage 就要 2.7 秒。
      每個資料夾要 folder_has_media（列檔案）+ list_subfolders（列資料夾）
      兩次列舉，總量很容易爆掉 —— 初版沒有進度回報也沒有上限，實測跑了
      20 分鐘沒有任何反應。

    所以這支函式現在強制要求呼叫端提供進度與取消的能力：
      progress(visited, found, current_name)  每處理完一個資料夾呼叫一次
      cancel() -> bool                        回 True 就中止
      max_folders                             走訪上限，超過就停（測試/保險用）

    ★ 先列檔案再列資料夾是刻意的：folder_has_media() 找到第一個符合的就早退，
      對「裡面全是照片」的資料夾只需要讀到第一筆。
    """
    root_pidl = root.abs_pidl if isinstance(root, (Device, FileEntry)) else root
    found: List[FileEntry] = []
    visited = 0
    stopped_early = False

    def check_cancel():
        if cancel is not None and cancel():
            raise OperationCancelled("使用者取消掃描")

    def walk(abs_pidl, depth):
        nonlocal visited, stopped_early
        if stopped_early:
            return
        check_cancel()
        if max_folders is not None and visited >= max_folders:
            stopped_early = True
            log.warning("達到 max_folders=%d 上限，掃描提前結束", max_folders)
            return

        visited += 1
        try:
            name = shell_ns.display_name(abs_pidl)
        except Exception:   # noqa: BLE001
            name = "?"

        if folder_has_media(abs_pidl, categories):
            found.append(FileEntry(name=name, is_dir=True, abs_pidl=abs_pidl))
            log.debug("找到照片資料夾：%s", name)

        if progress is not None:
            progress(visited, len(found), name)

        if depth >= max_depth:
            return
        for sub in list_subfolders(abs_pidl, cache):
            walk(sub.abs_pidl, depth + 1)

    walk(tuple(root_pidl), 0)
    log.info("掃描%s：走訪 %d 個資料夾，找到 %d 個含有媒體檔的資料夾",
             "中止" if stopped_early else "完成", visited, len(found))
    return found
