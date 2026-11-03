"""Research types independent of retired external AI transports."""
from dataclasses import dataclass
from datetime import datetime


class ResearchUnavailable(ValueError):
    """Fail closed instead of inventing an inference result."""


@dataclass(frozen=True)
class AiResearch:
    asset: str
    model: str
    generated_at: datetime
    direction: str
    confidence: str
    thesis: str
    risk: str
    source_count: int
    evidence_sha256: str
    from_cache: bool = False
