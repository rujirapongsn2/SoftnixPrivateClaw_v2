"""Rule-based security review of a skill, mapped to the OWASP Agentic Skills Top 10 (AST01-AST10).

`assess` is pure: it takes the skill's text and bundle files and returns findings, with no network and no
LLM. Storing results and the startup backfill live in claw/db/stores.py. Every rule
has a stable id `ASTxx-Rnn` that reports and the web UI key on. Pattern rules can be evaded, so a clean
result means "no findings", never "safe" (OWASP AST08).

Bump SCANNER_VERSION whenever a rule is added or changed: stored results of older versions are re-scanned.
"""

from __future__ import annotations

import codecs
import hashlib
import re
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import PurePosixPath
from typing import Iterable

SCANNER_VERSION = 4

SEVERITIES = ("info", "low", "medium", "high", "critical")
_RANK = {s: i for i, s in enumerate(SEVERITIES)}
NONE = "none"  # max_severity of a skill with no findings above info

# Which AST items the platform covers instead of the skill, or that do not apply to PrivateClaw.
PLATFORM_STATUS = {
    "AST06": "platform",  # shell runs in a per-command Docker sandbox with network=none
    "AST08": "pattern_only",  # unless the AI review is on (claw/skills/ai_review.py)
    "AST09": "platform",  # owner, sharing scope, subscriptions and audit log exist
    "AST10": "not_applicable",  # one SKILL.md format
}

# Every bundle file that decodes as UTF-8 is scanned. Script rules apply to all of them except prose and
# plain data, so .ps1, .ts, .rb, .bat or an extensionless `scripts/setup` count as scripts.
PROSE_SUFFIXES = {".md", ".markdown", ".txt", ".rst"}
DATA_SUFFIXES = {".json", ".yaml", ".yml", ".toml", ".csv", ".tsv", ".css", ".mmd", ".xml"}
# Files that should never be text. They are still searched for the pattern rules, since nothing stops a
# script from being named logo.png and run with `bash logo.png`.
BINARY_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".ico", ".woff", ".woff2", ".ttf", ".otf", ".pdf"}
MAX_SCAN_CHARS = 2 * 1024 * 1024  # per file; bundles already cap files at 2 MB
EXCERPT = 200


@dataclass(frozen=True)
class Finding:
    ast: str
    rule: str
    severity: str
    title: str
    file: str
    line: int
    excerpt: str
    count: int = 1  # matches of this rule in this file, the first one is the evidence
    detail: str = ""  # the AI review's reason; pattern rules have none
    source: str = "pattern"  # pattern | ai

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Assessment:
    sha256: str
    scanner_version: int
    max_severity: str
    findings: tuple[Finding, ...]
    fingerprint: str = ""  # content_fingerprint: what an admin block matches


@dataclass(frozen=True)
class LineRule:
    rule: str
    ast: str
    severity: str
    title: str
    pattern: re.Pattern
    scripts_only: bool = False


def _rx(p: str) -> re.Pattern:
    return re.compile(p, re.IGNORECASE)


_CREDENTIAL = (
    r"(~|\$HOME|%USERPROFILE%|/root|/home/[^/\s]+)/\.(ssh|aws|gnupg|kube|docker|netrc|git-credentials)\b"
    r"|\bid_(rsa|dsa|ecdsa|ed25519)\b|\.aws/credentials|/etc/shadow|cookies\.sqlite|[/\\]Login Data\b"
    r"|\bsecurity\s+(find|dump)-(generic-password|internet-password|keychain)\b|\blogin\.keychain"
)
_SEND = r"\b(curl|wget|invoke-webrequest|iwr|requests\.(post|put)|httpx\.(post|put)|fetch\s*\(|axios\.(post|put)|nc\s)\b|https?://"

