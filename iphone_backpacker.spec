# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包設定。

    pyinstaller iphone_backpacker.spec

★ 幾個關鍵決定，都跟「使用者能不能順利把 exe 給別人用」有關：

1. **onedir，不用 onefile**（決策 D7）
   onefile 執行時會把自己解壓到 temp 再執行，這個行為在防毒的啟發式
   偵測眼中就是標準的惡意軟體脫殼特徵，誤判率高非常多。
   onedir 產生一個資料夾，壓成 zip 發布即可。

2. **UPX 關閉**
   UPX 壓縮會大幅提高防毒誤判率，省下的體積不值得。

3. **console=False**
   沒有主控台視窗。這也是為什麼全專案不准用 print ——
   此時 sys.stdout 是 None，print() 會直接 AttributeError 閃退。
   所有訊息都走 logging 寫到 %LOCALAPPDATA%\\iPhoneBackpacker\\logs。

4. **不要求管理員權限**
   uac_admin=False（預設），manifest 會是 asInvoker。
   本程式只讀 iPhone、寫使用者指定的資料夾，不需要提權。
   多跳一個 UAC 只會更嚇人。

5. **排除用不到的 Qt 模組**
   PySide6 的 hook 預設會收一大堆東西。我們只用 QtCore / QtGui /
   QtWidgets，排掉其餘的可以明顯縮小體積並縮短啟動時間 ——
   實測開發模式下 import PySide6 就要 2.3 秒，那是啟動時間的 99%。

   ⚠ 如果打包後執行失敗，第一件事就是把下面的 excludes 清空再試一次，
     確認是不是排除得太積極。
"""

EXCLUDES = [
    # 我們只用 QtCore / QtGui / QtWidgets
    "PySide6.QtNetwork",
    "PySide6.QtQml",
    "PySide6.QtQuick",
    "PySide6.QtQuickWidgets",
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
    "PySide6.Qt3DCore",
    "PySide6.Qt3DRender",
    "PySide6.QtCharts",
    "PySide6.QtDataVisualization",
    "PySide6.QtOpenGL",
    "PySide6.QtOpenGLWidgets",
    "PySide6.QtPdf",
    "PySide6.QtPdfWidgets",
    "PySide6.QtSql",
    "PySide6.QtTest",
    "PySide6.QtBluetooth",
    "PySide6.QtPositioning",
    "PySide6.QtSerialPort",
    # 明確用不到、且排除後不會影響其他套件的
    "tkinter",
    "numpy",
    "PIL",
    "matplotlib",
]

# 刻意**不**排除 email / http / xml / unittest / pydoc 這類標準庫模組。
# 它們體積不大，卻常被其他套件間接 import，排掉的風險遠大於收益。
# 真的很在意體積再回頭試，一次加一個並實測。

# ★★ comtypes 要**整包**收進來（2026-09-14 實測修正）。
#
#   comtypes 是在 **runtime** 才產生 WPD 的 COM wrapper，而那份產生出來的
#   模組會 import comtypes 的其他子模組。程式碼裡沒有任何靜態 import 指向
#   它們，PyInstaller 的分析當然找不到。
#
#   實測（打包後執行）：
#       Creating comtypes.gen package failed: [WinError 3] ..._internal\comtypes\gen
#       Created a memory-only package.
#       Using writeable comtypes cache directory: ...\Temp\comtypes_cache\...
#       WPD 探針不可用：載入 WPD 型別庫失敗（No module named 'comtypes.stream'）
#
#   —— 前面幾步都成功了（它會自己退到可寫的暫存目錄），只差 `comtypes.stream`
#   這種沒被收進來的子模組。所以不要一個一個列，整包收。
#
#   ★ 這是**選配**的：comtypes 沒裝就收不到東西，打包照樣完成，
#     只是診斷報告會少「WPD 探針」那一段。絕不能讓它擋住打包。
try:
    from PyInstaller.utils.hooks import collect_submodules
    COMTYPES_MODULES = collect_submodules("comtypes")
except Exception as exc:      # noqa: BLE001
    print("（沒有收到 comtypes，WPD 探針將不可用：{}）".format(exc))
    COMTYPES_MODULES = []

a = Analysis(
    ["run_gui.py"],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[
        # pywin32 的 shell 擴充是動態載入的，PyInstaller 不一定找得到
        "win32com.shell.shell",
        "win32com.shell.shellcon",
        "win32com.server.policy",   # IFileOperationProgressSink 的 gateway（D19）
        "pythoncom",
        "pywintypes",
        # ★ 選配：WPD 診斷探針（D21）。詳見上面的 COMTYPES_MODULES。
        #   core/wpd_probe.py 的每一個 import 都在函式裡、每一步獨立
        #   try/except，所以**就算這裡收不齊也只是診斷報告少一段**，
        #   絕不會影響備份。
    ] + COMTYPES_MODULES,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,          # onedir 的關鍵
    name="iPhoneBackpacker",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                      # ★ 不要開，會提高防毒誤判率
    console=False,                  # ★ 無主控台 → 全專案禁用 print
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    uac_admin=False,                # ★ asInvoker，不提權
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="iPhoneBackpacker",
)
