import os
import json
import uuid
import logging
import re
from datetime import datetime, timezone
import asyncio
import httpx

from database import get_db

log = logging.getLogger(__name__)

def _extract_json(text: str):
    """Parse JSON from LLM output, tolerating trailing prose after the closing brace."""
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        if "Extra data" in str(e):
            return json.loads(text[:e.pos])
        raise

# Max output tokens per Anthropic model — Haiku is capped at 8192 by the API.
_ANTHROPIC_MAX_TOKENS: dict[str, int] = {
    "claude-haiku-4-5-20251001": 8192,
    "claude-haiku-4-5":          8192,
}

# ─────────────────────────────────────────────
# LLM Provider Configuration — read dynamically from os.environ at call time
# so that config changes via /api/config take effect without restart.
# ─────────────────────────────────────────────

def _cfg():
    """Return current provider config from environment (live — no restart needed)."""
    return {
        "provider":        os.environ.get("LLM_PROVIDER", "gemini").lower(),
        "default_model":   os.environ.get("DEFAULT_MODEL", "gemini-2.5-flash-lite"),
        "standard_model":  os.environ.get("STANDARD_MODEL", ""),
        "advanced_model":  os.environ.get("ADVANCED_MODEL", ""),
        "ollama_url":      os.environ.get("OLLAMA_BASE_URL", "http://host.docker.internal:11434"),
        "ollama_fallback": os.environ.get("OLLAMA_FALLBACK_MODEL", "llama3"),
        "gemini_key":      os.environ.get("GEMINI_API_KEY"),
        "anthropic_key":   os.environ.get("ANTHROPIC_API_KEY"),
        "openai_key":      os.environ.get("OPENAI_API_KEY"),
        "gcp_project":     os.environ.get("GCP_PROJECT_ID"),
        "gcp_region":      os.environ.get("GCP_REGION", "us-west1"),
    }

# Keep module-level aliases for any code that references them directly (backwards compat)
# These reflect startup values only — use _cfg() inside functions for live values.
LLM_PROVIDER        = os.environ.get("LLM_PROVIDER", "gemini").lower()
DEFAULT_MODEL       = os.environ.get("DEFAULT_MODEL", "gemini-2.5-flash-lite")
STANDARD_MODEL      = os.environ.get("STANDARD_MODEL", "")
ADVANCED_MODEL      = os.environ.get("ADVANCED_MODEL", "")
OLLAMA_BASE_URL     = os.environ.get("OLLAMA_BASE_URL", "http://host.docker.internal:11434")
OLLAMA_FALLBACK_MODEL = os.environ.get("OLLAMA_FALLBACK_MODEL", "llama3")

# ── Abstract model tier → provider model mapping ──────────────────────────
MODEL_TIERS = {
    "gemini":    {"fast": "gemini-2.5-flash-lite", "standard": "gemini-2.5-flash", "advanced": "gemini-2.5-pro"},
    "vertexai":  {"fast": "gemini-2.5-flash-lite", "standard": "gemini-2.5-flash", "advanced": "gemini-2.5-pro"},
    "anthropic": {"fast": "claude-haiku-4-5-20251001", "standard": "claude-sonnet-4-6", "advanced": "claude-opus-4-8"},
    "openai":    {"fast": "gpt-4o-mini",       "standard": "gpt-4o",           "advanced": "gpt-4o"},
    "ollama":    {"fast": None,                 "standard": None,               "advanced": None},
}

# Reverse map: known model name → tier (used for cross-provider conflict resolution)
_MODEL_TO_TIER = {
    "gemini-2.5-pro": "advanced",   "gemini-2.5-flash": "standard",
    "gemini-2.5-flash-lite": "fast",
    "gemini-2.0-pro": "advanced",   "gemini-2.0-flash": "fast",
    "gemini-3.1-pro-preview": "advanced", "gemini-3-flash-preview": "standard",
    "gemini-3.1-flash-lite": "fast",
    "claude-opus-4-8": "advanced",  "claude-sonnet-4-6": "standard",
    "claude-haiku-4-5-20251001": "fast", "claude-haiku-4-5": "fast",
    "claude-sonnet-5": "standard",  "claude-fable-5": "advanced",
    "gpt-4o": "standard",           "gpt-4o-mini": "fast",
    "o1": "advanced",               "o3-mini": "fast",
}

# ── Lazy client cache — keyed by (provider, api_key) so re-keying on config change ──
_client_cache: dict = {}

def _get_llm_client(provider: str, cfg: dict):
    """Return a cached LLM client, rebuilding if the API key has changed."""
    if provider == "gemini":
        key = ("gemini", cfg["gemini_key"] or cfg["gcp_project"])
        if key not in _client_cache:
            try:
                from google import genai
                if cfg["gemini_key"]:
                    _client_cache[key] = ("gemini", genai.Client(api_key=cfg["gemini_key"]))
                elif cfg["gcp_project"]:
                    _client_cache[key] = ("gemini", genai.Client(vertexai=True, project=cfg["gcp_project"], location=cfg["gcp_region"]))
                else:
                    raise RuntimeError("Set GEMINI_API_KEY or GCP_PROJECT_ID.")
            except ImportError:
                raise RuntimeError("google-genai not installed.")
        return _client_cache[key][1]
    elif provider == "anthropic":
        key = ("anthropic", cfg["anthropic_key"])
        if key not in _client_cache:
            try:
                from anthropic import AsyncAnthropic
                if not cfg["anthropic_key"]:
                    raise RuntimeError("Set ANTHROPIC_API_KEY.")
                _client_cache[key] = ("anthropic", AsyncAnthropic(api_key=cfg["anthropic_key"]))
            except ImportError:
                raise RuntimeError("anthropic package not installed.")
        return _client_cache[key][1]
    elif provider == "openai":
        key = ("openai", cfg["openai_key"])
        if key not in _client_cache:
            try:
                from openai import AsyncOpenAI
                if not cfg["openai_key"]:
                    raise RuntimeError("Set OPENAI_API_KEY.")
                _client_cache[key] = ("openai", AsyncOpenAI(api_key=cfg["openai_key"]))
            except ImportError:
                raise RuntimeError("openai package not installed.")
        return _client_cache[key][1]
    return None

# Keep top-level client alias for legacy references
client = None

DEFAULT_PLANNER_SYSTEM_PROMPT = """You are a Workflow Planner. Your function is to receive a raw user request and a predefined list of agents, and from these, construct a JSON object defining the execution steps.
Based on the USER_PROMPT and the list of AVAILABLE_AGENTS provided, you MUST generate a single, valid JSON object.
Do not include the word "json", markdown backticks, or any explanation.

step_type Rules — set step_type for each step based on the agent's Type field:
- "AI"        — AI_WORKFLOW agents
- "AGGREGATE" — AI_AGGREGATOR agents (parallel-branch workflows ONLY)
- "MCP_PAUSE" — MCP_* agents (pause the workflow and wait for human/client input)

JSON Structure Requirement:
{
  "workflow_definition": {
    "steps": [
      { "step_index": 0, "step_type": "AI", "agents": ["AGENT_1"], "dependencies": [] },
      { "step_index": 1, "step_type": "MCP_PAUSE", "agents": ["MCP_AGENT"], "dependencies": [0] }
    ]
  }
}"""


