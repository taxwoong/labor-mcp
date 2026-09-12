# -*- coding: utf-8 -*-
"""
archive.py — 노무 사례 로컬 아카이브 (SQLite + FTS5)

원천 사이트(law.go.kr·moel.go.kr·data.go.kr)에서 받아 온 사례를 한 파일 DB에 쌓아
전문검색한다. 실시간 도구가 원천 장애·인증 문제·검색 제한(노동위 복수 카테고리 5건
미리보기 등)으로 막힐 때의 안전망이자, "사례를 최대한 많이" 확보하기 위한 저장소.

설계:
- docs 테이블 1개에 자료원(source)별 공통 스키마로 정규화해 넣는다. 자료원 고유 필드는
  extra(JSON)에 보존.
- 한국어 전문검색: FTS5 기본 토크나이저(unicode61)는 어절 단위라 "해고를"이 "해고"에
  안 걸린다. 그래서 본문을 **글자 2-gram**으로 풀어 contentless FTS(content='')에 넣고,
  검색어도 같은 방식으로 2-gram 구(phrase)로 바꿔 MATCH 한다. 2글자 검색어("해고",
  "임금")가 그대로 동작하고, 색인만 저장하므로 원문 이중 저장이 없다.
  (trigram 토크나이저는 3글자 미만 검색어를 지원하지 않아 채택하지 않음.)
- 2-gram 구 검색은 어절 경계를 넘는 오탐이 드물게 생긴다 ("X부당 당해" ≈ "부당당해").
  반환 페이지에 한해 원문 포함 여부를 재검증해 걸러낸다. total은 근사치.
- WAL 모드: 적재 스크립트가 쓰는 동안 서버가 읽을 수 있다.

DB 위치: 환경변수 LABOR_ARCHIVE_DB, 기본 <프로젝트>/data/labor_archive.sqlite (.gitignore).
"""
import json
import os
import re
import sqlite3
import time
import unicodedata
from pathlib import Path
from typing import Iterable, Optional

import vintage

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB = BASE_DIR / "data" / "labor_archive.sqlite"


def db_path() -> Path:
    return Path(os.environ.get("LABOR_ARCHIVE_DB") or DEFAULT_DB)


