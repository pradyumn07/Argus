"""The RCA agent: a LangGraph state machine over Gemini function calling.

    recall  ->  investigate  ->  draft_rca  ->  remember

`recall` pulls precedent from episodic memory (tier 2). `investigate` runs
the tool loop — the model queries Mimir/Loki/Tempo until it has enough, with
turns accumulating in working memory (tier 1). `draft_rca` forces a
structured verdict. `remember` writes the incident back to tier 2.

Why the tool loop is hand-written rather than the SDK's automatic function
calling: each tool call is wrapped in an OpenTelemetry span, so the agent's
own investigation shows up in Tempo as a trace right next to the collector
polls it is reasoning about. Automatic calling hides that loop.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, TypedDict

from google import genai
from google.genai import types
from langgraph.graph import END, StateGraph
from opentelemetry import trace

from . import config, tools
from .memory import EpisodicMemory, Incident

log = logging.getLogger("argus.agent")
tracer = trace.get_tracer("argus-agent")

SYSTEM_PROMPT = """You are the SRE on call for Argus, which monitors a fleet of \
databases (Postgres, MySQL, MongoDB, Redis) running both locally in containers and \
on free-tier cloud providers.

You have read-only access to three telemetry backends, and they answer different \
questions:
  - Mimir (query_metrics, PromQL) — WHAT changed and WHEN.
  - Loki (query_logs, LogQL) — WHY, in the collector's own words. Every poll logs a \
status_reason; unreachable targets log the driver exception by name.
  - Tempo (query_traces, TraceQL) — the individual poll, as a span.
Use get_slo_context before judging any number: thresholds are engine-specific, and \
several hosted targets run deliberately widened budgets.

Method: establish what changed from metrics, then find the explanation in logs, then \
confirm on a specific trace if it is still ambiguous. Ground every claim in a value \
you actually retrieved — never assert a number you did not query.

Distinguish causes that matter. "Unreachable: AuthenticationError" is a credential \
problem, not a database problem. A cross-region target sitting at its normal 300ms is \
not an incident. A local container that jumped from 2ms to 500ms is.

Alerts carry an absolute timestamp for when the deviation happened. Query around THAT \
time, not around now — an investigation usually starts minutes after the event, and a \
short window anchored on the present will miss it entirely. If a reported signal does \
not appear, widen the window and use max_over_time before concluding it never \
happened: a spike lasting seconds is invisible to a query whose step is coarser than \
the spike.

Prior incidents are hypotheses to check, never evidence. Do not cite a past incident as \
proof about the present, and never let one raise your confidence — an earlier wrong \
conclusion would otherwise compound into a confidently wrong one.

If the evidence does not support a confident root cause, say so plainly and state what \
you would need. A wrong confident answer is worse than an honest uncertain one."""

RCA_INSTRUCTION = """Write the incident record now, as JSON only — no prose around it, \
no markdown fence.

