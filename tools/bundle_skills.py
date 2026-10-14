"""
Stage the SPEAKMAN.AI skills for bundling into the exe.

Run before PyInstaller:

    python tools/bundle_skills.py [--cc-dir PATH] [--out build/skills_bundle]

Reads a checkout of the speakmanai-cc repo (default: $SPEAKMANAI_CC_DIR, else a
`speakmanai-cc` folder beside this repo's grandparent) and writes:

    <out>/claude-code/skills/<skill>/...   the Claude Code skills (.claude/skills)
    <out>/claude-code/assets/prompts/...    the synthesis prompts
    <out>/mcp-skills/<skill>/...           the desktop variants, served by get_skill
    <out>/downloads/*.zip                  bundles for manual clients, built by
                                           desktop-skills/build.ps1 in that checkout
    <out>/manifest.json                    version, commit and a sha256 per file

speakmanai.spec picks <out> up when manifest.json exists.
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
# The synthesis prompts are the only supporting files the skills read. speakmanai-cc dropped
# its PDF scripts and templates (PR #6).
ASSET_DIRS = ("prompts",)
DOWNLOADS = (
    "speakmanai-sdlc.zip",
    "generate-sad.zip",
    "generate-compliance-report.zip",
    "generate-speakmanai-code.zip",
    "copilot-studio-skills.zip",
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git(cc: Path, *args: str) -> str:
    try:
        return subprocess.run(["git", "-C", str(cc), *args], capture_output=True, text=True, check=True).stdout.strip()
    except Exception:
        return ""


def _default_cc_dir() -> Path:
    env = os.environ.get("SPEAKMANAI_CC_DIR")
    return Path(env) if env else REPO_ROOT.parent.parent.parent / "speakmanai-cc"


def _copy_tree(src: Path, dst: Path) -> None:
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cc-dir", type=Path, default=_default_cc_dir())
    ap.add_argument("--out", type=Path, default=REPO_ROOT / "build" / "skills_bundle")
    ap.add_argument("--skip-zips", action="store_true", help="do not run desktop-skills/build.ps1 (no download bundles)")
    args = ap.parse_args()

    cc: Path = args.cc_dir.resolve()
    skills_src = cc / ".claude" / "skills"
    if not skills_src.is_dir():
        print(f"ERROR: no .claude/skills under {cc}. Pass --cc-dir or set SPEAKMANAI_CC_DIR.", file=sys.stderr)
        return 1

    out: Path = args.out.resolve()
    if out.exists():
        shutil.rmtree(out)
    cc_out = out / "claude-code"
    _copy_tree(skills_src, cc_out / "skills")
    for name in ASSET_DIRS:
        if (cc / name).is_dir():
            _copy_tree(cc / name, cc_out / "assets" / name)

    # The desktop variants are written for clients with no local scripts, so they are the
    # ones the MCP server hands out through list_skills / get_skill.
    mcp_src = cc / "desktop-skills" / "org-plugins" / "speakmanai-sdlc" / "skills"
    mcp_out = out / "mcp-skills"
    if mcp_src.is_dir():
        _copy_tree(mcp_src, mcp_out)
    else:
        print(f"WARNING: {mcp_src} not found; get_skill will have nothing to serve.")

    downloads_out = out / "downloads"
    downloads_out.mkdir(parents=True)
    if not args.skip_zips:
        build_ps1 = cc / "desktop-skills" / "build.ps1"
        if not build_ps1.is_file():
            print(f"ERROR: {build_ps1} not found.", file=sys.stderr)
            return 1
        shell = shutil.which("powershell") or shutil.which("pwsh")
        if not shell:
            print("ERROR: PowerShell is needed to run desktop-skills/build.ps1. Use --skip-zips to bundle without downloads.", file=sys.stderr)
            return 1
        subprocess.run([shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(build_ps1)], check=True)
        for name in DOWNLOADS:
            src = cc / "desktop-skills" / name
            if not src.is_file():
                print(f"ERROR: build.ps1 did not produce {name}.", file=sys.stderr)
                return 1
            shutil.copy2(src, downloads_out / name)

    plugin_version = ""
    try:
        plugin_json = cc / "desktop-skills" / "org-plugins" / "speakmanai-sdlc" / ".claude-plugin" / "plugin.json"
        plugin_version = json.loads(plugin_json.read_text(encoding="utf-8")).get("version", "")
    except Exception:
        pass
    commit = _git(cc, "rev-parse", "--short", "HEAD")
    dirty = bool(_git(cc, "status", "--porcelain"))
    version = "+".join(p for p in (plugin_version or "0.0.0", commit) if p) + (".dirty" if dirty else "")

    manifest = {
        "version": version,
        "commit": commit,
        "dirty": dirty,
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "skills": sorted(p.name for p in (cc_out / "skills").iterdir() if p.is_dir()),
        "claude_code": {p.relative_to(cc_out).as_posix(): _sha256(p) for p in sorted(cc_out.rglob("*")) if p.is_file()},
        "downloads": {p.name: _sha256(p) for p in sorted(downloads_out.iterdir()) if p.is_file()},
        "mcp_skills": {
            d.name: sorted(p.relative_to(d).as_posix() for p in d.rglob("*") if p.is_file())
            for d in sorted(mcp_out.iterdir()) if d.is_dir() and (d / "SKILL.md").is_file()
        } if mcp_out.is_dir() else {},
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    print(f"Skills bundle {version}: {len(manifest['skills'])} skills, {len(manifest['claude_code'])} files, "
          f"{len(manifest['downloads'])} downloads, {len(manifest['mcp_skills'])} MCP-served skills -> {out}")
    if dirty:
        print("WARNING: the speakmanai-cc checkout has uncommitted changes; the version is marked .dirty.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
