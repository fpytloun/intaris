## Major

- Implement OAuth 2.1 flow for MCP servers

## Issues

## Minor

- make MCP env variables editable (separate secret from regular values)
- more granular assignment of MCP servers to agents
- MCP servers sharing for multiple users

## Major complex

- there should be feedback-loop and self-learning from approve/deny actions. Evaluator should look for similar pattern already denied/approved especially if there is "Optional note" attached. But again we must not degrade performance.
- also ability to mark as false positive / false negative in audit

- configurable policies and evaluation behavior (eg catch PII data, some things for EU regulations, etc., more rules to enforce hard kill vs being more permissive)

- Prometheus metrics, we want to know more about performance, like having histogram for evaluations, etc.
- Grafana dashboard

- AI chat to examine session

- guardrails involving memories (facts, preferences) as another policy layer - eg. memory sais never run git push without user instruction and agent runs git push without explicit instruction
