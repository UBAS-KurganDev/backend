"""Per-search diagnostics owned by one planner invocation."""

from collections import Counter
from dataclasses import dataclass, field


@dataclass
class SearchDiagnostics:
    rejected: Counter[str] = field(default_factory=Counter)
    warnings: list[str] = field(default_factory=list)
    complete: bool = True
    evaluated: int = 0

    def partial(self, message: str) -> None:
        self.complete = False
        self.warnings.append(message)