def _resolve_model(model_name: str) -> tuple[str, str]:
    """
    Resolve an agent model string to (active_provider, bare_model_name).
    Reads provider config live from os.environ so changes via /api/config take effect immediately.
    """
    cfg = _cfg()
    provider    = cfg["provider"]
    default     = cfg["default_model"]
    standard    = cfg["standard_model"]
    advanced    = cfg["advanced_model"]
    ollama_fb   = cfg["ollama_fallback"]

    if not model_name:
        return provider, default

    # Abstract tier
    if model_name in ("fast", "standard", "advanced"):
        tiers = MODEL_TIERS.get(provider, {})
        if model_name == "fast" and default:
            # Validate default_model belongs to the active provider
            fast_provider = next(
                (p for p, t in MODEL_TIERS.items() if default in t.values()), None
            )
            if fast_provider is None or fast_provider == provider:
                return provider, default
            # Mismatch — fall through to tier lookup
        if model_name == "standard" and standard:
            # Validate standard_model belongs to the active provider
            std_provider = next(
                (p for p, t in MODEL_TIERS.items() if standard in t.values()), None
            )
            if std_provider is None or std_provider == provider:
                return provider, standard
            # Mismatch — fall through to tier lookup
        if model_name == "advanced" and advanced:
            # Validate advanced_model belongs to the active provider
            adv_provider = next(
                (p for p, t in MODEL_TIERS.items() if advanced in t.values()), None
            )
            if adv_provider is None or adv_provider == provider:
                return provider, advanced
            # Mismatch — fall through to tier lookup
        resolved = tiers.get(model_name) or default
        return provider, resolved

    # Provider-prefixed model
    if "/" in model_name:
        prefix, bare = model_name.split("/", 1)
        prefix = prefix.lower()
        provider_map = {
            "gemini": "gemini", "vertex": "gemini", "vertexai": "gemini",
            "anthropic": "anthropic", "claude": "anthropic",
            "openai": "openai", "ollama": "ollama",
        }
        declared = provider_map.get(prefix)
        if declared and declared != provider:
            # Cross-provider conflict — map to equivalent tier on active provider
            tier = _MODEL_TO_TIER.get(bare, "standard")
            tiers = MODEL_TIERS.get(provider, {})
            resolved = advanced if (tier == "advanced" and advanced) else (tiers.get(tier) or default)
            return provider, resolved
        if declared:
            return declared, bare
        return provider, model_name

    # Bare model name — use as-is, but guard Ollama against cloud model names
    if provider == "ollama" and any(x in model_name for x in ("gemini", "claude", "gpt", "grok")):
        return "ollama", ollama_fb or default

    return provider, model_name


async def _call_llm(system_prompt: str, user_content: str, model_name: str, mime_type: str, temperature: float = 0.0) -> str:
    """Unified LLM caller: routes to Gemini/Vertex, Anthropic, OpenAI, or Ollama.
    Reads config live from os.environ — no restart needed after /api/config changes."""

    cfg = _cfg()
    active_provider, model_name = _resolve_model(model_name)
    log.info(f"_call_llm provider={active_provider} model={model_name}")

    # ── Ollama ────────────────────────────────────────────────────────────────
    if active_provider == "ollama":
        payload = {
            "model": model_name,
            "system": system_prompt,
            "prompt": user_content,
            "stream": False,
            "options": {"temperature": temperature, "num_ctx": 32768},
        }
        async with httpx.AsyncClient(timeout=300.0) as http:
            resp = await http.post(f"{cfg['ollama_url']}/api/generate", json=payload)
            resp.raise_for_status()
            result_text = resp.json().get("response", "")

    # ── Anthropic ─────────────────────────────────────────────────────────────
    elif active_provider == "anthropic":
        client = _get_llm_client("anthropic", cfg)
        async with client.messages.stream(
            model=model_name,
            max_tokens=_ANTHROPIC_MAX_TOKENS.get(model_name, 64000),
            system=system_prompt,
            messages=[{"role": "user", "content": user_content}],
        ) as stream:
            result_text = await stream.get_final_text()

    # ── OpenAI ────────────────────────────────────────────────────────────────
    elif active_provider == "openai":
        client = _get_llm_client("openai", cfg)
        response = await client.chat.completions.create(
            model=model_name,
            temperature=temperature,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_content},
            ],
        )
        result_text = response.choices[0].message.content or ""

    # ── Gemini / Vertex AI ────────────────────────────────────────────────────
    else:
        client = _get_llm_client("gemini", cfg)
        try:
            from google.genai import types as _genai_types
        except ImportError:
            raise RuntimeError("google-genai not installed.")
        _gemini_config_kwargs = {
            "system_instruction": system_prompt,
            "response_mime_type": mime_type,
        }
        if not model_name.startswith("gemini-3"):
            _gemini_config_kwargs["temperature"] = temperature
        response = await client.aio.models.generate_content(
            model=model_name,
            contents=user_content,
            config=_genai_types.GenerateContentConfig(**_gemini_config_kwargs),
        )
        result_text = response.text

    log.info(f"--- RAW LLM RESPONSE START ---\n{result_text}\n--- RAW LLM RESPONSE END ---")

    if result_text:
        # Strip reasoning <think> blocks
        result_text = re.sub(r'<think>.*?</think>', '', result_text, flags=re.DOTALL).strip()
        # Strip markdown fencing for JSON responses
        if mime_type == "application/json":
            if result_text.startswith("```json"):
                result_text = result_text[7:]
            elif result_text.startswith("```"):
                result_text = result_text[3:]
            if result_text.endswith("```"):
                result_text = result_text[:-3]
            result_text = result_text.strip()

    return result_text

async def _log_event_to_db(session_id: str, event: dict, db):
    """
    Appends events to the session and promotes top-level fields (status, title, owner)
    to ensure the MCP UI state is accurate.
    """
    attrs = event.get("attributes", {})
    event_type = attrs.get("event_type", "WORKFLOW")
    status = attrs.get("status", "UNKNOWN")
    
    update_op = {
        "$push": {"events": event},
        "$set": {"updated_at": datetime.now(timezone.utc)},
        "$setOnInsert": {
            "session_id": session_id,
            "created_at": datetime.now(timezone.utc),
        }
    }
    
    # VALIDATION events shouldn't overwrite the main workflow status
    if event_type != "VALIDATION":
        update_op["$set"]["current_status"] = status
        
    if "owner_id" in attrs:
        update_op["$set"]["owner_id"] = attrs["owner_id"]
    if "session_title" in attrs:
        update_op["$set"]["session_title"] = attrs["session_title"]
        
    await db["events_raw"].update_one({"session_id": session_id}, update_op, upsert=True)


async def build_project_record(session_id: str, db):
    """Assembles a clean project record when a workflow reaches COMPLETED."""
    doc = await db["events_raw"].find_one({"session_id": session_id})
    if not doc: return
    
    events = doc.get("events", [])
    if not events: return
    
    # Get real agent types from DB for all agents present in events
    agent_ids = list(set(
        e.get("data", {}).get("execution_context", {}).get("source_outputs", {}).get("source_agent_id", "")
        for e in events
    ))
    agent_ids = [a for a in agent_ids if a and not a.startswith("root_planner")]
    
    agent_docs = await db["agents"].find({"agentId": {"$in": agent_ids}}).to_list(length=None)
    agent_type_map = {a["agentId"]: a.get("agentType", "") for a in agent_docs}
    
    # Keyed by agent_id, last-write-wins: an agent under a HITL_VALIDATOR gate logs one
    # event per review loop (each pause carries that attempt's content) plus one final
    # event on approval. Without dedup, every intermediate/superseded attempt would show
    # up as its own section alongside the final one. A dict (not a list) guarantees only
    # the last — i.e. final approved — content per agent survives, mirroring how
    # _aggregate_agent_index already dedups for get_output/poll_workflow.
    sections_by_agent = {}
    for event in events:
        attrs = event.get("attributes", {})
        if attrs.get("event_type") == "VALIDATION" or attrs.get("validation_retry") == "true" or attrs.get("hitl_retry") == "true" or attrs.get("update_retry") == "true":
            continue
        src = event.get("data", {}).get("execution_context", {}).get("source_outputs", {})
        agent_id = src.get("source_agent_id")

        # Filter out HITL pause steps and the planner from the final project document based on true agentType
        if agent_id and not agent_id.startswith("root_planner"):
            a_type = agent_type_map.get(agent_id, "")
            if a_type != "AI_PLANNER" and (a_type == "MCP_LLM_DELEGATE" or not a_type.startswith("MCP_")):
                sections_by_agent[agent_id] = {
                    "agent_id": agent_id,
                    "content": src.get("content", ""),
                    "title": agent_id.replace("_", " ").title()
                }
    sections = list(sections_by_agent.values())

    project_doc = {
        "session_id": session_id,
        "owner_id": "local_user",
        "title": doc.get("session_title", "Local Project"),
        "status": "COMPLETED",
        "outputs": {"sections": sections, "document": None, "items_json": None}
    }
    await db["projects"].update_one({"session_id": session_id}, {"$set": project_doc}, upsert=True)