# 자료원 코드 → (표시명, 출처 설명). 검색 도구의 sources 인자와 통계 표시에 쓴다.
SOURCES = {
    "nlrc":    ("노동위원회 결정문", "law.go.kr Open API target=nlrc — 판정사항·판정요지·판정결과"),
    "moel":    ("고용노동부 행정해석(질의회시)", "law.go.kr Open API target=moelCgmExpc — 질의요지·회답"),
    "eiac":    ("고용보험심사위원회 결정문", "law.go.kr Open API target=eiac — 주문·이유 전문"),
    "iaciac":  ("산업재해보상보험재심사위원회 결정문", "law.go.kr Open API target=iaciac — 쟁점·주문·이유 전문"),
    "decc":    ("행정심판례(노동·4대보험 관련)", "law.go.kr Open API target=decc — 노동·사회보험 키워드로 추린 재결례"),
    "admrul":  ("행정규칙(고용노동부·보건복지부 훈령·예규·고시)",
                "law.go.kr Open API target=admrul org=1492000·1352000"),
    "counsel": ("고용노동부 빠른인터넷상담", "moel.go.kr 빠른인터넷상담 게시판 질의·답변"),
    "qnabook": ("근로기준법 질의회시집(2018.4~2023.6)", "고용노동부 근로기준정책과 발간 PDF"),
    "comwel":  ("근로복지공단 산재보험 판례", "data.go.kr 근로복지공단 산재보험 판례 판결문 조회 서비스"),
    # --- v1.3에서 추가: 법원·헌재·법제처 전수 + 4대보험 특별행정심판 ---
    "prec":    ("법원 판례", "law.go.kr Open API target=prec — 판시사항·판결요지·판례내용 전문(전수)"),
    "expc":    ("법제처 법령해석례", "law.go.kr Open API target=expc — 질의요지·회답·이유(전수)"),
    "detc":    ("헌법재판소 결정례", "law.go.kr Open API target=detc — 판시사항·결정요지·전문(전수)"),
    "hidrc":   ("건강보험분쟁조정위원회 재결례",
                "simpan.go.kr 온라인행정심판 — 심판청구 재결문 PDF 전문"),
    "npsrv":   ("국민연금 (재)심사청구 결정사례",
                "nps.or.kr 자료실 — 처분내용·청구인주장·쟁점·판단"),
}
SOURCE_ALIASES = {
    "노동위원회": "nlrc", "노동위": "nlrc", "판정례": "nlrc",
    "행정해석": "moel", "질의회시": "moel", "고용노동부": "moel",
    "고용보험심사위원회": "eiac", "고용보험": "eiac",
    "산재재심사위원회": "iaciac", "산업재해보상보험재심사위원회": "iaciac", "산재재심사": "iaciac",
    "행정심판": "decc", "행정심판례": "decc",
    "행정규칙": "admrul", "예규": "admrul", "훈령": "admrul", "고시": "admrul",
    "빠른인터넷상담": "counsel", "상담": "counsel", "인터넷상담": "counsel",
    "질의회시집": "qnabook",
    "산재판례": "comwel", "근로복지공단": "comwel", "산재": "comwel",
    "판례": "prec", "법원판례": "prec", "법원": "prec", "대법원": "prec",
    "법령해석례": "expc", "법제처": "expc", "법령해석": "expc",
    "헌재": "detc", "헌법재판소": "detc", "헌재결정례": "detc",
    "건강보험분쟁조정위원회": "hidrc", "건강보험분쟁조정위": "hidrc",
    "건강보험": "hidrc", "심판청구": "hidrc", "건보": "hidrc",
    "국민연금": "npsrv", "국민연금심사청구": "npsrv", "연금": "npsrv", "심사청구": "npsrv",
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS docs(
    id         INTEGER PRIMARY KEY,
    source     TEXT NOT NULL,
    doc_id     TEXT NOT NULL,
    title      TEXT NOT NULL DEFAULT '',
    doc_no     TEXT NOT NULL DEFAULT '',
    doc_date   TEXT NOT NULL DEFAULT '',
    date_kind  TEXT NOT NULL DEFAULT '',
    category   TEXT NOT NULL DEFAULT '',
    org        TEXT NOT NULL DEFAULT '',
    summary    TEXT NOT NULL DEFAULT '',
    body       TEXT NOT NULL DEFAULT '',
    extra      TEXT NOT NULL DEFAULT '{}',
    fetched_at TEXT NOT NULL,
    UNIQUE(source, doc_id)
);
CREATE INDEX IF NOT EXISTS docs_src_date ON docs(source, doc_date);
CREATE INDEX IF NOT EXISTS docs_doc_no ON docs(doc_no);
CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(ngram, content='', tokenize='unicode61');
CREATE TABLE IF NOT EXISTS ingest_state(
    source      TEXT PRIMARY KEY,
    last_run    TEXT NOT NULL,
    last_status TEXT NOT NULL,
    added       INTEGER NOT NULL DEFAULT 0,
    note        TEXT NOT NULL DEFAULT ''
);
-- 지금은 담을 수 없지만 나중에 다시 봐야 하는 문서 (빠른인터넷상담의 '미완료' 글 등).
-- 목록에서 건너뛰기만 하면, 나중에 답변이 달려도 그 글은 이미 목록 깊숙이 밀려나 있어
-- 증분 스캔(앞쪽 몇 페이지)에 다시 걸리지 않는다 — 갱신할 때마다 수십 건씩 영구 누락됐다.
CREATE TABLE IF NOT EXISTS pending_recheck(
    source     TEXT NOT NULL,
    doc_id     TEXT NOT NULL,
    meta       TEXT NOT NULL DEFAULT '{}',
    first_seen TEXT NOT NULL,
    last_try   TEXT NOT NULL DEFAULT '',
    tries      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(source, doc_id)
);
"""

# 이 횟수만큼 다시 확인해도 담을 수 없으면 포기한다 (매달 갱신 기준 1년)
PENDING_MAX_TRIES = 12

_TOKEN_RE = re.compile(r"[0-9A-Za-z가-힣]+")


def _norm_text(s: str) -> str:
    return unicodedata.normalize("NFC", s or "").lower()


def ngrams(text: str) -> str:
    """색인·검색 공용 2-gram 변환. 어절 안에서만 2-gram을 만들고 1글자 어절은 그대로."""
    out = []
    for tok in _TOKEN_RE.findall(_norm_text(text)):
        if len(tok) == 1:
            out.append(tok)
        else:
            out.extend(tok[i:i + 2] for i in range(len(tok) - 1))
    return " ".join(out)


def build_match(keyword: str) -> tuple:
    """검색어 → (FTS MATCH 식, 재검증용 어절 목록). 1글자 어절은 MATCH에서 빼고
    재검증에서만 본다(2-gram 색인에 1글자 항목은 1글자 어절뿐이라 걸리지 않음)."""
    phrases, tokens = [], []
    for tok in _TOKEN_RE.findall(_norm_text(keyword)):
        tokens.append(tok)
        if len(tok) >= 2:
            grams = [tok[i:i + 2] for i in range(len(tok) - 1)]
            phrases.append('"' + " ".join(grams) + '"')
    return " AND ".join(phrases), tokens


def norm_date(value) -> str:
    """'2016.5.9.' · '2023.06.21' · '20260626' · '2026-09-02' → 'YYYY-MM-DD'. 못 읽으면 ''."""
    s = str(value or "").strip()
    if not s:
        return ""
    m = re.match(r"^\s*(\d{4})[.\-/년\s]*(\d{1,2})[.\-/월\s]*(\d{1,2})", s)
    if m:
        y, mo, d = m.groups()
    else:
        m = re.match(r"^\s*(\d{4})(\d{2})(\d{2})", s)
        if not m:
            return ""
        y, mo, d = m.groups()
    try:
        if not (1 <= int(mo) <= 12 and 1 <= int(d) <= 31):
            return ""
    except ValueError:
        return ""
    return f"{y}-{int(mo):02d}-{int(d):02d}"


def resolve_sources(spec) -> list:
    """'nlrc,moel' / '노동위원회' / ['nlrc'] → 자료원 코드 목록. 모르면 ValueError."""
    if not spec:
        return []
    parts = spec if isinstance(spec, (list, tuple)) else re.split(r"[,\s]+", str(spec))
    out = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        code = SOURCE_ALIASES.get(p, p)
        if code not in SOURCES:
            raise ValueError(f"알 수 없는 자료원 {p!r} — 사용 가능: {', '.join(SOURCES)} "
                             f"(별칭: {', '.join(SOURCE_ALIASES)})")
        if code not in out:
            out.append(code)
    return out


# ---------------------------------------------------------------------------
# 연결·스키마
# ---------------------------------------------------------------------------

def open_db(path: Optional[Path] = None, readonly: bool = False) -> sqlite3.Connection:
    p = Path(path) if path else db_path()
    if readonly:
        if not p.exists():
            raise FileNotFoundError(f"아카이브 DB가 없습니다: {p} — ingest_archive.py로 먼저 적재하세요")
        conn = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True, check_same_thread=False,
                               timeout=30)
    else:
        p.parent.mkdir(parents=True, exist_ok=True)
        # 적재 프로세스 둘(law.go.kr 계열·빠른인터넷상담)이 같은 DB에 쓰는 경우를 위해 잠금 대기를 길게
        conn = sqlite3.connect(str(p), check_same_thread=False, timeout=120)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA)
        _migrate(conn)
    conn.row_factory = sqlite3.Row
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """기존 DB에 새 컬럼을 더한다 (CREATE TABLE IF NOT EXISTS는 컬럼 추가를 못 한다)."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(docs)")}
    if "date_kind" not in cols:
        conn.execute("ALTER TABLE docs ADD COLUMN date_kind TEXT NOT NULL DEFAULT ''")
        conn.commit()


