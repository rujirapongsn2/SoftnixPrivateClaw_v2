"""Download a public GitHub archive at a full commit ID, never follow redirects."""

import re
import httpx
from claw.skills.bundles import MAX_ARCHIVE


async def download_bundle(repository: str, commit: str):
    match = re.fullmatch(r"https://github.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/?", repository)
    if not match or not re.fullmatch(r"[0-9a-fA-F]{40}", commit):
        raise ValueError("Use a public GitHub repository URL and a full 40-character commit SHA")
    owner, repo = match.groups()
    url = f"https://codeload.github.com/{owner}/{repo}/zip/{commit}"
    chunks = bytearray()
    async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
        async with client.stream("GET", url) as response:
            if response.status_code != 200:
                raise ValueError("Could not download this repository commit")
            async for chunk in response.aiter_bytes():
                chunks.extend(chunk)
                if len(chunks) > MAX_ARCHIVE:
                    raise ValueError("Repository ZIP exceeds 12 MB; upload the skill folder as ZIP instead")
    return bytes(chunks)
