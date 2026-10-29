"""One structured-output call with retries on transient errors (429, 5xx, timeouts). Never fakes an answer."""

from __future__ import annotations

import time

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel

from ..core.config import get_model, judge_model_id

_TRANSIENT = {"APIConnectionError", "APITimeoutError", "RateLimitError", "InternalServerError", "ServiceUnavailableError",
              "TimeoutError", "ConnectionError", "ReadTimeout", "ConnectTimeout", "RemoteProtocolError"}


def _transient(e: BaseException) -> bool:
    status = getattr(e, "status_code", None) or getattr(getattr(e, "response", None), "status_code", None)
    if isinstance(status, int):
        return status == 429 or status >= 500
    return any(c.__name__ in _TRANSIENT for c in type(e).__mro__)


def structured(schema: type[BaseModel], system: str, user: str, *, judge: bool = False, callbacks=None, max_tokens: int = 4000) -> BaseModel:
    model = get_model(judge_model_id() if judge else None, max_tokens=max_tokens)
    runnable = model.with_structured_output(schema, method="json_schema")
    last = None
    for attempt in range(4):
        try:
            out = runnable.invoke([SystemMessage(system), HumanMessage(user)], config={"callbacks": callbacks or []})
            if out is None:
                raise ValueError("empty structured output")
            return out
        except Exception as e:  # noqa: BLE001
            last = e
            if attempt < 3 and (_transient(e) or isinstance(e, ValueError)):
                time.sleep(2 * (attempt + 1)); continue
            raise
    raise last  # pragma: no cover
