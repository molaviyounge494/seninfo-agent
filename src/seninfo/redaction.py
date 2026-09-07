"""Credential redaction (PRD 4.1.2 / Appendix A).

Plaintext secrets exist only transiently in process memory. Every other
carrier (logs, prompts, storage, exports) receives the masked form.
"""


def mask_secret(raw: str, head: int = 4, tail: int = 4) -> str:
    """Mask a secret keeping `head` + `tail` boundary chars.

    ``AKIAIOSFODNN7EXAMPLE`` -> ``AKIA...[MASKED]...MPLE``
    Very short values are fully masked (never leak the whole secret).
    """
    if not raw:
        return ""
    if len(raw) <= head + tail:
        return "[REDACTED]"
    return raw[:head] + "[MASKED]" + raw[-tail:]