def _compile_dependencies_context(agent_config: dict, events: list) -> str:
    """Compiles a unified context block based on the agent's specific dependencies."""
    dependencies = agent_config.get("dependencies", []) if agent_config else []
    latest_outputs = {}
    first_output = ""
    for event in events:
        attrs = event.get("attributes", {})
        if attrs.get("event_type") == "VALIDATION" or attrs.get("validation_retry") == "true" or attrs.get("hitl_retry") == "true" or attrs.get("update_retry") == "true":
            continue
        src = event.get("data", {}).get("execution_context", {}).get("source_outputs", {})
        src_agent = src.get("source_agent_id")
        content = src.get("content")
        if src_agent and content:
            latest_outputs[src_agent] = content
            if not first_output:
                first_output = content
            
    if not dependencies:
        return first_output
        
    compiled_parts = []
    get_all = "*" in dependencies
    if get_all:
        for agent, content in latest_outputs.items():
            if not agent.startswith("root_planner"):
                compiled_parts.append(f"# CONTEXT FROM {agent}:\n{content}")
    else:
        for dep in dependencies:
            if dep in latest_outputs:
                compiled_parts.append(f"# CONTEXT FROM {dep}:\n{latest_outputs[dep]}")
                
    return "\n\n".join(compiled_parts) if compiled_parts else ""


def resolve_agent_step_index(workflow_def: dict, agent_id: str):
    """List position of the step containing agent_id — matches the steps[i] positional
    indexing already assumed throughout _handle_process_step/_emit_advance_event."""
    for i, step in enumerate(workflow_def.get("steps", [])):
        if agent_id in step.get("agents", []):
            return i
    return None


def _latest_output_for_agent(agent_id: str, events: list) -> str:
    """Same last-write-wins scan as _compile_dependencies_context, narrowed to one agent —
    seeds the revise-in-place previous_output baseline for update_session."""
    latest = ""
    for event in events:
        attrs = event.get("attributes", {})
        if (attrs.get("event_type") == "VALIDATION" or attrs.get("validation_retry") == "true"
                or attrs.get("hitl_retry") == "true" or attrs.get("update_retry") == "true"):
            continue
        src = event.get("data", {}).get("execution_context", {}).get("source_outputs", {})
        if src.get("source_agent_id") == agent_id and src.get("content"):
            latest = src["content"]
    return latest


async def _resolve_downstream_agents(workflow_id: str, agent_id: str, db) -> set:
    """Fixed-point walk of the dependencies graph declared on each agent's DB doc — everything
    transitively depending on agent_id, direct or indirect. Wildcard ("*") dependents
    (AI_AGGREGATOR-style) are always included conservatively, since they ingest every prior
    output. AI_PLANNER/AI_VALIDATOR/HITL_VALIDATOR agent docs are excluded — they're gate
    configs, not workflow_def.steps entries (mirrors _handle_start's workflow_agents filter)."""
    agents = await db["agents"].find({"workflows": workflow_id}).to_list(length=None)
    agents = [a for a in agents if a.get("agentType") not in ("AI_PLANNER", "AI_VALIDATOR", "HITL_VALIDATOR")]
    downstream, seen, changed = set(), {agent_id}, True
    while changed:
        changed = False
        for a in agents:
            aid = a.get("agentId")
            if not aid or aid in seen:
                continue
            deps = a.get("dependencies", []) or []
            if "*" in deps or any(d in seen for d in deps):
                downstream.add(aid)
                seen.add(aid)
                changed = True
    return downstream


def _extract_cascade(latest_event: dict):
    """Reads update-cascade bookkeeping off an event's attributes, if this event is part of an
    in-flight update_session cascade — threaded through _maybe_gate_by_hitl/_emit_advance_event
    (and the validate_step/hitl_response retry paths) so cascade steps are correctly skipped
    and the marker keeps propagating each hop."""
    attrs = latest_event.get("attributes", {})
    raw = attrs.get("update_cascade_steps")
    if not raw:
        return None
    return {
        "steps": [int(x) for x in raw.split(",") if x != ""],
        "loop": int(attrs.get("update_loop", "0")),
    }


async def transition_completed_or_cancelled_to_draft(session_id: str, db) -> bool:
    """Atomically transitions a session from COMPLETED/CANCELLED to DRAFT. This single
    conditional update IS the idempotency/state-restriction guardrail for update_session — only
    one of two racing callers can ever see it return True for the same session, so there is no
    check-then-write gap to race. SQLite mode uses the dedicated update_one_if primitive;
    Mongo mode's own update_one is already atomic when the expected state is in the filter."""
    coll = db["events_raw"]
    if hasattr(coll, "update_one_if"):
        return await coll.update_one_if(
            {"session_id": session_id}, {"$set": {"current_status": "DRAFT"}},
            expected_field="current_status", expected_values=["COMPLETED", "CANCELLED"],
        )
    result = await coll.update_one(
        {"session_id": session_id, "current_status": {"$in": ["COMPLETED", "CANCELLED"]}},
        {"$set": {"current_status": "DRAFT"}},
    )
    return getattr(result, "matched_count", 0) > 0


async def _emit_advance_event(session_id: str, db, queue: asyncio.Queue, workflow_def: dict, current_step_idx: int, agent_id: str, content: str, cascade: dict = None, hitl_cap_reached: bool = False):
    """Advances the workflow past current_step_idx using `content` as the accepted output of
    agent_id, or marks the session COMPLETED if this was the final step. Shared by the plain
    AI-step path, the delegate-resume path, and the AI_VALIDATOR/HITL_VALIDATOR PASS paths —
    'what happens once a step's output is accepted' lives in exactly one place.

    When `cascade` is set (an update_session revision in progress), advancement jumps straight
    to the next AFFECTED step in cascade['steps'] instead of current_step_idx + 1 — every step
    update_session determined is unaffected by the change is left untouched. The advancing event
    also seeds the next affected agent's own prior output + a note that its upstream context
    changed, so it revises in place on its next run instead of regenerating from scratch."""
    steps = workflow_def.get("steps", [])
    if cascade:
        remaining = [i for i in cascade.get("steps", []) if i > current_step_idx]
        next_step_idx = min(remaining) if remaining else len(steps)
    else:
        next_step_idx = current_step_idx + 1
    is_final = next_step_idx >= len(steps)
    next_step_type = steps[next_step_idx]["step_type"] if not is_final else None
    status = "COMPLETED" if is_final else ("AWAITING_INPUT" if next_step_type == "MCP_PAUSE" else "IN_PROGRESS")

    attrs = {
        "session_id": session_id, "owner_id": "local_user",
        "current_step_index": str(next_step_idx) if not is_final else str(current_step_idx),
        "status": status,
        "event_type": "MCP_PAUSE" if status == "AWAITING_INPUT" else "WORKFLOW"
    }
    if hitl_cap_reached:
        attrs["hitl_cap_reached"] = "true"
    exec_ctx = {"source_outputs": {"source_agent_id": agent_id, "content": content}}

    if cascade and not is_final:
        attrs["update_loop"] = str(cascade.get("loop", 0))
        attrs["update_cascade_steps"] = ",".join(str(i) for i in cascade.get("steps", []))
        next_agents = steps[next_step_idx].get("agents", [])
        if next_agents:
            next_agent_id = next_agents[0]
            fresh_doc = await db["events_raw"].find_one({"session_id": session_id})
            fresh_events = fresh_doc.get("events", []) if fresh_doc else []
            exec_ctx["previous_output"] = _latest_output_for_agent(next_agent_id, fresh_events)
            exec_ctx["update_feedback"] = (
                f"Upstream agent '{agent_id}' was just revised as part of a project update. "
                "Re-check your own output against its new content and adjust anything that's "
                "no longer consistent with it — otherwise leave your output unchanged."
            )

    new_event = {
        "event_id": str(uuid.uuid4()), "publish_time": datetime.now(timezone.utc).isoformat(),
        "attributes": attrs,
        "data": {"workflow_definition": workflow_def, "execution_context": exec_ctx}
    }
    await _log_event_to_db(session_id, new_event, db)

    if status == "COMPLETED":
        await build_project_record(session_id, db)
    if status == "IN_PROGRESS":
        await queue.put({"action": "process_step", "session_id": session_id})


