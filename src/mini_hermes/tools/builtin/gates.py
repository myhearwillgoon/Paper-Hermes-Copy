"""Gate checkers(注册为工具,E10)。移植自 academic-paper-loop/GATE_MATRIX.yaml。

返回 JSON 字符串,格式对齐 gate_1_report.json:
{gate_id, gate_name, status: PASSED|FAILED, criteria: [{name, threshold, actual, passed}],
 violations: [...]}
G1/G4 escalation=BLOCK;G2/G3=USER_DECISION(由 workflow YAML 的 gates 节定义)。

G1 的外部存在性验证(arXiv / Semantic Scholar)全部走 M0 cassette 层:
external_mode=live → record(透传+落盘);cassette → replay(miss 即违规)。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from ...cassettes import Cassette, CassetteMissError, CassetteMode, http_get
from ..registry import Registry, ToolContext

DEFAULT_VENUES = [
    "arXiv", "NeurIPS", "ICML", "ICLR", "ACL", "EMNLP", "AAMAS",
    "IEEE", "ACM", "Nature", "Science", "JMLR", "COLM", "COLING",
]

ARXIV_API = "https://export.arxiv.org/api/query"
S2_API = "https://api.semanticscholar.org/graph/v1/paper/search"

# GATE_MATRIX.yaml strong_assertion_patterns 原样
STRONG_ASSERTION_PATTERNS = [
    r"\d+×\s+(speedup|improvement|reduction|increase)",
    r"\d+pp?\s+(noise|floor|gain|improvement)",
    r"\d+%\s+(overhead|reduction|improvement|success)",
    r"O\([^)]+\)",
    r"n\s*=\s*\d+-\d+",
    r"\d+\s+(agents?|systems?|papers?)",
]

DISCLOSURE_KEYWORDS = [
    "reconstructed", "approximate", "estimated",
    "theoretical", "mathematical", "formula",
    "simulated", "simulation", "illustrative",
    "conceptual", "qualitative", "assessment",
]


def _result(gate_id: str, gate_name: str, criteria: list[dict], violations: list) -> str:
    status = "PASSED" if all(c["passed"] for c in criteria) else "FAILED"
    return json.dumps(
        {"gate_id": gate_id, "gate_name": gate_name, "status": status,
         "criteria": criteria, "violations": violations},
        ensure_ascii=False,
    )


def _criterion(name: str, threshold, actual, passed: bool) -> dict:
    return {"name": name, "threshold": threshold, "actual": actual, "passed": passed}


# ---------------------------------------------------------------------------
# G1 Citation Quality + Paper Existence
# ---------------------------------------------------------------------------

_REF_LINE = re.compile(r"^\[(\d+)\]\s+(.+)$", re.MULTILINE)
_YEAR = re.compile(r"\b(19|20)\d{2}\b")
_ARXIV_ID = re.compile(r"arXiv:(\d{4}\.\d{4,5})(v\d+)?", re.IGNORECASE)


def _parse_references(text: str) -> list[dict]:
    refs = []
    for m in _REF_LINE.finditer(text):
        entry = m.group(2).strip()
        year_m = _YEAR.search(entry)
        arxiv_m = _ARXIV_ID.search(entry)
        title = ""
        title_m = re.search(r"\)\.\s*(.+?)\.\s*[\*(]", entry) or re.search(
            r"\d{4}\)?\.\s*(.+?)\.\s", entry
        )
        if title_m:
            title = title_m.group(1).strip()
        refs.append({
            "n": int(m.group(1)),
            "entry": entry,
            "year": int(year_m.group(0)) if year_m else None,
            "arxiv_id": arxiv_m.group(1) if arxiv_m else None,
            "has_access": bool(arxiv_m or "doi.org/" in entry or "http" in entry),
            "title": title,
        })
    return refs


def _venue_of(entry: str, venues: list[str]) -> str | None:
    # 先剥掉 arXiv ID / URL,否则 "arXiv:xxxx" 会让任何条目都命中 venue=arXiv
    stripped = _ARXIV_ID.sub("", entry)
    stripped = re.sub(r"https?://\S+", "", stripped)
    low = stripped.lower()
    for v in venues:
        if v.lower() in low:
            return v
    return None


def _verify_existence(ref: dict, cassette: Cassette | None, violations: list) -> bool:
    """AT_LEAST_ONE:arXiv id → arXiv API;否则标题 → Semantic Scholar。"""
    try:
        if ref["arxiv_id"]:
            resp = http_get(ARXIV_API, params={"id_list": ref["arxiv_id"]},
                            cassette=cassette, timeout=30)
            return resp.status_code == 200 and b"<entry>" in resp.content
        if ref["title"]:
            resp = http_get(S2_API, params={"query": ref["title"][:120], "limit": "1"},
                            cassette=cassette, timeout=30)
            if resp.status_code == 200:
                return (resp.json().get("total") or 0) > 0
        return False
    except CassetteMissError as e:
        violations.append(f"[{ref['n']}] cassette miss:{e}")
        return False
    except Exception as e:
        violations.append(f"[{ref['n']}] 验证请求失败:{e}")
        return False


def _gate_citation_check(args: dict, ctx: ToolContext) -> str:
    workdir = Path(args.get("workdir") or ctx.cwd)
    targets = args.get("targets") or {}
    year_threshold = int(targets.get("year_threshold", 2024))
    venues = targets.get("venues", DEFAULT_VENUES)
    ref_path = workdir / args.get("references_file", "EXTENDED_REFERENCES.md")

    criteria: list[dict] = []
    violations: list = []

    exists = ref_path.is_file()
    criteria.append(_criterion("EXTENDED_REFERENCES.md", "exists",
                               "✓" if exists else "missing", exists))
    if not exists:
        return _result("G1", "CITATION_QUALITY", criteria, violations)

    refs = _parse_references(ref_path.read_text(encoding="utf-8"))
    total = len(refs)
    criteria.append(_criterion("Reference List", "complete",
                               f"{total} entries", total > 0))

    bad_year = [r["n"] for r in refs if r["year"] is None or r["year"] < year_threshold]
    criteria.append(_criterion("Year Gate", f">={year_threshold}",
                               f"{total - len(bad_year)}/{total}", not bad_year))
    violations += [f"[{n}] 年份缺失或 < {year_threshold}" for n in bad_year]

    bad_venue = [r["n"] for r in refs if _venue_of(r["entry"], venues) is None]
    criteria.append(_criterion("Venue Gate", "approved venues",
                               f"{total - len(bad_venue)}/{total}", not bad_venue))
    violations += [f"[{n}] venue 不在许可列表" for n in bad_venue]

    bad_access = [r["n"] for r in refs if not r["has_access"]]
    criteria.append(_criterion("Access Gate", "arXiv/DOI/URL",
                               f"{total - len(bad_access)}/{total}", not bad_access))
    violations += [f"[{n}] 缺 arXiv ID / DOI / URL" for n in bad_access]

    mode = args.get("external_mode", "cassette")
    cassette = None
    if mode in ("live", "cassette"):
        cassette_dir = args.get("cassette_dir") or str(workdir / "cassettes")
        cassette = Cassette(cassette_dir,
                            CassetteMode.RECORD if mode == "live" else CassetteMode.REPLAY)
    unverified = []
    for ref in refs:
        if not _verify_existence(ref, cassette, violations):
            unverified.append(ref["n"])
    criteria.append(_criterion("Existence Gate", "AT_LEAST_ONE db verifies",
                               f"{total - len(unverified)}/{total} verified",
                               not unverified))
    violations += [f"[{n}] 外部数据库无法验证存在性" for n in unverified]

    return _result("G1", "CITATION_QUALITY", criteria, violations)


# ---------------------------------------------------------------------------
# G2 Data Veracity(强断言必须有 [X] 引用)
# ---------------------------------------------------------------------------


def _gate_data_veracity(args: dict, ctx: ToolContext) -> str:
    workdir = Path(args.get("workdir") or ctx.cwd)
    draft_path = workdir / args.get("draft_file", "DRAFT.md")
    criteria: list[dict] = []
    violations: list = []

    if not draft_path.is_file():
        criteria.append(_criterion("draft exists", "exists", "missing", False))
        return _result("G2", "DATA_VERACITY", criteria, violations)

    text = draft_path.read_text(encoding="utf-8")
    unverified: list[str] = []
    for pattern in STRONG_ASSERTION_PATTERNS:
        for m in re.finditer(pattern, text):
            # 同一句子内(前后 160 字符窗口到句界)必须有 [N] 引用标记
            start = max(text.rfind(".", 0, m.start()), text.rfind("\n", 0, m.start())) + 1
            end_candidates = [text.find(".", m.end()), text.find("\n", m.end())]
            end = min((e for e in end_candidates if e != -1), default=len(text))
            sentence = text[start:end + 1]
            if not re.search(r"\[\d+\]", sentence):
                unverified.append(m.group(0))
    criteria.append(_criterion("strong assertions verified", "ZERO_VIOLATIONS",
                               f"{len(unverified)} unverified", not unverified))
    violations += [f"强断言无引用:{a!r}" for a in unverified]
    return _result("G2", "DATA_VERACITY", criteria, violations)


# ---------------------------------------------------------------------------
# G3 Citation Density
# ---------------------------------------------------------------------------


def _word_count(text: str) -> int:
    """中英文混合计数:CJK 字符逐字算,其余按空白分词。"""
    cjk = len(re.findall(r"[一-鿿]", text))
    non_cjk = re.sub(r"[一-鿿]", " ", text)
    return cjk + len(re.findall(r"\S+", non_cjk))


def _gate_citation_density(args: dict, ctx: ToolContext) -> str:
    workdir = Path(args.get("workdir") or ctx.cwd)
    targets = args.get("targets") or {}
    draft_path = workdir / args.get("draft_file", "DRAFT.md")
    criteria: list[dict] = []
    violations: list = []

    if not draft_path.is_file():
        criteria.append(_criterion("draft exists", "exists", "missing", False))
        return _result("G3", "CITATION_DENSITY", criteria, violations)

    text = draft_path.read_text(encoding="utf-8")
    words = _word_count(text)
    citations = len(set(re.findall(r"\[(\d+)\]", text)))
    density = citations / (words / 100) if words else 0.0

    density_min = float(targets.get("density_min", 0.8))
    words_range = targets.get("words_range", [2100, 2800])
    subsection_min = int(targets.get("subsection_min_citations", 3))

    criteria.append(_criterion("density_ratio", f">={density_min}",
                               round(density, 3), density >= density_min))
    criteria.append(_criterion("word_count", f"within {words_range}",
                               words, words_range[0] <= words <= words_range[1]))
    criteria.append(_criterion("citation_count", ">=1", citations, citations > 0))
    if density < density_min:
        violations.append(f"密度 {density:.3f} < {density_min}(需约 "
                          f"{int(density_min * words / 100) - citations} 个额外引用)")
    if not (words_range[0] <= words <= words_range[1]):
        violations.append(f"字数 {words} 超出 {words_range}")

    # 小节分布:每个 ## 小节 ≥ subsection_min 引用(无小节则整体视为一节)
    sections = re.split(r"(?m)^##+\s+", text)
    bodies = [s for s in sections[1:]] if len(sections) > 1 else [text]
    short = []
    for i, body in enumerate(bodies, start=1):
        n = len(set(re.findall(r"\[(\d+)\]", body)))
        if n < subsection_min:
            short.append((i, n))
    criteria.append(_criterion("subsection_distribution",
                               f">={subsection_min} citations each",
                               f"{len(bodies) - len(short)}/{len(bodies)}", not short))
    violations += [f"小节 {i} 只有 {n} 个引用(< {subsection_min})" for i, n in short]

    return _result("G3", "CITATION_DENSITY", criteria, violations)


# ---------------------------------------------------------------------------
# G4 Figure Integrity(文件存在 + 披露标签)
# ---------------------------------------------------------------------------

_FIG_REF = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")


def _gate_figure_integrity(args: dict, ctx: ToolContext) -> str:
    workdir = Path(args.get("workdir") or ctx.cwd)
    draft_path = workdir / args.get("draft_file", "DRAFT.md")
    criteria: list[dict] = []
    violations: list = []

    text = draft_path.read_text(encoding="utf-8") if draft_path.is_file() else ""
    refs = _FIG_REF.findall(text)
    if not refs:
        criteria.append(_criterion("figures", "all disclosed", "no figures", True))
        return _result("G4", "FIGURE_INTEGRITY", criteria, violations)

    missing, undisclosed = [], []
    lines = text.splitlines()
    for ref in refs:
        fig_path = workdir / ref
        if not fig_path.is_file():
            missing.append(ref)
            continue
        # caption = 引用所在行 + 下一行
        idx = next((i for i, l in enumerate(lines) if ref in l), None)
        caption = " ".join(lines[idx: idx + 2]).lower() if idx is not None else ""
        if not any(k in caption for k in DISCLOSURE_KEYWORDS):
            undisclosed.append(ref)

    criteria.append(_criterion("figure files exist", "100%",
                               f"{len(refs) - len(missing)}/{len(refs)}", not missing))
    criteria.append(_criterion("disclosure labels", "100% disclosed",
                               f"{len(refs) - len(undisclosed)}/{len(refs)}",
                               not undisclosed))
    violations += [f"图文件缺失:{r}" for r in missing]
    violations += [f"图缺披露标签(reconstructed/theoretical/simulated/...):{r}"
                   for r in undisclosed]
    return _result("G4", "FIGURE_INTEGRITY", criteria, violations)


def register_tools(registry: Registry) -> None:
    registry.register(
        "gate_citation_check",
        "G1:验证 EXTENDED_REFERENCES.md 的年份/venue/可访问性/存在性(arXiv+S2,cassette)。",
        {"type": "object", "properties": {"workdir": {"type": "string"}}},
        _gate_citation_check,
    )
    registry.register(
        "gate_data_veracity",
        "G2:草稿中每个强断言(数字/百分比/复杂度)必须有 [X] 引用标记。",
        {"type": "object", "properties": {"workdir": {"type": "string"}}},
        _gate_data_veracity,
    )
    registry.register(
        "gate_citation_density",
        "G3:引用密度 = citations/(words/100),阈值来自 workflow targets。",
        {"type": "object", "properties": {"workdir": {"type": "string"}}},
        _gate_citation_density,
    )
    registry.register(
        "gate_figure_integrity",
        "G4:引用的图文件必须存在且带披露标签(reconstructed/simulated/...)。",
        {"type": "object", "properties": {"workdir": {"type": "string"}}},
        _gate_figure_integrity,
    )
