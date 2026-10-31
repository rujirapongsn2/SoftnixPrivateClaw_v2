import assert from 'node:assert/strict';
import test from 'node:test';

import {
  SOFTNIX_CATALOG_ID,
  actionToMessage,
  CARD_UI,
  cardLang,
  deltaGlyph,
  parseA2ui,
} from '../.test-dist/a2ui/protocol.js';
import {
  CHART_LIMITS,
  arcPath,
  compactValue,
  formatValue,
  labelStep,
  niceScale,
  pieSlices,
  stackTotals,
  truncate,
  validateChart,
  valueRange,
} from '../.test-dist/a2ui/chartMath.js';

const clarify = [
  { version: 'v0.9', createSurface: { surfaceId: 'report-clarify', catalogId: 'https://example.com/other.json' } },
  {
    version: 'v0.9',
    updateComponents: {
      surfaceId: 'report-clarify',
      components: [
        { id: 'root', component: 'Card', child: 'col' },
        { id: 'col', component: 'Column', children: ['q', 'style', 'go'] },
        { id: 'q', component: 'Text', text: 'อยากได้รายงานแบบไหนครับ' },
        {
          id: 'style',
          component: 'ChoicePicker',
          options: [
            { label: 'ทางการ', value: 'ทางการ' },
            { label: 'ระบุเอง', value: 'ระบุเอง' },
          ],
          value: { path: '/style' },
        },
        { id: 'go', component: 'Button', child: 'go_t', action: { event: { name: 'report_brief', context: { style: { path: '/style' } } } } },
        { id: 'go_t', component: 'Text', text: 'ไปต่อ' },
      ],
    },
  },
  { version: 'v0.9', updateDataModel: { surfaceId: 'report-clarify', path: '/', value: { style: [] } } },
];

test('valid v0.9 list parses and is pinned to the Softnix catalog', () => {
  const r = parseA2ui(JSON.stringify(clarify));
  assert.equal(r.ok, true);
  assert.equal(r.surfaceId, 'report-clarify');
  assert.equal(r.messages[0].createSurface.catalogId, SOFTNIX_CATALOG_ID);
  assert.equal(r.messages.length, 3);
});

test('compact {components,data} form normalizes to the same list', () => {
  const r = parseA2ui({ components: clarify[1].updateComponents.components, data: { style: [] } });
  assert.equal(r.ok, true);
  assert.deepEqual(Object.keys(r.messages[2]), ['version', 'updateDataModel']);
});

test('invalid JSON never throws, it reports', () => {
  const r = parseA2ui('{"version": "v0.9", "createSurface": ');
  assert.equal(r.ok, false);
  assert.match(r.error, /invalid JSON/);
  assert.equal(parseA2ui('').ok, false);
  assert.equal(parseA2ui('null').ok, false);
  assert.equal(parseA2ui('[1,2]').ok, false);
});

test('XSS: unknown components, client functionCalls and prototype keys are rejected', () => {
  const script = parseA2ui({ components: [{ id: 'root', component: 'script', text: 'alert(1)' }] });
  assert.equal(script.ok, false);
  assert.match(script.error, /not in the Softnix catalog/);

  const html = parseA2ui({ components: [{ id: 'root', component: 'HtmlBlock', html: '<img src=x onerror=alert(1)>' }] });
  assert.equal(html.ok, false);

  const openUrl = parseA2ui({
    components: [
      { id: 'root', component: 'Button', child: 't', action: { functionCall: { call: 'openUrl', args: { url: 'javascript:alert(1)' } } } },
      { id: 't', component: 'Text', text: 'x' },
    ],
  });
  assert.equal(openUrl.ok, false);
  assert.match(openUrl.error, /functionCall is not allowed/);

  const proto = parseA2ui('{"components":[{"id":"root","component":"Text","text":"x","__proto__":{"admin":true}}]}');
  assert.equal(proto.ok, false);
  assert.match(proto.error, /forbidden key/);
});

test('XSS: markup inside text is preserved verbatim as data (rendered as a text node)', () => {
  const payload = '<img src=x onerror="alert(1)"><script>alert(2)</script>';
  const r = parseA2ui({ components: [{ id: 'root', component: 'Text', text: payload }] });
  assert.equal(r.ok, true);
  assert.equal(r.messages[1].updateComponents.components[0].text, payload);
});

