"""The `a2ui-cards` built-in skill: when and how the model emits A2UI cards."""

from claw.a2ui import SOFTNIX_CATALOG_ID

A2UI_SKILL_NAME = "a2ui-cards"

A2UI_SKILL_DESCRIPTION = (
    "READ FIRST whenever a request is too broad to act on well (e.g. 'สร้างรายงาน', 'ทำสไลด์ให้หน่อย' with no topic, "
    "audience or format) — ask with a clickable A2UI choice card instead of a text question. Also how to show "
    "structured results as cards (present_ui), any chart/graph/กราฟ of data (chart_from_data, shown in the chat) and OpenRouter price "
    "comparisons (openrouter_price_watch)."
)

A2UI_SKILL_CONTENT = f"""\
# A2UI cards (A2UI spec v0.9, Softnix catalog)

The chat renders A2UI v0.9 JSON as real UI built from the Softnix design system. Use it to
1. **Clarify** a broad request with clickable choices (preferred over asking in prose), and
2. **Present structured results** (stats with up/down arrows, tables, charts).

## When
- The request is ambiguous/too broad to do well (missing format, tone, audience, scope) -> ONE clarify card, then STOP.
  Write at most one short sentence before the card. Do not start the work and do not guess.
- Do NOT use a card for simple questions, chit-chat, or when the user already gave enough detail.
- OpenRouter "new models / price comparison" -> call tool `openrouter_price_watch` (it builds the chart and computes every
  number in code). Never compute prices, percentages or arrows yourself.
- Chart / graph / "show me the trend/share/ranking" of data -> tool `chart_from_data` (see "Charts" below).
- Never put credentials/password fields, links or HTML in a card. All text is plain text.

## Charts (`chart_from_data`)
The tool reads the file, aggregates and computes every number and ▲▼ % IN CODE and shows the chart card with a
data-table toggle. You only choose WHAT to chart. Never type chart numbers yourself.
- `source` = workspace path of a .csv/.tsv/.xlsx/.json file (or `rows` = records you got from another tool's output).
  If unsure of the column names, look first (read_excel / read_file a few lines) — the error lists the columns too.
- `chart_type`: line / area = trend over time (set `x_grain` day|week|month|quarter|year on a date column);
  bar = compare categories or periods; stackedBar = parts of each period (`series_by` = the part column);
  hbar = ranking or long labels (top N with `limit`); pie / donut = share of a whole (one `y`, <= 12 slices, the
  rest merge into "อื่น ๆ"/"Other"); progress = each value against `max`/the largest.
- `agg` sum (default, needs `y`) | avg | count (set agg "count" and omit `y` to count rows, e.g. events per day) | min | max.
  Several `y` columns -> several series.
- `delta: "previous"` adds ▲▼ % vs the previous period (use with time axes). `good_direction: "down"` for costs,
  expenses, errors, failures, latency, so a drop shows green.
- `title`, `x_label`, `y_label`, `series_labels`, `unit` and `lang` in the user's language ("th" for Thai, else "en").
- After the card: 2-4 sentences in the user's language; quote only numbers exactly as the tool returned them.
- Never write a `Chart` component yourself and never pre-compute the numbers with exec: call `chart_from_data` on
  the file. present_ui rejects Chart, and a Chart typed in a fenced block is shown with a "numbers typed by the AI,
  not verified" warning. If the numbers come from another tool's output, pass them as
  `rows` with `source: "inline"`.

## Language (always)
- Use the language of the user's latest message for EVERYTHING you write around and inside a card: the sentence before
  it, the question, labels, options, placeholder text and the button. Thai in -> Thai; English in -> English; another
  language -> that language.
- Use only that language. Do not mix in words from other languages: in Thai say "การ์ด" (never the English "card", never Chinese characters),
  "ปุ่ม" not "button". Brand/product names (OpenRouter, Word, Excel, PDF, HTML) and model ids stay as they are.
- The template below is a Thai example. For an English request translate every string (e.g. "What kind of report do
  you need?", Formal / Informal / Other, "Continue").
- `openrouter_price_watch`: pass `lang` = "th" for Thai, otherwise "en", and reply in the user's language.

## Two ways to emit
A. Fenced block in your reply (quick clarify cards): a code fence whose language is exactly `a2ui`
   containing the JSON message list below.
B. Tool `present_ui` with `messages` = the same list (validated server-side, saved as a card file).
   Use B for result cards and whenever a fenced card was shown as code (= invalid).

## Message list (v0.9) — catalogId must be "{SOFTNIX_CATALOG_ID}"
[createSurface, updateComponents, updateDataModel]. Components are a FLAT list; parents refer to children by id;
exactly one component has id "root".

Components: Text(text, variant h1-h5|body|caption) · Column(children[]) · Row(children[], justify start|end|spaceBetween)
· Card(child) · Divider · Button(child = id of a Text, variant primary|default|borderless, action)
· ChoicePicker(label, options[{{label,value}}], value = {{"path":"/x"}}, variant mutuallyExclusive|multipleSelection,
displayStyle chips|checkbox) · TextField(label, value = {{"path":"/y"}}, variant shortText|longText)
· DeltaStat(label, value:number, unit, direction up|down|flat|new, deltaPct:number, baseline)
· DataTable(columns[{{key,label,align}}], rows[{{key: value}}])
· Chart (written only by chart_from_data; shown here so you can read its cards)(chartType bar|line|area|pie|donut|stackedBar|hbar|progress, categories[string], series[{{label, values[number|null],
deltas?[{{pct, direction up|down|flat|new}}|null]}}], title, unit, decimals, xLabel, yLabel, goodDirection up|down, max).

Button action: {{"event": {{"name": "<snake_case>", "context": {{"<key>": {{"path": "/<field>"}}}}}}}}.
When clicked the user sends a normal message that starts with "[การ์ด]" (Thai card) or "[Card]" (any other
language), e.g.:
  [การ์ด] report_brief            [Card] report_brief
  - style: ทางการ                  - style: Formal
  - detail: ...                   - detail: ...
Treat that message as the user's answer and continue the original task with it, in the card's language.

## Clarify card template ("สร้างรายงาน")
```a2ui
[
 {{"version":"v0.9","createSurface":{{"surfaceId":"report-clarify","catalogId":"{SOFTNIX_CATALOG_ID}"}}}},
 {{"version":"v0.9","updateComponents":{{"surfaceId":"report-clarify","components":[
  {{"id":"root","component":"Card","child":"col"}},
  {{"id":"col","component":"Column","children":["q","style","detail","actions"]}},
  {{"id":"q","component":"Text","variant":"h4","text":"อยากได้รายงานแบบไหนครับ"}},
  {{"id":"style","component":"ChoicePicker","label":"รูปแบบรายงาน","variant":"mutuallyExclusive","displayStyle":"chips",
   "options":[{{"label":"ทางการ","value":"ทางการ"}},{{"label":"ไม่ทางการ","value":"ไม่ทางการ"}},{{"label":"ระบุเอง","value":"ระบุเอง"}}],
   "value":{{"path":"/style"}}}},
  {{"id":"detail","component":"TextField","label":"หัวข้อ/ผู้อ่าน/รายละเอียด (ถ้าเลือกระบุเอง ให้พิมพ์ที่นี่)","variant":"longText","value":{{"path":"/detail"}}}},
  {{"id":"actions","component":"Row","justify":"end","children":["go"]}},
  {{"id":"go","component":"Button","variant":"primary","child":"go_t",
   "action":{{"event":{{"name":"report_brief","context":{{"style":{{"path":"/style"}},"detail":{{"path":"/detail"}}}}}}}}}},
  {{"id":"go_t","component":"Text","text":"ไปต่อ"}}
 ]}}}},
 {{"version":"v0.9","updateDataModel":{{"surfaceId":"report-clarify","path":"/","value":{{"style":[],"detail":""}}}}}}
]
```
Adapt the question, options and keys to the request (2-5 options, short labels in the user's language). Keep the JSON valid:
double quotes only, no comments, no trailing commas.
"""
