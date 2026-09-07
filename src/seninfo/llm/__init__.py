"""LLM capability layer: client + schema validation + prompt assembly."""

from seninfo.llm.client import (
    ChatClient,
    LLMError,
    LLMHttpError,
    LLMProtocolError,
    LLMTimeoutError,
)
from seninfo.llm.schema import (
    ALLOWED_TOOLS,
    CriticCall,
    JudgeCall,
    JudgeSubmit,
    JudgeToolRequest,
    SchemaValidationError,
    parse_critic,
    parse_judge_call,
)

__all__ = [
    "ChatClient", "LLMError", "LLMHttpError", "LLMProtocolError", "LLMTimeoutError",
    "ALLOWED_TOOLS", "CriticCall", "JudgeCall", "JudgeSubmit", "JudgeToolRequest",
    "SchemaValidationError", "parse_critic", "parse_judge_call",
]
