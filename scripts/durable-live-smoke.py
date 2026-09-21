"""Bounded real-provider HTTP/worker smoke against the isolated dev server only."""
import asyncio
import argparse
import json
from pathlib import Path
import secrets
import time

import httpx
import websockets


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--research', action='store_true')
    args = parser.parse_args()
    base = 'http://127.0.0.1:8701'
    report = {'kind': 'live provider, isolated dev, HTTP and independent worker'}
    async with httpx.AsyncClient(base_url=base, timeout=15) as client:
        response = await client.post('/api/auth/register', json={
            'email': f'durable-smoke-{secrets.token_hex(4)}@example.invalid',
            'password': secrets.token_urlsafe(20), 'display_name': 'Dev smoke'})
        response.raise_for_status()
        body = response.json()
        token = body.get('access_token') or body.get('token')
        if not token:
            raise RuntimeError('registration returned no session token')
        client.headers['Authorization'] = 'Bearer ' + token
        response = await client.post('/api/sessions', json={'title': 'Durable dev synthetic workbook smoke'})
        response.raise_for_status()
        sid = response.json()['id']
        report['session_id'] = sid
        async with websockets.connect(f'ws://127.0.0.1:8701/ws/chat/{sid}?token={token}') as ws:
            instruction = ('Research and summarize Python lists versus tuples using exactly these two official sources: '
                'https://docs.python.org/3/tutorial/datastructures.html and '
                'https://docs.python.org/3/library/stdtypes.html . Fetch both source pages and cite both in a concise Thai answer. '
                'Do not create files, run shell commands, use connectors or delegate.' if args.research else
                'Create an Excel file named smoke.xlsx with one Data sheet, columns Name and Qty, exactly two rows: Alpha 2 and Beta 3. Write intermediate JSON and use generate_workbook. This is synthetic test data. Do not use shell, web, connectors or delegation. Reply briefly in Thai.')
            await ws.send(json.dumps({'content': instruction, 'permission_mode': 'auto'}))
            started = time.monotonic()
            job = None
            while time.monotonic() - started < 120:
                response = await client.get('/api/jobs', params={'session_id': sid})
                response.raise_for_status()
                rows = response.json()
                if rows:
                    job = rows[0]
                    if job['status'] in {'completed', 'paused', 'failed', 'cancelled', 'awaiting_input'}:
                        break
                await asyncio.sleep(2)
            report['seconds'] = round(time.monotonic() - started, 1)
            report['status'] = job['status'] if job else 'not_admitted'
            report['reason'] = job.get('reason') if job else ''
            report['job_id'] = job['job_id'] if job else ''
            if job and job['status'] not in {'completed', 'paused', 'failed', 'cancelled'}:
                await client.post(f"/api/jobs/{job['job_id']}/cancel")
            if job and job['status'] == 'completed':
                for _ in range(5):
                    await asyncio.sleep(2)
                    current = (await client.get(f"/api/jobs/{job['job_id']}")).json()
                    if current.get('deliveries'):
                        report['deliveries'] = current['deliveries']
                        break
    report_path = Path('/private/tmp/privateclaw-durable-dev') / ('live-research-smoke.json' if args.research else 'live-smoke.json')
    report_path.write_text(json.dumps(report, indent=2))
    print(json.dumps(report))


if __name__ == '__main__':
    asyncio.run(main())
