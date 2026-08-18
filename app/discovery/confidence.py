"""
Confidence-scored shortlists.

IMPORTANT — honesty about what this is and isn't:

The ideation doc cited real conformal-prediction research (Angelopoulos
et al., Vovk's Mondrian Confidence Machine) as inspiration for calibrated
uncertainty. True conformal prediction requires a labeled calibration set
— historical sites with KNOWN outcomes — to produce statistically valid
coverage guarantees. This system has no such labeled outcome data (nobody
does yet; siting outcomes take months/years to resolve).

So what's actually implemented here is a **transparent heuristic** that
aggregates the per-field `confidence` (high/medium/low/unknown) and
`status` (ok/absent/failed) that Mireye already returns on every field,
into one site-level trust score. It is explicitly NOT a calibrated
probability. Treat `confidence_score` as a ranking signal, not a
probability of anything.

The upgrade path to real conformal calibration, once you have outcome
data: log (predicted_score, actual_outcome) pairs via the workspace
engine's observation history, then swap `score_site()`'s weighting scheme
for a proper conformal calibration step (e.g. Mondrian buckets keyed by
field type). The hook point is marked below.
"""
from __future__ import annotations

CONFIDENCE_WEIGHTS = {"high": 1.0, "medium": 0.6, "low": 0.3, "unknown": 0.1}
STATUS_WEIGHTS = {"ok": 1.0, "absent": 0.5, "failed": 0.0}


def score_site(fields: dict, field_weights: dict[str, float] | None = None) -> dict:
    """
    fields: the `fields` dict from a /v1/fetch or /v1/fetch/batch entry.
    field_weights: optional per-field importance (default: uniform).

    Returns a breakdown, not just a number — so a caller can see WHY a site
    scored low (a critical field failed vs. every field just being medium
    confidence are very different situations to hand an agent).
    """
    field_weights = field_weights or {}
    total_weight, weighted_sum = 0.0, 0.0
    breakdown = []

    for name, record in fields.items():
        w = field_weights.get(name, 1.0)
        status = record.get("status", "ok")
        conf = record.get("confidence", "unknown")
        status_component = STATUS_WEIGHTS.get(status, 0.5)
        conf_component = CONFIDENCE_WEIGHTS.get(conf, 0.1)
        field_score = status_component * conf_component
        weighted_sum += field_score * w
        total_weight += w
        breakdown.append({
            "field": name, "status": status, "confidence": conf,
            "field_score": round(field_score, 3), "weight": w,
        })

    overall = round(weighted_sum / total_weight, 3) if total_weight else 0.0
    weakest = sorted(breakdown, key=lambda b: b["field_score"])[:5]

    return {
        "confidence_score": overall,           # 0.0-1.0, a ranking signal — NOT a probability
        "is_calibrated": False,                # flips true once real conformal calibration is wired in
        "weakest_fields": weakest,
        "field_breakdown": breakdown,
    }


def rank_shortlist_by_confidence(shortlist: list[dict], field_weights: dict[str, float] | None = None) -> list[dict]:
    """Attach a confidence_score to each shortlisted site and sort descending."""
    scored = []
    for site in shortlist:
        s = score_site(site.get("fields", {}), field_weights)
        scored.append({**site, **s})
    return sorted(scored, key=lambda s: s["confidence_score"], reverse=True)