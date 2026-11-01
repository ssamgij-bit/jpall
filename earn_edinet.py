"""주주환원 10년 추이 — 일본 금융청 EDINET 유가증권보고서(有価証券報告書) XBRL → CSV

- 유가증권보고서 한 건에 '주요 경영지표 추이'(5년: 주당배당·배당성향·순이익)와 현금흐름표(2년: 배당금 지급·자기주식 취득)가 들어 있음
  → 2년 간격으로 5건(최근, 2년 전, 4년 전, 6년 전, 8년 전)을 받으면 10년치가 채워짐
- 회사별 결과는 STATE_DIR/edinet/ret/종목코드.json 에 저장(다음 유가증권보고서가 나오기 전까지 재사용)
- 날짜별 공시 목록은 STATE_DIR/edinet/days/날짜.json 에 유가증권보고서만 저장(여러 회사가 같이 씀 → API 호출 절약)
- 환경 변수: EDINET_API_KEY (대량보유 수집과 같은 키)
"""
import os, re, json, time, datetime as dt, urllib.parse

import earn as E
import holdings as Hd

API = "https://api.edinet-fsa.go.jp/api/v2/"
DIR = os.path.join(E.STATE_DIR, "edinet")
KEY = lambda: os.environ.get("EDINET_API_KEY", "")


def _day_list(day):
    """그날 제출된 유가증권보고서 [(증권코드 5자리, docID, 기간 끝)] — 저장분 우선"""
    p = os.path.join(DIR, "days", day + ".json")
    try:
        return json.load(open(p))
    except Exception:
        pass
    q = urllib.parse.urlencode({"date": day, "type": 2, "Subscription-Key": KEY()})
    j = json.loads(Hd.get(API + "documents.json?" + q, timeout=60))
    if str(j.get("metadata", {}).get("status")) != "200":
        raise RuntimeError(f"edinet list {day}: {j.get('metadata')}")
    out = [[r.get("secCode"), r.get("docID"), r.get("periodEnd")] for r in j.get("results") or []
           if r.get("docTypeCode") == "120" and r.get("secCode") and r.get("csvFlag") == "1" and r.get("withdrawalStatus") not in ("1", "2")]
    os.makedirs(os.path.dirname(p), exist_ok=True)
    json.dump(out, open(p, "w"))
    return out


def _fy_ends(fem, n=5, step=2):
    """최근에 유가증권보고서가 나왔을 회계연도 말(YYYY-MM-DD) n개, step년 간격"""
    t = dt.date.today()
    y, m = t.year, fem
    end = dt.date(y, m, 1)
    while (t - end).days < 100:   # 결산 후 약 3개월 안에 제출 → 그 전이면 1년 전 회계연도가 최신
        y -= 1; end = dt.date(y, m, 1)
    out = []
    for k in range(n):
        yy = y - step * k
        nm = dt.date(yy + (m == 12), m % 12 + 1, 1)
        out.append(nm - dt.timedelta(days=1))
    return out


def _find_doc(sec, fy_end, budget):
    """회계연도 말 + 약 3개월 안의 제출일을 훑어 그 회사 유가증권보고서 docID → (docID, 기간 끝) 또는 None"""
    order = [86, 87, 88, 85, 89, 90, 84, 91, 83, 92, 82, 80, 81, 93, 79, 78, 77, 76, 75, 70, 65, 60]
    for off in order:
        if time.time() > budget: return "budget"
        d = fy_end + dt.timedelta(days=off)
        if d.weekday() >= 5 or d > dt.date.today(): continue
        for s, doc, pe in _day_list(d.strftime("%Y-%m-%d")):
            if s == sec and (not pe or abs((dt.date.fromisoformat(pe) - fy_end).days) < 20):
                return doc, pe or fy_end.isoformat()
    return None


SUM_DPS = re.compile(r"DividendPaidPerShare\w*SummaryOfBusinessResults")
SUM_PAY = re.compile(r"PayoutRatio\w*SummaryOfBusinessResults")
SUM_NI = re.compile(r"ProfitLossAttributableToOwnersOfParent\w*SummaryOfBusinessResults|NetIncomeLoss\w*SummaryOfBusinessResults|ProfitLoss\w*SummaryOfBusinessResults")
CF_DIV = re.compile(r"(Cash)?DividendsPaid\w*FinCF\w*|PaymentsOfDividends\w*FinCF\w*")
CF_BUY = re.compile(r"PurchaseOfTreasury(Stock|Shares)\w*FinCF\w*|PaymentsForPurchaseOfTreasury\w*")