async def _maybe_gate_by_hitl(session_id: str, db, queue: asyncio.Queue, workflow_def: dict, current_step_idx: int, agent_id: str, content: str, hitl_loop: int = 0, cascade: dict = None):
    """Checks whether agent_id declares a humanFeedbackId. If so, pauses the workflow for
    human review instead of advancing (current_step_index stays pinned to agent_id's step,
    exactly like an AI_VALIDATOR retry does). Otherwise advances normally via _emit_advance_event.
    Called after any AI_VALIDATOR gate has already passed (or wasn't configured) — this is what
    implements 'AI validator first, then HITL' when an agent declares both."""
    agent_config = await db["agents"].find_one({"agentId": agent_id})
    hitl_agent_id = agent_config.get("humanFeedbackId") if agent_config else None

    if not hitl_agent_id:
        await _emit_advance_event(session_id, db, queue, workflow_def, current_step_idx, agent_id, content, cascade=cascade)
        return

    max_loops = int((agent_config or {}).get("humanFeedbackConfig", {}).get("maxLoops", 3))
    if hitl_loop >= max_loops:
        # Loop budget exhausted. This content is the FRESH regeneration from the human's final
        # feedback round (not stale pre-round content) — advance with it rather than re-pausing
        # for a review round that would never resolve. hitl_cap_reached is a discoverable marker
        # in the event log for anyone auditing why this agent didn't get a final human sign-off.
        log.warning(f"[{session_id}] Agent {agent_id}: HITL loop budget ({max_loops}) exhausted. Advancing with the latest regenerated content instead of pausing again.")
        await _emit_advance_event(session_id, db, queue, workflow_def, current_step_idx, agent_id, content, cascade=cascade, hitl_cap_reached=True)
        return

    log.info(f"[{session_id}] Agent {agent_id} requires human review by {hitl_agent_id}. Pausing (loop {hitl_loop})...")

    pause_attrs = {
        "session_id": session_id, "owner_id": "local_user",
        "current_step_index": str(current_step_idx),
        "status": "AWAITING_INPUT",
        "event_type": "MCP_PAUSE",
        "hitl_pending_agent_id": hitl_agent_id,
        "hitl_loop": str(hitl_loop),
    }
    if cascade:
        pause_attrs["update_loop"] = str(cascade.get("loop", 0))
        pause_attrs["update_cascade_steps"] = ",".join(str(i) for i in cascade.get("steps", []))

    pause_event = {
        "event_id": str(uuid.uuid4()), "publish_time": datetime.now(timezone.utc).isoformat(),
        "attributes": pause_attrs,
        "data": {
            "workflow_definition": workflow_def,
            "execution_context": {"source_outputs": {"source_agent_id": agent_id, "content": content}}
        }
    }
    await _log_event_to_db(session_id, pause_event, db)


async def _handle_hitl_response(event: dict, queue: asyncio.Queue):
    """Resumes a workflow paused at a HITL_VALIDATOR gate. 'Approved' advances past the origin
    agent's step (mirroring AI_VALIDATOR's PASS branch); 'Update' re-runs the origin agent with
    the human's feedback injected and loops back to the same gate (mirroring the FAIL branch),
    up to the origin agent's humanFeedbackConfig.maxLoops."""
    session_id = event["session_id"]
    origin_agent_id = event["origin_agent_id"]
    current_step_idx = event["current_step_index"]
    raw_response = event["response"]

    db = get_db(os.environ.get("RAW_EVENTS_DB_NAME", "speakmanai_db"))
    session_doc = await db["events_raw"].find_one({"session_id": session_id})
    if not session_doc:
        log.error(f"[{session_id}] Cannot resume HITL gate: session not found.")
        return

    events = session_doc.get("events", [])
    workflow_def = events[0]["data"]["workflow_definition"]
    steps = workflow_def.get("steps", [])

    latest_event = events[-1]
    hitl_loop = int(latest_event.get("attributes", {}).get("hitl_loop", "0"))
    cascade = _extract_cascade(latest_event)
    origin_content = latest_event["data"]["execution_context"]["source_outputs"]["content"]

    try:
        parsed = _extract_json(raw_response.strip())
        status = parsed.get("status", "")
        feedback = parsed.get("feedback", "")
    except Exception:
        # Tolerate a bare "Approved" / "Update" string in addition to structured JSON.
        status = "Approved" if "approve" in raw_response.strip().lower() else "Update"
        feedback = raw_response

    if status == "Approved":
        await _emit_advance_event(session_id, db, queue, workflow_def, current_step_idx, origin_agent_id, origin_content, cascade=cascade)
        return

    # "Update" — retry the origin agent with the human's feedback injected, even on what may be
    # the final permitted loop: the regeneration still runs, so feedback is never silently
    # discarded. _maybe_gate_by_hitl (reached once this regenerates, after any validator) is the
    # sole place that caps how many times the gate re-pauses — once the cap is hit it
    # force-advances with the FRESH regenerated content, not stale pre-round content.
    origin_step_type = steps[current_step_idx]["step_type"] if current_step_idx < len(steps) else None
    is_delegated_step = origin_step_type == "MCP_PAUSE"
    retry_status = "AWAITING_INPUT" if is_delegated_step else "IN_PROGRESS"

    retry_attrs = {
        "session_id": session_id, "owner_id": "local_user", "current_step_index": str(current_step_idx),
        "status": retry_status,
        "event_type": "MCP_PAUSE" if is_delegated_step else "WORKFLOW",
        "hitl_retry": "true", "hitl_loop": str(hitl_loop + 1),
    }
    if cascade:
        retry_attrs["update_loop"] = str(cascade.get("loop", 0))
        retry_attrs["update_cascade_steps"] = ",".join(str(i) for i in cascade.get("steps", []))

    retry_event = {
        "event_id": str(uuid.uuid4()), "publish_time": datetime.now(timezone.utc).isoformat(),
        "attributes": retry_attrs,
        "data": {
            "workflow_definition": workflow_def,
            "execution_context": {
                "source_outputs": {"source_agent_id": origin_agent_id, "content": origin_content},
                "previous_output": origin_content,
                "hitl_feedback": feedback,
            }
        }
    }
    await _log_event_to_db(session_id, retry_event, db)
    if not is_delegated_step:
        await queue.put({"action": "process_step", "session_id": session_id})


