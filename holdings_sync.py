"""보유종목 자동 기록: 한투 IRP·메리츠 계좌 잔고·예수금을 조회해 구글 시트 '투자종목' 탭의 입력 칸(H 보유수량·M 매수가·예수금 F)만 갱신한다.

기본은 시험 모드(쓰기 없음, --dry-run 과 같음). --apply 를 줘야 실제로 쓴다.
행 번호는 하드코딩하지 않고 매 실행마다 B·C·D열을 읽어 구역(국내·해외·IRP)과 예수금 행을 찾는다.
증권사 단위로 처리하며, 조회 실패·구역 못 찾음·시트에 수량이 있는데 API 0건이면 그 증권사는 쓰지 않고 텔레그램으로 알린다.
조회 API 만 쓴다(주문·정정·취소 없음).
"""
import argparse
import json
import os
import re
import sys
import time
from datetime import datetime

import requests
from dotenv import load_dotenv

_BD = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_BD, ".env"))

SPREADSHEET_ID = os.getenv("HOLDINGS_SHEET_ID", "").strip()
TAB_NAME = "투자종목"
KEY_PATH = os.path.join(_BD, "portfolio-sync-key.json")
LOG_PATH = os.path.join(_BD, "cache", "holdings_sync_log.jsonl")
DART_CFG_PATH = os.path.join(_BD, "dart_monitor_config.json")

COL = {c: i for i, c in enumerate("ABCDEFGHIJKLMN")}
CODE_RE = re.compile(r"^[0-9A-Za-z]{6}$")
VENDOR_EXCH = {"0537": "NASDAQ", "0321": "NYSE"}


class SyncFail(Exception):
    pass


