# 03 — 決策紀錄

最後更新：2026-08-26

**已定案的方向不要重新提案。要推翻請在這裡新增一筆並註明理由。**

---

## D1 — 留在 Python，不改寫成 C#  ✅ 定案

**背景**：MTP/PTP 存取在 C# 有現成的 WPD 封裝套件，Python 要自己刻。

**決策**：留在 Python。

**理由**：專案**已經**用 `IShellFolder` + `IFileOperation` 打通 MTP 存取，
最難的一關已過。為了 MTP 而重寫成 C# 的理由不存在，重寫只是把驗證過的東西再踩一次雷。

---

## D2 — 放棄 Windows 7  ✅ 定案（使用者決定）

**背景**：原 README 標示支援 Windows 7 / 10 / 11。

**衝突**：
- **Qt 6 官方只支援 Windows 10 以上**，Win7/8 完全不支援
- **Python 3.8 是最後一個支援 Windows 7 的版本**，3.9 起要 Win8.1+

所以「支援 Win7」與「Python 3.12 + PySide6」互斥。

**決策**：**放棄 Win7**，目標平台 Windows 10 / 11，技術棧 Python 3.12 + PySide6 (Qt6) + 最新 pywin32。

**理由**：Win7 已 EOL 六年；新版 iPhone 在 Win7 上跑 Apple Devices 驅動本來就不穩；
為了它把整個技術棧凍在 2020 年，代價太大（連帶防毒誤判率也較高）。

**待辦**：README 的平台標示要改（階段 6）。

---

## D3 — GUI 用 PySide6 + `QTreeWidget`  ✅ 定案

**決策**：PySide6（LGPL，商業使用較單純；PyQt 是 GPL/商業雙授權）。
用 `QTreeWidget`（item-based），不用 model/view 的 `QTreeView`。

**★ 絕對不要用 `QFileSystemModel`** —— 它只認真實檔案系統路徑，**看不到 iPhone**。這是很多人會撞的第一面牆。

**理由**：`QTreeWidget` 撐幾萬個 item 沒問題，PIDL 直接塞 `setData(0, Qt.UserRole, pidl)`。
真的遇到效能瓶頸再換自訂 `QAbstractItemModel`。先求有。

---

## D4 — v1 只做「資料夾層級勾選」  ✅ 定案（使用者決定）

**決策**：v1 不做檔案層級逐張勾選。

**擴充性評估**：**不會卡住後續發展**，前提是核心邊界設計成「吃一份檔案清單」而非「吃一個資料夾」。
這件事本來就非做不可（是修 `getFilteringSignals` zip bug 的正解），等於免費拿到擴充性。
具體三條保險見 `01-architecture.md` 的「擴充性保險」。

**v2 真正會變麻煩的**：列舉成本被搬進互動路徑（點資料夾當下就要列舉幾千檔）。
但所需的背景執行緒 + 延遲載入，v1 的樹狀延遲展開本來就要蓋。

---

## D5 — 不要比對 Shell 顯示名稱  ✅ 定案

**背景**：既有 code 有 `ChineseCharacterChecking()`（`"Desktop"` → `"桌面"`）和 `rootName="本機"`。

**問題**：英文版 Windows 是 `"This PC"`、日文版是 `"PC"`，一給別人就爆炸。
使用者把 iPhone 改名成「小明的 iPhone」也會失效。

