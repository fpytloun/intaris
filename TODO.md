## Done

- UI: sessions should show last activity (not only creation date)
- UI: sessions list should be sorted on creation date and make sure it does not blink on every refresh
- UI: agent id is not seen anywhere in UI (Sessions, Audit, etc.)
- UI: intention is also not visible. We should make sure internals are visible to user.
- UI fix: collapse session is not possible if box (intention and tool exec details) is expanded
- UI: sort sessions and sub-sessions from newest on top

- UI: Last active, Agent, User needs separator
- Agent should be opencode, not "plan/explore/etc." that is basically sub-agent (agent within opencode), we could make it opencode:explore in our system.
- Also note that agent can change, eg. typically plan -> build so main session should be just `opencode` and subagent maybe just metadata (but it changes over time so it does not make sense or reflect it per tool call)
- UI: sessions are not sorted properly. Main session should be sorted by creation time and within that sub-sessions as well. Always newest on top
- I still dont see intention updated based on user message within session. Also intention can change within session. We should keep track of it for tool calls.


- opencode/UI: suddenly subagents does not have parent agent
- Make sure that reasoning for sub-session is based on intention from both parent and child session, to avoid escaping intention chain and our guardrails

- UI: Approve/Deny button does nothing
- UI: arguments truncated not possible to expand.
- UI: we should also pretty-print JSON to be nicely readable

- opencode: when ESCALATE, our plugin should just sleep, using some exponential back-off or some other mechanism and waiting for user approval (but also print in UI). Current behavior is this error and it proceeded anyway:
```
Error: [intaris] ESCALATED (3f0e0533-6a3e-4a72-b024-9f4f54232724): High risk — requires human review. The todowrite adds a set of high-impact tasks including DB migrations, audit improvements, and schema changes. While aligned with UI improvement goals, these changes affect persistent data structures and application behavior, warranting human review before execution.
Approve or deny this call in the Intaris UI, then retry.
```
- overall it seems that it does not actually block tool call

- UI: approvals entry should be clickable to session details
- opencode: increase timeout for actions (some large tool calls like sequentialthinking can take 10 seconds, overall I think we should be graceful and give 30 seconds for this)
- opencode: implement retries with exponential backoff

- review intention/reasoning logic whether it makes sense. Eg. intention update from user message from opencode should be immediate and reflect further tool calls

- Auto-refresh on Sessions mess up with sessions hierarchy and keep it jumping here and there

- Approvals page should show approved under another section (with pagination)
- Audit should have approved pill when escalate action was already approved, escalate should remain to make it clear it was approved manually

- UI: audit actions should show intention (one we added recently to store intention which changes within session into tool calls)
- UI: search bar for sessions, etc.
- UI: fix click-through to session (using that search maybe)
- UI: Recent Activity on dashboard should be also clickable

- UI: show pie charts for decisions, risks, paths

- Web notifications
- Approvals should have badge to see no. pending in menu
- Approvals page when resolved, it cannot be expanded to show details as prior to approval
- Session links does not work, it should show session details. We can use modal to open session details if linking to Sessions properly is an issue
- There is online green dot on Approvals (Pending Approvals) but it is redundant as whole app now uses websockets and there is online status on intaris.

## In progress

## Major

- opencode does not notify user on escalate action

## Minor

- Sessions filters (All, Active, Idle) should only filter parent session if in tree view, child sessions when parent is expanded should be shown no matter of state.
- Search sessions should not filter child sessions. It should query among parents and childs but always show parent + all it's children if tree view. We could also highlight matched string.
- Opencode plugin should put session into "Idle" when ended (waiting for user input) to make it clear it is not doing anything and resume right after user input when the work starts again.

## Major complex

- there should be feedback-loop and self-learning from approve/deny actions. Evaluator should look for similar pattern already denied/approved especially if there is "Optional note" attached. But again we must not degrade performance.
- also ability to mark as false positive / false negative in audit

- Prometheus metrics, we want to know more about performance, like having histogram for evaluations, etc.
- Grafana dashboard

## Issue

- When user resumes auto-suspended session it gets suspended again. There should be also reason and overall it should be same as for tool escalate
Reason for suspending session was: "Session is suspended — evaluation denied. Reason: Child intention conflicts with parent session: Investigating system prompt tuning is about prompt/LLM behavior, which does not support or decompose the task of designing a session storage architecture for Intaris; therefore it is outside the parent's scope." where intention was "Investigating system-prompt tuning for the Intaris project by examining evaluator, decision, and prompts code to inform the session storage architecture design." and it was child session to "Intaris session storage architecture design"
