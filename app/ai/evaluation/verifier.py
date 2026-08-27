from __future__ import annotations

import time
from typing import Any

from app.ai.schemas.orchestration import (
    AgentObservation,
    Claim,
    ClaimVerification,
    VerificationResult,
    VerificationState,
)
from app.infrastructure.observability import span


_RANK = {
    VerificationState.VERIFIED: 0,
    VerificationState.PARTIALLY_VERIFIED: 1,
    VerificationState.NEEDS_HUMAN_REVIEW: 2,
    VerificationState.CONFLICTED: 3,
    VerificationState.UNSUPPORTED: 4,
}


class VerificationEngine:
    """Checks model claims against typed evidence and deterministic outcomes."""

    def verify(self, observation: AgentObservation, context: dict[str, Any]) -> VerificationResult:
        evidence = {item.get("evidence_id"): item for item in context.get("evidence_items", [])}
        outcomes = context.get("deterministic_outcomes", {})
        with span("ai.verification", **{"task.id": observation.task_id, "agent.role": observation.agent_role.value}):
            claims = [self._verify_claim(claim, evidence, outcomes, context.get("now", time.time())) for claim in observation.claims]
        state = max((item.state for item in claims), key=lambda item: _RANK[item], default=VerificationState.VERIFIED)
        required = sorted({reason.removeprefix("missing:") for item in claims for reason in item.reasons if reason.startswith("missing:")})
        return VerificationResult(
            task_id=observation.task_id,
            state=state,
            claims=claims,
            replan_required=state in {VerificationState.UNSUPPORTED, VerificationState.CONFLICTED},
            required_evidence=required,
        )

    @staticmethod
    def _verify_claim(
        claim: Claim,
        evidence: dict[str | None, dict[str, Any]],
        outcomes: dict[str, str],
        now: float,
    ) -> ClaimVerification:
        reasons: list[str] = []
        records = []
        for evidence_id in claim.evidence_ids:
            record = evidence.get(evidence_id)
            if record is None:
                reasons.append(f"missing:{evidence_id}")
            else:
                records.append(record)
        if not claim.evidence_ids:
            reasons.append("No supporting evidence was cited.")
        if reasons:
            return ClaimVerification(
                claim_id=claim.claim_id, state=VerificationState.UNSUPPORTED, reasons=reasons, evidence_ids=claim.evidence_ids
            )
        if any(record.get("conflict_state") not in {None, "NONE", "CLEAR"} for record in records):
            return ClaimVerification(
                claim_id=claim.claim_id,
                state=VerificationState.CONFLICTED,
                reasons=["Supporting evidence is conflicted."],
                evidence_ids=claim.evidence_ids,
            )
        stale = [item["evidence_id"] for item in records if not _fresh(item, now)]
        if stale:
            return ClaimVerification(
                claim_id=claim.claim_id,
                state=VerificationState.UNSUPPORTED,
                reasons=[f"Evidence is stale: {', '.join(stale)}"],
                evidence_ids=claim.evidence_ids,
            )
        if claim.required_scope and any(not _scope_compatible(claim.required_scope, item.get("scope")) for item in records):
            return ClaimVerification(
                claim_id=claim.claim_id,
                state=VerificationState.UNSUPPORTED,
                reasons=["Evidence scope cannot prove the claim."],
                evidence_ids=claim.evidence_ids,
            )
        if claim.asserted_outcome in {"PASS", "FAIL"}:
            actual = outcomes.get(claim.requirement_id or "")
            if actual != claim.asserted_outcome:
                return ClaimVerification(
                    claim_id=claim.claim_id,
                    state=VerificationState.UNSUPPORTED,
                    reasons=["The asserted outcome is not present in deterministic evaluation."],
                    evidence_ids=claim.evidence_ids,
                )
        strengths = {item.get("semantic_strength") for item in records}
        if strengths & {"INSUFFICIENT_EVIDENCE", "UNSUPPORTED_SEMANTICS"}:
            state, why = VerificationState.UNSUPPORTED, "Evidence semantics do not support the claim."
        elif any(item.get("human_review_required") for item in records):
            state, why = VerificationState.NEEDS_HUMAN_REVIEW, "The source requires human review."
        elif "SOURCE_BACKED_SIGNAL" in strengths:
            state, why = VerificationState.PARTIALLY_VERIFIED, "The claim is supported only as a source-backed signal."
        else:
            state, why = VerificationState.VERIFIED, "The cited current evidence supports the claim at the requested scope."
        return ClaimVerification(claim_id=claim.claim_id, state=state, reasons=[why], evidence_ids=claim.evidence_ids)


def _fresh(record: dict[str, Any], now: float) -> bool:
    if record.get("status") not in {None, "ok", "CURRENT", "VERIFIED"}:
        return False
    expires_at = record.get("expires_at")
    return expires_at is None or (isinstance(expires_at, (int, float)) and expires_at > now)


def _scope_compatible(required: str, actual: Any) -> bool:
    aliases = {"POINT_TO_NEAREST_FEATURE": "NEAREST_FEATURE"}
    return aliases.get(str(required).upper(), str(required).upper()) == aliases.get(str(actual).upper(), str(actual).upper())
