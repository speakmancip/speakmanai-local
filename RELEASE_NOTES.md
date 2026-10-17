# Release Notes

## v1.5.0

**Release:** https://github.com/speakmancip/speakmanai-local/releases/tag/v1.5.0

### The short version

v1.5.0 is built around one download. The exe now carries the skills, and a fresh install runs in Delegate Only mode, so a new Windows user with Claude Code installed can connect, install the skills and run a workflow without an API key. Someone with their own model API key can still switch to it on the setup page.

### Headline changes

**1. No API key needed: `delegate_only` mode, and a fresh install defaults to it**
A fourth execution mode, `delegate_only`, sends the planner and every AI step to the connected assistant and pauses the model API without touching saved keys. Switching back to `auto` resumes API use. A new install with no model API set up starts in this mode. AI validators now pass automatically when no model API is available (or the mode is `delegate_only`), and the run record notes that the step was not validated. Before, a keyless run sent every validator to a model that did not exist, scored 0, and bounced the assistant through up to three redos on error text.

**2. Skills ship inside the exe**
The 17 Claude Code skills are bundled. On the setup page, **Add to Claude Code** registers the server and installs the skills in one click, without prompts. A file you edited is backed up under `~/.claude/speakmanai-cc/backups/` before it is replaced, and your own skills are never touched. The page shows whether the skills are up to date and offers the upload bundles for the Claude app and Copilot Studio.

**3. Skills served over MCP (experimental)**
Two new tools, `list_skills` and `get_skill`, let an assistant fetch a skill's text from the server when no skill is installed. A headless Claude Code run with skills disabled found and followed a skill this way unprompted, and a Copilot Studio run completed a full Solution Architecture document with no uploaded skills. The Claude app also completed a run this way. Behaviour in other clients is untested.

