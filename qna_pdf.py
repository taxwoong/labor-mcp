# -*- coding: utf-8 -*-
"""
qna_pdf.py — 「근로기준법 질의회시집(2018.4.~2023.6.)」 PDF 파서

출처: moel.go.kr 정책자료실 bbs_seq=20240101604 (2024-01-24 게시, 근로기준정책과), 615쪽.
내려받기: /common/downloadFile.do?file_seq=20240102279&bbs_seq=20240101604&bbs_id=23&file_ext=pdf

본문 텍스트 구조 (pypdf 추출 기준, 2026-09-03 실측):
- 홀수쪽 머리글 "제1장 총칙 / 39", 짝수쪽 머리글 "38 / 근로기준법 질의회시집"
- 항목 = 제목(어절 사이가 두 칸 공백 — "대학병원  교원이  협력병원에 …") → 질의 → 회시
  → 끝줄 "(근로기준정책과-4178, 2022.12.26.)" 문서번호. 질의/회시 경계 표식은 텍스트에 없다.
- 표지·목차(1~24쪽)와 장 표지쪽은 이미지라 텍스트가 비어 있다.

파싱 전략: 문서번호 줄을 항목 구분자로 삼아 그 앞 덩어리를 한 항목으로 본다. 제목은 덩어리
첫 줄부터 '두 칸 공백' 패턴이 있는 줄까지(다음 줄이 짧은 잔여 어절이면 이어 붙임).
"""
import re
from pathlib import Path
from typing import List

DOWNLOAD_URL = ("https://www.moel.go.kr/common/downloadFile.do"
                "?file_seq=20240102279&bbs_seq=20240101604&bbs_id=23&file_ext=pdf")

_DOCNO_LINE = re.compile(
    r"^\s*\(\s*(?P<no>[가-힣A-Za-z]+(?:\s?[가-힣A-Za-z]+)*\s?-?\s?\d[\d\-]*)\s*,\s*"
    r"(?P<date>\d{4}\s*\.\s*\d{1,2}\s*\.\s*\d{1,2}\s*\.?)\s*\)\s*$")
_HEADER_ODD = re.compile(r"^\s*(제\s*\d+\s*장\s+[^/]+?)\s*/\s*\d+\s*$")
_HEADER_EVEN = re.compile(r"^\s*\d+\s*/\s*근로기준법\s*질의회시집\s*$")
_TITLE_HINT = re.compile(r"\S  +\S")


def extract_pages(pdf_path) -> List[str]:
    from pypdf import PdfReader   # 지연 import — 서버 기동에 pypdf가 없어도 되게
    reader = PdfReader(str(pdf_path))
    return [(p.extract_text() or "") for p in reader.pages]


def _norm_docno(no: str) -> str:
    no = re.sub(r"\s+", " ", no).strip()
    return re.sub(r"\s*-\s*", "-", no)


def _norm_date(d: str) -> str:
    m = re.match(r"(\d{4})\s*\.\s*(\d{1,2})\s*\.\s*(\d{1,2})", d)
    return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}" if m else ""


def parse_entries(pages: List[str]) -> List[dict]:
    """페이지 텍스트 목록 → [{doc_no, date, title, body, chapter, page}]"""
    entries, chunk, chapter, chunk_page = [], [], "", 0
    for pno, text in enumerate(pages, start=1):
        for raw in text.split("\n"):
            line = raw.rstrip()
            if not line.strip():
                continue
            mh = _HEADER_ODD.match(line)
            if mh:
                chapter = re.sub(r"\s+", " ", mh.group(1)).strip()
                continue
            if _HEADER_EVEN.match(line):
                continue
            md = _DOCNO_LINE.match(line)
            if md:
                if chunk:
                    entries.append(_build(chunk, _norm_docno(md.group("no")),
                                          _norm_date(md.group("date")), chapter, chunk_page))
                chunk, chunk_page = [], 0
                continue
            if not chunk:
                chunk_page = pno
            chunk.append(line.strip())
    return [e for e in entries if e["body"] or e["title"]]


_SECTION_HEADER = re.compile(r"^(제\s*\d+\s*[장절]\b.*|\d{1,2}\s+\S{1,12}(\s\S{1,12}){0,4})$")


def _build(lines: List[str], doc_no: str, date: str, chapter: str, page: int) -> dict:
    # 장 표지("제1장 총 칙")·절 번호("7 임금 연대 책임")가 항목 첫머리에 섞여 오면 걷어낸다
    while lines and _SECTION_HEADER.match(lines[0]) and not _TITLE_HINT.search(lines[0]):
        lines = lines[1:]
    title_lines, i = [], 0
    while i < len(lines) and (_TITLE_HINT.search(lines[i]) or
                              (title_lines and len(lines[i]) <= 12 and not lines[i].startswith(("「", "-", "○", "「")))):
        title_lines.append(lines[i])
        i += 1
        if len(title_lines) >= 3:
            break
    if not title_lines:               # 제목 힌트가 없으면 첫 줄을 제목으로
        title_lines, i = lines[:1], 1
    title = re.sub(r"\s+", " ", " ".join(title_lines)).strip()
    body = "\n".join(lines[i:]).strip()
    return {"doc_no": doc_no, "date": date, "title": title, "body": body,
            "chapter": chapter, "page": page}


def parse_pdf(pdf_path) -> List[dict]:
    return parse_entries(extract_pages(pdf_path))


if __name__ == "__main__":
    import sys
    p = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data/근로기준법_질의회시집_2018-2023.pdf")
    es = parse_pdf(p)
    print(len(es), "entries")
    for e in es[:3]:
        print(e["doc_no"], e["date"], e["chapter"], "|", e["title"][:50], "|", len(e["body"]))
