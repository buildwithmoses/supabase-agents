"""Exact spend per run. ChatOpenRouter puts the USD cost of every call in
response_metadata["cost"]; this callback sums it."""

from __future__ import annotations

import threading
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import LLMResult


class CostTracker(BaseCallbackHandler):
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.cost_usd = 0.0
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0

    def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        for gens in response.generations:
            for gen in gens:
                msg = getattr(gen, "message", None)
                meta = getattr(msg, "response_metadata", None) or {}
                usage = getattr(msg, "usage_metadata", None) or {}
                with self._lock:
                    self.calls += 1
                    self.cost_usd += float(meta.get("cost") or 0.0)
                    self.input_tokens += int(usage.get("input_tokens", 0))
                    self.output_tokens += int(usage.get("output_tokens", 0))

    def summary(self) -> dict:
        return {"model_calls": self.calls, "cost_usd": round(self.cost_usd, 6),
                "input_tokens": self.input_tokens, "output_tokens": self.output_tokens}
