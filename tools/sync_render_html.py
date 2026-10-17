"""
Keep speakmanai-cc's scripts/render_html.py in step with the server's document_renderer.py.

render_html.py is a copy of the renderer with a header and a command line added. The skills tell
an assistant its output matches the render_document tool, so the two must not drift apart.
speakmanai-cc keeps the script in scripts/ and a further copy inside each desktop and plugin
skill (desktop-skills/..., served by get_skill and zipped for upload); all of them must be the same file.

    python tools/sync_render_html.py            check: exit 1 if the copy has drifted
    python tools/sync_render_html.py --write    rewrite the copy from document_renderer.py

Only the module body is generated (from `import re` to the end of document_renderer.py). The
header and the command line already in render_html.py are kept as they are. After --write, commit
the change in speakmanai-cc on a branch and open a PR.

tools/bundle_skills.py runs the check, so a build cannot ship a stale copy.
"""
import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RENDERER = REPO_ROOT / "orchestration" / "mcp-server" / "document_renderer.py"
HEADER_END = "from __future__ import annotations\n"
CLI_MARK = "# COMMAND LINE"
SEPARATOR = "# " + "-" * 93 + "\n"


def default_cc_dir() -> Path:
    env = os.environ.get("SPEAKMANAI_CC_DIR")
    return Path(env) if env else REPO_ROOT.parent.parent.parent / "speakmanai-cc"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8").replace("\r\n", "\n")


def _parts(target: str):
    """The header and the command line that stay as they are in render_html.py."""
    h = target.index(HEADER_END) + len(HEADER_END)
    mark = target.index(CLI_MARK)
    cli = target.rfind(SEPARATOR, 0, mark)
    if cli < 0:
        raise ValueError("render_html.py has no command line section marker")
    return target[:h], target[cli:]


def build(target_text: str) -> str:
    renderer = _read(RENDERER)
    body = renderer[renderer.index("\nimport re\n") + 1:].rstrip("\n")
    header, cli = _parts(target_text)
    return header + "\n\n" + body + "\n\n\n" + cli


def copies(cc: Path, canonical: Path):
    """The copies of render_html.py the desktop and plugin skills carry (served by get_skill and zipped)."""
    skip = {"node_modules", ".git"}
    return sorted(p for p in cc.rglob("render_html.py") if p != canonical and not (skip & set(p.relative_to(cc).parts)))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cc-dir", type=Path, default=default_cc_dir())
    ap.add_argument("--write", action="store_true", help="rewrite render_html.py (and its copies) from document_renderer.py")
    args = ap.parse_args(argv)

    target_path = args.cc_dir / "scripts" / "render_html.py"
    if not target_path.is_file():
        print(f"ERROR: {target_path} not found. Pass --cc-dir or set SPEAKMANAI_CC_DIR.", file=sys.stderr)
        return 2
    current = _read(target_path)
    expected = build(current)
    # Every copy must be the same file as the canonical one.
    stale = [p for p in [target_path] + copies(args.cc_dir, target_path)
             if _read(p) != expected]
    if not stale:
        print(f"render_html.py and its {len(copies(args.cc_dir, target_path))} copies match document_renderer.py")
        return 0
    if args.write:
        for p in stale:
            p.write_text(expected, encoding="utf-8", newline="\n")
            print(f"Rewrote {p}")
        print("Commit these in speakmanai-cc on a branch and open a PR.")
        return 0
    print("DRIFT: these no longer match " + RENDERER.name + ":\n  " + "\n  ".join(str(p) for p in stale) +
          "\nRun: python tools/sync_render_html.py --write   then commit it in speakmanai-cc (branch and PR).",
          file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
