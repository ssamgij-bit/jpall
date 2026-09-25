"""TDnet XBRL (inline) 파서: 결산단신 요약, 실적 전망 수정, 배당 전망 수정"""
import zipfile, io, re
from lxml import etree

IX = "http://www.xbrl.org/2013/inlineXBRL"
XBRLI = "http://www.xbrl.org/2003/instance"

def _num(el):
    if el.get("{http://www.w3.org/2001/XMLSchema-instance}nil") == "true":
        return None
    txt = "".join(el.itertext()).strip().replace(",", "")
    if not txt or not re.match(r"^-?[\d.]+$", txt):
        return None
    v = float(txt)
    sc = el.get("scale")
    if sc:
        v *= 10 ** int(sc)
    if el.get("sign") == "-":
        v = -v
    return v

def parse_ixbrl(data: bytes):
    root = etree.fromstring(data, etree.XMLParser(recover=True, huge_tree=True))
    ctx = {}
    for c in root.iter("{%s}context" % XBRLI):
        cid = c.get("id")
        s = c.find(".//{%s}startDate" % XBRLI); e = c.find(".//{%s}endDate" % XBRLI); i = c.find(".//{%s}instant" % XBRLI)
        ctx[cid] = (s.text if s is not None else None, (e.text if e is not None else (i.text if i is not None else None)))
    facts = []
    els = list(root.iter("{%s}nonFraction" % IX)) + list(root.iter("{http://www.xbrl.org/2008/inlineXBRL}nonFraction"))
    for el in els:
        v = _num(el)
        if v is None:
            continue
        facts.append((el.get("name", "").split(":")[-1], el.get("contextRef", ""), v))
    return facts, ctx

def open_zip(blob: bytes):
    """zip 안에서 요약(Summary) 또는 단독 ixbrl 문서를 찾아 (종류, facts, ctx) 반환"""
    zf = zipfile.ZipFile(io.BytesIO(blob))
    names = [n for n in zf.namelist() if n.endswith("ixbrl.htm")]
    pick = [n for n in names if "/Summary/" in n] or [n for n in names if "Attachment" not in n]
    if not pick:
        return None, [], {}
    n = pick[0]
    m = re.search(r"tse-([a-z]+)-", n.split("/")[-1])
    kind = m.group(1) if m else ""
    facts, ctx = parse_ixbrl(zf.read(n))
    return kind, facts, ctx

# 항목 매핑(일본 기준 / IFRS / US 기준 / 금융업)
METRICS = {
    "rev": [r"^(NetSales|Revenue|Sales|OperatingRevenue|OrdinaryRevenue|GrossOperatingRevenue|TotalRevenue|NetSalesOfCompletedConstruction|OperatingRevenues)\w*$"],
    "op": [r"^(OperatingIncome|OperatingProfit)\w*$"],
    "ord": [r"^(OrdinaryIncome|ProfitBeforeTax|IncomeBeforeIncomeTaxes)\w*$"],
    "ni": [r"^(ProfitAttributableToOwnersOfParent|NetIncome|ProfitLossAttributableToOwnersOfParent|NetIncomeAttributableToOwnersOfParent|Profit)(IFRS|US|JMIS)?$"],
    "eps": [r"^(NetIncomePerShare|BasicEarningsPerShare|BasicEarningsLossPerShare|EarningsPerShare)(IFRS|US|JMIS)?$"],
}
EXCL = re.compile(r"^(ChangeIn|AmountChange|Diluted|Ratio|.*Ratio|.*ToNetSales|.*ToTotalAssets|.*ToEquity|.*PerShareOf)")

def pick(facts, metric, ctx_pred):
    # 패턴 안의 대안들을 앞에서부터 우선순위로 적용 (예: 지배주주 순이익 > 당기순이익)
    alts = re.match(r"\^\((.*?)\)", METRICS[metric][0]).group(1).split("|")
    tail = METRICS[metric][0].split(")", 1)[1]
    for alt in alts:
        p = re.compile("^" + alt + tail)
        for name, cid, v in facts:
            if not EXCL.match(name) and p.match(name) and ctx_pred(cid):
                return v
    return None

def pick_change(facts, metric, ctx_pred):
    pats = [re.compile(p) for p in METRICS[metric]]
    for name, cid, v in facts:
        if name.startswith("ChangeIn") and any(p.match(name[len("ChangeIn"):]) for p in pats) and ctx_pred(cid):
            return v
    return None

