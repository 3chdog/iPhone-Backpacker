# 04 — Windows Shell COM / MTP 技術筆記

最後更新：2026-08-26

**寫任何 COM 相關程式碼前先讀這份。踩到新坑請補進來，這份文件的價值隨時間累積。**

---

## PIDL 與 COM 介面的執行緒規則 ★

- **`IShellFolder` / `IShellItem` 等 COM 介面不能跨執行緒直接傳遞**（COM apartment 的限制）。
- **PIDL 可以** —— 在 pywin32 裡本質是 bytes，是純資料。

所以複製作業的標準模式是：

```
主執行緒：收集使用者勾選項的「絕對 PIDL」（bytes）
   ↓ 丟給 worker thread
worker：pythoncom.CoInitialize()
        → shell.SHCreateItemFromIDList(abs_pidl) 重建 IShellItem
        → 執行複製
        → pythoncom.CoUninitialize()
```

**建議一律使用絕對 PIDL**（`shell.SHGetIDListFromObject(folder)` + 子項 PIDL 串接），
這樣 worker 端可以一步 `SHCreateItemFromIDList`，不必重走 bind 鏈。

---

## 目的地不需要走 shell 樹

目的地永遠是**真實檔案系統路徑**，直接用：

```python
shell.SHCreateItemFromParsingName(str(dest_path), None, shell.IID_IShellItem)
```

既有的 `getFolderObject_byAbsPath(dst)` 那整條路可以刪掉。這是不小的簡化。

---

## `IFileOperation` 的旗標

| 旗標 | 作用 |
|---|---|
| `FOF_NOCONFIRMATION` | 所有對話框一律回「全部是」。**撞名時等同全部覆蓋。** |
| `FOF_NOERRORUI` (`0x0400`) | **關掉錯誤/「是否略過」小視窗**，失敗項目靜默跳過 |
| `FOF_SILENT` | 關掉**進度視窗**（我們不要用它 —— 原生進度視窗品質比自己畫的高） |

- `FOF_NOCONFIRMATION` **不會**關掉進度視窗，所以 v1 直接用系統原生進度視窗就好。
- 記得 `pfo.SetOwnerWindow(hwnd)` 把它掛在 Qt 主視窗底下，否則會變成孤兒視窗。
- 複製後用 `pfo.GetAnyOperationsAborted()` 判斷是否被中途取消。

### ★ `PerformOperations()` 在零排程時會噴 `0x8000FFFF`

`-2147418113` = `0x8000FFFF` = `E_UNEXPECTED` =「災難性的失敗」。
**呼叫前一定要 guard「排程數為 0 就直接返回」。** 已於階段 0 修掉。

---

## 失敗檔案的處理（使用者回報的問題 3）

不要碰 `IFileOperationProgressSink`（pywin32 支援不完整）。用這個組合：

1. `SetOperationFlags(FOF_NOCONFIRMATION | FOF_NOERRORUI)` → 失敗項目靜默略過，不跳小視窗
2. 把批次切成每 100~200 檔一次 `PerformOperations()` → 得到**真實進度**與更細的失敗定位
3. 每批之後做**驗證掃描**：列舉來源選取清單 vs 目的地實際檔名/大小，對不上的就是失敗清單
4. UI 顯示「N 個檔案失敗」+ 可展開清單 + 「重試失敗項目」按鈕，同時寫進 log 檔

---

## 列舉順序不保證穩定 ★

**Shell 的 `IEnumIDList` 列舉順序不保證兩次一致**，MTP 上尤其。

既有 code 的 `getFilteringSignals()` 對同一個 folder 做**兩次獨立列舉**再 `zip`，
一旦順序對不齊就會**複製到錯的檔案**。這是真的 bug。

**正解**：一次列舉就把「PIDL + 檔名」綁在同一個 `FileEntry` 裡，永遠不要靠兩次列舉的順序對齊。
（`copier` 介面收 `list[FileEntry]` 也是為了這件事。）

---

## 效能：檔案總管為什麼慢 ★

在 MTP 上，慢的主因**不是列舉本身**，是這兩件事：

| 昂貴的操作 | 成本 |
|---|---|
| **縮圖（thumbnail）** | 每張照片都要把影像資料抓下來解碼。最大宗。 |
| **`GetDetailsOf` 取大小/日期** | **每個項目一次來回**。幾千個項目就是幾千次來回。 |

**本專案兩件都不做**（使用者明確表示不需要縮圖），這是我們能比檔案總管快的根本原因。

實作規則：
- 樹狀展開用 `SHCONTF_FOLDERS`，只要資料夾
- `list_children(..., want_details=False)` 是預設，`GetDetailsOf` 只在真的要用時呼叫
- 列舉結果進 session 快取，收合再展開零成本
- 「這個資料夾有沒有照片」用早退判斷，找到第一個就 return，不要數完

### 實測數據

**2026-08-27，階段 1 煙霧測試（本機資料夾，Windows + Python 3.12，插著 iPhone）**

| 操作 | 耗時 | 備註 |
|---|---|---|
| `this_pc_pidl()` | 7.1 ms | CSIDL_DRIVES，可忽略 |
| **列出「本機」底下 8 個節點** | **774.5 ms** | **★ 超過 0.5s 目標，見下方** |
| `list_subfolders()` 本機小資料夾（首次） | 6.5 ms | |
| `list_subfolders()` 同節點（快取命中） | < 0.1 ms | 快取有效 |
| `folder_has_media()` 早退 | 2.6 ms | |
| `iter_files()` 完整列舉（4 個檔案） | 2.1 ms | |
| `run_copy()` 複製 4 個檔案 | 598.6 ms | **固定成本，不是每檔成本** |
| `run_copy()` 重跑（全部命中增量去重） | 15.4 ms | 去重有效 |