async def _handle_update_session(event: dict, queue: asyncio.Queue):
    """Starts a revise-in-place update cascade against an already-completed session (the
    update_session MCP tool has already atomically transitioned it COMPLETED/CANCELLED -> DRAFT
    before this ever gets enqueued). Writes a self-loop retry event for the targeted agent
    (marked update_retry, excluded from last-write-wins the same way hitl_retry/validation_retry
    already are) carrying its own prior output + the requested change, then either re-enqueues
    process_step (background agent) or leaves it paused as MCP_PAUSE/AWAITING_INPUT (delegate
    agent) — the same delegate-vs-background dispatch _handle_hitl_response's Update branch
    already uses."""
    session_id = event["session_id"]
    agent_id = event["agent_id"]
    update_content = event["update_content"]

    db = get_db(os.environ.get("RAW_EVENTS_DB_NAME", "speakmanai_db"))
    session_doc = await db["events_raw"].find_one({"session_id": session_id})
    if not session_doc:
        log.error(f"[{session_id}] Cannot start update cascade: session not found.")
        return

    events = session_doc.get("events", [])
    if not events:
        log.error(f"[{session_id}] Cannot start update cascade: session has no events.")
        await db["events_raw"].update_one({"session_id": session_id}, {"$set": {"current_status": "COMPLETED"}})
        return

    workflow_def = events[0]["data"]["workflow_definition"]
    workflow_id = events[0]["attributes"].get("workflow_id")
    steps = workflow_def.get("steps", [])

    entry_step_idx = resolve_agent_step_index(workflow_def, agent_id)
    if entry_step_idx is None:
        log.error(f"[{session_id}] update_session: agent {agent_id} not found in workflow_definition.")
        await db["events_raw"].update_one({"session_id": session_id}, {"$set": {"current_status": "COMPLETED"}})
        return

    downstream = await _resolve_downstream_agents(workflow_id, agent_id, db)
    affected_step_indices = sorted({entry_step_idx} | {
        i for i, step in enumerate(steps) if any(a in downstream for a in step.get("agents", []))
    })
    previous_output = _latest_output_for_agent(agent_id, events)

    is_delegated_step = steps[entry_step_idx]["step_type"] == "MCP_PAUSE"
    retry_status = "AWAITING_INPUT" if is_delegated_step else "IN_PROGRESS"
    retry_event = {
        "event_id": str(uuid.uuid4()), "publish_time": datetime.now(timezone.utc).isoformat(),
        "attributes": {
            "session_id": session_id, "owner_id": "local_user",
            "current_step_index": str(entry_step_idx), "status": retry_status,
            "event_type": "MCP_PAUSE" if is_delegated_step else "WORKFLOW",
            "update_retry": "true", "update_loop": "1",
            "update_cascade_steps": ",".join(str(i) for i in affected_step_indices),
        },
        "data": {
            "workflow_definition": workflow_def,
            "execution_context": {
                "source_outputs": {"source_agent_id": agent_id, "content": previous_output},
                "previous_output": previous_output,
                "update_feedback": update_content,
            }
        }
    }
    await _log_event_to_db(session_id, retry_event, db)
    if not is_delegated_step:
        await queue.put({"action": "process_step", "session_id": session_id})
    log.info(f"[{session_id}] Update cascade started for {agent_id}; affected steps: {affected_step_indices}")


async def process_local_event(event: dict, queue: asyncio.Queue):
    """Main dispatcher for the integrated local workflow engine."""
    action = event.get("action")
    if action == "start":
        await _handle_start(event, queue)
    elif action == "resume":
        await _handle_resume(event, queue)
    elif action == "process_step":
        await _handle_process_step(event, queue)
    elif action == "validate_step":
        await _handle_validate_step(event, queue)
    elif action == "hitl_response":
        await _handle_hitl_response(event, queue)
    elif action == "update_session":
        await _handle_update_session(event, queue)
    else:
        log.warning(f"Unknown local engine action: {action}")


async def _handle_start(event: dict, queue: asyncio.Queue):
    """Initialises a workflow session and enqueues the first step."""
    session_id = event["session_id"]
    workflow_id = event["workflowId"]
    prompt = event["prompt"]
    title = event.get("session_title", "Local Project")

    db = get_db(os.environ.get("RAW_EVENTS_DB_NAME", "speakmanai_db"))
    workflow_doc = await db["workflows"].find_one({"workflowId": workflow_id})
    
    if not workflow_doc:
        log.error(f"Workflow {workflow_id} not found.")
        return

    # Fetch agents required by this workflow
    agents = await db["agents"].find({"workflows": workflow_id}).to_list(length=None)
    
    # Extract the custom planner for this workflow, if one exists
    planner_agent = next((a for a in agents if a.get("agentType") == "AI_PLANNER"), None)
    planner_system_prompt = (planner_agent.get("systemPrompt") or DEFAULT_PLANNER_SYSTEM_PROMPT) if planner_agent else DEFAULT_PLANNER_SYSTEM_PROMPT
    planner_model = (planner_agent.get("model") or DEFAULT_MODEL) if planner_agent else DEFAULT_MODEL
    planner_agent_id = planner_agent.get("agentId", "root_planner") if planner_agent else "root_planner"
    planner_exec_mode = planner_agent.get("executionMode", "auto") if planner_agent else "auto"

    workflow_agents = [a for a in agents if a.get("agentType") not in ("AI_PLANNER", "AI_VALIDATOR", "HITL_VALIDATOR")]
    
    # --- EXECUTION MODE OVERRIDE ---
    # Dynamically rewrite agent types based on the global execution mode
    settings = await db["settings"].find_one({"_id": "global_config"}) or {}
    exec_mode = settings.get("execution_mode", "auto")
    
    for a in workflow_agents:
        current_type = a.get("agentType", "AI_WORKFLOW")
        if exec_mode == "force_delegate" and current_type.startswith("AI_"):
            a["agentType"] = "MCP_LLM_DELEGATE"
        elif exec_mode == "force_background" and current_type == "MCP_LLM_DELEGATE":
            a["agentType"] = "AI_WORKFLOW"
        elif a.get("model") == "delegate" and current_type.startswith("AI_"):
            a["agentType"] = "MCP_LLM_DELEGATE"

    workflow_type = workflow_doc.get("workflowType", "mcp")
    
    if workflow_type == "system":
        log.info(f"[{session_id}] System workflow detected. Bypassing planner.")
        steps = [{"step_index": i, "step_type": "AI" if not a.get("agentType", "").startswith("MCP_") else "MCP_PAUSE", "agents": [a["agentId"]]} for i, a in enumerate(workflow_agents)]
        workflow_def = {"id": workflow_id, "title": title, "steps": steps}
    else:
        # Dynamic LLM Planner Execution
        agent_list_parts = []
        for a in workflow_agents:
            dep_str = f", Dependencies: {a.get('dependencies')}" if a.get("dependencies") else ""
            agent_list_parts.append(f"- ID: {a['agentId']}, Type: {a.get('agentType', 'AI_WORKFLOW')}, Description: {a.get('description', '')}{dep_str}")
        agent_list_for_prompt = "\n".join(agent_list_parts)
        
        user_content = f"Workflow Configuration:\n{workflow_doc.get('description', '')}\n\nAVAILABLE_AGENTS:\n{agent_list_for_prompt}\n\nUSER_PROMPT:\n{prompt}\nworkflow_id={workflow_id}"
        
        is_planner_delegated = exec_mode == "force_delegate" or (exec_mode == "auto" and planner_exec_mode == "delegate")
        
        if is_planner_delegated:
            log.info(f"[{session_id}] Delegating planner to CLI.")
            workflow_def = {
                "id": workflow_id, "title": title,
                "steps": [{"step_index": 0, "step_type": "MCP_PAUSE", "agents": [planner_agent_id]}]
            }
        else:
            log.info(f"[{session_id}] Calling Planner LLM to generate dynamic execution DAG...")
            try:
                llm_output = await _call_llm(
                    system_prompt=planner_system_prompt,
                    user_content=user_content,
                    model_name=planner_model,
                    mime_type="application/json",
                    temperature=0.0
                )
                llm_payload = _extract_json(llm_output)
                workflow_def = llm_payload.get("workflow_definition", {})
                workflow_def["id"] = workflow_id
                workflow_def["title"] = title
                
                # --- FORCE STEP TYPES, THEN SPLIT MULTI-AGENT STEPS ---
                # _handle_process_step only ever executes agent_configs[0] — a step the planner
                # bundled multiple agents into would silently drop every agent but the first
                # (and non-deterministically, since $in queries don't preserve order). Split any
                # such step into separate sequential single-agent steps so nothing gets dropped,
                # computing step_type per agent (not just the group's first) and renumbering
                # step_index across the whole resulting list.
                split_steps = []
                for step in workflow_def.get("steps", []):
                    agent_ids = step.get("agents") or []
                    if not agent_ids:
                        continue
                    if len(agent_ids) > 1:
                        log.warning(f"[{session_id}] Planner bundled {len(agent_ids)} agents into one step ({agent_ids}) — splitting into sequential steps.")
                    for agent_id in agent_ids:
                        agent_doc = next((a for a in workflow_agents if a["agentId"] == agent_id), None)
                        new_step = {"agents": [agent_id], "dependencies": step.get("dependencies", [])}
                        if agent_doc:
                            a_type = agent_doc.get("agentType", "")
                            a_exec_mode = agent_doc.get("executionMode", "auto")

                            if a_type.startswith("MCP_"): new_step["step_type"] = "MCP_PAUSE"
                            elif a_type == "AI_AGGREGATOR": new_step["step_type"] = "AGGREGATE"
                            elif exec_mode == "auto" and a_exec_mode == "delegate": new_step["step_type"] = "MCP_PAUSE"
                            else: new_step["step_type"] = "AI"
                        else:
                            new_step["step_type"] = step.get("step_type", "AI")
                        split_steps.append(new_step)
                for i, s in enumerate(split_steps):
                    s["step_index"] = i
                workflow_def["steps"] = split_steps
            except Exception as e:
                log.error(f"[{session_id}] Planner LLM failed to generate a valid workflow: {e}")
                error_event = {
                    "event_id": str(uuid.uuid4()), "publish_time": datetime.now(timezone.utc).isoformat(),
                    "attributes": {"session_id": session_id, "status": "FAILED", "event_type": "ERROR"},
                    "data": {"error_message": f"Planner failed: {str(e)}"}
                }
                await _log_event_to_db(session_id, error_event, db)
                return

    if is_planner_delegated and workflow_type != "system":
        initial_status = "AWAITING_INPUT"
        first_step_type = "MCP_PAUSE"
    else:
        first_step_type = workflow_def.get("steps", [{}])[0].get("step_type", "AI")
        initial_status = "AWAITING_INPUT" if first_step_type == "MCP_PAUSE" else "IN_PROGRESS"

    initial_event = {
        "event_id": str(uuid.uuid4()),
        "publish_time": datetime.now(timezone.utc).isoformat(),
        "attributes": {
            "session_id": session_id,
            "workflow_id": workflow_id,
            "owner_id": "local_user",
            "session_title": title,
            "current_step_index": "0",
            "status": initial_status,
            "event_type": "MCP_PAUSE" if is_planner_delegated or first_step_type == "MCP_PAUSE" else "WORKFLOW"
        },
        "data": {
            "workflow_definition": workflow_def,
            "execution_context": {
                "session_id": session_id,
                "source_outputs": {
                    "source_agent_id": planner_agent_id,
                    "content": user_content if is_planner_delegated and workflow_type != "system" else prompt
                }
            }
        }
    }

    # Log event via our integrated logger
    await _log_event_to_db(session_id, initial_event, db)
    
    log.info(f"[{session_id}] Workflow initialized. Status: {initial_status}")

    if initial_status == "IN_PROGRESS":
        await queue.put({"action": "process_step", "session_id": session_id})


