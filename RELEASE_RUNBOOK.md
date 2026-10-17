# Release runbook

How to release speakmanai_lite (the Windows exe and the repo). Written for v1.5.0 on 2026-10-17. Do the steps in order. Nothing here happens without the owner saying go: a release is outward-facing and cannot be taken back cleanly.

Test status lives in `speakmanai_business/rebirth/06-outstanding-tests.md`. Record every result there.

## 0. Before you start

- The release branch (`release/vX.Y.Z`) is pushed and its head is what you mean to ship.
- `APP_VERSION` in `orchestration/mcp-server/server.py` is the version being released.
- `RELEASE_NOTES.md` has a section for the version with its **Release:** link (the link works once the release exists in step 6), the README "Latest release" line names it, and SETUP.md matches the behaviour.
- The prompt-text check in `CLAUDE.md` prints nothing.
- `speakmanai-cc` changes the exe depends on are merged to its `master` through PRs (never push `master` there).

## 1. Build the candidate

Use a new folder name each time. Never rebuild into a folder the Windows Sandbox maps.

```powershell
cd D:\git\speakmanai\speakmanai_light\speakmanai_lite
git -C D:\git\speakmanai-cc switch master; git -C D:\git\speakmanai-cc pull
venv-desktop\Scripts\python.exe tools\bundle_skills.py
venv-desktop\Scripts\python.exe -m PyInstaller speakmanai.spec --noconfirm --distpath dist\vX.Y.Z-rcN --workpath build\pyi-rcN
```

`bundle_skills.py` runs `tools/sync_render_html.py` first and stops if any copy of `render_html.py` in speakmanai-cc differs from `document_renderer.py`. Fix it with `--write`, then a speakmanai-cc branch and PR, then pull master again. Check the line `Skills bundle <version>+<commit>` names the speakmanai-cc commit you expect.

## 2. Check the candidate

1. Regression tests in `D:\git\speakmanai\speakmanai_light\local-tests\` (outside the repo): `test_renderer_fixes`, `test_mode_guard`, `test_remote_gate`, `test_executed_by`, `test_delegate_only`, `test_autopass`, `test_cross_site`, `test_env_ignore`, `test_skills_install`, `test_scripts_install`, `test_mcpb_bridge`, `test_wf_sync2` (A, B, C). `test_autopass.py` prints its results and then hangs on exit (known); read the output and stop it.
2. Start the exe with a fake home (`USERPROFILE` and `HOME` set to an empty folder, model key variables cleared, `SPEAKMANAI_PORT` set to a spare port) and run the 18-check smoke test: page, tab icon, footer version, favicon, 19 tools with no `outputSchema`, `sid`, skills served over MCP, `get_skill` carrying the current `render_html.py` and the speakmanai-cc commit, `render_document`, remote gate refusals, delegate-only default, no log errors. All 18 must pass.
3. Owner looks at the taskbar icon, tray icon and browser tab icon.
4. Record the exe's size and SHA-256, the lite head commit and the speakmanai-cc commit in the test file.

Docker, if the Dockerfile or requirements changed: build the image, start it, and check `/health` and a forwarded request being refused.

## 3. The release PR

1. Release prep commit on the release branch: version in `RELEASE_NOTES.md`, README, SETUP.md, CLAUDE.md. Push.
2. Open a PR from `release/vX.Y.Z` into `main` (`gh pr create --base main`). Summarise from the release notes. Owner merges.
3. Pull `main`. If the exe was built before the merge from the same commits, no rebuild is needed. If anything in the build inputs changed, rebuild and repeat step 2.

## 4. Tag

```powershell
git switch main; git pull
git tag vX.Y.Z
git push origin vX.Y.Z
```

Tag only the commit the candidate was built from.

## 5. Sign

The owner runs `az login` interactively (suggest the `! az login` prefix in the session). Then:

```powershell
.\.codesign\sign-exe.ps1 -Path dist\vX.Y.Z-rcN\SpeakmanAI.exe
Get-AuthenticodeSignature dist\vX.Y.Z-rcN\SpeakmanAI.exe | Format-List Status, SignerCertificate
Get-FileHash dist\vX.Y.Z-rcN\SpeakmanAI.exe -Algorithm SHA256
```

Status must be `Valid`. Signing changes the file, so record the signed size and hash. Run the smoke test again on the signed exe, since it is the file that ships.

## 6. GitHub release

```powershell
gh release create vX.Y.Z dist\vX.Y.Z-rcN\SpeakmanAI.exe --repo speakmancip/speakmanai-local --title "vX.Y.Z" --notes-file <notes>
```

Use the version's section of `RELEASE_NOTES.md` for the notes. The GitHub repo is `speakmancip/speakmanai-local` (this checkout's `origin`). Open the release page and check the link in `RELEASE_NOTES.md` and README works and the asset downloads.

## 7. After the release

1. Clean-machine test on the signed exe: SmartScreen prompt (row 14), a skill run with no Git (row 17), the newest build in the Sandbox (row 19), then one run each in Cursor and Codex (row 20). Needs the Windows Sandbox or a cloud machine. Record results in the test file.
2. Update the memory files, `.speakmanai-summary.md`, and the brain note (`speakmanau-update-brain` skill: dry run first, ingest only after the owner agrees).
3. Optional marketing: a styled `marketing/release-vX.Y.Z.html` in the gitignored folder, staged and not published unless asked.
4. Tidy: delete old `dist` folders, close the release rows in the test file.

## Rules that bite

- `speakmanai-cc`: branch and PR only, never push `master`.
- Prompt text: no em or en dashes (`CLAUDE.md`).
- A new module imported by `server.py` must be added to `speakmanai.spec`.
- Do not push, tag, sign or release on your own initiative. Ask.
- Do not use PowerShell `Get-Content`/`Set-Content` on existing files with non-ASCII text.
