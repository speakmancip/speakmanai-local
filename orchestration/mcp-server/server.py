"""
SPEAKMAN.AI MCP Server
Exposes SPEAKMAN.AI workflows to remote agents via the Model Context Protocol.
Transport: Streamable HTTP (stateless, per-request auth)
"""
import asyncio
import os
import re
import json
import logging
import uuid
import httpx
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote
import json as _json

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, HTMLResponse
from mcp.server.fastmcp import FastMCP, Context
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

from database import get_db, get_user_db, close as close_db
from engine import (
    process_local_event,
    list_projects_local,
    get_project_local,
    generate_document_local
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# --- Integrated Workflow Queue ---
workflow_queue = asyncio.Queue()

async def local_workflow_consumer():
    """Background task that queues events"""
    while True:
        event = await workflow_queue.get()
        log.info(f"Local Workflow Engine picked up event: {event['action']} for session {event.get('session_id')}")
        try:
            await process_local_event(event, workflow_queue)
        except Exception as e:
            log.error(f"Error processing event {event['action']}: {e}", exc_info=True)
        finally:
            workflow_queue.task_done()

# --- PII Scrubber ---
# Applied to all user-supplied text before forwarding to workflow pipeline.
_PII_PATTERNS = {
    "AWS_KEY":      (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED_AWS_KEY]"),
    "PRIVATE_KEY":  (re.compile(r"-----BEGIN [A-Z]+ PRIVATE KEY-----"), "[REDACTED_PRIVATE_KEY_BLOCK]"),
    "CREDIT_CARD":  (re.compile(r"\b(?:\d[ -]*?){13,16}\b"), "[REDACTED_CREDIT_CARD]"),
    "SSN_SIN":      (re.compile(r"\b(?:\d{3}[-\s]\d{2}[-\s]\d{4}|\d{3}[-\s]\d{3}[-\s]\d{3})\b"), "[REDACTED_GOV_ID]"),
    "EMAIL":        (re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"), "[REDACTED_EMAIL]"),
    "PHONE":        (re.compile(r"(?:\+?1[-. ]?)?\(?\d{3}\)?[-. ]?\d{3}[-. ]?\d{4}\b"), "[REDACTED_PHONE]"),
}

def scrub_pii(text: str) -> str:
    """Redact PII and secrets from a string before sending to the workflow pipeline."""
    if not text or not isinstance(text, str):
        return text
    for pattern, replacement in _PII_PATTERNS.values():
        text = pattern.sub(replacement, text)
    return text



# --- MCP Server ---
# Explicitly disable DNS rebinding protection — FastMCP enables it by default for
# localhost hosts, which rejects external hostnames with 421. 
mcp = FastMCP(
    name="speakmanai",
    stateless_http=True,  # Each request is independent — no in-memory session state.
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    instructions=(
        "SPEAKMAN.AI is a multi-agent workflow engine. You can run built-in workflows (e.g. Solution "
        "Architecture, Capability Generator) or any custom workflow built with the Workflow Builder. "
        "Each workflow is a pipeline of AI agents that collaborate to produce structured outputs.\n\n"
        "WORKFLOW CALL CHAINS — follow exactly for the workflow type you are running:\n\n"
        "Standard workflow (most workflows, including MCP_SOLUTION_ARCHITECTURE_V1 and custom workflows):\n"
        "  1. list_workflows()  →  choose a workflow_id\n"
        "  2. start_session(workflow_id, title, description)  →  returns session_id immediately\n"
        "  3. poll_workflow(session_id) every 30s  →  wait for status AWAITING_INPUT or COMPLETED\n"
        "  4. When AWAITING_INPUT: review input_required.prompt and input_required.schema\n"
        "     Build the requested data matching the schema\n"
        "  5. submit_response(session_id, response)  →  pipeline continues\n"
        "  6. poll_workflow(session_id) every 30-60s  →  wait for status COMPLETED\n"
        "  7. get_outputs(session_id)  →  returns manifest: available_outputs[], document_capabilities[], suggested_next_steps[]\n"
        "  8. get_output(session_id, agent_id)  →  full content of one agent (call once per agent you need)\n\n"
        "Capability Generator workflow (MCP_CAPABILITY_GENERATOR_V1):\n"
        "  1. list_workflows()  →  find MCP_CAPABILITY_GENERATOR_V1\n"
        "  2. start_session(workflow_id, title=<group name>, description=<capabilities JSON array as string>)\n"
        "     → returns session_id immediately (description IS the capabilities payload)\n"
        "  3. poll_workflow(session_id)  →  wait for COMPLETED (~2-5 min)\n"
        "  !! DO NOT call submit_response for Capability Generator — no AWAITING_INPUT step !!\n\n"
        "poll_workflow returns status and completed_steps ONLY — never full content. "
        "Always call get_outputs then get_output to retrieve content.\n\n"
        "When poll_workflow returns status=AWAITING_INPUT, it includes an input_required block "
        "with prompt, schema, and context. Build your response and call submit_response.\n\n"
        "Agent outputs are fetched individually to keep each response within context limits. "
        "get_outputs returns the manifest only; get_output returns one agent's full content.\n\n"
        "get_outputs works on any past session_id — use it to resume downstream work across conversations.\n\n"
        "If a session is stuck or failed, call cancel_session(session_id) to free your concurrency slot.\n\n"
        "If you lose a session_id (context reset, new conversation), call list_sessions() to recover it."
    )
)

# ─────────────────────────────────────────────
# Tool 1: list_workflows
# ─────────────────────────────────────────────
@mcp.tool(name="list_workflows", annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True))
async def list_workflows() -> str:
    """
    List all available SPEAKMAN.AI workflows.
    Returns workflow IDs, names, descriptions, and credit cost.

    Returns all available workflows including built-in and any custom workflows you have created.
    Use the returned workflow_id to start a session.
    """
    db = get_db()

    workflows = await db["workflows"].find(
        {"workflowType": "mcp"},
        {"_id": 0, "workflowId": 1, "displayName": 1, "description": 1}
    ).to_list(length=50)

    return json.dumps(workflows)



# ─────────────────────────────────────────────
# Tool 3: start_session
# ─────────────────────────────────────────────
@mcp.tool(name="start_session", annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True))
async def start_session(
    workflow_id: str,
    title: str,
    description: str,
) -> str:
    """
    Start a new workflow session and return a session_id immediately.
    The workflow runs asynchronously — poll_workflow to track progress.

    Args:
        workflow_id: The workflow ID from list_workflows.
        title: Short title for this session (max 100 chars).
            For Capability Generator: use this as the capability group name.
        description: Context or payload for the workflow (max 10000 chars).
            - Most workflows: a plain-text description of what you want to accomplish.
            - Capability Generator (MCP_CAPABILITY_GENERATOR_V1): pass the capabilities JSON array
              as a string — this IS the payload, not a description.

    Returns:
        session_id, credit_balance, and next_step instructions.
        Some workflows will reach AWAITING_INPUT status and require a submit_response call before completing.
    """
    desc_limit = 10000
    if len(description) > desc_limit:
        raise ValueError(f"description must be {desc_limit} characters or less.")
    if len(title) > 100:
        raise ValueError("title must be 100 characters or less.")

    session_id = f"session-{uuid.uuid4()}"

    # Scrub PII from user-supplied text before it enters the workflow pipeline.
    clean_description = scrub_pii(description)
    clean_title = scrub_pii(title)

    # Enqueue workflow for async processing
    await workflow_queue.put({
        "action": "start",
        "prompt": clean_description,
        "workflowId": workflow_id,
        "session_id": session_id,
        "session_title": clean_title,
        "user_id": "local_user"
    })

    is_capability_generator = "CAPABILITY_GENERATOR" in workflow_id.upper()

    if is_capability_generator:
        next_step = (
            "Capability Generator is running. Call poll_workflow(session_id) to check status. "
            "Typical runtime: 2-5 minutes. DO NOT call submit_response — no AWAITING_INPUT step."
        )
    else:
        next_step = (
            "Workflow started. Call poll_workflow(session_id) every 15-30 seconds. "
            "When status is AWAITING_INPUT, read input_required.prompt and input_required.schema, "
            "build your response, then call submit_response(session_id, response). "
            "After submission, poll again until COMPLETED."
        )

    return json.dumps({
        "session_id": session_id,
        "next_step": next_step,
    }, indent=2)


