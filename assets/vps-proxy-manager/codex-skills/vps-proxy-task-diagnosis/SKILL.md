---
name: vps-proxy-task-diagnosis
description: Analyze a sanitized failed-task context from VPS Proxy Manager and return a structured, read-only diagnosis. Use only when the Codex Worker supplies a numeric diagnostic task ID and an absolute diagnosis context JSON path.
---

# Diagnose A Failed VPS Proxy Task

This is an analysis-only workflow. The controller already collected and redacted the runtime context.

## Workflow

1. Accept only the numeric diagnostic task ID and generated context path stated by the Worker.
2. Read the context JSON.
3. Inspect only the relevant source files named in that context and adjacent project tests when needed.
4. Correlate the technical error with transaction boundaries, task state, SSH action, parser, sing-box generation, or remote Agent behavior.
5. Return one JSON object matching the supplied output schema. Write the diagnosis in Chinese.

## Required Judgment

- Distinguish controller defects from target VPS connectivity, credentials, subscription data, proxy-node availability, and expected user cancellation.
- Cite concrete evidence from the context or source behavior.
- Set `retry_safe` to `true` only when repeating the original operation cannot modify network state unexpectedly and the diagnosed cause has already cleared or is transient.
- Recommend manual confirmation for proxy apply, restore, rollback, uninstall, resource deletion, or host deletion.

## Constraints

- Do not read `.env`, `/etc/vps-proxy-manager`, application databases, Codex auth files, SSH material, subscription content, or node links.
- Do not run SSH, network, package, service, firewall, routing, database-write, or proxy commands.
- Do not modify source code or runtime state.
- Do not automatically retry the source task.
- Do not invent missing evidence. State uncertainty in `summary` and `root_cause` when context is insufficient.
