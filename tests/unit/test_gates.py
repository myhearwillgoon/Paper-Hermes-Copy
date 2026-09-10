"""gate checker 单元测试(PLAN §6 M3:test_gates.py)。

G1 外部验证走 cassette replay:tests/fixtures/cassettes/
(arXiv 为一次性 live 录制;Semantic Scholar 因当时 429 限流为手工构造,格式同 M0 Cassette)。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mini_hermes.tools.registry import Registry, ToolContext, discover_builtin_tools

CASSETTES = str(Path(__file__).resolve().parents[1] / "fixtures" / "cassettes")

GOOD_REFS = """# Extended References

## References

[1] Vaswani, A., et al. (2017). Attention Is All You Need. *NeurIPS*. arXiv:1706.03762

[2] Devlin, J., et al. (2019). BERT: Pre-training of Deep Bidirectional Transformers. *NAACL*. arXiv:1810.04805
"""

TITLE_ONLY_REF = """# Extended References

## References

[1] Brown, T., et al. (2020). Language Models are Few-Shot Learners. *NeurIPS*, 33. https://proceedings.neurips.cc/paper/2020/hash/1457c0d6bfcb4967418bfb8ac142f64a-Abstract.html
"""


@pytest.fixture()
def registry():
    return discover_builtin_tools(Registry())


def _g1(registry, tmp_path, refs_text, *, year_threshold=2017):
    (tmp_path / "EXTENDED_REFERENCES.md").write_text(refs_text, encoding="utf-8")
    args = {
        "workdir": str(tmp_path),
        "targets": {"year_threshold": year_threshold},
        "external_mode": "cassette",
        "cassette_dir": CASSETTES,
    }
    return json.loads(registry.execute("gate_citation_check", args,
                                       ToolContext(cwd=tmp_path)))


# ------------------------------------------------------------------------ G1


def test_g1_pass_with_cassette(registry, tmp_path):
    result = _g1(registry, tmp_path, GOOD_REFS)
    assert result["status"] == "PASSED"
    assert result["gate_id"] == "G1"
    names = {c["name"]: c["passed"] for c in result["criteria"]}
    assert names["Year Gate"] and names["Venue Gate"]
    assert names["Access Gate"] and names["Existence Gate"]
    assert result["violations"] == []


def test_g1_year_gate_fail(registry, tmp_path):
    result = _g1(registry, tmp_path, GOOD_REFS, year_threshold=2024)
    assert result["status"] == "FAILED"
    year = next(c for c in result["criteria"] if c["name"] == "Year Gate")
    assert year["passed"] is False
    assert year["actual"] == "0/2"


def test_g1_venue_gate_fail(registry, tmp_path):
    refs = "[1] X, Y. (2024). Some Paper. *Obscure Workshop Letters*. arXiv:1706.03762\n"
    result = _g1(registry, tmp_path, refs)
    venue = next(c for c in result["criteria"] if c["name"] == "Venue Gate")
    assert venue["passed"] is False


def test_g1_existence_fail_nonexistent_arxiv(registry, tmp_path):
    """不存在的 arXiv id(cassette 里是空 feed)→ Existence Gate FAIL。"""
    refs = "[1] X, Y. (2024). Ghost Paper. *arXiv preprint*. arXiv:9999.99999\n"
    result = _g1(registry, tmp_path, refs)
    existence = next(c for c in result["criteria"] if c["name"] == "Existence Gate")
    assert existence["passed"] is False
    assert any("9999.99999" in v or "存在性" in v for v in result["violations"])


def test_g1_cassette_miss_is_violation(registry, tmp_path):
    """replay miss(未录制的 id)→ 违规,不是崩溃。"""
    refs = "[1] X, Y. (2024). Unrecorded. *arXiv preprint*. arXiv:1111.11111\n"
    result = _g1(registry, tmp_path, refs)
    assert result["status"] == "FAILED"
    assert any("cassette miss" in v for v in result["violations"])


def test_g1_title_only_via_semantic_scholar(registry, tmp_path):
    """无 arXiv ID → Semantic Scholar 标题搜索(手工构造 cassette)。"""
    result = _g1(registry, tmp_path, TITLE_ONLY_REF, year_threshold=2020)
    assert result["status"] == "PASSED", result["violations"]


def test_g1_missing_file(registry, tmp_path):
    result = _g1(registry, tmp_path, "")  # 文件没写
    # _g1 写了空文件,所以这里直接删了再测
    (tmp_path / "EXTENDED_REFERENCES.md").unlink()
    result = _g1_no_file(registry, tmp_path)
    assert result["status"] == "FAILED"


def _g1_no_file(registry, tmp_path):
    return json.loads(registry.execute(
        "gate_citation_check",
        {"workdir": str(tmp_path), "targets": {}, "external_mode": "cassette",
         "cassette_dir": CASSETTES},
        ToolContext(cwd=tmp_path)))


# ------------------------------------------------------------------------ G2


def _g2(registry, tmp_path, draft: str):
    (tmp_path / "DRAFT.md").write_text(draft, encoding="utf-8")
    return json.loads(registry.execute(
        "gate_data_veracity", {"workdir": str(tmp_path), "draft_file": "DRAFT.md"},
        ToolContext(cwd=tmp_path)))


def test_g2_assertion_with_citation_passes(registry, tmp_path):
    result = _g2(registry, tmp_path, "Pruning gives 40% overhead reduction [3].\n")
    assert result["status"] == "PASSED"


def test_g2_assertion_without_citation_fails(registry, tmp_path):
    result = _g2(registry, tmp_path, "Pruning gives 40% overhead reduction.\n")
    assert result["status"] == "FAILED"
    assert any("40%" in v for v in result["violations"])


def test_g2_complexity_pattern(registry, tmp_path):
    assert _g2(registry, tmp_path, "Cost is O(n log n) as shown [2].\n")["status"] == "PASSED"
    assert _g2(registry, tmp_path, "Cost is O(n log n).\n")["status"] == "FAILED"


# ------------------------------------------------------------------------ G3


def _g3(registry, tmp_path, draft: str, targets: dict):
    (tmp_path / "DRAFT.md").write_text(draft, encoding="utf-8")
    return json.loads(registry.execute(
        "gate_citation_density",
        {"workdir": str(tmp_path), "draft_file": "DRAFT.md", "targets": targets},
        ToolContext(cwd=tmp_path)))


def test_g3_density_math(registry, tmp_path):
    # 恰好 100 个 token(97 词 + 2 引用标记 + "## Intro" 2 token)→ density = 2.0
    body = " ".join(["word"] * 49 + ["[1]"] + ["word"] * 47 + ["[2]"])
    draft = f"## Intro\n{body}\n"
    result = _g3(registry, tmp_path, draft,
                 {"density_min": 0.8, "words_range": [50, 300],
                  "subsection_min_citations": 2})
    assert result["status"] == "PASSED", result
    density = next(c for c in result["criteria"] if c["name"] == "density_ratio")
    assert density["actual"] == 2.0


def test_g3_low_density_fails(registry, tmp_path):
    body = " ".join(["word"] * 99 + ["[1]"])  # density = 1.0... 用更低:0.5
    body = " ".join(["word"] * 199 + ["[1]"])  # 200 词 1 引用 = 0.5
    result = _g3(registry, tmp_path, body,
                 {"density_min": 0.8, "words_range": [50, 300],
                  "subsection_min_citations": 1})
    assert result["status"] == "FAILED"
    assert any("密度" in v for v in result["violations"])


def test_g3_subsection_shortfall(registry, tmp_path):
    draft = "## A\n" + " ".join(["word"] * 50) + " [1] [2] [3]\n## B\n" + " ".join(["word"] * 50) + "\n"
    result = _g3(registry, tmp_path, draft,
                 {"density_min": 0.5, "words_range": [10, 500],
                  "subsection_min_citations": 3})
    sub = next(c for c in result["criteria"] if c["name"] == "subsection_distribution")
    assert sub["passed"] is False
    assert any("小节 2" in v for v in result["violations"])


# ------------------------------------------------------------------------ G4


def _g4(registry, tmp_path, figures_md: str | None):
    if figures_md is not None:
        (tmp_path / "FIGURES.md").write_text(figures_md, encoding="utf-8")
    return json.loads(registry.execute(
        "gate_figure_integrity",
        {"workdir": str(tmp_path), "draft_file": "FIGURES.md"},
        ToolContext(cwd=tmp_path)))


def test_g4_disclosed_figure_passes(registry, tmp_path):
    (tmp_path / "figures").mkdir()
    (tmp_path / "figures" / "f1.svg").write_text("<svg/>")
    md = ("![Figure 1](figures/f1.svg)\n"
          "Data points reconstructed from [4]; exact values are illustrative.\n")
    assert _g4(registry, tmp_path, md)["status"] == "PASSED"


def test_g4_missing_file_fails(registry, tmp_path):
    md = "![Figure 1](figures/ghost.svg)\nreconstructed from [1].\n"
    result = _g4(registry, tmp_path, md)
    assert result["status"] == "FAILED"
    assert any("ghost.svg" in v for v in result["violations"])


def test_g4_undisclosed_fails(registry, tmp_path):
    (tmp_path / "figures").mkdir()
    (tmp_path / "figures" / "f1.svg").write_text("<svg/>")
    md = "![Figure 1](figures/f1.svg)\nResults look like this.\n"
    result = _g4(registry, tmp_path, md)
    assert result["status"] == "FAILED"
    assert any("披露" in v for v in result["violations"])


def test_g4_no_figures_vacuous_pass(registry, tmp_path):
    assert _g4(registry, tmp_path, "no figures here")["status"] == "PASSED"