test('structural errors: missing root, dangling child, oversize strings', () => {
  assert.match(parseA2ui({ components: [{ id: 'a', component: 'Text', text: 'x' }] }).error, /"root"/);
  assert.match(parseA2ui({ components: [{ id: 'root', component: 'Card', child: 'ghost' }] }).error, /missing child/);
  assert.match(parseA2ui({ components: [{ id: 'root', component: 'Text', text: 'x'.repeat(5000) }] }).error, /too long/);
});

test('actions become a visible, plain chat message', () => {
  const msg = actionToMessage({ name: 'report_brief', context: { style: ['ทางการ'], detail: 'สรุปยอดขาย Q3', empty: '' } });
  assert.equal(msg, '[การ์ด] report_brief\n- style: ทางการ\n- detail: สรุปยอดขาย Q3');
  assert.ok(actionToMessage({ name: 'x', context: { big: 'y'.repeat(5000) } }).length <= 1500);
  assert.equal(actionToMessage({ name: 'report_brief', context: { style: ['Formal'] } }, 'en'), '[Card] report_brief\n- style: Formal');
  assert.equal(actionToMessage({ name: 'go' }, 'th'), '[การ์ด] go');
});

test('delta glyphs', () => {
  assert.equal(deltaGlyph('up'), '▲');
  assert.equal(deltaGlyph('down'), '▼');
  assert.equal(deltaGlyph('flat'), '■');
  assert.equal(deltaGlyph('new'), '●');
});

test('cardLang follows the card text: Thai -> th, otherwise en', () => {
  assert.equal(cardLang([{ updateComponents: { components: [{ id: 'q', component: 'Text', text: 'อยากได้รายงานแบบไหน' }] } }]), 'th');
  assert.equal(cardLang([{ updateComponents: { components: [{ id: 'q', component: 'Text', text: 'What kind of report?' }] } }]), 'en');
  assert.equal(cardLang(undefined), 'en');
  assert.equal(cardLang([{ updateComponents: { components: [{ id: 'c', component: 'Chart', unit: '฿', title: 'Sales' }] } }]), 'en'); // baht sign is not Thai text
});

test('CARD_UI has an English set with no Thai text', () => {
  assert.ok(Object.values(CARD_UI.en).every((v) => !/[\u0E00-\u0E7F]/.test(v)));
  assert.deepEqual(Object.keys(CARD_UI.en).sort(), Object.keys(CARD_UI.th).sort());
});

// ---- Chart -----------------------------------------------------------------

const chartCard = (chart) => ({
  components: [
    { id: 'root', component: 'Card', child: 'c' },
    { id: 'c', component: 'Chart', ...chart },
  ],
});
const bar = { chartType: 'bar', categories: ['ม.ค.', 'ก.พ.'], series: [{ label: 'ยอดขาย', values: [1200.3, 1400], deltas: [null, { pct: 16.6, direction: 'up' }] }] };

test('Chart: valid cards of every type parse and are pinned to the Softnix catalog', () => {
  for (const chartType of ['bar', 'line', 'area', 'stackedBar', 'hbar', 'progress', 'pie', 'donut']) {
    const r = parseA2ui(chartCard({ ...bar, chartType }));
    assert.equal(r.ok, true, `${chartType}: ${r.error}`);
    assert.equal(r.messages[0].createSurface.catalogId, SOFTNIX_CATALOG_ID);
  }
  const nulls = parseA2ui(chartCard({ chartType: 'line', categories: ['a', 'b', 'c'], series: [{ label: 's', values: [1, null, 3] }] }));
  assert.equal(nulls.ok, true);
});