def _parse(doc, period_end):
    """유가증권보고서 CSV → {회계연도 말 YYYY-MM: {dps, payout, ni, div, buy}} (금액 백만 엔, 비율 %)"""
    blob = Hd.get(API + f"documents/{doc}?type=5&Subscription-Key={KEY()}", timeout=120)
    d = Hd.parse_csv(blob)
    pe = dt.date.fromisoformat(period_end)
    out = {}

    def ym(n):  # n년 전 회계연도 말
        try: e = pe.replace(year=pe.year - n)
        except ValueError: e = pe.replace(year=pe.year - n, day=28)
        return e.strftime("%Y-%m")

    def put(rx, key, scale=1.0, cons_first=True, sign=1):
        cands = {}
        for el, vals in d.items():
            if "NonControlling" in el or not rx.fullmatch(el): continue
            for ctx, v in vals:
                m = re.fullmatch(r"(CurrentYear|Prior(\d)Year)Duration(_NonConsolidatedMember)?", ctx)
                if not m: continue
                n = int(m.group(2) or 0); nc = bool(m.group(3))
                x = Hd.num(v)
                if x is None: continue
                pri = (1 if nc else 0) if cons_first else (0 if nc else 1)
                if n not in cands or pri < cands[n][0]: cands[n] = (pri, x)
        for n, (_, x) in cands.items():
            out.setdefault(ym(n), {})[key] = abs(x) * scale if sign else x * scale

    put(SUM_DPS, "dps", cons_first=False)
    put(SUM_PAY, "payout", scale=100.0, cons_first=True, sign=0)
    put(SUM_NI, "ni", scale=1e-6, sign=0)
    put(CF_DIV, "div", scale=1e-6)
    put(CF_BUY, "buy", scale=1e-6)
    for v in out.values():   # 배당성향이 0~1(소수)로 올 수도, 이미 %로 올 수도 있음
        if v.get("payout") is not None and v["payout"] > 300: v["payout"] /= 100
    return out


def history(code, fem, budget_s=90):
    """→ (연도별 목록[{fy, dps, payout, ni, div, buy, total}], 메모) — 최근 10개 회계연도"""
    if not KEY():
        return [], "EDINET 키가 없어 10년 추이를 만들지 못했습니다"
    p = os.path.join(DIR, "ret", f"{code}.json")
    try: c = json.load(open(p))
    except Exception: c = {"docs": {}, "years": {}}
    sec = (code + "0") if len(code) == 4 else code
    budget = time.time() + budget_s
    note = ""
    for fy_end in _fy_ends(fem):
        k = fy_end.strftime("%Y-%m")
        dv = c["docs"].get(k)
        if dv and not str(dv).startswith("none:"): continue
        if dv and time.time() - float(str(dv)[5:]) < 30 * 86400: continue   # 못 찾은 연도는 30일 뒤 다시
        try:
            r = _find_doc(sec, fy_end, budget)
        except Exception as e:
            E.log("edinet find error", code, e); note = "EDINET 목록 조회 오류"; break
        if r == "budget":
            note = "이번에는 일부 연도만 채웠습니다(다음 리포트 때 이어서 채움)"; break
        if not r:
            c["docs"][k] = f"none:{time.time()}"; continue
        doc, pe = r
        try:
            ys = _parse(doc, pe)
            for y, v in ys.items():   # 더 최근 보고서의 값을 우선(소급 조정 반영), 나머지는 빈 항목만 채움
                old = c["years"].get(y) or {}
                c["years"][y] = {**old, **v, "_pe": pe} if pe > old.get("_pe", "") else {**v, **old}
            c["docs"][k] = doc
        except Exception as e:
            E.log("edinet parse error", code, doc, e); c["docs"][k] = f"none:{time.time()}"
    c["t"] = time.time()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    json.dump(c, open(p, "w"), ensure_ascii=False)
    rows = []
    for y in sorted(c["years"])[-10:]:
        v = c["years"][y]
        tot = ((v.get("div") or 0) + (v.get("buy") or 0)) / v["ni"] * 100 if v.get("ni") and v["ni"] > 0 and (v.get("div") is not None or v.get("buy") is not None) else None
        rows.append({"fy": y, **{k: v.get(k) for k in ("dps", "payout", "ni", "div", "buy")}, "total": tot})
    return rows, note