def exists_db(path: Optional[Path] = None) -> bool:
    p = Path(path) if path else db_path()
    return p.exists() and p.stat().st_size > 0


# ---------------------------------------------------------------------------
# 적재
# ---------------------------------------------------------------------------

def _index_text(title: str, summary: str, body: str, doc_no: str) -> str:
    return ngrams(" ".join(x for x in (title, doc_no, summary, body) if x))


def upsert(conn: sqlite3.Connection, source: str, doc_id: str, *, title: str = "",
           doc_no: str = "", doc_date: str = "", category: str = "", org: str = "",
           summary: str = "", body: str = "", extra: Optional[dict] = None,
           date_kind: str = "") -> bool:
    """문서 1건 삽입/갱신. 새 문서면 True. 커밋은 호출자가 한다(배치)."""
    if source not in SOURCES:
        raise ValueError(f"알 수 없는 자료원: {source}")
    doc_id = str(doc_id).strip()
    if not doc_id:
        raise ValueError("doc_id가 비어 있습니다")
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    fields = dict(title=title or "", doc_no=doc_no or "", doc_date=norm_date(doc_date) or "",
                  date_kind=date_kind or "", category=category or "", org=org or "",
                  summary=summary or "", body=body or "",
                  extra=json.dumps(extra or {}, ensure_ascii=False), fetched_at=now)
    row = conn.execute("SELECT id, title, doc_no, summary, body FROM docs WHERE source=? AND doc_id=?",
                       (source, doc_id)).fetchone()
    new_ngram = _index_text(fields["title"], fields["summary"], fields["body"], fields["doc_no"])
    if row:
        old_ngram = _index_text(row["title"], row["summary"], row["body"], row["doc_no"])
        # contentless FTS는 삭제 시 원래 색인 텍스트를 다시 넘겨야 한다
        conn.execute("INSERT INTO docs_fts(docs_fts, rowid, ngram) VALUES('delete', ?, ?)",
                     (row["id"], old_ngram))
        conn.execute(
            "UPDATE docs SET title=:title, doc_no=:doc_no, doc_date=:doc_date, date_kind=:date_kind, "
            "category=:category, org=:org, summary=:summary, body=:body, extra=:extra, "
            "fetched_at=:fetched_at WHERE id=:id",
            dict(fields, id=row["id"]))
        conn.execute("INSERT INTO docs_fts(rowid, ngram) VALUES(?, ?)", (row["id"], new_ngram))
        return False
    cur = conn.execute(
        "INSERT INTO docs(source, doc_id, title, doc_no, doc_date, date_kind, category, org, "
        "summary, body, extra, fetched_at) "
        "VALUES(:source, :doc_id, :title, :doc_no, :doc_date, :date_kind, :category, :org, "
        ":summary, :body, :extra, :fetched_at)",
        dict(fields, source=source, doc_id=doc_id))
    conn.execute("INSERT INTO docs_fts(rowid, ngram) VALUES(?, ?)", (cur.lastrowid, new_ngram))
    return True