**4. Setup page reorganised**
The page used to open on "Choose your AI provider" and a key field. It now opens on who runs the AI steps and a "Connect your AI assistant" picker (Claude Code, Claude app, Copilot Studio, Cursor, Codex) that shows only the steps for the one you pick. The model API section is collapsed unless one is set up. The Claude Code panel shows whether the CLI was found, the install command when it was not, and a fold of help for a `claude` that is installed but not on PATH (Anthropic's Windows installer does not add it). Cursor and Codex are labelled untested.

**5. Update workflows button**
An existing database keeps the workflow prompts it was created with, because the exe only seeds a workflow that is missing. The setup page now shows an amber box with an **Update workflows** button when your stored workflows differ from the shipped ones. It replaces the shipped workflows, adds missing agents, unlinks retired ones, and writes what it replaced to `~/.speakmanai/backups/` first. It is never automatic. Your own workflows and agents are not touched.

**6. Remote access is off by default**
A public Dev Tunnel, which Copilot Studio needs, used to expose the whole app including the setup page and its API with no authentication. Now any request that is not local (Host other than `localhost`, `127.0.0.1` or `::1`, or carrying a forwarding header) gets a 403 until you turn on **Remote access** on the setup page. When it is on, only `/mcp` is reachable and it needs a token, sent as `x-api-key` or `Authorization: Bearer`. `/setup` and `/api/*` stay local only, so a tunnel can never read the token or switch the setting off. The token is made the first time you turn it on, shown with a Copy button, and can be replaced. Local use is unchanged and needs no token.
*Limits:* the gate separates tunnels and proxies from local software. The Host header is not proof of origin, so software on the same machine can claim to be local. A static key on a public URL narrows exposure but is not a secured cloud endpoint. Do not leave a tunnel running.
*Tested:* with a real Dev Tunnel (it keeps a non-local Host and adds forwarding headers, and the gate catches both), with Copilot Studio sending the token as an API key in `x-api-key`, with the Claude app's extension bridge (not refused), and with the Docker image bound to `0.0.0.0`. In Copilot Studio each edit of the MCP test creates a new connection, and a stale one returns 401, so pick the connection that holds the current key (see `COPILOT_STUDIO.md` in speakmanai-cc).

**7. A record of who did each step**
Each output now carries `executed_by`, `validated_by` and, for an approved human review gate, `gate_answered_by`, and `get_outputs` shows them with a short note on how to word them.
- A step the engine ran on its own model records provider, model and mode as fact.
- A delegated step records the name the assistant reported (a new optional `executed_by` argument on `submit_response`), the client's user agent, whether the connection was local or remote, and the execution mode. The name is stored as reported and is not checked.
- A validator that ran records its score. A skipped one records why.
- A human review gate is recorded as answered through the connected client. The engine cannot tell a person's answer from the assistant answering for them, so the record never says "human approved".
The record states facts and makes no claim of independent review. A run done by one assistant from start to finish reads that way.
The skills in the bundle tell assistants to pass `executed_by`.
*Not in this release:* a rule that refuses a review from the same executor as the step it reviews.

**8. Send feedback from the setup page**
A closed **Send feedback** section posts a bug report, feature request, question or "Keep me updated" note to the SPEAKMAN.AI feedback form. A line under the form states exactly what is sent: the type, the message, the email if you give one, and the app version. Nothing is sent without a click, and a note without an email is accepted and kept without a contact. "Keep me updated" needs an email and shows a consent line. This is the exe's first outbound request other than to a model API.

**9. A session cannot change who runs its steps halfway**
A session's steps are fixed as handed to your assistant or run by the engine when it is planned, but the execution mode is read live. Switching the mode mid-session used to send a delegated item twice or run a validator as if still delegating. The first event now records the mode the session started in, and `submit_response` and `update_session` refuse once the mode has moved to the other side. The message names the start mode and how to switch back, and nothing is changed. Switch back and the same call works. Sessions from before this version record no mode and are not guarded.

### Changes you may notice

- **The SPEAKMAN.AI agent circle** is now the tray icon, the exe's icon in the taskbar and Explorer, and the setup page's browser tab icon (served at `/favicon.png`). If the image is missing, the tray falls back to the old green square.
- **Each tool reply arrives once.** Every tool was publishing an output schema, so the MCP library sent each reply twice, as text and as structured content. That doubled what clients such as Copilot Studio counted. All 19 tools now return text only.
- **`render_document` and `scripts/render_html.py` produce the same, fixed HTML.** Fixed: indented sub-bullets that start with italics, numbered or bulleted items nested under a bullet, bold markers inside code, `>` quotes, LaTeX such as `$\ge 99.0\%$`, and unescaped `&` in the table of contents. The build now checks that every copy of `render_html.py` in the skills matches the server's renderer and refuses to bundle if one has drifted.
- **The `mcp` library is pinned to `>=1.27,<2`.** An unpinned Docker or run-from-source install resolved to mcp 2.x, which removed `mcp.server.fastmcp`, so the server exited at start. The exe was never affected (it bundles 1.27.0). Moving to 2.x is planned separately.
- **Default models** are now Claude Sonnet 5.5 (fast and standard) and Opus 5.5 (advanced), and OpenAI gpt-6-luna, gpt-6.1-sol and gpt-6-astra (fast, standard, advanced). Gemini and Vertex AI defaults are unchanged. Saved configs that pin an older model keep using it until you save the setup page again. Older models stay selectable. Claude Haiku is not offered: as the fast tier it scored a validator run 0.0, so only Sonnet 5.5 and Opus 5.5, which have been run end to end, are the defaults. If you saved the setup page with Haiku on an early v1.5.0 build, choose another fast model and save again.
- **HTML is opt-in.** The SAD and compliance report skills produce HTML only when you ask. A client that can run Python renders it locally with `scripts/render_html.py`, which the exe installs with the skills. `render_document` stays for clients with no shell, such as Copilot Studio.
- **Smaller pauses.** In delegate mode, a pause used to repeat the full text of every output it depended on. When those total more than 20,000 characters, large outputs are now listed by agent and size, with an instruction to use your own copy or fetch it with `get_output`. One pause in a Claude app run had reached 73,540 characters, over that app's result limit. A delegated planner also no longer passes its agent list to every later agent.
- **Find an old session by what it was about.** `list_sessions`, `poll_workflow` and `get_outputs` now return a `request_preview`, the first 200 characters of the request the session was started with, and `poll_workflow` returns the title as well. A session can sit for weeks between steps, and nobody remembers a session id. An assistant can match "the SAD about the bowling alley ecommerce tool" against the title and preview, then read the Business Context Clarifier output for the detail. Nothing times out: a session waits as long as it needs to.
- **The exe ignores model keys it finds in your environment.** Before, a key such as `ANTHROPIC_API_KEY` set for another tool counted as a configured model API, so a first run started in `auto` and every step and validator was billed to that account. The packaged exe now uses only what you save on the setup page. Docker and run-from-source still read the environment. Start the exe with `SPEAKMANAI_USE_ENV=1` to keep the old behaviour.
- **Other websites cannot change your settings.** A page open in your browser could post to the local setup API (`http://localhost:8000/api/...`) without any prompt, because the server read request bodies as JSON whatever their type. It could not read your token or keys, but it could switch the execution mode, update workflows or point the Ollama URL elsewhere. The engine now refuses state-changing requests to `/api/` that carry a browser `Origin` from another site (or `Sec-Fetch-Site: cross-site`). The setup page, curl, scripts and MCP clients are unaffected.
- **The setup page shows the app version** (v1.5.0) in its footer, and feedback sends it, so a bug report says which build it came from.
- **The setup page tabs are "Claude Code" and "Claude app".** Claude Code is for a terminal. The Claude app tab covers the app's chat, Cowork and Code tab through the extension.
- **`sid` as well as `session_id`.** The eight tools that take a session id accept `sid` too. The Claude app's link to a local engine drops an argument named `session_id`, which broke every session tool there.
- **Claude Code registration** now runs `claude mcp add --transport http --scope user speakmanai ...`. The old button wrote to `~/.claude/settings.json`, a file Claude Code does not read for MCP servers. The Claude Desktop and Cursor one-click buttons are gone; they shared that code and were never tested. The page shows the URL and command to copy.

### Fixed

- The setup page's Execution Mode dropdown never reached the engine. It wrote to `config.json`, while the engine reads a database row. Present since the first release.
- The output cap carried over from older Haiku models (8192) truncated every fast-tier Anthropic response. Sonnet 5.5 and Opus 5.5 now have 128,000.
- `generate_document` in `delegate_only` mode raises a clear error rather than calling a model that is paused.

### If you're upgrading

- **Workflows:** open the setup page and press **Update workflows** if the amber box appears. Before that, a database made by an older version keeps old prompts and review gates, and the planner may still offer the retired Technical Writer.
- **Skills:** press the install or update button on the setup page to receive the new skill set.
- **Tunnels and Copilot Studio:** a tunnel will now get a 403 until you turn on Remote access, and then Copilot Studio needs the token sent as an API key header. Set that up before relying on an existing tunnel.
- **Keys in your environment:** if you relied on the exe reading a model key from an environment variable, save it on the setup page instead, or start the exe with `SPEAKMANAI_USE_ENV=1`.
- **Models:** an install with a pinned Claude or OpenAI model keeps it. Choose the new defaults on the setup page if you want them.
- **Docker and run-from-source** users configure the model through `.env` and are not migrated automatically. Rebuild the image (`docker-compose up --build`) to pick up the `mcp` pin.
- **Sessions in progress:** finish a session before changing the execution mode, or switch back to the mode it started in.

### Not verified at the time of writing

- A first install on a clean machine with the signed build, including the SmartScreen prompt. The unsigned build passed in Windows Sandbox.
- Mongo-backed mode for the new run record fields and the workflow update (the exe uses SQLite only).
- The feedback section when offline or when HubSpot rejects the post.
- Cursor and Codex, which are labelled untested on the setup page.
- Copilot Studio cost for the full SAD run. The estimate is about 788 credits (about $15.76 at $0.02 a credit); the bill may not show for up to three weeks.

---

## v1.4.1

**Release:** https://github.com/speakmancip/speakmanai-local/releases/tag/v1.4.1

### Fixed: garbled characters in two bundled workflows

Since v1.2.0, `MCP_SOLUTION_ARCHITECTURE_V1` and `MCP_CAPABILITY_GENERATOR_V1` shipped with every dash, curly quote and arrow in their prompts mangled (`â€”` where an em dash belonged, `â†’` for an arrow): 76 instances across the two files, the result of the files passing through a Windows-1252 round trip. Models mostly read past it, but it was noise in every prompt those agents received. Both files are clean again.

### Changed: no em dashes in prompt text

Every agent prompt in every bundled workflow now uses plain ASCII punctuation: a spaced hyphen where an em dash was, `2-5` for ranges, `->` for arrows. The same applies to the text the server sends to models and MCP clients (the planner prompt, revision instructions, server instructions and tool descriptions). Punctuation in a prompt tends to show up in what the model writes, and em dashes are a recognizable AI-writing tell that generated documents shouldn't carry. A check for these characters is run before each release, so later edits don't reintroduce them.

### If you're upgrading

The exe only imports a bundled workflow if it isn't in your database yet, so it never overwrites a workflow you may have customized. That also means an existing install keeps its stored copies of these prompts after upgrading. A fresh install gets the fixed versions automatically. To refresh an existing install, have your connected assistant re-import the workflow files from `WorkflowsAndAgents/` on GitHub (`import_workflow` for the header, then `import_agent` for each agent). This replaces any local edits you've made to those agents.

---

## v1.4.0

**Release:** https://github.com/speakmancip/speakmanai-local/releases/tag/v1.4.0

### Headline changes

**1. Gemini 2.x retired, Gemini/Vertex defaults move to 3.x**
Google is shutting down the Gemini 2.x models in October 2026, so every 2.x model is gone from the setup page and the tier mappings. The new defaults for both the Gemini and Vertex AI providers:

| Tier | Before | Now |
|---|---|---|
| Fast | `gemini-2.5-flash-lite` | `gemini-3.5-flash-lite` |
| Standard | `gemini-2.5-flash` | `gemini-3.8-flash` |
| Advanced | `gemini-2.5-pro` | `gemini-3.1-pro-preview` |

`gemini-3.8-flash` is also selectable in the Fast and Advanced slots. It's worth trying in Advanced if 3.1 Pro's cost or speed is a problem for your workload.

**Upgrading:** the Windows exe migrates saved settings on startup. Any `gemini-2*` model in `~/.speakmanai/config.json` is replaced with the new default for that tier and the file is rewritten, so an existing install keeps working without a visit to the setup page. Docker and run-from-source users configure models through `.env`, which isn't touched automatically. Update `DEFAULT_MODEL`, `STANDARD_MODEL` and `ADVANCED_MODEL` by hand (`.env.template` shows the new values).

**2. `update_session` cascades now actually revise downstream agents**
Revising one agent in a finished session could leave the agents downstream of it returning their old output byte for byte. On a real Solution Architecture run, the Tech Architect gained a signed-QR-payload control and a CCPA deletion module, and the Compliance Officer re-ran and came back with the same 8.4 score, still listing the deletion workflow as missing.

The upstream data was never the problem: the compliance agent received the revised architecture. The prompt it got alongside it was. The cascade note named whichever step had run just before (here Visualization, which Compliance doesn't even depend on), never said what had changed, and told the agent to leave its output alone otherwise. Combined with the platform's "change only what the feedback calls out" instruction, keeping the old output was the model's correct reading.

Downstream agents are now told which agent the update started from and what the original change request was, with that request's own constraints explicitly scoped to the agent it was addressed to. They also get their own instruction: absorb the revised upstream content, keep everything it doesn't affect. Verified with a live re-run: the downstream output now changes.

### Under the hood
- `render_document` failed under Docker. One line in `document_renderer.py` used a backslash inside an f-string expression, which is a syntax error before Python 3.12, and the Docker image runs 3.11. The Windows exe bundles 3.12, which is why it never showed up there.
- `submit_response` used to mark the session `IN_PROGRESS` after queueing the resume, which could overwrite a status the engine had already moved on. If the resume reached the next delegate pause first, the session ended up showing `IN_PROGRESS` while it was actually waiting, and a client polling for input would wait forever. The pause is now claimed atomically before the resume is queued. A submit to a session that isn't waiting for input, such as one that has already completed, is now rejected instead of quietly reopening it.
- `poll_workflow` now returns a `pause_id` in `input_required`, and `submit_response` takes it as an optional argument. With it, a response can only apply to the pause it was written for. Without it, a repeated or late submit could land on the *next* pause: in testing, a duplicate submit for one step was read as a review-gate response for the step after it. The argument is optional, so existing clients keep working, but the MCP instructions now tell clients to always pass it.
- A re-opened human review gate now only picks up review feedback recorded for its own step.

The Docker, status-write and review-gate fixes were caught first in the hosted SPEAKMAN.AI platform and ported back here. `pause_id` is new in this release, and together with the status-write change it closes a next-pause race the hosted version still has.

---

## v1.3.0

**Release:** https://github.com/speakmancip/speakmanai-local/releases/tag/v1.3.0

### Headline features

**1. `render_document` — server-side document rendering, no local Python or Node required**
A new MCP tool converts a Markdown document into a fully self-contained, branded HTML document entirely on the server side — no local script, no Node/puppeteer dependency, nothing the calling client needs installed. This is what unblocks running `generate-sad` and `generate-compliance-report` from any MCP client, including ones that can't shell out to a local interpreter at all.

**2. Confirmed working: Microsoft Copilot Studio**
`generate-sad` has been run end-to-end inside a live Copilot Studio agent, connected to a local SPEAKMAN.AI server over a tunneled connection. SPEAKMAN.AI's MCP surface was already client-agnostic in principle; this is the first outside-Anthropic client it's been verified against.

### Under the hood
- PDF generation and the local template-directory system (`templates/default/`, `templates/corporate/`) are both retired — HTML is the deliverable now, print-to-PDF-able from any browser, with no per-user template setup required.
- The default document branding is a plain text wordmark rather than an embedded logo image — a full rendered document now runs about 1,400 tokens, down from what an unoptimized embedded logo would have cost (~57,000 tokens for the same document).
- Fixed a long-standing bug where a workflow step listing more than one agent in its `agents` array silently ran only the first one — the planner had always been free to bundle multiple agents into a single step, but the engine only ever executed one. Steps are now split and executed individually regardless of how the planner grouped them.
- Fixed a bug where reaching the HITL review-loop cap discarded the human's final round of feedback and advanced using stale, pre-feedback content instead of the freshly regenerated output.
- `RE_USE_CASE_ANALYST_V1` previously produced exactly one use case per functional requirement by construction; it now enumerates every use case a requirement actually implies (access patterns, CRUD completeness, screen-level states, actor variations), roughly 5x the catalog density on a real test project.
- `RE_DOMAIN_MODELER_V1` and `RE_API_SCHEMA_DESIGNER_V1`, previously single large agents, are each decomposed into focused sub-agents plus a mechanical synthesis step — reducing single-call cognitive load and the blast radius of any one regeneration.

---

## v1.2.1

**Release:** https://github.com/speakmancip/speakmanai-local/releases/tag/v1.2.1

### Expanded human review coverage

Every built-in workflow now has a human sign-off gate on the agents whose output ships straight into a delivered artifact with nothing else checking it first. `MCP_REQUIREMENTS_ENGINEERING_V1` gained six `HITL_VALIDATOR` gates — one each on the use case catalog, domain model, field definitions, naming dictionary, API schema, and database schema — replacing two older plain-pause review steps with the same revise-in-place loop the rest of the platform already uses. Three more workflows picked up a gate apiece: the Compliance Officer in Solution Architecture (previously the only agent in that pipeline with zero checks of any kind), the UX Needs Analyst in UX Design (the screen-inventory step everything downstream is built on), and the Sizing Context Clarifier in Sizing Estimate (the only agent reading raw business input, with nothing downstream able to catch a wrong reading).

### Also fixed

A workflow-generator meta-tool (`CLAUDE_WORKFLOW_CREATOR_V1`) had a dangling dependency on an agent ID that never existed — a leftover typo from before the shared planner was consolidated under one name.

---

## v1.2.0

**Release:** https://github.com/speakmancip/speakmanai-local/releases/tag/v1.2.0
**Announcement:** https://consulting.speakman.ai/blogs/speakmanai-v1-2-0

### Headline features

**1. Human-in-the-loop revision gates — `HITL_VALIDATOR`**
Any agent can now pause for a real human to review and either approve or send back with feedback, structurally parallel to the existing automated `AI_VALIDATOR` gate but driven by a person instead of a score. When an agent has both, the automated check always runs first — a human never spends time catching what a validator already could have. Revisions loop with the agent's own prior output plus the reviewer's feedback attached, up to a configurable retry limit, then force-advance rather than block forever.

**2. Delegate-first execution — hand a step to your own connected coding agent**
Any workflow step can now declare `executionMode: "delegate"`. Instead of a background model call, the engine pauses and hands the step to whichever coding agent is connected (Claude Code today), which executes it with real tools — filesystem, web fetch, your own MCP servers — instead of a human being asked to paste data in by hand. A new companion generator, `generate-speakmanai-skill-workflow`, builds workflows this way from the start.

**3. `update_session` — revise a finished session without starting over**
A new MCP tool reopens an already-completed session, revises one agent's output in light of new information, and cascades the change through everything that depends on it — nothing unaffected gets touched, and nothing gets regenerated from a blank slate. Every revised agent, from the one directly targeted to the last agent downstream, sees its own prior output and what changed, and revises in place rather than starting over. Guarded by the first genuinely atomic state transition in this codebase, so a duplicate or concurrent call can never double-run a revision.

**4. New workflow: UX & Interface Design**
A new built-in pipeline (`MCP_UX_DESIGN_V1`) maps architecture components and requirements into a screen inventory, checks that nothing traces back to a missing requirement, then proposes a design direction and produces clickable, self-contained HTML mockups for a human to actually react to — before any real code gets written. The design step runs through your connected coding agent rather than a fixed background model, since design judgment is exactly where the model behind that connection matters most.

### Under the hood
- Consolidated five separate, drifting copies of the workflow planner agent into one shared definition — found live, including one copy with a silently empty prompt that a `.get(key, default)` fallback bug let slip through undetected
- Fixed a real dedup bug where a human review loop could leave stale, superseded sections behind in an assembled project document
- Automated validators now actually read the dependency context their own prompts assumed they had — previously they only ever saw a primary agent's raw output, never the source-of-truth material it was checked against
- Every workflow-generator meta-tool now understands `HITL_VALIDATOR` as a first-class option alongside the existing automated validator

---

## v1.1.0

**Release:** https://github.com/speakmancip/speakmanai-local/releases/tag/v1.1.0
**Announcement:** https://consulting.speakman.ai/blogs/speakmanai-v1-1-0

### Headline features

**1. Three-tier model control — fast / standard / advanced**
Previously agents only had a "fast" and "advanced" model to choose from. A new **standard** tier sits between them, giving finer control over the cost/quality tradeoff on a per-agent basis across every workflow. No workflow files need editing — providers and models are swapped centrally on the setup page and every agent's abstract tier resolves automatically.

**2. Vertex AI now configurable directly in the app**
Enterprise GCP users no longer need to hand-edit `.env` files or Docker config to use Vertex AI. The setup page now has a one-click "Vertex AI" provider option — enter your GCP project ID and region, and it authenticates via Application Default Credentials (no API key to manage or leak).

**3. New workflow: Requirements Engineering**
A new built-in pipeline (`MCP_REQUIREMENTS_ENGINEERING_V1`) turns a business description into structured, implementation-ready technical contracts — domain model, use cases, field definitions, naming conventions, and a full OpenAPI schema — with validation loops at each stage so downstream coding agents get contracts they can build against directly.

**4. New workflows now appear automatically — no reset required**
Previously, adding a new built-in workflow only took effect on a brand-new install; existing users had to wipe their local database to see it. Now new workflows are picked up automatically on the next launch, without touching any workflows or agents you've already customized.

**5. Broader model support, including the newest frontier models**
Teams who want maximum quality (and are fine paying more per call) can now point any tier at the latest model families — Gemini 3 and Claude 5 (Sonnet 5, Fable) — right from the setup page, alongside the recommended cost-efficient defaults.

### Under the hood
- Fixed several model-resolution bugs where a configured model choice was silently ignored in favor of a hardcoded default (affected Anthropic users most)
- Fixed a Windows desktop-app bug where Vertex AI credentials could resolve to the wrong path
- Updated to `gemini-3.1-flash-lite` (Google's now-GA name, replacing the retired preview alias)

---

## v1.0.0

Initial release — SPEAKMAN.AI local multi-agent workflow engine.