LINE_RULES: tuple[LineRule, ...] = (
    LineRule("AST01-R01", "AST01", "high", "Reads a credential store (SSH keys, cloud credentials, keychain)", _rx(_CREDENTIAL)),
    LineRule(
        "AST01-R03",
        "AST01",
        "critical",
        "Reverse shell pattern",
        _rx(r"/dev/tcp/|\bnc(at)?\b[^\n]{0,200}\s-e\s|\bbash\s+-i\s*>&|\bsocat\b[^\n]{0,200}\bexec:|\bmkfifo\b[^\n]{0,200}\bnc\b"),
    ),
    LineRule(
        "AST01-R04",
        "AST01",
        "high",
        "Tells the agent to ignore its instructions",
        _rx(
            r"\b(ignore|disregard|forget|override)\s+(all\s+|any\s+|the\s+|your\s+)?(previous|prior|above|earlier|system)\s+"
            r"(instructions?|prompts?|rules|guidelines)"
            r"|ไม่ต้อง(สนใจ|ทำตาม)คำสั่ง(ก่อนหน้า|ของระบบ)|เพิกเฉยต่อคำสั่ง|ลืมคำสั่ง(ก่อนหน้า|เดิม)"
        ),
    ),
    LineRule(
        "AST01-R05",
        "AST01",
        "high",
        "Tells the agent to hide what it does from the user",
        _rx(
            r"\b(do\s+not|don't|never)\s+(tell|inform|mention|reveal|show|notify)\s+(this\s+|it\s+|anything\s+)?(to\s+)?the\s+user"
            r"|\bwithout\s+(telling|informing|asking|notifying)\s+the\s+user|\bsilently\s+(send|upload|post|delete|run|execute)"
            r"|(อย่า|ไม่ต้อง)(บอก|แจ้ง)ผู้ใช้|โดยไม่(บอก|แจ้ง|ถาม)ผู้ใช้"
        ),
    ),
    LineRule(
        "AST01-R06",
        "AST01",
        "critical",
        "Downloads and runs remote code",
        _rx(r"\b(curl|wget)\b[^\n|]{0,300}\|\s*(sudo\s+)?(ba|z)?sh\b|\biex\s*\(|\binvoke-expression\b[^\n]{0,200}(iwr|invoke-webrequest|webclient)"),
    ),
    LineRule(
        "AST01-R07",
        "AST01",
        "medium",
        "Writes to the agent's identity or memory files",
        _rx(r"\b(write|append|overwrite|modify|edit|update|replace)\b[^\n]{0,40}\b(SOUL|MEMORY|AGENTS|CLAUDE)\.md\b"),
    ),
    LineRule(
        "AST03-R01",
        "AST03",
        "medium",
        "Script runs other programs or evaluates code",
        _rx(r"\bsubprocess\.|\bos\.(system|popen|exec\w*)\s*\(|\bchild_process\b|\bnew\s+Function\s*\(|(?<![\w.])(eval|exec)\s*\("),
        scripts_only=True,
    ),
    LineRule(
        "AST03-R02",
        "AST03",
        "medium",
        "Script makes network requests",
        _rx(r"\b(requests|httpx|urllib\.request|aiohttp|axios|socket)\.|\bfetch\s*\(|\bhttp\.request\s*\(|\bcurl\b|\bwget\b"),
        scripts_only=True,
    ),
    LineRule(
        "AST03-R03",
        "AST03",
        "high",
        "Asks for elevated privileges or to skip confirmations",
        _rx(
            r"\bsudo\s+\w|\bchmod\s+(-R\s+)?777\b|\b(switch|set|change)\b[^\n]{0,30}\b(auto|permission)\s+mode"
            r"|\b(skip|bypass|disable)\b[^\n]{0,20}\b(confirmation|approval|permission)s?\b"
        ),
    ),
    LineRule(
        "AST04-R03",
        "AST04",
        "critical",
        "Unsafe YAML tag",
        _rx(r"!!python/|!!ruby/|!!java/|tag:yaml\.org,2002:python/"),
    ),
    LineRule(
        "AST05-R02",
        "AST05",
        "high",
        "Tells the agent to follow instructions from a remote URL",
        _rx(
            r"\b(follow|obey|execute|run|apply)\s+(the\s+)?(instructions|steps|commands|rules)\s+(at|from|in|on)\s+<?https?://"
            r"|\b(fetch|download|read|open)\b(?=[^\n]{0,60}https?://)[^\n]{8,630}?\b(and|then)\s+(follow|execute|run|obey)"
            r"|ทำตามคำสั่ง(ที่|จาก)[^\n]{0,20}https?://"
        ),
    ),
    LineRule(
        "AST06-R01",
        "AST06",
        "high",
        "Tries to leave the sandbox or reach the host",
        _rx(r"--privileged\b|/var/run/docker\.sock|--network[ =]host\b|\bnsenter\b|\bchroot\s+/host|--pid[ =]host\b"),
    ),
)