def known_ids(conn: sqlite3.Connection, source: str) -> set:
    return {r[0] for r in conn.execute("SELECT doc_id FROM docs WHERE source=?", (source,))}


def add_pending(conn: sqlite3.Connection, source: str, doc_id: str,
                meta: Optional[dict] = None) -> None:
    """다음 갱신 때 다시 볼 문서로 적어 둔다. 이미 있으면 first_seen을 보존한다."""
    conn.execute(
        "INSERT INTO pending_recheck(source, doc_id, meta, first_seen) VALUES(?,?,?,?) "
        "ON CONFLICT(source, doc_id) DO UPDATE SET meta=excluded.meta",
        (source, str(doc_id), json.dumps(meta or {}, ensure_ascii=False),
         time.strftime("%Y-%m-%dT%H:%M:%S")))


def pending(conn: sqlite3.Connection, source: str) -> list:
    """재확인 대기 목록 — [{doc_id, meta, first_seen, tries}] (오래 기다린 것부터)."""
    rows = conn.execute(
        "SELECT doc_id, meta, first_seen, tries FROM pending_recheck WHERE source=? "
        "ORDER BY first_seen", (source,)).fetchall()
    out = []
    for r in rows:
        try:
            meta = json.loads(r["meta"] or "{}")
        except ValueError:
            meta = {}
        out.append({"doc_id": r["doc_id"], "meta": meta, "first_seen": r["first_seen"],
                    "tries": r["tries"]})
    return out


def drop_pending(conn: sqlite3.Connection, source: str, doc_id: str) -> None:
    conn.execute("DELETE FROM pending_recheck WHERE source=? AND doc_id=?", (source, str(doc_id)))


def bump_pending(conn: sqlite3.Connection, source: str, doc_id: str) -> None:
    conn.execute("UPDATE pending_recheck SET tries=tries+1, last_try=? WHERE source=? AND doc_id=?",
                 (time.strftime("%Y-%m-%dT%H:%M:%S"), source, str(doc_id)))


def purge_pending(conn: sqlite3.Connection, source: str,
                  max_tries: int = PENDING_MAX_TRIES) -> int:
    """오래도록 담기지 않은 항목을 버린다. 반환: 버린 개수."""
    cur = conn.execute("DELETE FROM pending_recheck WHERE source=? AND tries>=?",
                       (source, int(max_tries)))
    return cur.rowcount or 0


def set_state(conn: sqlite3.Connection, source: str, status: str, added: int = 0, note: str = ""):
    conn.execute(
        "INSERT INTO ingest_state(source, last_run, last_status, added, note) VALUES(?,?,?,?,?) "
        "ON CONFLICT(source) DO UPDATE SET last_run=excluded.last_run, last_status=excluded.last_status, "
        "added=excluded.added, note=excluded.note",
        (source, time.strftime("%Y-%m-%dT%H:%M:%S"), status, int(added), note[:500]))
    conn.commit()


