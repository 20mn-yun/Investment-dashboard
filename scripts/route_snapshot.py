#!/usr/bin/env python3
"""주소표·조회 응답 스냅샷 (리팩터링 전후 대조용).

사용:
  python3 scripts/route_snapshot.py save <이름>   → logs/route_snapshots/<이름>.json
  python3 scripts/route_snapshot.py diff <A> <B>  → 주소표 다르면 실패, 응답은 상태 코드·최상위 키·자료형 다르면 실패

- 주소표는 app.py·dart_monitor.py(있으면)를 ast 로 정적 분석해서 얻는다. app 모듈을 로드하지 않는다(감시 루프가 뜨므로).
- 조회는 GET 만, 파일·메모리 읽기만 하는 주소만. 서버 127.0.0.1:8000.
"""
import ast
import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(BASE, "logs", "route_snapshots")
SERVER = "http://127.0.0.1:8000"
SOURCES = ["app.py", "dart_monitor.py"]
GET_URLS = [
    "/healthz", "/api/sector-leaders", "/api/stock-leaders",
    "/api/dart/daily", "/api/dart/daily?days=3", "/api/dart/dates",
    "/api/dart/monitor/config", "/api/dart/monitor/status", "/api/dart/monitor/log",
    "/api/dart/daily/resummarize",
]


def route_table():
    """[(경로, 메서드, 파일)] 정렬. @<무엇>.route(경로, methods=[...]) 데코레이터 기준, methods 없으면 GET."""
    rows = []
    for fn in SOURCES:
        p = os.path.join(BASE, fn)
        if not os.path.exists(p):
            continue
        tree = ast.parse(open(p, encoding="utf-8").read())
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for d in node.decorator_list:
                if not (isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr == "route" and d.args):
                    continue
                path = ast.literal_eval(d.args[0])
                methods = ["GET"]
                for kw in d.keywords:
                    if kw.arg == "methods":
                        methods = ast.literal_eval(kw.value)
                for m in methods:
                    rows.append([path, m.upper(), fn])
    return sorted(rows)


def _shape(v):
    if isinstance(v, dict):
        return {k: type(x).__name__ for k, x in v.items()}
    if isinstance(v, list):
        return f"list[{len(v)}]"
    return type(v).__name__


def fetch(url):
    try:
        with urllib.request.urlopen(SERVER + url, timeout=60) as r:
            status, body = r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        status, body = e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return {"status": None, "error": f"{type(e).__name__}: {e}", "raw": ""}
    try:
        data = json.loads(body)
        return {"status": status, "json": True, "kind": type(data).__name__, "shape": _shape(data), "raw": body}
    except ValueError:
        return {"status": status, "json": False, "kind": "text", "shape": None, "raw": body}


def save(name):
    os.makedirs(OUT_DIR, exist_ok=True)
    snap = {"routes": route_table(), "responses": {u: fetch(u) for u in GET_URLS}}
    path = os.path.join(OUT_DIR, f"{name}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, indent=1)
    print(f"저장: {path}")
    print(f"주소표 {len(snap['routes'])}개 (파일별: " + ", ".join(f"{s} {sum(1 for r in snap['routes'] if r[2] == s)}" for s in SOURCES) + ")")
    for u, r in snap["responses"].items():
        keys = list(r["shape"].keys()) if isinstance(r.get("shape"), dict) else r.get("shape")
        print(f"  {r['status']} {u:34s} {r.get('kind')} {keys if keys is not None else r.get('error', '')} ({len(r.get('raw') or '')}자)")


def diff(a, b):
    A = json.load(open(os.path.join(OUT_DIR, f"{a}.json"), encoding="utf-8"))
    B = json.load(open(os.path.join(OUT_DIR, f"{b}.json"), encoding="utf-8"))
    fail = 0
    ra = {(p, m) for p, m, _ in A["routes"]}
    rb = {(p, m) for p, m, _ in B["routes"]}
    if ra != rb:
        fail += 1
        print("[실패] 주소표 다름")
        for x in sorted(ra - rb):
            print("   빠짐:", x)
        for x in sorted(rb - ra):
            print("   추가:", x)
    else:
        moved = sorted((p, m, fa, fb) for p, m, fa in A["routes"] for p2, m2, fb in B["routes"] if (p, m) == (p2, m2) and fa != fb)
        print(f"[통과] 주소표 같음 ({len(ra)}개, 경로+메서드). 정의 파일만 바뀐 주소 {len(moved)}개")
    for u in GET_URLS:
        x, y = A["responses"].get(u, {}), B["responses"].get(u, {})
        why = []
        if x.get("status") != y.get("status"):
            why.append(f"상태 {x.get('status')}→{y.get('status')}")
        if x.get("kind") != y.get("kind"):
            why.append(f"자료형 {x.get('kind')}→{y.get('kind')}")
        elif isinstance(x.get("shape"), dict) and isinstance(y.get("shape"), dict):
            if set(x["shape"]) != set(y["shape"]):
                why.append(f"최상위 키 다름 -{sorted(set(x['shape']) - set(y['shape']))} +{sorted(set(y['shape']) - set(x['shape']))}")
            else:
                t = [k for k in x["shape"] if x["shape"][k] != y["shape"][k]]
                if t:
                    why.append("값 자료형 다름 " + ", ".join(f"{k}:{x['shape'][k]}→{y['shape'][k]}" for k in t))
        if why:
            fail += 1
            print(f"[실패] {u}: " + "; ".join(why))
        else:
            note = []
            if x.get("shape") != y.get("shape"):
                note.append(f"목록 길이 {x.get('shape')}→{y.get('shape')}")
            if x.get("raw") != y.get("raw"):
                note.append(f"원문 다름({len(x.get('raw') or '')}→{len(y.get('raw') or '')}자)")
            print(f"[통과] {y.get('status')} {u}" + (f"  · {'; '.join(note)}" if note else "  · 원문 동일"))
    print(f"\n결과: {'합격' if fail == 0 else f'불합격 {fail}건'}")
    return fail


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "save":
        save(sys.argv[2])
    elif len(sys.argv) >= 4 and sys.argv[1] == "diff":
        sys.exit(1 if diff(sys.argv[2], sys.argv[3]) else 0)
    else:
        print(__doc__)
        sys.exit(2)
