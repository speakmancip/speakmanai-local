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