test('Chart: invalid props are rejected with a reason', () => {
  const bad = [
    [{ ...bar, chartType: 'radar' }, /chartType/],
    [{ ...bar, categories: [] }, /categories/],
    [{ ...bar, categories: ['a'.repeat(61), 'b'] }, /categories/],
    [{ ...bar, series: [{ label: 's', values: [1] }] }, /one value per category/],
    [{ ...bar, series: [{ label: 's', values: ['1', 2] }] }, /numbers or null/],
    [{ ...bar, series: [{ label: 's', values: [Infinity, 2] }] }, /numbers or null/],
    [{ ...bar, series: [{ label: 's', values: [1, 2], deltas: [null, { pct: 1, direction: 'sideways' }] }] }, /deltas/],
    [{ ...bar, series: Array.from({ length: 9 }, (_, i) => ({ label: `s${i}`, values: [1, 2] })) }, /series/],
    [{ ...bar, chartType: 'pie', series: [bar.series[0], { label: 'b', values: [1, 2] }] }, /exactly one series/],
    [{ ...bar, chartType: 'pie', series: [{ label: 's', values: [-1, 2] }] }, />= 0/],
    [{ chartType: 'donut', categories: Array.from({ length: 13 }, (_, i) => `c${i}`), series: [{ label: 's', values: Array(13).fill(1) }] }, /slices/],
  ];
  for (const [chart, re] of bad) {
    const r = parseA2ui(chartCard(chart));
    assert.equal(r.ok, false, JSON.stringify(chart).slice(0, 80));
    assert.match(r.error, re);
  }
  assert.equal(validateChart({ ...bar, categories: Array.from({ length: 60 }, String), series: Array.from({ length: 8 }, (_, i) => ({ label: `${i}`, values: Array(60).fill(1) })) }), null);
  assert.equal(CHART_LIMITS.maxPoints, 480);
});

test('Chart XSS: markup in labels/title stays data; no new keys can smuggle HTML', () => {
  const xss = '<img src=x onerror=alert(1)>';
  const r = parseA2ui(chartCard({ ...bar, title: xss, categories: [xss, '<script>x</script>'], series: [{ label: xss, values: [1, 2] }] }));
  assert.equal(r.ok, true);
  const c = r.messages[1].updateComponents.components[1];
  assert.equal(c.categories[0], xss);
  assert.equal(c.series[0].label, xss);
  const proto = parseA2ui('{"components":[{"id":"root","component":"Chart","chartType":"bar","categories":["a"],"series":[{"label":"s","values":[1],"__proto__":{"x":1}}]}]}');
  assert.equal(proto.ok, false);
});

test('chart maths: nice scale, formatting, label thinning, truncation', () => {
  assert.deepEqual(niceScale(0, 1400).ticks, [0, 500, 1000, 1500]);
  const neg = niceScale(-120, 300);
  assert.ok(neg.min <= -120 && neg.max >= 300 && neg.ticks.includes(0));
  assert.deepEqual(niceScale(0, 0).ticks, [0, 0.25, 0.5, 0.75, 1]);
  assert.equal(formatValue(1234.5, 2), '1,234.50');
  assert.equal(formatValue(null), '–');
  assert.equal(compactValue(1500), '1.5K');
  assert.equal(compactValue(2_500_000), '2.5M');
  assert.equal(compactValue(950), '950');
  assert.equal(labelStep(31, 300), 7); // 5 labels fit in 300px at 56px
  assert.equal(labelStep(5, 1000), 1);
  assert.equal(truncate('กรุงเทพมหานคร', 5), 'กรุง…'); // 4 chars + ellipsis = 5
  assert.equal(truncate('abc', 5), 'abc');
});

test('chart maths: pie shares, arcs, stacks and ranges', () => {
  const s = pieSlices([1, 3, null, -2]);
  assert.deepEqual(s.map((x) => x.pct), [25, 75, 0, 0]);
  assert.ok(Math.abs(s[1].end - Math.PI * 2) < 1e-9);
  assert.match(arcPath(50, 50, 40, 0, 0, Math.PI), /^M50,50 L50,10 A40,40 0 0 1 50,90 Z$/);
  assert.match(arcPath(50, 50, 40, 20, 0, Math.PI * 2), /A20,20/);
  const series = [{ label: 'a', values: [1, -2, null] }, { label: 'b', values: [3, 1, 4] }];
  assert.deepEqual(stackTotals(series, 3), [4, -1, 4]);
  assert.deepEqual(valueRange('stackedBar', series, 3), [-1, 4]);
  assert.deepEqual(valueRange('bar', series, 3), [-2, 4]);
});