# ─────────────────────────────────────────────
# Tool 3: submit_response  (generic HITL resume)
# ─────────────────────────────────────────────
@mcp.tool(name="submit_response", annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True))
async def submit_response(
    session_id: str,
    response: str,
) -> str:
    """
    Submit your response to a workflow that is waiting for input (status=AWAITING_INPUT).

    Call poll_workflow first — when it returns status=AWAITING_INPUT, read the input_required
    block (prompt, schema, context) to understand what to provide, then call this tool.

    For architecture workflows the response is a JSON array of capabilities.
    For other workflows the response is whatever input_required.prompt asks for.

    !! Only call this when poll_workflow returns status=AWAITING_INPUT !!
    !! DO NOT call for Capability Generator!!

    Args:
        session_id: The session_id from start_session
        response:   Your response as a string (JSON-encoded array/object for structured inputs)
    """
    if not response or not response.strip():
        raise ValueError("response must be a non-empty string.")
    if len(response) > 50000:
        raise ValueError("response payload exceeds 50,000 character limit.")

    db = get_db(os.environ.get("RAW_EVENTS_DB_NAME", "speakmanai_db"))
    COLLECTION = os.environ.get("RAW_EVENTS_COLLECTION", "events_raw")

    doc = await db[COLLECTION].find_one({"session_id": session_id})
    if not doc:
        raise RuntimeError(f"Session {session_id} not found.")

    mcp_input_config = await _get_mcp_pause_config(doc)
    if not mcp_input_config:
        current_status = doc.get("current_status", "UNKNOWN")
        raise RuntimeError(
            f"Session {session_id} is not waiting for input (status: {current_status}). "
            "Only call submit_response when poll_workflow returns status=AWAITING_INPUT."
        )

    step_index = mcp_input_config.get("step_index", 1)
    agent_id = mcp_input_config.get("agent_id", "MCP_INPUT_REQUIRED")

    # Push resume event to internal local queue
    await workflow_queue.put({
        "action": "resume",
        "session_id": session_id,
        "content": response,
        "current_step_index": step_index,
        "agentId": agent_id,
        "user_id": "local_user"
    })

    # Clear AWAITING_INPUT state so poll_workflow reflects IN_PROGRESS immediately
    await db[COLLECTION].update_one(
        {"session_id": session_id},
        {"$set": {"current_status": "IN_PROGRESS"}},
    )

    return json.dumps({
        "ok": True,
        "session_id": session_id,
        "message": (
            "Response accepted. The workflow is now continuing. "
            "Call poll_workflow(session_id) every 15-30 seconds until status is COMPLETED."
        ),
    })



# ─────────────────────────────────────────────
# Tool 5: poll_workflow
# ─────────────────────────────────────────────
@mcp.tool(name="poll_workflow", annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True))
async def poll_workflow(session_id: str, mcp_ctx: Context) -> str:
    """
    Check the status of a running workflow and retrieve outputs when complete.

    Args:
        session_id: The session_id returned by start_session

    Returns:
        status (IN_PROGRESS | AWAITING_CAPABILITIES | COMPLETED | FAILED),
        completed_steps list, and outputs dict keyed by agent_id when COMPLETED.
    """
    db = get_db(os.environ.get("RAW_EVENTS_DB_NAME", "speakmanai_db"))
    COLLECTION = os.environ.get("RAW_EVENTS_COLLECTION", "events_raw")

    doc = await db[COLLECTION].find_one({"session_id": session_id})
    if not doc:
        return json.dumps({
            "session_id": session_id,
            "status": "IN_PROGRESS",
            "completed_steps": [],
            "message": "Session is starting — wait 30 seconds and poll again.",
        }, indent=2)

    current_status = doc.get("current_status", "UNKNOWN")
    events = doc.get("events") or []
    mcp_input_config = await _get_mcp_pause_config(doc) if current_status == "AWAITING_INPUT" else None

    additional_exclusions = set()
    if mcp_input_config:
        additional_exclusions.add(mcp_input_config.get("agent_id", ""))

    human_agents = await _build_human_exclusion_set(events, additional_exclusions)
    agent_index = _aggregate_agent_index(events, human_agents)
    # completed_steps ordered by step index
    completed_steps = [
        aid for aid, _ in sorted(agent_index.items(), key=lambda x: x[1]["index"])
    ]

    # Emit progress notifications — one per completed agent
    wf_def = next((e.get("data", {}).get("workflow_definition", {}) for e in events if e.get("data", {}).get("workflow_definition")), {})
    total_agents = len(wf_def.get("steps", [])) or len(completed_steps)
    for i, agent_id in enumerate(completed_steps):
        await mcp_ctx.report_progress(
            progress=i + 1,
            total=total_agents,
            message=f"[{i + 1}/{total_agents}] {agent_id}",
        )

    is_completed = current_status == "COMPLETED"
    is_failed = current_status in ("FAILED", "ERROR")

    # Determine whether this session is a system workflow (never incremented, so don't decrement).
    session_workflow_type = next(
        (e.get("data", {}).get("workflow_definition", {}).get("workflow_type")
         for e in events if e.get("data", {}).get("workflow_definition")),
        None
    )
    is_system_session = session_workflow_type == "system"

    if is_failed:
        error_msg = doc.get("error_message", "Workflow failed — check agent configuration.")
        raise RuntimeError(f"Workflow {session_id} failed: {error_msg}")

    is_awaiting_input = current_status == "AWAITING_INPUT"

    result = {
        "session_id": session_id,
        "status": current_status,
        "completed_steps": completed_steps,
    }

    if is_awaiting_input and mcp_input_config:
        agent_type = mcp_input_config.get("agent_type", "MCP_INPUT_REQUIRED")
        result["input_required"] = {
            "prompt": mcp_input_config.get("prompt", ""),
            "schema": mcp_input_config.get("schema", {}),
            "context": mcp_input_config.get("context", ""),
            "agent_id": mcp_input_config.get("agent_id", ""),
            "agent_type": agent_type,
        }
        
        if agent_type == "MCP_LLM_DELEGATE":
            result["message"] = (
                "The workflow has paused and is delegating this execution step to YOU (the connected AI assistant). "
                "Do NOT ask the user for this information. Read input_required.prompt, input_required.context, "
                "and input_required.schema. Execute the task yourself using your own capabilities, "
                "then immediately call submit_response(session_id, response) with the result."
            )
        else:
            result["message"] = (
                "Workflow is waiting for human input. Ask the user to provide the information requested in "
                "input_required.prompt. Once they answer, format it according to input_required.schema "
                "and call submit_response(session_id, response)."
            )
    elif is_completed:
        result["message"] = (
            "Workflow complete. Call get_outputs(session_id) to get the manifest of available agent outputs, "
            "then call get_output(session_id, agent_id) for each output you need. "
        )
    else:
        result["message"] = f"Workflow is {current_status}. Poll again in 30-60 seconds."

    return json.dumps(result, indent=2)