async def _handle_resume(event: dict, queue: asyncio.Queue):
    """Resumes a paused workflow after a submit_response call."""
    session_id = event["session_id"]
    content = event["content"]
    
    # Globally strip reasoning <think> blocks from CLI delegate responses
    if content:
        content = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL).strip()
        
    current_step_index = event["current_step_index"]
    agent_id = event["agentId"]

    db = get_db(os.environ.get("RAW_EVENTS_DB_NAME", "speakmanai_db"))
    session_doc = await db["events_raw"].find_one({"session_id": session_id})
    
    if not session_doc:
        log.error(f"[{session_id}] Cannot resume: session not found.")
        return

    workflow_def = session_doc["events"][0]["data"]["workflow_definition"]
    steps = workflow_def.get("steps", [])
    
    is_planner_resume = False
    if len(steps) == 1 and steps[0].get("agents", [""])[0] == agent_id:
        agent_doc = await db["agents"].find_one({"agentId": agent_id})
        if agent_doc and agent_doc.get("agentType") == "AI_PLANNER":
            is_planner_resume = True
            
    if is_planner_resume:
        try:
            cleaned = content.strip()
            if cleaned.startswith("```json"): cleaned = cleaned[7:]
            elif cleaned.startswith("```"): cleaned = cleaned[3:]
            if cleaned.endswith("```"): cleaned = cleaned[:-3]
            
            parsed = _extract_json(cleaned.strip())
            workflow_def = parsed.get("workflow_definition", {})
            workflow_def["id"] = session_doc["events"][0]["attributes"].get("workflow_id")
            workflow_def["title"] = session_doc.get("session_title", "Local Project")
            
            # Force step types, then split any step the planner bundled multiple agents into
            # (this engine executes exactly one agent per step — see _handle_start for the
            # same fix and full rationale).
            workflow_agents = await db["agents"].find({"workflows": workflow_def["id"]}).to_list(length=None)
            settings = await db["settings"].find_one({"_id": "global_config"}) or {}
            exec_mode = settings.get("execution_mode", "auto")

            split_steps = []
            for step in workflow_def.get("steps", []):
                agent_ids = step.get("agents") or []
                if not agent_ids:
                    continue
                if len(agent_ids) > 1:
                    log.warning(f"[{session_id}] Delegated planner bundled {len(agent_ids)} agents into one step ({agent_ids}) — splitting into sequential steps.")
                for aid in agent_ids:
                    a_doc = next((a for a in workflow_agents if a["agentId"] == aid), None)
                    new_step = {"agents": [aid], "dependencies": step.get("dependencies", [])}
                    if a_doc:
                        a_type = a_doc.get("agentType", "")
                        a_exec_mode = a_doc.get("executionMode", "auto")

                        if exec_mode == "force_delegate" and a_type.startswith("AI_"): a_type = "MCP_LLM_DELEGATE"
                        elif exec_mode == "auto" and a_exec_mode == "delegate" and a_type.startswith("AI_"): a_type = "MCP_LLM_DELEGATE"
                        elif a_doc.get("model") == "delegate" and a_type.startswith("AI_"): a_type = "MCP_LLM_DELEGATE"

                        if a_type.startswith("MCP_"): new_step["step_type"] = "MCP_PAUSE"
                        elif a_type == "AI_AGGREGATOR": new_step["step_type"] = "AGGREGATE"
                        else: new_step["step_type"] = "AI"
                    else:
                        new_step["step_type"] = step.get("step_type", "AI")
                    split_steps.append(new_step)
            for i, s in enumerate(split_steps):
                s["step_index"] = i
            workflow_def["steps"] = split_steps

            next_step_index = 0
            steps = workflow_def.get("steps", [])
            is_final = len(steps) == 0

            # Retroactively update the initial event in DB with the true parsed plan
            await db["events_raw"].update_one(
                {"session_id": session_id, "events.attributes.current_step_index": "0"},
                {"$set": {"events.$.data.workflow_definition": workflow_def}}
            )

        except Exception as e:
            log.error(f"[{session_id}] Failed to parse CLI planner JSON: {e}")
            await db["events_raw"].update_one({"session_id": session_id}, {"$set": {"current_status": "FAILED"}})
            return

        # Prevent the JSON DAG from overwriting the initial user prompt in latest_outputs
        agent_id = "root_planner_dag"

        next_step_type = steps[next_step_index]["step_type"] if not is_final else None
        status = "COMPLETED" if is_final else ("AWAITING_INPUT" if next_step_type == "MCP_PAUSE" else "IN_PROGRESS")

        resume_event = {
            "event_id": str(uuid.uuid4()),
            "publish_time": datetime.now(timezone.utc).isoformat(),
            "attributes": {
                "session_id": session_id,
                "owner_id": "local_user",
                "current_step_index": str(next_step_index) if not is_final else str(current_step_index),
                "status": status,
                "event_type": "MCP_PAUSE" if status == "AWAITING_INPUT" else "WORKFLOW"
            },
            "data": {
                "workflow_definition": workflow_def,
                "execution_context": {
                    "source_outputs": {
                        "source_agent_id": agent_id,
                        "content": content
                    }
                }
            }
        }

        await _log_event_to_db(session_id, resume_event, db)

        log.info(f"[{session_id}] Resumed by local user (planner).")

        if status == "COMPLETED":
            await build_project_record(session_id, db)

        if status == "IN_PROGRESS":
            await queue.put({"action": "process_step", "session_id": session_id})
        return

    # --- Non-planner resume: a delegate agent's output arrived via submit_response ---
    latest_event = session_doc["events"][-1]
    hitl_loop = int(latest_event.get("attributes", {}).get("hitl_loop", "0"))
    cascade = _extract_cascade(latest_event)

    # --- Validation Check (delegated/paused steps) ---
    # Mirrors the check in _handle_process_step: a delegate agent's output
    # never passes through that function, so it must be validated here instead.
    agent_config = await db["agents"].find_one({"agentId": agent_id})
    validator_agent_id = agent_config.get("validatorAgentId") if agent_config else None
    if validator_agent_id:
        validation_loop = int(latest_event.get("attributes", {}).get("validation_loop", "0"))
        log.info(f"[{session_id}] Delegated agent {agent_id} requires validation by {validator_agent_id}. Queueing validation...")
        await queue.put({
            "action": "validate_step",
            "session_id": session_id,
            "current_step_idx": current_step_index,
            "workflow_def": workflow_def,
            "agent_id": agent_id,
            "validator_agent_id": validator_agent_id,
            "content": content,
            "validation_loop": validation_loop,
            "hitl_loop": hitl_loop,
            "cascade": cascade,
            "source_outputs": {"source_agent_id": agent_id, "content": content}
        })
        return

    log.info(f"[{session_id}] Resumed by local user.")
    await _maybe_gate_by_hitl(session_id, db, queue, workflow_def, current_step_index, agent_id, content, hitl_loop, cascade=cascade)


