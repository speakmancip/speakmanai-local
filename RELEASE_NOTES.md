# Release Notes

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

All three were caught first in the hosted SPEAKMAN.AI platform and ported back here. The `submit_response` fix goes a step further than the hosted version, which still loses that next-pause race.

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