驗證通過的假設：
- `IEnumIDList.Next(64)` 批次列舉可用，沒有觸發單筆 fallback。
- 「本機」底下同時列出磁碟機（`OS (C:)`）、使用者資料夾（`下載`/`圖片`/`桌面`…）
  與 `Apple iPhone`，證實 `SFGAO_FOLDER && !SFGAO_FILESYSTEM` 這個判斷有東西可分。

### 「本機」列舉的 ~770 ms —— 已釐清，**不是** MTP warm-up

**2026-08-27 實測對照組：**

| | 沒插 iPhone | 插著 iPhone |
|---|---|---|
| 列出「本機」（冷） | 772.7 ms | 768.5 ms |
| 列出「本機」（熱） | 484.8 ms | 503.7 ms |
| `find_portable_devices()` | 476.3 ms | 498.7 ms |
| `probe()` | — | 860.0 ms |

兩組幾乎一模一樣 → **這是列舉「本機」本身的固有成本，跟 iPhone 無關。**
（原本假設是 Shell 為 MTP 開 WPD session 的 warm-up，**該假設已被推翻。**）

最可能的來源：測試機上有 `SDXC (D:)` 與 `USB 磁碟機 (F:)`，
可移除式磁碟機要查媒體狀態與磁碟區標籤，這在 Shell 列舉時是同步的。

**設計結論（不變，但理由不同）**：裝置偵測總成本約
`列舉本機 ~500ms + find_portable_devices ~500ms + probe ~860ms ≈ 1.9 秒`。
**UI 啟動絕不能同步等它** —— 先畫出視窗與「正在偵測裝置…」狀態，
偵測在背景執行緒跑完再填樹。

### 另一個實測發現：`IFileOperation` 有固定啟動成本

複製 4 個檔案花了 598 ms。這幾乎全是建立 `IFileOperation` +
`PerformOperations()` 的固定開銷，不是每檔成本。

**設計含意**：chunk 不能切太小，否則固定成本會被乘上批次數。
`chunk_size=200` 的預設值是合理的，不要為了進度更新頻繁而調小。

### iPhone 實測（2026-08-27，一支有 50~100 個資料夾的 iPhone）

| 操作 | 耗時 | 判讀 |
|---|---|---|
| 展開 `Apple iPhone`（1 個子項） | 13.2 ms | 快 |
| **展開 `Internal Storage`（50~100 個子項）** | **2772.1 ms** | **★ 主要瓶頸** |
| 展開葉節點 `200101__`（2 個項目） | 98.2 ms | |
| 同一節點再展開（快取命中） | < 0.1 ms | 快取有效 |
| 單次列舉的固定開銷 | **~45 ms** | 見下 |

**★ MTP 每次列舉有 ~45ms 的固定開銷。**
證據：在只有 2 個項目的葉節點上，三種旗標分別量到 44.0 / 47.8 / 48.3 ms ——
內容幾乎不影響，全是固定成本。

這個數字的設計含意很大：**任何「對每個資料夾都做一次列舉」的演算法，
在 100 個資料夾的裝置上光固定開銷就是 4.5 秒**，而 `find_photo_folders`
初版對每個資料夾做**兩次**列舉（`folder_has_media` + `list_subfolders`）。
實測跑了 20 分鐘沒有結束，這就是原因之一。

### ★ `SHCONTF_FOLDERS` 是事後過濾，**不會**減少工作量

在 `Internal Storage`（**184 個資料夾、0 個檔案**）上量到：

| 旗標 | 回傳項目 | 耗時 |
|---|---|---|
| `SHCONTF_FOLDERS` | 184 | 1556.2 ms |
| `FOLDERS \| NONFOLDERS` | 184 | 1556.8 ms |
| **`SHCONTF_NONFOLDERS`** | **0** | **1544.3 ms** |

**關鍵證據是第三行**：要求「只列檔案」、實際回傳 0 個項目，
卻仍然花了 1544 ms —— 幾乎等同於完整列舉 184 項。
這證明 **Shell 把全部子項走訪了一遍才過濾**，旗標只是事後篩選。

（腳本 v2 在這裡印出「SHCONTF_FOLDERS 有效」是**判定邏輯的漏洞**：
判定條件寫成 `len(files) > len(folders)*2`，檔案數 0 時直接落空掉進 else。
v3 已改為以「回傳 0 項的旗標仍耗時」為主要判準。）

**設計結論**：不要指望旗標帶來效能。
`list_subfolders()` 仍然該用 `FOLDERS_ONLY`（語意正確、回傳量小），
但**別把它當成效能手段**。真正的解法是非同步 + 快取。

### 成本模型

| 節點 | 項目數 | 耗時 |
|---|---|---|
| `Apple iPhone` | 1 | 16.5 ms |
| 葉節點 `200101__` | 2 | ~47 ms |
| `Internal Storage` | **184** | **~1550 ms** |

粗估 **≈ 40 ms 固定 + 8.2 ms × 項目數**（184 × 8.2 = 1509，對得上實測 1550）。

含意：
- **一個 5000 張照片的資料夾，光列舉就要約 41 秒。**
  這解釋了 `find_photo_folders` 初版跑 20 分鐘的原因 ——
  184 個資料夾各兩次列舉，其中若有幾個裝了數千張照片就會爆掉。
- `iter_files()` 設計成 generator 是對的：複製時邊列舉邊排程，
  不會先卡 41 秒才開始複製第一批。

### ★★ 批次大小才是主要的效能參數（2026-08-27 實測）

