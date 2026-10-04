# Semantic Guardrails (Monitor)

Jev and OpenThai SystemOne use separate server-side connections. Configure both credentials if needed, but select one active provider. Regex enforcement remains independent; semantic judgments never block a chat request in this release.

```dotenv
CLAW_SEMANTIC_GUARDRAILS__PROVIDER=jev
CLAW_SEMANTIC_GUARDRAILS__JEV__API_KEY=your-key
CLAW_SEMANTIC_GUARDRAILS__JEV__ENDPOINT=https://api.typesafe.ai/v1/systemone
CLAW_SEMANTIC_GUARDRAILS__JEV__MODEL=jev-1.13.0
CLAW_SEMANTIC_GUARDRAILS__LAYA__API_KEY=your-key
CLAW_SEMANTIC_GUARDRAILS__LAYA__ENDPOINT=https://genai.softnix.ai/laya/v1/decide
CLAW_SEMANTIC_GUARDRAILS__LAYA__MODEL=iapp/openthai-systemone
CLAW_SEMANTIC_GUARDRAILS__TIMEOUT_SECONDS=5
CLAW_SEMANTIC_GUARDRAILS__AUTO_FALLBACK=true
CLAW_SEMANTIC_GUARDRAILS__MAX_CHARS=8000
CLAW_SEMANTIC_GUARDRAILS__MAX_CONCURRENT=4
```

Set `PROVIDER=laya` to use OpenThai SystemOne through the existing Laya adapter, or `off` to stop all semantic calls. Restart with `bash scripts/claw restart` after editing `.env`. The integrated Sbot mode shares the parent connection. These settings do not contain client-side secrets.

Control Plane > Guardrails displays configuration status and a sample tester. A configured key means configuration is ready, not that the upstream has passed a health check. The latest failed check is shown separately. Test negations, quoted examples and Thai prompts before deciding whether a signal is useful.

Scope: primary chat input and final answers in PrivateClaw and Sbot. Tool calls, tool results, nested agents and scheduling/missions do not receive semantic checks in this initial release. Existing deterministic rules still apply at their existing boundaries. Output checks are observational; they do not intercept already streamed output.

Known PII/secret patterns and active regex matches are redacted from the copy sent to the selected upstream. This is not a guarantee that every sensitive value is recognized. Enabling a provider sends the remaining chat text to that provider. Source text is not stored in semantic audit records.

Audit events `semantic_guardrail` record provider, model, scope, policy version, probabilities, duration and safe error categories. `semantic_guardrail_config` records startup configuration without keys. HTTP errors record a status code, not the upstream body. Timeout, invalid response and unavailable service leave the normal chat flow unchanged. Oversized text is skipped and logged; it is not silently truncated. With the provider off or missing a key, runtime checks make no network calls; startup and explicit tests still log their status.

## Admin-authored Semantic rules

Control Plane > Guardrails > Semantic rules lets administrators choose an inert template or write a condition, exclusions and input/output scopes. Templates are not seeded as active policies. Creating a rule or saving an edited rule leaves it Off; use its switch to enable it explicitly after testing. Test a saved rule with the per-rule Run test action even while it is Off. Tests do not enable the rule.


### Sensitivity scale

Each rule stores a `scale` float from 0 to 1 (persisted with the rule in `app_settings`). New rules and templates default to `0.5`. Creation preserves the chosen sensitivity with `enabled: false`. Switching Off/On preserves sensitivity; existing saved values are not reset.

`SemanticMonitor` treats `scale` as sensitivity for monitor alerts on the noul score (0–1 probability):

- `scale = 0` — no monitor alert (disabled-equivalent for alerting)
- `scale = 1` — most sensitive; alert when score `> 0`
- `0 < scale < 1` — alert when score `>= 1 - scale`

Alerts are recorded on the observe result as `alerts` / `scales` alongside `scores`. Chat flow stays Monitor only; scale does not block or mask.

### Built-in template groups

Templates ship `enabled: false` and are only starting points. Evaluate on your own data before enabling. All enabled rules remain Monitor only.

1. **Technical security** — `instruction_override`, `secret_disclosure`, `prompt_injection` (injected or obfuscated prompts / exfiltration tricks).
2. **Personal safety** — `targeted_harassment`, `hate_speech`, `self_harm`.
3. **Internal operations** — `data_leakage` (customer/salary/confidential export), `cross_team_access`, `privilege_escalation` (explicit permission-bypass requests).
4. **Compliance / anomalous** — `pii_processing`, `anomalous_time` (explicit monitoring-evasion requests using hours or sources), `attack_probing` (reconnaissance / malware-or-exploit questions).

Rules are persisted in `app_settings` separately from regex policies and loaded at each primary chat boundary, so changes take effect without restarting. If no applicable rules are enabled, no semantic request is made. The previous fixed judgments are available as templates rather than running implicitly. Conditions are yes/no judgments; the displayed percentage is the probability that the condition applies, not its severity. All enabled rules still operate in Monitor only.

