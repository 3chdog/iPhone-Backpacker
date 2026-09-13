# 自己打包成 exe

如果你不想用 Releases 裡打包好的 `iPhoneBackpacker.zip`（很合理 —— 你沒辦法確認
別人打包的東西裡裝了什麼），可以自己從原始碼打包一份。

沒有網路的話，請先看 [OFFLINE-INSTALL.md](OFFLINE-INSTALL.md)。

---

## 需要什麼

- **Windows 10 / 11**（64 位元）
- **Python 3.12**
- 執行程式本身需要的兩個套件：`pywin32`、`PySide6`
- **打包才需要的**：`pyinstaller`

```bash
git clone https://github.com/3chdog/iPhone-Backpacker.git
cd iPhone-Backpacker

pip install -r requirements.txt
pip install pyinstaller
```

`pip install pyinstaller` 會連帶裝進這些相依套件：
`pyinstaller-hooks-contrib`、`altgraph`、`packaging`、`pefile`、
`pywin32-ctypes`、`setuptools`。

---

## 先確認原始碼跑得起來

打包之前先確定程式本身沒問題：

```bash
python run_gui.py
```

視窗能開、能看到 iPhone 的資料夾，就可以往下走。

---

## 打包

```bash
build.bat
```

或者直接跑（一樣的東西，只是跳過 `.bat` 那層）：

```bash
python tools/build.py
```

成功的話會看到：

```
完成：C:\iPhone-Backpacker\dist\iPhoneBackpacker\iPhoneBackpacker.exe
資料夾大小：113 MB
```

產物在 `dist\iPhoneBackpacker\`，裡面是 `iPhoneBackpacker.exe` 加上一個
`_internal` 資料夾。**Python 直譯器與所有套件都在 `_internal` 裡**，
所以這個資料夾拿到沒裝 Python 的電腦上也能執行。

> ⚠ 要發布或搬移，請把**整個資料夾**壓成 zip。只複製 `.exe` 是不會動的。

---

## 打包設定裡的幾個決定

都寫在 `iphone_backpacker.spec` 的註解裡，這裡摘要：

| 設定 | 為什麼 |
|---|---|
| **onedir**，不用 onefile | onefile 執行時會把自己解壓到 temp 再執行，這在防毒的啟發式偵測眼中是標準的惡意軟體脫殼特徵，誤判率高很多 |
| **`upx=False`** | UPX 壓縮會大幅提高防毒誤判率，省下的體積不值得 |
| **`console=False`** | 沒有主控台視窗。這也是為什麼整個專案不准用 `print()` —— 此時 `sys.stdout` 是 `None`，`print()` 會直接 `AttributeError` 閃退 |
| **`uac_admin=False`** | manifest 是 `asInvoker`，不要求管理員權限。程式只讀 iPhone、寫使用者指定的資料夾，不需要提權 |
| 排除用不到的 Qt 模組 | PySide6 的 hook 預設會收一大堆東西，我們只用 QtCore / QtGui / QtWidgets |

---

## 打包失敗的話

### `ModuleNotFoundError` / `ImportError`

多半是 `iphone_backpacker.spec` 的 `EXCLUDES` 排掉了某個間接被 import 的模組。

**把 `EXCLUDES` 改成空 list 再打包一次**：

```python
EXCLUDES = []
```

這樣會變大一點、啟動慢一點，但一定能跑。確認能跑之後再一個一個加回去。

### 打包成功但執行時閃退

執行檔沒有主控台，看不到錯誤訊息。去看紀錄檔：

```
%LOCALAPPDATA%\iPhoneBackpacker\logs\backpacker.log
```

如果紀錄檔根本沒產生，代表在 logging 初始化之前就掛了 —— 通常是缺少套件。
可以暫時把 `iphone_backpacker.spec` 的 `console=False` 改成 `console=True`
重新打包，這樣就會有主控台視窗顯示 traceback。**查完記得改回來。**

---

## 打包後的啟動時間

從原始碼跑的時候，`import PySide6` 大約要 2.3 秒，佔啟動時間的 99%。
打包後這段會快很多。實際數字會印在紀錄檔第一行：

```
啟動耗時：import 1013 ms + 我們的初始化 33 ms（合計 1046 ms）
```

第一次啟動因為 Windows 還沒把那些 DLL 讀進檔案快取，會比後續慢很多，
這是正常的。

## 關於 comtypes（v1.0.2-rc.2 起）

`requirements.txt` 多了一個 **選配** 套件 `comtypes`，只給 WPD 診斷探針用
（`core/wpd_probe.py`，決策 D21）。

**它壞掉不影響任何功能** —— 診斷報告只會少「WPD 探針」那一段。

已知風險：comtypes 會在 **runtime** 產生 COM wrapper 到 `comtypes/gen`，
這和 PyInstaller 的凍結環境有已知衝突。程式裡每一個 `import` 都放在函式內、
每一步獨立 `try/except`，所以最壞的情況也只是報告少一段。

**如果打包時因為 comtypes 出錯**：把 `iphone_backpacker.spec` 的
`hiddenimports` 裡這三行刪掉再打包一次就好，其他功能完全不受影響。

```
"comtypes",
"comtypes.client",
"comtypes.gen",
```
