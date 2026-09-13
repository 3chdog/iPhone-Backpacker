"""把 pywin32 換成假的，讓 Linux 上也能測**真正的** core 程式碼。

★★ 為什麼值得這樣做：

  `tests/test_device_classify.py` 用的是「對照實作」—— 在測試檔裡把判斷
  邏輯重寫一遍。那擋得住邏輯錯誤，卻擋不住「真正跑的那份 code 跟對照實作
  長得不一樣」，而本專案已經被「假設某個 API 存在」（`shell.SHBindToParent`）
  害過一次了。

  列舉的重試／重驗邏輯是整個備份工具**最關鍵**的一段：它判斷的是
  「這個資料夾到底有沒有東西」，判斷錯的代價是使用者以為備份好了、
  實際上照片沒帶走。這一段必須測真品。

  所以這裡只假造 pywin32 的**最外層邊界**（`com_error` 與幾個常數），
  `shell_ns` 裡的每一行控制流程都是真的在跑。

★ 呼叫 `install()` 必須在 import 任何 `iphone_backpacker.core` 模組**之前**。
"""

import sys
import types

_SHELLCON_VALUES = {
    "SHCONTF_FOLDERS": 0x0020,
    "SHCONTF_NONFOLDERS": 0x0040,
    "SHGDN_NORMAL": 0x0000,
    "SHGDN_FORPARSING": 0x8000,
    "CSIDL_DRIVES": 0x0011,
    "CSIDL_DESKTOPDIRECTORY": 0x0010,
    "FOF_NOCONFIRMATION": 0x0010,
    "FOF_NOERRORUI": 0x0400,
    "SIGDN_DESKTOPABSOLUTEPARSING": 0x80028000,
    "SHGDFIL_DESCRIPTIONID": 1,
}

_SHELL_IIDS = (
    "IID_IShellFolder", "IID_IShellItem", "IID_IEnumIDList",
    "IID_IFileOperation", "IID_IFileOperationProgressSink",
    "CLSID_FileOperation",
)

_SHELL_FUNCTIONS = (
    "SHGetDesktopFolder", "SHGetSpecialFolderLocation",
    "SHCreateItemFromIDList", "SHCreateItemFromParsingName",
    "SHGetPathFromIDList", "SHGetNameFromIDList",
    "SHParseDisplayName", "SHILCreateFromPath", "SHGetFolderPath",
)


def _unsupported(name):
    def call(*_args, **_kwargs):
        raise AssertionError(
            "測試裡不應該真的呼叫 {} —— 請用 monkeypatch 換掉它".format(name))
    return call


def install():
    """裝上假的 pywin32。重複呼叫是安全的，回傳假的 pythoncom 模組。"""
    if "pythoncom" in sys.modules:
        return sys.modules["pythoncom"]

    pythoncom = types.ModuleType("pythoncom")

    class com_error(Exception):
        """對應 pywintypes.com_error。args[0] 是 HRESULT。"""

    pythoncom.com_error = com_error
    pythoncom.CoInitialize = lambda: None
    pythoncom.CoUninitialize = lambda: None
    pythoncom.CLSCTX_ALL = 0x17
    pythoncom.CoCreateInstance = _unsupported("CoCreateInstance")
    pythoncom.WrapObject = lambda obj, _iid: obj
    sys.modules["pythoncom"] = pythoncom

    win32com = types.ModuleType("win32com")
    win32com.__path__ = []

    shell_pkg = types.ModuleType("win32com.shell")
    shell_pkg.__path__ = []

    shell = types.ModuleType("win32com.shell.shell")
    for iid in _SHELL_IIDS:
        setattr(shell, iid, iid)
    for name in _SHELL_FUNCTIONS:
        setattr(shell, name, _unsupported(name))
    # ★ 刻意**不定義** SHBindToParent / SHGetDataFromIDList ——
    #   真實的 pywin32 就是沒有它們，api_report() 必須看得出這件事。

    shellcon = types.ModuleType("win32com.shell.shellcon")
    for name, value in _SHELLCON_VALUES.items():
        setattr(shellcon, name, value)

    shell_pkg.shell = shell
    shell_pkg.shellcon = shellcon
    win32com.shell = shell_pkg

    server_pkg = types.ModuleType("win32com.server")
    server_pkg.__path__ = []
    policy = types.ModuleType("win32com.server.policy")

    class DesignatedWrapPolicy:
        def _wrap_(self, _obj):
            return None

    policy.DesignatedWrapPolicy = DesignatedWrapPolicy
    server_pkg.policy = policy
    win32com.server = server_pkg

    sys.modules["win32com"] = win32com
    sys.modules["win32com.shell"] = shell_pkg
    sys.modules["win32com.shell.shell"] = shell
    sys.modules["win32com.shell.shellcon"] = shellcon
    sys.modules["win32com.server"] = server_pkg
    sys.modules["win32com.server.policy"] = policy
    return pythoncom
