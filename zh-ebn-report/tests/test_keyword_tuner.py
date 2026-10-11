"""Tests for C: PubMed keyword tuner + sweet-spot iteration."""

from __future__ import annotations

import pytest

from zh_ebn_report.pipeline.keyword_tuner import (
    NARROW_THRESHOLD,
    SWEET_SPOT_MAX,
    SWEET_SPOT_MIN,
    TuneResult,
    WIDE_THRESHOLD,
    _distance_from_sweet_spot,
    needs_tuning,
    pick_better,
    tune_pubmed_query,
)


class TestNeedsTuning:
    def test_well_under_min_triggers(self) -> None:
        assert needs_tuning(20) is True

    def test_well_over_max_triggers(self) -> None:
        assert needs_tuning(8000) is True

    def test_sweet_spot_skips(self) -> None:
        assert needs_tuning(100) is False
        assert needs_tuning(500) is False
        assert needs_tuning(1000) is False

    def test_acceptable_band_skips(self) -> None:
        # 50–100 and 1000–5000 are acceptable, no re-tune
        assert needs_tuning(75) is False
        assert needs_tuning(3000) is False

    def test_threshold_edges(self) -> None:
        assert needs_tuning(NARROW_THRESHOLD - 1) is True
        assert needs_tuning(NARROW_THRESHOLD) is False
        assert needs_tuning(WIDE_THRESHOLD) is False
        assert needs_tuning(WIDE_THRESHOLD + 1) is True


class TestDistanceFromSweetSpot:
    def test_inside_sweet_spot_is_zero(self) -> None:
        assert _distance_from_sweet_spot(SWEET_SPOT_MIN) == 0
        assert _distance_from_sweet_spot(500) == 0
        assert _distance_from_sweet_spot(SWEET_SPOT_MAX) == 0

    def test_below_min(self) -> None:
        assert _distance_from_sweet_spot(30) == 70

    def test_above_max(self) -> None:
        assert _distance_from_sweet_spot(1200) == 200


class TestPickBetter:
    def test_tuner_improves_picks_new(self) -> None:
        q, hits, tag = pick_better(
            orig_query="A", orig_hits=30, new_query="B", new_hits=500
        )
        assert q == "B"
        assert hits == 500
        assert tag == "tuner_improved"

    def test_tuner_worsens_keeps_original(self) -> None:
        q, hits, tag = pick_better(
            orig_query="A", orig_hits=500, new_query="B", new_hits=20
        )
        assert q == "A"
        assert hits == 500
        assert tag == "tuner_no_improvement"

    def test_both_out_of_sweet_spot_picks_closer(self) -> None:
        # original at 30 (dist 70), new at 80 (dist 20) → new wins
        q, _, tag = pick_better(
            orig_query="A", orig_hits=30, new_query="B", new_hits=80
        )
        assert q == "B"
        assert tag == "tuner_improved"


class _FakeLLM:
    """Minimal LLMClient stub returning a canned TuneResult JSON."""

    def __init__(self, new_query: str, rationale: str = "test rationale") -> None:
        self.new_query = new_query
        self.rationale = rationale
        self.calls: list[dict] = []

    def model_for(self, tier: str) -> str:
        return f"claude-{tier}-stub"

    async def complete_json(
        self, *, tier, system_blocks, user_message, max_tokens=4096, temperature=0.2
    ):
        self.calls.append({"tier": tier, "user_message": user_message})
        return {"new_query": self.new_query, "rationale_zh": self.rationale}

    async def complete(
        self,
        *,
        tier,
        system_blocks,
        user_message,
        max_tokens=4096,
        temperature=0.2,
        json_mode=False,
    ):
        return ""


@pytest.mark.asyncio
async def test_tune_pubmed_query_shapes_result(tmp_path) -> None:
    from zh_ebn_report.config import PipelineConfig

    # Skill root at tmp_path so build_system doesn't fail on missing refs.
    refs = tmp_path / "zh-ebn-report" / "references"
    refs.mkdir(parents=True)
    (refs / "pico-and-search.md").write_text("(stub)", encoding="utf-8")
    prompts = tmp_path / "zh-ebn-report" / "src" / "zh_ebn_report" / "prompts"
    prompts.mkdir(parents=True)
    (prompts / "keyword_tuner.md").write_text("(role)", encoding="utf-8")

    cfg = PipelineConfig(
        max_parallel_casp=1,
        max_parallel_sections=1,
        default_year_range=5,
        output_root=tmp_path / "output",
        skill_root=tmp_path / "zh-ebn-report",
        enable_keyword_tuner=True,
    )
    llm = _FakeLLM(new_query="(improved query)", rationale="widened with MeSH")
    result = await tune_pubmed_query(
        llm=llm,
        cfg=cfg,
        original_query="(original query)",
        hit_count=30,
        if_too_narrow=["add synonyms"],
        if_too_wide=[],
    )
    assert isinstance(result, TuneResult)
    assert result.new_query == "(improved query)"
    assert result.rationale_zh == "widened with MeSH"
    assert len(llm.calls) == 1
    # The LLM received the count in its user message
    assert "30" in llm.calls[0]["user_message"]