Changes are audited under `semantic_rule_change`. Check logs include rule IDs and definition hashes so an in-flight decision can be associated with the evaluated version. Template instructions are starting points; evaluate on your own data before enabling. This release does not supply authorization context to a condition, so avoid rules that assume the model can infer a user's permissions.

Internal-access and PII templates judge explicit textual evidence, not actual authorization. The time/source template detects stated evasion intent, not live network or time anomalies. No verified role, ACL, consent, or network metadata is supplied to the model. Existing saved conditions are not overwritten; edit or recreate a rule to adopt updated template wording.

The Guardrails page separates Keyword/Regex and Semantic rules into tabs. Semantic templates and saved rules are shown in group cards. Choose a template to open its editor; select a group when authoring a custom rule. The group is persisted with the rule and is organizational only; it does not change the semantic judgment or enforcement mode. Legacy rules without a group use a matching template name, or Custom rules.

The `laya` provider and `LAYA` environment prefix remain as compatibility identifiers. Its default model is now `iapp/openthai-systemone`. An upstream response with `usage.truncated_state: true` is logged as skipped with reason `truncated_state`; no scores or alerts from partial input are accepted.

## Actions (phase 1)

Each rule has an **action**, chosen with the same buttons as the keyword/regex rules:

| Action | Effect | Where |
|---|---|---|
| Monitor (default) | Audit alert only. Runs in the background; never delays the message. | input, output |
| Warn | Message goes through; the user sees a notice. | input, output |
| Confirm | The user must approve before the message reaches the model. Declined or unanswered = cancelled. Not available from scheduled tasks or chat bots (the message is not processed). | input only (at output it degrades to Warn) |
| Block | The message is stopped (input) or the answer withheld (output), with a message. | input, output |

* **Two levels.** *Sensitivity* decides when a rule *alerts* (probability ≥ 1 − sensitivity). An acting rule only
  *acts* at `max(alert level, act threshold)`; the act threshold defaults to 90%.
* **Dry run** (default on for acting rules). The would-be action is recorded in the audit log
  (`semantic_guardrail_action`, `enforced: false`) but the message is let through. Review it, then turn dry run off.
* **Fail-open.** If the provider errors, times out or is unreachable (after automatic fallback), the message is
  allowed and the failure is audited. A provider outage never blocks users.
* **Latency.** Only rules that can act make the turn wait for the check (one request judging every rule for that
  point, capped by `timeout_seconds`). Monitor-only rules run in the background.
* Every acted or dry-run decision is written to the audit log with the rule name, score, action and whether it was enforced.
  The message text is never logged.
* **Long texts.** Text over `max_chars` (default 8000) is not sent whole. A rule that can act (Warn/Confirm/Block)
  judges the first and last half of the limit, so padding a message cannot skip the rule; the audit row is marked
  `windowed`. The middle of a very long text is **not seen**: a provider input is bounded, so this narrows but does not
  close the gap. Monitor-only rules still skip over-long text (`text_too_large`).
* **Time budget.** The whole check (primary, then fallback if needed) shares one deadline of 1.5 × `timeout_seconds`.
  Waiting for a free slot is not blamed on the provider; if the budget runs out the message is let through.
* **Admin test button** is limited to 20 calls per minute per admin because it calls the paid provider.

## Auto model and Model routing

The chat model picker offers **Auto** when the semantic provider is ready, a fallback chat model is set, and at least two admin models are enabled. Auto chooses a model per message, so a coding question can go to a coding model and a short summary to a cheap one. A user's own (My Models) models are never chosen by Auto.

For each Auto message the guardrail model answers one `score` question over the six model purposes, plus one question per enabled routing rule. `claw/core/model_router.py` then picks the model whose **Purposes** match best, among the models the user's plan allows. Equal scores go to the cheaper model, then the admin default. If the judgment is weak (top probability under 0.5), missing, or the check fails, the admin default is used, then the fallback model. An attached image counts as a multimodal task. The check runs in parallel with the input guardrail check, has its own 1.5 second budget, and every decision is written to the audit log as one `auto_model_route` event with the chosen model, the reason and the check result.

**Model routing** is a rule group in Guardrails > Semantic rules. A routing rule has the action **Use Local AI**: when it matches, Auto only considers models marked **Local AI**. Five templates ship, all off and in dry run: personal data, confidential business data, internal material, credentials and private code, health data. A secret or PII mask match also forces Local AI, decided locally without the provider. When a routing rule is live and the check fails, times out or errors, or the rule list cannot be read, Auto fails safe to Local AI. If no Local AI model is available the message is not sent, and the user is told why. Routing rules affect Auto only, never a model the user picked.

Measured on 48 labelled messages (`scripts/eval_auto_routing.py`, 2026-10-04): Jev 45/48 correct, latency p50 367 ms and p95 505 ms. OpenThai SystemOne 43/48 correct, p50 139 ms, one 3.6 second outlier. The set is small and written by the developers, so check it against your own traffic before relying on Auto.