def _compile_dependencies_context(agent_doc: dict, events: list) -> str:
    """Compiles a unified context block based on the agent's specific dependencies."""
    dependencies = agent_doc.get("dependencies", []) if agent_doc else []
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


async def _get_mcp_pause_config(doc: dict) -> dict | None:
    """
    Resolve input_required config for a session in AWAITING_INPUT state.

    Reads step_type from the workflow plan and fetches prompt/schema directly
    from the agent doc in the database — no dependency on embedded mcp_input_config.
    """
    events = doc.get("events") or []
    # workflow_definition is carried in every event — read from the first.
    workflow_definition = events[0].get("data", {}).get("workflow_definition", {}) if events else {}
    steps = workflow_definition.get("steps", [])

    # Find the MCP_PAUSE event to get the active step index and the prior output as context.
    mcp_pause_step_index = None
    for event in reversed(events):
        if event.get("attributes", {}).get("event_type") == "MCP_PAUSE":
            try:
                mcp_pause_step_index = int(event.get("attributes", {}).get("current_step_index", -1))
            except (TypeError, ValueError):
                pass
            break

    if mcp_pause_step_index is None:
        return None

    # Find the matching step in the plan and confirm it is a MCP_PAUSE step.
    mcp_pause_step = next(
        (s for s in steps if s.get("step_index") == mcp_pause_step_index),
        None
    )
    if not mcp_pause_step or mcp_pause_step.get("step_type") != "MCP_PAUSE":
        return None
    if not mcp_pause_step.get("agents"):
        return None

    agent_id = mcp_pause_step["agents"][0]

    # Fetch prompt and schema directly from the agent document.
    ops_db = get_db()
    agent_doc = await ops_db["agents"].find_one({"agentId": agent_id})

    agent_type = agent_doc.get("agentType", "MCP_INPUT_REQUIRED") if agent_doc else "MCP_INPUT_REQUIRED"

    # Apply global and agent-level execution mode overrides to the reported agent_type
    settings = await ops_db["settings"].find_one({"_id": "global_config"}) or {}
    exec_mode = settings.get("execution_mode", "auto")
    a_exec_mode = agent_doc.get("executionMode", "auto") if agent_doc else "auto"

    if exec_mode == "force_delegate" and agent_type.startswith("AI_"):
        agent_type = "MCP_LLM_DELEGATE"
    elif exec_mode == "force_background" and agent_type == "MCP_LLM_DELEGATE":
        agent_type = "AI_WORKFLOW"
    elif exec_mode == "auto" and a_exec_mode == "delegate" and agent_type.startswith("AI_"):
        agent_type = "MCP_LLM_DELEGATE"

    context = _compile_dependencies_context(agent_doc, events)
    schema = agent_doc.get("inputSchema", {}) if agent_doc else {}

    return {
        "prompt": agent_doc.get("systemPrompt", "") if agent_doc else "",
        "schema": schema,
        "context": context,
        "step_index": mcp_pause_step_index,
        "agent_id": agent_id,
        "agent_type": agent_type,
    }


async def _build_human_exclusion_set(events: list, additional_exclusions: set = None) -> set:
    """
    Return the set of agent IDs to exclude from output aggregation.
    Excludes HITL pause steps based on their true database agentType.
    """
    human_agents = set()
    if additional_exclusions:
        human_agents.update(a for a in additional_exclusions if a)
        
    if not events:
        return human_agents
        
    agent_ids = list(set(
        e.get("data", {}).get("execution_context", {}).get("source_outputs", {}).get("source_agent_id", "")
        for e in events
    ))
    agent_ids = [a for a in agent_ids if a and not a.startswith("root_planner")]
    
    if agent_ids:
        ops_db = get_db()
        agent_docs = await ops_db["agents"].find({"agentId": {"$in": agent_ids}}).to_list(length=None)
        for a in agent_docs:
            a_type = a.get("agentType", "")
            if (a_type.startswith("MCP_") and a_type != "MCP_LLM_DELEGATE") or a_type == "AI_PLANNER":
                human_agents.add(a["agentId"])
                
    return human_agents


def _aggregate_agent_index(events: list, human_agents: set) -> dict:
    """
    Aggregate agent outputs from events, skipping VALIDATION and validation_retry events.
    Returns a dict keyed by agent_id with metadata and content.
    """
    agent_index = {}
    step_index = 0
    for event in events:
        attrs = event.get("attributes", {})
        if attrs.get("event_type") == "VALIDATION":
            continue
        if attrs.get("validation_retry") == "true":
            continue
        src = event.get("data", {}).get("execution_context", {}).get("source_outputs", {})
        agent_id = src.get("source_agent_id")
        if agent_id:
            # Parallel agents can emit a single event with a comma-separated agent ID string.
            # Split so each agent gets its own entry in the index.
            for aid in [a.strip() for a in agent_id.split(",")]:
                if aid and aid not in human_agents:
                    agent_index[aid] = {
                        "content": src.get("content", ""),
                        "mime_type": src.get("mime_type", "text/plain"),
                        "index": step_index,
                    }
            step_index += 1
    return agent_index


# ─────────────────────────────────────────────
# Tool 6: get_outputs  (manifest — no content)
# ─────────────────────────────────────────────
@mcp.tool(name="get_outputs", annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True))
async def get_outputs(session_id: str, mcp_ctx: Context) -> str:
    """
    Return the manifest of available agent outputs for a completed workflow session.
    Does NOT return content — call get_output(session_id, agent_id) for each output you need.

    Works on any past session_id so you can resume downstream work across conversations.

    Args:
        session_id: The session_id from start_session or poll_workflow

    Returns:
        Session metadata, list of available outputs (agent_id, description, mime_type, index),
        document_capabilities, and suggested next steps.
        Use get_output(session_id, agent_id) to fetch the full content of each output.
    """
    db = get_db(os.environ.get("RAW_EVENTS_DB_NAME", "speakmanai_db"))
    COLLECTION = os.environ.get("RAW_EVENTS_COLLECTION", "events_raw")

    doc = await db[COLLECTION].find_one({"session_id": session_id})
    if not doc:
        raise RuntimeError(f"Session {session_id} not found.")

    current_status = doc.get("current_status", "UNKNOWN")
    if current_status == "CANCELLED":
        raise RuntimeError(
            f"Session {session_id} was cancelled — no outputs are available. "
            "Start a new session to generate a complete workflow."
        )

    events = doc.get("events") or []

    human_agents = await _build_human_exclusion_set(events)
    agent_index = _aggregate_agent_index(events, human_agents)

    # Build manifest — metadata only, no content
    available_outputs = [
        {
            "agent_id": agent_id,
            "description": f"Output from {agent_id}",
            "mime_type": meta["mime_type"],
            "index": meta["index"],
        }
        for agent_id, meta in sorted(agent_index.items(), key=lambda x: x[1]["index"])
    ]

    # Session metadata: title is stored top-level; workflow_id is in event attributes
    title = doc.get("session_title", "")
    workflow_id = (events[0].get("attributes") or {}).get("workflow_id", "") if events else ""

    # Extract document_capabilities from the COMPLETED event (added by capabilities_processor)
    document_capabilities = []
    for event in events:
        if event.get("attributes", {}).get("status") == "COMPLETED":
            dc = event.get("data", {}).get("execution_context", {}).get("source_outputs", {}).get("document_capabilities")
            if dc:
                document_capabilities = dc
            break

    return json.dumps({
        "session_id": session_id,
        "title": title,
        "workflow_id": workflow_id,
        "status": current_status,
        "agent_count": len(available_outputs),
        "available_outputs": available_outputs,
        "document_capabilities": document_capabilities,
        "suggested_next_steps": (
            [
                "Capabilities have been extracted and saved to your local library — no get_output call needed.",
                f"A capability group was {'created' if len(document_capabilities) >= 10 else 'not created (fewer than 10 capabilities saved)'} for this session.",
                "To run a Solution Architecture workflow using these capabilities: call list_workflows, choose an architecture workflow_id, then call start_session with a title and business description.",
            ]
            if "CAPABILITY_GENERATOR" in workflow_id.upper()
            else [
                "Call get_output(session_id, agent_id) for each output you need — fetch only what is relevant to your task.",
                "Primary deliverable: get_output(session_id, 'MCP_TECHNICAL_WRITER_V2') — full Solution Architecture Document in Markdown.",
                "For PowerPoint: fetch MCP_TECHNICAL_WRITER_V2 and MCP_TECHNICAL_VISUALIZATION_SPECIALIST_V1, then use python-pptx.",
                "For dev planning / ADR register: fetch MCP_TECHNICAL_SOLUTION_ARCHITECT_V1 and MCP_BUSINESS_APPLICATION_ARCHITECT_V1.",
                "For compliance report: fetch MCP_COMPLIANCE_OFFICER_V2.",
                "For stakeholder briefing: fetch MCP_BUSINESS_ANALYST_V1.",
                "To save capabilities to your SPEAKMAN.AI library: if document_capabilities is non-empty, call list_workflows to find the Capability Generator, then start_session with a group name as title and the document_capabilities JSON as description.",
            ]
        ),
    }, indent=2)


