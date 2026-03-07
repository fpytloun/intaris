"""POST /evaluate endpoint for tool call safety evaluation."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from intaris.api.schemas import EvaluateRequest, EvaluateResponse

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/evaluate", response_model=EvaluateResponse)
async def evaluate(request: EvaluateRequest) -> EvaluateResponse:
    """Evaluate a tool call for safety and intention alignment.

    Runs the full evaluation pipeline:
    1. Classify (read-only allowlist → auto-approve)
    2. Critical pattern check (→ auto-deny)
    3. LLM safety evaluation (→ decision matrix)
    4. Audit logging
    5. Session counter update

    Returns the decision with reasoning, risk level, and latency.
    """
    from intaris.server import _get_evaluator

    try:
        evaluator = _get_evaluator()
        result = evaluator.evaluate(
            session_id=request.session_id,
            agent_id=request.agent_id,
            tool=request.tool,
            args=request.args,
            context=request.context,
        )
        return EvaluateResponse(**result)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception:
        logger.exception("Error in /evaluate")
        raise HTTPException(
            status_code=500,
            detail="Internal error during evaluation",
        )
