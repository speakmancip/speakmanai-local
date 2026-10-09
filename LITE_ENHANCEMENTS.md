# LITE_ENHANCEMENTS.md

This file tracks where `speakmanai_lite` has diverged from the enterprise SPEAKMAN.AI repo — features built here first that don't exist upstream yet. It exists so this work can be ported back rather than lost. Update it whenever a change here has no enterprise equivalent.

Each entry: what it is, why it was built, what it touches, and what porting to enterprise would involve.

---

## Multi-platform LLM API support

**What:** Every agent targets an abstract provider — Gemini, Claude (Anthropic), OpenAI, Ollama (local), or Vertex AI — chosen centrally on the setup page rather than hardcoded per workflow. Switching providers takes effect immediately, no workflow file edits.

**Why:** Lets a single install run against whatever the user already has access to or trusts (including fully local/offline via Ollama), instead of being locked to one vendor.

**Touches:** `orchestration/mcp-server/engine.py` (`_call_llm` and provider dispatch), the setup UI in `server.py`.

**Porting notes:** Already summarized in `README.md` and `RELEASE_NOTES.md` v1.1.0 ("Vertex AI now configurable directly in the app", "Broader model support"). Full detail not re-derived here — see those docs for the current provider/model matrix.

---

## Model-tier labeling (fast / standard / advanced)

**What:** Agents declare an abstract `model` tier (`fast`, `standard`, `advanced`) rather than a literal model name. Each tier resolves to a concrete model per the active provider, set centrally.

**Why:** Decouples workflow/agent definitions from any specific model family — a workflow authored today keeps working as model lineups change, and cost/quality tradeoffs are tunable per-tier without touching agent JSON.

**Touches:** Agent schema (`model` field), resolution logic in `engine.py`, setup UI in `server.py`.

**Porting notes:** Also covered in `RELEASE_NOTES.md` v1.1.0 ("Three-tier model control"). See that entry for the full before/after (previously only fast/advanced existed).

---

## Delegate-first execution mode (`executionMode: "delegate"`, `MCP_LLM_DELEGATE`)

**What:** Any `AI_*` agent can declare `executionMode: "delegate"` on its DB doc. At runtime, instead of calling a background LLM, the engine pauses the step (`agentType` rewritten to `MCP_LLM_DELEGATE`) and hands it to the connected coding agent (e.g. Claude Code) — which executes the task itself using real tools (filesystem, web, MCP servers, DB/API access) and calls `submit_response` with the result. A global override (`set_execution_mode`: `auto` / `force_delegate` / `force_background`) exists too.

**Why:** A background LLM has no tools — any step needing external data (a web lookup, a DB query, a file read) previously had to pause for a *human* to fetch and paste it in via `MCP_INPUT_REQUIRED`. Delegate mode lets the connected coding agent fetch it directly, and also lets the (usually stronger) coding-agent model do the reasoning work itself for steps where that's cheaper/better than a hosted call.

**Added this session (2026-08):**
- `WorkflowsAndAgents/CLAUDE_SKILL_WORKFLOW_CREATOR_V1.json` — a new meta-workflow (3 agents: `CLAUDE_SKILL_WORKFLOW_PLANNER_V1`, `CLAUDE_SKILL_WORKFLOW_ANALYST_V1`, `CLAUDE_SKILL_PROMPT_ENGINEER_V1`, all `executionMode: "delegate"`) that designs *other* delegate-first workflows. Its Analyst agent defaults every generated agent to `executionMode: "delegate"` unless it's a validator/aggregator/pure-drafting step, and requires every generated workflow to include its own `AI_PLANNER` agent (a gap found in the existing pipeline — see below).
- Companion skill `~/.claude/skills/generate-speakmanai-skill-workflow/SKILL.md` — drives that meta-workflow end-to-end. Notably, because the meta-workflow's own steps are *also* delegate, the skill itself performs the planning/architecture/prompt-engineering work in character at each pause, rather than waiting on a hosted model.

**Known pre-existing gap noted, now resolved:** ~~The original `generate-speakmanai-workflow` skill's Step 4b safety check assumes a generated workflow's planner agent is always literally named `GENERIC_WORKFLOW_PLANNER`, but the engine actually identifies the planner by `agentType == "AI_PLANNER"`...~~ — as of the planner consolidation below (2026-08-09), every workflow genuinely does share one agent literally named `GENERIC_WORKFLOW_PLANNER`, so that assumption is now correct in practice, not just in the engine's lookup logic. See "Planner consolidation" below — `generate-speakmanai-workflow`'s Step 4b was rewritten to match the new minimal-stub pattern.