**決策**：一律用 CSIDL / PIDL / `SHGDN_FORPARSING` 判斷。
裝置偵測改成「從 `CSIDL_DRIVES` 底下找出 `SHGDN_FORPARSING` 不符合 `X:\` 格式的節點」。

**附帶好處**：這也順便解掉「iOS 改版導致資料夾結構改變」的問題 —— 不再依賴任何寫死的路徑深度。

---

## D6 — 不碰 `IFileOperationProgressSink`  ✅ 定案

**背景**：使用者希望有「被略過的檔案」的 log。

**決策**：不實作 progress sink（pywin32 對它的支援不完整，要自己寫 COM gateway）。
改用 **`FOF_NOERRORUI` 靜默略過 + 分批執行 + 事後驗證掃描**。

**理由**：驗證掃描（比對來源選取清單 vs 目的地實際檔名/大小）同時給出失敗清單、
真實進度、以及「重試失敗項目」的基礎，成本遠低於刻 COM gateway。

---

## D8 — 瀏覽效能是第一優先，瀏覽路徑不碰檔案  ✅ 定案（使用者指定）

**背景**：使用者做這個工具的**原始動機**就是「用檔案總管備份 iPhone 太慢」——
點進資料夾要 load 很久、檔案多到看不完、直接複製整個資料夾又不穩定容易失敗。

**要求**：點開/勾選資料夾，**0.5 秒內要有畫面反應**。

**這個決策推翻了 `01-architecture.md` 原本的一項設計** ——
原本右側面板要顯示「檔案數、各分類數量、預估大小」，
那會強迫在瀏覽時就列舉檔案，直接違反上述要求。已改為背景非同步、不阻塞。

**決策**：把列舉切成兩條路徑，見 `01-architecture.md` 的「效能契約」。

- 瀏覽路徑（互動）：只 `SHCONTF_FOLDERS`，不取 details、不取縮圖、結果進快取
- 複製路徑（背景）：按下備份後才展開檔案，streaming pipeline，有進度

**關鍵洞察**：檔案總管在 MTP 上慢的主因是**縮圖**（每張圖都要抓下來解碼）
與**每項一次來回的 `GetDetailsOf`**。使用者一開始就說「不需要顯示縮圖」——
這個需求的價值比表面看起來大得多，它直接砍掉了檔案總管慢的主因。

**未驗證項**：`SHCONTF_FOLDERS` 在 iPhone MTP shell extension 上是否真的能省下走訪全部項目，目前未知。
**階段 2 要先量測再談優化**，不要用猜的。

---

## D9 — 複製的排程單位是「檔案」，不是「資料夾」  ✅ 定案

**背景**：使用者實際經驗是「直接複製整個資料夾容易出問題、常常有檔案無法複製、十分不穩定」，
後來改成點進去框選圖檔才穩定，但那樣很慢很累。

**決策**：即使 v1 的 UI 是資料夾層級勾選，**送進 `IFileOperation` 的排程單位仍是逐個檔案**。

**理由**：整包丟給 shell 遞迴雖然快一點，但失敗時完全拿不到「是哪個檔案失敗」，
正是使用者遇到的不穩定情況。逐檔排程才換得到失敗清單、重試能力與增量去重。

**代價**：列舉檔案要時間。但這發生在按下備份之後的背景執行緒，不在互動路徑上（見 D8）。

---

## D12 — 一個來源資料夾 = 一次 `IFileOperation`  ✅ 定案（由使用者實測經驗決定）

**背景**：使用者早期用 `copyShellItem()` 逐檔各開一次操作，結果
「**超級慢**，而且 Windows 的原生進度視窗會反覆彈出**」，
後來才改成 `copyShellItem_batch()` 一次排程整個資料夾。

**這推翻了本專案 `run_copy()` 原本的 chunk 設計。**
原本每 200 檔送一次 `PerformOperations()`，那是同一個問題的縮小版：
- 每次 `PerformOperations()` 有約 **600 ms 固定開銷**
  （smoke_local 實測：複製 4 個檔案花 598.6 ms）
- 一個 5000 張的資料夾切 200 一批 → **25 次進度視窗 + 15 秒純浪費**

**決策（2026-08-27 修訂）**：**一整個備份任務只用一次 `IFileOperation`。**

第一版改成「一個資料夾一次操作」，但實測選 5 個資料夾就跳 5 次進度視窗。
`IFileOperation` 允許同一次操作裡每個 `CopyItem` 帶**不同的目的地**，
所以不管幾個資料夾都只需要一次 `PerformOperations()`。`chunk_size` 參數已移除。

代價：所有資料夾的列舉都要先完成才會開始複製。
但列舉階段有我們自己的進度回報，而且列舉本來就比複製快得多。

**那進度怎麼辦？** 拆成三段，各自有合適的進度來源：

| 段 | 誰負責進度 |
|---|---|
| 1. 列舉 + 過濾 + 去重 | **我們自己**回報「已找到 N 個」。MTP 列舉 ~3.4 ms/項，5000 張要十幾秒，不回報會被當成當掉 |
| 2. 複製 | **IFileOperation 的原生進度視窗**（逐檔進度、剩餘時間、取消鈕），只出現一次，品質比自己畫的高 |
| 3. 驗證掃描 | 本機 `os.scandir`，很快 |

**注意**：這不違反 D9。排程單位仍然是「檔案」（逐個 `CopyItem`），
改變的只是「幾個 `CopyItem` 共用一次 `PerformOperations()`」。
失敗清單與增量去重都還在。

---

## D16 — 「重試」就是重跑同一個任務  ✅ 定案

**背景**：階段 5 規劃了「重試失敗項目」按鈕。

**決策**：不需要特別的重試機制。**增量去重會自動跳過已經複製好的檔案**，
所以重跑同一個備份任務，實際上就只會複製沒完成的那些。

完成對話框在有 `failed` 或 `cancelled` 時多出一顆「重試未完成的項目」，
按下去就是拿 `self._last_job`（來源、目的地、分類）再跑一次。

**附帶說明**：對話框會分開提示兩種情況 ——
- `cancelled`：還沒輪到，重試會接續
- `failed`：這次沒複製成功，**重試通常就會成功**

**★ 關於 `failed` 的措辭，實測修正過一次。**
原本寫「重試通常無效，多半是檔案本身有問題」，但 2026-08-28 的實測打臉：

```
02:04:41  複製失敗：201711__\IMG_6004.JPG
          複製失敗：201711__\IMG_2951.JPG
