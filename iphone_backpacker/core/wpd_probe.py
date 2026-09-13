"""WPD 診斷探針（決策 D21）。

★★ 這個模組**只回答問題，不搬任何資料**。

  為什麼需要它：Shell 路徑（IShellFolder）把所有裝置狀態都壓成同一個
  `0x8007001E`（ERROR_READ_FAULT，一個泛用的磁碟讀取錯誤）。
  於是「別的軟體佔用中」「裝置當掉」「資料夾真的是空的」在原理上分不開 ——
  那正是民眾B 的案子卡住的地方。

  WPD 分得出來：

      E_WPD_DEVICE_ALREADY_OPENED  0x802A0001  已被其他程式開啟
      E_WPD_DEVICE_IS_HUNG         0x802A0006  裝置不再回應
      ERROR_BUSY                   0x800700AA  忙碌中，稍後重試會好
      ERROR_DEVICE_IN_USE          0x80070964  被其他程式佔用
      ERROR_DEVICE_NOT_CONNECTED   0x8007048F  已拔除
      成功 Open                                裝置本身是好的，問題在 Shell 層

  另外 `GetDevices()` 本身就是「iPhone 到底在不在」的**第二個獨立答案**，
  完全不經過 Shell 的列舉 —— 民眾B 15:55 那次 Apple iPhone 從候選裡消失，
  有了這個就不必再靠推論。

★ **刻意不當資料路徑**（決策 D21）。走 WPD 搬檔案等於重做一次整個專案：
  位置的表示、錯誤模型、進度、取消全部要另寫一套，而 `IFileOperation`
  的原生進度視窗（D12 的全部價值）會歸零。我們要的是錯誤解析度，不是傳輸能力。

★ **失敗不是錯誤。** comtypes 沒裝、型別庫產不出來、Open 被拒、
  apartment 不合 —— 一律回報「探針不可用」外加原因，不影響程式任何功能。
  所有 import 都放在函式裡，模組本身在沒有 comtypes 的機器上也 import 得起來。

★ 打包注意：comtypes 會在 **runtime** 產生 wrapper（`comtypes/gen`），
  這與 PyInstaller 有已知衝突。所以這裡的每一步都獨立 try/except，
  探針掛掉只會讓診斷報告少一段。詳見 docs/BUILD.md。
"""

import logging
from dataclasses import dataclass, field
from typing import List, Optional

log = logging.getLogger(__name__)

#: 只做這幾件事，做完立刻關掉。不列舉內容、不讀任何檔案。
_STEPS = "GetDevices → 讀名稱 → Open → Close"


@dataclass
class WpdDevice:
    device_id: str = ""
    friendly_name: str = ""
    manufacturer: str = ""
    description: str = ""
    #: Open() 的結果。None = 沒試成、0 = 成功、其他 = HRESULT。
    open_hresult: Optional[int] = None
    open_error: str = ""

    @property
    def opened(self):
        return self.open_hresult == 0


@dataclass
class WpdReport:
    available: bool = False
    #: 探針為什麼不能用（available=False 時才有意義）。
    unavailable_reason: str = ""
    devices: List[WpdDevice] = field(default_factory=list)
    #: 走到一半失敗時的說明。
    error: str = ""


def _load_modules():
    """載入 WPD 的型別庫。回傳 (portabledeviceapi, portabledevicetypes)。

    ★ import 放在函式裡是刻意的 —— 沒有 comtypes 的機器上，
      這個模組本身仍然要 import 得起來。
    """
    from comtypes.client import GetModule       # noqa: PLC0415

    # GetModule 直接回傳產生好的模組，不必去猜 comtypes.gen 底下那些
    # 以 GUID 命名的檔名。
    api = GetModule("portabledeviceapi.dll")
    types = GetModule("portabledevicetypes.dll")
    return api, types


def _device_ids(manager):
    """兩段式呼叫：先問幾台，再要清單。這是 WPD 的標準用法。"""
    from ctypes import POINTER, c_ulong, c_wchar_p, pointer   # noqa: PLC0415

    count = pointer(c_ulong(0))
    manager.GetDevices(POINTER(c_wchar_p)(), count)
    if not count.contents.value:
        return []
    buffer = (c_wchar_p * count.contents.value)()
    manager.GetDevices(buffer, count)
    return [device_id for device_id in buffer if device_id]


