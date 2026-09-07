# Evaluation outcome floor

`POST /api/v1/evaluate` accepts optional `minimum_outcome`: `deny`, `escalate`, or `approve`.
The evaluator chooses the stricter result in this order: `deny < escalate < approve`.
It applies this constraint before audit storage and the response. Requests without the field retain existing behavior.

An `escalate` floor requires human review. The evaluate endpoint does not invoke Judge for this escalation.
After approval, the client supplies the original `approval_call_id` together with the same floor, session, tool, and arguments.
Intaris checks the owner's audit record and human decision. Current denials remain denials.
A new floor request does not use the general recent-approval cache.

This reference is not a single-use grant. The trusted controller sends it only when it resumes the approved operation.
The response echoes `minimum_outcome` when supplied, so clients can detect unsupported servers.
