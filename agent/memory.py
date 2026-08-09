"""Two-tier memory.

**Tier 1 — working memory.** The conversation turns and tool results of one
investigation. Lives in the LangGraph state, dies with the run. It's what
lets the agent build on its own previous query instead of restarting.

**Tier 2 — episodic memory.** Closed incidents, persisted as JSON on disk
and recalled at the start of a later investigation. It's what lets the agent
say "this instance did the same thing last Tuesday, and the cause was X."

Why files and structured recall rather than a vector database: recall here
is naturally *filtered*, not fuzzy — the question is always "what happened
to THIS instance, or to this engine, on this SLI, before?", which is an
exact-match query over a handful of labels. At this incident volume, filter
+ recency beats an embedding index, and it adds no service, no model, and no
extra failure mode. Revisit if incidents ever reach the thousands, where
"find me something semantically similar to this narrative" starts to earn a
real index.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import config


@dataclass
class Incident:
    instance_id: str
    engine: str
    trigger: str                      # what opened the investigation
    summary: str                      # one-line what happened
    root_cause: str
    evidence: list[str] = field(default_factory=list)
    breached_slis: list[str] = field(default_factory=list)
    recommendation: str = ""
    opened_at: float = field(default_factory=time.time)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    def headline(self) -> str:
        age_h = max(0.0, (time.time() - self.opened_at) / 3600)
        when = f"{age_h:.1f}h ago" if age_h < 48 else f"{age_h / 24:.1f}d ago"
        slis = ",".join(self.breached_slis) or "n/a"
        return f"[{when}] {self.instance_id} ({slis}): {self.summary} — cause: {self.root_cause}"


class EpisodicMemory:
    def __init__(self, directory: str | Path = config.MEMORY_DIR):
        self.dir = Path(directory)

    def _ensure(self) -> bool:
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            return True
        except OSError:
            return False  # read-only volume: degrade to no memory, don't crash

    def save(self, incident: Incident) -> Path | None:
        if not self._ensure():
            return None
        path = self.dir / f"{int(incident.opened_at)}-{incident.instance_id}-{incident.id}.json"
        path.write_text(json.dumps(asdict(incident), indent=2), encoding="utf-8")
        return path

    def all(self) -> list[Incident]:
        if not self.dir.exists():
            return []
        out = []
        for path in self.dir.glob("*.json"):
            try:
                out.append(Incident(**json.loads(path.read_text(encoding="utf-8"))))
            except (json.JSONDecodeError, TypeError, OSError):
                continue  # a corrupt memory shouldn't break recall
        return sorted(out, key=lambda i: i.opened_at, reverse=True)

    def recall(
        self,
        instance_id: str,
        engine: str | None = None,
        breached_slis: list[str] | None = None,
        limit: int = config.MEMORY_RECALL_LIMIT,
    ) -> list[Incident]:
        """Most relevant prior incidents, most-relevant-first.

        Scored rather than filtered so a same-engine precedent still surfaces
        when this exact instance has no history — which is the common case
        early on, and exactly when precedent is most useful.
        """
        wanted = set(breached_slis or [])
        scored: list[tuple[float, Incident]] = []
        for inc in self.all():
            score = 0.0
            if inc.instance_id == instance_id:
                score += 10
            elif engine and inc.engine == engine:
                score += 4
            else:
                continue  # unrelated engine — no precedent value
            if wanted and wanted & set(inc.breached_slis):
                score += 3
            # Gentle recency tiebreak; never outweighs an instance match.
            score += max(0.0, 2.0 - (time.time() - inc.opened_at) / 86400 * 0.1)
            scored.append((score, inc))
        scored.sort(key=lambda p: p[0], reverse=True)
        return [inc for _, inc in scored[:limit]]
