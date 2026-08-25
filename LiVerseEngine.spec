# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path


project_root = Path(SPECPATH)
bible_core_src = project_root / "packages" / "bible_parser_core" / "src"
bible_data = bible_core_src / "bible_parser_core" / "data"
model_name = "vosk-model-small-streaming-ru-0.54"
model_dir = project_root / "build_assets" / "models" / model_name

required_paths = [
    project_root / ".env.example",
    project_root / "LiVerse.ico",
    project_root / "LiVerse.png",
    project_root / "assets" / "help" / "holyrics-api-server.png",
    project_root / "assets" / "help" / "holyrics-tokens.png",
    project_root / "assets" / "help" / "holyrics-permissions.png",
    project_root / "slide_display" / "operator.html",
    bible_data / "rst.json",
    bible_data / "risk_model.json",
    bible_data / "rst_overrides.json",
    project_root / "bible_index" / "bible_index.db",
    model_dir / "am-onnx" / "encoder.onnx",
    model_dir / "am-onnx" / "decoder.onnx",
    model_dir / "am-onnx" / "joiner.onnx",
    model_dir / "lang" / "tokens.txt",
]
missing = [str(path) for path in required_paths if not path.is_file()]
if missing:
    raise SystemExit("Missing LiVerse build assets:\n  - " + "\n  - ".join(missing))

gui_analysis = Analysis(
    ["tools/liverse_gui.py"],
    pathex=[str(project_root), str(bible_core_src)],
    binaries=[],
    datas=[
        (str(project_root / ".env.example"), "."),
        (str(project_root / "LiVerse.png"), "."),
        (str(project_root / "assets" / "help"), "assets/help"),
    ],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

engine_analysis = Analysis(
    ["tools/vosk_grammar_probe.py"],
    pathex=[str(project_root), str(bible_core_src)],
    binaries=[],
    datas=[
        (str(bible_data / "rst.json"), "bible_parser_core/data"),
        (str(bible_data / "risk_model.json"), "bible_parser_core/data"),
        (str(bible_data / "rst_overrides.json"), "bible_parser_core/data"),
        (str(project_root / "bible_index" / "bible_index.db"), "bible_index"),
        (str(model_dir), f".cache/liverse/models/{model_name}"),
        (str(project_root / "slide_display"), "slide_display"),
    ],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

gui_pyz = PYZ(gui_analysis.pure)
engine_pyz = PYZ(engine_analysis.pure)

gui_exe = EXE(
    gui_pyz,
    gui_analysis.scripts,
    [],
    exclude_binaries=True,
    name="LiVerse",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    icon=str(project_root / "LiVerse.ico"),
)

engine_exe = EXE(
    engine_pyz,
    engine_analysis.scripts,
    [
        ("X utf8", None, "OPTION"),
        ("u", None, "OPTION"),
    ],
    exclude_binaries=True,
    name="LiVerseEngine",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    icon=str(project_root / "LiVerse.ico"),
)

coll = COLLECT(
    gui_exe,
    engine_exe,
    gui_analysis.binaries,
    gui_analysis.datas,
    engine_analysis.binaries,
    engine_analysis.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="LiVerse",
)