02:04:51  使用者要求重試未完成的項目
02:04:56  備份完成：複製 2 個、跳過 320 個（已存在）
```

那 2 個檔案**重試一次就成功了** → 這類失敗多半是 MTP 傳輸的暫時性問題，
不是檔案本身壞掉。措辭改成鼓勵重試。

---

## D17 — 分不出哪一台是手機時，不猜，請使用者自己選  ✅ 定案（由實測決定）

**背景**：民眾B（2026-08-30）的電腦上裝了 CopyTrans Studio，它把自己掛進
「本機」的 shell namespace。程式抓到它，橫幅顯示「已連接：CopyTrans Studio」。

**根因是訊號解析度不足，不是實作有 bug。** 查證後確認：

> `SFGAO_FOLDER` 且非 `SFGAO_FILESYSTEM` 只能分出「虛擬資料夾」與
> 「真實檔案系統資料夾」。**可攜式裝置與第三方掛進「本機」的
> namespace extension 同屬虛擬資料夾。**
> —— [The Old New Thing, 2017-11-01](https://devblogs.microsoft.com/oldnewthing/20171101-00/?p=97325)

實測佐證，兩者的屬性**完全相同**：

```
CopyTrans Studio   attrs = 0x20000000
Apple iPhone       attrs = 0x20000000
```

**決策**：

1. **新增正面證據**（`shell_ns.looks_like_portable_device`）——
   WPD 裝置的解析名稱裡一定有裝置介面路徑 `\\?\` 或
   `GUID_DEVINTERFACE_WPD = {6AC27878-A6FA-4155-BA85-F98F491D4F33}`；
   第三方 namespace extension 是 `::{自己的 CLSID}`，不會有。
   （[Microsoft Learn](https://learn.microsoft.com/en-us/windows-hardware/drivers/install/guid-devinterface-wpd)）

2. **判斷改成三值**：`CONFIRMED`（有正面證據）／`LIKELY`（是虛擬資料夾但
   沒證據）／`EXCLUDED`。這是「有證據才升級」，**不是**「沒證據就排除」——
   取不到解析名稱時仍可能是裝置，只是無法確認。

3. **★ 分不出來時不猜。** 有多個 `CONFIRMED`、或只有多個 `LIKELY` 且不只一個
   讀得到內容 → `DeviceStatus.AMBIGUOUS`，把候選清單列給使用者，
   請他自己在樹狀清單裡展開。

**為什麼「挑第一個 probe 成功的」不夠**：那個保險只能擋掉空殼。
CopyTrans Studio **真的有內容**（`Photo library` 底下有 Albums、Camera roll…），
所以 probe 完全擋不住。硬挑一個然後宣稱「已連接：CopyTrans Studio」，
比誠實說「有這幾個，請你選」糟糕得多 —— 何況樹狀清單本來就全部列出來，
使用者自己展開就能備份。

**沒有採用的方向**：用 `Internal Storage`、`DCIM` 這類名稱來判斷。
那還是在比對字串，只是換一個字串，違反 D5。

---

## D18 — 目的地資料夾名稱一律消毒  ✅ 定案

**背景**：使用者可以在樹狀清單裡選**任何**節點，不只是手機的照片資料夾。

**問題**：虛擬節點的顯示名稱**不受檔案系統的命名限制**。
勾到「Local Disk (C:)」時，`plan.dest_dir / source.name` 會拿含冒號的名字去
`mkdir()`，直接拋 `OSError`。

**決策**：新增 `core/naming.py` 的 `safe_folder_name()`，處理非法字元、
控制字元、結尾的句點與空白、Windows 保留字（CON/PRN/COM1…）、過長名稱。

`core/naming.py` **刻意不 import pywin32**，所以在 Linux 上也能跑真正的測試
（不是對照實作）。

---

## D15 — 取消時「還沒輪到的檔案」不算失敗  ✅ 定案（由實測發現）

**背景**：實測 log

```
使用者取消了複製（COPYENGINE_E_USER_CANCELLED）
備份已取消：複製 11 個、跳過 0 個（已存在）、失敗 243 個
```

那 243 個檔案**根本沒被嘗試過**，只是排在後面還沒輪到就被取消了。
把它們算成「失敗」會讓 UI 列出一份幾百筆的假失敗清單，非常嚇人。

**決策**：`CopyReport` 新增 `cancelled` 欄位。
驗證掃描時，若 `aborted` 為真，沒落地的檔案一律歸到 `cancelled` 而非 `failed`。

**★ 2026-08-28 修訂 —— 第一版矯枉過正了。**

只看 `GetAnyOperationsAborted()` 會把「檔案複製失敗被靜默略過」也判成取消，
等於把真正的失敗藏起來 —— 那正是本專案要解決的問題之一。實測證據：

```
00:54:19  使用者取消了複製（COPYENGINE_E_USER_CANCELLED）   ← 有這行
          複製 58 個、跳過 46 個、尚未複製 47 個（已取消）      ← 正確