# ─────────────────────────────────────────────
# Tool 7: get_output  (single agent content)
# ─────────────────────────────────────────────
@mcp.tool(name="get_output", annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True))
async def get_output(session_id: str, agent_id: str) -> str:
    """
    Retrieve the full content of one agent output from a workflow session.
    Call get_outputs(session_id) first to get the list of available agent_ids.

    Args:
        session_id: The session_id from start_session or poll_workflow
        agent_id:   The agent_id from get_outputs available_outputs list
                    (e.g. 'MCP_TECHNICAL_WRITER_V2', 'MCP_COMPLIANCE_OFFICER_V2')

    Returns:
        agent_id, description, mime_type, and the full content string for that agent.
    """
    db = get_db(os.environ.get("RAW_EVENTS_DB_NAME", "speakmanai_db"))
    COLLECTION = os.environ.get("RAW_EVENTS_COLLECTION", "events_raw")

    doc = await db[COLLECTION].find_one({"session_id": session_id})
    if not doc:
        raise RuntimeError(f"Session {session_id} not found.")

    if doc.get("current_status") == "CANCELLED":
        raise RuntimeError(
            f"Session {session_id} was cancelled — no outputs are available. "
            "Start a new session to generate a complete workflow."
        )

    events = doc.get("events") or []
    human_agents = await _build_human_exclusion_set(events)
    agent_index = _aggregate_agent_index(events, human_agents)

    if agent_id not in agent_index:
        available = sorted(agent_index.keys())
        raise RuntimeError(
            f"Agent '{agent_id}' not found in session {session_id}. "
            f"Available agent_ids: {available}"
        )

    meta = agent_index[agent_id]
    return json.dumps({
        "session_id": session_id,
        "agent_id": agent_id,
        "description": f"Output from {agent_id}",
        "mime_type": meta["mime_type"],
        "content": meta["content"],
    }, indent=2)


# ─────────────────────────────────────────────
# Tool 8: list_sessions
# ─────────────────────────────────────────────
@mcp.tool(name="list_sessions", annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True))
async def list_sessions(limit: int = 10) -> str:
    """
    List your recent MCP workflow sessions with their current status and session_ids.

    Use this to recover a session_id after a context reset, crash, or new conversation,
    or to check the status of sessions you have started previously.

    Args:
        limit: Number of recent sessions to return (default 10, max 25)

    Returns:
        List of sessions ordered by most recent first, each with:
          session_id     — pass to poll_workflow, get_outputs, get_output, cancel_session
          title          — the title supplied to start_session
          workflow_id    — identifies the workflow type and therefore the correct call chain
          status         — one of:
                           COMPLETED          → call get_outputs then get_output to retrieve content
                           AWAITING_INPUT      → call submit_response to continue the workflow
                           IN_PROGRESS        → call poll_workflow to check progress (poll every 30s)
                           FAILED             → call cancel_session to free your slot, then start_session
                           CANCELLED          → session is closed; start a new session
          awaiting_input — true when status is AWAITING_INPUT (submit_response required)
          created_at     — ISO 8601 timestamp
          completed_steps — number of agent steps that have finished
    """
    db = get_db(os.environ.get("RAW_EVENTS_DB_NAME", "speakmanai_db"))
    COLLECTION = os.environ.get("RAW_EVENTS_COLLECTION", "events_raw")

    limit = min(max(1, limit), 25)

    # Restrict to MCP-originated sessions only — exclude UI workflow sessions.
    mcp_workflow_docs = await db["workflows"].find(
        {"workflowType": "mcp"},
        {"workflowId": 1, "_id": 0}
    ).to_list(length=100)
    mcp_workflow_ids = [w["workflowId"] for w in mcp_workflow_docs]

    # Hardcoded local user for single-container installation
    query = {"owner_id": "local_user"}

    # Filter to MCP sessions only
    if mcp_workflow_ids:
        query["events.0.attributes.workflow_id"] = {"$in": mcp_workflow_ids}

    cursor = db[COLLECTION].find(
        query,
        {"session_id": 1, "session_title": 1, "current_status": 1, "created_at": 1, "events": 1}
    ).sort("created_at", -1).limit(limit)

    sessions = []
    async for doc in cursor:
        events = doc.get("events") or []
        workflow_id_val = ""
        if events:
            workflow_id_val = (events[0].get("attributes") or {}).get("workflow_id", "")
        human_agents = await _build_human_exclusion_set(events)
        agent_index = _aggregate_agent_index(events, human_agents)
        status = doc.get("current_status", "UNKNOWN")
        sessions.append({
            "session_id": doc["session_id"],
            "title": doc.get("session_title", ""),
            "workflow_id": workflow_id_val,
            "status": status,
            "awaiting_input": status in ("AWAITING_INPUT", "AWAITING_CAPABILITIES"),
            "created_at": doc["created_at"].isoformat() if doc.get("created_at") else None,
            "completed_steps": len(agent_index),
        })

    return json.dumps({"sessions": sessions, "count": len(sessions)}, indent=2)