def _string_property(getter, device_id):
    """同樣的兩段式呼叫，用來取名稱那幾個字串屬性。"""
    from ctypes import c_ulong, create_unicode_buffer, pointer, wstring_at  # noqa: PLC0415, E501

    length = pointer(c_ulong(0))
    getter(device_id, None, length)
    if not length.contents.value:
        return ""
    buffer = create_unicode_buffer(length.contents.value)
    getter(device_id, buffer, length)
    return wstring_at(buffer)


def probe():
    """問 WPD：有哪些裝置、每一台現在是什麼狀態。

    回傳 `WpdReport`。**這個函式不會拋例外。**
    """
    report = WpdReport()

    try:
        import comtypes                          # noqa: PLC0415
        from comtypes.client import CreateObject  # noqa: PLC0415
    except Exception as exc:                      # noqa: BLE001
        report.unavailable_reason = "沒有 comtypes（{}）".format(exc)
        log.info("WPD 探針不可用：%s", report.unavailable_reason)
        return report

    try:
        api, types = _load_modules()
    except Exception as exc:                      # noqa: BLE001
        report.unavailable_reason = "載入 WPD 型別庫失敗（{}）".format(exc)
        log.info("WPD 探針不可用：%s", report.unavailable_reason)
        return report

    try:
        manager = CreateObject(
            api.PortableDeviceManager,
            clsctx=comtypes.CLSCTX_INPROC_SERVER,
            interface=api.IPortableDeviceManager,
        )
    except Exception as exc:                      # noqa: BLE001
        report.unavailable_reason = "建立 PortableDeviceManager 失敗（{}）".format(exc)
        log.info("WPD 探針不可用：%s", report.unavailable_reason)
        return report

    report.available = True

    try:
        ids = _device_ids(manager)
    except Exception as exc:                      # noqa: BLE001
        report.error = "GetDevices 失敗：{}".format(exc)
        log.warning("WPD GetDevices 失敗：%s", exc)
        return report

    for device_id in ids:
        entry = WpdDevice(device_id=device_id)
        # 名稱三個屬性各自獨立 —— 任何一個取不到都不該讓整台裝置消失。
        for attr, getter_name in (("friendly_name", "GetDeviceFriendlyName"),
                                  ("manufacturer", "GetDeviceManufacturer"),
                                  ("description", "GetDeviceDescription")):
            try:
                getter = getattr(manager, getter_name)
                setattr(entry, attr, _string_property(getter, device_id))
            except Exception as exc:              # noqa: BLE001
                log.debug("WPD %s 取不到：%s", getter_name, exc)

        _try_open(entry, api, types, comtypes, CreateObject)
        report.devices.append(entry)

    log.info("WPD 探針：看到 %d 台裝置", len(report.devices))
    for entry in report.devices:
        log.info("    %-28s open=%s", entry.friendly_name or entry.device_id,
                 "成功" if entry.opened else entry.open_error or entry.open_hresult)
    return report


def _try_open(entry, api, types, comtypes, CreateObject):
    """開一下再立刻關掉。**這一步的 HRESULT 就是我們要的答案。**

    ★ 開完一定要關。這個探針絕不能變成「佔用裝置的那個程式」——
      那正是我們在懷疑 CopyTrans 做的事。
    """
    device = None
    try:
        client_info = CreateObject(
            types.PortableDeviceValues,
            clsctx=comtypes.CLSCTX_INPROC_SERVER,
            interface=types.IPortableDeviceValues,
        )
        device = CreateObject(
            api.PortableDevice,
            clsctx=comtypes.CLSCTX_INPROC_SERVER,
            interface=api.IPortableDevice,
        )
        device.Open(entry.device_id, client_info)
        entry.open_hresult = 0
    except Exception as exc:                      # noqa: BLE001
        entry.open_hresult = getattr(exc, "hresult", None)
        entry.open_error = str(exc)
        if entry.open_hresult is not None:
            entry.open_hresult &= 0xFFFFFFFF
        return
    finally:
        if device is not None:
            try:
                device.Close()
            except Exception as exc:              # noqa: BLE001
                log.debug("WPD Close 失敗：%s", exc)