00:54:53  開始複製：160 個檔案，來自 2 個資料夾
00:55:14  複製 158 個、尚未複製 2 個（已取消）                ← **錯誤**
          （前面沒有取消那一行 → 那 2 個是真的失敗）
```

**兩條路徑可以分辨**：

| `PerformOperations()` | `GetAnyOperationsAborted()` | 意義 | 沒落地的檔案算 |
|---|---|---|---|
| 拋 `COPYENGINE_E_USER_CANCELLED` | — | 使用者取消 | `cancelled` |
| 正常返回 | `True` | 有項目被 `FOF_NOERRORUI` 靜默略過 | **`failed`** |
| 正常返回 | `False` | 一切正常 | `failed`（理論上不該發生） |

`_perform()` 因此回傳 `(user_cancelled, any_aborted)` 兩個值，
只有 `user_cancelled` 才會把沒落地的檔案歸到 `cancelled`。

**仍存在的取捨**：使用者取消時，`IFileOperation` 不告訴我們處理到第幾個，
所以那時仍無法分辨「真的失敗」與「還沒輪到」，一律當成後者。
但那是**取消**的情境，使用者本來就知道自己中斷了，不會被誤導。

UI 明說：「已經複製完成的檔案會保留，直接再按一次『開始備份』就能接續。」

---

## D13 — `COPYENGINE_E_USER_CANCELLED` 是正常結果  ✅ 定案（由實測決定）

**背景**：使用者在 GUI 上關掉 Windows 的複製進度視窗（或按取消）後，
log 出現嚇人的 traceback：

```
pywintypes.com_error: (-2144927744, 'OLE error 0x80270000', None, None)
ShellError: 複製作業失敗：(-2144927744, ...)
```

**原因**：`0x80270000` = `COPYENGINE_E_USER_CANCELLED`。
`PerformOperations()` 在使用者取消時就是回這個 HRESULT。
原本的程式碼一律當成錯誤往上拋，所以「使用者主動取消」被顯示成「備份失敗」。

**決策**：`_perform()` 明確判斷這個 HRESULT，回傳 `aborted=True` 而不是拋例外。
UI 顯示「備份已取消」，並說明**已完成的檔案會保留，下次備份會自動跳過**。

**衍生**：使用者也觀察到「取消一個複製到隨身碟的大任務」時，
`PerformOperations()` 會阻塞很久（Windows 自己在收尾）。
那段期間 worker 執行緒被卡住，其他操作都會排隊。
→ UI 的忙碌訊息要講清楚「備份進行中，期間點選資料夾不會計算檔案數」，
  取消時顯示「正在取消…如果 Windows 的複製視窗還開著，請在那裡按取消」。
  這是單一 worker 執行緒的必然代價，換來的是不會有並行存取 MTP 的風險。

---

## D14 — 檔案數要能批次計算  ✅ 定案（使用者需求）

**背景**：使用者想快速知道「Internal Storage 底下所有資料夾」或
「最新十個資料夾」各有幾個檔案，但原本只有「停在某個節點 400 ms」才會算一個。
實測批次計算時 CPU 只到 33-37%，算完落回 15-20%，**不是效能問題**。

**決策**：
- 樹狀改成 `ExtendedSelection` —— 可以滑鼠拖曳框選、Ctrl / Shift 複選
- 新增「計算檔案數」按鈕：算選取的；沒選取就算目前節點底下所有已載入的
- 新增「停止計算」按鈕
- worker 新增 `count_files_batch()`，**序列執行**、每算完一個就 emit，
  數字會一個一個填上去，而不是等到全部算完

**為什麼不並行**：MTP 不適合並行存取。序列已經夠用 ——
瓶頸是每項 ~3.4 ms 的 I/O，不是 CPU。

---

## D10 — 樹狀展開一律非同步；旗標不是效能手段  ✅ 定案（由實測決定）

**背景**：2026-08-27 在一支有 184 個資料夾的 iPhone 上實測。

**實測結論**：
1. **`SHCONTF_FOLDERS` 是事後過濾，不會減少 Shell 的工作量。**
   要求「只列檔案」、回傳 0 項，卻仍花了 1544 ms（完整列舉 184 項是 1556 ms）。
2. 展開 `Internal Storage` 要 **1.5 ~ 2.5 秒**，這個數字**砍不掉**。
3. 裝置偵測總成本約 **1.9 秒**（列舉本機 + find_portable_devices + probe）。
4. 列舉「本機」的 ~770 ms 與 iPhone 無關（沒插也是 772.7 ms）。

**決策**：
- **樹狀節點展開一律走背景執行緒 + 骨架畫面**（節點先出現，內容稍後補），
  絕不同步阻塞 UI。0.5 秒目標對「展開一個 184 項的節點」不可能達成，
  能達成的是「**0.5 秒內有畫面反應**」——  這才是真正該守的契約。
- **啟動時不同步等裝置偵測**，先畫視窗與「正在偵測裝置…」狀態。
- `list_subfolders()` 仍用 `FOLDERS_ONLY`（語意正確、回傳量小），
  但**不要把它當成效能手段**。
- **`NamespaceCache`（session 內）是 v1 的解法**：一個 session 只付一次 2.5 秒。
- **跨 session 的磁碟快取延後到 v1.1 再評估**。它能讓第二次啟動立刻顯示樹，
  但要處理失效與陳舊顯示，複雜度不低。先看 v1 實際用起來 2.5 秒煩不煩。

---

## D11 — `find_photo_folders` 不是預設動作  ✅ 定案（由實測決定）

**背景**：初版對每個資料夾做兩次列舉，在 184 個資料夾的裝置上跑 20 分鐘沒結束。

**成本模型（實測）**：單次列舉 ≈ `40 ms + 8.2 ms × 項目數`。
一個 5000 張照片的資料夾光列舉就要約 **41 秒**。

**決策**：
- 這是**選配的慢操作**，必須有進度回報、可取消、有走訪上限。
- **預設的使用流程不需要它** —— 對 iPhone 來說 `Internal Storage` 的子資料夾
  就是照片資料夾，直接列出來讓使用者挑即可（一次 2.5 秒，之後走快取）。
- 掃描深度預設 2（`裝置 / Internal Storage / 照片資料夾`）。

---

## D7 — 打包用 PyInstaller `--onedir`  ✅ 定案

**決策**：`--onedir` 再壓成 zip 發布，**不要 `--onefile`**。

**理由**：onefile 執行時會解壓到 temp 再執行，這在啟發式偵測眼中就是標準的惡意軟體脫殼特徵，
誤判率高非常多。若誤判仍嚴重，再考慮 Nuitka（編成 C，特徵完全不同）。

**相關**：使用者原本擔心的「防火牆問題」不存在 —— 本程式不連網路，不會跳防火牆提示。
真正擋人的是 SmartScreen（未簽章必跳）與防毒誤判。
簽章憑證 OV 一年約台幣一萬且強制硬體金鑰/雲端 HSM，對免費工具不划算，
改在 README 教使用者點「其他資訊 → 仍要執行」。

---

## D19 — 用 `IFileOperationProgressSink` 取得逐檔 HRESULT（**推翻 D6**）  ✅ 定案

**背景**：D6 當時寫「pywin32 對 `IFileOperationProgressSink` 支援不完整，
改用 `FOF_NOERRORUI` + 事後驗證掃描」。**那個前提經查證是錯的。**

pywin32 不但支援，還自己附了範例
（`com/win32comext/shell/demos/IFileOperationProgressSink.py`）：

```python
class FileOperationProgressSink(DesignatedWrapPolicy):
    _com_interfaces_ = [shell.IID_IFileOperationProgressSink]
    _public_methods_ = ["StartOperations", "FinishOperations", ...]