async def _handle_process_step(event: dict, queue: asyncio.Queue):
    """Executes a single workflow step via the configured LLM."""
    session_id = event["session_id"]
    
    db = get_db(os.environ.get("RAW_EVENTS_DB_NAME", "speakmanai_db"))
    doc = await db["events_raw"].find_one({"session_id": session_id})
    
    if not doc or doc.get("current_status") in ("CANCELLED", "COMPLETED", "AWAITING_INPUT"):
        return  # Nothing to execute

    events = doc.get("events", [])
    latest_event = events[-1]
    
    current_step_idx = int(latest_event["attributes"]["current_step_index"])
    workflow_def = latest_event["data"]["workflow_definition"]
    steps = workflow_def.get("steps", [])
    
    if current_step_idx >= len(steps):
        return
        
    current_step = steps[current_step_idx]
    agents_to_run = current_step.get("agents", [])
    source_content = latest_event["data"]["execution_context"]["source_outputs"]["content"]

    # Load agent configs
    agent_configs = await db["agents"].find({"agentId": {"$in": agents_to_run}}).to_list(length=None)
    
    # For this stripped-down local version, we assume sequential execution (one agent per step)
    agent_config = agent_configs[0] if agent_configs else None
    if not agent_config:
        log.error(f"[{session_id}] Agent config not found for step {current_step_idx}.")
        return

    agent_id = agent_config["agentId"]
    system_prompt = agent_config.get("systemPrompt", "")
    model_name = agent_config.get("model", DEFAULT_MODEL)
    mime_type = agent_config.get("mimeType", "text/plain")
    
    # --- Validation / HITL / Update Retry Injection ---
    exec_ctx = latest_event.get("data", {}).get("execution_context", {})
    validation_feedback = exec_ctx.get("validation_feedback")
    hitl_feedback        = exec_ctx.get("hitl_feedback")
    update_feedback      = exec_ctx.get("update_feedback")
    previous_output     = exec_ctx.get("previous_output")
    validation_loop     = int(latest_event.get("attributes", {}).get("validation_loop", "0"))
    hitl_loop           = int(latest_event.get("attributes", {}).get("hitl_loop", "0"))
    cascade             = _extract_cascade(latest_event)

    PRESERVE_INSTRUCTION = (
        "This is a targeted revision, not a rewrite: change only what the feedback below "
        "actually calls out. Everything else in your previous output — every field, value, "
        "component, and structural choice not mentioned — must carry over exactly as it was. "
        "Do not regenerate the document from scratch and do not silently drop, rename, or "
        "restructure anything the feedback didn't ask you to touch."
    )

    user_content = _compile_dependencies_context(agent_config, events)
    if validation_feedback:
        prior_block = (
            f"\n\n# YOUR PREVIOUS OUTPUT (Attempt {validation_loop})\n{previous_output}"
            if previous_output else ""
        )
        user_content += (
            f"{prior_block}\n\n"
            f"# VALIDATOR RESPONSE (Attempt {validation_loop})\n"
            f"{PRESERVE_INSTRUCTION} Address every issue raised below:\n"
            f"{validation_feedback}"
        )
    if hitl_feedback:
        prior_block = (
            f"\n\n# YOUR PREVIOUS OUTPUT (Attempt {hitl_loop})\n{previous_output}"
            if previous_output else ""
        )
        user_content += (
            f"{prior_block}\n\n"
            f"# HUMAN REVIEWER RESPONSE (Attempt {hitl_loop})\n"
            f"{PRESERVE_INSTRUCTION} Address the feedback below:\n"
            f"{hitl_feedback}"
        )
    if update_feedback:
        update_attempt = cascade.get("loop", 1) if cascade else 1
        prior_block = (
            f"\n\n# YOUR PREVIOUS OUTPUT (Attempt {update_attempt})\n{previous_output}"
            if previous_output else ""
        )
        user_content += (
            f"{prior_block}\n\n"
            f"# PROJECT UPDATE REQUEST (Attempt {update_attempt})\n"
            f"{PRESERVE_INSTRUCTION} Address the requested change below:\n"
            f"{update_feedback}"
        )

    log.info(f"[{session_id}] Executing AI step {current_step_idx} with agent {agent_id} via {LLM_PROVIDER}...")

    try:
        llm_output = await _call_llm(
            system_prompt=system_prompt,
            user_content=user_content,
            model_name=model_name,
            mime_type=mime_type
        )
    except Exception as e:
        log.error(f"[{session_id}] LLM execution failed: {e}", exc_info=True)
        error_event = {
            "event_id": str(uuid.uuid4()),
            "publish_time": datetime.now(timezone.utc).isoformat(),
            "attributes": {
                "session_id": session_id,
                "owner_id": "local_user",
                "status": "FAILED",
                "event_type": "ERROR"
            },
            "data": {"error_message": str(e)}
        }
        await _log_event_to_db(session_id, error_event, db)
        await db["events_raw"].update_one({"session_id": session_id}, {"$set": {"current_status": "FAILED", "error_message": str(e)}})
        return

    # --- Validation Check ---
    validator_agent_id = agent_config.get("validatorAgentId")
    if validator_agent_id:
        log.info(f"[{session_id}] Agent {agent_id} requires validation by {validator_agent_id}. Queueing validation...")
        await queue.put({
            "action": "validate_step",
            "session_id": session_id,
            "current_step_idx": current_step_idx,
            "workflow_def": workflow_def,
            "agent_id": agent_id,
            "validator_agent_id": validator_agent_id,
            "content": llm_output,
            "validation_loop": validation_loop,
            "hitl_loop": hitl_loop,
            "cascade": cascade,
            "source_outputs": latest_event["data"]["execution_context"]["source_outputs"]
        })
        return

    # No validator configured (or it already passed) — check the HITL gate before advancing.
    await _maybe_gate_by_hitl(session_id, db, queue, workflow_def, current_step_idx, agent_id, llm_output, hitl_loop, cascade=cascade)


