# Local Agent

An assistant that works toward the user's goals through computer actions and delegated work.

## Language

**Proactive work**:
Work initiated by a schedule, a relevant event, or an ongoing user-assigned goal without requiring a new prompt for every step.

**Sensitive action**:
An action that spends money, sends messages to people, changes account or security settings, permanently deletes data, or downloads something onto the machine. Each occurrence requires a detailed explanation and explicit user approval before execution.

**Long-term memory**:
The agent's retained knowledge available across conversations and restarts, including relevant goals and prior progress.
_Avoid_: Slow memory

**Short-term memory**:
The working context an agent uses for its current task, including relevant instructions, recent exchanges, and action results.
_Avoid_: Context window as a synonym for all stored memory

**Context compaction**:
Preserving an agent's original history while replacing its working context with a shorter account of its goal, decisions, progress, pending work, and relevant references. Unresolved approvals remain unresolved.

**Top agent**:
The coordinating agent that chooses what work to delegate, which model a child uses, and what context or memory access the child receives.

**Child agent**:
An agent assigned work by the top agent, with its own working context and the permissions granted for its task.

**Fresh child**:
A child agent that starts with task instructions and applicable permission rules, without inherited conversation or memory access.
_Avoid_: Blank agent when it implies an agent without permission rules
