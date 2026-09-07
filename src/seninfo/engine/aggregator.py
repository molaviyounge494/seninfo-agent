"""Aggregation & de-duplication (PRD 4.2.5, design M5 / ACC-07).

Findings sharing the same raw secret (trimmed) inside one report collapse
into a single event whose ``locations`` carry every hit; cross-file
definitions surfaced by LSP are folded in by the deep engine already.
Conservative rule: when the same secret received conflicting verdicts, the
sensitive side wins (nothing is masked by a not_sensitive twin).
"""

from typing import List, Tuple

from seninfo.models import Judgment, NormalizedFinding, Verdict

Pair = Tuple[NormalizedFinding, Judgment]


def aggregate(pairs: List[Pair]) -> List[Judgment]:
    groups: dict = {}
    order: List[str] = []
    for finding, judgment in pairs:
        key = finding.secret_raw.strip() or finding.finding_id
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append((finding, judgment))

    merged: List[Judgment] = []
    for key in order:
        members = groups[key]
        if len(members) == 1:
            merged.append(members[0][1])
            continue
        winner = _winner(members)
        locations = _merge_locations(members)
        note = {
            "type": "aggregation",
            "summary": "聚合 %d 处相同密文命中为单条事件" % len(members),
            "detail": {"merged_hits": len(members), "secret_key": key[:8]},
        }
        result = _clone_with(winner, locations=locations, evidence=winner.evidence + [note])
        merged.append(result)
    return merged


def _winner(members: List[Pair]) -> Judgment:
    best: Judgment = members[0][1]
    for _, judgment in members[1:]:
        if _rank(judgment) > _rank(best):
            best = judgment
    return best


def _rank(j: Judgment) -> tuple:
    # conservative: sensitive > not_sensitive; then higher confidence.
    sensitive = 1 if j.verdict == Verdict.SENSITIVE else 0
    conf = j.confidence if j.confidence is not None else 0.0
    return (sensitive, conf)


def _merge_locations(members: List[Pair]) -> List[dict]:
    seen = set()
    out: List[dict] = []
    for _, judgment in members:
        for loc in judgment.locations:
            key = (loc.get("file"), loc.get("line"))
            if key in seen:
                continue
            seen.add(key)
            out.append(loc)
    return out


def _clone_with(judgment: Judgment, **kwargs) -> Judgment:
    data = dict(
        finding_id=judgment.finding_id,
        verdict=judgment.verdict,
        category=judgment.category,
        confidence=judgment.confidence,
        secret_masked=judgment.secret_masked,
        locations=judgment.locations,
        evidence=judgment.evidence,
        tool=judgment.tool,
        detector=judgment.detector,
        verified=judgment.verified,
        entropy=judgment.entropy,
        engine_version=judgment.engine_version,
        rule_version=judgment.rule_version,
    )
    data.update(kwargs)
    from seninfo.models import Judgment as J

    return J(**data)
