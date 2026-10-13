# PyInstaller recipe for the Windows desktop app. Build with build_windows.bat.
from PyInstaller.utils.hooks import collect_all

datas = [("static", "static"), ("templates", "templates"), ("fonts", "fonts"), ("models", "models")]
binaries = []
hiddenimports = ["printer", "printer_windows", "pdf417gen", "zxingcpp", "win32timezone"]
for pkg in ("onnxruntime", "webview"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(["desktop.py"], pathex=["."], datas=datas, binaries=binaries,
             hiddenimports=hiddenimports, excludes=["tkinter", "printer_mac", "printer_link"])
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="CardStudio", console=False)
coll = COLLECT(exe, a.binaries, a.datas, name="CardStudio")
