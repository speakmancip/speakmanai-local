# Release Notes

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
