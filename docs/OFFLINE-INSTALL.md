# 離線安裝

給**要執行原始碼或自己打包、但那台 Windows 沒有網路**的情況。

有網路的話不用看這份，直接 `pip install -r requirements.txt` 就好。

作法是在**另一台有網路的電腦**上把 `.whl` 檔下載好，複製到目標電腦後用
`pip install` 安裝本機檔案。

---

## 先確認 Python

目標電腦需要 **Python 3.12（Windows 64 位元）**。
沒有的話要先從 [python.org](https://www.python.org/downloads/) 下載安裝檔一起帶過去。

下面所有 `.whl` 都是給 **Python 3.12 / Windows 64 位元**用的。
Python 版本不同（3.11、3.13…）就要抓對應版本的檔案，不能混用。

---

## A. 只想執行原始碼（`python run_gui.py`）

需要 **5 個檔案**。`pywin32` 是一個，`PySide6` 會拆成四個。

| 套件 | 檔名 | 下載連結 |
|---|---|---|
| pywin32 306 | `pywin32-306-cp312-cp312-win_amd64.whl` | [下載](https://files.pythonhosted.org/packages/83/1c/25b79fc3ec99b19b0a0730cc47356f7e2959863bf9f3cd314332bddb4f68/pywin32-306-cp312-cp312-win_amd64.whl) |
| PySide6 6.7.3 | `PySide6-6.7.3-cp39-abi3-win_amd64.whl` | [下載](https://files.pythonhosted.org/packages/69/f0/18c6b7f5087eec0d673eb8703c3e9eb76d62cfc427b015d4fc833287c1c4/PySide6-6.7.3-cp39-abi3-win_amd64.whl) |
| shiboken6 6.7.3 | `shiboken6-6.7.3-cp39-abi3-win_amd64.whl` | [下載](https://files.pythonhosted.org/packages/3e/cc/e2b95aedb5e5f900c20325ef347e51c7750d18369d157f9b7da99943dae1/shiboken6-6.7.3-cp39-abi3-win_amd64.whl) |
| PySide6_Essentials 6.7.3 | `PySide6_Essentials-6.7.3-cp39-abi3-win_amd64.whl` | [下載](https://files.pythonhosted.org/packages/0b/18/8679adff0b7a6ccacc2e0febd15c1b78d02abc0180b56007c24e94c9d582/PySide6_Essentials-6.7.3-cp39-abi3-win_amd64.whl) |
| PySide6_Addons 6.7.3 | `PySide6_Addons-6.7.3-cp39-abi3-win_amd64.whl` | [下載](https://files.pythonhosted.org/packages/3e/ca/1b34d0785298f86bed27582103760f67f7f78d9e2006405e256e9c770993/PySide6_Addons-6.7.3-cp39-abi3-win_amd64.whl) |

> `PySide6` 這個套件本身只是個殼，實際的東西在 `PySide6_Essentials`、
> `PySide6_Addons` 和 `shiboken6` 裡，所以四個都要。

### 安裝

把上面五個檔案放進同一個資料夾（例如 `C:\wheels`），然後：

```bash
pip install --no-index --find-links=C:\wheels pywin32 PySide6
```

`--no-index` 表示不要連 PyPI，`--find-links` 指向你放 `.whl` 的資料夾。
pip 會自己從那個資料夾找出相依的其他檔案。

裝完就可以：

```bash
python run_gui.py
```

---

## B. 還想自己打包成 exe

在 A 的五個檔案之外，**再加 7 個**：

| 檔名 | 大小 | 下載連結 |
|---|---|---|
| `pyinstaller-6.3.0-py3-none-win_amd64.whl` | 1.2 MB | [下載](https://files.pythonhosted.org/packages/fc/2b/72fe6fa39f0353331f0e8c54e0915768ad48e0bff0433d10353ba81ffea5/pyinstaller-6.3.0-py3-none-win_amd64.whl) |
| `pyinstaller_hooks_contrib-2024.0-py2.py3-none-any.whl` | 319 KB | [下載](https://files.pythonhosted.org/packages/80/7a/00aac9e2db19df940adc5681a1f0cde0ed3aab3655a84279106db7ef0275/pyinstaller_hooks_contrib-2024.0-py2.py3-none-any.whl) |
| `altgraph-0.17.4-py2.py3-none-any.whl` | 20 KB | [下載](https://files.pythonhosted.org/packages/4d/3f/3bc3f1d83f6e4a7fcb834d3720544ca597590425be5ba9db032b2bf322a2/altgraph-0.17.4-py2.py3-none-any.whl) |
| `packaging-23.2-py3-none-any.whl` | 51 KB | [下載](https://files.pythonhosted.org/packages/ec/1a/610693ac4ee14fcdf2d9bf3c493370e4f2ef7ae2e19217d7a237ff42367d/packaging-23.2-py3-none-any.whl) |
| `pefile-2023.2.7-py3-none-any.whl` | 70 KB | [下載](https://files.pythonhosted.org/packages/55/26/d0ad8b448476d0a1e8d3ea5622dc77b916db84c6aa3cb1e1c0965af948fc/pefile-2023.2.7-py3-none-any.whl) |
| `pywin32_ctypes-0.2.2-py3-none-any.whl` | 29 KB | [下載](https://files.pythonhosted.org/packages/a4/bc/78b2c00cc64c31dbb3be42a0e8600bcebc123ad338c3b714754d668c7c2d/pywin32_ctypes-0.2.2-py3-none-any.whl) |
| `setuptools-69.5.1-py3-none-any.whl` | 873 KB | [下載](https://files.pythonhosted.org/packages/f7/29/13965af254e3373bceae8fb9a0e6ea0d0e571171b80d6646932131d6439b/setuptools-69.5.1-py3-none-any.whl) |

### 安裝

一樣全部放進 `C:\wheels`：

```bash
pip install --no-index --find-links=C:\wheels pywin32 PySide6 pyinstaller
```

然後照 [BUILD.md](BUILD.md) 打包。

---

## 想自己抓最新版怎麼辦

上面的連結是 v1.0.0 實測驗證過的版本。如果你要換版本，
在**有網路的電腦**上這樣抓最乾淨：

```bash
pip download -r requirements.txt -d wheels --only-binary=:all: ^
    --python-version 312 --platform win_amd64
```

`pip download` 會把套件和它的所有相依項目一起抓下來，不用自己一個一個找。
要打包的話再加一次：

```bash
pip download pyinstaller -d wheels --only-binary=:all: ^
    --python-version 312 --platform win_amd64
```

把 `wheels` 資料夾整個複製到目標電腦即可。

---

## 版本必須對得起來

`.whl` 的檔名就寫著它適用的環境：

```
pywin32-306-cp312-cp312-win_amd64.whl
         ^^^  ^^^^^        ^^^^^^^^^
         版本 Python 3.12   Windows 64 位元
```

`cp39-abi3` 表示「Python 3.9 以上通用」，所以 PySide6 那幾個檔案在 3.12 上也能裝。

裝錯版本 pip 會直接拒絕，訊息通常是
`is not a supported wheel on this platform`。

---

## D. 選配：comtypes（v1.0.2-rc.2 起）

只給 **WPD 診斷探針**用（`core/wpd_probe.py`）。**沒有它程式完全正常**，
只是診斷報告會少「WPD 探針」那一段，而那一段能分辨
「裝置被其他軟體佔用」「裝置當掉」「資料夾真的是空的」——
Shell 那條路把這三種全部壓成同一個 `0x8007001E`。

| 套件 | 檔名 | 下載處 |
|---|---|---|
| comtypes 1.4.16 | `comtypes-1.4.16-py3-none-any.whl` | [PyPI](https://pypi.org/project/comtypes/1.4.16/#files) |

**這是 universal wheel**（`py3-none-any`）—— 不分 Python 版本、不分 32/64 位元，
一個檔案到處都能裝，也不需要編譯器。

```
pip install comtypes-1.4.16-py3-none-any.whl
```

### 沒裝會怎樣

打包時 PyInstaller 會印兩行 `ERROR: Hidden import 'comtypes.client' not found`，
**但打包會正常完成**，產出的 exe 一切功能正常。
執行時診斷報告的「六、WPD 探針」那一段會寫「沒有 comtypes」。

如果你不想看到那兩行 ERROR，把 `iphone_backpacker.spec` 的 `hiddenimports`
裡這三行刪掉即可：

```
"comtypes",
"comtypes.client",
"comtypes.gen",
```