在三個照片資料夾上量「取前 N 筆」的耗時：

| 取前 N 筆 | `202508__`(293 項) | `202408__`(421 項) | `201810__`(479 項) |
|---|---|---|---|
| 1 | 671.5 ms | 719.0 ms | 665.0 ms |
| 10 | 674.1 ms | 727.6 ms | 668.3 ms |
| 50 | 673.2 ms | 734.4 ms | 684.1 ms |
| 200 | 2438.6 ms | 2654.2 ms | 2415.5 ms |
| 全部 | 2696.9 ms | 4257.6 ms | 4436.6 ms |

**1 / 10 / 50 筆完全一樣 —— 這不是線性成長，是階梯。**
階梯落在 64 的倍數上，而 64 正是 `shell_ns._enum_pidls()` 寫死的批次大小。

驗算 `ceil(項目數 / 64)` 次 `Next()` 呼叫：

| 資料夾 | 項目數 | 呼叫次數 | 實測 | 每次呼叫 |
|---|---|---|---|---|
| `Internal Storage` | 184 | 3 | ~1440 ms | 480 ms |
| `202508__` | 293 | 5 | 2696.9 ms | 539 ms |
| `202408__` | 421 | 7 | 4257.6 ms | 608 ms |
| `201810__` | 479 | 8 | 4436.6 ms | 555 ms |

**結論：成本 ≈ 每個「被實體化的項目」~10 ms，而 `Next(n)` 不管呼叫端
實際要幾筆，都會把 n 筆準備好。**

含意：
- **早退在 `_ENUM_BATCH = 64` 之下根本沒有生效。**
  `folder_has_media()` 花 ~670 ms 去拿它只需要 1 筆的答案。
  → 已改為使用 `PROBE_BATCH`（小批次），`is_empty()` 用 `batch=1`。
- `bench_enum.py` 印的「固定 711 ms + 每項 8.43 ms」是把階梯硬擬成
  直線的結果 —— 那個「固定成本」其實是第一個 batch 的錢，**不要採信**。

### 批次大小的實測結果（2026-08-27，`tools/bench_batch.py`）

**A. 早退（只取第 1 筆）—— 批次大小差 24 倍**

| batch | 293 項 | 421 項 | 479 項 |
|---|---|---|---|
| **1** | **74.5 ms** | **67.1 ms** | **65.4 ms** |
| 4 | 76.9 | 77.3 | 76.7 |
| 16 | 124.1 | 123.5 | 136.9 |
| 64 | 292.8 | 295.3 | 279.9 |
| 256 | 951.8 | 950.6 | 952.2 |
| 1024 | 997.2 | 1443.5 | 1611.5 |

→ **`PROBE_BATCH = 1`**。用 64 要付 4 倍的錢。

**B. 完整列舉 —— 批次大小完全無關**

| 項目數 | 最快 | 最慢 | 差距 |
|---|---|---|---|
| 293 | 981 ms (b=4) | 1038 ms (b=1024) | 1.1x |
| 421 | 1415 ms (b=1024) | 1501 ms (b=4) | 1.1x |
| 479 | 1612 ms (b=1024) | 1644 ms (b=1) | 1.0x |

三組都落在 **~3.4 ms/項**。→ `DEFAULT_BATCH` 設多少都一樣，維持 64。

### ★★ 成本模型（結論版）

把 `bench_batch` 與 `smoke_device --copy` 的數字合起來解，得到單一模型：

```
單次列舉成本 ≈ 70 ms 開場 + 3.4 ms × 「實際被實體化的項目數」
```

「開場」是第一次觸碰該資料夾時建立列舉 session 的成本；
「被實體化的項目數」由 `Next(n)` 的 n 決定，**不是**呼叫端實際用了幾筆。

四組實測都符合：

| 情境 | 模型預測 | 實測 |
|---|---|---|
| batch=1 取 1 筆（293 項） | 70 + 1×3.4 = 73 | 74.5 ms |
| batch=64 取 1 筆 | 70 + 64×3.4 = 288 | 292.8 ms |
| batch=1 取全部（293 項） | 70 + 293×3.2 = 1008 | 1008.1 ms |
| batch=64 取全部 | 同上 | 1002.4 ms |

推論：
- **`PROBE_BATCH = 1` 永遠不會比較差**，在大資料夾上快 4 倍以上。
- 批次大小本身不花錢，花錢的是被實體化幾筆 → `DEFAULT_BATCH` 隨意。
- 旗標（`SHCONTF_*`）在**實體化之後**才過濾，所以省不到 —— 與先前結論一致。

### 冷 vs 熱：同一節點差 4 倍

`Internal Storage`（184 項）在同一次執行內：

| | 耗時 |
|---|---|
| 首次展開（冷） | 2613.5 ms |
| 同次執行內再列舉（熱） | ~650 ms |

**4 倍差距。** 熱的 650 ms ÷ 184 = 3.5 ms/項，符合上面的模型；
冷的額外 ~2 秒是 Shell 對該節點的一次性建置。

含意：
- `NamespaceCache`（session 內）價值很高，但**每次重開 App 都要重付冷成本**。
- 這強化了「跨 session 磁碟快取」的價值 —— 但仍照 D10 延後到 v1.1 評估。

### ★ 量測紀律：A/B 一律先 warm-up

**這個坑在本專案已經製造過三次錯誤結論**：

1. 第一版 A/B 跑在 2 個項目的葉節點 → 「只省 8%」（無效）
2. 第二版檔案數 0 時掉進 else → 「SHCONTF_FOLDERS 有效」（相反的結論）
3. `smoke_device` 第 4、5 節沒 warm-up → 「早退省下 -447%」、
   「只列資料夾花了 115%」（純粹是先跑的那個要付 70 ms 開場）