# ─────────────────────────────────────────────
# Tool 9: cancel_session
# ─────────────────────────────────────────────
@mcp.tool(name="cancel_session", annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True))
async def cancel_session(session_id: str) -> str:
    """
    Cancel a running or stalled workflow session and free your concurrency slot.

    Use this if a session is stuck in IN_PROGRESS, failed without updating status,
    or you want to abandon a session without waiting for a timeout.

    Args:
        session_id: The session_id to cancel

    Returns:
        Confirmation that the session was marked CANCELLED and the concurrency slot freed.
    """
    db = get_db(os.environ.get("RAW_EVENTS_DB_NAME", "speakmanai_db"))
    COLLECTION = os.environ.get("RAW_EVENTS_COLLECTION", "events_raw")

    doc = await db[COLLECTION].find_one({"session_id": session_id})
    if not doc:
        raise RuntimeError(f"Session {session_id} not found.")

    current_status = doc.get("current_status", "UNKNOWN")
    if current_status in ("COMPLETED", "CANCELLED"):
        return json.dumps({
            "ok": True,
            "session_id": session_id,
            "message": f"Session already in terminal state: {current_status}. No action taken."
        })

    await db[COLLECTION].update_one(
        {"session_id": session_id},
        {"$set": {"current_status": "CANCELLED"}}
    )

    return json.dumps({
        "ok": True,
        "session_id": session_id,
        "message": "Session cancelled."
    })



# ─────────────────────────────────────────────
# Tool 11: list_projects
# ─────────────────────────────────────────────
@mcp.tool(name="list_projects", annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True))
async def list_projects() -> str:
    """
    List your completed workflow projects from the projects cache.

    Returns projects most recent first, each with session_id, title, status,
    workflow_type, and output summary (section count, has_document, has_items).

    Use get_project(session_id) to fetch a specific project with its sections
    and available on-demand operations.
    """
    return await list_projects_local()


# ─────────────────────────────────────────────
# Tool 12: get_project
# ─────────────────────────────────────────────
@mcp.tool(name="get_project", annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True))
async def get_project(session_id: str) -> str:
    """
    Get a specific project with its section manifest and available on-demand operations.

    The response includes a capabilities block that tells you exactly what operations
    are available for this project:
      can_generate_document — true if sections exist; call generate_document to assemble

    Sections are listed by title/agent — each is a panel of the workflow output.
    Call generate_document to produce derived artefacts on demand.

    Args:
        session_id: The session_id from list_projects or start_session
    """
    try:
        return await get_project_local(session_id)
    except ValueError as e:
        raise RuntimeError(str(e))


# ─────────────────────────────────────────────
# Tool 17: set_execution_mode
# ─────────────────────────────────────────────
@mcp.tool(name="set_execution_mode", annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True))
async def set_execution_mode(mode: str) -> str:
    """
    Set the global execution mode for all workflows.
    Allows you to override the agent configurations dynamically.
    
    Args:
        mode: One of "auto" (default JSON config), "force_delegate" (Claude Code executes all AI steps), or "force_background" (Ollama/Vertex executes all delegated steps).
    """
    if mode not in ("auto", "force_delegate", "force_background"):
        raise ValueError('mode must be "auto", "force_delegate", or "force_background"')
        
    db = get_db()
    await db["settings"].update_one(
        {"_id": "global_config"},
        {"$set": {"execution_mode": mode}},
        upsert=True
    )
    return json.dumps({"ok": True, "message": f"Global execution mode set to '{mode}'."})


def _strip_mongo_export_fields(doc: dict) -> None:
    """Removes MongoDB-specific extended JSON fields and enterprise artifacts from exports."""
    doc.pop("_id", None)
    doc.pop("createdAt", None)
    doc.pop("updatedAt", None)
    doc.pop("tenantId", None)


# ─────────────────────────────────────────────
# Tool 15: import_architecture_plan
# ─────────────────────────────────────────────
@mcp.tool(name="import_architecture_plan", annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True))
async def import_architecture_plan(plan_json: str) -> str:
    """
    Import a complete workflow and its associated agents into the database.
    Accepts the exact JSON output generated by the Workflow Analyst prompt.

    Args:
        plan_json: JSON string containing {"workflow": {...}, "agents": [...]}
    """
    try:
        data = json.loads(plan_json)
        workflow = data.get("workflow")
        agents = data.get("agents", [])

        db = get_db()
        msg_parts = []
        
        if workflow and "workflowId" in workflow:
            _strip_mongo_export_fields(workflow)
            await db["workflows"].update_one(
                {"workflowId": workflow["workflowId"]},
                {"$set": workflow},
                upsert=True
            )
            msg_parts.append(f"workflow '{workflow['workflowId']}'")

        agent_names = []
        for agent in agents:
            if "agentId" not in agent:
                continue

            _strip_mongo_export_fields(agent)

            # Safely append to the workflows array instead of overwriting it
            workflows_list = agent.pop("workflows", [])
            update_op = {"$set": agent}
            if workflows_list:
                update_op["$addToSet"] = {"workflows": {"$each": workflows_list}}

            await db["agents"].update_one(
                {"agentId": agent["agentId"]},
                update_op,
                upsert=True
            )
            agent_names.append(agent["agentId"])
            
        if agent_names:
            msg_parts.append(f"{len(agent_names)} agents")
            
        if not msg_parts:
            raise ValueError("Payload must contain a valid 'workflow' object or 'agents' array.")

        return json.dumps({
            "ok": True,
            "message": f"Successfully imported {' and '.join(msg_parts)}."
        })
    except Exception as e:
        raise RuntimeError(f"import_architecture_plan failed: {str(e)}")


# ─────────────────────────────────────────────
# Tool 18: import_workflow
# ─────────────────────────────────────────────
@mcp.tool(name="import_workflow", annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True))
async def import_workflow(workflow_json: str) -> str:
    """
    Import or update a workflow definition independently (without agents).
    
    Args:
        workflow_json: JSON string of the workflow document. Must contain 'workflowId'.
    """
    try:
        workflow = json.loads(workflow_json)
        if "workflowId" not in workflow:
            raise ValueError("Workflow document must contain 'workflowId'.")
            
        _strip_mongo_export_fields(workflow)
        db = get_db()
        await db["workflows"].update_one(
            {"workflowId": workflow["workflowId"]},
            {"$set": workflow},
            upsert=True
        )
        return json.dumps({"ok": True, "message": f"Workflow '{workflow['workflowId']}' imported successfully."})
    except Exception as e:
        raise RuntimeError(f"import_workflow failed: {str(e)}")


# ─────────────────────────────────────────────
# Tool 19: import_agent
# ─────────────────────────────────────────────
@mcp.tool(name="import_agent", annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True))
async def import_agent(agent_json: str) -> str:
    """
    Import or update a single agent independently. Safely appends to the agent's workflow list.
    
    Args:
        agent_json: JSON string of the agent document. Must contain 'agentId'.
    """
    try:
        agent = json.loads(agent_json)
        if "agentId" not in agent:
            raise ValueError("Agent document must contain 'agentId'.")
            
        _strip_mongo_export_fields(agent)
        workflows_list = agent.pop("workflows", [])
        
        update_op = {"$set": agent}
        if workflows_list:
            update_op["$addToSet"] = {"workflows": {"$each": workflows_list}}
            
        db = get_db()
        await db["agents"].update_one(
            {"agentId": agent["agentId"]},
            update_op,
            upsert=True
        )
        return json.dumps({"ok": True, "message": f"Agent '{agent['agentId']}' imported successfully."})
    except Exception as e:
        raise RuntimeError(f"import_agent failed: {str(e)}")


