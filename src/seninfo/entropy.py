"""Shannon entropy over a secret's characters.

PRD decision: entropy is only an *auxiliary feature* fed to the LLM, never a
standalone short-circuit (short real tokens may have low entropy).
"""

import math
from collections import Counter


def shannon_entropy(text: str) -> float:
    """Character-level Shannon entropy in bits. 0.0 for empty/single-char."""
    if not text:
        return 0.0
    counts = Counter(text)
    total = len(text)
    return -sum((c / total) * math.log2(c / total) for c in counts.values())