_CREDENTIAL_RX = _rx(_CREDENTIAL)
_SEND_RX = _rx(_SEND)
_URL = re.compile(r"https?://[^\s<>()\[\]\"'`]+", re.IGNORECASE)
_BASE64 = re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{240,}={0,2}")
# A blob is exempt only as the payload of a data URI: `data:<mime>;base64,` right before it.
# Searched only this far back from each blob, so a line of thousands of data URIs stays linear.
_DATA_URI_WINDOW = 256
_DATA_URI_PREFIX = re.compile(r"data:[\w.+-]+/[\w.+-]+(;[\w-]+=[\w.-]+)*;base64,$", re.IGNORECASE)
_TAG_CHARS = re.compile("[\U000e0000-\U000e007f]")
_HIDDEN = re.compile("[​-‏ -‮⁠-⁤⁦-⁩﻿]")
_FRONTMATTER = re.compile(r"^﻿?---\s*\n(.*?)\n---\s*(\n|$)", re.S)
_FRONTMATTER_KEY = re.compile(r"^([A-Za-z0-9_-]+)\s*:", re.M)
KNOWN_FRONTMATTER = {"name", "description", "license", "metadata", "allowed-tools", "compatibility"}
# `//tree/`: imports made before the repository URL was normalised kept its trailing slash.
_PINNED_GITHUB = re.compile(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+//?tree/[0-9a-fA-F]{40}")
_BRANDS = re.compile(r"\b(official|verified|openai|anthropic|claude|google|microsoft|github|softnix)\b", re.IGNORECASE)


def content_digest(content: str, bundle_id: str | None, description: str = "") -> str:
    """The key a review is stored under: SKILL.md, the description and the bundle version, whose files
    never change. Of the text as stored: the database columns drop U+0000 (claw/db/types.py)."""
    content, description = content.replace("\0", ""), description.replace("\0", "")
    h = hashlib.sha256()
    h.update(b"SKILL.md\0")
    h.update(content.encode("utf-8"))
    h.update(b"\0description\0" + description.encode("utf-8"))
    h.update(b"\0bundle\0" + (bundle_id or "").encode("utf-8"))
    return h.hexdigest()


def content_fingerprint(content: str, description: str, files: dict[str, bytes]) -> str:
    """The skill's text and files, whatever its id or bundle version: the same skill created again or
    imported again has the same fingerprint, so an admin block follows it."""
    h = hashlib.sha256()
    for path, data in [("SKILL.md", content.replace("\0", "").encode()), ("description", description.replace("\0", "").encode()),
                       *sorted((k, v) for k, v in files.items() if k != "SKILL.md")]:
        h.update(len(path.encode()).to_bytes(4, "big") + path.encode() + len(data).to_bytes(8, "big") + data)
    return h.hexdigest()


def _visible(text: str) -> str:
    """An excerpt a reviewer can read: invisible and control characters shown as code points, capped."""
    out = []
    for ch in text.strip():
        if ch in "\t ":
            out.append(" ")
        elif _TAG_CHARS.match(ch) or _HIDDEN.match(ch) or unicodedata.category(ch) in ("Cc", "Cf"):
            out.append(f"[U+{ord(ch):04X}]")
        else:
            out.append(ch)
        if len(out) >= EXCERPT:
            break
    return "".join(out)[:EXCERPT]


def _lines(text: str) -> Iterable[tuple[int, str]]:
    # Not splitlines(): it also breaks on U+2028, U+2029 and \x0b-\x1e, which would hide them from the
    # hidden-character rule and shift every later line number away from what an editor shows.
    return enumerate((line.removesuffix("\r") for line in text[:MAX_SCAN_CHARS].split("\n")), start=1)


def _scan_text(path: str, text: str, prose: bool = False, patterns_only: bool = False) -> list[Finding]:
    """`patterns_only` (a binary file) runs only the pattern rules, all of them as for a script: the
    character and blob rules would fire on image bytes."""
    suffix = PurePosixPath(path).suffix.lower()
    is_script = patterns_only or (not prose and suffix not in PROSE_SUFFIXES | DATA_SUFFIXES)
    found: dict[str, Finding] = {}

    def hit(rule: str, ast: str, severity: str, title: str, line: int, excerpt: str) -> None:
        prev = found.get(rule)
        if prev is None:
            found[rule] = Finding(ast, rule, severity, title, path, line, _visible(excerpt))
        else:
            found[rule] = Finding(**{**prev.as_dict(), "count": prev.count + 1})

    lines = list(_lines(text))
    if len(text) > MAX_SCAN_CHARS:
        hit("AST08-R02", "AST08", "high", "Too long to review in full, so the end was not checked", len(lines), "")
    for no, line in lines:
        for r in LINE_RULES:
            if (not r.scripts_only or is_script) and r.pattern.search(line):
                hit(r.rule, r.ast, r.severity, r.title, no, line)
        if patterns_only:
            continue
        if suffix not in {".svg", ".html", ".htm", ".css"} and any(
            not _DATA_URI_PREFIX.search(line, max(0, m.start() - _DATA_URI_WINDOW), m.start()) for m in _BASE64.finditer(line)
        ):
            hit("AST04-R02", "AST04", "medium", "Long base64 blob that may hide a payload", no, line)
        if _TAG_CHARS.search(line):
            hit("AST04-R01", "AST04", "critical", "Invisible Unicode tag characters (ASCII smuggling)", no, line)
        elif _HIDDEN.search(line.lstrip("﻿") if no == 1 else line):
            hit("AST04-R06", "AST04", "high", "Zero-width or bidirectional control characters", no, line)

    # A credential read next to a send is exfiltration, not just a mention.
    for no, line in lines:
        if not _CREDENTIAL_RX.search(line):
            continue
        window = " ".join(text for _, text in lines[max(0, no - 3) : no + 2])
        if _SEND_RX.search(window):
            hit("AST01-R02", "AST01", "critical", "Reads credentials and sends data out", no, line)

    if patterns_only:
        return list(found.values())
    if suffix in PROSE_SUFFIXES:
        urls = []
        first = 0
        for no, line in lines:
            for u in _URL.findall(line):
                urls.append(u.rstrip(".,;:"))
                first = first or no
        if urls:
            unique = list(dict.fromkeys(urls))
            found["AST05-R01"] = Finding(
                "AST05", "AST05-R01", "info", "References external URLs", path, first, _visible(" ".join(unique[:10])), len(unique)
            )

    if PurePosixPath(path).name == "requirements.txt":
        for no, line in lines:
            spec = line.split("#", 1)[0].strip()
            if spec and not spec.startswith("-") and "==" not in spec and "@" not in spec:
                hit("AST02-R02", "AST02", "medium", "Dependency is not pinned to an exact version", no, line)
    if PurePosixPath(path).name == "package.json":
        for no, line in lines:
            if re.search(r'"[^"]+"\s*:\s*"(\^|~|\*|>|latest)', line):
                hit("AST02-R02", "AST02", "medium", "Dependency is not pinned to an exact version", no, line)
    return list(found.values())


def _scan_frontmatter(content: str) -> list[Finding]:
    m = _FRONTMATTER.match(content)
    if not m:
        return []
    findings = []
    unknown = sorted({k for k in _FRONTMATTER_KEY.findall(m.group(1)) if k not in KNOWN_FRONTMATTER})
    if unknown:
        findings.append(
            Finding("AST04", "AST04-R04", "low", "Unknown frontmatter keys", "SKILL.md", 1, _visible(", ".join(unknown)), len(unknown))
        )
    name = re.search(r"^name\s*:\s*(.+)$", m.group(1), re.M)
    if name and _BRANDS.search(name.group(1)):
        findings.append(
            Finding("AST04", "AST04-R05", "low", "Name uses a brand or trust word", "SKILL.md", 1, _visible(name.group(1)))
        )
    return findings


def _provenance(source: dict | None) -> list[Finding]:
    """AST02: where the skill came from. A GitHub import pinned to a commit is the best we can verify."""
    kind = str((source or {}).get("source") or "")
    if _PINNED_GITHUB.fullmatch(kind):
        return []
    if source:
        return [Finding("AST02", "AST02-R01", "low", "Uploaded archive with no verifiable publisher", "SKILL.md", 0, _visible(str(kind)))]
    return [Finding("AST02", "AST02-R03", "info", "Written in PrivateClaw, no external source", "SKILL.md", 0, "")]


def max_severity(findings: Iterable[Finding]) -> str:
    worst = max((_RANK[f.severity] for f in findings if f.severity != "info"), default=-1)
    return SEVERITIES[worst] if worst >= 0 else NONE


def combined(findings: Iterable[dict]) -> tuple[str, str]:
    """max_severity and the ast_codes column of stored findings from both review stages."""
    findings = list(findings)
    worst = max((_RANK.get(f.get("severity"), 0) for f in findings), default=0)
    codes = sorted({f["ast"] for f in findings if _RANK.get(f.get("severity"), 0) > 0})
    return (SEVERITIES[worst] if worst else NONE), (f",{','.join(codes)}," if codes else "")


def _text_of(data: bytes) -> str | None:
    try:
        if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
            return data.decode("utf-16")
        return None if b"\0" in data[:8192] else data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _lossy_text_of(data: bytes) -> str:
    """Whatever text a file holds. NULs are dropped, so UTF-16 without a BOM reads as its ASCII."""
    return data.decode("utf-8", errors="replace").replace("\0", "")


def assess(
    content: str, files: dict[str, bytes] | None = None, source: dict | None = None, digest: str = "", description: str = ""
) -> Assessment:
    """Review SKILL.md (`content`), the description and the bundle's other files. `source` is the bundle
    metadata, if imported.

    The description is reviewed on its own because every chat's skills summary carries it, even when
    SKILL.md is never read. `digest` is the `content_digest` the result is stored under."""
    files = {k: v for k, v in (files or {}).items() if k != "SKILL.md"}
    findings: list[Finding] = [*_scan_frontmatter(content), *_scan_text("SKILL.md", content), *_provenance(source)]
    if description.strip() and description not in content:  # an imported skill's comes from its frontmatter
        findings.extend(_scan_text("description", description, prose=True))
    for path in sorted(files):
        binary = PurePosixPath(path).suffix.lower() in BINARY_SUFFIXES
        text = _text_of(files[path])
        if text is None and not binary:
            findings.append(
                Finding("AST08", "AST08-R01", "high", "File is not clean text, so it was only partly reviewed", path, 0, "")
            )
        findings.extend(_scan_text(path, text if text is not None else _lossy_text_of(files[path]), patterns_only=binary))
    findings.sort(key=lambda f: (-_RANK[f.severity], f.ast, f.rule, f.file, f.line))
    return Assessment(
        digest, SCANNER_VERSION, max_severity(findings), tuple(findings), content_fingerprint(content, description, files)
    )
