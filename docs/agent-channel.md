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

A turn keeps running after the caller disconnects. Its result stays on the
server even if the caller never polls.

Every turn response also carries `created_at` and `finished_at` (ISO 8601,
UTC). `finished_at` is `null` while the turn is `working`.

### Find a turn whose id was lost

A caller can lose a turn id, for example when a voice assistant times out
before the `202` arrives. To find the turn again, list the newest turns of the
conversation:

```
GET /api/agent/v1/conversations/{conversation_id}/turns?limit=5&status=completed
Authorization: Bearer pcak_...
```

To list turns across all conversations of the key, or only turns since a
given time, use:

```
GET /api/agent/v1/turns?conversation_id=grok-main&since=2026-11-03T10:00:00Z&limit=5
Authorization: Bearer pcak_...
```

| Parameter | Default | Rules |
|---|---|---|
| `limit` | `5` | 1 to 50 |
| `status` | (any) | `working`, `completed` or `failed` |
| `since` | (none) | ISO 8601 time. A time without a zone is read as UTC |
| `conversation_id` | (any) | `/turns` only. Same rules as in `POST /messages` |

The response is `{"turns": [...]}`, newest first. Each turn has the fields of
the poll response, plus these:

- `message_preview`: the first 200 characters of the message, with runs of
  whitespace collapsed to one space.
- `webhook`: `null` if the key has no webhook. Otherwise it is
  `{"status": "pending|delivered|failed", "attempts": n, "at": "..."}`.

A key sees only its own turns. Another key of the same owner cannot see them.

The listing is for recent turns only. It returns at most 50 turns and has no
cursor for paging. Use `since` to narrow it. Turns are deleted after 7 days.

Typical use: when the user asks "is it done yet?", call the conversation
listing with `limit=1` and answer from the newest turn. If that turn is still
`working`, do not send the question again. A new message would be refused
with `429` while the turn runs.

### Completion webhook (optional)

A key can have a callback URL. When a turn of the key finishes, as
`completed` or `failed`, the server POSTs the result to that URL. The caller
then does not need to poll.

To set the URL, the owner opens **Settings → Agent access → Set webhook**, or
calls:

```
PUT /api/agent-clients/{client_id}/callback
{"callback_url": "https://bot.example.com/claw-hook", "rotate_secret": false}
```

- The URL must use `https`, must use a public host name, and must not contain
  a user name or password. IP addresses (including forms such as
  `2130706433`), `localhost` and local names (`.local`, `.internal`, `.lan`, a
  name without a dot) are refused. If the name resolves to a private address
  at save time, it is refused too.
- A revoked or expired key cannot get a webhook (`409`).
- The first time a URL is set, the response includes a signing secret
  (`whsec_...`). The secret is shown only once. The server stores it
  encrypted. To get a new secret, send `rotate_secret: true`.
- To remove the webhook, send `callback_url: null`.

Each delivery is a POST with a JSON body:

```
POST <callback_url>
Content-Type: application/json
X-Claw-Timestamp: 1762163000
X-Claw-Signature: sha256=<hex>
X-Claw-Turn-Id: <turn_id>
X-Claw-Delivery-Attempt: 1

{"event": "turn.finished", "turn_id": "...", "conversation_id": "...",
 "status": "completed", "reply": "...", "reply_truncated": false,
 "created_at": "...", "finished_at": "..."}
```

To verify a delivery, the receiver computes
`HMAC-SHA256(secret, "<X-Claw-Timestamp>.<raw body>")` as hex. It compares the
result with the signature in constant time. It also rejects a timestamp that
is more than about 5 minutes old. A reply longer than 64,000 characters is cut
and `reply_truncated` is `true`. The full reply is always available from
`GET /api/agent/v1/turns/{turn_id}`.

A delivery can arrive more than once, for example when the receiver answered
but the answer was lost and the server retried. Deduplicate by `turn_id`,
which is also sent in the `X-Claw-Turn-Id` header. A turn has exactly one
final result, so the first delivery of a turn id is enough.

Delivery rules:

- The server checks the host on every attempt. If the host resolves to a
  private, loopback, link-local or metadata address, the delivery is refused.
  The connection goes to the address that was checked.
- The server does not follow redirects.
- The server makes up to 3 attempts, with 2 s and then 8 s between them. Each
  attempt has a total limit of 5 s, which includes the DNS lookup. At most 8
  deliveries run at the same time in a server process; the rest wait. Only `408`, `425`, `429`, `5xx` and network
  errors are retried. Any `2xx` counts as delivered.
- The webhook never delays or changes the turn. The result is stored first and
  can always be polled.
- Each turn records the delivery result (`webhook` in the turn listing). The
  server log shows the host, the number of attempts and the status code. It
  never shows the full URL or the secret.
- A turn that the server fails as stale (its process stopped) sends no
  webhook. The caller sees it in the turn listing.
- If the server can no longer decrypt the signing secret (its secret key
  changed), nothing is sent. The turn's webhook shows `failed`, with the hint
  to rotate the secret.
- On shutdown, the server waits up to 10 s for running turns and deliveries,
  then cancels them. A cancelled turn is recorded as `failed`.

On a development server only, the operator can set
`CLAW_AGENT_WEBHOOK_ALLOW_HTTP=true` to allow `http` callback URLs. The host must
still be public.

## Set up the Grok side

1. Give Grok a tool, for example `ask_privateclaw(message, conversation_id)`.
   The tool makes the `POST` above and returns `reply`.
2. Store the key as a secret in the Grok bot's configuration. Do not put the
   key in its prompt.
3. Use one stable `conversation_id` for each Grok conversation, so that
   PrivateClaw keeps the context.
4. Handle a `202` by polling, and a `429` by waiting for `Retry-After`.
5. Keep the `turn_id` of each `202`. If it is lost, list
   `/api/agent/v1/conversations/{conversation_id}/turns` before you send the
   question again.
6. Optional: set a completion webhook, so results arrive without polling.
   Verify `X-Claw-Signature` before you trust a delivery.

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
