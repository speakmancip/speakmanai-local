# v1.5.0 plan: one exe, skills included, delegate mode by default

Drafted 2026-10-13 on `release/v1.5.0`. Working document, not a decision record. Inputs: the business-docs work brief (`speakmanai_business/findings/2026-10-13-lite-v1.5.0-work-brief.md`), the delegate-mode findings in the same folder, and checks made in this repo and in `speakmanai-cc` on 2026-10-13.

## Status and how to resume (2026-10-14)

Branch `release/v1.5.0`, pushed, head `8cc3feb`, no release PR, no tag, no signed build, nothing merged to `master`. `speakmanai-cc` is on `master` at `0cbaeaa` (PRs #6 and #7 merged). Newest unsigned build: `dist\v1.5.0-cc2\SpeakmanAI.exe` (14:51 on 10-14, adds the Claude Code install and PATH help on the setup page, tidied), skills from `speakmanai-cc` `0cbaeaa`; it is the build `tools/clean-machine-test.wsb` maps. `v1.5.0-cc` (14:29) is the same help before the tidy-up; the owner's earlier tested build is `dist\v1.5.0-wf` (11:48). The other `dist\v1.5.0*` and `dist\spike-sid` folders are older builds and can be deleted. A build needs `python tools/bundle_skills.py` first, then `python -m PyInstaller speakmanai.spec`. Any new module that `server.py` imports must also go in `speakmanai.spec` (`datas` and `excludes`), because the server is loaded by path.

**Built and tested** (details and evidence in `LITE_ENHANCEMENTS.md`, entries dated 2026-10-13 and 2026-10-14): validators auto-pass with no model API; `delegate_only` mode and a fresh install defaulting to it; Claude Code registration through `claude mcp add`; the 17 skills bundled in the exe and installed with backups; `list_skills` and `get_skill`; the reorganised setup page; the Claude app link dropping `session_id` (every session tool also takes `sid`); delegated pauses listing large dependency outputs by reference; HTML opt-in and rendered locally by `scripts/render_html.py` (`speakmanai-cc` PR #7); and the "Update workflows" button (`workflows_sync.py`). The owner tested the `v1.5.0-wf` exe and it passed.

**Decisions this session:** shipped workflow IDs are the owner's domain and an update replaces them with a backup (a user reapplies an edit or copies to a new ID); HTML only when the user asks, locally if the client has a shell, `render_document` only without one; fix the skills, not the server, for client behavior rules; the Technical Writer is out of the SAD workflow on purpose (cost); no release PR or tag until just before outside testing; Mongo-mode workflow refresh is a later script plus instructions.

**Open, in rough order:**
1. Clean-machine install test (test round item 1), ideally on the signed exe. First pass done 2026-10-14 in Windows Sandbox with the unsigned `v1.5.0-wf` exe (see the test round): Claude Code installed from the setup page's link, Check again found it with `claude` off PATH, Connect worked, Claude Code in the app worked. Not yet recorded from that run: SmartScreen, the Claude app panel walk, the mode-switch check. The setup page now shows the install command and PATH help (built 2026-10-14, not yet in an exe; rebuild and look at it in the Sandbox). `tools/clean-machine-test.wsb` (gitignored, local) maps the exe folder.
2. Copilot Studio full run (storage for `outputs/`, whether a large `render_document` argument works), Cursor and Codex once each.
3. Old-database warning for Mongo mode, and whether to apply the workflow update automatically after an upgrade (today: button only).
4. Parked: "Disconnect and remove", "Reset all data", `executed_by` on events, the plain-text second content block, `install.sh` upgrade prompts (17 y/N prompts, stops on closed input), pinning `speakmanai-cc` for the bundle, the synced `speakmanai-sdlc` plugin duplicating skills in Claude Code.
5. Release: PR for `release/v1.5.0`, merge, tag `v1.5.0`, signed exe (`az login`, `.codesign\sign-exe.ps1`), GitHub release, then the clean-machine test on the signed build.

**Elsewhere:** `speakman_generic/LITE_PARITY_HANDOFF.md` (untracked there) has Tasks 4 (`sid`) and 5 (pause references) for a Generic session. `D:\git\speakmanAU\ingest-build-record-corpus.js` has two uncommitted added lines for the `speakmanai/business` file list. The brain's `2026-10-13-lite-v1.5.0-progress.md` is current as of this status (brain updates need `--dry-run` and a go-ahead first). Reusable test scripts are in `D:\git\speakmanai\speakmanai_light\local-tests\` (outside the repo) with an index.
## Goal

A new Windows user downloads one exe and gets the engine and the skills together. They run workflows in delegate mode through the coding tool they already use, with no model API key. Someone with their own key switches on the setup page. A hosted trial is out of scope.

## Priorities

| Tier | Client | Position |
| --- | --- | --- |
| 1 | Windows Claude Code | Fully automatic. The release is built around it. |
| 2 | The Claude app (Desktop and Cowork, now one product) | Guided manual, plus a spike on serving skills over MCP. |
| 3 | Copilot Studio, Cursor, Codex | Best effort. Existing docs and zips stay. Cursor and Codex are labelled untested. |

Naming: Anthropic is merging Claude Desktop, Cowork and chat into a single "Claude" app, rolling out to Pro and Max over the coming weeks. Chats, projects, artifacts, connectors and skills carry over. This plan says "the Claude app" for all of it. Existing docs that say Desktop or Cowork need the same rename, and the install steps should be re-checked once the merged app is on the test machine, because the menus may have moved.
| After 1.5.0 | Linux and macOS | Docker build from the repo instructions today. Revisit once v1.5.0 ships. |

Copilot Studio needs a public URL (a tunnel), which makes it an enterprise fit. Support what already works and build nothing new for it. Whether attached storage lets its skills save `outputs/` files is unverified.

## What we know

- Already built on the branch: validators auto-pass with no model API, the setup-page mode now reaches the engine, tier defaults moved to Claude 5.5 and GPT-6, and a `delegate_only` mode pauses the model API. A full SAD run in the unsigned `dist\v1.5.0` exe confirmed it: 4 validators skipped, 0 retries, 0 provider errors.
- **The Claude Code one-click helper writes to a file Claude Code does not read.** It adds `mcpServers.speakmanai` to `~/.claude/settings.json` (`server.py:1490`). Running `claude mcp list` from a neutral directory with the engine up did not list `speakmanai`. Claude Code keeps MCP servers in `~/.claude.json`: user scope at the top level, local scope per project. The helper also omits `"type": "http"`, and if the file fails to parse it replaces the contents with just `mcpServers`.
- **None of the 17 skills produce a PDF.** `generate-sad` and `generate-compliance-report` render HTML through `render_document` and tell the user to print to PDF. Puppeteer, `scripts/doc_writer.py` and `scripts/preprocess.py` are used by nothing but a README how-to.
- Two skill sets exist in `speakmanai-cc`: `.claude/skills` (17, installed by `install.ps1`) and `desktop-skills/` (Desktop, Cowork, Copilot Studio). `desktop-skills/build.ps1` builds committed zips (`copilot-studio-skills.zip`, `generate-sad.zip`, `generate-compliance-report.zip`, `generate-speakmanai-code.zip`, `speakmanai-sdlc.zip`). The Copilot Studio doc is `desktop-skills/COPILOT_STUDIO.md`.
- The skills find their assets under `$HOME` and write `outputs/` relative to the model's own working directory. The exe does not need to detect a working directory.
- No shipped keys: `docker-compose.yml` passes keys through from the environment with empty defaults, and `.env.template` has them commented out.
- Validators exist in three workflows: 7 in `MCP_REQUIREMENTS_ENGINEERING_V1` (maxLoops 2, minScore 8), 4 in `MCP_SOLUTION_ARCHITECTURE_V1` (maxLoops 2, 2, 2 and 3), 1 in `MCP_UX_DESIGN_V1` (no config, engine defaults).

## Work items

### 1. Replace the one-click MCP helpers

- **Claude Code:** the exe runs `claude mcp add --transport http --scope user speakmanai http://localhost:8000/mcp` when `claude` is on the PATH, and shows the same command to copy when it is not. Stop writing to `settings.json`. Treat an existing registration as success.
- **Cursor and the Claude app's Desktop helper:** remove both one-click buttons. Their helpers share the overwrite-on-parse-failure code and have not been tested. Replace them with a copy block and a plain "untested" label for Cursor. The Claude app's local config may not accept a `url` entry at all; confirm before documenting any config snippet.
- **The Claude app (Desktop and Cowork):** the MCP server has to be created by hand and the skills bundle uploaded by hand. Research whether any command or deep link registers an MCP server, now that the product is merging. Until one is found, show the manual steps.
- **Codex:** copyable config only, labelled untested.
- Acceptance: on a machine with Claude Code and nothing else, one click or one pasted command makes `claude mcp list` show `speakmanai` from a directory the user has never opened.

### 2. Bundle and install the skills

- Single source: the exe build pulls a pinned copy of `speakmanai-cc` (a tag or commit), with its skills and synthesis prompts, and records that version.
- Claude Code install, unattended, user level only: copy the 17 skills to `~/.claude/skills`, and the synthesis `prompts` to `~/.claude/speakmanai-cc`. Triggered on first run or from the setup page button.
- Updates: if a skill file differs from the version the exe last installed, back it up before overwriting. Write the installed version to a marker file so the exe can tell.
- `install.ps1` and `install.sh` stay for people who want the skills without the engine. Remove puppeteer from both (item 5).
- For the manual clients, the setup page offers the bundles for download.
- **Source of the bundle (decided):** the exe build gets the skills from the same place the zips are built, `desktop-skills/build.ps1` in `speakmanai-cc`, run at build time. The built zips (`desktop-skills/*.zip`) are gitignored, so the repo holds only sources and each exe carries a freshly built, versioned set. No `speakmanai-cc` change is needed for this: the ignore rule is already there and no zip is tracked (an earlier draft of this plan said five were committed, which was wrong). The Claude Code skills come from the same checkout (`.claude/skills`), so one commit of `speakmanai-cc` feeds both.
- **Built (2026-10-13):** `tools/bundle_skills.py` stages the bundle and a manifest; `skills_installer.py` installs it; the setup page shows status, an install button and the download links. The checkout is located by `--cc-dir`, `SPEAKMANAI_CC_DIR` or a sibling folder. Not done: pinning the build to a specific `speakmanai-cc` tag or commit (today it takes whatever the checkout holds and records the commit), and installing on first run without a click.

### 3. Spike: serve skills over MCP

Skills are not an MCP concept, so no client installs one from a server. The content can still be delivered:

- **Built (2026-10-13):** `list_skills` and `get_skill(name)` serve all 17 skills from the `desktop-skills` variants, with bundled files inlined, and the server instructions point at them. Verified over real MCP from the exe. Tested headless in Claude Code with all skills disabled: Claude found and followed the skill unprompted and completed a keyless workflow. What remains is the test in the Claude app itself.
- Found while testing: the `speakmanai-sdlc` plugin uploaded in the Claude app syncs into Claude Code (`~/.claude/plugins/synced/`), so a user of both gets the skills twice, as `generate-sad` and `speakmanai-sdlc:generate-sad`, possibly at different versions. Decide whether the installer should detect that and skip, or whether it does not matter.
- Test with `generate-sad` and one self-contained skill, first in the Claude app, then Copilot Studio if cheap.
- Success means a user who has only added the MCP server can say "run the SAD workflow" and the model finds and follows the skill unprompted. If that is unreliable, the zip route stays as the supported path.
- Claude Code keeps real installed skills. They are discovered automatically and `get_skill` cannot match that.
- Some skills stay Claude Code only: `pentest` and `pentest-execute` use subagents, and `generate-ux-design` uses artifacts.

### 4. Settle open client questions

- Copilot Studio: does attached storage let a skill save `outputs/` files? Validate with the existing SAD run.
- The Claude app: confirm the manual steps in the merged build, and what happens to files a skill writes.
  - Found 2026-10-14: the app's computer link drops an argument named `session_id`. Fixed by also accepting `sid` (see LITE_ENHANCEMENTS). A full SAD run through the link then completed using `sid` and the MCP skills tools (2026-10-14); the remaining problem is result size, see LITE_ENHANCEMENTS.
- Cursor and Codex: test once each and record the result either way, or leave them labelled untested.

### 5. Cleanup

- **Done (2026-10-13, speakmanai-cc PR #6, merged):** puppeteer, `scripts/doc_writer.py`, `scripts/preprocess.py`, `templates/` and `package.json` are removed, and the two install scripts and the README no longer mention them or the Node requirement. Generated-app note kept: `generate-speakmanai-code-python` can scaffold an app with a Node PDF subprocess, which is separate from the installer. (PR #7 later added `scripts/render_html.py`, a different script.)

### 6. Optional, owner to pick

- `executed_by` attribute on output events, so a run shows it was done by the user's tool. Today the audit trail is inferred from `MCP_PAUSE` events, the log and the `validation_skipped` events.
- Carry-overs, none required: whether GPT-6 models accept `temperature`; saved configs that pin old models are not migrated; the startup `DEFAULT_MODEL` alias in `engine.py`; `seed_if_empty` never overwrites existing workflows; stuck delegate steps wait forever.

## To do (owner asked for these to be parked, 2026-10-13)

- **"Disconnect and remove" on the Claude Code panel.** Undo what Connect did: remove the user-scope MCP registration (`claude mcp remove speakmanai -s user`) and remove the skill files the exe installed. Use `installed.json` so only files we installed and the user has not changed are deleted; leave changed files, the user's own skills and extra files in place, and say what was left. Mention per-project registrations and the old `settings.json` entry, which this does not touch.
- **"Reset all data".** A separate, explicit action that clears `~/.speakmanai` (config and keys, the database with sessions and the execution mode, the log), so the next launch is a first run. It deletes the user's sessions and keys, so it needs a clear confirmation and should not be bundled with Disconnect.
- Until both exist, a manual clean-up is: quit the exe, delete or rename `~/.speakmanai`, run the `claude mcp remove` command, and delete the 17 skill folders in `~/.claude/skills` and `~/.claude/speakmanai-cc`.
- **Plain-text second content block for long results (parked 2026-10-14).** Return poll_workflow metadata as a small JSON block and the long context (and get_output content) as a second plain-text block, so the text is not JSON-escaped. Measured saving about 9% of tool-result tokens on a SAD run (see LITE_ENHANCEMENTS). Needs a test in every client, because clients treat extra content blocks differently; same caution as structured_output. A move away from JSON as the agent format was considered and not pursued.
- **Workflow refresh (built 2026-10-14, in v1.5.0).** Setup page "Update workflows" button backs up and replaces shipped workflows that differ from this version (workflows_sync.py). Open: whether to also apply on a first launch after an upgrade, and what to do about workflows a user edited in place (today they are replaced, with a backup). Owner expectation, 2026-10-14: shipped workflows are the owner's domain; a user who edits one reapplies the edit after an upgrade (the backup file holds the old version), or copies it to a new ID. Mongo-backed mode is not covered by the button (only SQLite was run): later, likely a script plus instructions.

## Test round (unsigned exe, local)

Tier 1 and 2 must pass before release. Tier 3 is recorded either way.

1. Clean Windows machine, no API key, ideally with the signed exe (see Release): install, check the skills are in place and the setup page shows no mode controls with the mode on `delegate_only`. Run a short workflow from Claude Code: no model call in the log and validators show `validation_skipped`. Add a model key: the controls appear, the mode moves to `auto` and validators run. Switch back to `delegate_only` and confirm the keyless behavior again. This is the one mode-switch check on the signed build.
   - **No Claude Code on the machine (not done on a real machine).** Simulated on 2026-10-14 with the built exe (dist\v1.5.0-wf), a fake home, and a PATH holding only the Windows folders: the assistant status reports Claude Code not found, mode delegate_only, no model API; the page shows "Claude Code was not found on this computer" with Get Claude Code and Check again, no Connect button, "17 skills: not installed yet", the Model API fold closed, and no mode controls. To test for real: a machine that has never had Claude Code, install the exe, walk the Claude app panel by hand (connector URL and the skills plugin download), then install Claude Code, press Check again, and Connect. Check what a user with only the Claude app sees, and whether Check again picks up a Claude Code installed in the meantime.
2. Registration: run the one-click or pasted command, then `claude mcp list` from an unrelated directory.
3. One short workflow end to end in delegate mode from Claude Code, then from the Claude app. The tool must run the planner and every agent.
4. Zero platform-funded model calls: check the engine log and the Vertex billing line.
5. Keyless run: validators are skipped with the `validation_skipped` marker. With a user key set, validators still run.
6. Switch to the user's own key and run the same workflow, then switch back with `delegate_only`.
7. Skill update: edit an installed skill, reinstall, confirm the backup and the version marker.
8. Tier 3: Copilot Studio via the tunnel (re-test, as the earlier run is not confirmed as delegate mode), Codex once, Cursor once.
9. Record where testers stall during install.
- **Windows Sandbox run, 2026-10-14 (owner, unsigned `v1.5.0-wf` exe):** on a clean Windows with no Claude Code, the owner installed Claude Code with Anthropic's `irm https://claude.ai/install.ps1 | iex`. The installer put `claude.exe` in `%USERPROFILE%\.local\bin` and did not add it to PATH (its output says so in a "Setup notes" line). The setup page's Check again still found it (the server also looks in `~/.local/bin`), Connect Claude Code worked (it runs the CLI by full path), and Claude Code in the app worked. Finding: a new user cannot type `claude` in a terminal until PATH is fixed, and the first run needs it to sign in, so the CLI is a barrier, not a one-click step. The desktop app bundles its own Claude Code but puts no `claude` command on PATH. Decision: the exe does not run the installer itself (it would be a localhost endpoint that downloads and executes a script; the install and sign-in are Anthropic's). Instead the page shows the install command with a Copy button, tells the user to run `claude` once to sign in, and when `claude` is not on PATH shows the full-path run command and the one-line PATH command (`cli_on_path` and `windows` fields in `/api/assistant-status`; Windows only). Checked on a fake home: nothing installed, installed off PATH, installed on PATH, settings folder only; the page script passes `node --check`. Still to do: rebuild, look at the page in the Sandbox, and mention the PATH gotcha in the install docs and release notes.
  - **Where the owner stalled (item 9), same run:** (1) downloaded the Claude app and signed in; (2) pressed Check on the SPEAKMANAI page and it failed, because the app alone has no CLI; (3) found the `irm` command on the Claude Code page, further down, and ran it; (4) Check succeeded, Connect succeeded; (5) reopened Claude: the Claude Code part of the app found the speakmanai server and listed its tools, but the chat side of the app did not, because the two keep separate configuration (the chat side needs the connector added by hand). Missing `claude` on PATH annoyed the owner as a terminal user; the owner's view is that new users will mostly use the app, and a user who does use the terminal has to sort out PATH anyway. Existing CLI users: no issues. Anthropic's official steps (`https://code.claude.com/docs/en/overview`, read 2026-10-14) match the command we show; the page's link now goes there ("Official install steps") instead of the marketing page, and the PATH block links Anthropic's PATH help. That page also says the desktop app includes Claude Code (no CLI needed for the app itself), that every surface shares the same engine and MCP servers, and that Git for Windows is recommended: without it Claude Code uses PowerShell as its shell tool. Open check: the skills (the `render_html.py` step, file writes) have only been run where Git is installed; run one on the Sandbox, which has no Git. Changes made for this: the install command is on the setup page, and the "Next" box after Connect now says Connect covers Claude Code (including its part of the Claude app) and that the app's chat side is set up from the Claude app tab.
- **Owner test, 2026-10-14:** the unsigned dist\v1.5.0-wf\SpeakmanAI.exe (sid alias, pause references, local HTML script, Update workflows button) was tested by the owner and passed. Details of what was exercised were not recorded.

## Release

1. Build and test locally on `release/v1.5.0`. No release for testing.
2. Just before live testing with outside users: PR, merge, tag `v1.5.0`, signed exe (the signing script needs an interactive `az login`), GitHub release.
3. Run the clean-machine install test (test round item 1) with the signed build.

## Risks

- The `claude` CLI may not be on the PATH for every user. The copy-the-command fallback covers that, but it is one more manual step on the main path.
- Skills over MCP may not be picked up reliably. The spike exists to find out, and the zips are the fallback.
- A skills update that overwrites user edits would cost trust. The backup and version marker are not optional.
- Unsigned test builds trigger SmartScreen warnings, so testers need to know that.
- Pinning `speakmanai-cc` couples two repos. Each exe version needs to name the skills version it carries.