**Touches:** `orchestration/mcp-server/engine.py` (`_handle_start`, `_handle_resume` step-type forcing), `server.py` (`poll_workflow` delegate messaging, `set_execution_mode` tool), `WorkflowsAndAgents/CLAUDE_WORKFLOW_CREATOR_V1.json` (pre-existing reference implementation this session's addition parallels).

**Porting notes:** This is a genuinely new execution model with no enterprise equivalent — porting means introducing the `executionMode` field, the `MCP_LLM_DELEGATE` agent-type rewrite, and the CLI-facing pause/resume contract (`poll_workflow`'s delegate message, `submit_response`) to the enterprise engine.

---

## HITL Loop capability (`humanFeedbackId`, `humanFeedbackConfig`, `HITL_VALIDATOR`)

**What:** A revision loop attached to any primary agent, structurally parallel to the existing `AI_VALIDATOR` gate but human-driven instead of LLM-scored:
```json
"humanFeedbackId": "SOME_HITL_AGENT_V1",
"humanFeedbackConfig": { "maxLoops": 3 }
```
`humanFeedbackId` points to a new agent-type, `HITL_VALIDATOR` — attached, not a DAG step (same non-step status as `AI_VALIDATOR`), authored the same way as an `MCP_APPROVER`/`MCP_INPUT_REQUIRED` agent (a `systemPrompt` shown as the review prompt, an `inputSchema` for the response: `{"status": "Approved"|"Update", "feedback": "..."}`).

When the primary agent produces output, the workflow pauses for human review instead of advancing. `Approved` continues the workflow; `Update` sends the primary agent's last output + the human's feedback + any named `dependencies` on the `HITL_VALIDATOR` doc back to the primary agent, which regenerates, and the cycle repeats up to `maxLoops` (then force-advances with a warning, same exhaustion behavior as `AI_VALIDATOR`). If an agent declares both `validatorAgentId` and `humanFeedbackId`, the AI validator runs first on every fresh output; the HITL gate only sees output that has already passed (or exhausted) automated validation.

**Why:** Previously, no mechanism existed for "a human should review this and either approve it or ask for changes, with the agent redoing the work using that feedback." The only human-facing step types (`MCP_REVIEWER`, `MCP_APPROVER`, `MCP_INPUT_REQUIRED`) are one-shot DAG steps — none of them loop back to revise a prior agent's output.

**Bundled fix:** `AI_VALIDATOR` agents have always declared a `dependencies` array in their DB docs (e.g. `MCP_BA_VAL_LOOP_V1` depends on `MCP_BUSINESS_CONTEXT_CLARIFIER_V1`), but `_handle_validate_step` never read it — validators only ever saw the primary agent's raw output, not the source-of-truth context their own prompts assume they have. Fixed in the same pass (validators now receive `# OUTPUT UNDER REVIEW` + compiled dependency context), since the new HITL gate needed the same dependency-compilation wiring anyway and the two attached-agent types now share the pattern.

**Added this session (2026-08):**
- `orchestration/mcp-server/engine.py` — new shared helpers `_emit_advance_event` (factored out of three duplicated inline blocks) and `_maybe_gate_by_hitl`; new `hitl_response` action + `_handle_hitl_response`; `hitl_loop`/`hitl_retry`/`hitl_feedback`/`hitl_pending_agent_id` event fields threaded alongside the existing `validation_loop`/`validation_retry`/`validation_feedback` fields; `HITL_VALIDATOR` excluded from DAG scheduling like `AI_VALIDATOR`; `_handle_validate_step` now compiles and sends validator dependency context.
- `orchestration/mcp-server/server.py` — `_get_mcp_pause_config` resolves HITL pauses directly from the pause event's own attributes (`hitl_pending_agent_id`) rather than via `workflow_definition.steps`, since a HITL gate isn't a real DAG step; `submit_response` routes to a new `hitl_response` queue action when `pause_kind == "hitl_feedback"`; `poll_workflow` has a new `HITL_VALIDATOR` message branch instructing the connected assistant to actually surface the review to the real user (ask questions, point out gaps) rather than self-approve or treat it like a delegate "do it yourself" step; `_aggregate_agent_index` skips `hitl_retry` bookkeeping events.
- `WorkflowsAndAgents/MCP_SOLUTION_ARCHITECTURE_V1.json` — worked example: `MCP_CONTEXT_CLARIFIER_HITL_V1` (`HITL_VALIDATOR`) attached to `MCP_BUSINESS_CONTEXT_CLARIFIER_V1` via `humanFeedbackId`.

**Design decisions (confirmed with the user):**
1. Gate order when both are configured: AI validator first, then HITL, on every fresh output (including HITL-triggered revisions).
2. Loop exhaustion: force-advance with the last output + warning log, matching `AI_VALIDATOR` — no blocking-forever state.
3. The `AI_VALIDATOR` dependency-context gap was fixed in this same pass rather than deferred.

**Follow-up:**
- ~~Teach `CLAUDE_WORKFLOW_ANALYST_V1` / `CLAUDE_SKILL_WORKFLOW_ANALYST_V1` about `HITL_VALIDATOR`~~ — done (2026-08). All three workflow-generator files in `WorkflowsAndAgents/` (`WORKFLOW_CREATOR_V1.json`, `CLAUDE_WORKFLOW_CREATOR_V1.json`, `CLAUDE_SKILL_WORKFLOW_CREATOR_V1.json`) now teach their Analyst and Prompt Engineer agents about `HITL_VALIDATOR` as a first-class option: when to use it vs `AI_VALIDATOR` (judgment call vs objective check) vs `MCP_INPUT_REQUIRED` (reviewing existing output vs gathering data), the validator-first gate ordering when combined with `validatorAgentId`, the `humanFeedbackConfig` shape, and — for the delegate-first generator specifically — that it's orthogonal to `executionMode` (a delegate agent's output can still get a human sign-off gate; the HITL_VALIDATOR itself never delegates to the connected coding agent, it always pauses for the real human). Imported into the DB; not yet exercised live through a freshly-generated workflow.
- `generate-speakmanai-workflow` / `generate-speakmanai-skill-workflow` SKILL.md documentation passes for driving `HITL_VALIDATOR` pauses (behavior is already correct today since both skills just read `input_required` generically, but worth calling out explicitly). Still open.

**Touches:** `orchestration/mcp-server/engine.py`, `orchestration/mcp-server/server.py`, any workflow JSON under `WorkflowsAndAgents/` that opts an agent into `humanFeedbackId`.

**Porting notes:** Requires the enterprise engine's equivalent of the `AI_VALIDATOR` attached-gate mechanism as a prerequisite (this was built as a structural sibling to it, reusing the same pause/retry event shape). The `HITL_VALIDATOR` agent-type and the `hitl_*` event field family are new; the `AI_VALIDATOR` dependency-context fix is a small, independently portable bugfix that should land regardless of whether the HITL feature itself ports.

**Live-testing note:** The local MCP server (`http://localhost:8000/mcp`, `orchestration/mcp-server/server.py` run via uvicorn) was running as a persistent process before these edits — it needed a restart (rebuilt exe) to pick up this session's `engine.py`/`server.py` changes. After restart, the HITL loop was exercised live end-to-end: standalone on `MCP_BUSINESS_CONTEXT_CLARIFIER_V1` (approve-on-first-pass and update-then-approve, both paths), and in combination with an `AI_VALIDATOR` on `MCP_BUSINESS_ANALYST_V1`, `MCP_BUSINESS_APPLICATION_ARCHITECT_V1`, and `MCP_TECHNICAL_SOLUTION_ARCHITECT_V1` (validator ran silently in the background before every HITL pause, exactly as designed) — full run against a real business concept (`D:\git\tutorial\input\tip_app_concept.txt`), producing a complete, human-reviewed Solution Architecture Document.

**Bug found and fixed during that run:** `build_project_record` (engine.py) appended one list entry per *qualifying event* rather than deduping by `agent_id`. For a normal step this only ever produces one entry, so it was never a problem before — but a `HITL_VALIDATOR` pause event is not a step advance (`current_step_index` doesn't move) and isn't marked `hitl_retry`, so each review loop's pause added another entry for the same agent, alongside the final approved one. An agent revised twice ended up with 3 near-duplicate/stale sections. `get_output`/`poll_workflow` were unaffected (`_aggregate_agent_index` in server.py already dedups via a dict), but `get_project`/`generate_document` read directly from this list, so a document assembled through `generate_document` after a HITL loop would have included superseded drafts alongside the final one. Fixed by building `sections` as a dict keyed by `agent_id` (last-write-wins) before converting to a list, mirroring `_aggregate_agent_index`'s existing approach. Confirmed the fix imports cleanly; not yet re-verified live (the completed test session already had its project record built pre-fix — its downstream `MCP_TECHNICAL_WRITER_V2` step was unaffected since agent-to-agent context passing uses the separately-deduped `_compile_dependencies_context` path, not this one).

---

## Planner consolidation — single shared `GENERIC_WORKFLOW_PLANNER` (2026-08-09)

**What:** Every workflow's `AI_PLANNER` agent is now the same single database document, `GENERIC_WORKFLOW_PLANNER`, referenced from each workflow's own JSON as a minimal stub:
```json
{ "agentId": "GENERIC_WORKFLOW_PLANNER", "workflows": ["<this workflow's ID>"] }
```
No other fields. The real `systemPrompt`/`model`/`temperature`/`executionMode`/etc. live once, centrally, on that one document — canonically mirrored in `WorkflowsAndAgents/MCP_SOLUTION_ARCHITECTURE_V1.json` (the only file that carries the full definition; every other workflow file carries only the stub).

**Why:** Live inspection of the running DB found **five separate planner agents** doing the identical job — `GENERIC_WORKFLOW_PLANNER`, `CLAUDE_GENERIC_WORKFLOW_PLANNER`, `CLAUDE_SKILL_WORKFLOW_PLANNER_V1`, `VCF_PLANNER_V1`, `SIZING_WORKFLOW_PLANNER_V1` — one per workflow, instead of one shared. Two concrete problems this caused:
1. `SIZING_WORKFLOW_PLANNER_V1` was a byte-identical hand-copy of `GENERIC_WORKFLOW_PLANNER`'s prompt — pure drift risk, since a future prompt fix would need to be applied in both places by hand.
2. `VCF_PLANNER_V1` (for `VIBE_CODE_FIXER_PIPELINE_V1`) had a **literal empty-string `systemPrompt`** live in the DB. Traced to a false claim written into `CLAUDE_SKILL_WORKFLOW_ANALYST_V1`'s own instructions — "set systemPrompt to `""`, a fixed canonical prompt is applied at import time" — which does not exist anywhere in `import_agent`/`import_architecture_plan`. Compounded by a second real bug: `engine.py`'s `planner_agent.get("systemPrompt", DEFAULT_PLANNER_SYSTEM_PROMPT)` only falls back when the key is *missing*, not when it's present-but-empty, so the empty string was used as-is.

The correct pattern (confirmed against the enterprise product's own convention) was never "create a new planner per workflow" — it was always "register the new workflow onto the one existing shared planner." That registration mechanism already existed and already worked correctly: `import_agent`/`import_architecture_plan` both pop `workflows` out of the `$set` payload and merge it separately via `$addToSet`/`$each`, so re-importing an agent never clobbers its existing `workflows` array. The generator prompts simply never knew this pattern existed, so they kept inventing new planner agents instead of using it.

**Fixed:**
- `orchestration/mcp-server/engine.py:587-588` — `.get(key, default)` → `.get(key) or default`, so an empty-but-present `systemPrompt`/`model` falls back correctly instead of being used as-is.
- All three workflow-generator meta-tools (`WORKFLOW_ANALYST_V1`, `CLAUDE_WORKFLOW_ANALYST_V1`, `CLAUDE_SKILL_WORKFLOW_ANALYST_V1`, plus `CLAUDE_SKILL_PROMPT_ENGINEER_V1`) now instruct: reuse `GENERIC_WORKFLOW_PLANNER` via the minimal stub, never invent a new planner agentId, never add other fields to the stub (explained why — `$set` semantics mean any other field would silently overwrite the shared document for every other workflow depending on it). `CLAUDE_SKILL_WORKFLOW_ANALYST_V1`'s false "canonical prompt applied at import time" claim removed.
- `~/.claude/skills/generate-speakmanai-workflow/SKILL.md` Step 4b rewritten to match — it previously carried its own embedded "canonical" planner prompt (the old, now-stale, verbose text) and would rewrite any planner entry back to a full agent block before import. That directly conflicted with the new pattern and would have silently reverted the consolidation on the next workflow generated through this skill. Now it enforces the minimal-stub shape instead.
- Live DB: `GENERIC_WORKFLOW_PLANNER` updated to the validated, terser prompt (previously used only by `CLAUDE_GENERIC_WORKFLOW_PLANNER` — confirmed structurally identical in task/output-schema terms to the older verbose prompt, just written for less hand-holding) and registered to all 8 workflows; the 4 orphaned clones deleted. Three agents' `dependencies` arrays (`CLAUDE_WORKFLOW_ANALYST_V1`, `CLAUDE_SKILL_WORKFLOW_ANALYST_V1`, `CLAUDE_SKILL_PROMPT_ENGINEER_V1`) that pointed at the deleted clones were repointed to `GENERIC_WORKFLOW_PLANNER`.

**Design note on `executionMode`:** the shared planner's own `executionMode` stays `"auto"` (background) rather than `"delegate"`, even though two of the eight workflows (`CLAUDE_WORKFLOW_CREATOR_V1`, `CLAUDE_SKILL_WORKFLOW_CREATOR_V1`) are delegate-first. This is safe because `engine.py`'s `is_planner_delegated` check is `exec_mode == "force_delegate" or (exec_mode == "auto" and planner_exec_mode == "delegate")` — the *global* execution-mode setting (`force_delegate`) overrides the planner's own doc-level default unconditionally. So planning defaults to background for everyone, and flipping the global mode to `force_delegate` before running one of the two delegate-first generators hands planning to the connected coding agent too, exactly as before — without needing a second shared planner document.

**Touches:** `orchestration/mcp-server/engine.py`, all 6 `WorkflowsAndAgents/*.json` files with a planner reference (7th, `VIBE_CODE_FIXER_PIPELINE_V1`, has no source file — DB-only), `~/.claude/skills/generate-speakmanai-workflow/SKILL.md`.

**Porting notes:** The single-shared-planner pattern is (per the user) already the enterprise convention — this brings `speakmanai_lite` in line with it rather than diverging further. The `engine.py:587-588` fallback fix and the `$addToSet`-based workflow-array merge in `import_agent`/`import_architecture_plan` are the two pieces worth checking against the enterprise engine if it hasn't already been ported.

---

## `update_session` — revise-in-place update cascade ("Correct Course")

**What:** A new MCP tool, `update_session(session_id, agent_id, update_content)`, that reopens an already-`COMPLETED`/`CANCELLED` session, revises one agent's output in light of new information, and cascades that revision through every agent that transitively depends on it — without regenerating anything unaffected. Each revised agent (the directly-targeted one and every downstream dependent) sees its own prior output plus what changed, and revises in place rather than regenerating from scratch, bounding drift on the rewrite.

**Why:** Previously, `COMPLETED` was a hard terminal state — nothing could revise a finished session; a changed requirement meant re-running the whole workflow from scratch and losing the original decision trail. This is the "Correct Course" gap from this session's BMAD-METHOD comparison, deliberately reframed as an engine capability (per the user: SPEAKMAN.AI itself should be the audit manager for this, not a skill working around the engine from outside).

**State machine:** Only `COMPLETED`/`CANCELLED` sessions are eligible. The transition to a new status, `DRAFT`, is a genuinely atomic conditional SQL `UPDATE ... WHERE session_id=? AND current_status IN (...)` — the *first* atomic check-and-set primitive in this codebase (`database_sqlite.py`'s `update_one_if`; every other write path here, including the ones used everywhere else in this same feature, is check-then-write). This single operation is both the state-restriction guard and the idempotency mechanism: only one of two racing `update_session` calls can ever win it, with no caller-supplied idempotency key needed (`session_id` is already 100% server-generated). An `IN_PROGRESS`/`AWAITING_INPUT` session must be explicitly cancelled first via the existing `cancel_session` tool — no silent auto-cancel.

**Cascade mechanics:** `_resolve_downstream_agents` walks the `dependencies` graph declared on each agent's DB doc (fixed-point, transitive, wildcard-`*` dependents like `AI_AGGREGATOR` always included). `_emit_advance_event` gained an optional `cascade` param — when set, it jumps straight to the next *affected* step instead of `current_step_idx + 1`, and seeds the next affected agent's own prior output + a note that its upstream context changed. `cascade` threads through every step-execution function (`_maybe_gate_by_hitl`, `_handle_hitl_response`, `_handle_validate_step`, `_handle_process_step`, `_handle_resume`) exactly parallel to how `hitl_loop` already threads — so an update cascade composes correctly with existing `AI_VALIDATOR`/`HITL_VALIDATOR` gates on any agent it passes through (validator-first, then HITL, same as always, just cascade-aware). A new event marker, `update_retry`, added to every existing `validation_retry`/`hitl_retry` skip-filter, so the entry agent's self-loop retry-request event stays invisible to last-write-wins scans — but critically, the *cascade-advance* events (carrying each agent's genuinely new output) are deliberately **not** marked this way, or the regenerated content would never surface.

**Added this session (2026-08-09):**
- `orchestration/mcp-server/database_sqlite.py` — `SQLiteCollection.update_one_if`, the new atomic conditional-update primitive (events_raw/sessions table only).
- `orchestration/mcp-server/engine.py` — `transition_completed_or_cancelled_to_draft`, `resolve_agent_step_index`, `_latest_output_for_agent`, `_resolve_downstream_agents`, `_extract_cascade`, `_handle_update_session` (new handler, dispatched via a new `update_session` queue action); `cascade` param threaded through the six functions above; `update_retry` skip-filter added to `build_project_record` (both call sites) and `_compile_dependencies_context`.
- `orchestration/mcp-server/server.py` — new `update_session` MCP tool (mirrors `cancel_session`'s structure/annotations); `update_retry` skip-filter added to `_compile_dependencies_context` and `_aggregate_agent_index`; new `update_feedback` block in `_get_mcp_pause_config` parallel to the existing `validation_feedback`/`hitl_feedback` ones, so a re-paused delegate agent (entry or downstream) actually sees the update request.

**Confirmed already correct, no changes needed:** `_aggregate_agent_index`, `build_project_record`, and `_compile_dependencies_context` all already scan the full ordered event list and dict-overwrite by `agent_id` (last-write-wins) — a cascade's newly-appended events are picked up automatically. The originally-suspected "always fetch latest" gap turned out not to exist.

**Mongo-mode portability:** confirmed, no new code needed — `update_one({"session_id": sid, "current_status": {"$in": [...]}}, {"$set": {...}})` is atomic by default in MongoDB. The `update_one_if` primitive exists only because SQLite mode's abstraction layer never had a conditional-write path before this.

**Live-verified end-to-end** against a real completed `MCP_SOLUTION_ARCHITECTURE_V1` session (full detail in `project_state.md`): guard path, invalid-agent path, and concurrent-call path all correctly rejected; main cascade test (adding an in-app merchant reporting requirement) correctly regenerated the targeted agent and all 4 real dependents while leaving the one upstream, unaffected agent **byte-identical**; validator-first-then-HITL held through 3 sequential HITL gates in the cascade; `get_project` showed no duplicate/stale sections; a standalone 10-way concurrent stress test on `update_one_if` produced exactly 1 winner every time.

**Touches:** `orchestration/mcp-server/database_sqlite.py`, `orchestration/mcp-server/engine.py`, `orchestration/mcp-server/server.py`. No workflow JSON changes — works generically against any completed session for any workflow.

**Explicitly out of scope (deferred, not forgotten):** no caller-supplied idempotency key (session_id already serves this role), no cross-SDLC-phase orchestration (a human re-running a downstream phase skill against the same session_id, once it exists, is the whole integration for now), no direct-content-replacement mode (every revision goes through the real agent/LLM, never a raw patch), no natural-language change-detection (the caller states `agent_id` + `update_content` explicitly).

**Porting notes:** The CAS primitive (`update_one_if`) is SQLite-shim-specific; enterprise/Mongo mode gets the same guarantee natively via a filtered `update_one`, already confirmed in code. The cascade mechanics (`_resolve_downstream_agents`, the `cascade` threading pattern, the `update_retry` marker family) are fully portable as-is — they only depend on the DB abstraction's existing `find`/`update_one` interface, not anything SQLite-specific.

---

## FIXED (2026-08-11): multi-agent steps silently drop every agent but one

**Status:** Diagnosed and fixed same day. Confirmed by the user to be an old, latent bug present since the engine's original design, not something introduced by this session's HITL work. Confirmed the enterprise version does not have this limitation.

**Fix applied:** both parts of the recommended fix below were implemented — `_handle_start` and `_handle_resume`'s delegated-planner-resume path now split any planner-generated step with more than one agent into separate sequential single-agent steps (step_type recomputed per agent, `step_index` renumbered across the whole list) before execution ever begins, so nothing can silently drop again regardless of what the planner outputs. `GENERIC_WORKFLOW_PLANNER`'s systemPrompt was also updated to instruct exactly one agent ID per step. Verified: `engine.py` compiles clean; both prompt-only sibling fixes in this file were live-imported into the DB. The engine-code half needs a rebuilt exe + server restart to take effect (compiled code, not DB-loaded).

**What:** `_handle_process_step` (`engine.py`, ~line 1071) executes only `agent_configs[0]` when a workflow step's `agents` array contains more than one agent ID — every other agent in that step is silently dropped. No error, no log warning, no partial-completion marker. The comment already on that line names this as a deliberate simplification: `# For this stripped-down local version, we assume sequential execution (one agent per step)`.

**Why it surfaced now:** Workflows that use the dynamic `GENERIC_WORKFLOW_PLANNER` (i.e. every non-`system`-type workflow) get a *freshly LLM-generated* step graph on every session run — nothing guarantees the planner keeps independent agents in separate steps. Caught live on a real `MCP_SOLUTION_ARCHITECTURE_V1` run ("HelpSell.ca Solution Architecture", `session-a8c7072b-...`): the planner bundled `MCP_TECHNICAL_VISUALIZATION_SPECIALIST_V1` and `MCP_COMPLIANCE_OFFICER_V2` into one step (`step_index: 5`, both agents in the same `agents` array, both depending only on `MCP_TECHNICAL_SOLUTION_ARCHITECT_V1`) — a legitimate reading of the dependency graph, since neither depends on the other. Only the Visualization Specialist ran; the Compliance Officer (and its brand-new `MCP_COMPLIANCE_HITL_V1` gate, added this session) never fired at all. Confirmed via direct inspection of the session's `workflow_definition` and event log in the SQLite DB — the DB registration of the agent (dependencies, `humanFeedbackId`) was completely correct; this is purely a runtime execution gap.

**Non-determinism note:** `agent_configs[0]` isn't even deterministically "the first agent listed in the step's `agents` array" — `database_sqlite.py`'s `$in` query (`_kv_fetch_all` → `_matches`) doesn't preserve input-array order, so which of the bundled agents silently wins is effectively arbitrary per run.

**Blast radius:** Any workflow using the dynamic planner (every built-in workflow except `system`-type ones) can hit this any time two or more agents in the dependency graph happen to share the same upstream dependency and the planner decides to co-locate them. Plausible this has silently dropped content on past runs of other workflows too, just unnoticed because nothing was watching closely enough to catch it — this is the first time a HITL gate on the dropped agent made the omission visible.

**Recommended fix (not yet implemented), two parts:**
1. **Engine-level, defensive/deterministic:** after the planner returns its step list in `_handle_start` (and the equivalent post-processing in `_handle_resume`'s delegated-planner-resume path), split any step with more than one agent (except genuine `AI_AGGREGATOR`/`AGGREGATE` steps, which are fan-in by design) into separate sequential single-agent steps, renumbering `step_index` and fixing up `dependencies`. Guarantees correctness regardless of what the planner does.
2. **Prompt-level, reduces how often splitting is even needed:** teach `GENERIC_WORKFLOW_PLANNER`'s systemPrompt that every step's `agents` array must contain exactly one agent ID — parallel-eligible agents still get separate sequential steps, since this engine has no true parallel-execution model (one scalar `current_step_index` per session).

**Touches (once fixed):** `orchestration/mcp-server/engine.py` (`_handle_start`, `_handle_resume`), the `GENERIC_WORKFLOW_PLANNER` agent doc's `systemPrompt`.

**Porting notes:** User confirmed the enterprise version handles this correctly already (real parallel step execution, not the single-agent-per-step simplification this stripped-down local engine assumes) — nothing to port for this one, it's a lite-only gap being closed to match parity, not a lite-only feature to carry upstream.

---

## FIXED (2026-08-11): Use Case Analyst caps at 1 happy + 1 unhappy scenario per use case

**Status:** Found and fixed same day. Found via a real `MCP_REQUIREMENTS_ENGINEERING_V1` run (10 use cases, exactly 20 scenarios — 1 happy + 1 unhappy each, no exceptions), surfaced specifically because the new `RE_USE_CASE_HITL_V1` gate (added this session) put a human in front of the catalog and prompted the "is this actually enough coverage?" question an AI-only validator wasn't asking.

**Fix applied:** all three recommended changes below were implemented in `RE_USE_CASE_ANALYST_V1` and `RE_USE_CASE_VALIDATOR_V1`'s systemPrompts and live-imported into the DB — instruction rewritten to require one unhappy scenario per applicable category (not "at least one" total) and allow multiple happy paths where genuinely warranted, example schema expanded from 1 to 3 scenario objects, and the validator's check #2 rewritten to verify per-category coverage by name rather than a non-zero unhappy count.

**What:** `RE_USE_CASE_ANALYST_V1` reliably produces exactly one happy-path and one unhappy-path scenario per use case, even when a use case genuinely has multiple distinct failure modes (e.g. invalid input *and* a business-rule violation *and* an auth failure, all realistically applicable to the same UC) or multiple valid entry patterns worth its own happy-path scenario.

**Why — three compounding causes, all in `RE_USE_CASE_ANALYST_V1`'s own systemPrompt:**
1. The instruction sets only a floor: "the happy path and *at least one* unhappy path." It separately lists 4 failure categories that scenarios "must cover... where applicable" (invalid input, auth failure, resource not found, business rule violations), but never says *one scenario per applicable category* — so a single vague unhappy scenario gesturing at several categories at once technically satisfies the instruction.
2. The JSON example schema embedded in the prompt shows exactly **one** scenario object in the `scenarios` array. LLMs pattern-match a single-example schema's cardinality even when the prose says "at least" — this is likely doing more work to anchor the 1-and-1 output than the prose itself.
3. `RE_USE_CASE_VALIDATOR_V1`'s own bar is the same floor — check #2 is just "every UC has at least one scenario with type 'unhappy'," never whether more categories genuinely applied and got collapsed into one. A thin catalog still scores 8-10 and never gets sent back for revision.

**Recommended fix (not yet implemented):**
- Rewrite `RE_USE_CASE_ANALYST_V1`'s instruction to require one unhappy scenario **per applicable category**, not "at least one" total — and allow multiple happy-path scenarios when a use case genuinely has more than one valid entry pattern.
- Expand the embedded JSON example schema to show 2-3 scenario objects instead of 1, so the few-shot cardinality signal matches the intended range.
- Tighten `RE_USE_CASE_VALIDATOR_V1` check #2 to verify category coverage, not just a non-zero unhappy count.

**Touches (once fixed):** `WorkflowsAndAgents/MCP_REQUIREMENTS_ENGINEERING_V1.json` — `RE_USE_CASE_ANALYST_V1` and `RE_USE_CASE_VALIDATOR_V1` systemPrompts only. No engine code changes needed — this is a prompt-quality gap, not a runtime bug.

**Porting notes:** Prompt-only fix, portable to the enterprise version's equivalent use-case agent (if one exists) with no engine dependency either way.

---

## FIXED (2026-08-11): Domain Modeler doesn't reconcile domain_events against every use case on the first pass

**Status:** Found and fixed same day, same root-cause family as the Use Case Analyst gap above — surfaced via the `RE_DOMAIN_HITL_V1` gate on a real `MCP_REQUIREMENTS_ENGINEERING_V1` run. Already self-corrected for that specific session via the normal HITL revise-in-place loop (see below) — this entry originally tracked the underlying prompt gap, now closed at the source too.

**Fix applied:** added a "MANDATORY GATE — domain event reconciliation" instruction to `RE_DOMAIN_MODELER_V1`'s systemPrompt (mirrors the proven `Phase_3_Self_Coverage_Check` pattern already used in `MCP_BUSINESS_ANALYST_V1`), requiring every state-changing scenario across the full use case catalog to have a corresponding `domain_events` entry before output is emitted, plus a `source_use_case` field on each event for traceability. `RE_DOMAIN_VALIDATOR_V1` also gained a 6th check verifying this same reconciliation, naming any scenario with no corresponding event. Both live-imported into the DB.

**What:** `RE_DOMAIN_MODELER_V1`'s first-pass output can under-cover the use case catalog it was given — aggregates get modeled correctly, but not every use case's distinct state transitions make it into `domain_events`. On the run that surfaced this, the use case catalog had just been revised via `RE_USE_CASE_HITL_V1` to add UC-11 (subscription trial-to-paid transition, including a failed-auto-charge scenario) and UC-12 (listing cancellation) — the modeler's first pass correctly built out `Storefront` and `Listing` aggregates (proving it *did* see both new use cases) but only emitted `StorefrontTrialExpired`, with no `ListingCancelled`/`ListingWithdrawn` event and no distinct payment-failure event (`StorefrontPaymentFailed` or similar) for UC-11's failed-charge path.

**Confirmed NOT a stale-data bug — verified, not assumed:** replayed the exact `_compile_dependencies_context` last-write-wins scan against the real event log up to the moment the Domain Modeler's first pass ran, and confirmed it received all 12 use cases (including UC-11/UC-12) as input context. The gap is purely in generation completeness, not context retrieval — structurally identical to the Use Case Analyst finding above, just one agent downstream.

**Already resolved for that session:** the `RE_DOMAIN_HITL_V1` gate caught it, the human's "Update" feedback triggered a regeneration, and the second pass correctly added both `ListingCancelled` and `StorefrontPaymentFailed` — that revised output is the approved record the session advanced on. Nothing to clean up in that session's data.

**Recommended fix (not yet implemented):** add an explicit reconciliation instruction to `RE_DOMAIN_MODELER_V1`'s systemPrompt — for every use case in the input catalog, confirm at least one `domain_events` entry corresponds to each of its distinct scenarios/state transitions (happy and unhappy alike, once the Use Case Analyst fix above lands and starts producing more scenarios per use case), not just that the owning aggregate exists.

**Touches (once fixed):** `WorkflowsAndAgents/MCP_REQUIREMENTS_ENGINEERING_V1.json` — `RE_DOMAIN_MODELER_V1` systemPrompt only. No engine changes.

**Porting notes:** Prompt-only fix, same portability profile as the Use Case Analyst gap above.

---

## FIXED (2026-08-11): HITL loop-cap silently discards the human's final feedback

**Status:** Found and fixed same day, on a real `MCP_REQUIREMENTS_ENGINEERING_V1` run, `RE_API_SCHEMA_HITL_V1` gate. Confirmed via direct event-log inspection, not inferred. Was the most urgent of this batch — silently dropped real human input with zero signal to the client.

**Fix applied — both parts:**
1. **Loop-cap bug:** `_handle_hitl_response` no longer short-circuits on `status == "Approved" or (hitl_loop + 1) >= max_loops` — only an actual `"Approved"` advances immediately. Every `"Update"` now always triggers a real regeneration, cap or no cap. The cap check moved to the single choke point every path already flows through before pausing or advancing — `_maybe_gate_by_hitl` — which now compares `hitl_loop >= max_loops` and, if exhausted, calls `_emit_advance_event` with the **freshly regenerated** content (not stale pre-round content) instead of writing another pause event. A new `hitl_cap_reached: "true"` marker is stamped on that advance event for auditability (deliberately NOT added to any last-write-wins skip-filter, since this event carries genuinely new, real content that downstream context-compilation must still pick up). This required zero changes to `_handle_process_step` or `_handle_validate_step` — both already thread `hitl_loop` correctly through validator-retry sub-loops into whichever `_maybe_gate_by_hitl` call eventually fires, confirmed by re-reading the validator-retry branch (`hitl_loop` preserved unchanged across `validation_retry` events).
2. **Regeneration-fidelity mitigation:** `_handle_process_step`'s three feedback-injection blocks (`validation_feedback`, `hitl_feedback`, `update_feedback` — shared by every revise-in-place loop platform-wide, not just HITL) now all prepend a shared `PRESERVE_INSTRUCTION`: "this is a targeted revision, not a rewrite... everything else in your previous output... must carry over exactly as it was." Applies uniformly to every agent using any of these three revision paths, not just the API Schema Designer.

**What:** `_handle_hitl_response` (`engine.py`, line 633): `if status == "Approved" or (hitl_loop + 1) >= max_loops:`. Hitting the loop cap is treated identically to the human explicitly approving — the step force-advances using the **stale pre-round content**, and whatever feedback the human just submitted in that final "Update" is discarded entirely. The only trace is a server-side log line (`"Advancing despite unresolved feedback"`) the MCP client never sees. Confirmed live: on a 3-round (`maxLoops: 3`) API Schema Designer HITL loop, the human's 3rd "Update" submission (a precise, well-scoped "two focused fixes, preserve everything else" feedback) triggered this branch — the session's final `COMPLETED` content is byte-identical to round 2's output; the 3rd round's feedback was never applied to anything.

**Compounding factor found in the same incident (separate, real issue, not a data bug):** verified the injection pipeline itself is correct — replayed the actual round-3 retry event and confirmed `previous_output` was exactly the prior round's real content and `hitl_feedback` was the human's full, explicit text. Despite that, the round-2→round-3 regeneration regressed on several things the feedback explicitly said to preserve (component schemas that existed in round 2 disappeared in round 3, a size cap was lost, a previously-removed redundant field came back) while still failing to apply either of the two requested fixes. This is a genuine LLM full-document-regeneration reliability problem on large structured outputs (this schema was ~40-50K characters) — the model isn't doing a faithful minimal edit even when told exactly what to preserve, and it compounds with the loop-cap bug above: by the time the cap silently discards feedback, the "stale" content it force-advances with may already have regressed from an earlier, better round.

**Recommended fix (not yet implemented):**
1. **Loop-cap bug (engine, clear win):** on the final permitted "Update," attempt one last regeneration incorporating the feedback before force-advancing, instead of discarding it outright — and/or surface a clear signal in the tool response that the loop cap was hit and the final feedback may not be reflected, so the human knows to use `update_session` afterward if it matters.
2. **Regeneration-fidelity problem (harder, needs discussion):** the generic HITL-feedback injection block in `_handle_process_step` (shared by every HITL-gated agent platform-wide) could add an explicit "anything not called out above must be preserved character-for-character" instruction, rather than relying solely on the human's own feedback wording to say so. Worth checking whether this is model/provider-dependent (same class of concern already noted for the UX Design delegate step — weaker models handling large structured edits worse). Per-agent `maxLoops` is already configurable — bumping it for schema-heavy agents specifically is a cheap interim mitigation, though it doesn't fix the underlying drift.

**Touches (once fixed):** `orchestration/mcp-server/engine.py` (`_handle_hitl_response` for the loop-cap fix; `_handle_process_step`'s injection block for the fidelity mitigation).

**Porting notes:** The loop-cap silent-discard behavior should be checked against the enterprise version's equivalent HITL mechanism if one exists — not yet confirmed either way.

---

## BUILT (2026-08-11): decompose RE_DOMAIN_MODELER_V1 and RE_API_SCHEMA_DESIGNER_V1

**Status:** Designed and built same day, cross-repo (`speakmanai_lite` + `speakmanai-cc`). Full design with a Mermaid diagram was worked through as a published Artifact during design; this entry is the durable written record. Motivated directly by the loop-cap/regeneration-fidelity incident above ($7 of retries against `RE_API_SCHEMA_DESIGNER_V1` in one session) plus the domain-events coverage gap — both incidents hit agents that bundle multiple independent concerns into one large, input-scaling JSON output, which is exactly the shape that's hardest for an LLM to faithfully revise across HITL rounds.

**Build notes:** all 14 new agent docs (6 removed: `RE_DOMAIN_MODELER_V1`/`RE_DOMAIN_VALIDATOR_V1`/`RE_DOMAIN_HITL_V1`/`RE_API_SCHEMA_DESIGNER_V1`/`RE_API_SCHEMA_VALIDATOR_V1`/`RE_API_SCHEMA_HITL_V1`) authored and live-imported into the local DB exactly as designed below — 29 total agents in the workflow, verified zero dangling dependencies and zero stale references to the removed IDs. One gap caught during import verification (not a design gap): 5 gate/validator docs that reference the domain model as cross-check context (`RE_NAMING_HITL_V1`, `RE_FIELD_DEFINITIONS_HITL_V1`, `RE_DB_SCHEMA_HITL_V1`, `RE_DB_SCHEMA_VALIDATOR_V1`, `RE_FIELD_DEFINITIONS_VALIDATOR_V1`) were correctly renamed in the source file but initially left out of the import batches — caught by re-querying the live DB after the first two batches, fixed with a third batch. Also fixed in the same pass, same session: the Use Case Analyst's cardinality gap (see below) and the `generate-requirements`/`generate-speakmanai-code` skills in `speakmanai-cc` (plus their Desktop plugin mirrors and the rebuilt `speakmanai-sdlc.zip`) — including an adjacent stale-reference cleanup found along the way (`RE_NAMING_REVIEW_V1`/`RE_FIELD_DEFINITIONS_REVIEW_V1` still described in `generate-requirements`'s AWAITING_INPUT handling section, left over from the HITL-gate upgrade earlier this session and never updated; corrected to describe the actual `{status, feedback}` HITL_VALIDATOR response shape). Pure workflow JSON + skill-doc changes — no engine code involved, no exe rebuild needed for this piece.

**Precedent already proven in this workflow:** `RE_FIELD_DEFINITIONS_CATALOG_V1` exists as its own agent specifically because field constraints were pulled out of domain modeling earlier. This plan applies the same principle to the two remaining "kitchen sink" agents.

### The split

**`RE_DOMAIN_MODELER_V1` → three agents:**
- `RE_DOMAIN_STRUCTURE_V1` — aggregates, entities, value objects, repository interfaces, DDD layer assignment. Same inputs as today (`RE_CONTEXT_INTAKE_V1`, `RE_USE_CASE_ANALYST_V1`). Own `AI_VALIDATOR` + `HITL_VALIDATOR` gate.
- `RE_DOMAIN_EVENTS_V1` — `domain_events` only, reconciled against every use case scenario *and* the settled structure from the step above (depends on `RE_USE_CASE_ANALYST_V1` + `RE_DOMAIN_STRUCTURE_V1`). This is exactly the concern that had the coverage bug — now isolated so a fix to it can never regress the aggregate list. Own `AI_VALIDATOR` + `HITL_VALIDATOR` gate.
- `RE_DOMAIN_MODEL_SYNTHESIS_V1` — new `AI_AGGREGATOR`, `fast` tier, depends on both of the above. Its only job: output the JSON union of Structure + Events, reproducing the exact combined shape `RE_DOMAIN_MODELER_V1` used to emit directly (`{aggregates, domain_events, ddd_layer_assignment, inferred_items}`). No validator/HITL gate of its own — trivial deterministic-ish merge task, the two inputs are already human-approved.

**`RE_API_SCHEMA_DESIGNER_V1` → three agents:**
- `RE_API_TYPES_DESIGNER_V1` — component schemas (the nouns), applying field constraints from `RE_FIELD_DEFINITIONS_CATALOG_V1` per type. Own `AI_VALIDATOR` + `HITL_VALIDATOR` gate.
- `RE_API_ENDPOINTS_DESIGNER_V1` — paths: HTTP methods, security, pagination, rate limiting, cross-use-case data-continuity checks. Depends on `RE_API_TYPES_DESIGNER_V1` (references the now-settled types rather than defining them inline) + `RE_USE_CASE_ANALYST_V1` + `RE_CONTEXT_INTAKE_V1`. Own `AI_VALIDATOR` + `HITL_VALIDATOR` gate. This was the actual pair that regressed in production.
- `RE_API_SCHEMA_SYNTHESIS_V1` — new `AI_AGGREGATOR`, `fast` tier, depends on both of the above. Merges `components` (Types) with `paths` (Endpoints) into one valid OpenAPI document (`{openapi, info, paths, components}`), reproducing what `RE_API_SCHEMA_DESIGNER_V1` used to emit directly.

**Downstream engine rewiring (small):** `RE_FIELD_DEFINITIONS_CATALOG_V1`, `RE_NAMING_CONVENTION_ARCHITECT_V1`, `RE_DATABASE_SCHEMA_DESIGNER_V1`, `RE_TEST_STRATEGIST_V1` all currently depend on `RE_DOMAIN_MODELER_V1` — they repoint to `RE_DOMAIN_MODEL_SYNTHESIS_V1` instead. One agent-ID swap each, not two new dependency edges — the synthesis agents deliberately preserve the pre-split dependency shape for everything downstream of them.

### Why the synthesis-agent design over the cheaper-looking alternative

Considered and rejected: skip the synthesis agents, have the two consuming *skills* (below) fetch both halves and merge the JSON themselves. Rejected because that duplicates the exact same merge logic in two separate skill files (three counting the Desktop plugin mirror) that would then have to stay hand-synchronized forever as either split agent's output shape evolves. Two cheap `fast`-tier aggregator calls is a better trade than a permanent maintenance/drift liability in code outside this repo.

### Downstream SDLC impact (the reason this is cross-repo)

Swept every skill in `speakmanai-cc` for references to `RE_DOMAIN_MODELER_V1` / `RE_API_SCHEMA_DESIGNER_V1`. `promote-environment` and the JS/Go/Python coding-language variants: unaffected, no references. Two skills are affected, each duplicated in two places (live `.claude/skills/` copy + the manually-synced `desktop-skills/org-plugins/speakmanai-sdlc/` Desktop plugin mirror — not auto-mirrored):

- **`generate-requirements`** (`Step 4 — Download and Save Contracts`) — its agent-ID→filename table maps `RE_DOMAIN_MODELER_V1` → `domain_model.txt` and `RE_API_SCHEMA_DESIGNER_V1` → `api_schema.txt`. Needs exactly a one-line agent-ID swap per row (→ `RE_DOMAIN_MODEL_SYNTHESIS_V1` / `RE_API_SCHEMA_SYNTHESIS_V1`) — no new merge logic, since the synthesis agents already reproduce the same combined file shape.
- **`generate-speakmanai-code`** (Phase 3) — has its *own separate copy* of the same mapping (`Step`s under "Branch A — Local files on disk" / "Branch B — Fetch from SPEAKMAN.AI"). Branch B calls `get_output(session_id, agent_id)` **directly against a live MCP session**, bypassing the saved file — this path would break outright post-split (the old agent IDs stop existing) if not updated. Same one-line-per-row fix as above.
- **`generate-infrastructure`, `generate-uat-tests`, `generate-ux-design`** — read only the saved files (`domain_model.txt`, `api_schema.txt`), never the agent IDs directly. Zero changes needed, as long as the synthesis agents keep the file contract byte-shape-compatible (which is the whole point of the synthesis-agent design).

**Touches once built:**
- `speakmanai_lite`: `WorkflowsAndAgents/MCP_REQUIREMENTS_ENGINEERING_V1.json` (2 agents removed, 8 added, 4 downstream agents' `dependencies` updated). No engine code changes — this is pure workflow authoring, reusing existing `AI_WORKFLOW`/`AI_AGGREGATOR`/`AI_VALIDATOR`/`HITL_VALIDATOR` machinery.
- `speakmanai-cc`: `.claude/skills/generate-requirements/SKILL.md`, `.claude/skills/generate-speakmanai-code/SKILL.md`, and their two mirrors under `desktop-skills/org-plugins/speakmanai-sdlc/skills/` — one-line agent-ID rename each, 4 files total.

**Cost tradeoff:** 8 new agent docs replace 2 (6 real content/validator/HITL agents + 2 cheap fast-tier synthesis aggregators) — more calls in the happy path, but each is cheaper and the bet is fewer retries per gate now that each gate reviews one concern instead of a 40-50K character multi-concern document.

**Porting notes:** Not evaluated yet — depends on whether the enterprise version has the same monolithic-agent shape for its domain modeling / API schema equivalents.

---

## FIXED (2026-08-11): Use Case Analyst produces one UC per requirement instead of every UC a requirement implies

**Status:** Found and fixed same day, on an in-progress real build the user was running. Same failure family as the scenario-coverage gap above (a "cover the floor, not the ceiling" instruction pattern), but one level up — this is about how many *use cases* a requirement produces, not how many *scenarios* within one use case.

**What:** `RE_USE_CASE_ANALYST_V1`'s original instruction was literally "for every REQ-F-xxx functional requirement, produce **a** UC-XX use case" — a 1:1 mapping by construction. Real symptom the user hit repeatedly on a real project: a screen got built in the UX Design phase with no use case that ever led to it (an after-the-fact design fix); "no showing items on page load, just a search" (the list/browse-with-no-query use case was never generated, only search was). The user's own diagnosis, confirmed correct: the model likely considers the fuller set of plausible use cases during generation and collapses to a single representative one rather than being told not to.

**Fix applied:** `RE_USE_CASE_ANALYST_V1` rewritten to require enumerating every use case a requirement implies, with an explicit checklist to work through per requirement (access patterns — browse vs. search vs. filter vs. sort vs. navigate-in; CRUD completeness; screen-level states — empty vs. populated, first-time vs. returning; actor variations — differing scope/permissions) and an explicit "do not silently prune to the most representative one" instruction. `RE_USE_CASE_VALIDATOR_V1` gained a new check (independently re-deriving the expected use case set per requirement and flagging under-enumeration by name) inserted between the existing coverage and scenario-category checks.

**Touches:** `WorkflowsAndAgents/MCP_REQUIREMENTS_ENGINEERING_V1.json` — `RE_USE_CASE_ANALYST_V1` and `RE_USE_CASE_VALIDATOR_V1` systemPrompts only. No engine changes, live-imported into the DB.

**Porting notes:** Prompt-only fix, same portability profile as the other Use Case Analyst gap.

## ADDED (2026-08-12): `render_document` MCP tool — server-side HTML rendering, PDF/template system retired

**Status:** Built and verified this session, driven by the user porting `generate-sad`/`generate-compliance-report` to Microsoft Copilot Studio alongside the existing Claude Code deployment.

**What:** Both skills used to finish by shelling out locally to `preprocess.py` then `doc_writer.py` (Python scripts bundled as skill assets) to turn synthesized Markdown into branded HTML, then attempt a PDF via a Node `puppeteer` subprocess. That's a portability blocker for any client whose skill runtime can't shell out to Python/Node — Copilot Studio can't be assumed to have either, but it (like every other client) can already make MCP tool calls.

**Fix applied:** Moved the deterministic Markdown→HTML rendering into the MCP server as a new stateless tool, `render_document(markdown, title, subtitle) -> {"html": ...}`. Dropped PDF entirely (it was already a soft-fail local subprocess dependency; HTML is print-to-PDF-able from any browser) and dropped the template-directory system entirely (`templates/default/`, `templates/corporate/`, the `--template` flag) — the output is self-contained HTML with inline CSS, and a calling agent that wants custom branding just edits the returned HTML directly rather than needing a filesystem convention. This also de-duplicated `doc_writer.py`/`preprocess.py`/`templates/`, which were byte-identical copies living in both `generate-sad` and `generate-compliance-report`.

**Branding, revised twice this session:** the first version baked in the SPEAKMAN.AI logo as base64 — but the source PNG was an unoptimized 8364×919px asset (156KB) despite being displayed at 36px tall, adding ~52,000 tokens to every response. First pass: resized/palette-quantized it to 983×108px (7.6KB, ~2,500 tokens). Final pass, per direct user feedback: dropped the image entirely in favor of a plain CSS-styled `SPEAKMAN.AI` text wordmark — a full test document is now ~1,400 tokens total, down from ~57,000 at the start. `DEFAULT_LOGO_B64` is gone; a code comment in `document_renderer.py` documents how to reinstate an image logo if ever wanted.

**Touches:**
- New: `orchestration/mcp-server/document_renderer.py` (ported from `doc_writer.py` + `preprocess.py`, minus `generate_pdf()`/subprocess/argparse/CLI).
- `orchestration/mcp-server/engine.py` — new `render_document_local()`, stateless, no DB access.
- `orchestration/mcp-server/server.py` — new `render_document` tool, same annotation/delegation pattern as `generate_document`.
- `speakmanai.spec` — added `document_renderer.py` to `datas` (required: `server.py`/`engine.py` are loaded dynamically via `importlib.util.spec_from_file_location` at runtime, not normal PyInstaller import analysis, so any new sibling module must be explicitly bundled the same way `engine.py`/`database.py`/`database_sqlite.py` already are).
- `speakmancip/speakmanai-cc` — `generate-sad`/`generate-compliance-report` `SKILL.md` (3 locations each: `.claude/skills/`, `desktop-skills/`, `desktop-skills/org-plugins/speakmanai-sdlc/skills/`) updated to call `render_document` instead of local scripts; retired `scripts/doc_writer.py`, `scripts/preprocess.py`, `templates/` deleted from both skills and their org-plugin mirrors.

**Porting notes:** Verified `render_document_local` end-to-end against the live source (ToC correct, tables rendered), verified `document_renderer.py`/`server.py`/`engine.py` are present with correct content in the rebuilt `SpeakmanAI.exe` via `PyInstaller.archive.readers.CArchiveReader`, and confirmed the rebuilt exe starts and serves `/health`. **Confirmed live end-to-end in Microsoft Copilot Studio** (2026-08-12) — `generate-sad` run to completion through a real Copilot Studio agent, tunneled via VS Code port forwarding to the local server; see `speakmancip/speakmanai-cc`'s `desktop-skills/COPILOT_STUDIO.md` for the deployment specifics (connector setup, tunnel requirements, upload mechanics). Exe rebuilt a final time after the wordmark change and re-verified via `CArchiveReader` — bundled `document_renderer.py` confirmed to contain no `base64,` reference at all (16.5KB module, down from 221KB with the original logo).

## CHANGED (2026-10-07): Gemini 2.x retired, 3.x tier defaults, startup config migration

**Why:** Google is shutting down the Gemini 2.x models in October 2026. Any install still pointed at a 2.x model would start failing every LLM call.

**New Gemini/Vertex defaults:** fast `gemini-3.5-flash-lite`, standard `gemini-3.8-flash`, advanced `gemini-3.1-pro-preview`. `gemini-3.8-flash` is also offered in the fast and advanced dropdowns, since early testing suggested it can hold its own against 3.1 Pro for SAD work at lower cost.

**Touches:**
- `engine.py`: `MODEL_TIERS`, `_MODEL_TO_TIER` (2.0/2.5 entries removed, `gemini-3.5-flash-lite` and `gemini-3.8-flash` added), and the `DEFAULT_MODEL` fallbacks. The existing `startswith("gemini-3")` temperature skip already covers the new models.
- `server.py`: `_PROVIDER_MODELS` defaults and the setup page's `MODELS` dropdown lists (3.x only for `gemini` and `vertexai`).
- `launcher.py`: new `_migrate_retired_models()`. On startup, any `default_model`/`standard_model`/`advanced_model` in `~/.speakmanai/config.json` starting with `gemini-2` is replaced by the new default for that tier and the file is rewritten. Idempotent; a second run changes nothing.
- `.env.template`, `docker-compose.yml`, `README.md`, `SETUP.md`: defaults updated. Docker/source users' own `.env` files are not migrated.

**Not changed:** workflow JSON files only reference tier names (`fast`/`standard`/`advanced`), never model IDs, so no workflow needed editing.

## FIXED (2026-10-07): `update_session` cascade left downstream agents unchanged

**Symptom:** On a Tip App `MCP_SOLUTION_ARCHITECTURE_V1` session, an update to `MCP_TECHNICAL_SOLUTION_ARCHITECT_V1` (signed QR payloads, CCPA deletion module) cascaded to `MCP_COMPLIANCE_OFFICER_V2`, which returned output byte-identical to its pre-update version.

**Diagnosis (from the session's event log, not assumed):** the dependency context was correct. The compliance agent's `CONTEXT FROM MCP_TECHNICAL_SOLUTION_ARCHITECT_V1` held the latest revised content, and ordering was fine. The "always fetch latest" read path is not the problem; don't re-investigate it. The cause was the cascade note `_emit_advance_event` seeded:
- It named `agent_id`, the step that had just run (Visualization), not the agent the update started from. Compliance doesn't depend on Visualization, and Visualization's output hadn't changed.
- It never included the change request.
- It ended "otherwise leave your output unchanged", and `_handle_process_step` prefixed `PRESERVE_INSTRUCTION` ("change only what the feedback actually calls out").

**Fix:** new `_latest_update_request(events)` reads the origin agent and request off the most recent `update_retry` event. The seeded note names that origin and includes the request, explicitly scoped ("that request, including any 'change nothing else' wording, was addressed to that agent, not to you"), then tells the agent its previous output was written against the old content. The seed also sets `update_origin_agent_id`, and `_handle_process_step` uses it to swap `PRESERVE_INSTRUCTION` for a downstream-specific instruction. The directly targeted agent keeps `PRESERVE_INSTRUCTION`. The delegate re-prompt in `server.py` needed no change.

**Verified:** live re-run by the user on 2026-10-08; the downstream output changed. Commit `07d373f`. The hosted platform has the identical bug in `cascade_helpers.py`; a port is written up in `speakman_generic/LITE_PARITY_HANDOFF.md`.

## PORTED (2026-10-08): three fixes back from the hosted platform (`speakman_generic`)

- **`document_renderer.py` f-string backslash** (enterprise `59a7e03`): `re.sub(r"^\d+\.\s", ...)` inside an f-string's `{...}` is a SyntaxError before Python 3.12. The Docker image is `python:3.11-slim`, so `render_document` failed there; the exe's 3.12 masked it. The import is lazy, so the container still started. Compile checks for this class of bug need a pre-3.12 interpreter (`py -3.8 -m py_compile` works locally).
- **`submit_response` status race** (enterprise `916870f`, strengthened here): the old code queued the resume and then wrote `IN_PROGRESS` unconditionally. Enterprise made that trailing write conditional on `AWAITING_INPUT`, but that still loses when the resume reaches the next delegate pause first (status is `AWAITING_INPUT` again, so the write matches and poll_workflow sticks on `IN_PROGRESS`). Lite now claims the pause atomically (`update_one_if`, AWAITING_INPUT → IN_PROGRESS) before queueing, and rejects the submit if the claim fails. Safe because neither `_handle_resume` nor `_handle_hitl_response` checks status on arrival, and every MCP_PAUSE event carries `status: AWAITING_INPUT` (all 165 in the local DB checked).
- **HITL prior-feedback lookup scoped to its own step** (enterprise `0957ce8`): `_get_mcp_pause_config`'s `hitl_loop > 0` scan now also matches `current_step_index`. Hardening only: lite runs a session sequentially, and no path was found where another agent's `hitl_retry` could be the most recent one.

**Not ported, and why:** Pub/Sub idempotency and out-of-order status guards (lite uses an in-process queue), the validator-resolution retry loop (lite catches broadly and is bounded by `maxLoops`), and all tenant, BYOK and signup/payment work. Per-agent `executionMode: "delegate"` stays lite-only by design.
