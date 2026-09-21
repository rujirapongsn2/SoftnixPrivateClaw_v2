"""Real Chromium component E2E with synthetic API responses, never production.

Start: cd web && npx vite --config test/browser/vite.config.ts
Run with DURABLE_UI_BASE=http://127.0.0.1:8702. No provider calls are made.
"""
import os

import pytest
from playwright.async_api import async_playwright, expect


@pytest.mark.parametrize('width', [390, 1280])
async def test_durable_ui_progress_cancel_reconnect_and_delivery(width, tmp_path):
    base = os.environ.get('DURABLE_UI_BASE')
    if not base:
        pytest.skip('isolated UI harness not running')
    assert base == 'http://127.0.0.1:8702'
    job = {'job_id': 'fixture', 'locale': 'th', 'mode': 'privateclaw', 'session_id': 'fixture-session',
           'status': 'running', 'reason': '', 'sequence': 1, 'deliveries': 0,
           'steps': [{'id': 'extract', 'status': 'running', 'reason': '', 'actor': '', 'approval': None}]}
    state = {'offline': False, 'transient_failures': 0, 'cancels': 0}
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            page = await browser.new_page(viewport={'width': width, 'height': 844})
            async def route_api(route):
                if state['offline'] or state['transient_failures']:
                    if state['transient_failures']:
                        state['transient_failures'] -= 1
                    await route.abort()
                elif route.request.url.endswith('/cancel'):
                    state['cancels'] += 1
                    job['status'] = 'cancelled'
                    await route.fulfill(json=job)
                elif '/api/jobs' in route.request.url:
                    await route.fulfill(json=[job])
                else:
                    await route.abort()
            await page.route('**/api/**', route_api)
            await page.goto(base + '/test/browser/durable.html')
            await expect(page.get_by_role('button', name='ยกเลิก', exact=True)).to_be_visible()
            await expect(page.get_by_role('status')).to_contain_text('กำลังทำงาน')
            assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            await page.screenshot(path=str(tmp_path / f'progress-{width}.png'), full_page=True)
            # A single failed status request must retain the last verified card
            # without claiming the chat connection was interrupted.
            state['transient_failures'] = 1
            await page.wait_for_timeout(3500)
            await expect(page.get_by_role('alert')).to_have_count(0)
            await expect(page.get_by_role('status')).to_contain_text('กำลังทำงาน')
            state['offline'] = True
            # Even a sustained status-poll outage is not proof that the job
            # failed. Keep the verified state without alarming the user.
            await page.wait_for_timeout(8500)
            await expect(page.get_by_role('alert')).to_have_count(0)
            await expect(page.get_by_role('status')).to_contain_text('กำลังทำงาน')
            state['offline'] = False
            job['status'], job['deliveries'] = 'completed', 1
            job['steps'][0]['status'] = 'completed'
            await expect(page.get_by_role('status')).to_contain_text('เสร็จแล้ว', timeout=12000)
            await expect(page.locator('html')).to_have_attribute('data-deliveries', '1')
            await page.wait_for_timeout(10500)
            await expect(page.locator('html')).to_have_attribute('data-deliveries', '1')
            job['status'] = 'running'
            await page.reload()
            await page.get_by_role('button', name='ยกเลิก', exact=True).click()
            await expect(page.get_by_role('status')).to_contain_text('ยกเลิกแล้ว')
            assert state['cancels'] == 1
        finally:
            await browser.close()


async def test_older_backend_without_jobs_api_is_silent_and_stops_polling():
    base = os.environ.get('DURABLE_UI_BASE')
    if not base:
        pytest.skip('isolated UI harness not running')
    calls = 0
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            page = await browser.new_page()

            async def route_api(route):
                nonlocal calls
                calls += 1
                await route.fulfill(status=404, json={'detail': 'Not Found'})

            await page.route('**/api/jobs**', route_api)
            await page.goto(base + '/test/browser/durable.html')
            await page.wait_for_timeout(3500)
            await expect(page.get_by_role('alert')).to_have_count(0)
            assert calls == 1
        finally:
            await browser.close()