# ─────────────────────────────────────────────
# Tool 13: generate_document
# ─────────────────────────────────────────────
@mcp.tool(name="generate_document", annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False))
async def generate_document(session_id: str, format: str = "document") -> str:
    """
    Assemble workflow sections into a formatted document via AI.
    The result is cached in the project record — subsequent calls regenerate it.

    Check get_project(session_id).capabilities.can_generate_document before calling.
    Returns 400 if the project has no sections.

    Args:
        session_id: The session_id of the completed project
        format:     Document format hint for the assembler, e.g. "document",
                    "executive_summary", "sad", "report" (default: "document")

    Returns:
        The assembled document as a Markdown string, plus session_id and format.
    """
    try:
        return await generate_document_local(session_id, format)
    except Exception as e:
        raise RuntimeError(f"generate_document failed: {str(e)}")


# ─────────────────────────────────────────────
# Prompt 1: architecture_brief
# ─────────────────────────────────────────────
@mcp.prompt()
async def architecture_brief(system_name: str, business_context: str) -> str:
    """
    Generate a structured prompt to kick off a SPEAKMAN.AI architecture workflow.

    Args:
        system_name: Name of the system or project to architect (e.g. 'Payment Platform', 'Customer Data Hub')
        business_context: Description of the business problem, opportunity, or goal to be addressed
    """
    return (
        f"I need to create a Solution Architecture Document for: {system_name}\n\n"
        f"Business context: {business_context}\n\n"
        "Please complete the full SPEAKMAN.AI architecture workflow:\n"
        "1. Call list_workflows() to see available workflows\n"
        "2. Call start_session() with the architecture workflow_id, "
        f"title='{system_name}', and the business context as the description\n"
        "3. Poll poll_workflow(session_id) every 30 seconds until status is AWAITING_INPUT\n"
        "4. When status is AWAITING_INPUT: read input_required.prompt and input_required.schema\n"
        "5. Build the requested capabilities list matching the schema\n"
        "6. Call submit_response(session_id, json_capabilities_string) to continue the pipeline\n"
        "7. Poll with poll_workflow() every 30-60 seconds until status is COMPLETED\n"
        "8. Call get_outputs() to get the manifest, then get_output() for each agent deliverable\n\n"
        "The primary deliverable is MCP_TECHNICAL_WRITER_V2 — the full Solution Architecture Document in Markdown."
    )


# ─────────────────────────────────────────────
# Prompt 2: resume_session
# ─────────────────────────────────────────────
@mcp.prompt()
async def resume_session() -> str:
    """
    Resume work on a previous SPEAKMAN.AI workflow after a context reset or new conversation.
    Use this when you need to retrieve outputs or continue a session started in a prior conversation.
    """
    return (
        "I need to resume a previous SPEAKMAN.AI architecture workflow.\n\n"
        "Please:\n"
        "1. Call list_sessions() to see my recent sessions with their current status\n"
        "2. Identify the session to continue based on title and status\n"
        "3. Based on the session status:\n"
        "   - COMPLETED: call get_outputs(session_id) to get the manifest, "
        "then get_output(session_id, agent_id) for each agent you need\n"
        "   - AWAITING_INPUT: call poll_workflow to get input_required, then submit_response(session_id, response)\n"
        "   - IN_PROGRESS: call poll_workflow(session_id) every 30-60 seconds until COMPLETED\n"
        "   - FAILED or stuck: call cancel_session(session_id) to free your concurrency slot, "
        "then start a new session with start_session()"
    )




# ─────────────────────────────────────────────
# FastAPI app — Streamable HTTP transport
# ─────────────────────────────────────────────

# Streamable HTTP transport — returns a Starlette app with a single route at /mcp
mcp_asgi = mcp.streamable_http_app()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Seed database on first launch if empty
    if os.environ.get("USE_SQLITE", "").lower() in ("true", "1", "yes"):
        await seed_if_empty()

    # Start the integrated local worker queue
    asyncio.create_task(local_workflow_consumer())
    async with mcp_asgi.router.lifespan_context(app):
        try:
            yield
        finally:
            await close_db()


app = FastAPI(title="SPEAKMAN.AI MCP Server", lifespan=lifespan)


# ─────────────────────────────────────────────
# First-launch database seeding
# ─────────────────────────────────────────────

def _workflows_and_agents_dir() -> Path:
    """Locate the WorkflowsAndAgents directory relative to this file or the PyInstaller bundle."""
    # PyInstaller sets sys._MEIPASS; fall back to relative path for dev
    import sys
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent.parent.parent))
    return base / "WorkflowsAndAgents"


async def seed_if_empty() -> None:
    """Import all bundled workflow JSON files if the database has no workflows yet."""
    db = get_db()
    existing = await db["workflows"].find({"workflowType": "mcp"}).to_list(length=1)
    if existing:
        return

    wf_dir = _workflows_and_agents_dir()
    if not wf_dir.is_dir():
        log.warning(f"WorkflowsAndAgents directory not found at {wf_dir} — skipping seed.")
        return

    count = 0
    for json_path in sorted(wf_dir.glob("*.json")):
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
            workflow = data.get("workflow")
            agents = data.get("agents", [])

            if workflow and "workflowId" in workflow:
                _strip_mongo_export_fields(workflow)
                await db["workflows"].update_one(
                    {"workflowId": workflow["workflowId"]},
                    {"$set": workflow},
                    upsert=True,
                )

            for agent in agents:
                if "agentId" not in agent:
                    continue
                _strip_mongo_export_fields(agent)
                workflows_list = agent.pop("workflows", [])
                update_op = {"$set": agent}
                if workflows_list:
                    update_op["$addToSet"] = {"workflows": {"$each": workflows_list}}
                await db["agents"].update_one(
                    {"agentId": agent["agentId"]}, update_op, upsert=True
                )
            count += 1
            log.info(f"Seeded {json_path.name}")
        except Exception as e:
            log.warning(f"Failed to seed {json_path.name}: {e}")

    log.info(f"Database seeding complete — {count} workflow file(s) imported.")


@app.get("/health")
async def health():
    return {"status": "ok", "service": "speakmanai-mcp-server"}


@app.get("/quit")
async def quit_server():
    import threading, os
    threading.Thread(target=lambda: os._exit(0), daemon=True).start()
    return {"status": "ok", "message": "Shutting down."}


# ─────────────────────────────────────────────
# Setup / configuration endpoints
# ─────────────────────────────────────────────

_CONFIG_PATH = Path.home() / ".speakmanai" / "config.json"

_PROVIDER_MODELS = {
    "gemini":    {"fast": "gemini-2.5-flash",       "advanced": "gemini-2.5-pro"},
    "anthropic": {"fast": "claude-sonnet-4-6",       "advanced": "claude-opus-4-6"},
    "openai":    {"fast": "gpt-4o-mini",             "advanced": "gpt-4o"},
    "ollama":    {"fast": "",                        "advanced": ""},
}

_MCP_CLIENT_PATHS = {
    "claude_desktop": Path(os.environ.get("APPDATA", "")) / "Claude" / "claude_desktop_config.json",
    "claude_code":    Path.home() / ".claude" / "settings.json",
    "cursor":         Path.home() / ".cursor" / "mcp.json",
}


def _read_config() -> dict:
    if _CONFIG_PATH.exists():
        try:
            return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _write_config(cfg: dict) -> None:
    _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def _apply_config_to_env(cfg: dict) -> None:
    mapping = {
        "llm_provider":      "LLM_PROVIDER",
        "gemini_api_key":    "GEMINI_API_KEY",
        "anthropic_api_key": "ANTHROPIC_API_KEY",
        "openai_api_key":    "OPENAI_API_KEY",
        "ollama_base_url":   "OLLAMA_BASE_URL",
        "default_model":     "DEFAULT_MODEL",
        "advanced_model":    "ADVANCED_MODEL",
        "execution_mode":    "EXECUTION_MODE",
    }
    for cfg_key, env_key in mapping.items():
        val = cfg.get(cfg_key)
        if val:
            os.environ[env_key] = str(val)


