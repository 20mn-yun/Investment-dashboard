"""DART 원본 XML(dart4.xsd) → 목차·절 본문 텍스트.

MCP 도구 get_filing_toc / get_filing_section 용. 수집은 OpenDART 공식 API(document.xml)만 쓴다(뷰어·PDF 경로 아님).
저장: cache/dart_original/<rcept_no>.xml (본문 xml 1개, 첨부 제외). 한 번 받으면 다시 부르지 않는다.
"""
import io
import os
import re
import threading
import zipfile
from collections import OrderedDict
from datetime import date, timedelta

from lxml import etree

_BD = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(_BD, "cache", "dart_original")
_RCEPT_RE = re.compile(r"^\d{14}$")
_MEM_MAX = 4
_mem = OrderedDict()          # (rcept_no, mtime) → sections
_mem_lock = threading.Lock()

_WS = re.compile(r"[ \t\r\n 　]+")
_CELL_TAGS = {"TD", "TH", "TU", "TE"}
_SECTION_RE = re.compile(r"^SECTION-(\d)$")


_STRAY_LT = re.compile(rb"<(?!/?[A-Za-z][A-Za-z0-9-]*[\s/>]|\?xml|!)")
_STRAY_AMP = re.compile(rb"&(?!#\d+;|#x[0-9A-Fa-f]+;|[A-Za-z][A-Za-z0-9]*;)")


def _parse_xml(raw):
    """DART 원본은 본문에 '<법률 제20991호…>' 같은 맨 '<'·'&' 가 섞여 엄격한 XML 이 아니다.
    recover 모드만 쓰면 그 뒤 구조(SECTION·TITLE·TABLE)가 한 덩어리 글로 뭉개지므로, 태그가 아닌 '<'·'&' 를 먼저 이스케이프한다."""
    fixed = _STRAY_AMP.sub(b"&amp;", _STRAY_LT.sub(b"&lt;", raw))
    parser = etree.XMLParser(recover=True, huge_tree=True, resolve_entities=False, no_network=True)
    return etree.fromstring(fixed, parser)


def _clean(s):
    return _WS.sub(" ", s or "").strip()


def _table_lines(tbl):
    lines = []
    for tr in tbl.iter("TR"):
        cells = [_clean("".join(c.itertext())) for c in tr if isinstance(c.tag, str) and c.tag in _CELL_TAGS]
        if any(cells):
            lines.append(" | ".join(cells))
    return lines


def parse_blocks(raw):
    """원본 바이트 → [(kind, level, text)] 문서 순서. kind = title|p|table"""
    root = _parse_xml(raw)
    blocks = []

    def depth_of(el):
        d = 0
        p = el.getparent()
        while p is not None:
            m = _SECTION_RE.match(p.tag) if isinstance(p.tag, str) else None
            if m:
                d = max(d, int(m.group(1)))
            p = p.getparent()
        return d

    def walk(el):
        for ch in el:
            if not isinstance(ch.tag, str):
                continue
            tag = ch.tag
            if tag in ("TITLE", "COVER-TITLE"):
                t = _clean("".join(ch.itertext()))
                if t:
                    par = ch.getparent()
                    first = next((x for x in par if isinstance(x.tag, str) and x.tag == "TITLE"), None)
                    d = depth_of(ch) or 1
                    blocks.append(("title", d if first is ch else d + 1, t))
            elif tag == "TABLE" and ch.find(".//TABLE") is None:
                lines = _table_lines(ch)
                if lines:
                    blocks.append(("table", 0, "\n".join(lines)))
            elif tag == "P":
                t = _clean("".join(ch.itertext()))
                if t:
                    blocks.append(("p", 0, t))
            else:   # 표 안에 표가 든 바깥 표 포함: 칸에 직접 쓴 글은 문단으로, 안쪽은 계속 내려간다
                t = _clean(ch.text)
                if t and tag in _CELL_TAGS:
                    blocks.append(("p", 0, t))
                walk(ch)

    walk(root)
    return blocks


def build_sections(blocks):
    """→ [{idx, level, title, text, chars, tables}]. 첫 제목 앞 내용은 버린다."""
    secs = []
    cur = None
    for kind, level, text in blocks:
        if kind == "title":
            cur = {"idx": len(secs), "level": level, "title": text, "parts": [], "tables": 0}
            secs.append(cur)
        elif cur is not None:
            cur["parts"].append(text if kind == "p" else "[표]\n" + text)
            if kind == "table":
                cur["tables"] += 1
    for s in secs:
        s["text"] = "\n\n".join(s.pop("parts"))
        s["chars"] = len(s["text"])
    return secs