`tools/smoke_device.py` 的第 4、5 節已補上 warm-up，
`bench_enum.py` / `bench_batch.py` 本來就有。**做 A/B 一律先 warm-up。**

### 絕對數字不可跨 session 比較（±3 倍）

同一支 iPhone、同一個資料夾，兩天量到的差異：

| | 2026-08-26 | 2026-08-27 |
|---|---|---|
| 列出「本機」（冷） | 774 ms | 328.7 ms |
| `find_portable_devices()` | ~500 ms | 188.7 ms |
| `202508__` 完整列舉（293 項） | 2696.9 ms | ~990 ms |
| 推得的每項成本 | ~9 ms | ~3.4 ms |

**約 3 倍的差距。** 原因未確定（USB 埠/線材、機器負載、Windows 快取、
裝置電源狀態都有可能）。

**結論：只相信「同一輪執行內的比例」，不要跨 session 比絕對值。**
所有效能決策都必須建立在同一輪內的 A/B 對照上。

### `GetDisplayNameOf` 幾乎不花錢

三次量測「只取 PIDL」vs「取 PIDL + 顯示名稱」：
`1538.5 / 1534.4`、`1405.1 / 1405.3`、`1340.2 / 1651.1` ms
→ 佔比 -0% / 0% / 19%。

**結論：顯示名稱基本上是免費的**（MTP extension 很可能已經隨 PIDL 帶回來了）。
第三組的 19% 落在量測雜訊範圍內 —— 同一個節點跨多次執行量到
1340~1651 ms，本身就有 ±15% 的浮動。**不需要為了省名稱而改設計。**

---

## MTP / iPhone 的行為特性

- **未解鎖或未點「信任這部電腦」時，資料夾會列舉成「空的」而不是報錯。**
  使用者只會看到空白視窗，以為程式壞了。
  → 必須偵測「裝置節點存在但內容為空」並明確提示。
  **這是投報率最高的單一功能。**

### ★★ 「已經按了信任」之後還要再等 1~2 分鐘（實測）

使用者實測的完整狀態變化：

| 步驟 | 「本機」 | `Internal Storage` |
|---|---|---|
| 1. 沒插手機 | 沒有 `Apple iPhone` | — |
| 2. 插入手機 | 出現 `Apple iPhone` | 存在，但**空的** |
| 3. 不理會 / 點「不允許」 | 同 2 | 同 2 |
| 4. **點了「允許」之後 1~2 分鐘（有時更久）** | 同 2 | **仍然是空的** |
| 5. 再等一陣子 | 同 2 | 終於出現資料夾 |

**第 4 步是 UX 的關鍵**：使用者明明已經按了「信任」，程式卻還在叫他去按「信任」，
會讓人以為程式壞了或自己按錯。

這段期間**連 Windows 檔案總管進去看也是空的**，所以不是本程式的問題，
但訊息必須主動說明這件事，否則使用者只會覺得工具有問題。

`device.status_message()` 的 `LOCKED_OR_UNTRUSTED` 分支因此列出三種可能，
第三種明確寫「已經點過信任了 → 請再等一到兩分鐘」。
- **列舉幾千張照片超過 30 秒是正常的。** 樹狀節點必須延遲展開 + 背景執行緒，否則視窗直接凍住。
- **MTP 偶爾回傳不完整的清單**，這是 Windows 的已知毛病，重新插拔就好 → UI 放「重新整理裝置」按鈕。
- **`GetDetailsOf` 取 size/mtime 在 MTP 上很慢**，只在真的需要時才呼叫（`want_details=True`）。
- **iOS 改版會改變 DCIM 結構**，實際觀察到三種形態（見 `00-overview.md`）。
  → 永遠不要寫死路徑深度；用樹狀瀏覽 + 「自動尋找照片資料夾」遞迴掃描。

---

### ★ 「自動」模式的即時轉檔會讓複製速度掉到 0（2026-08-28 實測）

使用者回報：備份到隨身碟或 2TB 隨身硬碟時，複製到一半
**Windows 進度視窗顯示 0 位元組／秒**，卡住好幾分鐘。
但同樣的資料夾改用「保留原始檔」就一路 4 MB/s 以上順利跑完。

**原因**：「自動」是手機端**即時轉檔**（HEIC→JPG、HEVC→H.264）。
Shell 跟 iPhone 要一個檔案時，手機必須先把整個檔案轉完才有 bytes 可送 ——
這段期間連線是通的但沒有資料流動，所以顯示 0 B/s。
照片小、轉得快，感覺不明顯；**影片大，轉檔可能好幾分鐘**，看起來就像當掉。

**這不是我們能修的東西** —— 轉檔發生在手機上，`IFileOperation` 只是在等 I/O。
但**一定要在 UI 與 README 講清楚**，否則使用者會以為是這個工具壞了。

已寫進：`ui/dialogs.py` 的首次啟動引導、README 的「常見狀況」。

## ★★ 靜默失敗是這個專案最危險的失敗方式

2026-08-29 的災情回報揭露了兩條**會讓資料悄悄消失**的路徑，
兩條都在 `shell_ns.py`，原本都只有 `log.warning` 然後繼續：

```python
# ① 列舉中途失敗 → 直接 return，清單被截斷
except pythoncom.com_error as exc:
    log.warning("列舉中斷（MTP 偶發，建議重新插拔）：%s", exc)
    return

# ② 取不到顯示名稱 → 跳過這個項目
except pythoncom.com_error as exc:
    log.warning("取得顯示名稱失敗，略過一個項目：%s", exc)
    continue
```