# ---------------------------------------------------------------------------
# 조회
# ---------------------------------------------------------------------------

def _snippet(text: str, tokens: list, width: int = 110) -> str:
    plain = re.sub(r"\s+", " ", text or "")
    low = _norm_text(plain)
    for tok in tokens:
        i = low.find(tok)
        if i >= 0:
            s = max(0, i - width)
            e = min(len(plain), i + len(tok) + width)
            return ("…" if s > 0 else "") + plain[s:e] + ("…" if e < len(plain) else "")
    return plain[:2 * width] + ("…" if len(plain) > 2 * width else "")


def _date_fields(r: sqlite3.Row) -> dict:
    """일자를 **있는 그대로** 표기한다 — 추정치를 확정 일자처럼 보이게 하지 않는 것이 요점.

    date_kind가 붙은 자료(산재판례의 사건번호 접수연도 등)는 무엇을 근거로 한 연도인지
    함께 알리고, 일자가 없으면 '미상'으로 명시한다. 정렬·대조용 ISO 값은 일자_ISO로 따로 준다.
    """
    d, kind = r["doc_date"], (r["date_kind"] if "date_kind" in r.keys() else "")
    if not d:
        return {"일자": "미상", "일자_ISO": ""}
    if kind:
        # 문구를 자료원에 안 맞게 박아 두면 안 된다 — '결정연도'(국민연금)에 "선고·의결일
        # 아님"이 붙으면 어색하고, 요점은 어느 쪽이든 **확정 일자가 아니라는 것**이다
        return {"일자": f"{d[:4]}년경 ({kind} — 확정 일자 아님)", "일자_ISO": d, "일자근거": kind}
    return {"일자": d, "일자_ISO": d}


def _row_to_item(r: sqlite3.Row, tokens: list, with_body: bool = False, max_chars: int = 8000) -> dict:
    item = {
        "자료원": r["source"], "자료원명": SOURCES.get(r["source"], (r["source"],))[0],
        "doc_id": r["doc_id"], "제목": r["title"], "문서번호": r["doc_no"],
    }
    item.update(_date_fields(r))
    if r["category"]:
        item["구분"] = r["category"]
    if r["org"]:
        item["기관"] = r["org"]
    if with_body:
        if r["summary"]:
            item["요지"] = r["summary"]
        body = r["body"] or ""
        if len(body) > max_chars:
            item["본문"] = body[:max_chars]
            item["잘림"] = f"본문 전체 {len(body)}자 중 앞 {max_chars}자 — max_chars를 늘려 재조회"
        else:
            item["본문"] = body
        try:
            extra = json.loads(r["extra"] or "{}")
        except ValueError:
            extra = {}
        if extra:
            item["추가정보"] = extra
    else:
        item["발췌"] = _snippet(" ".join(x for x in (r["summary"], r["body"]) if x), tokens)
    return item


