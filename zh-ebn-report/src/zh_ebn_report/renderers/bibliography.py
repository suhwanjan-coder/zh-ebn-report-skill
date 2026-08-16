"""Convert :class:`Paper` objects to a BibTeX bibliography.

Chose BibTeX over CSL-JSON because Quarto's pandoc pipeline accepts .bib natively
and APA 7 CSL resolves against BibTeX entry types cleanly.
"""

from __future__ import annotations

from ..models import Paper, StudyDesign


def _bibtex_type(paper: Paper) -> str:
    if paper.study_design in {StudyDesign.SR, StudyDesign.MA}:
        return "article"  # SRs are journal articles
    return "article"


def _escape(value: str) -> str:
    return value.replace("{", "\\{").replace("}", "\\}")


def _bibtex_author(name: str) -> str:
    """Normalise a PubMed-style "Surname Initials" author to BibTeX "Surname, Initials".

    Pandoc's BibTeX reader treats the *last* whitespace token as the family name,
    so "Xie S" is misparsed as family="S". Inserting a comma ("Xie, S") makes the
    leading token the family name, so APA 7 renders the surname correctly.
    """
    name = name.strip()
    if not name or "," in name:
        return name
    head, _, rest = name.partition(" ")
    return f"{head}, {rest}" if rest else head


def paper_to_entry(paper: Paper) -> str:
    key = paper.citekey()
    authors = " and ".join(_bibtex_author(a) for a in paper.authors) if paper.authors else "Anon"
    fields = [
        ("title", _escape(paper.title)),
        ("author", _escape(authors)),
        ("journal", _escape(paper.journal)),
        ("year", str(paper.year)),
    ]
    if paper.doi:
        fields.append(("doi", _escape(paper.doi)))
    if paper.abstract:
        fields.append(("abstract", _escape(paper.abstract[:800])))
    body = ",\n  ".join(f"{k} = {{{v}}}" for k, v in fields if v)
    return f"@{_bibtex_type(paper)}{{{key},\n  {body}\n}}"


def papers_to_bibtex(papers: list[Paper]) -> str:
    return "\n\n".join(paper_to_entry(p) for p in papers) + "\n"
