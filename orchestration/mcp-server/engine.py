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

# ─────────────────────────────────────────────
# LLM Provider Configuration — read dynamically from os.environ at call time
# so that config changes via /api/config take effect without restart.
# ─────────────────────────────────────────────

def _cfg():
    """Return current provider config from environment (live — no restart needed)."""
    return {
        "provider":        os.environ.get("LLM_PROVIDER", "gemini").lower(),
        "default_model":   os.environ.get("DEFAULT_MODEL", "gemini-2.5-flash"),
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
DEFAULT_MODEL       = os.environ.get("DEFAULT_MODEL", "gemini-2.5-flash")
ADVANCED_MODEL      = os.environ.get("ADVANCED_MODEL", "")
OLLAMA_BASE_URL     = os.environ.get("OLLAMA_BASE_URL", "http://host.docker.internal:11434")
OLLAMA_FALLBACK_MODEL = os.environ.get("OLLAMA_FALLBACK_MODEL", "llama3")

# ── Abstract model tier → provider model mapping ──────────────────────────
MODEL_TIERS = {
    "gemini":    {"fast": "gemini-2.5-flash", "standard": "gemini-2.5-flash", "advanced": "gemini-2.5-pro"},
    "vertexai":  {"fast": "gemini-2.5-flash", "standard": "gemini-2.5-flash", "advanced": "gemini-2.5-pro"},
    "anthropic": {"fast": "claude-haiku-4-5-20251001", "standard": "claude-sonnet-4-6", "advanced": "claude-opus-4-6"},
    "openai":    {"fast": "gpt-4o-mini",       "standard": "gpt-4o",           "advanced": "gpt-4o"},
    "ollama":    {"fast": None,                 "standard": None,               "advanced": None},
}

# Reverse map: known model name → tier (used for cross-provider conflict resolution)
_MODEL_TO_TIER = {
    "gemini-2.5-pro": "advanced",   "gemini-2.5-flash": "fast",
    "gemini-2.0-pro": "advanced",   "gemini-2.0-flash": "fast",
    "claude-opus-4-6": "advanced",  "claude-sonnet-4-6": "standard",
    "claude-haiku-4-5-20251001": "fast", "claude-haiku-4-5": "fast",
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
    advanced    = cfg["advanced_model"]
    ollama_fb   = cfg["ollama_fallback"]

    if not model_name:
        return provider, default

    # Abstract tier
    if model_name in ("fast", "standard", "advanced"):
        tiers = MODEL_TIERS.get(provider, {})
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
        response = await client.messages.create(
            model=model_name,
            max_tokens=8192,
            temperature=temperature,
            system=system_prompt,
            messages=[{"role": "user", "content": user_content}],
        )
        result_text = response.content[0].text if response.content else ""

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
        response = await client.aio.models.generate_content(
            model=model_name,
            contents=user_content,
            config=_genai_types.GenerateContentConfig(
                system_instruction=system_prompt,
                temperature=temperature,
                response_mime_type=mime_type,
            ),
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
    
    sections = []
    for event in events:
        attrs = event.get("attributes", {})
        if attrs.get("event_type") == "VALIDATION" or attrs.get("validation_retry") == "true":
            continue
        src = event.get("data", {}).get("execution_context", {}).get("source_outputs", {})
        agent_id = src.get("source_agent_id")
        
        # Filter out HITL pause steps and the planner from the final project document based on true agentType
        if agent_id and not agent_id.startswith("root_planner"):
            a_type = agent_type_map.get(agent_id, "")
            if a_type != "AI_PLANNER" and (a_type == "MCP_LLM_DELEGATE" or not a_type.startswith("MCP_")): 
                sections.append({
                    "agent_id": agent_id,
                    "content": src.get("content", ""),
                    "title": agent_id.replace("_", " ").title()
                })
            
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
        if attrs.get("event_type") == "VALIDATION" or attrs.get("validation_retry") == "true":
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
    planner_system_prompt = planner_agent.get("systemPrompt", DEFAULT_PLANNER_SYSTEM_PROMPT) if planner_agent else DEFAULT_PLANNER_SYSTEM_PROMPT
    planner_model = planner_agent.get("model", DEFAULT_MODEL) if planner_agent else DEFAULT_MODEL
    planner_agent_id = planner_agent.get("agentId", "root_planner") if planner_agent else "root_planner"
    planner_exec_mode = planner_agent.get("executionMode", "auto") if planner_agent else "auto"

    workflow_agents = [a for a in agents if a.get("agentType") not in ("AI_PLANNER", "AI_VALIDATOR")]
    
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
                llm_payload = json.loads(llm_output)
                workflow_def = llm_payload.get("workflow_definition", {})
                workflow_def["id"] = workflow_id
                workflow_def["title"] = title
                
                # --- FORCE STEP TYPES ---
                for step in workflow_def.get("steps", []):
                    if not step.get("agents"): continue
                    agent_id = step["agents"][0]
                    agent_doc = next((a for a in workflow_agents if a["agentId"] == agent_id), None)
                    if agent_doc:
                        a_type = agent_doc.get("agentType", "")
                        a_exec_mode = agent_doc.get("executionMode", "auto")
                        
                        if a_type.startswith("MCP_"): step["step_type"] = "MCP_PAUSE"
                        elif a_type == "AI_AGGREGATOR": step["step_type"] = "AGGREGATE"
                        elif exec_mode == "auto" and a_exec_mode == "delegate": step["step_type"] = "MCP_PAUSE"
                        else: step["step_type"] = "AI"
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
            
            parsed = json.loads(cleaned.strip())
            workflow_def = parsed.get("workflow_definition", {})
            workflow_def["id"] = session_doc["events"][0]["attributes"].get("workflow_id")
            workflow_def["title"] = session_doc.get("session_title", "Local Project")
            
            # Force step types
            workflow_agents = await db["agents"].find({"workflows": workflow_def["id"]}).to_list(length=None)
            settings = await db["settings"].find_one({"_id": "global_config"}) or {}
            exec_mode = settings.get("execution_mode", "auto")
            
            for step in workflow_def.get("steps", []):
                if not step.get("agents"): continue
                a_doc = next((a for a in workflow_agents if a["agentId"] == step["agents"][0]), None)
                if a_doc:
                    a_type = a_doc.get("agentType", "")
                    a_exec_mode = a_doc.get("executionMode", "auto")
                    
                    if exec_mode == "force_delegate" and a_type.startswith("AI_"): a_type = "MCP_LLM_DELEGATE"
                    elif exec_mode == "auto" and a_exec_mode == "delegate" and a_type.startswith("AI_"): a_type = "MCP_LLM_DELEGATE"
                    
                    if a_type.startswith("MCP_"): step["step_type"] = "MCP_PAUSE"
                    elif a_type == "AI_AGGREGATOR": step["step_type"] = "AGGREGATE"
                    else: step["step_type"] = "AI"
                    
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
    else:
        next_step_index = current_step_index + 1
        is_final = next_step_index >= len(steps)
        
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

    log.info(f"[{session_id}] Resumed by local user.")

    if status == "COMPLETED":
        await build_project_record(session_id, db)

    if status == "IN_PROGRESS":
        await queue.put({"action": "process_step", "session_id": session_id})


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
    
    # --- Validation Retry Injection ---
    validation_feedback = latest_event.get("data", {}).get("execution_context", {}).get("validation_feedback")
    validation_loop = int(latest_event.get("attributes", {}).get("validation_loop", "0"))
    
    user_content = _compile_dependencies_context(agent_config, events)
    if validation_feedback:
        user_content += f"\n\n# VALIDATION FEEDBACK (Attempt {validation_loop})\nPlease revise your previous output to address this feedback and achieve a passing score:\n{validation_feedback}"

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
            "source_outputs": latest_event["data"]["execution_context"]["source_outputs"]
        })
        return

    # Prepare next step
    next_step_idx = current_step_idx + 1
    is_final = next_step_idx >= len(steps)
    next_step_type = steps[next_step_idx]["step_type"] if not is_final else None
    
    status = "COMPLETED" if is_final else ("AWAITING_INPUT" if next_step_type == "MCP_PAUSE" else "IN_PROGRESS")

    new_event = {
        "event_id": str(uuid.uuid4()),
        "publish_time": datetime.now(timezone.utc).isoformat(),
        "attributes": {
            "session_id": session_id,
            "owner_id": "local_user",
            "current_step_index": str(next_step_idx) if not is_final else str(current_step_idx),
            "status": status,
            "event_type": "MCP_PAUSE" if status == "AWAITING_INPUT" else "WORKFLOW"
        },
        "data": {"workflow_definition": workflow_def, "execution_context": {"source_outputs": {"source_agent_id": agent_id, "content": llm_output}}}
    }

    await _log_event_to_db(session_id, new_event, db)
    
    if status == "COMPLETED":
        await build_project_record(session_id, db)

    if status == "IN_PROGRESS":
        await queue.put({"action": "process_step", "session_id": session_id})


async def _handle_validate_step(event: dict, queue: asyncio.Queue):
    """Scores agent output and re-enqueues the step for retry if it fails validation."""
    session_id = event["session_id"]
    current_step_idx = event["current_step_idx"]
    workflow_def = event["workflow_def"]
    agent_id = event["agent_id"]
    validator_agent_id = event["validator_agent_id"]
    content_to_validate = event["content"]
    validation_loop = event.get("validation_loop", 0)
    original_source_outputs = event["source_outputs"]
    
    db = get_db(os.environ.get("RAW_EVENTS_DB_NAME", "speakmanai_db"))
    
    validator_config = await db["agents"].find_one({"agentId": validator_agent_id})
    if not validator_config:
        log.warning(f"[{session_id}] Validator {validator_agent_id} not found. Auto-passing.")
        score, feedback, max_loops = 10, "Validator not found.", 0
    else:
        val_config = validator_config.get("validationConfig", {})
        max_loops = int(val_config.get("maxLoops", 3))
        min_score = float(val_config.get("minScore", 7.0))
        
        log.info(f"[{session_id}] Running validation loop {validation_loop+1}/{max_loops} with {validator_agent_id}...")
        try:
            val_output = await _call_llm(
                system_prompt=validator_config.get("systemPrompt", ""),
                user_content=f"# CONTENT TO VALIDATE\n\n{content_to_validate}",
                model_name=validator_config.get("model", DEFAULT_MODEL),
                mime_type="application/json",
                temperature=0.0
            )
            parsed = json.loads(val_output)
            score = float(parsed.get("audit_summary", {}).get("overall_score") or parsed.get("score") or 0)
            feedback = parsed.get("feedback", "")
        except Exception as e:
            log.error(f"[{session_id}] Validation failed: {e}")
            score, feedback = 0, f"Validation parsing error: {e}"

    log.info(f"[{session_id}] Validation result: Score={score}, Feedback={feedback[:50]}...")

    if score >= min_score or (validation_loop + 1) >= max_loops:
        if score < min_score:
            log.warning(f"[{session_id}] Max validation loops hit. Advancing despite low score ({score}).")
            
        # PASS! Save the content and advance to the next workflow step
        steps = workflow_def.get("steps", [])
        next_step_idx = current_step_idx + 1
        is_final = next_step_idx >= len(steps)
        next_step_type = steps[next_step_idx]["step_type"] if not is_final else None
        
        status = "COMPLETED" if is_final else ("AWAITING_INPUT" if next_step_type == "MCP_PAUSE" else "IN_PROGRESS")
        
        new_event = {
            "event_id": str(uuid.uuid4()), "publish_time": datetime.now(timezone.utc).isoformat(),
            "attributes": {"session_id": session_id, "owner_id": "local_user", "current_step_index": str(next_step_idx) if not is_final else str(current_step_idx), "status": status, "event_type": "MCP_PAUSE" if status == "AWAITING_INPUT" else "WORKFLOW"},
            "data": {"workflow_definition": workflow_def, "execution_context": {"source_outputs": {"source_agent_id": agent_id, "content": content_to_validate}}}
        }
        await _log_event_to_db(session_id, new_event, db)
        
        if status == "COMPLETED": await build_project_record(session_id, db)
        if status == "IN_PROGRESS": await queue.put({"action": "process_step", "session_id": session_id})
            
    else:
        # FAIL! Force the AI to retry the exact same step with the feedback injected
        retry_event = {
            "event_id": str(uuid.uuid4()), "publish_time": datetime.now(timezone.utc).isoformat(),
            "attributes": {
                "session_id": session_id, "owner_id": "local_user", "current_step_index": str(current_step_idx),
                "status": "IN_PROGRESS", "event_type": "WORKFLOW", "validation_retry": "true", "validation_loop": str(validation_loop + 1)
            },
            "data": {
                "workflow_definition": workflow_def,
                "execution_context": {
                    "source_outputs": original_source_outputs,
                    "validation_feedback": f"Score: {score}/10. Feedback: {feedback}"
                }
            }
        }
        await _log_event_to_db(session_id, retry_event, db)
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