pythoncom.WrapObject(FileOperationProgressSink(), shell.IID_IFileOperationProgressSink)
```

而 `PostCopyItem` 的簽章帶著**逐檔的 HRESULT**：

```
PostCopyItem(Flags, Item, DestinationFolder, NewName, hrCopy, NewlyCreated)
                                             ^^^^^^^
```

`PyIFileOperation.Advise` / `Unadvise` 也都在。

**代價是實打實的**：2026-08-30 民眾B 那次 3649 個檔案全數失敗，我們只知道
「全部失敗」，一個錯誤碼都沒有，事後怎麼推都推不出根因。有了 sink，
同樣的災情會留下 3649 個 HRESULT 的分布，一次就能定位。

**決策**：`core/copysink.py` 掛上 sink，但**保留** `FOF_NOERRORUI` 與第 3 段
驗證掃描 —— 兩者是**獨立的證據來源**：sink 說「Shell 認為這個檔案複製成功了」，
驗證掃描說「檔案真的在目的地而且不是 0 byte」。**兩者不一致本身就是最有價值的訊息。**

**三個地雷（外部實測來源，不要自己試）**：
1. **`PreCopyItem` 一律回 `S_OK`。** 回 `S_FALSE` 會**中止整批操作**，
   不是「跳過這一個」（xplorer² 實測）。
2. **callback 裡絕不寫 log、絕不呼叫 COM。** 它每個檔案被呼叫一次；
   3649 次 `log.warning` 是 3649 次磁碟寫入，而 `Item.GetDisplayName()`
   在 MTP 上是一次來回（~3.4 ms），3649 次就是 12 秒純浪費。
   名稱用 Shell 免費給的 `NewName`。
3. **任何例外都不能逸出 callback** —— 它在 COM 的呼叫堆疊裡執行。

**sink 是加分項，不是必需品**：建立失敗、`Advise` 失敗、pywin32 版本不合，
一律降級成原本的驗證掃描，備份照跑。**診斷能力可以失去，備份能力不行。**

---

## D20 — 靜默 0 是 `IEnumIDList` 的語意上限，不是 bug  ✅ 定案

**這一條是整個 v1.0.2 的核心認知。**

`IEnumIDList` 在**語意上**就無法區分「列舉完了」與「provider 放棄了但選擇不報錯」：

- 微軟文件（`IEnumIDList::Next`）：S_FALSE 且 `pceltFetched = 0`
  就是「沒有更多項目」，**沒有第三種可能**。
- Raymond Chen（2024-08-12）：`EnumObjects` 回 S_FALSE 時**允許**把
  enumerator 設成 NULL。
- pywin32 原始碼確認：`Next` 的檢查是
  `if (HRESULT_CODE(hr) != ERROR_NO_MORE_ITEMS && FAILED(hr))` ——
  S_FALSE 不拋例外，回傳長度 = `celtFetched` 的 list（可能是空的）。
  `EnumObjects` 的 `IEnumIDList *ppeidl;` **宣告時未初始化**，
  provider 回 S_FALSE 時 Python 端拿到 `None`。

**所以「0 項」這個回答本身不帶任何可信度資訊，而且沒有 API 能補救。**

**決策**：不改判斷規則（沒得改），改成**自己補三個獨立訊號**：

1. **耗時** —— 8.8 秒的 0 和 20 毫秒的 0 是完全不同的兩件事
2. **重驗** —— 重新 `BindToObject`、重新 `EnumObjects`，兩次都 0 才採信
3. **旗標交叉** —— `只列檔案 + 只列資料夾` 應該等於 `全部`（診斷報告在做）

**重驗策略刻意分兩路**（`verify_empty` 參數）：

| 路徑 | 策略 | 理由 |
|---|---|---|
| 瀏覽（`list_subfolders`） | 只驗「慢速的 0」 | 344 個資料夾裡本來就有很多空的，每個都驗等於展開成本加倍，違反 D8 |
| 複製（`iter_files`）、`is_empty` | **一律驗** | 這裡的假 0 = 整個資料夾沒備份到而且回報成功 |

**連帶的兩條規則**：
- 重試一律**丟掉 enumerator、重新 bind**。微軟文件對「`Next()` 回錯誤之後
  enumerator 還能不能用」**完全沒有保證**；而且重新 bind 會重走
  Desktop → 本機 → 裝置 這條路，那正是重建裝置連線的機會。
- 重試的去重**用 PIDL，不靠順序**。Shell 不保證兩次列舉順序一致 ——
  舊 CLI 版 `getFilteringSignals` 就是 zip 兩份獨立列舉而複製到錯的檔案。

---

## D21 — WPD 只當診斷探針，**不當資料路徑**  ✅ 定案

**背景**：Shell 路徑（`IShellFolder`）把所有裝置狀態壓成同一個
`0x8007001E`（ERROR_READ_FAULT，一個泛用的磁碟讀取錯誤）。於是
「被其他程式佔用」「裝置當掉」「資料夾真的是空的」在原理上分不開。

WPD 分得出來：

| 常數 | 值 | 意義 |
|---|---|---|
| `E_WPD_DEVICE_ALREADY_OPENED` | `0x802A0001` | 已被其他程式開啟 |
| `E_WPD_DEVICE_IS_HUNG` | `0x802A0006` | 裝置不再回應 |
| `ERROR_BUSY` | `0x800700AA` | 忙碌中，**稍後重試會好** |
| `ERROR_DEVICE_IN_USE` | `0x80070964` | 被其他程式佔用 |
| `ERROR_DEVICE_NOT_CONNECTED` | `0x8007048F` | 已拔除 |

而且 `IPortableDeviceManager.GetDevices()` 本身就是「iPhone 到底在不在」的
**第二個獨立答案**，完全不經過 Shell 列舉。

**評估過但否決的方案：雙路徑（Shell 為主、失敗時 fallback 到 WPD）。**
否決的理由是**觸發條件定義不出來**：

- 「手機沒解鎖／沒點信任」在 Shell 上的表現是「列舉成空的，不報錯」
- 「裝置 session 壞掉」的表現**也是**空的或 `0x8007001E`
- 「資料夾真的是空的」的表現**還是**空的

這三者分不開正是我們想 fallback 的理由，於是觸發條件只能寫成
「空的或出錯就 fallback」，而「空的」在正常使用中極為常見
（344 個資料夾裡本來就有空的）→ **會被大量誤觸發。**

而且 WPD 路徑不是「某支函式的備援」，是**列舉 + 複製 + 進度 + 取消**的
平行實作：位置從 PIDL 變成 `(device_id, object_id)`、錯誤模型從
`ShellError` 變成 `COMError.hresult`，最痛的是
**`IFileOperation` 的原生進度視窗（D12 的全部價值）會歸零**，
要自己開 stream、自己做 buffer 迴圈、自己畫進度、自己實作取消。
**那不是維護成本翻倍，是把整個專案再做一次。**

**決策**：`core/wpd_probe.py` 只做
`GetDevices → 讀名稱 → Open → Close`，**不列舉內容、不搬任何資料**，
結果只出現在診斷報告。

- 所有 `import` 放在函式裡 —— 沒有 comtypes 的機器上模組本身也要 import 得起來
- 每一步獨立 `try/except`，探針掛掉只會讓報告少一段
- **開完一定要關** —— 這個探針絕不能變成「佔用裝置的那個程式」，
  那正是我們在懷疑 CopyTrans 做的事
- comtypes 會在 runtime 產生 wrapper（`comtypes/gen`），與 PyInstaller
  有已知衝突 —— 這是接受風險而非忽視風險，圍堵方式見上

**將來若真的要走雙路徑**，匯流點是 `listing.FileEntry` 與 `copier.run_copy`
的**介面層**（把 `abs_pidl` 換成不透明的 `Location`、`run_copy` 抽成
`CopyBackend`），**不要在列舉層匯流** —— 複製才是兩條路徑差異最大的地方。