async def _handle_validate_step(event: dict, queue: asyncio.Queue):
    """Scores agent output and re-enqueues the step for retry if it fails validation."""
    session_id = event["session_id"]
    current_step_idx = event["current_step_idx"]
    workflow_def = event["workflow_def"]
    agent_id = event["agent_id"]
    validator_agent_id = event["validator_agent_id"]
    content_to_validate = event["content"]
    validation_loop = event.get("validation_loop", 0)
    hitl_loop = event.get("hitl_loop", 0)
    cascade = event.get("cascade")
    original_source_outputs = event["source_outputs"]

    db = get_db(os.environ.get("RAW_EVENTS_DB_NAME", "speakmanai_db"))
    session_doc = await db["events_raw"].find_one({"session_id": session_id})
    events = session_doc.get("events", []) if session_doc else []

    validator_config = await db["agents"].find_one({"agentId": validator_agent_id})
    if not validator_config:
        log.warning(f"[{session_id}] Validator {validator_agent_id} not found. Auto-passing.")
        score, feedback, max_loops, min_score, val_output = 10, "Validator not found.", 0, 0.0, "Validator not found."
    else:
        val_config = validator_config.get("validationConfig", {})
        max_loops = int(val_config.get("maxLoops", 3))
        min_score = float(val_config.get("minScore", 7.0))

        dep_context = _compile_dependencies_context(validator_config, events)
        validator_user_content = f"# OUTPUT UNDER REVIEW ({agent_id})\n{content_to_validate}"
        if dep_context:
            validator_user_content += f"\n\n{dep_context}"

        log.info(f"[{session_id}] Running validation loop {validation_loop+1}/{max_loops} with {validator_agent_id}...")
        try:
            val_output = await _call_llm(
                system_prompt=validator_config.get("systemPrompt", ""),
                user_content=validator_user_content,
                model_name=validator_config.get("model", DEFAULT_MODEL),
                mime_type="application/json",
                temperature=0.0
            )
            parsed = _extract_json(val_output)
            audit = parsed.get("audit_summary", {})
            score = float(audit.get("overall_score") or parsed.get("score") or 0)
            feedback = parsed.get("feedback", "")

            # Critical violations always force a fail regardless of overall_score
            critical_count = int(audit.get("critical_violation_count") or 0)
            has_critical_in_list = any(
                v.get("severity") == "CRITICAL"
                for v in parsed.get("identified_violations", [])
            )
            if critical_count > 0 or has_critical_in_list:
                log.warning(f"[{session_id}] Critical violations detected (count={critical_count}) — overriding score {score} → 0")
                score = 0.0
                feedback = f"[{critical_count} CRITICAL VIOLATION(S) — score overridden] {feedback}"
        except Exception as e:
            log.error(f"[{session_id}] Validation failed: {e}")
            score, feedback = 0, f"Validation parsing error: {e}"
            val_output = feedback

    log.info(f"[{session_id}] Validation result: Score={score}, Feedback={feedback[:50]}...")

    steps = workflow_def.get("steps", [])
    origin_step_type = steps[current_step_idx]["step_type"] if current_step_idx < len(steps) else None
    is_delegated_step = origin_step_type == "MCP_PAUSE"

    if score >= min_score or (validation_loop + 1) >= max_loops:
        if score < min_score:
            log.warning(f"[{session_id}] Max validation loops hit. Advancing despite low score ({score}).")

        # PASS! Check the HITL gate before advancing to the next workflow step —
        # this is what makes "AI validator first, then HITL" work when both are configured.
        await _maybe_gate_by_hitl(session_id, db, queue, workflow_def, current_step_idx, agent_id, content_to_validate, hitl_loop, cascade=cascade)

    else:
        # FAIL! Retry the exact same step with the feedback injected.
        # Delegated (MCP_PAUSE) steps must re-pause and wait for submit_response —
        # they have no real "model" to call via _call_llm — while background AI
        # steps loop straight back through process_step.
        retry_status = "AWAITING_INPUT" if is_delegated_step else "IN_PROGRESS"
        retry_attrs = {
            "session_id": session_id, "owner_id": "local_user", "current_step_index": str(current_step_idx),
            "status": retry_status,
            "event_type": "MCP_PAUSE" if is_delegated_step else "WORKFLOW",
            "validation_retry": "true", "validation_loop": str(validation_loop + 1),
            "hitl_loop": str(hitl_loop),
        }
        if cascade:
            retry_attrs["update_loop"] = str(cascade.get("loop", 0))
            retry_attrs["update_cascade_steps"] = ",".join(str(i) for i in cascade.get("steps", []))
        retry_event = {
            "event_id": str(uuid.uuid4()), "publish_time": datetime.now(timezone.utc).isoformat(),
            "attributes": retry_attrs,
            "data": {
                "workflow_definition": workflow_def,
                "execution_context": {
                    "source_outputs": original_source_outputs,
                    "previous_output": content_to_validate,
                    "validation_feedback": val_output
                }
            }
        }
        await _log_event_to_db(session_id, retry_event, db)
        if not is_delegated_step:
            await queue.put({"action": "process_step", "session_id": session_id})

# --- Local Project Output Operations ---

async def list_projects_local() -> str:
    db = get_db(os.environ.get("RAW_EVENTS_DB_NAME", "speakmanai_db"))
    cursor = db["projects"].find({"owner_id": "local_user"}).sort("created_at", -1).limit(50)
    projects = []
    async for doc in cursor:
        projects.append({
            "session_id": doc["session_id"],
            "title": doc.get("title", ""),
            "status": doc.get("status", "COMPLETED"),
            "section_count": len(doc.get("outputs", {}).get("sections", [])),
            "has_document": bool(doc.get("outputs", {}).get("document")),
            "has_items": bool(doc.get("outputs", {}).get("items_json"))
        })
    return json.dumps(projects, indent=2)


async def get_project_local(session_id: str) -> str:
    db = get_db(os.environ.get("RAW_EVENTS_DB_NAME", "speakmanai_db"))
    doc = await db["projects"].find_one({"session_id": session_id})
    if not doc:
        raise ValueError(f"Project {session_id} not found.")
    
    can_generate = len(doc.get("outputs", {}).get("sections", [])) > 0

    return json.dumps({
        "session_id": doc["session_id"],
        "title": doc.get("title", ""),
        "status": doc.get("status", "COMPLETED"),
        "sections": doc.get("outputs", {}).get("sections", []),
        "capabilities": {
            "can_generate_document": can_generate
        }
    }, indent=2)


async def generate_document_local(session_id: str, format_hint: str) -> str:
    db = get_db(os.environ.get("RAW_EVENTS_DB_NAME", "speakmanai_db"))
    project = await db["projects"].find_one({"session_id": session_id})
    if not project or not project.get("outputs", {}).get("sections"):
        raise ValueError(f"Cannot generate document: Project {session_id} not found or has no completed sections.")
    
    full_context = "\n\n".join([f"## {s['title']}\n{s['content']}" for s in project["outputs"]["sections"]])
    system_prompt = f"You are an expert technical writer. Assemble the provided sections into a highly professional {format_hint}. Output in clean Markdown format."
    
    log.info(f"[{session_id}] Calling {LLM_PROVIDER} to generate {format_hint} document...")
    document_text = await _call_llm(
        system_prompt=system_prompt,
        user_content=f"# SOURCE SECTIONS\n\n{full_context}",
        model_name=DEFAULT_MODEL,
        mime_type="text/plain",
        temperature=0.2
    )
    
    await db["projects"].update_one({"session_id": session_id}, {"$set": {"outputs.document": document_text}})
    return json.dumps({"session_id": session_id, "format": format_hint, "document": document_text}, indent=2)


async def render_document_local(markdown_text: str, title: str = None, subtitle: str = None) -> str:
    import document_renderer
    html = document_renderer.render_markdown_to_html(markdown_text, title, subtitle)
    return json.dumps({"html": html, "title": title, "subtitle": subtitle})


