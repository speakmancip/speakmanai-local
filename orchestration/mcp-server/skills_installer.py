"""
Install the bundled SPEAKMAN.AI skills into Claude Code, at user level.

The bundle is staged by tools/bundle_skills.py and shipped inside the exe. Installing copies
the skills to ~/.claude/skills and the supporting assets to ~/.claude/speakmanai-cc, the same
places speakmanai-cc's install.ps1 uses, but unattended and file by file:

- a file the user has not touched since the last install is overwritten
- a file that differs from what was last installed (edited, or installed some other way) is
  copied to ~/.claude/speakmanai-cc/backups/<timestamp>/ first
- files in a skill folder that the bundle does not own are left alone

~/.claude/speakmanai-cc/installed.json records the installed version and a hash per file.
"""
import hashlib
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

MARKER_NAME = "installed.json"


def bundle_dir() -> Path:
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass) / "skills_bundle"
    return Path(__file__).resolve().parent.parent.parent / "build" / "skills_bundle"


def load_manifest(bundle: Path | None = None) -> dict | None:
    try:
        return json.loads(((bundle or bundle_dir()) / "manifest.json").read_text(encoding="utf-8"))
    except Exception:
        return None


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _asset_dir(home: Path) -> Path:
    return home / ".claude" / "speakmanai-cc"


def _dest(home: Path, rel: str) -> Path | None:
    """Map a bundle path ("skills/..." or "assets/...") to where it is installed."""
    head, _, tail = rel.partition("/")
    if not tail or ".." in Path(tail).parts:
        return None
    if head == "skills":
        return home / ".claude" / "skills" / tail
    if head == "assets":
        return _asset_dir(home) / tail
    return None


def _load_marker(home: Path) -> dict:
    try:
        return json.loads((_asset_dir(home) / MARKER_NAME).read_text(encoding="utf-8"))
    except Exception:
        return {}


def _classify(home: Path, manifest: dict, marker: dict) -> dict[str, str]:
    """rel -> "missing" | "current" | "outdated" (safe to overwrite) | "modified" (back up first)."""
    last = marker.get("files", {})
    result = {}
    for rel, new_hash in manifest.get("claude_code", {}).items():
        dest = _dest(home, rel)
        if dest is None:
            continue
        if not dest.is_file():
            result[rel] = "missing"
            continue
        current = _sha256(dest)
        if current == new_hash:
            result[rel] = "current"
        elif last.get(rel) == current:
            result[rel] = "outdated"
        else:
            result[rel] = "modified"
    return result


def status(home: Path | None = None, bundle: Path | None = None) -> dict:
    home = home or Path.home()
    manifest = load_manifest(bundle)
    if not manifest:
        return {"bundled": False, "state": "not_bundled", "message": "This build does not include the skills."}
    marker = _load_marker(home)
    counts = {"missing": 0, "current": 0, "outdated": 0, "modified": 0}
    for kind in _classify(home, manifest, marker).values():
        counts[kind] += 1
    total = sum(counts.values())
    if counts["current"] == total:
        state = "up_to_date"
    elif counts["missing"] == total:
        state = "not_installed"
    else:
        state = "update_available"
    return {
        "bundled": True,
        "state": state,
        "bundled_version": manifest.get("version"),
        "installed_version": marker.get("version"),
        "skills": len(manifest.get("skills", [])),
        "counts": counts,
        "claude_code_detected": (home / ".claude").is_dir(),
        "downloads": sorted(manifest.get("downloads", {})),
    }


def install(home: Path | None = None, bundle: Path | None = None) -> dict:
    home = home or Path.home()
    bundle = bundle or bundle_dir()
    manifest = load_manifest(bundle)
    if not manifest:
        return {"ok": False, "message": "This build does not include the skills."}
    if not (home / ".claude").is_dir():
        return {"ok": False, "message": "Claude Code not detected (no .claude folder in your home directory). Install Claude Code, run it once, then try again."}

    marker = _load_marker(home)
    kinds = _classify(home, manifest, marker)
    backup_root = _asset_dir(home) / "backups" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    counts = {"installed": 0, "updated": 0, "unchanged": 0, "backed_up": 0}
    src_root = bundle / "claude-code"

    for rel, kind in kinds.items():
        dest = _dest(home, rel)
        src = src_root / rel
        if kind == "current":
            counts["unchanged"] += 1
            continue
        if kind == "modified":
            backup = backup_root / rel
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dest, backup)
            counts["backed_up"] += 1
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
        counts["installed" if kind == "missing" else "updated"] += 1

    asset_dir = _asset_dir(home)
    asset_dir.mkdir(parents=True, exist_ok=True)
    new_marker = {
        "version": manifest.get("version"),
        "commit": manifest.get("commit"),
        "installed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "files": {rel: manifest["claude_code"][rel] for rel in kinds},
    }
    tmp = asset_dir / (MARKER_NAME + ".tmp")
    tmp.write_text(json.dumps(new_marker, indent=2), encoding="utf-8")
    tmp.replace(asset_dir / MARKER_NAME)

    changed = counts["installed"] + counts["updated"]
    if changed == 0:
        message = f"Skills already up to date (version {manifest.get('version')})."
    else:
        message = f"Installed {len(manifest.get('skills', []))} skills (version {manifest.get('version')}): {changed} files written."
    if counts["backed_up"]:
        message += f" {counts['backed_up']} changed files were backed up to {backup_root}."
    return {"ok": True, "message": message, "version": manifest.get("version"), "counts": counts,
            "backup_dir": str(backup_root) if counts["backed_up"] else None}
