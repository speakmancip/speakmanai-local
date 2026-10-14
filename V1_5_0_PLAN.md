# v1.5.0 plan: one exe, skills included, delegate mode by default

Drafted 2026-10-13 on `release/v1.5.0`. Working document, not a decision record. Inputs: the business-docs work brief (`speakmanai_business/findings/2026-10-13-lite-v1.5.0-work-brief.md`), the delegate-mode findings in the same folder, and checks made in this repo and in `speakmanai-cc` on 2026-10-13.

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

- **Built (2026-10-13):** `list_skills` and `get_skill(name)` serve all 17 skills from the `desktop-skills` variants, with bundled files inlined, and the server instructions point at them. Verified over real MCP from the exe. What remains is the part only a person can do: the test in the Claude app below.
- Add `list_skills` and `get_skill(name)` tools, with prompt files inlined so the `ASSET_DIR` lookup disappears. Point the server instructions at them.
- Test with `generate-sad` and one self-contained skill, first in the Claude app, then Copilot Studio if cheap.
- Success means a user who has only added the MCP server can say "run the SAD workflow" and the model finds and follows the skill unprompted. If that is unreliable, the zip route stays as the supported path.
- Claude Code keeps real installed skills. They are discovered automatically and `get_skill` cannot match that.
- Some skills stay Claude Code only: `pentest` and `pentest-execute` use subagents, and `generate-ux-design` uses artifacts.

### 4. Settle open client questions

- Copilot Studio: does attached storage let a skill save `outputs/` files? Validate with the existing SAD run.
- The Claude app: confirm the manual steps in the merged build, and what happens to files a skill writes.
- Cursor and Codex: test once each and record the result either way, or leave them labelled untested.

### 5. Cleanup

- **Done (2026-10-13, speakmanai-cc PR #6, merged):** puppeteer, `scripts/doc_writer.py`, `scripts/preprocess.py`, `templates/` and `package.json` are removed, and the two install scripts and the README no longer mention them.
- Remove puppeteer, `scripts/doc_writer.py` and `scripts/preprocess.py` from `speakmanai-cc`, the two install scripts and the README. Keep the generated-app note: `generate-speakmanai-code-python` can scaffold an app with a Node PDF subprocess, which is separate from the installer.
- Remove the stale Node requirement from the docs.

### 6. Optional, owner to pick

- `executed_by` attribute on output events, so a run shows it was done by the user's tool. Today the audit trail is inferred from `MCP_PAUSE` events, the log and the `validation_skipped` events.
- Carry-overs, none required: whether GPT-6 models accept `temperature`; saved configs that pin old models are not migrated; the startup `DEFAULT_MODEL` alias in `engine.py`; `seed_if_empty` never overwrites existing workflows; stuck delegate steps wait forever.

## To do (owner asked for these to be parked, 2026-10-13)

- **"Disconnect and remove" on the Claude Code panel.** Undo what Connect did: remove the user-scope MCP registration (`claude mcp remove speakmanai -s user`) and remove the skill files the exe installed. Use `installed.json` so only files we installed and the user has not changed are deleted; leave changed files, the user's own skills and extra files in place, and say what was left. Mention per-project registrations and the old `settings.json` entry, which this does not touch.
- **"Reset all data".** A separate, explicit action that clears `~/.speakmanai` (config and keys, the database with sessions and the execution mode, the log), so the next launch is a first run. It deletes the user's sessions and keys, so it needs a clear confirmation and should not be bundled with Disconnect.
- Until both exist, a manual clean-up is: quit the exe, delete or rename `~/.speakmanai`, run the `claude mcp remove` command, and delete the 17 skill folders in `~/.claude/skills` and `~/.claude/speakmanai-cc`.

## Test round (unsigned exe, local)

Tier 1 and 2 must pass before release. Tier 3 is recorded either way.

1. Clean Windows machine, no API key: install, check the skills are in place and the setup page shows the mode switch.
2. Registration: run the one-click or pasted command, then `claude mcp list` from an unrelated directory.
3. One short workflow end to end in delegate mode from Claude Code, then from the Claude app. The tool must run the planner and every agent.
4. Zero platform-funded model calls: check the engine log and the Vertex billing line.
5. Keyless run: validators are skipped with the `validation_skipped` marker. With a user key set, validators still run.
6. Switch to the user's own key and run the same workflow, then switch back with `delegate_only`.
7. Skill update: edit an installed skill, reinstall, confirm the backup and the version marker.
8. Tier 3: Copilot Studio via the tunnel (re-test, as the earlier run is not confirmed as delegate mode), Codex once, Cursor once.
9. Record where testers stall during install.

## Release

1. Build and test locally on `release/v1.5.0`. No release for testing.
2. Just before live testing with outside users: PR, merge, tag `v1.5.0`, signed exe (the signing script needs an interactive `az login`), GitHub release.
3. Re-run the mode-switch check once in the signed build.

## Risks

- The `claude` CLI may not be on the PATH for every user. The copy-the-command fallback covers that, but it is one more manual step on the main path.
- Skills over MCP may not be picked up reliably. The spike exists to find out, and the zips are the fallback.
- A skills update that overwrites user edits would cost trust. The backup and version marker are not optional.
- Unsigned test builds trigger SmartScreen warnings, so testers need to know that.
- Pinning `speakmanai-cc` couples two repos. Each exe version needs to name the skills version it carries.
