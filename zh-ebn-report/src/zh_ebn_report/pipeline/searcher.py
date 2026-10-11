"""Execute the multi-database search plan: call auto-APIs, ingest manual RIS,
deduplicate, and validate DOIs.

The ``run_searches`` function is called by the orchestrator after the Search
Strategist produces a :class:`SearchStrategy`.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from ..clients.crossref import CrossrefClient, DoiCheck
from ..clients.embase import EmbaseClient
from ..clients.llm import LLMClient
from ..clients.manual_import import load_manual_import, record_to_paper
from ..clients.pubmed import PubMedClient
from ..clients.scopus import ScopusClient
from ..config import AppConfig
from ..models import (
    OxfordLevel,
    Paper,
    SearchHistoryRow,
    SearchResult,
    SearchStrategy,
    SourceDB,
    StudyDesign,
)
from ..utils.dedup import dedup
from .keyword_tuner import needs_tuning, pick_better, tune_pubmed_query

log = logging.getLogger(__name__)


async def run_searches(
    *,
    app_cfg: AppConfig,
    strategy: SearchStrategy,
    manual_imports: dict[SourceDB, Path] | None = None,
    max_per_db: int = 100,
    llm: LLMClient | None = None,
) -> SearchResult:
    """Execute search strategy across available APIs, then ingest manual imports.

    Automatic databases (PubMed / Scopus / Embase) are queried in parallel.
    Manual databases (Cochrane / CINAHL / Airiti) are loaded from user-provided
    RIS/BibTeX files.

    When ``llm`` is provided AND ``app_cfg.pipeline.enable_keyword_tuner`` is
    set, the PubMed arm will fire a single tuner retry if the initial hit
    count falls outside the 100–1000 sweet spot.
    """

    history: list[SearchHistoryRow] = []
    hits: list[Paper] = []

    await asyncio.gather(
        _search_pubmed(app_cfg, strategy, hits, history, max_per_db, llm=llm),
        _search_scopus(app_cfg, strategy, hits, history, max_per_db),
        _search_embase(app_cfg, strategy, hits, history, max_per_db),
    )

    # Manual imports
    if manual_imports:
        for src_db, path in manual_imports.items():
            try:
                records = load_manual_import(path, source_db=src_db)
            except Exception as exc:  # noqa: BLE001 — any parse error we degrade gracefully
                log.warning("manual import failed for %s: %s", path, exc)
                history.append(
                    SearchHistoryRow(
                        keywords="(manual import)",
                        database=src_db,
                        field_limit="N/A",
                        initial_hits=0,
                        deduplicated_hits=0,
                        inclusion_criteria="(未納入，匯入失敗)",
                        exclusion_criteria=str(exc),
                        included_count=0,
                        note=f"manual import 讀取失敗：{exc}",
                    )
                )
                continue
            added = [record_to_paper(r) for r in records]
            hits.extend(added)
            history.append(
                SearchHistoryRow(
                    keywords="(manual import)",
                    database=src_db,
                    field_limit="RIS/BibTeX",
                    initial_hits=len(added),
                    deduplicated_hits=len(added),
                    inclusion_criteria="人工匯入",
                    exclusion_criteria="—",
                    included_count=len(added),
                    note=f"來源：{path.name}",
                )
            )

    # Deduplicate across all sources
    dedup_result = dedup(hits)
    for group in dedup_result.groups:
        if group.duplicates:
            log.info(
                "dedup: %s merged %d duplicates",
                group.canonical.doi or group.canonical.title[:30],
                len(group.duplicates),
            )

    # DOI validation for the deduplicated set
    validated = await _validate_dois(app_cfg, dedup_result.unique)

    return SearchResult(
        strategy=strategy,
        history=history,
        papers=validated,
    )


async def _search_pubmed(
    app_cfg: AppConfig,
    strategy: SearchStrategy,
    hits: list[Paper],
    history: list[SearchHistoryRow],
    max_per_db: int,
    *,
    llm: LLMClient | None = None,
) -> None:
    query = strategy.six_piece_strategy.boolean_query_pubmed
    field_limit = strategy.six_piece_strategy.field_codes_used.get("pubmed", "(none)")
    if not query:
        return
    async with PubMedClient(app_cfg.dbs.pubmed) as client:
        try:
            total = await client.count(query)
        except Exception as exc:  # noqa: BLE001
            log.warning("PubMed count failed: %s", exc)
            history.append(
                SearchHistoryRow(
                    keywords=query,
                    database=SourceDB.PUBMED,
                    field_limit=field_limit,
                    initial_hits=0,
                    deduplicated_hits=0,
                    inclusion_criteria="(未執行)",
                    exclusion_criteria=str(exc),
                    included_count=0,
                    note="PubMed API 錯誤",
                )
            )
            return

        # v0.8 tuner: if the initial hit count is out of the 100–1000 sweet
        # spot AND the caller supplied an LLM AND the feature is enabled,
        # ask the tuner for a new Boolean string and run ONE more count.
        tuned_note: str | None = None
        if (
            llm is not None
            and app_cfg.pipeline.enable_keyword_tuner
            and needs_tuning(total)
        ):
            try:
                tuned = await tune_pubmed_query(
                    llm=llm,
                    cfg=app_cfg.pipeline,
                    original_query=query,
                    hit_count=total,
                    if_too_narrow=strategy.tuning_plan.if_too_narrow,
                    if_too_wide=strategy.tuning_plan.if_too_wide,
                )
                new_total = await client.count(tuned.new_query)
                history.append(
                    SearchHistoryRow(
                        keywords=query,
                        database=SourceDB.PUBMED,
                        field_limit=field_limit,
                        initial_hits=total,
                        deduplicated_hits=0,
                        inclusion_criteria="(初輪，未取件)",
                        exclusion_criteria="(初輪，未取件)",
                        included_count=0,
                        note=(
                            f"初輪 {total} 篇超出甜蜜區；交予 keyword_tuner 調整"
                        ),
                    )
                )
                query, total, picked_tag = pick_better(
                    orig_query=query,
                    orig_hits=total,
                    new_query=tuned.new_query,
                    new_hits=new_total,
                )
                tuned_note = (
                    f"{picked_tag}；tuner 建議 {new_total} 篇；"
                    f"採用：{'新字串' if query == tuned.new_query else '原字串'}；"
                    f"rationale：{tuned.rationale_zh}"
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("keyword_tuner failed: %s — keeping original query", exc)
                tuned_note = f"tuner 錯誤：{exc}"

        try:
            pmids = await client.search_pmids(query, retmax=min(max_per_db, total))
            metadata = await client.fetch_metadata(pmids)
        except Exception as exc:  # noqa: BLE001
            log.warning("PubMed fetch failed: %s", exc)
            history.append(
                SearchHistoryRow(
                    keywords=query,
                    database=SourceDB.PUBMED,
                    field_limit=field_limit,
                    initial_hits=total,
                    deduplicated_hits=0,
                    inclusion_criteria="(未執行)",
                    exclusion_criteria=str(exc),
                    included_count=0,
                    note=tuned_note or "PubMed API 錯誤（fetch 階段）",
                )
            )
            return

    initial = len(metadata)
    added: list[Paper] = []
    for h in metadata:
        added.append(
            Paper(
                title=h.title,
                authors=h.authors,
                year=h.year,
                journal=h.journal,
                doi=h.doi or "",
                doi_validated=False,
                study_design=StudyDesign.OTHER,
                oxford_level=OxfordLevel.IV,
                source_db=SourceDB.PUBMED,
                abstract=h.abstract,
            )
        )
    hits.extend(added)
    base_note = f"PubMed 初命中 {total}，抓取上限 {max_per_db}，實際取 {initial}"
    history.append(
        SearchHistoryRow(
            keywords=query,
            database=SourceDB.PUBMED,
            field_limit=field_limit,
            initial_hits=total,
            deduplicated_hits=initial,
            inclusion_criteria="符合 Boolean、年份、語言",
            exclusion_criteria="超出甜蜜區/不符 PICO（後續篩選）",
            included_count=initial,
            note=f"{base_note}；{tuned_note}" if tuned_note else base_note,
        )
    )


async def _search_scopus(
    app_cfg: AppConfig,
    strategy: SearchStrategy,
    hits: list[Paper],
    history: list[SearchHistoryRow],
    max_per_db: int,
) -> None:
    if not app_cfg.dbs.scopus:
        return
    query = strategy.six_piece_strategy.boolean_query_pubmed  # reuse if no scopus-specific query
    if not query:
        return
    try:
        async with ScopusClient(
            app_cfg.dbs.scopus, inst_token=app_cfg.dbs.scopus_inst_token
        ) as client:
            total = await client.count(query)
            entries = await client.search(query, max_results=min(max_per_db, total))
    except Exception as exc:  # noqa: BLE001
        log.warning("Scopus search failed: %s", exc)
        history.append(
            SearchHistoryRow(
                keywords=query,
                database=SourceDB.SCOPUS,
                field_limit="TITLE-ABS-KEY",
                initial_hits=0,
                deduplicated_hits=0,
                inclusion_criteria="(未執行)",
                exclusion_criteria=str(exc),
                included_count=0,
                note="Scopus API 錯誤",
            )
        )
        return

    added = [
        Paper(
            title=e.title,
            authors=e.authors,
            year=e.year or 0,
            journal=e.journal,
            doi=e.doi or "",
            doi_validated=False,
            study_design=StudyDesign.OTHER,
            oxford_level=OxfordLevel.IV,
            source_db=SourceDB.SCOPUS,
            abstract=e.abstract,
        )
        for e in entries
    ]
    hits.extend(added)
    history.append(
        SearchHistoryRow(
            keywords=query,
            database=SourceDB.SCOPUS,
            field_limit="TITLE-ABS-KEY",
            initial_hits=total,
            deduplicated_hits=len(added),
            inclusion_criteria="符合 Boolean",
            exclusion_criteria="—",
            included_count=len(added),
            note=f"Scopus 初命中 {total}，取 {len(added)}",
        )
    )


async def _search_embase(
    app_cfg: AppConfig,
    strategy: SearchStrategy,
    hits: list[Paper],
    history: list[SearchHistoryRow],
    max_per_db: int,
) -> None:
    if not app_cfg.dbs.embase:
        return
    query = strategy.six_piece_strategy.boolean_query_pubmed
    if not query:
        return
    try:
        async with EmbaseClient(
            app_cfg.dbs.embase,
            inst_token=app_cfg.dbs.embase_inst_token,
            auth_token=app_cfg.dbs.embase_auth_token,
        ) as client:
            entries, err = await client.search(query, max_results=min(max_per_db, 25))
    except Exception as exc:  # noqa: BLE001
        log.warning("Embase search failed: %s", exc)
        history.append(
            SearchHistoryRow(
                keywords=query,
                database=SourceDB.EMBASE,
                field_limit="(default)",
                initial_hits=0,
                deduplicated_hits=0,
                inclusion_criteria="(未執行)",
                exclusion_criteria=str(exc),
                included_count=0,
                note="Embase API 錯誤",
            )
        )
        return

    if err:
        history.append(
            SearchHistoryRow(
                keywords=query,
                database=SourceDB.EMBASE,
                field_limit="(default)",
                initial_hits=0,
                deduplicated_hits=0,
                inclusion_criteria="(未執行)",
                exclusion_criteria=err,
                included_count=0,
                note="Embase 未授權；請改用手動 RIS 匯入",
            )
        )
        return

    added = [
        Paper(
            title=e.title,
            authors=e.authors,
            year=e.year or 0,
            journal=e.journal,
            doi=e.doi or "",
            doi_validated=False,
            study_design=StudyDesign.OTHER,
            oxford_level=OxfordLevel.IV,
            source_db=SourceDB.EMBASE,
            abstract=None,
        )
        for e in entries
    ]
    hits.extend(added)
    history.append(
        SearchHistoryRow(
            keywords=query,
            database=SourceDB.EMBASE,
            field_limit="(default)",
            initial_hits=len(added),
            deduplicated_hits=len(added),
            inclusion_criteria="符合 Boolean",
            exclusion_criteria="—",
            included_count=len(added),
            note=f"Embase 取 {len(added)}",
        )
    )


async def _validate_dois(
    app_cfg: AppConfig, papers: list[Paper]
) -> list[Paper]:
    async with CrossrefClient(app_cfg.dbs.crossref_mailto) as client:
        checks: list[Any] = await asyncio.gather(
            *(
                client.validate(
                    p.doi,
                    expected_title=p.title,
                    expected_year=p.year if p.year else None,
                    expected_first_author_surname=_first_surname(p),
                )
                if p.doi
                else _dummy_doi_check(p.doi)
                for p in papers
            )
        )
    updated: list[Paper] = []
    for p, chk in zip(papers, checks):
        chk_obj: DoiCheck = chk  # type: ignore[assignment]
        updated.append(
            p.model_copy(
                update={
                    "doi_validated": chk_obj.resolvable,
                    "doi_metadata_matches": chk_obj.matches_paper,
                }
            )
        )
    return updated


def _first_surname(paper: Paper) -> str | None:
    if not paper.authors:
        return None
    first = paper.authors[0]
    if "," in first:
        return first.split(",", 1)[0].strip()
    parts = first.strip().split()
    return parts[-1] if parts else None


async def _dummy_doi_check(doi: str) -> DoiCheck:
    return DoiCheck(
        doi=doi,
        resolvable=False,
        metadata=None,
        matches_paper=None,
        mismatch_details="no DOI",
        http_status=None,
    )