# ---------- 수집·캐시 ----------
def valid_rcept_no(s):
    """접수번호 14자리 숫자만 통과. 이 검사를 거친 값으로만 경로를 만든다."""
    return isinstance(s, str) and bool(_RCEPT_RE.match(s))


def _path(rcept_no):
    return os.path.join(CACHE_DIR, f"{rcept_no}.xml")


def _pick_main(zf, rcept_no):
    """dart_report._save_zip_members 와 같은 본문 선택 규칙: 이름이 rcept_no 인 항목, 없으면 가장 큰 xml"""
    members = [i for i in zf.infolist() if not i.is_dir()]
    if not members:
        return None
    main = next((i for i in members if os.path.splitext(os.path.basename(i.filename))[0] == str(rcept_no)), None)
    if main is None:
        xmls = [i for i in members if i.filename.lower().endswith(".xml")] or members
        main = max(xmls, key=lambda i: i.file_size)
    return main


def ensure_cached(rcept_no):
    """원본 본문 xml 경로. 반환 (path, err). 캐시에 있으면 DART 호출 없음.
    HTML 응답(점검)은 dart_report.DartPausedError 를 던진다. 로그·사유에 키·요청 URL 을 넣지 않는다."""
    if not valid_rcept_no(rcept_no):
        return None, "rcept_no 형식 오류"
    path = _path(rcept_no)
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return path, None
    import requests
    import dart_report
    if not dart_report.DART_API_KEY:
        return None, "DART 키 없음"
    dart_report._check_paused()
    try:
        res = requests.get("https://opendart.fss.or.kr/api/document.xml",
                           params={"crtfc_key": dart_report.DART_API_KEY, "rcept_no": rcept_no}, timeout=30)
    except Exception as e:
        return None, f"요청 실패 {type(e).__name__}"
    if dart_report._is_html(res):
        raise dart_report.DartPausedError(dart_report._mark_html("document.xml", res))
    if res.status_code != 200 or len(res.content) <= 100:
        return None, f"HTTP {res.status_code}, {len(res.content)}B"
    if res.content[:2] != b"PK":
        return None, f"ZIP 아님 ({(res.headers.get('Content-Type') or '?').split(';')[0]}, {len(res.content)}B)"
    try:
        with zipfile.ZipFile(io.BytesIO(res.content)) as zf:
            main = _pick_main(zf, rcept_no)
            if main is None:
                return None, "zip 안이 비어 있음"
            data = zf.read(main)
    except zipfile.BadZipFile:
        return None, "ZIP 손상"
    os.makedirs(CACHE_DIR, exist_ok=True)
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)          # 쓰다 만 파일이 캐시로 남지 않게
    return path, None


def load_sections(rcept_no):
    """ensure_cached → parse_blocks → build_sections. 반환 (sections, cached_before, err). (rcept_no, mtime) 키로 최근 4건 메모리 보관"""
    cached_before = valid_rcept_no(rcept_no) and os.path.exists(_path(rcept_no))
    path, err = ensure_cached(rcept_no)
    if err:
        return None, cached_before, err
    key = (rcept_no, os.path.getmtime(path))
    with _mem_lock:
        if key in _mem:
            _mem.move_to_end(key)
            return _mem[key], cached_before, None
    with open(path, "rb") as f:
        raw = f.read()
    secs = build_sections(parse_blocks(raw))
    with _mem_lock:
        for k in [k for k in _mem if k[0] == rcept_no]:
            _mem.pop(k, None)
        _mem[key] = secs
        while len(_mem) > _MEM_MAX:
            _mem.popitem(last=False)
    return secs, cached_before, None


def latest_periodic(corp_code):
    """최근 400일 정기보고서 중 (rcept_dt, rcept_no) 가 가장 큰 1건 {rcept_no, report_nm, rcept_dt, corp_name} 또는 None"""
    import dart_report
    today = date.today()
    items = dart_report.list_periodic_filings(corp_code, (today - timedelta(days=400)).strftime("%Y%m%d"), today.strftime("%Y%m%d"))
    items = [i for i in items if valid_rcept_no(i.get("rcept_no") or "")]
    if not items:
        return None
    return max(items, key=lambda i: (i.get("rcept_dt") or "", i.get("rcept_no") or ""))
