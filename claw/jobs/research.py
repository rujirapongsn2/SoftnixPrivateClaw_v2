"""Research evidence: fetched-source provenance, not a claim of factual truth."""
import hashlib
import re

from claw.jobs.provider import source_context


def is_multistep_research(content):
    return bool(re.search(r'\b(?:research|investigate)\b|ค้นคว้า|วิจัย|ค้นหาข้อมูล', content, re.I)
                and re.search(r'\b(?:compare|summari[sz]e|sources|report)\b|เปรียบเทียบ|สรุป|หลายแหล่ง|รายงาน', content, re.I))


def cached_source(url):
    ctx = source_context()
    return ctx.state.get('research_sources', {}).get(url) if ctx else None


async def record_source(url, text, status):
    ctx = source_context()
    if ctx is None or not 200 <= status < 300 or not text.strip():
        return
    sources = dict(ctx.state.get('research_sources', {}))
    if url not in sources and len(sources) >= 100:
        return  # bound persisted source material per job
    sources[url] = {'text': text, 'sha256': hashlib.sha256(text.encode()).hexdigest(),
                    'status': status, 'origin': 'web_fetch'}
    await ctx.checkpoint({**ctx.state, 'research_sources': sources})


def research_evidence(state, final):
    cited = set(re.findall(r'https?://[^\s<>\[\]"\)]+', final or ''))
    sources = state.get('research_sources', {})
    proven = {url: {'sha256': item['sha256']} for url, item in sources.items()
              if url in cited and item.get('origin') == 'web_fetch'
              and 200 <= item.get('status', 0) < 300
              and hashlib.sha256(item.get('text', '').encode()).hexdigest() == item.get('sha256')}
    # Require every cited URL to have been retrieved, not invented in the answer.
    if len(proven) < 2 or set(proven) != cited:
        return None
    return {'sources': proven, 'answer_sha256': hashlib.sha256(final.encode()).hexdigest(),
            'validation_scope': 'retrieved source provenance and citations only; factual completeness not verified'}
