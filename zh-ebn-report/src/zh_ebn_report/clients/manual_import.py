"""Manual import for databases without a usable search API.

Cochrane, CINAHL, 華藝 (Airiti), Taiwan Thesis System — all require the user to
search in their browser and export results as RIS or CSV. This module parses
those exports into :class:`Paper` objects so they flow through the same
deduplication / CASP pipeline as auto-fetched results.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import bibtexparser
from bibtexparser.bparser import BibTexParser

from ..models import OxfordLevel, Paper, SourceDB, StudyDesign


@dataclass(frozen=True)
class ManualRecord:
    title: str
    authors: list[str]
    year: int
    journal: str
    doi: str | None
    abstract: str | None
    source_db: SourceDB
    # Airiti-only hint so downstream ``record_to_paper`` can downgrade theses
    # / dissertations to observational-level defaults without re-parsing the
    # CSV. None means "unknown / not reported".
    doc_type: str | None = None


def _ris_to_records(text: str, default_source: SourceDB) -> list[ManualRecord]:
    """Minimal RIS parser. Accepts the fields we actually need."""

    records: list[ManualRecord] = []
    current: dict[str, Any] = {}
    authors: list[str] = []
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line:
            continue
        if len(line) < 6 or line[2:6] != "  - ":
            # continuation of previous field
            if "_last_tag" in current:
                current[current["_last_tag"]] += " " + line.strip()
            continue
        tag = line[:2]
        value = line[6:].strip()
        if tag == "TY":
            current = {"_type": value}
            authors = []
        elif tag in {"AU", "A1", "A2"}:
            authors.append(value)
            current["_authors"] = authors
        elif tag == "TI" or tag == "T1":
            current["_title"] = value
            current["_last_tag"] = "_title"
        elif tag == "PY" or tag == "Y1":
            if value[:4].isdigit():
                current["_year"] = int(value[:4])
        elif tag == "JO" or tag == "T2" or tag == "JF":
            current["_journal"] = value
        elif tag == "DO":
            current["_doi"] = value
        elif tag == "AB" or tag == "N2":
            current["_abstract"] = value
            current["_last_tag"] = "_abstract"
        elif tag == "ER":
            records.append(
                ManualRecord(
                    title=current.get("_title", ""),
                    authors=current.get("_authors") or [],
                    year=int(current.get("_year", 0) or 0),
                    journal=current.get("_journal", ""),
                    doi=current.get("_doi"),
                    abstract=current.get("_abstract"),
                    source_db=default_source,
                )
            )
            current = {}
            authors = []
    return records


def _bibtex_to_records(text: str, default_source: SourceDB) -> list[ManualRecord]:
    parser = BibTexParser(common_strings=True)
    db = bibtexparser.loads(text, parser=parser)
    records: list[ManualRecord] = []
    for entry in db.entries:
        authors_raw = entry.get("author", "")
        authors = [a.strip() for a in authors_raw.split(" and ") if a.strip()]
        year_raw = entry.get("year", "0")
        try:
            year = int(year_raw[:4])
        except ValueError:
            year = 0
        records.append(
            ManualRecord(
                title=entry.get("title", "").strip("{}"),
                authors=authors,
                year=year,
                journal=entry.get("journal", ""),
                doi=entry.get("doi") or None,
                abstract=entry.get("abstract") or None,
                source_db=default_source,
            )
        )
    return records


# ---------------------------------------------------------------------------
# 華藝 Airiti CSV export — column names may be in Chinese or English depending on
# the user's Airiti UI language. Both are accepted; alias map below.
# ---------------------------------------------------------------------------
_AIRITI_FIELD_ALIASES = {
    "title": ("標題", "篇名", "題名", "Title"),
    "authors": ("作者", "Author", "Authors"),
    "year": ("年份", "出版年", "Year"),
    "journal": ("期刊", "刊名", "Journal", "Source"),
    "doi": ("DOI", "doi"),
    "abstract": ("摘要", "Abstract"),
    "doc_type": ("類型", "文獻類型", "Type"),
}


def _airiti_pick(row: dict[str, str], key: str) -> str:
    for alias in _AIRITI_FIELD_ALIASES[key]:
        if alias in row and row[alias]:
            return row[alias].strip()
    return ""


def _airiti_csv_to_records(text: str) -> list[ManualRecord]:
    """Parse an Airiti書目 CSV export.

    Airiti exports Chinese-language BOM-prefixed CSV. We rely on
    ``csv.DictReader`` for quoting/escape handling and use the Chinese or
    English field names (``_AIRITI_FIELD_ALIASES``). Thesis / dissertation
    entries (類型 == "學位論文") are tagged so the caller can keep them at
    Oxford Level IV (observational-level default).
    """

    # Strip any UTF-8 BOM Airiti inserts.
    if text.startswith("﻿"):
        text = text.lstrip("﻿")
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise ValueError("Airiti CSV: header row missing")

    records: list[ManualRecord] = []
    for row in reader:
        title = _airiti_pick(row, "title")
        if not title:
            continue  # skip blank rows without a title
        authors_raw = _airiti_pick(row, "authors")
        # Airiti separator may be "；" (fullwidth), "、", or ","
        authors = [
            a.strip()
            for a in authors_raw.replace("；", ";").replace("、", ";").split(";")
            if a.strip()
        ]
        year_raw = _airiti_pick(row, "year")
        try:
            year = int(year_raw[:4]) if year_raw[:4].isdigit() else 0
        except ValueError:
            year = 0
        doc_type = _airiti_pick(row, "doc_type") or None
        journal = _airiti_pick(row, "journal")
        # Thesis entries often ship without a 期刊 field; A1 Paper validator
        # rejects empty journals, so supply a non-empty fallback sourced from
        # the doc_type so downstream processing does not crash.
        if not journal and is_airiti_thesis(doc_type or ""):
            journal = doc_type or "學位論文"
        records.append(
            ManualRecord(
                title=title,
                authors=authors,
                year=year,
                journal=journal,
                doi=_airiti_pick(row, "doi") or None,
                abstract=_airiti_pick(row, "abstract") or None,
                source_db=SourceDB.AIRITI,
                doc_type=doc_type,
            )
        )
    return records


def is_airiti_thesis(row_type: str) -> bool:
    """True when the Airiti row corresponds to a thesis / dissertation."""

    if not row_type:
        return False
    return any(k in row_type for k in ("學位論文", "碩士論文", "博士論文", "Thesis"))


def airiti_record_doc_types(text: str) -> list[str]:
    """Return the per-row 類型 strings in order — used by callers to decide
    per-record default ``StudyDesign`` / ``OxfordLevel`` before conversion."""

    if text.startswith("﻿"):
        text = text.lstrip("﻿")
    reader = csv.DictReader(io.StringIO(text))
    out: list[str] = []
    for row in reader:
        if not _airiti_pick(row, "title"):
            continue
        out.append(_airiti_pick(row, "doc_type"))
    return out


def load_manual_import(path: Path, *, source_db: SourceDB) -> list[ManualRecord]:
    """Load an RIS / BibTeX / Airiti CSV file. Format detected by suffix."""

    text = path.read_text(encoding="utf-8")
    suffix = path.suffix.lower()
    if suffix in {".ris", ".txt"}:
        return _ris_to_records(text, source_db)
    if suffix in {".bib", ".bibtex"}:
        return _bibtex_to_records(text, source_db)
    if suffix == ".csv":
        if source_db != SourceDB.AIRITI:
            raise ValueError(
                f"CSV import currently only supports Airiti; got {source_db.value}"
            )
        return _airiti_csv_to_records(text)
    raise ValueError(f"Unsupported manual import format: {suffix}")


def record_to_paper(
    record: ManualRecord,
    *,
    study_design: StudyDesign | None = None,
    oxford_level: OxfordLevel | None = None,
) -> Paper:
    """Convert a :class:`ManualRecord` into a :class:`Paper`.

    Defaults to ``StudyDesign.OTHER`` + ``OxfordLevel.IV``. Airiti theses /
    dissertations (``doc_type`` containing "學位論文"/"碩士論文"/"博士論文"/
    "Thesis") stay at Level IV even after the CASP appraiser runs — they are
    student works that do not undergo peer review, so the v0.2 evidence
    guardrail would cap them at IV regardless. We set it explicitly here for
    audit clarity.
    """

    if study_design is None:
        study_design = StudyDesign.OTHER
    if oxford_level is None:
        oxford_level = OxfordLevel.IV
    # Thesis override: never promote above Level IV regardless of what
    # downstream processing decides.
    if is_airiti_thesis(record.doc_type or ""):
        study_design = StudyDesign.OTHER
        oxford_level = OxfordLevel.IV

    return Paper(
        title=record.title,
        authors=record.authors,
        year=record.year,
        journal=record.journal,
        doi=record.doi or "",
        doi_validated=False,
        study_design=study_design,
        oxford_level=oxford_level,
        source_db=record.source_db,
        abstract=record.abstract,
    )
