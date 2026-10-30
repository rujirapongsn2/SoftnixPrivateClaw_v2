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
});

test('CARD_UI has an English set with no Thai text', () => {
  assert.ok(Object.values(CARD_UI.en).every((v) => !/[\u0E00-\u0E7F]/.test(v)));
  assert.deepEqual(Object.keys(CARD_UI.en).sort(), Object.keys(CARD_UI.th).sort());
});