@app.get("/setup", response_class=HTMLResponse)
async def setup_page():
    """Self-contained onboarding page — no external JS/CSS dependencies."""
    cfg = _read_config()
    return HTMLResponse(
        _build_setup_html(cfg),
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )


@app.get("/api/config")
async def get_config():
    cfg = _read_config()
    # Mask API keys
    for key in ("gemini_api_key", "anthropic_api_key", "openai_api_key"):
        if cfg.get(key):
            cfg[key] = cfg[key][:6] + "••••••••"
    return JSONResponse(cfg)


@app.post("/api/config")
async def post_config(request: Request):
    body = await request.json()
    cfg = _read_config()
    cfg.update({k: v for k, v in body.items() if v is not None})
    _write_config(cfg)
    _apply_config_to_env(cfg)
    return JSONResponse({"ok": True, "message": "Configuration saved."})


@app.get("/api/check-ollama")
async def check_ollama():
    base = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(f"{base}/api/tags")
            resp.raise_for_status()
            data = resp.json()
            models = [
                {
                    "name": m["name"],
                    "parameter_size": m.get("details", {}).get("parameter_size", ""),
                }
                for m in data.get("models", [])
            ]
            return JSONResponse({"available": True, "models": models})
    except Exception:
        return JSONResponse({"available": False, "models": []})


@app.post("/api/setup-mcp-client")
async def setup_mcp_client(request: Request):
    body = await request.json()
    client_name = body.get("client")  # "claude_desktop" | "claude_code" | "cursor"
    port = body.get("port", 8000)

    config_path = _MCP_CLIENT_PATHS.get(client_name)
    if not config_path:
        raise HTTPException(status_code=400, detail=f"Unknown client: {client_name}")

    if not config_path.parent.exists():
        return JSONResponse({"ok": False, "message": f"{client_name} not detected (directory missing)."})

    entry = {"url": f"http://localhost:{port}/mcp"}

    try:
        existing: dict = {}
        if config_path.exists():
            try:
                existing = json.loads(config_path.read_text(encoding="utf-8"))
            except Exception:
                existing = {}

        mcp_servers = existing.get("mcpServers", {})
        mcp_servers["speakmanai"] = entry
        existing["mcpServers"] = mcp_servers

        config_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
        return JSONResponse({"ok": True, "message": f"Added to {client_name}. Restart the client to activate."})
    except Exception as e:
        return JSONResponse({"ok": False, "message": str(e)})