{
  "summary":        "one sentence: what happened, to which instance",
  "root_cause":     "the cause you can defend from the evidence, or an explicit \
statement that it is undetermined and why",
  "root_cause_determined": true if you identified an actual cause, false if it \
remains undetermined. False is a perfectly good answer — say it here, not only in prose,
  "evidence":       ["specific retrieved facts — values, log lines, trace ids"],
  "breached_slis":  ["latency_ms" | "err_rate" | "conn_pct" | "storage_pct" | \
"cache_hit_ratio" | "availability"],
  "recommendation": "the next concrete action, or 'no action needed' with the reason",
  "confidence":     "high" | "medium" | "low"
}"""


class State(TypedDict, total=False):
    instance_id: str
    engine: str
    trigger: str
    precedent: list[str]
    transcript: list[Any]      # tier-1 working memory
    tool_calls: int
    rca: dict
    incident_id: str
    error: str


@dataclass
class AgentDeps:
    client: genai.Client
    memory: EpisodicMemory
    model: str = config.MODEL
    trace_log: list[str] = field(default_factory=list)
    # Optional progress sink. The CLI leaves it None; the UI passes a callback
    # so an investigation can be streamed to the browser as it happens rather
    # than appearing all at once when it finishes.
    on_event: Any = None

    def emit(self, kind: str, **payload: Any) -> None:
        if self.on_event:
            try:
                self.on_event({"type": kind, **payload})
            except Exception:  # a broken UI must never fail an investigation
                pass


NO_KEY_MESSAGE = (
    f"{config.API_KEY_ENV} is not set. Get a free key at "
    "https://aistudio.google.com/apikey and put it in .env"
)


def make_client(required: bool = True) -> genai.Client | None:
    """Build the Gemini client, or None when no key is configured.

    `required=False` lets the watch loop keep running detection without a
    key instead of crash-looping — anomaly detection needs no model at all,
    so the useful half of the service should not be held hostage to a
    missing credential. RCA switches on as soon as the key appears.
    """
    key = os.getenv(config.API_KEY_ENV, "").strip()
    if not key:
        if required:
            raise SystemExit(NO_KEY_MESSAGE)
        return None
    return genai.Client(api_key=key)


def _generate(deps: AgentDeps, contents: list, *, use_tools: bool) -> types.GenerateContentResponse:
    """One model call, with backoff for free-tier rate limits."""
    cfg = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        max_output_tokens=config.MAX_OUTPUT_TOKENS,
        # Manual loop — see module docstring.
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        tools=tools.TOOLS if use_tools else None,
    )

    delay = config.RETRY_BASE_DELAY_S
    last: Exception | None = None
    for attempt in range(config.RETRY_ATTEMPTS):
        try:
            return deps.client.models.generate_content(
                model=deps.model, contents=contents, config=cfg
            )
        except Exception as exc:  # SDK raises typed errors, but free tier also 429s
            text = str(exc)
            retryable = any(s in text for s in ("429", "RESOURCE_EXHAUSTED", "503", "UNAVAILABLE"))
            if not retryable or attempt == config.RETRY_ATTEMPTS - 1:
                raise
            last = exc
            log.warning("model call retry %d after %.0fs: %s", attempt + 1, delay, text[:120])
            time.sleep(delay)
            delay *= 2
    raise last  # unreachable, but keeps the type checker honest


# ── Nodes ─────────────────────────────────────────────────────────────

def node_recall(state: State, deps: AgentDeps) -> State:
    past = deps.memory.recall(
        instance_id=state["instance_id"],
        engine=state.get("engine"),
        breached_slis=[],
    )
    lines = [p.headline() for p in past]
    if lines:
        log.info("recalled %d prior incident(s)", len(lines))
    deps.emit("recall", count=len(lines), items=lines)
    return {"precedent": lines}


def node_investigate(state: State, deps: AgentDeps) -> State:
    opening = [f"Investigate this signal on `{state['instance_id']}`:\n\n{state['trigger']}"]
    if state.get("precedent"):
        opening.append(
            "\nPrior incidents on this instance or engine (episodic memory) — treat as "
            "hypotheses to check, not as fact:\n"
            + "\n".join(f"- {p}" for p in state["precedent"])
        )
    opening.append(
        "\nInvestigate with the tools, then stop and report. Do not guess before querying."
    )

    contents: list = [types.Content(role="user", parts=[types.Part.from_text(text="\n".join(opening))])]
    calls_made = 0

    for round_no in range(config.MAX_TOOL_ROUNDS):
        try:
            response = _generate(deps, contents, use_tools=True)
        except Exception as exc:
            return {"transcript": contents, "tool_calls": calls_made,
                    "error": f"model call failed: {type(exc).__name__}: {exc}"}

        candidate = (response.candidates or [None])[0]
        if candidate is None or candidate.content is None:
            return {"transcript": contents, "tool_calls": calls_made,
                    "error": f"empty response (finish_reason={getattr(candidate, 'finish_reason', None)})"}

        contents.append(candidate.content)
        fn_calls = response.function_calls or []
        if not fn_calls:
            log.info("investigation complete after %d tool call(s)", calls_made)
            deps.emit("investigated", tool_calls=calls_made)
            break

        result_parts = []
        for fc in fn_calls:
            args = dict(fc.args or {})
            with tracer.start_as_current_span(f"agent.tool.{fc.name}") as span:
                span.set_attribute("argus.instance_id", state["instance_id"])
                span.set_attribute("argus.tool", fc.name or "")
                span.set_attribute("argus.tool_args", json.dumps(args, default=str)[:900])
                payload = tools.call(fc.name or "", args)
                span.set_attribute("argus.tool_error", "error" in payload)
            calls_made += 1
            deps.trace_log.append(f"{fc.name}({json.dumps(args, default=str)[:120]})")
            log.info("  [%d] %s -> %s", round_no + 1, fc.name, tools.preview(payload))
            deps.emit(
                "tool",
                round=round_no + 1,
                name=fc.name,
                args=args,
                ok="error" not in payload,
                preview=tools.preview(payload, limit=400),
            )
            result_parts.append(types.Part.from_function_response(name=fc.name or "", response=payload))

        contents.append(types.Content(role="user", parts=result_parts))
    else:
        log.warning("hit MAX_TOOL_ROUNDS (%d) — drafting with what we have",
                    config.MAX_TOOL_ROUNDS)

    return {"transcript": contents, "tool_calls": calls_made}


def node_draft_rca(state: State, deps: AgentDeps) -> State:
    if state.get("error"):
        return {}
    contents = list(state["transcript"])
    contents.append(types.Content(role="user", parts=[types.Part.from_text(text=RCA_INSTRUCTION)]))
    try:
        response = _generate(deps, contents, use_tools=False)
    except Exception as exc:
        return {"error": f"RCA drafting failed: {type(exc).__name__}: {exc}"}

    raw = (response.text or "").strip()
    rca = _parse_json(raw)
    if rca is None:
        return {"error": "model did not return parseable RCA JSON", "rca": {"raw": raw}}
    deps.emit("rca", rca=rca)
    return {"rca": rca}


def node_remember(state: State, deps: AgentDeps) -> State:
    rca = state.get("rca") or {}
    if state.get("error") or not rca.get("root_cause"):
        return {}  # never persist a failed or empty investigation as precedent
    if rca.get("root_cause_determined") is False:
        # An unresolved investigation is not precedent — it is an open question.
        # Persisting it lets a wrong "not observed" come back as corroboration
        # and raise confidence on the next run. Observed happening: a bad RCA
        # was recalled and cited as evidence, turning low confidence into high.
        log.info("root cause undetermined — not saving as precedent")
        return {}
    incident = Incident(
        instance_id=state["instance_id"],
        engine=state.get("engine", "?"),
        trigger=state["trigger"],
        summary=rca.get("summary", ""),
        root_cause=rca.get("root_cause", ""),
        evidence=list(rca.get("evidence") or []),
        breached_slis=list(rca.get("breached_slis") or []),
        recommendation=rca.get("recommendation", ""),
    )
    path = deps.memory.save(incident)
    if path:
        log.info("incident %s saved to %s", incident.id, path)
    deps.emit("saved", incident_id=incident.id)
    return {"incident_id": incident.id}


def _parse_json(text: str) -> dict | None:
    if text.startswith("```"):
        text = text.split("```")[1] if "```" in text[3:] else text[3:]
        text = text.removeprefix("json").strip()
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            return None
        try:
            parsed = json.loads(text[start:end + 1])
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            return None


def build(deps: AgentDeps):
    g = StateGraph(State)
    g.add_node("recall", lambda s: node_recall(s, deps))
    g.add_node("investigate", lambda s: node_investigate(s, deps))
    g.add_node("draft_rca", lambda s: node_draft_rca(s, deps))
    g.add_node("remember", lambda s: node_remember(s, deps))
    g.set_entry_point("recall")
    g.add_edge("recall", "investigate")
    g.add_edge("investigate", "draft_rca")
    g.add_edge("draft_rca", "remember")
    g.add_edge("remember", END)
    return g.compile()


def investigate(deps: AgentDeps, instance_id: str, trigger: str, engine: str = "") -> State:
    """Run one full investigation. The whole run is a single Tempo trace."""
    with tracer.start_as_current_span("agent.investigation") as span:
        span.set_attribute("argus.instance_id", instance_id)
        span.set_attribute("argus.trigger", trigger[:400])
        result = build(deps).invoke({
            "instance_id": instance_id,
            "engine": engine,
            "trigger": trigger,
            "transcript": [],
            "tool_calls": 0,
        })
        span.set_attribute("argus.tool_calls", result.get("tool_calls", 0))
        span.set_attribute("argus.failed", bool(result.get("error")))
        return result
