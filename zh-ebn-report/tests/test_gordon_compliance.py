"""Tests for B: Gordon 11 Functional Health Patterns coverage in 護理評估."""

from __future__ import annotations

from zh_ebn_report.models import Section, SectionSelfCheck
from zh_ebn_report.pipeline.compliance import (
    _GORDON_11_KEYWORDS,
    _check_gordon_11_coverage,
)


def _section(body: str) -> Section:
    return Section(
        section_name="護理評估",  # type: ignore[arg-type]
        content_zh=body,
        word_count_estimate=len(body),
        citation_placeholders=[],
        self_check=SectionSelfCheck(
            uses_bi_jia_not_wo=True,
            uses_ge_an_not_bing_ren=True,
            formal_register_only=True,
            cites_phrasing_bank=False,
        ),
    )


def _body_with_patterns(pattern_indices: list[int]) -> str:
    """Synthesise a section body containing the first alias from each listed
    pattern index (1-based)."""
    parts = []
    for idx in pattern_indices:
        alias = _GORDON_11_KEYWORDS[idx - 1][0]
        parts.append(f"### 型態 {idx}：{alias} 相關評估內容")
    return "\n\n".join(parts)


class TestGordon11Coverage:
    def test_eleven_patterns_passes(self) -> None:
        body = _body_with_patterns(list(range(1, 12)))
        sections = {"護理評估": _section(body)}
        issues = _check_gordon_11_coverage(sections, kind="twna_case")
        assert issues == []

    def test_nine_patterns_passes_flexible_threshold(self) -> None:
        # Skip #9 (性-生殖) and #11 (價值-信念) — 9 of 11 covered
        body = _body_with_patterns([1, 2, 3, 4, 5, 6, 7, 8, 10])
        sections = {"護理評估": _section(body)}
        issues = _check_gordon_11_coverage(sections, kind="twna_case")
        assert issues == []

    def test_eight_patterns_flagged(self) -> None:
        body = _body_with_patterns([1, 2, 3, 4, 5, 6, 7, 8])
        sections = {"護理評估": _section(body)}
        issues = _check_gordon_11_coverage(sections, kind="twna_case")
        assert len(issues) == 1
        assert issues[0].rule == "gordon_11_incomplete"
        assert "9(性)" in issues[0].detail or "性" in issues[0].detail

    def test_twna_project_also_checked(self) -> None:
        body = _body_with_patterns([1, 2, 3])
        sections = {"護理評估": _section(body)}
        issues = _check_gordon_11_coverage(sections, kind="twna_project")
        assert any(i.rule == "gordon_11_incomplete" for i in issues)

    def test_reading_kind_skipped(self) -> None:
        body = _body_with_patterns([1, 2])  # only 2 of 11
        sections = {"護理評估": _section(body)}
        # Reading reports use a different framework; check should be a no-op.
        assert _check_gordon_11_coverage(sections, kind="reading") == []

    def test_case_kind_skipped(self) -> None:
        body = _body_with_patterns([1])
        sections = {"護理評估": _section(body)}
        assert _check_gordon_11_coverage(sections, kind="case") == []

    def test_missing_section_no_false_positive(self) -> None:
        # When the 護理評估 section isn't present at all, let the
        # missing_section rule handle it — don't emit gordon_11 error.
        assert _check_gordon_11_coverage({}, kind="twna_case") == []

    def test_any_alias_satisfies_pattern(self) -> None:
        # Use alternate aliases instead of primary ones
        aliases = [
            "健康處理",  # pattern 1 alt
            "飲食",  # pattern 2 alt
            "排便",  # pattern 3 alt
            "ADL",  # pattern 4 alt
            "休息",  # pattern 5 alt
            "疼痛",  # pattern 6 alt
            "自尊",  # pattern 7 alt
            "家庭",  # pattern 8 alt
            "生殖",  # pattern 9 alt
            "壓力",  # pattern 10 alt
            "信念",  # pattern 11 alt
        ]
        body = "；".join(f"本案{a}評估結果" for a in aliases)
        sections = {"護理評估": _section(body)}
        assert _check_gordon_11_coverage(sections, kind="twna_case") == []