**為什麼這對備份工具特別致命**：

- `iter_files()` 被截斷 → `copier` 判定「待複製 0 個」→ **靜默跳過整個資料夾不備份**。
  使用者看到「備份完成」，實際上那個資料夾一個檔案都沒複製。
- `list_subfolders()` 漏掉項目 → 使用者根本看不到那個資料夾，
  自然也不會勾選它。

**兩條都改成「重試一次，仍然失敗就 raise `ShellError`」。**
MTP 偶發性失敗是真的存在，但「拿到不完整的資料卻不知道」比「看到錯誤訊息」糟得多。

**同一個原則的延伸**：`attributes_of()` 失敗時改回 `None`（未知）而不是 `0`。
回 `0` 會讓呼叫端讀成「不是資料夾、不是檔案系統」，裝置偵測就把整個節點排除掉。

**UI 層對應**：檔案數讀不到時顯示「讀取失敗」而**不是 0**。
顯示 0 會讓使用者以為資料夾是空的而不去備份它。

## ★★★ 不要假設 API 存在；「取不到」不是證據

2026-08-29 的災難，值得完整記下來。

**症狀**：程式顯示「偵測到『下載』，但還讀不到裡面的內容」——
連 iPhone 都沒插上去的時候也一樣。

**根因**：`shell.SHBindToParent` **在 pywin32 裡根本不存在**。

```
parsing = （取不到：module 'win32com.shell.shell' has no attribute 'SHBindToParent'）
```

於是 `parsing_name()` 對每一個節點、每一次呼叫都拋 `AttributeError`。
而當時的判斷規則寫著「取不到解析名稱 → 視為 MTP 裝置的典型特徵」，
結果「本機」底下**每一個**節點都被判成 iPhone，程式取了第一個：「下載」。

**兩個錯誤疊在一起**：

1. **假設某個 API 存在，而且沒有檢查。**
   更糟的是，早期的 log 一直印著「（無解析名稱）」，
   我把它解讀成「MTP 裝置的行為特徵」寫進註解與文件 ——
   **把一個純粹的程式錯誤誤讀成了領域知識。**

2. **把「取不到資訊」當成正面證據。**
   「取不到」只代表我們不知道。不知道不等於是裝置。

**修正**：

- `parsing_name()` 改用**已經驗證能動的** `GetDisplayNameOf` +
  `SHGDN_FORPARSING`（桌面資料夾會把絕對 PIDL 當成相對 PIDL），
  並依序嘗試多種作法，全部失敗才 raise。
- `display_name()` 有同樣的問題（呼叫端用 `except Exception` 包住，
  失敗會靜靜變成名稱「?」，所以一直沒被發現），一併改掉。
- `_classify()` 在資訊不足時回 **False**，不再從寬。
- 新增 `shell_ns.api_report()` / `log_api_report()`，
  **啟動時檢查一遍我們依賴的每一個 Shell API 是否存在**，
  並寫進 log 與診斷報告。這類錯誤不該再有機會偽裝成裝置行為。
- `tests/test_device_classify.py` 加了一組 `TestUnknownIsNotEvidence`，
  直接重現當時的資料（所有節點 parsing 都是 None），確保不再退化。

## ★★★ SFGAO 在原理上分不出「可攜式裝置」與「第三方掛載」

2026-08-30，民眾B 的報告揭露了一個**訊號解析度**的問題，不是實作 bug。

**症狀**：程式顯示「已連接：CopyTrans Studio」，而不是他的 iPhone。