def _num(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).replace(",", "").strip()
    if s in ("", "-"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _qty(v):
    f = _num(v)
    if f is None:
        return 0
    return int(f) if float(f).is_integer() else f


def _cell(rows, r, col):
    row = rows[r - 1] if 0 < r <= len(rows) else []
    i = COL[col]
    return row[i] if i < len(row) else ""


def _s(v):
    return str(v if v is not None else "").strip()


def mask2(x):
    s = str(x or "")
    return "*" * max(0, len(s) - 2) + s[-2:]


def collect_meritz():
    import meritz_api
    hold = meritz_api.get_holdings()
    ovs = meritz_api.get_ovs_holdings()
    dep = meritz_api.get_deposit()
    cur = meritz_api.get_ovs_currencies()
    domestic = {}
    for x in hold:
        iscd = _s(x.get("iscd"))
        if _s(x.get("pros_trgt_cls_code")) != "1" or not re.fullmatch(r"A[0-9A-Z]{6}", iscd):
            continue
        q = _qty(x.get("rmnd_qty"))
        if not q:
            continue
        domestic[iscd[1:]] = {"name": _s(x.get("isnm")), "qty": q, "avg": _num(x.get("byng_unpr"))}
    overseas = {}
    for x in ovs:
        t = _s(x.get("ovrs_stck_symb_iscd"))
        q = _qty(x.get("rmnd_dqty"))
        if not t or not q:
            continue
        overseas[t] = {"name": _s(x.get("isnm")), "qty": q, "avg": _num(x.get("avrg_unpr")), "vendor": _s(x.get("ovrs_exch_vndr_idno")), "crcd": _s(x.get("crcd"))}
    d2 = _num(dep.get("d2_aft_tfam"))
    if d2 is None:
        raise SyncFail("메리츠 예수금 응답에 d2_aft_tfam 없음")
    by_cur = {_s(c.get("crcd")): c for c in cur}
    if "USD" not in by_cur or "JPY" not in by_cur:
        raise SyncFail(f"메리츠 외화잔고에 USD/JPY 없음 ({sorted(by_cur)})")
    usd = _num(by_cur["USD"].get("estm_frcr_amt"))
    jpy = _num(by_cur["JPY"].get("frcr_amt"))
    return {"domestic": domestic, "overseas": overseas, "krw_d2": int(round(d2)), "usd": usd, "jpy": jpy,
            "raw_counts": {"holdings": len(hold), "ovs_holdings": len(ovs)}}


def collect_kis_irp():
    import kis_api
    cano, prdt = os.environ.get("KIS_IRP_CANO"), os.environ.get("KIS_IRP_PRDT")
    if not cano or not prdt:
        raise SyncFail("KIS_IRP_CANO/KIS_IRP_PRDT 환경변수 없음")
    params = {"CANO": cano, "ACNT_PRDT_CD": prdt, "AFHR_FLPR_YN": "N", "OFL_YN": "", "INQR_DVSN": "02", "UNPR_DVSN": "01",
              "FUND_STTL_ICLD_YN": "N", "FNCG_AMT_AUTO_RDPT_YN": "N", "PRCS_DVSN": "00", "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""}
    rows, out2, tr_cont = [], None, ""
    for _ in range(5):
        headers = {"content-type": "application/json; charset=utf-8", "authorization": f"Bearer {kis_api.get_access_token()}",
                   "appkey": kis_api.APP_KEY, "appsecret": kis_api.APP_SECRET, "tr_id": "TTTC8434R", "custtype": "P", "tr_cont": tr_cont}
        try:
            r = requests.get(kis_api.BASE_URL + "/uapi/domestic-stock/v1/trading/inquire-balance", headers=headers, params=params, timeout=20)
            b = r.json()
        except (requests.RequestException, ValueError) as e:
            raise SyncFail(f"한투 IRP 조회 실패: {type(e).__name__}") from None
        if r.status_code != 200 or b.get("rt_cd") != "0":
            raise SyncFail(f"한투 IRP 조회 실패 HTTP {r.status_code} rt_cd={b.get('rt_cd')} {(b.get('msg1') or '').strip()}")
        rows += b.get("output1") or []
        out2 = (b.get("output2") or [None])[0] or out2
        if r.headers.get("tr_cont") in ("F", "M"):
            tr_cont = "N"
            params["CTX_AREA_FK100"], params["CTX_AREA_NK100"] = b.get("ctx_area_fk100", ""), b.get("ctx_area_nk100", "")
            continue
        break
    stocks = {}
    for x in rows:
        q = _qty(x.get("hldg_qty"))
        if not q:
            continue
        stocks[_s(x.get("pdno"))] = {"name": _s(x.get("prdt_name")), "qty": q, "avg": _num(x.get("pchs_avg_pric"))}
    d2 = _num((out2 or {}).get("prvs_rcdl_excc_amt"))
    if d2 is None:
        raise SyncFail("한투 IRP output2 에 prvs_rcdl_excc_amt 없음")
    return {"stocks": stocks, "krw_d2": int(round(d2)), "raw_count": len(rows), "cano_tail": mask2(cano)}


def open_sheet():
    import gspread
    if not SPREADSHEET_ID:
        raise SyncFail("HOLDINGS_SHEET_ID 환경변수가 없습니다 (.env 에 스프레드시트 ID 를 넣을 것)")
    gc = gspread.service_account(filename=KEY_PATH)
    return gc.open_by_key(SPREADSHEET_ID).worksheet(TAB_NAME)


def read_sheet(ws):
    last = ws.row_count
    vals = ws.get(f"A1:N{last}", value_render_option="UNFORMATTED_VALUE")
    forms = ws.get(f"A1:N{last}", value_render_option="FORMULA")
    return vals, forms


def _find_rows(vals, start, end, pred):
    return [r for r in range(start, end) if pred(r)]


def locate(vals):
    n = len(vals)
    B = lambda r: _s(_cell(vals, r, "B"))
    C = lambda r: _s(_cell(vals, r, "C"))
    D = lambda r: _s(_cell(vals, r, "D"))

    def first(pred, frm=1, to=None):
        for r in range(frm, (to or n) + 1):
            if pred(r):
                return r
        return None

    out = {"meritz": None, "irp": None, "errors": {}}
    try:
        dom_start = first(lambda r: B(r) == "국내")
        if not dom_start:
            raise SyncFail("국내 구역(B열 '국내') 없음")
        dom_sub = first(lambda r: D(r) == "소계", dom_start + 1)
        if not dom_sub:
            raise SyncFail("국내 소계 행 없음")
        dom_stocks = {C(r): r for r in _find_rows(vals, dom_start, dom_sub, lambda r: bool(CODE_RE.fullmatch(C(r))))}
        krw = first(lambda r: D(r) == "원화 예수금", dom_start, dom_sub - 1)
        if not krw:
            raise SyncFail("국내 '원화 예수금' 행 없음")
        ovs_start = dom_sub + 1
        ovs_sub = first(lambda r: D(r) == "소계", ovs_start)
        if not ovs_sub:
            raise SyncFail("해외 소계 행 없음")
        ovs_stocks = {C(r): r for r in _find_rows(vals, ovs_start, ovs_sub, lambda r: B(r) in ("NYSE", "NASDAQ") and C(r) and C(r) != "메리츠증권")}
        usd = first(lambda r: D(r) == "달러 예수금", ovs_start, ovs_sub - 1)
        jpy = first(lambda r: D(r) == "엔화 예수금", ovs_start, ovs_sub - 1)
        if not usd or not jpy:
            raise SyncFail("해외 '달러 예수금'/'엔화 예수금' 행 없음")
        out["meritz"] = {"dom_range": (dom_start, dom_sub), "dom_stocks": dom_stocks, "krw_row": krw,
                         "ovs_range": (ovs_start, ovs_sub), "ovs_stocks": ovs_stocks, "usd_row": usd, "jpy_row": jpy}
    except SyncFail as e:
        out["errors"]["meritz"] = str(e)
    try:
        irp_start = first(lambda r: B(r) == "IRP")
        if not irp_start:
            raise SyncFail("IRP 구역(B열 'IRP') 없음")
        irp_sub = first(lambda r: D(r) == "소계", irp_start + 1)
        if not irp_sub:
            raise SyncFail("IRP 소계 행 없음")
        irp_stocks = {C(r): r for r in _find_rows(vals, irp_start, irp_sub, lambda r: bool(CODE_RE.fullmatch(C(r))))}
        dep = first(lambda r: D(r) == "예수금", irp_start, irp_sub - 1)
        if not dep:
            raise SyncFail("IRP '예수금' 행 없음")
        out["irp"] = {"range": (irp_start, irp_sub), "stocks": irp_stocks, "dep_row": dep}
    except SyncFail as e:
        out["errors"]["irp"] = str(e)
    return out


def _diff_stock(vals, section, key, row, api, changes):
    cur_h = _qty(_cell(vals, row, "H"))
    cur_m = _num(_cell(vals, row, "M"))
    if api is None:
        if cur_h:
            changes.append({"section": section, "item": key, "cell": f"H{row}", "before": cur_h, "after": 0, "kind": "zero"})
        return
    if cur_h != api["qty"]:
        changes.append({"section": section, "item": key, "cell": f"H{row}", "before": cur_h, "after": api["qty"], "kind": "qty"})
    if api.get("avg") is not None:
        new_m = round(api["avg"], 4)
        if cur_m is None or round(cur_m, 4) != new_m:
            changes.append({"section": section, "item": key, "cell": f"M{row}", "before": cur_m, "after": new_m, "kind": "avg"})


def _diff_formula(forms, section, label, row, amount, changes):
    cur = _s(_cell(forms, row, "F"))
    new = f"={amount}/1000"
    if cur.replace(" ", "") != new:
        changes.append({"section": section, "item": label, "cell": f"F{row}", "before": cur, "after": new, "kind": "deposit"})


def _diff_number(vals, section, label, row, amount, changes):
    cur = _num(_cell(vals, row, "F"))
    new = round(float(amount), 2)
    if cur is None or round(cur, 2) != new:
        changes.append({"section": section, "item": label, "cell": f"F{row}", "before": cur, "after": new, "kind": "deposit"})


def plan(vals, forms, loc, meritz, irp, failures):
    changes, inserts = [], []
    if meritz and loc.get("meritz"):
        L = loc["meritz"]
        sheet_has_qty = any(_qty(_cell(vals, r, "H")) for r in list(L["dom_stocks"].values()) + list(L["ovs_stocks"].values()))
        if sheet_has_qty and not meritz["domestic"] and not meritz["overseas"]:
            failures["메리츠"] = "시트에 보유수량이 있는데 API 국내·해외 잔고가 0건"
        else:
            for code, row in L["dom_stocks"].items():
                _diff_stock(vals, "국내", code, row, meritz["domestic"].get(code), changes)
            for code, a in meritz["domestic"].items():
                if code not in L["dom_stocks"]:
                    inserts.append({"section": "국내", "item": code, "name": a["name"], "qty": a["qty"], "avg": a["avg"]})
            for t, row in L["ovs_stocks"].items():
                _diff_stock(vals, "해외", t, row, meritz["overseas"].get(t), changes)
            for t, a in meritz["overseas"].items():
                if t in L["ovs_stocks"]:
                    continue
                if a["vendor"] not in VENDOR_EXCH:
                    failures.setdefault("메리츠", "")
                    failures["메리츠"] = (failures["메리츠"] + f" 새 해외종목 {t} 벤더코드 {a['vendor']} 미지원").strip()
                    continue
                inserts.append({"section": "해외", "item": t, "name": a["name"], "qty": a["qty"], "avg": a["avg"], "exch": VENDOR_EXCH[a["vendor"]]})
            _diff_formula(forms, "국내", "원화 예수금", L["krw_row"], meritz["krw_d2"], changes)
            _diff_number(vals, "해외", "달러 예수금", L["usd_row"], meritz["usd"], changes)
            _diff_number(vals, "해외", "엔화 예수금", L["jpy_row"], meritz["jpy"], changes)
    if irp and loc.get("irp"):
        L = loc["irp"]
        sheet_has_qty = any(_qty(_cell(vals, r, "H")) for r in L["stocks"].values())
        if sheet_has_qty and not irp["stocks"]:
            failures["한투 IRP"] = "시트에 보유수량이 있는데 API 잔고가 0건"
        else:
            for code, row in L["stocks"].items():
                _diff_stock(vals, "IRP", code, row, irp["stocks"].get(code), changes)
            for code, a in irp["stocks"].items():
                if code not in L["stocks"]:
                    inserts.append({"section": "IRP", "item": code, "name": a["name"], "qty": a["qty"], "avg": a["avg"]})
            _diff_formula(forms, "IRP", "예수금", L["dep_row"], irp["krw_d2"], changes)
    return changes, inserts


def alert_text(failures):
    lines = [f"[보유종목 자동 기록] 실패 {len(failures)}건 — {datetime.now().strftime('%Y-%m-%d %H:%M')}"]
    for broker, why in failures.items():
        lines.append(f"- {broker}: {str(why)[:200]}")
    return "\n".join(lines)


def tg_creds():
    token = os.environ.get("TELEGRAM_TOKEN_GENERAL")
    try:
        with open(DART_CFG_PATH, "r", encoding="utf-8") as f:
            chat_id = (json.load(f).get("telegram_chat_ids") or {}).get("general")
    except (OSError, ValueError):
        chat_id = None
    return (token, chat_id) if token and chat_id else (None, None)


def tg_send(text):
    token, chat_id = tg_creds()
    if not token or not chat_id:
        print("텔레그램 설정 없음 — 발송 생략")
        return False
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": True}, timeout=15)
        return bool(r.ok and r.json().get("ok"))
    except (requests.RequestException, ValueError) as e:
        print(f"텔레그램 발송 실패: {type(e).__name__}")
        return False


def _log(entries):
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    ts = datetime.now().isoformat(timespec="seconds")
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(dict(e, ts=ts), ensure_ascii=False) + "\n")


def _insert_row(ws, vals, forms, loc, ins):
    sid = ws.id
    if ins["section"] == "국내":
        rows = sorted(loc["meritz"]["dom_stocks"].values())
    elif ins["section"] == "해외":
        rows = sorted(loc["meritz"]["ovs_stocks"].values())
    else:
        rows = sorted(loc["irp"]["stocks"].values())
    if not rows:
        raise SyncFail(f"{ins['section']} 구역에 복제할 종목 행이 없음")
    last = rows[-1]
    ws.spreadsheet.batch_update({"requests": [
        {"insertDimension": {"range": {"sheetId": sid, "dimension": "ROWS", "startIndex": last - 1, "endIndex": last}, "inheritFromBefore": True}},
        {"copyPaste": {"source": {"sheetId": sid, "startRowIndex": last, "endRowIndex": last + 1},
                       "destination": {"sheetId": sid, "startRowIndex": last - 1, "endRowIndex": last}, "pasteType": "PASTE_NORMAL"}},
    ]})
    new_row = last
    d_is_formula = _s(_cell(forms, last, "D")).startswith("=")
    raw = [{"range": f"C{new_row}", "values": [[ins["item"]]]}]
    user = [{"range": f"H{new_row}", "values": [[ins["qty"]]]}, {"range": f"M{new_row}", "values": [[round(ins["avg"] or 0, 4)]]},
            {"range": f"E{new_row}", "values": [["미분류"]]}, {"range": f"J{new_row}", "values": [[0]]}]
    if ins.get("exch"):
        raw.append({"range": f"B{new_row}", "values": [[ins["exch"]]]})
    if not d_is_formula:
        raw.append({"range": f"D{new_row}", "values": [[ins["name"]]]})
    ws.batch_update(raw, value_input_option="RAW")
    ws.batch_update(user, value_input_option="USER_ENTERED")
    return new_row


def apply_changes(ws, changes):
    data = [{"range": c["cell"], "values": [[c["after"]]]} for c in changes]
    if data:
        ws.batch_update(data, value_input_option="USER_ENTERED")


def run(apply=False):
    failures, report = {}, {"mode": "apply" if apply else "dry-run"}
    meritz = irp = None
    try:
        meritz = collect_meritz()
    except Exception as e:
        failures["메리츠"] = f"조회 실패: {e}"
    try:
        irp = collect_kis_irp()
        report["irp_cano"] = irp["cano_tail"]
    except Exception as e:
        failures["한투 IRP"] = f"조회 실패: {e}"
    try:
        ws = open_sheet()
        vals, forms = read_sheet(ws)
    except Exception as e:
        failures["시트"] = f"시트 읽기 실패: {type(e).__name__} {str(e)[:120]}"
        return report, [], [], failures, None
    loc = locate(vals)
    if "meritz" in loc["errors"]:
        failures["메리츠"] = f"시트 구역 못 찾음: {loc['errors']['meritz']}"
        meritz = None
    if "irp" in loc["errors"]:
        failures["한투 IRP"] = f"시트 구역 못 찾음: {loc['errors']['irp']}"
        irp = None
    if "메리츠" in failures:
        meritz = None
    if "한투 IRP" in failures:
        irp = None
    changes, inserts = plan(vals, forms, loc, meritz, irp, failures)
    report["loc"] = {"meritz": loc.get("meritz") and {"국내": loc["meritz"]["dom_range"], "국내 종목행": sorted(loc["meritz"]["dom_stocks"].values()),
                                                     "원화 예수금": loc["meritz"]["krw_row"], "해외": loc["meritz"]["ovs_range"],
                                                     "해외 종목행": sorted(loc["meritz"]["ovs_stocks"].values()),
                                                     "달러 예수금": loc["meritz"]["usd_row"], "엔화 예수금": loc["meritz"]["jpy_row"]},
                     "irp": loc.get("irp") and {"IRP": loc["irp"]["range"], "종목행": sorted(loc["irp"]["stocks"].values()), "예수금": loc["irp"]["dep_row"]}}
    report["api"] = {"meritz_domestic": len(meritz["domestic"]) if meritz else None, "meritz_overseas": len(meritz["overseas"]) if meritz else None,
                     "irp_stocks": len(irp["stocks"]) if irp else None}
    if apply:
        logs = []
        for ins in inserts:
            try:
                new_row = _insert_row(ws, vals, forms, loc, ins)
                logs.append({"section": ins["section"], "item": ins["item"], "cell": f"row{new_row}", "before": None, "after": "insert", "kind": "insert"})
                vals, forms = read_sheet(ws)
                loc = locate(vals)
            except Exception as e:
                failures[ins["section"]] = f"행 삽입 실패 {ins['item']}: {type(e).__name__}"
        if inserts:
            changes, _ = plan(vals, forms, loc, meritz, irp, failures)
        apply_changes(ws, changes)
        _log(logs + changes)
    if failures and apply:
        tg_send(alert_text(failures))
    return report, changes, inserts, failures, alert_text(failures) if failures else None


def main():
    ap = argparse.ArgumentParser(description="보유종목 자동 기록 (기본 시험 모드)")
    ap.add_argument("--apply", action="store_true", help="시트에 실제로 쓴다")
    ap.add_argument("--dry-run", action="store_true", help="쓰지 않고 변경 예정만 출력 (기본)")
    a = ap.parse_args()
    apply = bool(a.apply and not a.dry_run)
    t0 = time.time()
    report, changes, inserts, failures, alert = run(apply)
    print(f"모드: {report['mode']}  ({round(time.time() - t0, 1)}s)")
    if report.get("irp_cano"):
        print(f"한투 IRP 계좌: {report['irp_cano']}")
    print("API 건수:", json.dumps(report.get("api"), ensure_ascii=False))
    print("시트 구역:", json.dumps(report.get("loc"), ensure_ascii=False))
    print("\n구역 | 종목 | 칸 | 현재값 → 새값")
    for c in changes:
        print(f"{c['section']} | {c['item']} | {c['cell']} | {c['before']} → {c['after']}")
    if not changes:
        print("(변경 없음)")
    print("\n삽입 예정 행:")
    for i in inserts:
        print(f"  {i['section']} | {i['item']} {i['name']} | 수량 {i['qty']} | 평균단가 {i['avg']}" + (f" | {i['exch']}" if i.get("exch") else ""))
    if not inserts:
        print("  (없음)")
    print("\n실패 내역:")
    for k, v in failures.items():
        print(f"  {k}: {v}")
    if not failures:
        print("  (없음)")
    if alert and not apply:
        print("\n(시험 모드: 아래 알림은 발송하지 않음)\n" + alert)
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