def summarize_results(kind, facts, ctx):
    """결산단신: 당기 누적 실적, 전년 동기, 증감률, 연간 회사 전망"""
    scope = "NonConsolidatedMember" if "ned" in kind else "ConsolidatedMember"
    cur_ids = [c for c in ctx if c.startswith("Current") and "Result" in c and scope in c and "Duration" in c and "_" in c]
    # 누적 기간 컨텍스트 이름(예: CurrentAccumulatedQ2Duration / CurrentYearDuration)
    base = None
    for pref in ["CurrentAccumulatedQ3Duration", "CurrentAccumulatedQ2Duration", "CurrentAccumulatedQ1Duration", "CurrentYearDuration"]:
        if any(c.startswith(pref + "_") for c in cur_ids) and any(n for n, cid, v in facts if cid.startswith(pref + "_" + scope + "_Result")):
            base = pref; break
    if base is None:
        return None
    q = {"CurrentAccumulatedQ1Duration": 1, "CurrentAccumulatedQ2Duration": 2, "CurrentAccumulatedQ3Duration": 3, "CurrentYearDuration": 4}[base]
    cur = lambda cid: cid == f"{base}_{scope}_ResultMember"
    pri = lambda cid: cid == f"{base.replace('Current', 'Prior')}_{scope}_ResultMember"
    res = {"q": q, "scope": "연결" if scope == "ConsolidatedMember" else "개별"}
    for m in ["rev", "op", "ord", "ni", "eps"]:
        res[m] = pick(facts, m, cur)
        res[m + "_py"] = pick(facts, m, pri)
        res[m + "_yoy"] = pick_change(facts, m, cur)
    # 기간
    cid0 = f"{base}_{scope}_ResultMember"
    res["start"], res["end"] = ctx.get(cid0, (None, None))
    # 연간 회사 전망 (분기: 당기 연간 / 연간결산: 다음 연도)
    fbase = "NextYearDuration" if q == 4 else "CurrentYearDuration"
    for tag in ["ForecastMember", "UpperMember"]:
        fc = lambda cid, t=tag: cid == f"{fbase}_{scope}_{t}"
        vals = {m: pick(facts, m, fc) for m in ["rev", "op", "ord", "ni", "eps"]}
        if any(v is not None for v in vals.values()):
            res["fc"] = vals; res["fc_kind"] = "상단" if tag == "UpperMember" else ""
            fcid = f"{fbase}_{scope}_{tag}"
            res["fc_end"] = ctx.get(fcid, (None, None))[1]
            break
    return res

def summarize_revision(facts, ctx):
    """실적 전망 수정: 이전(A) → 수정(B)"""
    out = {}
    for scope in ["ConsolidatedMember", "NonConsolidatedMember"]:
        for per in ["CurrentYearDuration", "CurrentAccumulatedQ2Duration", "CurrentAccumulatedQ1Duration", "CurrentAccumulatedQ3Duration"]:
            prev = lambda cid: cid.startswith(f"{per}_{scope}_PreviousMember_") and cid.endswith("ForecastMember")
            curr = lambda cid: cid.startswith(f"{per}_{scope}_CurrentMember_") and cid.endswith("ForecastMember")
            row = {m: (pick(facts, m, prev), pick(facts, m, curr)) for m in ["rev", "op", "ord", "ni", "eps"]}
            if any(b is not None for a, b in row.values()):
                cid = next((c for c in ctx if c.startswith(f"{per}_{scope}_CurrentMember_")), None)
                out = {"per": per, "scope": "연결" if scope == "ConsolidatedMember" else "개별", "rows": row,
                       "end": ctx.get(cid, (None, None))[1] if cid else None}
                return out
    return None

def summarize_dividend(facts, ctx):
    """배당 전망 수정: 연간 주당배당 이전 → 수정, 전기 실적"""
    def get(member, kind):
        for name, cid, v in facts:
            if name == "DividendPerShare" and "AnnualMember" in cid and member in cid and cid.endswith(kind):
                return v
        return None
    prev = get("PreviousMember", "ForecastMember"); curr = get("CurrentMember", "ForecastMember")
    last = None
    for name, cid, v in facts:
        if name == "DividendPerShare" and cid.startswith("PriorYearDuration") and "AnnualMember" in cid and cid.endswith("ResultMember"):
            last = v
    if prev is None and curr is None:
        return None
    return {"prev": prev, "curr": curr, "last": last}
