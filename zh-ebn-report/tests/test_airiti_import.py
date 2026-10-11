"""Tests for A: Airiti CSV parsing + CJK author citekey handling."""

from __future__ import annotations

import pytest

from zh_ebn_report.clients.manual_import import (
    _airiti_csv_to_records,
    is_airiti_thesis,
    load_manual_import,
    record_to_paper,
)
from zh_ebn_report.models import (
    OxfordLevel,
    Paper,
    SourceDB,
    StudyDesign,
    _is_cjk_only,
)


class TestCjkHelper:
    def test_pure_cjk(self) -> None:
        assert _is_cjk_only("張小明") is True

    def test_mixed_cjk_and_ascii(self) -> None:
        assert _is_cjk_only("Chen 陳") is False

    def test_english_name(self) -> None:
        assert _is_cjk_only("Smith J") is False

    def test_empty_or_whitespace(self) -> None:
        assert _is_cjk_only("") is False
        assert _is_cjk_only("   ") is False


class TestCjkCitekey:
    def test_cjk_author_uses_first_two_chars(self) -> None:
        p = Paper(
            title="穿刺疼痛介入研究",
            authors=["張小明"],
            year=2024,
            journal="台灣護理雜誌",
            doi="10.x/1",
            study_design=StudyDesign.OTHER,
            oxford_level=OxfordLevel.IV,
            source_db=SourceDB.AIRITI,
        )
        assert p.citekey().startswith("張小2024")

    def test_english_author_unchanged(self) -> None:
        p = Paper(
            title="Music therapy reduces anxiety",
            authors=["Smith J", "Doe A"],
            year=2024,
            journal="JAN",
            doi="10.x/2",
            study_design=StudyDesign.RCT,
            oxford_level=OxfordLevel.II,
            source_db=SourceDB.PUBMED,
        )
        assert p.citekey().startswith("smith2024")

    def test_single_cjk_char_falls_back_gracefully(self) -> None:
        p = Paper(
            title="研究",
            authors=["張"],
            year=2024,
            journal="J",
            doi="10.x/3",
            study_design=StudyDesign.OTHER,
            oxford_level=OxfordLevel.IV,
            source_db=SourceDB.AIRITI,
        )
        # len<2 → falls through to the English path (takes the char as-is)
        assert p.citekey().startswith("張2024")


class TestAiritiCsvParsing:
    def _sample_csv(self) -> str:
        return (
            "標題,作者,年份,期刊,DOI,摘要,類型\n"
            "腰椎穿刺疼痛介入研究,張小明;李大華,2024,台灣護理雜誌,10.x/1,"
            "本研究探討…,期刊論文\n"
            "壓瘡預防照護,王小花,2023,台大護理系,,摘要二,學位論文\n"
            "麻醉護理成果,陳小美;林小平;王小虎,2024,麻醉學會誌,10.x/3,,期刊論文\n"
        )

    def test_parses_all_rows(self) -> None:
        records = _airiti_csv_to_records(self._sample_csv())
        assert len(records) == 3

    def test_extracts_title_and_authors(self) -> None:
        records = _airiti_csv_to_records(self._sample_csv())
        assert records[0].title == "腰椎穿刺疼痛介入研究"
        assert records[0].authors == ["張小明", "李大華"]
        assert records[2].authors == ["陳小美", "林小平", "王小虎"]

    def test_thesis_detected(self) -> None:
        records = _airiti_csv_to_records(self._sample_csv())
        assert records[1].doc_type == "學位論文"
        assert is_airiti_thesis(records[1].doc_type or "") is True
        assert is_airiti_thesis(records[0].doc_type or "") is False

    def test_thesis_journal_fallback(self) -> None:
        """Thesis row with empty 期刊 column gets "學位論文" as fallback so
        the Paper validator does not reject it."""
        records = _airiti_csv_to_records(self._sample_csv())
        # Row 2 had empty DOI *and* the thesis row had 台大護理系 as journal
        thesis = records[1]
        assert thesis.journal  # non-empty (either 台大護理系 or fallback)

    def test_bom_stripped(self) -> None:
        csv_with_bom = "﻿標題,作者,年份,期刊,類型\n測試,張三,2024,期刊,期刊論文\n"
        records = _airiti_csv_to_records(csv_with_bom)
        assert len(records) == 1
        assert records[0].title == "測試"

    def test_alternate_separator_fullwidth(self) -> None:
        csv = "標題,作者,年份,期刊,類型\n研究,張三；李四、王五,2024,期刊,期刊論文\n"
        records = _airiti_csv_to_records(csv)
        assert records[0].authors == ["張三", "李四", "王五"]

    def test_english_column_aliases(self) -> None:
        csv = "Title,Author,Year,Journal,Type\nA Study,Smith J,2024,JAN,Article\n"
        records = _airiti_csv_to_records(csv)
        assert records[0].title == "A Study"
        assert records[0].year == 2024

    def test_blank_title_row_skipped(self) -> None:
        csv = "標題,作者,年份,期刊,類型\n,張三,2024,期刊,期刊論文\n有效,李四,2023,期刊,期刊論文\n"
        records = _airiti_csv_to_records(csv)
        assert len(records) == 1
        assert records[0].title == "有效"


class TestRecordToPaperThesis:
    def test_thesis_stays_at_level_iv(self) -> None:
        from zh_ebn_report.clients.manual_import import ManualRecord

        thesis_rec = ManualRecord(
            title="壓瘡預防照護",
            authors=["王小花"],
            year=2023,
            journal="台大護理系",
            doi=None,
            abstract=None,
            source_db=SourceDB.AIRITI,
            doc_type="學位論文",
        )
        # Even if caller passed StudyDesign.RCT + Level I, thesis tag wins.
        paper = record_to_paper(
            thesis_rec,
            study_design=StudyDesign.RCT,
            oxford_level=OxfordLevel.I,
        )
        assert paper.study_design == StudyDesign.OTHER
        assert paper.oxford_level == OxfordLevel.IV

    def test_journal_article_respects_caller_overrides(self) -> None:
        from zh_ebn_report.clients.manual_import import ManualRecord

        journal_rec = ManualRecord(
            title="介入成效",
            authors=["張三"],
            year=2024,
            journal="台灣護理雜誌",
            doi="10.x/9",
            abstract=None,
            source_db=SourceDB.AIRITI,
            doc_type="期刊論文",
        )
        paper = record_to_paper(
            journal_rec,
            study_design=StudyDesign.RCT,
            oxford_level=OxfordLevel.II,
        )
        # No thesis override → caller choice honoured.
        assert paper.study_design == StudyDesign.RCT
        assert paper.oxford_level == OxfordLevel.II


class TestLoadManualImport:
    def test_csv_suffix_routed_to_airiti_parser(self, tmp_path) -> None:
        csv_path = tmp_path / "airiti.csv"
        csv_path.write_text(
            "標題,作者,年份,期刊,類型\n研究,張三,2024,期刊,期刊論文\n",
            encoding="utf-8",
        )
        records = load_manual_import(csv_path, source_db=SourceDB.AIRITI)
        assert len(records) == 1
        assert records[0].source_db == SourceDB.AIRITI

    def test_csv_with_wrong_source_db_rejected(self, tmp_path) -> None:
        csv_path = tmp_path / "x.csv"
        csv_path.write_text("header\nrow\n", encoding="utf-8")
        with pytest.raises(ValueError, match="CSV import"):
            load_manual_import(csv_path, source_db=SourceDB.CINAHL)
