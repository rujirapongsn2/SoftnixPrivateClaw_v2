# Agent Channel

The Agent Channel lets another AI agent, such as a Grok bot, chat with
PrivateClaw as a specific user. The agent mainly uses that user's connectors.
It works like the Telegram channel, but the caller is a program, so it is held
to tighter limits.

## Turn it on

1. An administrator opens **Control Plane → Integrations → Agent channel** and
   turns the switch on. While the channel is off, every agent key is refused
   and users do not see the menu.
2. The user opens **Settings → Agent access** and creates a key. They name the
   agent, pick the connectors it may use, and choose when the key expires.
3. The user copies the key. It is shown only once. The server keeps only a
   keyed hash of the key.

## What an agent can do

- It acts as the user who created the key, with that user's plan, quotas and
  guardrails. Its chats appear in that user's chat list (channel `agent`).
  They are read-only from the web: a web turn there would run with every
  tool on a history the external agent wrote.
- It never sees the owner's long-term memory, and its chats are never folded
  into that memory.
- It may call every tool of the connectors picked for the key, plus
  `update_plan` and `read_skill`.
- It is never offered, and is refused if it asks for, any other tool. This
  covers running code, the browser, files, workflows, sub-agents and
  scheduling.
- One message runs at a time for each key.
- A key can hold up to 50 conversations. A new `conversation_id` beyond that
  is refused, so reuse existing ids.
- A key may make up to 60 requests a minute, polls included. Failed keys are
  limited per remote address.
- A user can hold up to 10 active keys at once.

## API

Send each message as an HTTPS `POST`. Put the key in the `Authorization`
header. The server does not accept the key in a query string, because query
strings end up in proxy logs.

```
POST /api/agent/v1/messages
Authorization: Bearer pcak_...
Content-Type: application/json
Idempotency-Key: <optional, up to 128 chars>

{"conversation_id": "grok-main", "message": "How many open deals do we have?", "locale": "th"}
```

Every field except `message` is optional:

| Field | Default | Rules |
|---|---|---|
| `message` | (required) | 1 to 16,000 characters |
| `conversation_id` | `default` | Letters, digits and `. _ : -`, up to 128 characters |
| `locale` | `en` | Language of the reply |

Send the same `conversation_id` to continue a conversation in context. A new
id starts a new chat.

### Responses

| Status | Body | Meaning |
|---|---|---|
| `200` | `{"status": "completed", "reply": "...", "turn_id": "...", "conversation_id": "..."}` | The reply is ready |
| `200` | `{"status": "failed", "reply": "<reason>", ...}` | The turn ran but failed, for example on a quota or a guardrail |
| `202` | `{"status": "working", "reply": null, "turn_id": "..."}` | The turn took longer than about 55 seconds. Poll for the result |
| `401` | `{"error": "..."}` | The key is wrong, revoked or expired |
| `403` | `{"error": "..."}` | The channel is off, or the owner's account is suspended |
| `409` | `{"error": "..."}` | The key already has 50 conversations, or the message could not be recorded |
| `429` | `{"error": "..."}` + `Retry-After` | Another message from this key is still running, or too many requests |

To get the result of a `202`, poll this endpoint every few seconds until
`status` is no longer `working`:

```
GET /api/agent/v1/turns/{turn_id}
Authorization: Bearer pcak_...
```

When a request is retried with the same `Idempotency-Key`, the server returns
the same turn and does not run the message again. A turn still `working`
after 30 minutes is reported as failed (its server process stopped). Turns
are kept for 7 days.

## Set up the Grok side

1. Give Grok a tool, for example `ask_privateclaw(message, conversation_id)`.
   The tool makes the `POST` above and returns `reply`.
2. Store the key as a secret in the Grok bot's configuration. Do not put the
   key in its prompt.
3. Use one stable `conversation_id` for each Grok conversation, so that
   PrivateClaw keeps the context.
4. Handle a `202` by polling, and a `429` by waiting for `Retry-After`.

## Risks to keep in mind

- **The reply leaves PrivateClaw.** Whatever connector data the agent asks
  for goes to the agent's provider. Pick connectors with that in mind.
- **The agent can write through a connector.** It can call every tool of a
  chosen connector, including tools that write, such as creating an issue.
- **Revoke a key at once if it may have leaked.** The user revokes it in
  Settings → Agent access. An administrator can revoke any key in Control
  Plane → Agent channel.
- **Messages count against the owner.** They use the owner's daily message
  quota and model limits. Assign the owner a plan with suitable limits.