def search(conn: sqlite3.Connection, keyword: str, sources=None, date_from: str = "",
           date_to: str = "", limit: int = 10, offset: int = 0, latest_first: bool = False) -> dict:
    """2-gram 전문검색. 어절은 AND. 반환 {total(근사), items[...]}."""
    match, tokens = build_match(keyword)
    if not tokens:
        raise ValueError("검색어(keyword)가 비어 있습니다")
    limit = max(1, min(int(limit), 50))
    offset = max(0, int(offset))
    codes = resolve_sources(sources)
    where, params = [], []
    if codes:
        where.append(f"d.source IN ({','.join('?' * len(codes))})")
        params.extend(codes)
    df, dt = norm_date(date_from), norm_date(date_to)
    if date_from and not df:
        raise ValueError(f"date_from 형식 오류: {date_from!r} (YYYYMMDD 또는 YYYY-MM-DD)")
    if date_to and not dt:
        raise ValueError(f"date_to 형식 오류: {date_to!r} (YYYYMMDD 또는 YYYY-MM-DD)")
    if df:
        where.append("d.doc_date >= ?"); params.append(df)
    if dt:
        where.append("d.doc_date <= ?"); params.append(dt)
    if match:
        base = "FROM docs_fts f JOIN docs d ON d.id = f.rowid WHERE docs_fts MATCH ?"
        base_params = [match] + params
        order = "d.doc_date DESC, d.id DESC" if latest_first else "bm25(docs_fts), d.doc_date DESC"
    else:
        # 1글자 어절만 있는 검색어 — 색인으로 못 찾으므로 LIKE 전수 검색 (느림)
        base = "FROM docs d WHERE 1=1"
        base_params = list(params)
        for tok in tokens:
            base += " AND (d.title LIKE ? OR d.summary LIKE ? OR d.body LIKE ?)"
            base_params.extend([f"%{tok}%"] * 3)
        order = "d.doc_date DESC, d.id DESC"
    extra_where = (" AND " + " AND ".join(where)) if where else ""
    total = conn.execute(f"SELECT count(*) {base}{extra_where}", base_params).fetchone()[0]
    # 재검증에서 걸러질 몫을 감안해 여유 있게 가져온다
    rows = conn.execute(
        f"SELECT d.* {base}{extra_where} ORDER BY {order} LIMIT ? OFFSET ?",
        base_params + [limit * 2, offset]).fetchall()
    items, dropped = [], 0
    for r in rows:
        hay = _norm_text(" ".join((r["title"], r["doc_no"], r["summary"], r["body"])))
        if all(t in hay for t in tokens):
            items.append(_row_to_item(r, tokens))
            if len(items) >= limit:
                break
        else:
            dropped += 1
    out = {"total": max(0, total - dropped), "items": items, "keyword": keyword,
           "자료원": codes or list(SOURCES)}
    if dropped:
        out["참고"] = f"2-gram 색인 오탐 {dropped}건 제외 — total은 근사치"
    vintage.apply_to(out, keyword=keyword)
    return out


def get(conn: sqlite3.Connection, source: str, doc_id: str, max_chars: int = 8000) -> Optional[dict]:
    codes = resolve_sources(source)
    if len(codes) != 1:
        raise ValueError("source는 자료원 하나만 지정하세요")
    r = conn.execute("SELECT * FROM docs WHERE source=? AND doc_id=?", (codes[0], str(doc_id).strip())).fetchone()
    return _row_to_item(r, [], with_body=True, max_chars=max_chars) if r else None


def find_by_doc_no(conn: sqlite3.Connection, doc_no: str, sources=None, limit: int = 10) -> list:
    """문서번호 정확/접미 일치 — verify_citations의 로컬 빠른 경로."""
    q = re.sub(r"\s+", "", doc_no or "")
    if not q:
        return []
    codes = resolve_sources(sources)
    sql = "SELECT * FROM docs WHERE replace(doc_no, ' ', '') LIKE ?"
    params = [f"%{q}"]
    if codes:
        sql += f" AND source IN ({','.join('?' * len(codes))})"
        params.extend(codes)
    sql += " ORDER BY doc_date DESC LIMIT ?"
    params.append(int(limit))
    rows = conn.execute(sql, params).fetchall()
    return [_row_to_item(r, []) for r in rows]


def stats(conn: sqlite3.Connection) -> dict:
    per = {}
    for r in conn.execute("SELECT source, count(*) n, min(nullif(doc_date,'')) d0, max(nullif(doc_date,'')) d1 "
                          "FROM docs GROUP BY source"):
        per[r["source"]] = {"자료원명": SOURCES.get(r["source"], (r["source"],))[0], "건수": r["n"],
                            "최초일자": r["d0"] or "", "최근일자": r["d1"] or ""}
    for r in conn.execute("SELECT * FROM ingest_state"):
        per.setdefault(r["source"], {"자료원명": SOURCES.get(r["source"], (r["source"],))[0], "건수": 0})
        per[r["source"]].update({"최근적재": r["last_run"], "적재상태": r["last_status"],
                                 "최근추가": r["added"]})
        if r["note"]:
            per[r["source"]]["비고"] = r["note"]
    for r in conn.execute("SELECT source, count(*) n FROM pending_recheck GROUP BY source"):
        if r["source"] in per:
            per[r["source"]]["재확인대기"] = r["n"]
    total = conn.execute("SELECT count(*) FROM docs").fetchone()[0]
    out = {"총건수": total, "자료원별": per}
    try:
        p = db_path()
        if p.exists():
            out["DB파일"] = str(p)
            out["DB크기MB"] = round(p.stat().st_size / 1048576, 1)
    except OSError:
        pass
    missing = [f"{code}({name[0]})" for code, name in SOURCES.items() if code not in per]
    if missing:
        out["미적재자료원"] = missing
    return out