**查證結論**（[The Old New Thing](https://devblogs.microsoft.com/oldnewthing/20171101-00/?p=97325)）：

| 型態 | 屬性組合 |
|---|---|
| **虛擬資料夾**（控制台、**可攜式裝置**、**第三方 namespace extension**） | `FOLDER`，**沒有** `FILESYSTEM` |
| 真實檔案系統目錄 | `FOLDER` + `FILESYSTEM` |
| 檔案裡的虛擬目錄（ZIP） | `FOLDER` + `FILESYSTEM` + `STREAM` |

**`SFGAO_FILESYSTEM` 只能分出「虛擬」與「真實檔案系統」。
可攜式裝置和第三方掛載同屬虛擬資料夾，這組旗標在原理上分不出來。**

實測佐證：

```
CopyTrans Studio   attrs = 0x20000000   ← 完全相同
Apple iPhone       attrs = 0x20000000
```

之前沒出事，只是因為測試機與民眾A 的機器上剛好沒有第三方掛載。

### 第三方怎麼掛進「本機」

在 `HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\MyComputer\NameSpace\{CLSID}`
註冊一個 CLSID 就好，任何軟體都能做，**不需要是磁碟機也不需要對應到檔案系統**。
（[Microsoft Learn](https://learn.microsoft.com/en-us/previous-versions/windows/desktop/legacy/cc144096(v=vs.85))）

實務上「本機」底下至少有四種型態：磁碟機、使用者資料夾、
**可攜式裝置**、**第三方 namespace extension**。

### ★ 正面證據：WPD 裝置介面

WPD 裝置的解析名稱有固定結構：

```
::{20D04FE0-3AEA-1069-A2D8-08002B30309D}\\\?\usb#vid_05ac&pid_12a8#<序號>#{6ac27878-a6fa-4155-ba85-f98f491d4f33}
   ^^^^^^^^ 本機的 CLSID                 ^^^^^^ 裝置介面路徑              ^^^^^^^^ GUID_DEVINTERFACE_WPD
```

- **`{6AC27878-A6FA-4155-BA85-F98F491D4F33}` = `GUID_DEVINTERFACE_WPD`**
  （[官方文件](https://learn.microsoft.com/en-us/windows-hardware/drivers/install/guid-devinterface-wpd)）。
  所有 WPD 驅動都會註冊這個介面。
- **`{35786D3C-B075-49b9-88DD-029876E11C01}` = 「Portable Devices」** delegate folder。

第三方 namespace extension 則是 `::{自己的 CLSID}`，不會有這些東西。

`shell_ns.looks_like_portable_device()` 就是在測這三個特徵。
**刻意測 `\\?\` 與 WPD GUID 而不是測 `usb#`** ——
MTP over IP / Bluetooth 的裝置沒有 `usb#`，但仍有 WPD 介面 GUID。

### 「不確定」要誠實表達，不要硬猜

判斷改成三值（`CONFIRMED` / `LIKELY` / `EXCLUDED`），
分不出來時回 `DeviceStatus.AMBIGUOUS`，把候選列給使用者自己選。詳見決策 D17。

**「挑第一個 probe 成功的」擋不住這種情況** —— CopyTrans Studio 真的有內容
（`Photo library` 底下有 Albums、Camera roll…），probe 一樣會過。

### GetAttributesOf 只回傳你問到的位元

```python
folder.GetAttributesOf([pidl], mask)   # 回傳值已經跟 mask 做過 AND
```

以前只問 `FOLDER|FILESYSTEM`，所以 `0x20000000` 的意思是
「**在我問的兩個位元裡**只有 FOLDER」，**不代表其他位元是 0 —— 我們根本沒問**。

現在 `DEFAULT_ATTRIBUTE_MASK` 多問了 `FILESYSANCESTOR / STORAGE / STREAM /
STORAGEANCESTOR / REMOVABLE / BROWSABLE`。**同一次 COM 呼叫，零額外成本。**

**這些位元目前只寫進診斷報告，不參與判斷** —— 先累積真實資料，
確認 WPD 裝置與第三方掛載在這些位元上真的有穩定差異，再考慮採用。
官方文件裡**沒有任何一個旗標被定義為「這是可攜式裝置」**，所以不能靠猜。

## 裝置偵測的正確依據

**實測資料（2026-08-29，繁體中文 Windows 10）：**

| 節點 | attrs | 意義 |
|---|---|---|
| 下載／圖片／音樂／桌面／文件／影片 | `0x60000000` | `FOLDER \| FILESYSTEM` |
| OS (C:)／SDXC (D:)／USB 磁碟機 (F:) | `0x60000000` | `FOLDER \| FILESYSTEM` |
| **Apple iPhone** | **`0x20000000`** | **`FOLDER` 而已** |

**`SFGAO_FOLDER && !SFGAO_FILESYSTEM` 在這筆資料上完美區分**，
而且完全不看顯示名稱 —— 使用者把手機改成任何名字都不受影響。

v1.0.0 原本的規則就是對的，是後來「放寬」把它改壞的。

## 自動偵測失敗 ≠ 不能用

樹狀瀏覽（`list_subfolders`）走的是純列舉，**不看屬性**，
所以就算裝置偵測失敗，使用者照樣展得開、備份得了。

**訊息措辭必須反映這件事**，不能讓使用者以為「偵測不到就沒救了」：

> 沒有自動偵測到 iPhone。
> …
> **如果左邊的清單裡看得到你的手機，可以直接展開它使用** ——
> 自動偵測只是輔助，失敗不影響瀏覽與備份。

## 裝置偵測不能只靠單一訊號

災情：使用者把 iPhone 改名成「阿神ㄟ@iPhone」（含 `@` 與注音），
程式顯示「沒有偵測到 iPhone」，**但樹狀瀏覽卻能正常展開到 Internal Storage**。

**根因是兩條路走不同判斷**：

| 路徑 | 判斷 |
|---|---|
| 樹狀瀏覽 `list_subfolders()` | 只做列舉，**不看屬性** → 看得到 |
| 裝置偵測 `find_portable_devices()` | 要求 `SFGAO_FOLDER && !SFGAO_FILESYSTEM` → 漏掉 |

改成多重訊號，順序由可靠到次要：

1. 解析名稱像 `C:\` 或 UNC → 磁碟機／使用者資料夾，排除
2. **取不到解析名稱 → 視為裝置候選**（實測 iPhone 就是這樣，
   `SHBindToParent` 對 MTP 根節點會失敗；磁碟機則一定拿得到）
3. 兩者都不成立 → 回頭看 `SFGAO_FILESYSTEM`
4. 屬性也讀不到 → **寧可放行**

**偵測不到任何裝置時，把「本機」底下每個節點的
`名稱 / attrs / parsing / 判定依據` 全部 dump 到 log**，
下次收到回報就有資料可查，不用再靠猜。

判斷邏輯有純 Python 的單元測試（`tests/test_device_classify.py`），
在 Linux 上就能跑。

## `IFileOperation` 的錯誤碼

| HRESULT | 十進位 | 意義 | 怎麼處理 |
|---|---|---|---|
| `0x8000FFFF` | -2147418113 | `E_UNEXPECTED` | **零排程**時 `PerformOperations()` 就回這個。呼叫前一定要 guard。 |
| `0x80270000` | -2144927744 | `COPYENGINE_E_USER_CANCELLED` | 使用者按取消或關掉進度視窗。**這是正常結果，不是錯誤。** |

`COPYENGINE_*` 系列的錯誤碼都落在 `0x8027xxxx`。
不要把它們一律當成例外往上拋 —— 使用者取消會被顯示成「備份失敗」，
而且 log 裡會出現嚇人的 traceback。

**取消一個大任務時 `PerformOperations()` 會阻塞很久**（Windows 自己在收尾，
目的地是慢速隨身碟時特別明顯）。這段期間 worker 執行緒被卡住，
無法中斷 —— COM 呼叫沒有取消機制。UI 要把這件事講清楚。

## 開發機是 Linux、目標是 Windows 的坑

### `.bat` 一定要 CRLF + 純 ASCII

在 Linux 寫出來的 `.bat` 預設是 LF 換行，而 `cmd.exe` 需要 CRLF。
症狀是**每行的第一個字元被吃掉**：

```
'equirements.txt' 不是內部或外部命令      ← 少了開頭的 r
'cho.' 不是內部或外部命令                  ← 少了開頭的 e
```

再加上 `cmd.exe` 用系統 OEM codepage（繁中是 950）讀檔，
UTF-8 的中文註解會變成亂碼。

**解法**：把邏輯放在 `.py`（UTF-8 沒問題），`.bat` 只留一行純 ASCII 的轉呼叫。
另外加 `.gitattributes` 的 `*.bat text eol=crlf` 強制換行字元。

## 打包相關的坑

- **PyInstaller `--windowed` 後 `sys.stdout` / `sys.stderr` 是 `None`**，
  此時 `print()` 直接拋 `AttributeError` 讓程式閃退。`tqdm` 寫 stderr，同樣中招。
  → GUI 路徑上不准有 `print` / `tqdm`，一律 `logging` 寫檔。
- **`assert` 在 `-O` 模式會被整行移除** → 不要拿 `assert` 做執行期檢查。
- manifest 用 `asInvoker`，**不要**要求管理員權限 —— 本程式不需要，多跳一個 UAC 只會更嚇人。

---

# 列舉的「0 項」在 API 層就無法判真假（2026-09-13 查證）

**這是整個 v1.0.2 的核心認知，寫 code 之前一定要先懂這一段。**

## 三份互相印證的證據

**① 微軟文件 `IEnumIDList::Next`**

> If this method returns a COM error code (as determined by the FAILED macro),
> then **no entries in the rgelt array are valid on exit**.
>
> S_FALSE indicates that more items were requested than remained in the enumeration.
> **Note that the value will be 0 if there are no more items to retrieve.**

→ `S_FALSE` + `pceltFetched = 0` 就是「沒有更多項目」，**沒有第三種可能**。
→ 而且錯誤發生時**整批都無效**，不是「前面幾筆還能用」。

**② Raymond Chen, The Old New Thing（2024-08-12）**

> `EnumObjects` has the rule that if it returns `S_FALSE`, then it is allowed to
> return a null pointer instead of an enumerator.

→ 拿到 `None` 是**合法且正常**的，意思是「沒有項目」。

**③ pywin32 原始碼**（`com/win32comext/shell/src/`）

`PyIEnumIDList::Next`：
```c
if (HRESULT_CODE(hr) != ERROR_NO_MORE_ITEMS && FAILED(hr)) { ...raise... }
PyObject *result = PyList_New(celtFetched);
```
→ `S_FALSE` **不拋例外**，回傳長度 = `celtFetched` 的 list（可能是空的）。

`PyIShellFolder::EnumObjects`：
```c
IEnumIDList *ppeidl;                       // ← 宣告時未初始化
hr = pISF->EnumObjects(hwndOwner, grfFlags, &ppeidl);
if (FAILED(hr)) return PyCom_BuildPyException(hr, pISF, IID_IShellFolder);
return PyCom_PyObjectFromIUnknown(ppeidl, IID_IEnumIDList, FALSE);
```
→ `S_FALSE` 不是 `FAILED`，不拋例外。`ppeidl` **未初始化**，
provider 回 S_FALSE 時 Python 端拿到 `None`。

## 結論與對策

**「0 項」這個回答本身不帶任何可信度資訊，而且沒有任何 API 能補救。**
這不是我們的 bug，也不是 pywin32 的 bug —— 是 API 的解析度不夠。

對策是自己補三個獨立訊號（見 D20）：耗時、重驗、旗標交叉比對。

## 兩條連帶規則

**故障後不要重用 enumerator。** 微軟文件對「`Next()` 回錯誤之後 enumerator
還能不能用」**完全沒有保證**。舊版在同一個 enumerator 上重呼叫 `Next()`
是沒有文件支持的行為。正解是丟掉它、**重新 `BindToObject` + `EnumObjects`**，
而且重新 bind 會重走 Desktop → 本機 → 裝置 這條路，那正是重建裝置連線的機會。

**重試的去重要用 PIDL，不能靠順序。** Shell 不保證兩次列舉的順序一致。
（舊 CLI 版 `getFilteringSignals` 就是 zip 兩份獨立列舉而複製到錯的檔案。）

## `EnumObjects(0, flags)` 的 hwnd 是 NULL

微軟文件：`hwndOwner` 為 NULL 時，列舉器不應張貼任何訊息，
**需要使用者輸入時應該靜默失敗**。我們目前確實傳 0 ——
在「裝置已信任」的情境下沒有輸入需求，所以還沒觀察到影響，但這是已知的邊界。

---

# `IFileOperation` 的三個實測坑（2026-09-13）

**① `PostCopyItem` 帶著逐檔 HRESULT，而 pywin32 完整支援 sink。**

```
PostCopyItem(Flags, Item, DestinationFolder, NewName, hrCopy, NewlyCreated)
```

pywin32 自己附了範例：`com/win32comext/shell/demos/IFileOperationProgressSink.py`，
用 `DesignatedWrapPolicy` + `_com_interfaces_` + `pythoncom.WrapObject`，
16 個 callback 全部可用，`PyIFileOperation.Advise` / `Unadvise` 也都在。
**D6 當初寫「支援不完整」是沒查證就下的結論**，代價見 D19。

**② 從 `PreCopyItem` 回 `S_FALSE` 會中止整批操作，不是跳過單一項目。**
（xplorer² 實測。）想過濾檔案就在排程階段過濾，不要在 sink 裡擋。

**③ callback 在跑 `PerformOperations()` 的那條執行緒上、每個檔案被呼叫一次。**
裡面**絕不能寫 log、絕不能呼叫 COM**：3649 次 `log.warning` 是 3649 次磁碟寫入，
而 `Item.GetDisplayName()` 在 MTP 上是一次來回（~3.4 ms），3649 次就是 12 秒純浪費。
名稱用 Shell 免費給的 `NewName`。

**另外**：微軟文件對 `GetAnyOperationsAborted` 的措辭是
「operations can be stopped before they are complete either by user action
**or silently by the system**」—— 後半句就是 8/30 那次 3649 全失敗的官方名稱。

---

# WPD 能分辨的裝置狀態（Shell 路徑全部壓成 `0x8007001E`）

| 常數 | 值 | 意義 |
|---|---|---|
| `E_WPD_DEVICE_ALREADY_OPENED` | `0x802A0001` | 已被其他 client 開啟 |
| `E_WPD_DEVICE_NOT_OPEN` | `0x802A0002` | 尚未開啟 |
| `E_WPD_DEVICE_IS_HUNG` | `0x802A0006` | 裝置不再回應 |
| `ERROR_BUSY` | `0x800700AA` | 正在處理別的操作，**稍後重試會好** |
| `ERROR_DEVICE_IN_USE` | `0x80070964` | 被其他程式佔用 |
| `ERROR_DEVICE_NOT_CONNECTED` | `0x8007048F` | 已拔除 |
| `ERROR_GEN_FAILURE` / `ERROR_IO_DEVICE` / `ERROR_SEM_TIMEOUT` / `ERROR_TIMEOUT` | `0x8007001F` / `0x8007045D` / `0x80070079` / `0x800705B4` | 皆歸類為「裝置沒有回應」 |

**而 Shell 路徑給我們的一律是 `0x8007001E`（ERROR_READ_FAULT）。**
這就是為什麼「CopyTrans 佔用」「裝置當掉」「真的是空的」分不開 ——
也是 `core/wpd_probe.py` 存在的唯一理由（D21）。

`0x8007001E` 是 MTP 這條路的**通病，不是 iPhone 專屬**：calibre 的 MTP Windows
程式碼有一模一樣的錯誤字串
（`Failed to EnumObjects() of folder from device: [hr=0x8007001e]`），
而且它對 iPhone 根本不走 MTP（走 libimobiledevice），樣本是 Android 與電子書閱讀器。

---

# iPhone 的傳輸模式無法只靠副檔名判斷（2026-09-13）

常見說法是「看到 `.heic` 就是保留原始檔」。**那是錯的。**

「自動」模式**只有在 iOS 認為這台 PC 讀不懂 HEIC 時**才會即時轉檔。
若電腦上裝了 HEIC 解碼器（CopyTrans HEIC、微軟的 HEIF 影像擴充功能…），
iOS 可能判定「這台讀得懂」而直接給 HEIC —— 即使使用者選的是「自動」。

→ 診斷報告的「傳輸模式判定」段輸出的是**證據 + 傾向**，不是結論。
→ 要確定還是得問使用者本人（民眾B 2026-09-13 回信：**保留原始檔**）。

---

# 裝置壞掉之後，同一個 process 內不會自己恢復（2026-09-12 實測時間軸）

| 時刻 | 事件 |
|---|---|
| 16:16:02 | 啟動 #2，偵測正常，樹狀展開拿到 344 個資料夾 |
| 16:16:50 | `200807__` 0 個檔案（**9 ms**）、`201301__` 0 個檔案（**42.3 秒**） |
| 16:49:11 | **同一個 process**，probe 拿到 `0x8007001E`（壞了 32 分鐘還沒好） |
| 16:50:47 | 關閉程式 |
| 16:58:38 | 啟動 #3 |
| 16:59:34 | **同樣那兩個資料夾**：12 個、1 個檔案，複製成功 |

**32 分鐘沒自癒，關掉重開 8 分鐘後正常 → 時間解釋不了，process 邊界可以。**

08-30 那次也一樣：3649 全失敗之後，兩輪重試共 26 次列舉**每次都花 8.73~8.83 秒
回傳 0 項**（離散度小到只可能是固定逾時），直到關掉程式為止都沒恢復。

→ 這是「重新整理裝置」被改成**重建 worker 執行緒與 COM apartment** 的理由。
→ 還沒查到 WPD namespace provider 的 session 生命週期文件，
  所以這是**假設不是結論**，等民眾B 驗證。
→ 已知相關：重啟 Apple Mobile Device Service 會強制 Windows 重新列舉 iPhone
  並清掉卡住的 handle（來源品質一般，但與觀察一致）。

---

# comtypes + PyInstaller

comtypes 在 **runtime** 產生 COM wrapper 到 `comtypes/gen`，
與 PyInstaller 的凍結環境有已知衝突（`FileNotFoundError(gen_dir)` 等）。

→ `core/wpd_probe.py` 的每一個 `import` 都放在函式裡、每一步獨立 `try/except`，
  探針掛掉只會讓診斷報告少一段，**不影響任何其他功能**。
→ 這是**接受風險**而非忽視風險。若打包後探針一直不能用，
  退路是改用 ctypes 手刻 vtable 呼叫（只有 5 個呼叫，工作量可控）。
