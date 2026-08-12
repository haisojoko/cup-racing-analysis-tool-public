"""Cup Racing Analysis Tool — a conversational, fact-grounded analysis partner
for the Cup Racing league coordinator.

Wraps the deterministic `cup-racing-insights` data layer (DuckDB + detectors +
scoring) in an LLM agent that discusses the data, gives grounded opinions, reads
driver psychology from behavioral signals plus recorded qualitative sources, and
conservatively maintains per-driver profiles.
"""

__version__ = "0.1.0"
