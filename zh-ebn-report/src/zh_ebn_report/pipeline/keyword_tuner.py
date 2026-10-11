"""Keyword tuner — pull PubMed hit count back into the 100–1000 sweet spot.

When the first PubMed eSearch for a generated strategy lands outside the
sweet spot (``references/pico-and-search.md:216``), this module asks a small
LLM to rewrite ONLY the boolean query string given the current hit count +
the strategy's ``tuning_plan``. The searcher then runs one extra eSearch
with the new string; whichever of the two results is closer to 100–1000 is
kept for downstream CASP processing.

Capped at **one** retry per run to bound cost. If the second attempt is
still outside the sweet spot we log a warning and keep the closer result.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..clients.llm import LLMClient
from ..config import PipelineConfig
from .prompts import build_system

log = logging.getLogger(__name__)

SWEET_SPOT_MIN = 100
SWEET_SPOT_MAX = 1000
# Absolute bounds for invocation; inside [SWEET_SPOT_MIN, SWEET_SPOT_MAX] is
# fine, outside these we call the tuner.
NARROW_THRESHOLD = 50   # hits < 50 → widen
WIDE_THRESHOLD = 5000   # hits > 5000 → narrow
# hits between 50–100 or 1000–5000 are "acceptable" — don't re-tune (cost)


@dataclass(frozen=True)
class TuneResult:
    new_query: str
    rationale_zh: str


def needs_tuning(hit_count: int) -> bool:
    """Return True when the hit count is *out of the acceptable band*.

    100–1000 is ideal, 50–5000 is acceptable. Only the extremes trigger a
    rerun — the middle zone is "good enough" and re-tuning costs more than
    it saves.
    """

    return hit_count < NARROW_THRESHOLD or hit_count > WIDE_THRESHOLD


async def tune_pubmed_query(
    *,
    llm: LLMClient,
    cfg: PipelineConfig,
    original_query: str,
    hit_count: int,
    if_too_narrow: list[str],
    if_too_wide: list[str],
) -> TuneResult:
    """Ask the Keyword Tuner LLM to rewrite ``original_query`` given the
    actual hit count and the strategy's ``tuning_plan``.
    """

    system = build_system(
        cfg,
        skill_refs=["pico-and-search.md"],
        role_prompt_file="keyword_tuner.md",
    )
    user = (
        f"原始 Boolean 字串：\n{original_query}\n\n"
        f"本輪命中篇數：{hit_count}\n\n"
        f"tuning_plan.if_too_narrow：{if_too_narrow}\n"
        f"tuning_plan.if_too_wide：{if_too_wide}\n\n"
        "請輸出 JSON（對應 TuneResult：new_query + rationale_zh）。"
    )
    data = await llm.complete_json(
        tier="haiku",
        system_blocks=system,
        user_message=user,
        max_tokens=1024,
    )
    return TuneResult(
        new_query=str(data.get("new_query", original_query)),
        rationale_zh=str(data.get("rationale_zh", "(no rationale)")),
    )


def pick_better(
    *,
    orig_query: str,
    orig_hits: int,
    new_query: str,
    new_hits: int,
) -> tuple[str, int, str]:
    """Return whichever of the two attempts is closer to the sweet spot.

    Tuple: (chosen_query, chosen_hits, reason_tag).
    """

    orig_dist = _distance_from_sweet_spot(orig_hits)
    new_dist = _distance_from_sweet_spot(new_hits)
    if new_dist < orig_dist:
        return new_query, new_hits, "tuner_improved"
    return orig_query, orig_hits, "tuner_no_improvement"


def _distance_from_sweet_spot(hits: int) -> int:
    if SWEET_SPOT_MIN <= hits <= SWEET_SPOT_MAX:
        return 0
    if hits < SWEET_SPOT_MIN:
        return SWEET_SPOT_MIN - hits
    return hits - SWEET_SPOT_MAX
