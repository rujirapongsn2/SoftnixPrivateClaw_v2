// Browser harness for the A2UI renderer (valid / invalid / XSS / price card).
// Run: npx vite --config test/browser/vite.config.ts  then open /test/browser/a2ui.html
import { Markdown } from '@astryxdesign/core/Markdown';
import { createRoot } from 'react-dom/client';
import { A2uiCard, makeA2uiCodeRenderer } from '../../src/a2ui/A2uiCard';
import '../../src/styles.css';
import clarify from './a2ui-clarify.fixture.json';
import price from './a2ui-price.fixture.json';

const record = (t: string) => {
  (window as any).__a2uiActions = [...((window as any).__a2uiActions ?? []), t];
};
const xss = JSON.stringify({
  components: [
    { id: 'root', component: 'Card', child: 'col' },
    { id: 'col', component: 'Column', children: ['t1', 't2'] },
    { id: 't1', component: 'Text', text: '<img src=x onerror="window.__pwned=1"><script>window.__pwned=2</script>' },
    { id: 't2', component: 'Text', text: '**not bold** [link](javascript:alert(1))' },
  ],
});
const md = '```a2ui\n' + JSON.stringify(clarify) + '\n```\n\n```python\nprint(1)\n```';
const live = { code: makeA2uiCodeRenderer({ interactive: true, onAction: record }) };

createRoot(document.getElementById('root')!).render(
  <main style={{ maxWidth: 760, margin: '16px auto', padding: 12, display: 'grid', gap: 24 }}>
    <section id="fenced"><h4>1. fenced ```a2ui in Markdown</h4><Markdown components={live}>{md}</Markdown></section>
    <section id="invalid"><h4>2. invalid JSON</h4><A2uiCard source={'{"components": [ {"id": "root", '} interactive onAction={record} /></section>
    <section id="xss"><h4>3. XSS payload</h4><A2uiCard source={xss} interactive onAction={record} /></section>
    <section id="price"><h4>4. price watch artifact</h4><A2uiCard source={JSON.stringify(price)} interactive={false} /></section>
  </main>,
);
