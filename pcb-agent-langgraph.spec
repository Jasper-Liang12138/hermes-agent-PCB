# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

block_cipher = None
root = Path(SPECPATH).resolve()


def maybe_data(path: str, target: str):
    source = root / path
    return [(str(source), target)] if source.exists() else []


datas = []
datas += maybe_data("config.example.ini", ".")
datas += maybe_data("convert.py", ".")
datas += maybe_data("tools", "tools")
datas += maybe_data("vendor", "vendor")

hiddenimports = [
    "websockets",
    "langgraph",
    "langchain_core",
    "pcb_agent_langgraph.websocket.server",
    "pcb_agent_langgraph.evaluation.runner",
]


a = Analysis(
    [str(root / "pcb_agent_langgraph" / "__main__.py")],
    pathex=[str(root)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "pytest",
        "unittest",
        "tkinter",
        "matplotlib.tests",
        "numpy.tests",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="agent",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="PCB-AGENT-langgraph",
)



