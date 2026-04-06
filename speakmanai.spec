# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for SpeakmanAI Desktop
Build: pyinstaller speakmanai.spec
Output: dist/SpeakmanAI.exe  (single file — no folder needed)
"""
import sys
from pathlib import Path

block_cipher = None

a = Analysis(
    ["launcher.py"],
    pathex=["."],
    binaries=[],
    datas=[
        # Bundle WorkflowsAndAgents JSON files
        ("WorkflowsAndAgents", "WorkflowsAndAgents"),
        # Bundle the MCP server application code
        ("orchestration/mcp-server/server.py",  "orchestration/mcp-server"),
        ("orchestration/mcp-server/engine.py",  "orchestration/mcp-server"),
        ("orchestration/mcp-server/database.py","orchestration/mcp-server"),
        ("orchestration/mcp-server/database_sqlite.py", "orchestration/mcp-server"),
    ],
    hiddenimports=[
        # FastAPI / Starlette
        "uvicorn",
        "uvicorn.logging",
        "uvicorn.loops",
        "uvicorn.loops.auto",
        "uvicorn.protocols",
        "uvicorn.protocols.http",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan",
        "uvicorn.lifespan.on",
        "starlette",
        "fastapi",
        # MCP
        "mcp",
        "mcp.server",
        "mcp.server.fastmcp",
        # LLM providers (imported conditionally — include all)
        "google.genai",
        "anthropic",
        "openai",
        # SQLite / aiosqlite
        "aiosqlite",
        # Other
        "httpx",
        "dotenv",
        "pystray",
        "PIL",
        "PIL.Image",
        "PIL.ImageDraw",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["motor", "pymongo", "server", "engine", "database", "database_sqlite"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# Single-file exe — everything bundled, no _internal folder needed
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="SpeakmanAI",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    console=False,   # No terminal window — tray-only UX
    icon=None,       # Replace with: icon="assets/speakmanai.ico"
)
