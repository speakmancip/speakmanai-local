"""
SPEAKMAN.AI Desktop Launcher
Entry point for the PyInstaller .exe build.
Loads config.json → sets env vars → starts uvicorn → opens /setup in browser → shows tray icon.
"""
import json
import logging
import os
import sys
import threading
import time
import webbrowser
from pathlib import Path

LOG_PATH = Path.home() / ".speakmanai" / "speakmanai.log"
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

# In windowed (console=False) PyInstaller builds stdout/stderr are None.
# Uvicorn's log formatter calls sys.stderr.isatty() and crashes if it's None.
# Redirect both to the log file so all output is captured.
if sys.stdout is None:
    sys.stdout = open(LOG_PATH, "a", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(LOG_PATH, "a", encoding="utf-8")

_handlers = [logging.FileHandler(LOG_PATH, encoding="utf-8")]
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=_handlers,
)
log = logging.getLogger(__name__)

CONFIG_PATH = Path.home() / ".speakmanai" / "config.json"
PORT = int(os.environ.get("SPEAKMANAI_PORT", "8000"))


def load_config_to_env() -> None:
    """Read ~/.speakmanai/config.json and inject values into the process environment."""
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            mapping = {
                "llm_provider":      "LLM_PROVIDER",
                "gemini_api_key":    "GEMINI_API_KEY",
                "anthropic_api_key": "ANTHROPIC_API_KEY",
                "openai_api_key":    "OPENAI_API_KEY",
                "ollama_base_url":   "OLLAMA_BASE_URL",
                "default_model":     "DEFAULT_MODEL",
                "advanced_model":    "ADVANCED_MODEL",
            }
            for cfg_key, env_key in mapping.items():
                val = cfg.get(cfg_key)
                if val:
                    os.environ[env_key] = str(val)
        except Exception as e:
            log.warning(f"Could not read config.json: {e}")

    # Always run in SQLite mode from the launcher
    os.environ["USE_SQLITE"] = "true"
    os.environ.setdefault(
        "SPEAKMANAI_DB_PATH",
        str(Path.home() / ".speakmanai" / "speakmanai.db"),
    )
    os.environ.setdefault("LLM_PROVIDER", "gemini")


def start_server() -> None:
    """Start uvicorn in a background thread. Adjusts sys.path for the PyInstaller bundle."""
    import importlib.util
    import uvicorn

    meipass = getattr(sys, "_MEIPASS", None)
    server_dir = (
        Path(meipass) / "orchestration" / "mcp-server"
        if meipass
        else Path(__file__).parent / "orchestration" / "mcp-server"
    )
    log.info(f"server_dir: {server_dir}")
    log.info(f"server.py exists: {(server_dir / 'server.py').exists()}")

    # Ensure sibling modules (engine, database, etc.) are importable from same dir
    if str(server_dir) not in sys.path:
        sys.path.insert(0, str(server_dir))

    try:
        # Load server.py directly from disk — bypasses PyInstaller's compiled archive
        server_path = server_dir / "server.py"
        spec = importlib.util.spec_from_file_location("server", server_path)
        server_module = importlib.util.module_from_spec(spec)
        sys.modules["server"] = server_module
        spec.loader.exec_module(server_module)
        log.info("server.py loaded successfully")
    except Exception as e:
        log.exception(f"Failed to load server.py: {e}")
        return

    try:
        uvicorn.run(
            server_module.app,
            host="127.0.0.1",
            port=PORT,
            log_level="info",
        )
    except Exception as e:
        log.exception(f"uvicorn failed to start: {e}")


def open_browser() -> None:
    """Wait for the server to be ready, then open the setup page."""
    import httpx
    for _ in range(30):
        try:
            httpx.get(f"http://127.0.0.1:{PORT}/health", timeout=1.0)
            webbrowser.open(f"http://127.0.0.1:{PORT}/setup")
            return
        except Exception:
            time.sleep(1)
    log.error("Server did not start in time.")


def run_tray() -> None:
    """Show a system tray icon with Open / Quit menu items."""
    try:
        import pystray
        from PIL import Image, ImageDraw

        # Simple icon: dark background, green rounded square
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([4, 4, 60, 60], radius=12, fill="#00ff7f")

        def on_open(_icon, _item):
            webbrowser.open(f"http://127.0.0.1:{PORT}/setup")

        def on_quit(icon, _item):
            icon.stop()
            os._exit(0)

        icon = pystray.Icon(
            "SPEAKMAN.AI",
            img,
            "SPEAKMAN.AI — Running on port " + str(PORT),
            menu=pystray.Menu(
                pystray.MenuItem("Open SPEAKMAN.AI", on_open, default=True),
                pystray.MenuItem("Quit", on_quit),
            ),
        )
        icon.run()
    except ImportError:
        log.info("pystray/Pillow not installed — running without tray icon. Press Ctrl+C to quit.")
        # Keep the process alive so uvicorn thread keeps running
        try:
            while True:
                time.sleep(60)
        except KeyboardInterrupt:
            os._exit(0)


def main() -> None:
    load_config_to_env()

    server_thread = threading.Thread(target=start_server, daemon=True)
    server_thread.start()

    browser_thread = threading.Thread(target=open_browser, daemon=True)
    browser_thread.start()

    run_tray()


if __name__ == "__main__":
    main()