def _build_setup_html(cfg: dict) -> str:
    """Generate the setup page HTML."""
    provider = cfg.get("llm_provider", "gemini")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>SPEAKMAN.AI Setup</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: system-ui, -apple-system, sans-serif; background: #0a0f1a; color: #e2e8f0; min-height: 100vh; display: flex; align-items: center; justify-content: center; padding: 24px; }}
  .card {{ background: #111827; border-radius: 12px; padding: 32px; width: 100%; max-width: 520px; box-shadow: 0 4px 32px rgba(0,0,0,0.6), 0 0 0 1px rgba(0,255,127,0.08); }}
  .brand {{ font-size: 1.5rem; font-weight: 800; color: #00ff7f; letter-spacing: 0.04em; margin-bottom: 4px; }}
  .subtitle {{ color: #64748b; font-size: 0.875rem; margin-bottom: 28px; }}
  h2 {{ font-size: 0.75rem; font-weight: 600; text-transform: uppercase; letter-spacing: 0.08em; color: #475569; margin-bottom: 12px; }}
  .section {{ margin-bottom: 24px; }}
  .divider {{ border: none; border-top: 1px solid #1e293b; margin: 24px 0; }}
  .radio-group {{ display: flex; gap: 8px; flex-wrap: wrap; }}
  .radio-group label {{ cursor: pointer; padding: 8px 16px; border-radius: 8px; border: 2px solid #1e293b; font-size: 0.875rem; transition: all 0.15s; color: #94a3b8; }}
  .radio-group label.active {{ border-color: #00ff7f; background: #021a0d; color: #00ff7f; }}
  .radio-group label:hover:not(.active) {{ border-color: #334155; color: #e2e8f0; }}
  label.field-label {{ display: block; font-size: 0.8rem; color: #64748b; margin-bottom: 4px; margin-top: 12px; }}
  input[type=text], input[type=password], select {{ width: 100%; background: #0a0f1a; border: 1px solid #1e293b; border-radius: 6px; padding: 8px 12px; color: #e2e8f0; font-size: 0.875rem; }}
  input:focus, select:focus {{ outline: none; border-color: #00ff7f; box-shadow: 0 0 0 2px rgba(0,255,127,0.12); }}
  select option {{ background: #111827; }}
  .btn {{ display: inline-flex; align-items: center; gap: 8px; padding: 10px 20px; border-radius: 8px; font-size: 0.875rem; font-weight: 600; cursor: pointer; border: none; transition: all 0.15s; }}
  .btn-primary {{ background: #00ff7f; color: #0a0f1a; width: 100%; justify-content: center; margin-top: 16px; font-weight: 700; }}
  .btn-primary:hover {{ background: #00e070; box-shadow: 0 0 16px rgba(0,255,127,0.3); }}
  .btn-outline {{ background: transparent; border: 2px solid #1e293b; color: #94a3b8; }}
  .btn-outline:hover {{ border-color: #00ff7f; color: #00ff7f; background: #021a0d; }}
  .connect-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }}
  .status-dot {{ width: 8px; height: 8px; border-radius: 50%; display: inline-block; background: #ef4444; }}
  .status-dot.up {{ background: #00ff7f; }}
  .warning {{ background: #1a0f00; border: 1px solid #78350f; border-radius: 6px; padding: 10px 12px; font-size: 0.8rem; color: #fbbf24; margin-top: 8px; }}
  .exec-hint {{ font-size: 0.75rem; color: #475569; margin-top: 6px; min-height: 1.5em; }}
  .toast {{ position: fixed; bottom: 24px; right: 24px; background: #00ff7f; color: #0a0f1a; padding: 12px 20px; border-radius: 8px; font-weight: 700; font-size: 0.875rem; opacity: 0; transition: opacity 0.3s; pointer-events: none; }}
  .toast.show {{ opacity: 1; }}
  .toast.error {{ background: #ef4444; color: #fff; }}
  #ollama-section {{ display: none; }}
  #cloud-section {{ display: none; }}
</style>
</head>
<body>
<div class="card">
  <div class="brand">SPEAKMAN.AI</div>
  <p class="subtitle">Configure your AI provider and connect to your AI assistant.</p>

  <div class="section">
    <h2>Choose your AI Provider</h2>
    <div class="radio-group" id="provider-group">
      <label class="{'active' if provider == 'gemini' else ''}" onclick="setProvider('gemini')"><span>Gemini</span></label>
      <label class="{'active' if provider == 'anthropic' else ''}" onclick="setProvider('anthropic')"><span>Claude</span></label>
      <label class="{'active' if provider == 'openai' else ''}" onclick="setProvider('openai')"><span>OpenAI</span></label>
      <label class="{'active' if provider == 'ollama' else ''}" onclick="setProvider('ollama')"><span>Ollama</span></label>
    </div>
  </div>

  <div id="cloud-section" class="section">
    <label class="field-label">API Key</label>
    <input type="password" id="api-key" placeholder="Paste your API key…">
    <label class="field-label">Default model (fast steps)</label>
    <select id="default-model"></select>
    <label class="field-label">Advanced model (complex reasoning)</label>
    <select id="advanced-model"></select>
  </div>

  <div id="ollama-section" class="section">
    <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px;">
      <span id="ollama-dot" class="status-dot"></span>
      <span id="ollama-status" style="font-size:0.875rem;color:#64748b;">Checking…</span>
    </div>
    <label class="field-label">Base URL</label>
    <input type="text" id="ollama-url" value="http://localhost:11434">
    <label class="field-label">Model</label>
    <select id="ollama-model" onchange="checkModelSize()"><option>Loading…</option></select>
    <div id="ollama-warning" class="warning" style="display:none">
      ⚠ Models under 14B often produce incomplete outputs for multi-step workflows.
      A 14B+ model (~10 GB RAM) is recommended. 32B+ for best quality.
    </div>
  </div>

  <div class="section">
    <label class="field-label">Execution Mode</label>
    <select id="exec-mode" onchange="updateExecHint()">
      <option value="auto">Auto — use each agent's configured model</option>
      <option value="force_delegate">Force Delegate — your AI assistant executes every step</option>
      <option value="force_background">Force Background — run all steps with the LLM above (no assistant handoff)</option>
    </select>
    <div class="exec-hint" id="exec-hint"></div>
  </div>

  <button class="btn btn-primary" onclick="saveConfig()">Save &amp; Apply</button>

  <hr class="divider">

  <div class="section">
    <h2>Connect your AI Assistant</h2>
    <div class="connect-grid">
      <button class="btn btn-outline" onclick="connectClient('claude_desktop')">Claude Desktop</button>
      <button class="btn btn-outline" onclick="connectClient('claude_code')">Claude Code</button>
      <button class="btn btn-outline" onclick="connectClient('cursor')">Cursor</button>
    </div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
const MODELS = {{
  gemini:    {{ fast: ['gemini-2.5-flash','gemini-2.0-flash'], advanced: ['gemini-2.5-pro','gemini-2.5-flash'] }},
  anthropic: {{ fast: ['claude-sonnet-4-6','claude-haiku-4-5-20251001'], advanced: ['claude-opus-4-6','claude-sonnet-4-6'] }},
  openai:    {{ fast: ['gpt-4o-mini','gpt-4o'], advanced: ['gpt-4o','gpt-4o-mini'] }},
  ollama:    {{ fast: [], advanced: [] }},
}};

let currentProvider = '{provider}';

function setProvider(p) {{
  currentProvider = p;
  document.querySelectorAll('#provider-group label').forEach((l,i) => {{
    l.classList.toggle('active', ['gemini','anthropic','openai','ollama'][i] === p);
  }});
  document.getElementById('cloud-section').style.display = p === 'ollama' ? 'none' : 'block';
  document.getElementById('ollama-section').style.display = p === 'ollama' ? 'block' : 'none';
  if (p !== 'ollama') populateModelDropdowns(p);
  if (p === 'ollama') checkOllama();
}}

function populateModelDropdowns(p) {{
  const m = MODELS[p] || {{ fast: [], advanced: [] }};
  const fill = (id, opts) => {{
    const sel = document.getElementById(id);
    sel.innerHTML = opts.map(o => `<option value="${{o}}">${{o}}</option>`).join('');
  }};
  fill('default-model', m.fast);
  fill('advanced-model', m.advanced);
}}

async function checkOllama() {{
  const url = document.getElementById('ollama-url').value;
  const dot = document.getElementById('ollama-dot');
  const status = document.getElementById('ollama-status');
  const sel = document.getElementById('ollama-model');
  try {{
    const r = await fetch('/api/check-ollama');
    const d = await r.json();
    if (d.available) {{
      dot.className = 'status-dot up';
      status.textContent = 'Running';
      sel.innerHTML = d.models.length
        ? d.models.map(m => `<option value="${{m.name}}" data-param-size="${{m.parameter_size}}">${{m.name}}${{m.parameter_size ? ' (' + m.parameter_size + ')' : ''}}</option>`).join('')
        : '<option>No models found — run: ollama pull qwen2.5:14b</option>';
      checkModelSize();
    }} else {{
      dot.className = 'status-dot';
      status.textContent = 'Not running — start Ollama first';
      sel.innerHTML = '<option>Ollama offline</option>';
    }}
  }} catch(e) {{
    dot.className = 'status-dot';
    status.textContent = 'Cannot connect';
  }}
}}

function checkModelSize() {{
  const sel = document.getElementById('ollama-model');
  const warn = document.getElementById('ollama-warning');
  const opt = sel.options[sel.selectedIndex];
  const paramSize = opt ? opt.getAttribute('data-param-size') || '' : '';
  // Use parameter_size from API (e.g. "8B", "14B") — fall back to parsing the name
  const match = paramSize.match(/(\\d+(?:\\.\\d+)?)/i) || (sel.value || '').match(/(\\d+)b/i);
  const size = match ? parseFloat(match[1]) : 99;
  warn.style.display = size < 14 ? 'block' : 'none';
}}

const EXEC_HINTS = {{
  auto:             'Each agent runs with its own configured model. The provider chosen above is used to resolve abstract model tiers (fast / standard / advanced).',
  force_delegate:   'Every AI step pauses and is handed to your connected AI assistant (e.g. Claude Code) to execute instead of the LLM.',
  force_background: 'Any steps configured to delegate to an AI assistant are overridden and run automatically using the provider above — fully hands-off.',
}};

function updateExecHint() {{
  const val = document.getElementById('exec-mode').value;
  document.getElementById('exec-hint').textContent = EXEC_HINTS[val] || '';
}}

async function saveConfig() {{
  const p = currentProvider;
  const body = {{
    llm_provider: p,
    execution_mode: document.getElementById('exec-mode').value,
  }};
  if (p === 'ollama') {{
    body.ollama_base_url = document.getElementById('ollama-url').value;
    body.default_model = document.getElementById('ollama-model').value;
    body.advanced_model = document.getElementById('ollama-model').value;
  }} else {{
    const key = document.getElementById('api-key').value;
    if (key && !key.includes('••')) body[p + '_api_key'] = key;
    body.default_model = document.getElementById('default-model').value;
    body.advanced_model = document.getElementById('advanced-model').value;
  }}
  const r = await fetch('/api/config', {{ method:'POST', headers:{{'Content-Type':'application/json'}}, body: JSON.stringify(body) }});
  const d = await r.json();
  showToast(d.ok ? 'Configuration saved!' : d.message, !d.ok);
}}

async function connectClient(name) {{
  const r = await fetch('/api/setup-mcp-client', {{
    method: 'POST',
    headers: {{'Content-Type':'application/json'}},
    body: JSON.stringify({{ client: name }})
  }});
  const d = await r.json();
  showToast(d.message, !d.ok);
}}

function showToast(msg, isError=false) {{
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = 'toast show' + (isError ? ' error' : '');
  setTimeout(() => t.className = 'toast', 3500);
}}

// Initialise on load
setProvider(currentProvider);
updateExecHint();
</script>
</body>
</html>"""


# Mount MCP handler at root.
# The API gateway preserves /mcp prefix (rewritePrefix: '/mcp'), so upstream receives /mcp/*.
# FastMCP's internal Starlette route is at /mcp, which matches.
app.mount("/", mcp_asgi)
