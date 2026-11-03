// Browser harness for the A2UI renderer (valid / invalid / XSS / price card).
// Run: npx vite --config test/browser/vite.config.ts  then open /test/browser/a2ui.html
import { Markdown } from '@astryxdesign/core/Markdown';
import { createRoot } from 'react-dom/client';
import { A2uiCard, makeA2uiCodeRenderer } from '../../src/a2ui/A2uiCard';
import '../../src/styles.css';
import clarify from './a2ui-clarify.fixture.json';
import price from './a2ui-price.fixture.json';
import locationAsk from './a2ui-location.fixture.json';

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
const wx = (o: Record<string, unknown>) =>
  JSON.stringify({ components: [{ id: 'root', component: 'WeatherCard', unit: 'C', ...o }] });
const weatherCards = [
  { condition: 'partlyCloudy', isDay: true, temperature: 30, label: 'มีเมฆบางส่วน', date: 'วันจันทร์ที่ 2 พฤศจิกายน 2569', location: 'กรุงเทพมหานคร, ประเทศไทย', updated: 'อัปเดต 14:30 · ข้อมูลจาก Open-Meteo' },
  { condition: 'rain', isDay: true, temperature: 26, label: 'Rain', date: 'Monday, 2 November 2026', location: 'Chiang Mai, Thailand', updated: 'Updated 14:30 · Data from Open-Meteo' },
  { condition: 'clear', isDay: false, temperature: 24, label: 'ท้องฟ้าแจ่มใส', date: 'วันจันทร์ที่ 2 พฤศจิกายน 2569', location: 'ภูเก็ต, ประเทศไทย', updated: 'อัปเดต 21:05 · ข้อมูลจาก Open-Meteo' },
  { condition: 'thunder', isDay: true, temperature: 28, label: 'พายุฝนฟ้าคะนอง', location: 'หาดใหญ่, ประเทศไทย' },
  { condition: 'cloudy', isDay: true, temperature: 22, label: 'Cloudy', location: 'London, United Kingdom' },
  { condition: 'fog', isDay: true, temperature: 12, label: 'Fog', location: 'San Francisco, United States' },
  { condition: 'snow', isDay: true, temperature: -3, unit: 'F', label: 'Snow', location: 'Sapporo, Japan' },
  { condition: 'drizzle', isDay: false, temperature: 25, label: 'ฝนปรอย', location: 'เชียงราย, ประเทศไทย' },
];
const md = '```a2ui\n' + JSON.stringify(clarify) + '\n```\n\n```python\nprint(1)\n```';
const live = { code: makeA2uiCodeRenderer({ interactive: true, onAction: record }) };

createRoot(document.getElementById('root')!).render(
  <main style={{ maxWidth: 760, margin: '16px auto', padding: 12, display: 'grid', gridTemplateColumns: 'minmax(0, 1fr)', gap: 24 }}>
    <section id="fenced"><h4>1. fenced ```a2ui in Markdown</h4><Markdown components={live}>{md}</Markdown></section>
    <section id="invalid"><h4>2. invalid JSON</h4><A2uiCard source={'{"components": [ {"id": "root", '} interactive onAction={record} /></section>
    <section id="xss"><h4>3. XSS payload</h4><A2uiCard source={xss} interactive onAction={record} /></section>
    <section id="weather"><h4>5. weather_now artifacts (inside the chat's .claw-artifacts flex row)</h4>{weatherCards.map((w, i) => <div key={i} className="claw-artifacts"><A2uiCard source={wx(w)} interactive={false} language="json" /></div>)}</section>
    <section id="weather-narrow" style={{ maxWidth: 360 }}><h4>6. weather, narrow column</h4><div className="claw-artifacts"><A2uiCard source={wx(weatherCards[0])} interactive={false} language="json" /></div></section>
    <section id="location"><h4>8. weather_now with no location: ask where the user is</h4><A2uiCard source={JSON.stringify(locationAsk)} interactive onAction={record} language="json" /></section>
    <section id="location-inline"><h4>9. location button typed inline by the model</h4><A2uiCard source={JSON.stringify(locationAsk)} interactive onAction={record} /></section>
    <section id="weather-inline"><h4>7. weather typed inline by the model</h4><A2uiCard source={wx(weatherCards[0])} interactive={false} /></section>
    <section id="price"><h4>4. price watch artifact</h4><A2uiCard source={JSON.stringify(price)} interactive={false} /></section>
  </main>,
);
