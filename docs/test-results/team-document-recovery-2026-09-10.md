# Document workflow output-limit recovery

Production incident `1f40d73b05768969ef42caf6734d6812`: review ended with output_limit after 61 seconds; 631,323 input + output tokens exceeded the fixed 400,000-token mission budget. The writer and coordinator never started.

## Structural changes

- Specialists inherit configured output and iteration limits instead of separate 4,096/20 constants. Context compaction also respects the configured input budget, even for models advertising very large windows.
- Required-completion tasks can continue twice after a truncated model response, within the same conversation and original time/iteration limits. Incomplete tool calls are discarded. Successful actions are retained; the scheduler does not replay the node. Repeated truncation still fails and blocks dependent tasks.
- Background jobs allocate the existing token allowance per planned step, including final synthesis. Time, attempt and admission ceilings remain bounded; historical spend is not reset.
- Document tasks save findings in sections, hand off validated files, and avoid repeating the entire file in the completion response.
- DOCX reading includes tables in document order and supports character paging.

## Real-model evidence

Command: `.venv/bin/python scripts/team-document-e2e.py <env-file>`

Uses admin-default model credentials from Control Plane in memory, a fresh SQLite database, synthetic Thai input, and Docker without network inside the tool sandbox. Production task files are not inputs to this test.

Model: `openai/deepseek-v4-flash`. Completed review → rewrite → final synthesis in 33 model calls / 277,798 tokens. Independently reopened the generated Word file: all 120 paragraphs preserved, table EPS changed from 9000 to 5000, source unchanged. All four checks passed.

Automated tests cover bounded cutoff recovery, discarded partial writes, preserved successful tool history, dependent-node gating, configured output limits and paged table extraction. A prior initial live probe used obsolete environment-only provider settings and failed before producing output; the successful run resolved the configured Control Plane model.

This proves the tested pipeline; it does not guarantee every document or model response completes within the finite budget.
