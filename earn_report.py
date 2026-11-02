"""실적 스크리닝 카드의 [📄 리포트] 버튼 → 한 파일짜리 HTML 리포트(차트 내장)

자료를 모으는 순서
 - TDnet: 결산단신 XBRL(손익계산서·세그먼트)·PDF, 결산 설명 자료 PDF, 질의응답 요지
 - 회사 IR 홈페이지: 결산 설명 자료·설명회 기록(書き起こし)·질의응답 (TDnet에 없을 때)
 - 로그미 파이낸스: 결산 설명회 기록 기사
 - 가부탄: 3개월·연간 실적, 시가총액, 대주주 / 네이버 증권: 주가 일봉

구성
 1. 머리말·KPI → 2. 이번 실적 결론(한 줄 + 설명)
 3. 사업부별 실적(매출·영업이익·이익률 표 + 매출·영업이익 성장 기여 %p 차트)
 4. 지역별 실적(같은 구성)
    - 결산단신 XBRL 세그먼트가 사업부(또는 지역)이면 그 숫자를 쓰고, 나머지 구분은 설명 자료에서 Gemini가 뽑은 숫자를 씀
    - 설명 자료 숫자는 전사 합계와 맞춰 보고 차이를 표시
 5. IR 자료에서 고른 핵심 데이터: Gemini가 설명 자료를 읽고 의미 있는 표·그래프를 골라 숫자를 뽑으면 한국어 차트로 다시 그림
    (수주·수주잔고, 제품·용도별, 가격·물량, KPI 추이 등) + 원문 쪽 이미지(접기)
 6. 경영진 Q&A(설명회 기록·질의응답이 있을 때)
 7. 분기·누적 손익 상세·연간 실적 표, 주가·매출·영업이익 차트
 8. 가이던스·리스크·다음에 확인할 점, 대주주, 원문 링크
"""
import io, os, re, json, time, base64, html, zipfile, subprocess, tempfile, datetime as dt

import earn as E
import xbrl as X
from jpall import http, gemini_json, GEMINI_MODELS, nfkc
def b64png(fig):
    buf = io.BytesIO(); fig.savefig(buf, format="png", dpi=150, facecolor="white", bbox_inches="tight"); E._plt().close(fig)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()

def esc(s): return html.escape(str(s if s is not None else ""), quote=False)

SLOW = {}  # 모델 → 다시 시도할 시각(응답 지연·과부하일 때)
def gemini_pdf(prompt, pdfs, max_tokens=12000):
    import jpall; key = jpall.gem_key()
    bad = [p for p in pdfs if p and b"%PDF" not in p[:1024]]
    if bad: E.log("not-pdf docs dropped", len(bad))
    pdfs = [p for p in pdfs if p and b"%PDF" in p[:1024]]   # PDF가 아닌 파일(웹페이지)을 PDF로 보내면 모든 모델이 400 오류
    if not pdfs:
        import jpall as _j
        if getattr(_j.AI_TL, "fails", None) is not None: _j.AI_TL.fails.append("nodoc")
    if not key or not pdfs: return None
    import aicache
    ck = aicache.key(prompt + f"|{max_tokens}|v2", pdfs); hit = aicache.get(ck)   # v2: 예전 저장분(가벼운 모델 결과일 수 있음)은 쓰지 않음
    if hit: E.log("ai cache hit(리포트)"); return hit   # 같은 자료·같은 지시문 → 저장해 둔 결과(한도 절약)
    parts = [{"inline_data": {"mime_type": "application/pdf", "data": base64.b64encode(p).decode()}} for p in pdfs] + [{"text": prompt}]
    body = json.dumps({"contents": [{"parts": parts}],
                       "generationConfig": {"temperature": 0.2, "maxOutputTokens": max_tokens, "responseMimeType": "application/json"}}).encode()
    out, model = E.heavy_call(body, "pdf", SLOW)   # 한도가 나면 같은 모델로 다른 보조 키 먼저, 다 막히면 가벼운 모델
    if out is not None and E.is_top(model): aicache.put(ck, out)   # 가벼운 모델 결과는 저장하지 않음 → 한도가 풀린 뒤 상위 모델로 다시
    return out

def tr_google(texts):
    """일본어 짧은 문자열 → 한국어(키 없는 공개 번역 주소). 실패하면 원문 그대로"""
    out = {}
    need = [t for t in dict.fromkeys(texts) if t]
    for i in range(0, len(need), 25):
        part = need[i:i + 25]
        try:
            import urllib.parse
            url = "https://translate.googleapis.com/translate_a/single?client=gtx&sl=ja&tl=ko&dt=t&q=" + urllib.parse.quote("\n".join(part))
            r = json.loads(http(url, timeout=20))
            ko = "".join(x[0] for x in r[0]).split("\n")
            if len(ko) == len(part): out.update({a: b.strip() for a, b in zip(part, ko)})
        except Exception as e:
            E.log("translate error", e)
    return out

# ───────────────────────── 결산단신 XBRL 첨부: 손익계산서·세그먼트 ─────────────────────────
def _labels(zf):
    """회사 고유 요소(부문 이름 등)의 일본어 표준 라벨"""
    out = {}
    for n in zf.namelist():
        if not n.endswith("-lab.xml") or "Attachment" not in n: continue
        t = zf.read(n).decode("utf-8", "ignore")
        loc = dict((lab, href.split("#")[-1].split("_", 1)[-1]) for href, lab in re.findall(r'<link:loc[^>]*xlink:href="([^"]+)"[^>]*xlink:label="([^"]+)"', t))
        res = {}
        for m in re.finditer(r'<link:label[^>]*xlink:label="([^"]+)"[^>]*xlink:role="([^"]+)"[^>]*>([^<]*)</link:label>', t):
            res[m.group(1)] = (m.group(2), m.group(3))
        for fr, to in re.findall(r'<link:labelArc[^>]*xlink:from="([^"]+)"[^>]*xlink:to="([^"]+)"', t):
            role, txt = res.get(to, ("", ""))
            if role.endswith("/label") and fr in loc: out[loc[fr]] = txt
    return out

KO_COMMON = {"日本": "일본", "国内": "국내", "海外": "해외", "アジア": "아시아", "中国": "중국", "アメリカ": "미국", "米国": "미국", "北米": "북미", "米州": "미주",
             "南米": "남미", "ヨーロッパ": "유럽", "欧州": "유럽", "その他": "기타", "東南アジア": "동남아시아", "インド": "인도", "韓国": "한국", "台湾": "대만"}
STD_MEMBER = {"OtherReportableSegmentsMember": "その他", "OtherSegmentsMember": "その他", "OtherMember": "その他"}

def attachments(xbrl_url=None, blob=None):
    """→ {'pl': {항목: (당기누적, 전기누적)}, 'seg': [{name, rev, rev_py, op, op_py}], 'per': '누적 기간 이름'}"""
    blob = blob or http(xbrl_url, timeout=60)
    zf = zipfile.ZipFile(io.BytesIO(blob))
    labels = _labels(zf)
    out = {"pl": {}, "seg": [], "kind": "", "cf": {}}
    for n in zf.namelist():
        if not n.endswith("ixbrl.htm") or "/Attachment/" not in n: continue
        kind = re.search(r"/\d+-([a-z]+)\d*-", n)
        kind = kind.group(1) if kind else ""
        facts, ctx = X.parse_ixbrl(zf.read(n))
        if kind.endswith("pl") or kind.endswith("pc"):  # 손익계산서(또는 손익·포괄 통합)
            for name, cid, v in facts:
                if "_" in cid: continue
                slot = 1 if cid.startswith("Prior1") else 0 if not cid.startswith("Prior") else None
                if slot is None: continue
                cur = out["pl"].setdefault(name, [None, None])
                if cur[slot] is None: cur[slot] = v
        elif kind.endswith("cf"):  # 현금흐름표(배당금 지급·자기주식 취득 → 주주환원)
            for name, cid, v in facts:
                if "_" in cid: continue
                slot = 1 if cid.startswith("Prior1") else 0 if not cid.startswith("Prior") else None
                if slot is None: continue
                cur = out["cf"].setdefault(name, [None, None])
                if cur[slot] is None: cur[slot] = v
        elif kind.endswith("sg"):  # 세그먼트 정보
            seg = {}
            for name, cid, v in facts:
                if "_" not in cid: continue
                per, mem = cid.split("_", 1)
                if not per.startswith(("Prior1", "Interim", "Current", "YTD", "Quarter", "Year")) and not per.endswith("Duration"): continue
                if not per.endswith("Duration"): continue
                slot = 1 if per.startswith("Prior1") else 0
                key = mem.split("_")[0] if mem.count("_") else mem
                if key in ("ReportableSegmentsMember", "ReconcilingItemsMember", "TotalMember") or "Reconciling" in key or "Elimination" in key or "Corporate" in key: continue
                mname = re.sub(r"^.*?\d{5}", "", key)  # 회사 접두어 제거
                d = seg.setdefault(mname, {})
                d.setdefault(name, [None, None])
                if d[name][slot] is None: d[name][slot] = v
            for mname, d in seg.items():
                rev = d.get("RevenuesFromExternalCustomers") or next((d[k] for k in d if re.match(r"(NetSales|Revenue|OperatingRevenue)", k)), None)
                op = next((d[k] for k in d if re.match(r"(OperatingIncome|OperatingProfit|SegmentProfit|SegmentIncome|ProfitLossOfReportableSegment|OrdinaryIncome)", k)), None)
                if not rev and not op: continue
                lab = labels.get(mname) or STD_MEMBER.get(mname) or re.sub(r"(ReportableSegments?Member|SegmentMember|Member)$", "", mname)
                out["seg"].append({"key": mname, "name": nfkc(lab), "rev": (rev or [None, None])[0], "rev_py": (rev or [None, None])[1],
                                   "op": (op or [None, None])[0], "op_py": (op or [None, None])[1]})
    return out

def pl_pick(pl, *pats):
    for p in pats:
        for k, v in pl.items():
            if re.fullmatch(p, k): return v
    return [None, None]

def pl_compact(pl):
    """XBRL 손익 항목 → {표 항목 이름: [당기 누적, 전기 누적]}(엔)"""
    out = {}
    for lab, pats in PL_ROWS:
        v = pl_pick(pl, *pats)
        if v[0] is not None or v[1] is not None: out[lab] = list(v)
    return out

PL_ROWS = [("매출", [r"NetSales", r"Revenue(IFRS)?", r"OperatingRevenue\w*", r"NetSalesIFRS", r"RevenueFromContractsWithCustomers\w*"]),
           ("매출원가", [r"CostOfSales(IFRS)?", r"CostOfRevenue\w*"]),
           ("매출총이익", [r"GrossProfit(IFRS)?"]),
           ("판관비", [r"SellingGeneralAndAdministrativeExpenses(IFRS)?"]),
           ("영업이익", [r"OperatingIncome", r"OperatingProfitLoss(IFRS)?"]),
           ("영업외수익", [r"NonOperatingIncome"]), ("영업외비용", [r"NonOperatingExpenses"]),
           ("경상이익", [r"OrdinaryIncome"]), ("특별이익", [r"ExtraordinaryIncome"]), ("특별손실", [r"ExtraordinaryLoss"]),
           ("세전이익", [r"IncomeBeforeIncomeTaxes", r"ProfitLossBeforeTax(IFRS)?"]),
           ("지배주주 순이익", [r"ProfitLossAttributableToOwnersOfParent(IFRS)?", r"ProfitAttributableToOwnersOfParent", r"ProfitLoss(IFRS)?"])]

# ───────────────────────── Gemini: 결론·번역 ─────────────────────────

# ───────────────────────── Gemini: 설명 자료·기록 분석 ─────────────────────────
IR_PROMPT = """너는 일본 중소형주를 깊게 보는 한국 바이사이드 애널리스트다. 첨부 PDF(결산 설명 자료, 결산단신, 설명회 기록 중 있는 것)와 아래 글을 읽고
이번 실적을 공부하는 데 필요한 것을 한국어 JSON으로 정리하라.

규칙
- 자료에 있는 숫자·사실만. 추측 금지. 없으면 빈 값. 일본어를 그대로 쓰지 말 것(제품·지역·인명은 한국어 또는 통용 영문).
- 금액 단위는 억 엔으로 통일(백만 엔 ÷ 100). 기간 표기를 period에 꼭 적는다(예: "2Q(7~9월)", 누적이면 "2Q 누적(4~9월)").
{qrule}- business·region·insights·headline·narrative 모두 위 기간 기준을 따른다. headline·narrative의 매출·이익은 '수치'의 이번 분기 숫자.
- business: 사업부·제품군별 매출·영업이익(전년 같은 기간과 이번). 자료에 사업부 구분이 여러 개면 매출 비중이 가장 큰 구분 하나.
- region: 지역별 매출(·이익). 자료에 없으면 빈 배열.
  · 두 표 모두 business/region 항목 값은 숫자(억 엔), 모르면 null. period는 두 숫자의 기간.
- insights: 투자 판단에 의미 있는 자료를 네가 골라 3~6개. 우선순위: 생산능력(캐파)·증설 일정·투자액과 가동률 추이, 수주·수주잔고·BB, 용도·고객 산업별 매출, 가격·물량·환율 요인(영업이익 증감 분석),
  핵심 KPI 추이(점포 수·단가·가동률·고객 수·ARR 등), 가이던스 수정 내역, 주주환원. 매출/이익 합계를 다시 보여주는 것은 제외.
  각 항목: title, doc(그 자료가 있는 첨부 번호, 1부터 — 아래 '첨부 목록' 기준), page(그 첨부 PDF의 쪽 번호, 1부터), why(왜 의미 있는지 한 줄), takeaway(숫자로 된 결론 한 줄),
  chart: {{"type": "bar" 또는 "line" 또는 "waterfall", "unit": "", "labels": [], "series": [{{"name": "", "values": [숫자]}}]}}
  (waterfall은 labels=[시작, 요인1, 요인2, ..., 끝], series 하나, 시작·끝은 수준값, 가운데는 증감값)
- '이전 IR 참고 자료'(첨부 목록에 표시)는 이번 분기 이전에 나온 자료다. 이번 분기 자료에 없는 구조 자료 — 생산능력(캐파)과 증설 일정·투자액,
  수주잔고, 제품·용도별 매출 구성, 중기 목표·KPI — 는 여기서 골라 insights에 넣는다(최소 1개·최대 3개, 캐파·증설 자료가 있으면 반드시 포함:
  연도별 생산능력 추이나 증설 일정을 차트로). 같은 내용이 이번 분기 자료에 있으면 이번 것을 쓴다.
  참고 자료에서 고른 항목의 why에는 그 자료가 언제 기준인지 밝히고, 이번 실적과 엮어 지금 어디쯤인지 한 줄로 말한다.
- qa: 설명회 기록·질의응답이 있으면 투자 판단에 중요한 것만 중요한 순서로 3~6개 {{"q": "", "a": "2~3문장", "who": "답변자 직함(있으면)"}}
  우선순위: 가이던스 전제·상향 여지 > 수주·수요 지속성 > 가격·원가·마진 > 일회성/구조적 구분 > 투자·M&A > 자본 정책 > 경영진이 인정한 리스크. 인사말·단순 확인·숫자 반복·홍보성 답변은 뺀다. 답변의 숫자는 그대로, 답을 피한 부분도 그대로.
- headline: 이번 실적 한 줄 결론(40자, 숫자 포함). narrative: 왜 그렇게 됐는지 3~5문장(사업부·지역 숫자, 가격·물량·비용 요인, 가이던스 변화).
- overview: 회사가 무엇을 만들어/팔아 누구에게 파는지 2~3문장. guidance: 가이던스·배당 변경과 이유. risks: 하방 요인. watch: 다음에 확인할 점 2~3개.
- tr: '번역할 말' 각각의 한국어(회사·은행 이름은 통용 한국어 표기).
출력: {{"headline": "", "narrative": "", "overview": "",
 "business": {{"period": "", "items": [{{"name": "", "rev_prev": 0, "rev_cur": 0, "op_prev": null, "op_cur": null}}]}},
 "region": {{"period": "", "items": [{{"name": "", "rev_prev": 0, "rev_cur": 0, "op_prev": null, "op_cur": null}}]}},
 "insights": [], "qa": [], "guidance": "", "risks": "", "watch": [""], "tr": {{}}}}

회사: {name}({code}) · 업종 {sector}
회사 개요(일본어): {profile}
수치(억 엔): {nums}
결산단신 XBRL 세그먼트({seg_basis}, 억 엔): {segs}
번역할 말: {words}
결산단신 본문:
{qual}
{extra}
"""

def fmt(v, d=1):
    return "—" if v is None else f"{v:,.{d}f}"

def pct(a, b):
    return (a / b - 1) * 100 if a is not None and b not in (None, 0) and b > 0 else None

def num(x):
    try:
        if x is None or x == "": return None
        return float(str(x).replace(",", ""))
    except Exception:
        return None

REGION_RE = re.compile(r"日本|国内|アジア|中国|米|欧|北米|南米|海外|欧州|アメリカ|ヨーロッパ|Japan|Asia|America|Europe|일본|국내|해외|아시아|중국|미국|북미|미주|유럽|남미|인도|대만|한국", re.I)
def is_region(names):
    names = [n for n in names if n]
    return bool(names) and sum(bool(REGION_RE.search(n)) for n in names) >= max(1, len(names) * 0.6)

# ───────────────────────── 차트 ─────────────────────────
def contrib_chart(plt, rows, key, title, total_label):
    """구분별 성장 기여(%p) = (구분 이번 − 전년) ÷ 전년 합계 × 100"""
    rows = [s for s in rows if s.get(key) is not None and s.get(key + "_py") is not None]
    if len(rows) < 2: return None
    base = sum(s[key + "_py"] for s in rows)
    if not base or base <= 0: return None
    data = sorted(((s["ko"], (s[key] - s[key + "_py"]) / base * 100) for s in rows), key=lambda x: -x[1])
    tot = sum(v for _, v in data)
    labels = [d[0] for d in data] + [total_label]
    vals = [d[1] for d in data] + [tot]
    fig, ax = plt.subplots(figsize=(8, 0.55 * len(labels) + 1.4))
    cols = [("#2f6fd6" if v >= 0 else "#d9534f") for v in vals[:-1]] + ["#1f2328"]
    y = list(range(len(labels)))[::-1]
    ax.barh(y, vals, color=cols, height=0.6)
    span = (max(vals + [0]) - min(vals + [0])) or 1
    for yi, v in zip(y, vals):
        ax.text(v + (span * 0.02 if v >= 0 else -span * 0.02), yi, f"{v:+.1f}%p", va="center", ha="left" if v >= 0 else "right", fontsize=10, fontweight="bold")
    ax.set_yticks(y); ax.set_yticklabels(labels, fontsize=11)
    ax.axvline(0, color="#999", lw=0.8)
    ax.set_title(title, loc="left", fontsize=12, fontweight="bold")
    for sp in ("top", "right"): ax.spines[sp].set_visible(False)
    lo, hi = min(vals + [0]), max(vals + [0]); pad = span * 0.3
    ax.set_xlim(lo - pad, hi + pad)
    ax.grid(axis="x", color="#eee")
    return b64png(fig)

def mix_chart(plt, rows, title):
    """구분별 매출: 전년 vs 이번(막대) + YoY"""
    rows = [r for r in rows if r.get("rev") is not None]
    if len(rows) < 2: return None
    import numpy as np
    rows = sorted(rows, key=lambda r: -(r["rev"] or 0))
    x = np.arange(len(rows))
    fig, ax = plt.subplots(figsize=(8, 3.6))
    prev = [r.get("rev_py") or 0 for r in rows]; cur = [r["rev"] for r in rows]
    ax.bar(x - 0.2, prev, 0.4, color="#b9b6ae", label="전년")
    ax.bar(x + 0.2, cur, 0.4, color="#2f6fd6", label="이번")
    for i, (a, b) in enumerate(zip(prev, cur)):
        if a: ax.text(i + 0.2, b, f"{(b / a - 1) * 100:+.0f}%", ha="center", va="bottom", fontsize=9, color="#e8743b", fontweight="bold")
    ax.set_xticks(x); ax.set_xticklabels([r["ko"] for r in rows], fontsize=10, rotation=0 if len(rows) <= 5 else 25)
    ax.set_ylabel("억 엔", fontsize=9)
    ax.set_title(title, loc="left", fontsize=12, fontweight="bold")
    ax.legend(frameon=False, fontsize=9)
    for sp in ("top", "right"): ax.spines[sp].set_visible(False)
    ax.grid(axis="y", color="#eee")
    return b64png(fig)

def insight_chart(plt, ch, title):
    try:
        labels = [str(v) for v in ch.get("labels") or []]
        series = [(s.get("name") or "", [num(v) for v in s.get("values") or []]) for s in ch.get("series") or []]
        series = [(n, v) for n, v in series if len(v) == len(labels) and any(x is not None for x in v)]
        if not labels or not series: return None
    except Exception:
        return None
    import numpy as np
    typ = ch.get("type") or "bar"
    fig, ax = plt.subplots(figsize=(8, 3.6))
    x = np.arange(len(labels))
    pal = ["#b9b6ae", "#2f6fd6"] if len(series) == 2 and typ == "bar" else ["#2f6fd6", "#e8743b", "#2e7d32", "#9dbbea", "#b9b6ae", "#6b7280"]
    if typ == "waterfall" and len(series[0][1]) >= 3:
        v = [a or 0 for a in series[0][1]]
        cum = v[0]; bottoms, heights, cols = [0], [v[0]], ["#6b7280"]
        for d in v[1:-1]:
            bottoms.append(cum if d >= 0 else cum + d); heights.append(abs(d)); cols.append("#2f6fd6" if d >= 0 else "#d9534f"); cum += d
        bottoms.append(0); heights.append(v[-1]); cols.append("#1f2328")
        ax.bar(x, heights, bottom=bottoms, color=cols, width=0.6)
        for i, d in enumerate(v):
            lab = f"{d:,.1f}" if i in (0, len(v) - 1) else f"{d:+,.1f}"
            ax.text(i, bottoms[i] + heights[i], lab, ha="center", va="bottom", fontsize=9, fontweight="bold")
    elif typ == "line":
        for k, (n, v) in enumerate(series):
            ax.plot(x, [a if a is not None else np.nan for a in v], marker="o", color=pal[k % len(pal)], label=n, lw=1.8)
    else:
        w = 0.8 / len(series)
        for k, (n, v) in enumerate(series):
            ax.bar(x - 0.4 + w * (k + 0.5), [a or 0 for a in v], w, color=pal[k % len(pal)], label=n)
            if len(series) <= 2:
                for i, a in enumerate(v):
                    if a is not None: ax.text(i - 0.4 + w * (k + 0.5), a, f"{a:,.0f}" if abs(a) >= 100 else f"{a:,.1f}", ha="center", va="bottom", fontsize=8)
    ax.set_xticks(x); ax.set_xticklabels(labels, fontsize=9, rotation=0 if len(labels) <= 6 else 30)
    ax.set_title(f"{title}" + (f" ({ch.get('unit')})" if ch.get("unit") else ""), loc="left", fontsize=12, fontweight="bold")
    if len(series) > 1 or typ == "line": ax.legend(frameon=False, fontsize=9)
    for sp in ("top", "right"): ax.spines[sp].set_visible(False)
    ax.grid(axis="y", color="#eee")
    return b64png(fig)

def page_image(pdf, page):
    """원문 PDF의 한 쪽을 작은 이미지로(접어서 보여줌)"""
    try:
        page = int(page)
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "a.pdf"); open(p, "wb").write(pdf)
            subprocess.run(["pdftoppm", "-f", str(page), "-l", str(page), "-r", "70", "-jpeg", "-jpegopt", "quality=70", p, os.path.join(d, "pg")],
                           capture_output=True, timeout=60)
            f = [x for x in os.listdir(d) if x.startswith("pg")]
            if f: return "data:image/jpeg;base64," + base64.b64encode(open(os.path.join(d, f[0]), "rb").read()).decode()
    except Exception as e:
        E.log("page image error", e)
    return None

def seg_rows_from_ai(block, trm):
    rows = []
    for it in (block or {}).get("items") or []:
        if not isinstance(it, dict) or not it.get("name"): continue
        rows.append({"ko": str(it["name"]), "rev": num(it.get("rev_cur")), "rev_py": num(it.get("rev_prev")),
                     "op": num(it.get("op_cur")), "op_py": num(it.get("op_prev"))})
    return rows

def seg_section(plt, title, rows, period, source, check_total=None):
    """구분별 표 + 매출 비교 + 성장 기여 차트(매출·영업이익). rows 금액은 억 엔"""
    if len(rows) < 2: return "", []
    trs = []
    tot_rev = sum(r["rev"] or 0 for r in rows); tot_py = sum(r["rev_py"] or 0 for r in rows)
    for r in sorted(rows, key=lambda r: -(r["rev"] or 0)):
        m0 = r["op"] / r["rev"] * 100 if r.get("op") is not None and r.get("rev") else None
        m1 = r["op_py"] / r["rev_py"] * 100 if r.get("op_py") is not None and r.get("rev_py") else None
        share = r["rev"] / tot_rev * 100 if r.get("rev") and tot_rev else None
        trs.append(f"<tr><td>{esc(r['ko'])}</td><td>{fmt(r.get('rev_py'))}</td><td>{fmt(r.get('rev'))}</td><td>{fmt(pct(r.get('rev'), r.get('rev_py')))}%</td>"
                   f"<td>{fmt(share, 0)}%</td><td>{fmt(r.get('op'))}</td><td>{fmt(pct(r.get('op'), r.get('op_py')))}%</td><td>{fmt(m1)}% → {fmt(m0)}%</td></tr>")
    note = ""
    if check_total and tot_rev:
        diff = (tot_rev / check_total - 1) * 100
        note = (f" · 합계 {fmt(tot_rev)}억 엔 = 전사 매출과 일치" if abs(diff) < 2 else
                f" · 합계 {fmt(tot_rev)}억 엔, 전사 매출 {fmt(check_total)}억 엔과 {diff:+.0f}% 차이(내부 거래·기타 포함 여부 차이일 수 있음)")
    html_ = (f"<h2>{title}</h2><p class=muted>{esc(period)} · 출처: {esc(source)}{note}</p><div class=scroll><table>"
             "<tr><th>구분</th><th>전년 매출</th><th>이번 매출</th><th>YoY</th><th>비중</th><th>이번 영업이익</th><th>YoY</th><th>영업이익률</th></tr>"
             + "".join(trs) + "</table></div>")
    charts = []
    c0 = mix_chart(plt, rows, f"{title} 매출 (억 엔)")
    if c0: charts.append(("", c0))
    c1 = contrib_chart(plt, rows, "rev", f"{title.split(' ')[0]} 매출 성장 기여 (%p)", "합계 YoY")
    if c1: charts.append(("막대 = (그 구분의 이번 − 전년 매출) ÷ 전년 매출 합계. 어느 구분이 성장을 이끌었는지 보여줌.", c1))
    c2 = contrib_chart(plt, rows, "op", f"{title.split(' ')[0]} 영업이익 성장 기여 (%p)", "합계 YoY")
    if c2: charts.append(("막대 = (그 구분의 이번 − 전년 영업이익) ÷ 전년 영업이익 합계(본사 비용·조정 전).", c2))
    return html_ + "".join(f'<figure><img src="{c}">' + (f'<figcaption>{esc(cap)}</figcaption>' if cap else '') + '</figure>' for cap, c in charts), charts

# ───────────────────────── 연간 가이던스: 전년 대비 성장률(수정이 있으면 수정 전·후를 함께) ─────────────────────────
def guide_section(plt, card, kb):
    """→ HTML(표 + 차트) 또는 "" — 회사 연간 예상이 있는 모든 카드. 금액 백만 엔"""
    r = card["r"]; fc = r.get("fc") or {}; fc_end = (r.get("fc_end") or "")[:7]
    if not fc_end or not any(fc.get(m) for m in ("rev", "op", "ni")): return ""
    new = {m: fc[m] / 1e6 if fc.get(m) is not None else None for m in ("rev", "op", "ni")}
    # 전년 실적: 4분기 결산이면 이번 결산 숫자, 아니면 가부탄 연간 실적에서 예상 회계연도의 1년 전
    if (r.get("q") or 0) == 4 and fc_end > r["end"][:7]:
        base = {m: r[m] / 1e6 if r.get(m) is not None else None for m in ("rev", "op", "ni")}
    else:
        y = next((y for y in kb.get("y") or [] if not y["fc"] and y["end"] == E.ym_add(fc_end, -12)), None)
        base = {m: (y or {}).get(m) for m in ("rev", "op", "ni")}
    # 비교할 예상들: 期初(연초) 예상 → 직전 예상(이번에 수정됐으면) → 현재 예상. 가부탄 수정 이력 + 같은 날 수정 공시
    rv = card.get("rev") or {}
    hist = []
    try: hist = E.kb_fc_hist(card["code"]).get(fc_end) or []
    except Exception: pass
    same = lambda a, b: all(a.get(m) is None or b.get(m) is None or abs(a[m] - b[m]) < 0.5 for m in ("rev", "op", "ni"))
    sets = []
    init = hist[0] if hist and hist[0]["k"] == "초" else (rv.get("init") or None)
    if init and not same(init, new): sets.append((f"기초 예상({init['d'][5:].replace('-', '/')})", init, "#d6d3cc"))
    old = {m: (rv.get(m) or (None, None))[0] for m in ("rev", "op", "ni")}
    if any(v is not None for v in old.values()) and not same(old, new) and not (init and same(init, old)):
        sets.append(("직전 예상", old, "#9aa0a6"))
    revised = bool(sets)
    sets.append(("현재 예상" if revised else "회사 예상", new, E.BLUE))
    lab_fy = E.fy_label_end(r.get("fc_end"))
    names = {"rev": "매출", "op": "영업이익", "ni": "순이익"}
    g = lambda a, b: (a / b - 1) * 100 if a is not None and b not in (None, 0) and b > 0 else None
    ms = [m for m in ("rev", "op", "ni") if new.get(m) is not None]
    if not ms: return ""
    rows = []
    for m in ms:
        cells = "".join((f"<td><b>{E.oku(d.get(m))}</b></td><td><b>{fmt(g(d.get(m), base.get(m)))}%</b></td>" if k == len(sets) - 1 else
                         f"<td>{E.oku(d.get(m))}</td><td>{fmt(g(d.get(m), base.get(m)))}%</td>") for k, (_, d, _) in enumerate(sets))
        rows.append(f"<tr><td>{names[m]}</td><td>{E.oku(base.get(m))}</td>{cells}</tr>")
    img = ""
    if any(g(new.get(m), base.get(m)) is not None for m in ms):
        import numpy as np
        fig, ax = plt.subplots(figsize=(8, 3.3))
        x = np.arange(len(ms)); w = 0.8 / len(sets)
        for k, (nm, d, col) in enumerate(sets):
            vals = [g(d.get(m), base.get(m)) for m in ms]
            bars = ax.bar(x - 0.4 + w * (k + 0.5), [v if v is not None else 0 for v in vals], w * 0.92, color=col, label=nm)
            for b_, v in zip(bars, vals):
                if v is not None:
                    ax.text(b_.get_x() + b_.get_width() / 2, v, f"{v:+.1f}%", ha="center", va="bottom" if v >= 0 else "top",
                            fontsize=9 if len(sets) > 2 else 10, fontweight="bold", color=col if col != "#d6d3cc" else "#8a877f")
        ax.axhline(0, color="#999", lw=0.8)
        ax.set_xticks(x); ax.set_xticklabels([names[m] for m in ms], fontsize=11)
        ax.set_ylabel("전년 실적 대비 %", fontsize=9)
        ax.set_title(f"{lab_fy} 회사 예상의 전년 대비 성장률" + (" — 수정 경과" if revised else ""), loc="left", fontsize=12, fontweight="bold")
        if revised: ax.legend(fontsize=9, frameon=False, loc="upper left", ncol=len(sets))
        for sp in ("top", "right"): ax.spines[sp].set_visible(False)
        lo, hi = ax.get_ylim(); ax.set_ylim(min(lo, 0) * 1.15, max(hi, 0) * 1.25)
        fig.tight_layout(); img = f'<figure><img src="{b64png(fig)}"></figure>'
    head = "<tr><th>항목</th><th>전년 실적</th>" + "".join(f"<th>{esc(nm)}</th><th>YoY</th>" for nm, _, _ in sets) + "</tr>"
    hist_txt = " → ".join(f"{x['d'][2:].replace('-', '/')} {'기초' if x['k'] == '초' else '수정'}" for x in hist if x["k"] in ("초", "수")) if len(hist) > 1 else ""
    note = (f" · 수정 경과: {hist_txt}" if hist_txt else "")
    # 회사 예상 vs 시장 컨센서스(Yahoo 애널리스트 평균, 이번 회계연도)
    cons_html = ""
    try:
        cy = cons_fy(card["code"])
        parts = []
        if cy.get("rev") and new.get("rev"):
            d_ = (new["rev"] * 1e6 / cy["rev"] - 1) * 100
            parts.append(f"매출: 회사 {E.oku(new['rev'])} vs 컨센서스 {E.oku(cy['rev'] / 1e6)} → <b>{d_:+.1f}%</b>" + (f"({cy['rev_n']}명)" if cy.get("rev_n") else ""))
        feps = (r.get("fc") or {}).get("eps")
        if cy.get("eps") and feps:
            d2 = (feps / cy["eps"] - 1) * 100
            parts.append(f"EPS: 회사 {feps:,.1f}엔 vs 컨센서스 {cy['eps']:,.1f}엔 → <b>{d2:+.1f}%</b>" + (f"({cy['eps_n']}명)" if cy.get("eps_n") else ""))
        if parts:
            dd = [float(re.search(r"([+-][\d.]+)%", p_).group(1)) for p_ in parts]
            tone = "회사 예상이 시장 기대보다 보수적" if max(dd) < -5 else "회사 예상이 시장 기대보다 높음" if min(dd) > 5 else "회사 예상이 시장 기대와 비슷"
            card["_cons"] = 1
            cons_html = (f"<div class=card><b>회사 예상 vs 시장 컨센서스</b> — {tone}<br>" + "<br>".join(parts)
                         + "<br><small class=muted>컨센서스: Yahoo Finance 애널리스트 평균(이번 회계연도). 발표 전에 받아 둔 값이 있으면 그것(=발표 전 시장 기대), 없으면 리포트 작성 시점 값</small></div>")
    except Exception as e:
        E.log("consensus fy error", e)
    return (f"<h2>연간 가이던스 ({esc(lab_fy)})</h2><p class=muted>금액 억 엔 · 전년 실적 대비 · 출처: 결산단신(회사 예상), 가부탄(수정 이력·전년 실적){note}</p>"
            f"<div class=scroll><table>{head}{''.join(rows)}</table></div>{img}{cons_html}")

def cons_fy(code):
    """Yahoo 애널리스트 평균 — 이번 회계연도(0y) 매출(엔)·EPS(엔)와 애널리스트 수"""
    out = {}
    c = (E.jp_state().get("cons") or {}).get(code) or {}   # 공시봇이 발표 전에 받아 둔 값(발표 전 기대치)
    if c.get("rev_y0"): out.update(rev=c["rev_y0"], rev_n=c.get("rev_y0_n"))
    if c.get("eps_y0"): out.update(eps=c["eps_y0"], eps_n=c.get("eps_y0_n"))
    if out: return out
    import yfinance as yf
    tk = yf.Ticker(code + ".T")
    re_, ee = tk.revenue_estimate, tk.earnings_estimate
    if re_ is not None and "0y" in re_.index and re_.loc["0y", "avg"] == re_.loc["0y", "avg"] and re_.loc["0y", "avg"]:
        out["rev"] = float(re_.loc["0y", "avg"]); out["rev_n"] = int(re_.loc["0y", "numberOfAnalysts"] or 0)
    if ee is not None and "0y" in ee.index and ee.loc["0y", "avg"] == ee.loc["0y", "avg"] and ee.loc["0y", "avg"]:
        out["eps"] = float(ee.loc["0y", "avg"]); out["eps_n"] = int(ee.loc["0y", "numberOfAnalysts"] or 0)
    return out

# ───────────────────────── 심층 추출 한 번에: 캐파 · 비즈니스 심층 · 주주환원 목표 ─────────────────────────
# 같은 IR 자료를 세 번 읽던 것을 한 번의 AI 호출로 합침(한도·시간 절약). 비즈니스 심층은 분기마다 크게 안 바뀌어 종목별 90일 저장
BIZ_VER = 2   # 2: 밸류체인 위 회사별 담당 단계 지도(vc_map) 추가
def deep_extract(card, name, code, pdfs, pnames):
    import hashlib
    idx = [i for i, (nm, _) in enumerate(pnames) if "결산단신" not in nm and "질의응답" not in nm and "수정 공시" not in nm][:2]
    cpath = os.path.join(E.STATE_DIR, "deepcache.json")
    try: cache = json.load(open(cpath))
    except Exception: cache = {}
    old = cache.get(code) or {}
    reuse_biz = bool(old.get("biz")) and old.get("v", 1) >= BIZ_VER and time.time() - old.get("t", 0) < 90 * 86400   # 형식이 바뀌면(BIZ_VER) 새로 정리
    docs = "; ".join(f"{k}. {pnames[i][0]}" for k, i in enumerate(idx, 1)) or "없음"
    parts = [CAPA_PROMPT.split("출력:")[0].format(name=name, code=code, docs=docs).strip(),
             "- 주주환원 목표: " + POLICY_PROMPT.split("출력:")[0].split("\n- ", 1)[1].format(docs=docs, name=name, code=code).strip()]
    schema = ['"capa": {"found": false} 또는 {"found": true, "title": "", "doc": 첨부 번호, "page": 쪽, "why": "", "takeaway": "", "chart": {"type": "bar", "unit": "", "labels": [], "series": [{"name": "", "values": []}]}}',
              '"policy": [{"metric": "배당성향|총주주환원율|DOE|누진배당|자사주|기타", "target": "", "period": "", "src": ""}]']
    if not reuse_biz:
        parts.append("- 비즈니스 심층(아래 규칙):\n" + BIZ_PROMPT.split("규칙", 1)[1].split("출력:")[0].strip().replace("{{", "{").replace("}}", "}"))
        schema.append('"biz": {"model": {"flow": [""], "mix": [{"name": "", "share": 0}], "mix_src": "", "mix_period": ""}, "value_chain": [], "vc_map": {"headline": "", "caption": "", "stages": [], "cross": [], "players": []}, "competitors": [], "position": "", "strengths": [], "weaknesses": [], "leading": []}')
    prompt = (f"너는 일본 중소형주를 깊게 보는 한국 바이사이드 애널리스트다. {name}({code}, 업종 {E.SECTOR_KO.get(card['sector'], card['sector'])})의 첨부 IR 자료(첨부 목록: {docs})와 "
              "아래 회사 개요·결산단신 본문을 읽고 세 가지를 한 번에 한국어 JSON으로 정리하라. 자료에 없는 숫자는 쓰지 말 것. 일본어를 그대로 쓰지 말 것.\n\n"
              + "\n\n".join(parts) + "\n\n출력(JSON 하나): {" + ", ".join(schema) + "}\n\n"
              + f"회사 개요(일본어): {card.get('profile', '')[:600]}\n결산단신 본문(일부): {(card.get('qual') or '')[:3500]}")
    out = (gemini_pdf(prompt, [pdfs[i] for i in idx], max_tokens=9000) if idx else gemini_json(prompt, timeout=150)) or {}
    if isinstance(out, list): out = out[0] if out and isinstance(out[0], dict) else {}
    if reuse_biz: out["biz"] = old["biz"]; out["_biz_cached"] = old.get("t")
    elif out.get("biz") and not E.ai_used_light():   # 가벼운 모델로 만든 사업 분석은 90일 저장하지 않음
        cache[code] = {"t": time.time(), "biz": out["biz"], "v": BIZ_VER}
        try:
            cache = {k: v for k, v in cache.items() if time.time() - v.get("t", 0) < 120 * 86400}
            os.makedirs(E.STATE_DIR, exist_ok=True); json.dump(cache, open(cpath, "w"), ensure_ascii=False)
        except Exception as e: E.log("deepcache save error", e)
    c = out.get("capa")
    if isinstance(c, dict) and c.get("found") and idx:
        try: c["doc"] = idx[int(c.get("doc") or 1) - 1] + 1
        except Exception: c["doc"] = idx[0] + 1
    return out

# ───────────────────────── 주주환원: 회사 목표 · 주당배당·배당성향 ─────────────────────────
POLICY_PROMPT = """첨부 IR 자료(첨부 목록: {docs})와 아래 결산단신 본문에서 {name}({code})이 밝힌 주주환원 정책·목표만 찾아 한국어 JSON으로.
- 찾을 것: 배당성향 목표, 총주주환원율(총환원성향) 목표, DOE(자기자본배당률) 목표, 누진배당(감배하지 않음) 방침, 자사주 매입 방침·규모, 적용 기간(예: 중기계획 기간 FY3/27~FY3/31).
- 자료에 적힌 것만. 추측 금지. 없으면 {{"items": []}}. 일본어를 그대로 쓰지 말 것.
출력: {{"items": [{{"metric": "배당성향|총주주환원율|DOE|누진배당|자사주|기타", "target": "예: 30% 이상", "period": "적용 기간(없으면 빈 문자열)", "src": "어느 자료(예: 중기 경영계획 2026-05)"}}]}}

결산단신 본문(일부): {qual}
"""

def ret_policy(card, pdfs, pnames):
    """회사가 밝힌 주주환원 목표(가이던스) → [{metric, target, period, src}]"""
    idx = [i for i, (nm, _) in enumerate(pnames) if "질의응답" not in nm and "수정 공시" not in nm and "결산단신" not in nm][:2]
    name = card.get("name_ko") or card["name"]
    p = POLICY_PROMPT.format(name=name, code=card["code"], docs="; ".join(f"{k}. {pnames[i][0]}" for k, i in enumerate(idx, 1)) or "없음",
                             qual=(card.get("qual") or "")[:4000])
    out = (gemini_pdf(p, [pdfs[i] for i in idx], max_tokens=2000) if idx else gemini_json(p, timeout=90)) or {}
    if isinstance(out, list): out = out[0] if out and isinstance(out[0], dict) else {}
    return [x for x in (out.get("items") or []) if isinstance(x, dict) and x.get("target")]

def ret_hist_chart(plt, rows, fc=None, progressive=False):
    """주당배당(막대, 분할 조정) + 배당성향(선) 10년 + 이번 회계연도 회사 예상(빗금 막대)"""
    import numpy as np
    rows = [r for r in rows if r.get("dps") is not None or r.get("payout") is not None]
    if len(rows) < 3: return ""
    lab = [f"FY{int(r['fy'][5:7])}/{r['fy'][2:4]}" for r in rows]
    dps = [r.get("dps") for r in rows]; pay = [r.get("payout") for r in rows]
    fc_on = bool(fc and fc.get("dps") is not None and fc.get("lab") and fc["lab"] not in lab)
    if fc_on: lab.append(fc["lab"] + "\n(예상)"); dps.append(fc["dps"]); pay.append(fc.get("payout"))
    fig, ax = plt.subplots(figsize=(8, 3.8))
    x = np.arange(len(lab))
    cols = ["#9dbbea"] * len(lab)
    if fc_on: cols[-1] = "#ffffff"
    bars = ax.bar(x, [d or 0 for d in dps], 0.6, color=cols, edgecolor=[E.BLUE if (fc_on and i == len(lab) - 1) else "#9dbbea" for i in range(len(lab))],
                  hatch=None, label="주당배당(엔, 분할 조정)")
    if fc_on: bars[-1].set_hatch("///")
    for i, d in enumerate(dps):
        if d is not None: ax.annotate(f"{d:,.0f}" if d >= 10 else f"{d:,.1f}", (i, d), textcoords="offset points", xytext=(0, 3), ha="center", fontsize=7.5, color="#374151")
    ax.set_ylabel("엔", fontsize=9); ax.set_xticks(x); ax.set_xticklabels(lab, fontsize=8)
    ax2 = ax.twinx()
    xp = [i for i, v in enumerate(pay) if v is not None]
    if xp:
        ax2.plot(xp, [min(pay[i], 200) for i in xp], color=E.ORANGE, marker="o", ms=4, label="배당성향(%)")
        i = xp[-1]; ax2.annotate(f"{pay[i]:.0f}%", (i, min(pay[i], 200)), textcoords="offset points", xytext=(0, 7), ha="center", fontsize=8, color=E.ORANGE)
        top = max(min(max(pay[i] for i in xp), 200), 20)
        ax2.set_ylim(-top * 1.15, top * 1.25)   # 배당성향 선은 위쪽, 주당배당 막대는 아래쪽에 → 겹치지 않게
        ax2.set_yticks([v for v in ax2.get_yticks() if 0 <= v <= top * 1.2])
    ax2.set_ylabel("%", fontsize=9)
    h1, l1 = ax.get_legend_handles_labels(); h2, l2 = ax2.get_legend_handles_labels()
    ax.legend(h1 + h2, l1 + l2, fontsize=8, frameon=False, loc="upper left", ncol=3, bbox_to_anchor=(0, 1.02))
    ax.set_title("주당 배당금과 배당성향 추이" + (" — 누진배당 방침" if progressive else ""), loc="left", fontsize=12, fontweight="bold")
    for sp in ("top",): ax.spines[sp].set_visible(False); ax2.spines[sp].set_visible(False)
    lo, hi = ax.get_ylim(); ax.set_ylim(0, hi * 1.9)
    fig.tight_layout()
    return f'<figure><img src="{b64png(fig)}"></figure>'

def ret_section(card, kb, att, plt=None, policy=None, div_html=""):
    """주주환원: 회사 목표 → 주당배당·배당성향 10년(EDINET)+이번 회계연도 예상 → IR 자료의 배당 자료 → 이번 회계연도 표"""
    ret = card.get("ret")
    if not ret:   # 예전 카드: 결산단신 XBRL 현금흐름표로 다시 계산
        r = dict(card["r"])
        cf = att.get("cf") or {}
        pick = lambda rx: next((v for k, v in cf.items() if rx.fullmatch(k) and "NonControlling" not in k), None)
        r["cf"] = {"div": pick(E.CF_DIV), "buy": pick(E.CF_BUY)}
        ret = E.shareholder_return(card["code"], r, kb, card["d"])
    ret = ret or {}
    rows = []
    fy_lab = lambda e: f"FY{int(e[5:7])}/{e[2:4]}" if e else ""
    if ret.get("payout_fc") is not None:
        rows.append(f"<tr><td>배당성향(회사 예상, {fy_lab(ret.get('fy_end'))})</td><td><b>{ret['payout_fc']:.1f}%</b></td><td>주당배당 {fmt(ret.get('dps_fc'))}엔 ÷ 주당순이익 {fmt(ret.get('eps_fc'))}엔</td></tr>")
    if ret.get("payout_last") is not None:
        rows.append(f"<tr><td>배당성향(최근 실적, {fy_lab(ret.get('last_end'))})</td><td>{ret['payout_last']:.1f}%</td><td>주당배당 {fmt(ret.get('dps_last'))}엔</td></tr>")
    if ret.get("ytd_div") is not None or ret.get("ytd_buy") is not None:
        rows.append(f"<tr><td>올해 누적 지급액</td><td>배당 {E.oku(ret.get('ytd_div'))}</td><td>자사주 취득 {E.oku(ret.get('ytd_buy'))} (결산단신 현금흐름표)</td></tr>")
    buys = ret.get("buys") or []
    if buys:
        rows.append("<tr><td>자사주 매입 결정(최근 1년)</td><td>" + f"{len(buys)}건</td><td>" + " · ".join(
            f"{x['d'][2:4]}/{x['d'][4:6]}/{x['d'][6:]} {('상한 ' + E.oku(x['amt'])) if x.get('amt') else '(금액 확인 못 함)'}" for x in buys) + "</td></tr>")
    # 회사가 밝힌 주주환원 목표(가이던스)
    pol_html, progressive = "", False
    try:
        pol = [x for x in (policy or []) if isinstance(x, dict) and x.get("target")]
        progressive = any("누진" in f"{x.get('metric')}{x.get('target')}" for x in pol) or "누진" in (div_html or "")
        if pol:
            pol_html = ("<div class=card><b>회사가 밝힌 주주환원 목표</b><ul>" + "".join(
                f"<li><b>{esc(x.get('metric'))}</b> {esc(x.get('target'))}" + (f" · {esc(x['period'])}" if x.get("period") else "")
                + (f" <small class=muted>({esc(x.get('src'))})</small>" if x.get("src") else "") + "</li>" for x in pol) + "</ul></div>")
        else:
            pol_html = "<p class=muted>회사가 수치로 밝힌 주주환원 목표(배당성향·DOE·누진배당 등)는 이번 자료에서 찾지 못했습니다.</p>"
    except Exception as e:
        E.log("ret policy error", e)
    # 10년 추이(EDINET 유가증권보고서) + 이번 회계연도 회사 예상
    hist_html = ""
    try:
        import earn_edinet
        hist, note = earn_edinet.history(card["code"], card.get("fem") or 3)
        E.log("주주환원 10년", card["code"], f"{len(hist)}개년", note or "")
        card["_edinet_n"] = len(hist)
        if hist:
            fc = {"lab": fy_lab(ret.get("fy_end")), "dps": ret.get("dps_fc"), "payout": ret.get("payout_fc")} if ret.get("dps_fc") is not None else None
            ds = [h.get("dps") for h in hist if h.get("dps") is not None]
            cut = [hist[i]["fy"] for i in range(1, len(hist)) if hist[i].get("dps") is not None and hist[i - 1].get("dps") is not None and hist[i]["dps"] < hist[i - 1]["dps"] * 0.995]
            summ = ""
            if len(ds) >= 3:
                n = len(ds) - 1
                cagr = ((ds[-1] / ds[0]) ** (1 / n) - 1) * 100 if ds[0] and ds[0] > 0 else None
                summ = (f"<p><b>{len(ds)}개년 주당배당 {fmt(ds[0])}엔 → {fmt(ds[-1])}엔</b>" + (f"(연평균 {cagr:+.1f}%)" if cagr is not None else "")
                        + (" · 이 기간 감배 없음" if not cut else " · 감배 " + ", ".join(f"FY{int(c[5:7])}/{c[2:4]}" for c in cut))
                        + (f" · 이번 회계연도 회사 예상 {fmt(fc['dps'])}엔" if fc else "") + "</p>")
            trs = "".join(f"<tr><td>FY{int(h['fy'][5:7])}/{h['fy'][2:4]}</td><td><b>{fmt(h.get('dps'))}엔</b>{f"<br><small>(당시 {fmt(h.get('dps_raw'))}엔, 1:{h['split']:g} 분할 조정)</small>" if (h.get('split') or 1) > 1 else ""}</td>"
                          f"<td>{fmt(h.get('payout'))}%</td><td>{E.oku(h.get('div'))}</td><td>{E.oku(h.get('buy'))}</td><td>{E.oku(h.get('ni'))}</td></tr>" for h in hist)
            if fc:
                trs += f"<tr class=cur><td>{fc['lab']} 회사 예상</td><td><b>{fmt(fc['dps'])}엔</b></td><td>{fmt(fc.get('payout'))}%</td><td></td><td></td><td></td></tr>"
            hist_html = (summ + (ret_hist_chart(plt, hist, fc, progressive) if plt else "") +
                         "<div class=scroll><table><tr><th>회계연도</th><th>주당배당</th><th>배당성향</th><th>배당금 지급</th><th>자사주 매입</th><th>순이익</th></tr>"
                         + trs + "</table></div><p class=muted>출처: EDINET 유가증권보고서(주요 경영지표 추이·현금흐름표), 이번 회계연도 예상은 결산단신. 배당금·자사주는 그해 실제 지급액(현금 기준). "
                         "주당배당은 발행주식수 변화로 주식분할을 찾아 지금 기준으로 조정(조정한 해는 당시 숫자 병기)."
                         + (f" {esc(note)}" if note else "") + "</p>")
        elif note:
            hist_html = f"<p class=muted>10년 추이: {esc(note)}</p>"
    except Exception as e:
        E.log("ret history error", e)
    if div_html:
        div_html = "<h3>회사 IR 자료의 배당 자료</h3>" + div_html
    if not rows and not hist_html and not pol_html and not div_html: return ""
    return ("<h2>주주환원</h2>" + pol_html + "<h3>주당 배당금·배당성향 추이</h3>" + (hist_html or "<p class=muted>10년 추이 자료 없음</p>") + div_html
            + "<h3>이번 회계연도</h3><p class=muted>배당성향 = 주당배당 ÷ 주당순이익(가부탄 연간 실적·예상, 분할 반영) · "
            "올해 누적은 결산단신 현금흐름표의 실제 지급액 · 자사주 매입 금액은 결정 공시의 취득 총액 상한(실제 매입액과 다를 수 있음)</p>"
            + ("<div class=scroll><table><tr><th>항목</th><th>값</th><th>계산</th></tr>" + "".join(rows) + "</table></div>" if rows else ""))

# ───────────────────────── 비즈니스 심층: 사업 구조·밸류체인·경쟁 구도·선행지표 ─────────────────────────
BIZ_PROMPT = """너는 일본 중소형주를 깊게 보는 한국 바이사이드 애널리스트다. {name}({code}, 업종 {sector})의 사업을 처음 공부하는 운용역이 구조를 한 번에 이해하도록
첨부 IR 자료(첨부 목록: {docs})와 아래 회사 개요·결산단신 본문을 읽고 한국어 JSON으로 정리하라.
규칙
- 각 항목에 src를 단다: "자료"(첨부·본문에 근거) 또는 "일반"(자료에 없고 일반적으로 알려진 업계 지식). 숫자는 자료에 있는 것만 쓰고, 일반 지식에는 숫자를 쓰지 않는다.
- 확실하지 않은 경쟁사·고객 이름은 넣지 않는다. 일본어를 그대로 쓰지 말 것(회사명은 한국어 통용 표기, 일본 상장사는 4자리 종목코드).
- model.flow: 돈이 도는 구조를 화살표 한 줄씩 3~5줄(예: "원료 조달 → 정제·가공 → 반도체 웨이퍼 연마재로 판매 → 고객: 웨이퍼·소자 업체").
- model.mix: 사업·제품·용도별 매출 비중(자료에 있을 때만, share는 % 숫자).
- value_chain: 이 회사가 속한 산업의 단계 4~6개. 각 {{"stage": 단계, "what": 하는 일, "players": [대표 기업 2~4개], "role": "이 회사의 위치(해당 없으면 빈 문자열)", "src": ""}}.
- vc_map: 밸류체인 위에 '회사별로 어느 단계를 하는지'를 겹쳐 그리는 지도(경쟁 구도를 한 장에). 
  {{"headline": "이 회사가 밸류체인의 어디에 집중하고 무엇을 안 하는지 한 문장(예: \"A사는 '전략 → IT 구상'의 앞단에 집중 — 구축·운영은 하지 않음\")",
   "caption": "지도 설명 한 줄(예: 기업 변혁 프로젝트의 일 흐름과 회사별 담당 단계)",
   "stages": [{{"name": "value_chain 단계 이름(짧게)", "sub": "8자 안팎 부연"}}],
   "cross": [{{"name": "여러 단계에 걸친 수요·테마 이름(예: AI 도입)", "from": 0, "to": 4, "text": "예: 전 단계에 걸친 수요"}}],
   "players": [{{"name": "회사명", "country": "일본|미국|한국|중국|유럽 등", "spans": [{{"from": 시작 단계 번호(0부터), "to": 끝 단계 번호(포함), "label": "그 구간의 사업 이름(예: 컨설팅 100%(단일 부문))", "main": true면 주력·본업, false면 일부·부수 사업}}], "src": ""}}]}}
  stages는 value_chain과 같은 순서·개수. players는 첫 번째가 이 회사, 이어서 경쟁·비교 기업 5~8개를 나라별로 묶어 정렬(일본 → 미국 → 한국 → 기타).
  한 회사가 떨어진 두 구간을 하면 spans를 2개로. cross는 없으면 빈 배열. 확실하지 않은 기업·구간은 넣지 말 것.
- competitors: 주요 경쟁사 3~6개. {{"name": "", "code": "일본 상장사면 4자리, 아니면 null", "country": "", "overlap": "겹치는 제품·시장", "edge": "이 회사 대비 강점·약점 한 줄", "src": ""}}.
- position: 시장 내 위치·점유율·진입장벽(자료 근거 우선) 2~3문장. strengths/weaknesses: 각 2~4개 짧은 문장(src 포함 객체 {{"t": "", "src": ""}}).
- leading: 실적을 앞서 보여주는 선행지표 2~3개 {{"name": "", "why": "왜 실적에 앞서는지", "where": "어디서 확인하는지(월간 공시·업계 통계 등)"}}.
출력: {{"model": {{"flow": [""], "mix": [{{"name": "", "share": 0}}], "mix_src": "", "mix_period": ""}}, "value_chain": [], "vc_map": {{}}, "competitors": [],
 "position": "", "strengths": [], "weaknesses": [], "leading": []}}

회사 개요(일본어): {profile}
결산단신 본문(일부): {qual}
"""

def vc_map_html(m, name, code):
    """밸류체인 지도: 위에 단계 상자(화살표), 그 아래 회사별로 담당 단계 구간을 막대로(주력=붉은 테두리, 일부=회색). 나라가 바뀌면 점선"""
    if not isinstance(m, dict): return ""
    st = [s for s in (m.get("stages") or []) if isinstance(s, dict) and s.get("name")][:7]
    n = len(st)
    if n < 3: return ""
    def rng(x):
        try:
            a_, b_ = int(x.get("from")), int(x.get("to"))
            a_, b_ = max(0, min(a_, n - 1)), max(0, min(b_, n - 1))
            return (min(a_, b_), max(a_, b_))
        except Exception: return None
    pl = []
    for p_ in (m.get("players") or [])[:10]:
        if not isinstance(p_, dict) or not p_.get("name"): continue
        sp = [(rng(s), s) for s in (p_.get("spans") or []) if isinstance(s, dict)]
        sp = [(r_, s) for r_, s in sp if r_]
        if sp: pl.append((p_, sp))
    if len(pl) < 2: return ""
    cols = f"grid-template-columns:118px repeat({n},minmax(92px,1fr))"
    G = [f"<div class=vmg style='{cols}'>", "<div class=vml>일의 흐름</div>"]
    for i, s in enumerate(st):
        G.append(f"<div class='vst{' la' if i == n - 1 else ''}' style='grid-column:{i + 2}'><b>{esc(s['name'])}</b>"
                 + (f"<small>{esc(s.get('sub'))}</small>" if s.get("sub") else "") + "</div>")
    for c in (m.get("cross") or [])[:2]:
        r_ = rng(c) if isinstance(c, dict) else None
        if not r_ or not c.get("name"): continue
        G.append(f"<div class=vml>{esc(c['name'])}</div><div class=vcx style='grid-column:{r_[0] + 2}/{r_[1] + 3}'>{esc(c.get('text') or '')}</div>")
    prev = None
    for k, (p_, sp) in enumerate(pl):
        ctry = str(p_.get("country") or "")
        if prev is not None and ctry != prev: G.append("<div class=vsep></div>")
        prev = ctry
        me = k == 0
        G.append(f"<div class='vml vco{' me' if me else ''}'><b>{esc(p_['name'])}</b><small>{esc(ctry)}"
                 + (" · 일반 지식" if str(p_.get("src", "")).startswith("일반") else "") + "</small></div>")
        for (a_, b_), s in sorted(sp, key=lambda z: z[0][0]):
            main = s.get("main") is not False and str(s.get("main")).lower() != "false"
            G.append(f"<div class='vbar{' mn' if main else ' sub'}{' me' if me else ''}' style='grid-column:{a_ + 2}/{b_ + 3}'>{esc(s.get('label') or '')}</div>")
        G.append("<div class=vrowend></div>")
    G.append("</div>")
    head = (f"<p class=vmh><b>{esc(m.get('headline'))}</b></p>" if m.get("headline") else "") + (f"<p class=muted>{esc(m.get('caption'))}</p>" if m.get("caption") else "")
    return (head + "<div class=scroll>" + "".join(G) + "</div>"
            "<p class=muted>붉은 테두리 = 주력·본업 구간 · 회색 = 일부·부수 사업 · 맨 위 굵은 이름이 이 회사 · 점선 = 나라 구분 · Gemini 정리(자료+업계 일반 지식, 검증 필요)</p>")

def biz_section(plt, card, name, code, b, cached_t=None):
    """심층 추출 결과(b) → HTML 또는 """""
    if not isinstance(b, dict) or not b: return ""
    tag = lambda x: ' <span class=tag>일반 지식</span>' if str((x or {}).get("src", "")).startswith("일반") else ""
    H = ["<h2>비즈니스 심층 — 사업 구조 · 밸류체인 · 경쟁 구도</h2>",
         "<p class=muted>IR 자료·결산단신을 바탕으로 Gemini가 정리. <span class=tag>일반 지식</span> 표시는 자료에 없고 업계에 일반적으로 알려진 내용(검증 필요)."
         + (f" 사업 구조는 분기마다 크게 바뀌지 않아 {time.strftime('%Y-%m-%d', time.localtime(cached_t))}에 정리한 내용을 다시 씀(90일마다 새로 정리)." if cached_t else "") + "</p>"]
    m = b.get("model") or {}
    flow = [str(x) for x in (m.get("flow") or []) if str(x).strip()][:5]
    if flow:
        def chain(line):   # "A → B → C" 한 줄을 상자 사슬로
            steps = [t.strip() for t in re.split(r"\s*(?:→|->|⇒)\s*", line) if t.strip()]
            if len(steps) < 2: return f"<div class=flow>{esc(line)}</div>"
            return "<div class=fchain>" + "<span class=farr>→</span>".join(f"<span class=fstep>{esc(t)}</span>" for t in steps) + "</div>"
        H.append("<h3>돈이 도는 구조</h3><div class=card>" + "".join(chain(x) for x in flow) + "</div>")
    mix = [x for x in (m.get("mix") or []) if isinstance(x, dict) and num(x.get("share"))]
    if len(mix) >= 2:
        try:
            fig, ax = plt.subplots(figsize=(8, 0.5 + 0.42 * len(mix)))
            mix = sorted(mix, key=lambda x: num(x["share"]))
            ax.barh([str(x.get("name"))[:22] for x in mix], [num(x["share"]) for x in mix], color=E.BLUE)
            for i, x in enumerate(mix): ax.text(num(x["share"]), i, f" {num(x['share']):.0f}%", va="center", fontsize=9)
            ax.set_title("매출 구성" + (f" ({m.get('mix_period')})" if m.get("mix_period") else ""), loc="left", fontsize=12, fontweight="bold")
            for sp in ("top", "right"): ax.spines[sp].set_visible(False)
            fig.tight_layout(); H.append(f'<figure><img src="{b64png(fig)}"></figure>')
        except Exception as e: E.log("mix chart error", e)
    vc = [x for x in (b.get("value_chain") or []) if isinstance(x, dict) and x.get("stage")]
    vmap = ""
    try: vmap = vc_map_html(b.get("vc_map"), name, code)
    except Exception as e: E.log("vc map error", e)
    if vmap: H.append("<h3>밸류체인과 회사별 담당 단계</h3>" + vmap)
    if vc:
        # 단계별 설명: 단계 상자를 화살표로 이어 붙이고, 이 회사가 있는 단계는 파란 테두리·'여기' 표시(휴대폰에서는 세로로 쌓임)
        boxes = []
        for k, x in enumerate(vc[:6], 1):
            me = bool(str(x.get("role") or "").strip())
            boxes.append(f"<div class='vc{' me' if me else ''}'><div class=vn>{k}</div><div class=vs>{esc(x['stage'])}</div>"
                         f"<div class=vw>{esc(x.get('what'))}</div>"
                         + (f"<div class=vp>{esc(' · '.join(map(str, (x.get('players') or [])[:4])))}</div>" if x.get("players") else "")
                         + (f"<div class=vr>📍 {esc(code)} 여기: {esc(x.get('role'))}{tag(x)}</div>" if me else "") + "</div>")
        body = ("<div class=vchain>" + "<div class=varr>→</div>".join(boxes) + "</div>"
                "<p class=muted>왼쪽(상류)에서 오른쪽(하류) 순서 · 파란 상자 = 이 회사가 있는 단계 · 상자 안 회사는 각 단계의 대표 기업</p>")
        H.append(("<details><summary><b>단계별 설명·대표 기업</b></summary>" + body + "</details>") if vmap else ("<h3>밸류체인</h3>" + body))
    comp = [x for x in (b.get("competitors") or []) if isinstance(x, dict) and x.get("name")][:6]
    if comp:
        rows = []
        for x in comp:
            c = re.sub(r"\D", "", str(x.get("code") or ""))
            nums_ = ""
            if len(c) == 4 and c != code:
                try:   # 일본 상장 경쟁사: 가부탄 3개월 실적으로 최근 분기 성장률·이익률·시총
                    kq = E.kb_finance(c)["q"]; qq = kq[-1]; pq = next((z for z in kq if z["end"] == E.ym_add(qq["end"], -12)), {})
                    ry, _ = E.yoy(qq.get("rev"), pq.get("rev")); oy, ost = E.yoy(qq.get("op"), pq.get("op"))
                    cap, _ = E.kb_profile(c)
                    opm = qq["op"] / qq["rev"] * 100 if qq.get("op") is not None and qq.get("rev") else None
                    nums_ = (f"{qq['end'][2:].replace('-', '/')} 매출 {fmt(ry)}% · 영익 {E.yoy_txt(oy, ost)} · 영업이익률 {fmt(opm)}%"
                             + (f" · 시총 {cap:,.0f}억 엔" if cap else ""))
                except Exception as e:
                    E.log("peer error", c, e)
            rows.append(f"<tr><td>{esc(x['name'])}{f' ({c})' if len(c) == 4 else ''}<br><small>{esc(x.get('country'))}</small></td>"
                        f"<td style='white-space:normal'>{esc(x.get('overlap'))}</td><td style='white-space:normal'>{esc(x.get('edge'))}{tag(x)}</td>"
                        f"<td style='white-space:normal'><small>{esc(nums_) or '—'}</small></td></tr>")
        H.append("<h3>경쟁 구도</h3>" + (f"<div class=card>{esc(b.get('position'))}</div>" if b.get("position") else "")
                 + "<div class=scroll><table><tr><th>경쟁사</th><th>겹치는 영역</th><th>비교</th><th>최근 분기(일본 상장사, 가부탄)</th></tr>" + "".join(rows) + "</table></div>")
    sw = [(k, [x for x in (b.get(k) or []) if isinstance(x, dict) and x.get("t")]) for k in ("strengths", "weaknesses")]
    if any(v for _, v in sw):
        H.append("<h3>강점 · 약점</h3><div class=card>" + "".join(
            f"<p><b>{'강점' if k == 'strengths' else '약점'}</b></p><ul>" + "".join(f"<li>{esc(x['t'])}{tag(x)}</li>" for x in v) + "</ul>" for k, v in sw if v) + "</div>")
    ld = [x for x in (b.get("leading") or []) if isinstance(x, dict) and x.get("name")][:3]
    if ld:
        H.append("<h3>선행지표 — 다음 실적을 먼저 보여주는 것</h3><div class=card><ul>" + "".join(
            f"<li><b>{esc(x['name'])}</b> — {esc(x.get('why'))}<br><small class=muted>확인: {esc(x.get('where'))}</small></li>" for x in ld) + "</ul></div>")
    return "".join(H) if len(H) > 2 else ""

# ───────────────────────── 캐파(생산능력)·증설 자료 따로 찾기 ─────────────────────────
DIV_RE = re.compile(r"배당|주주환원|자사주|DOE|환원")
CAPA_RE = re.compile(r"캐파|생산능력|생산 능력|증설|설비 능력|가동률|라인 증설|공장 신설|생산 체제")
CAPA_PROMPT = """첨부 PDF는 일본 상장사 {name}({code})의 IR 자료다(첨부 목록: {docs}). 생산능력(캐파)·증설과 관련된 숫자만 찾아 한국어 JSON으로.
- 찾을 것: 연도·시점별 생산능력(톤/년, 개/월, 배수, 지수 등)과 그 추이, 증설·신규 공장의 완공·가동 시기와 투자액, 가동률.
- 자료에 적힌 숫자만. 추측 금지. 일본어를 그대로 쓰지 말 것. 관련 내용이 없으면 {{"found": false}}.
출력: {{"found": true, "title": "한국어 제목(예: 초고순도 콜로이달 실리카 생산능력 추이)", "doc": 첨부 번호, "page": 그 PDF 쪽 번호,
 "why": "왜 중요한지 한 줄", "takeaway": "숫자로 된 결론 한 줄(예: 2025년 대비 2029년 생산능력 1.8배)",
 "chart": {{"type": "bar", "unit": "단위", "labels": ["시점"], "series": [{{"name": "생산능력", "values": [숫자]}}]}}}}
"""

# ───────────────────────── 분기 단독(3개월) 계산 ─────────────────────────
PREV_PROMPT = """첨부 PDF는 일본 상장사 {name}({code})의 직전 분기 결산단신이다. PDF 표에 적힌 숫자만 읽어 JSON으로 출력하라. 추측 금지, 없으면 null.
단위는 모두 백만 엔으로 바꿔라(표가 천 엔이면 ÷1000, 엔이면 ÷1,000,000). 손실·마이너스는 음수.
1) pl: 연결 손익계산서(연결이 없으면 개별)의 당기 누적(cur)과 전기 누적(prev).
   항목: 매출(売上高·売上収益·営業収益), 매출원가, 매출총이익, 판관비(販売費及び一般管理費), 영업이익, 영업외수익, 영업외비용, 경상이익, 특별이익, 특별손실, 세전이익, 지배주주 순이익
2) seg: 세그먼트 정보(セグメント情報)의 보고 세그먼트별 외부 고객 매출(外部顧客への売上高)과 세그먼트 이익(セグメント利益), 당기 누적·전기 누적.
   아래 이름 목록 순서대로, name은 목록의 이름 그대로: {names}
출력: {{"pl": {{"매출": [cur, prev], "영업이익": [cur, prev]}}, "seg": [{{"name": "", "rev": 0, "rev_py": 0, "op": 0, "op_py": 0}}]}}
"""

def prev_cum(card, att):
    """직전 분기 누적(엔) → ({"pl": {항목: [cur, py]}, "seg": {key 또는 이름: [rev, rev_py, op, op_py]}}, 출처) 또는 (None, "")"""
    import earn_ir
    r = card["r"]; prev_end = E.ym_add(r["end"][:7], -3)
    c = E.cum_load(card["code"]).get(prev_end)
    if c:
        seg = {}
        for k, n, *v in c.get("seg") or []: seg[k] = v; seg.setdefault(n, v)
        return {"pl": c.get("pl") or {}, "seg": seg}, "직전 분기 결산단신 XBRL"
    pdf, url = earn_ir.prev_tanshin_pdf(card)
    if not pdf: return None, ""
    names = [x["name"] for x in att["seg"]]
    out = gemini_pdf(PREV_PROMPT.format(name=card["name"], code=card["code"], names=json.dumps(names, ensure_ascii=False)), [pdf], max_tokens=4000) or {}
    m = lambda v: num(v) * 1e6 if num(v) is not None else None
    pl = {k: [m((v or [None, None])[0]), m((v or [None, None])[1])] for k, v in (out.get("pl") or {}).items() if isinstance(v, list) and len(v) >= 2}
    seg = {}
    for x in out.get("seg") or []:
        if isinstance(x, dict) and x.get("name"): seg[nfkc(str(x["name"]))] = [m(x.get("rev")), m(x.get("rev_py")), m(x.get("op")), m(x.get("op_py"))]
    return {"pl": pl, "seg": seg}, "직전 분기 결산단신 PDF에서 AI 추출 · 3개월 매출로 검산"

def quarterize(card, att, q, py):
    """이번 누적 − 직전 분기 누적 → 3개월 단독 손익·세그먼트. → (pl{항목:[이번, 전년]}, segs[], 출처) / 계산 못 하면 (None, None, 이유)"""
    r = card["r"]; qn = r.get("q") or 1
    cur_pl = pl_compact(att["pl"])
    if qn == 1: return cur_pl, att["seg"], "1분기라 누적 = 3개월"
    prev, src = prev_cum(card, att)
    if not prev: return None, None, "직전 분기 결산단신을 못 찾음"
    sub = lambda a, b: a - b if a is not None and b is not None else None
    qpl = {k: [sub(v[0], (prev["pl"].get(k) or [None, None])[0]), sub(v[1], (prev["pl"].get(k) or [None, None])[1])] for k, v in cur_pl.items()}
    # 검산: 계산한 3개월 매출이 가부탄 3개월 매출과 맞아야 씀(단위·기간 오독 방지)
    def close(a, b, tol): return a is not None and b not in (None, 0) and abs(a / b - 1) <= tol
    rv = (qpl.get("매출") or [None, None])
    ok_pl = close((rv[0] or 0) / 1e6 if rv[0] is not None else None, q.get("rev"), 0.015) and \
        (py.get("rev") is None or close((rv[1] or 0) / 1e6 if rv[1] is not None else None, py.get("rev"), 0.015))
    if not ok_pl: qpl = None
    qsegs = []
    for x in att["seg"]:
        pv = prev["seg"].get(x["key"]) or prev["seg"].get(x["name"])
        if not pv: qsegs = None; break
        qsegs.append(dict(x, rev=sub(x["rev"], pv[0]), rev_py=sub(x["rev_py"], pv[1]), op=sub(x["op"], pv[2]), op_py=sub(x["op_py"], pv[3])))
    if qsegs:
        tot = sum(x["rev"] or 0 for x in qsegs) / 1e6
        # 세그먼트 합계가 내부 거래 포함 등으로 전사 매출과 원래 다를 수 있음 → 누적에서의 비율과 3개월 비율이 같아야 함
        cum_tot = sum(x["rev"] or 0 for x in att["seg"]); base = (cum_tot / r["rev"]) if r.get("rev") else 1.0
        if any((x["rev"] or 0) < 0 for x in qsegs) or not q.get("rev") or abs(tot / q["rev"] - base) > (0.06 if ok_pl else 0.03): qsegs = None
    if qpl is None and qsegs is None: return None, None, "직전 분기 숫자가 3개월 매출과 맞지 않음"
    return qpl, qsegs, src

# ───────────────────────── 본체 ─────────────────────────
def build(card):
    import earn_ir
    code = card["code"]; fem = card["fem"]
    name = card.get("name_ko") or card["name"]
    kb = E.kb_finance(code)
    plt = E._plt()
    series = card["series"]
    full = {q["end"]: q for q in kb["q"]}; full.update({q["end"]: q for q in series})
    allq = [full[k] for k in sorted(full)]
    s = allq[-8:]
    r = card["r"]; fc = r.get("fc") or {}

    kd = {}
    try:   # 오래된 결산(TDnet에서 내려감)·요청 카드: 가부탄의 TDnet 사본으로 보충 + 발표 이후 업적 예상 수정
        import earn_ir
        kd = earn_ir.kabutan_docs(card)
        for k in ("pdf", "mat"):
            if kd.get(k) and not card.get(k): card[k] = kd[k]
    except Exception as e: E.log("report kabutan docs error", e)
    per = earn_ir.card_period(card)
    # 1) 결산단신 XBRL 첨부(손익 상세·세그먼트)
    att = {"pl": {}, "seg": []}
    if not card.get("xbrl") and card.get("pdf"):  # 예전 카드: TDnet 파일 이름 규칙(1401…pdf ↔ 0812…zip)으로 XBRL 주소 복원
        card["xbrl"] = re.sub(r"/1401(\d+)\.pdf$", r"/0812\1.zip", card["pdf"])
    if card.get("xbrl"):
        try: att = attachments(card["xbrl"])
        except Exception as e: E.log("attachment error", e)
    segs = att["seg"]
    # 1-2) 분기 기준: 결산단신 손익·세그먼트(회계연도 누적)를 이번 3개월로(이번 누적 − 직전 분기 누적)
    q = s[-1]; py = full.get(E.ym_add(q["end"], -12)) or {}
    qpl, qsegs, qsrc = None, None, ""
    if att["pl"] or segs:
        try: qpl, qsegs, qsrc = quarterize(card, att, q, py)
        except Exception as e: E.log("quarterize error", e); qsrc = "계산 오류"
        E.log("분기 단독 계산", code, "손익" if qpl else "-", "세그먼트" if qsegs else "-", qsrc)
    seg_q = qsegs is not None
    if seg_q: segs = qsegs
    # 2) 대주주
    holders = []
    try:
        hs = E.mget(f"https://s.kabutan.jp/stocks/{code}/stockholders/")
        tb = [t for t in re.findall(r"<table.*?</table>", hs, re.S) if "株主名" in t]
        if tb:
            for tr in re.findall(r"<tr.*?</tr>", tb[0], re.S)[1:12]:
                c = [E.text_of(x) for x in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", tr, re.S)]
                if len(c) >= 2 and c[0]:
                    m = re.search(r"([\d.]+)\s*%", c[1])
                    holders.append((nfkc(c[0]), m.group(1) if m else c[1]))
    except Exception as e:
        E.log("holders error", e)
    # 3) 설명 자료·기록 모으기(TDnet → 회사 IR 홈페이지 → 로그미)
    mat_pdf, mat_url, mat_src = None, card.get("mat"), card.get("mat_src") or "TDnet"
    if not mat_url:
        try:
            for t, u, d in earn_ir.ir_site_docs(code, card["d"], limit=4, pat=earn_ir.MAT_LINK, period=per):
                if earn_ir.QA_LINK.search(t): continue
                kind, body = earn_ir.fetch_doc(u, pat=earn_ir.MAT_LINK, period=per)
                if kind == "pdf" and len(body) < 12_000_000: mat_pdf, mat_url, mat_src = body, u, "회사 IR 홈페이지"; break
        except Exception as e:
            E.log("ir site material error", e)
    elif mat_url:
        try: mat_pdf = earn_ir.get_pdf(mat_url, period=per)   # 회사 IR 홈페이지 주소면 페이지 속 PDF로 따라감
        except Exception as e: E.log("material download error", e)
    tan_pdf = None
    if card.get("pdf"):
        try: tan_pdf = http(card["pdf"], timeout=60)
        except Exception as e: E.log("tanshin pdf error", e)
    rec_texts, rec_pdfs, rec_links = [], [], []
    try:
        qa_rows = earn_ir.find_qa(code, card["d"], days=min(14, max(0, (E.now().date() - dt.date(int(card["d"][:4]), int(card["d"][4:6]), int(card["d"][6:]))).days)))
        for x in qa_rows[:1]:
            rec_pdfs.append(http(x["pdf"], timeout=60)); rec_links.append(("질의응답(TDnet)", x["pdf"]))
        if not qa_rows:
            for nm, kind, body, url in earn_ir.other_qa(card)[:2]:
                if kind == "pdf": rec_pdfs.append(body)
                else: rec_texts.append(f"[{nm}] {body[:20000]}")
                rec_links.append((nm, url))
    except Exception as e:
        E.log("record error", e)
    rev_pdf, rev_note = None, ""
    for day, u, t in (kd.get("revs") or [])[:1]:   # 발표 이후 나온 업적 예상 수정 → 최신 가이던스로 반영
        try:
            b = http(u, timeout=60)
            if b"%PDF" in b[:1024]:
                rev_pdf = b; rec_links.append((f"이후 업적 예상 수정({day[:4]}-{day[4:6]}-{day[6:]})", u))
                rev_note = f"\n참고: 첨부 중 하나는 이번 결산 발표 뒤 {day[:4]}-{day[4:6]}-{day[6:]}에 나온 업적 예상 수정 공시다. 가이던스(guidance) 서술에는 이 최신 수정치와 날짜를 반드시 반영하라."
        except Exception as e: E.log("revision pdf error", e)
    pdfs, total, pnames = [], 0, []
    for p_, nm in [(mat_pdf, "이번 결산 설명 자료"), (rec_pdfs[0] if rec_pdfs else None, "설명회 질의응답 기록"), (tan_pdf, "이번 결산단신"), (rev_pdf, "이후 업적 예상 수정 공시")]:
        if p_ and p_ not in pdfs and total + len(p_) <= 12_000_000:  # Gemini 요청 한도(약 20MB, base64 포함) 안으로
            pdfs.append(p_); total += len(p_); pnames.append((nm, None))
    # 3-2) 최근 IR 참고 자료: 이번 분기 설명 자료가 없으면 최근 1년 안의 발표 자료·중기 경영계획·설비투자 공시로 구조 자료(캐파·증설·수주잔고 등)를 보완
    refs = []
    try:
        refs = earn_ir.latest_refs(card, skip=(mat_url,) if mat_url else (), limit=1 if mat_pdf else 2, budget=12_000_000 - total)
    except Exception as e: E.log("latest refs error", e)
    for b_, u_, t_, d_ in refs:
        pdfs.append(b_); total += len(b_); pnames.append((f"이전 IR 참고 자료: {t_} ({d_} 발표)", (u_, t_, d_)))
    ref_note = "\n첨부 목록: " + "; ".join(f"{i}. {nm}" for i, (nm, _) in enumerate(pnames, 1)) if pdfs else ""
    # 4) Gemini 분석(설명 자료 + 결산단신 + 기록)
    lab_q, months_q = E.qlabel(q, fem)
    nums = (f"이번 3개월 분기({lab_q} {months_q}) 매출 {E.oku(q.get('rev'))}(전년 {E.oku(py.get('rev'))}), 영업이익 {E.oku(q.get('op'))}(전년 {E.oku(py.get('op'))}), "
            f"순이익 {E.oku(q.get('ni'))}(전년 {E.oku(py.get('ni'))}); 참고 누적 매출 {E.oku(r['rev'] / 1e6) if r.get('rev') else '—'}; 연간 예상 영업이익 {E.oku(fc['op'] / 1e6) if fc.get('op') else '없음'}")
    seg_txt = "; ".join(f"{x['name']} 매출 {fmt((x['rev'] or 0) / 1e8)}(전년 {fmt((x['rev_py'] or 0) / 1e8)}) 영익 {fmt((x['op'] or 0) / 1e8)}(전년 {fmt((x['op_py'] or 0) / 1e8)})" for x in segs)
    words = [x["name"] for x in segs] + [h[0] for h in holders]
    prompt = IR_PROMPT.format(name=name, code=code, sector=E.SECTOR_KO.get(card["sector"], card["sector"]), profile=card.get("profile", "")[:500],
                              nums=nums, segs=seg_txt or "없음", qrule=E.QRULE,
                              seg_basis=f"이번 3개월 분기 {lab_q}({months_q}), 누적 − 직전 분기 누적" if seg_q else "회계연도 누적 — 분기 숫자 아님", words=json.dumps(words, ensure_ascii=False), qual=card.get("qual", "")[:5000],
                              extra=("설명회 기록(웹):\n" + "\n\n".join(rec_texts)) if rec_texts else "")
    prompt += rev_note + ref_note
    ai = (gemini_pdf(prompt, pdfs, max_tokens=16000) if pdfs else gemini_json(prompt, timeout=180)) or {}
    card["_ai_ok"] = bool(ai)
    if isinstance(ai, list): ai = ai[0] if ai and isinstance(ai[0], dict) else {}
    # 4-2) 캐파·비즈니스 심층·주주환원 목표를 한 번의 호출로(캐파는 핵심 데이터에 없을 때만 맨 앞에 넣음)
    capa_hit, deep = False, {}
    try:
        ins = [x for x in (ai.get("insights") or []) if isinstance(x, dict)]
        capa_hit = any(CAPA_RE.search(f"{x.get('title', '')} {x.get('takeaway', '')}") for x in ins)
        if ai: deep = deep_extract(card, name, code, pdfs, pnames)
        cp_ = deep.get("capa")
        if not capa_hit and isinstance(cp_, dict) and cp_.get("found") and cp_.get("title"):
            ai["insights"] = [cp_] + ins[:5]; capa_hit = True
            E.log("캐파 자료 따로 찾음", code, cp_.get("title", "")[:30])
    except Exception as e:
        E.log("deep extract error", e)
    trm = {w: KO_COMMON[w] for w in words if w in KO_COMMON}
    trm.update({k: v for k, v in (ai.get("tr") or {}).items() if v})
    miss = [w for w in words if w not in trm or re.search(r"[぀-ヿ一-鿿]", trm.get(w) or "")]
    if miss: trm.update(tr_google(miss))

    # 5) 사업부별·지역별: XBRL 세그먼트가 어느 쪽인지 판별해 배치, 나머지는 설명 자료
    xrows = [{"ko": trm.get(x["name"], x["name"]), "rev": x["rev"] / 1e8 if x.get("rev") is not None else None,
              "rev_py": x["rev_py"] / 1e8 if x.get("rev_py") is not None else None, "op": x["op"] / 1e8 if x.get("op") is not None else None,
              "op_py": x["op_py"] / 1e8 if x.get("op_py") is not None else None} for x in segs]
    per_lab = (f"{lab_q}({months_q}) 3개월" + (f" · 이번 누적 − 직전 분기 누적({qsrc})" if (r.get('q') or 1) > 1 else "")) if seg_q else \
        f"{'누적' if r.get('q', 4) > 1 else ''} {r.get('start', '')[:7]}~{r.get('end', '')[:7]}".strip() + (f" · 분기 분리 불가({qsrc})" if qsrc else "")
    QPER = lambda p: bool(p) and "누적" not in p and bool(re.search(r"3개월|[1-4]Q\s*\(|분기|\d+~\d+월", p))
    total_cum = r["rev"] / 1e8 if r.get("rev") else None
    biz_rows, biz_src, biz_per = [], "", ""
    reg_rows, reg_src, reg_per = [], "", ""
    if len(xrows) >= 2:
        ai_q = lambda blk: QPER((ai.get(blk) or {}).get("period")) and len(seg_rows_from_ai(ai.get(blk), trm)) >= 2
        if not seg_q and (ai_q("business") or ai_q("region")):   # XBRL이 누적뿐이면 설명 자료의 3개월 숫자를 우선
            xrows = []
    if len(xrows) >= 2:
        if is_region([x["name"] for x in segs] + [x["ko"] for x in xrows]):
            reg_rows, reg_src, reg_per = xrows, "결산단신 XBRL 세그먼트 정보(외부 고객 매출)", per_lab
        else:
            biz_rows, biz_src, biz_per = xrows, "결산단신 XBRL 세그먼트 정보(외부 고객 매출)", per_lab
    src_name = "결산 설명 자료" if mat_pdf else "결산단신"
    if not biz_rows:
        biz_rows = seg_rows_from_ai(ai.get("business"), trm); biz_src = f"{src_name}(Gemini 추출)"; biz_per = (ai.get("business") or {}).get("period", "")
    if not reg_rows:
        reg_rows = seg_rows_from_ai(ai.get("region"), trm); reg_src = f"{src_name}(Gemini 추출)"; reg_per = (ai.get("region") or {}).get("period", "")
    def total_for(per):
        if seg_q and per == per_lab and q.get("rev"): return q["rev"] / 100
        if "누적" in (per or "") and total_cum: return total_cum
        if QPER(per) and q.get("rev"): return q["rev"] / 100
        return total_cum
    biz_html, _ = seg_section(plt, "사업부별 실적", biz_rows, biz_per, biz_src, total_for(biz_per))
    reg_html, _ = seg_section(plt, "지역별 실적", reg_rows, reg_per, reg_src, total_for(reg_per))
    if not biz_html: biz_html = "<h2>사업부별 실적</h2><p class=muted>결산단신·설명 자료에 사업부 구분 숫자가 없습니다(단일 사업 회사일 수 있음).</p>"
    if not reg_html: reg_html = "<h2>지역별 실적</h2><p class=muted>결산단신·설명 자료에 지역 구분 숫자가 없습니다.</p>"

    try: guide_html = guide_section(plt, card, kb)
    except Exception as e: E.log("guide section error", e); guide_html = ""
    # IR 자료에서 고른 배당·주주환원 자료(예: 주당 배당금 추이·누진배당)는 '핵심 데이터'가 아니라 주주환원 섹션에 합쳐 보여줌
    all_ins = [x for x in (ai.get("insights") or []) if isinstance(x, dict) and x.get("title")][:6]
    div_ins = [x for x in all_ins if DIV_RE.search(x.get("title", "")) and not CAPA_RE.search(x.get("title", ""))]
    other_ins = [x for x in all_ins if x not in div_ins]
    base_pdf = mat_pdf or tan_pdf
    used_refs = []
    def ins_card(it):
        img = insight_chart(plt, it.get("chart") or {}, it["title"])
        try: di = int(it.get("doc") or 0) - 1
        except Exception: di = -1
        src_pdf, ref = (pdfs[di], pnames[di][1]) if 0 <= di < len(pdfs) else (base_pdf, None)
        pg = page_image(src_pdf, it.get("page")) if src_pdf and it.get("page") else None
        tag = ""
        if ref:
            tag = f' <small class=muted>({esc(ref[2])} 발표 「<a href="{esc(ref[0])}">{esc(ref[1][:30])}</a>」 기준)</small>'
            if ref not in used_refs: used_refs.append(ref)
        return (f"<div class=card><h3>{esc(it['title'])}{tag}</h3>" + (f"<p><b>{esc(it.get('takeaway'))}</b></p>" if it.get("takeaway") else "")
                + (f"<p class=muted>왜 중요한가: {esc(it.get('why'))}</p>" if it.get("why") else "")
                + (f'<img src="{img}">' if img else "")
                + (f"<details><summary>원문 {esc(it.get('page'))}쪽 보기</summary><img src=\"{pg}\"></details>" if pg else "") + "</div>")
    div_html = ""
    for it in div_ins:
        try: div_html += ins_card(it)
        except Exception as e: E.log("div insight error", e)
    try: ret_html = ret_section(card, kb, att, plt, deep.get("policy"), div_html)
    except Exception as e: E.log("ret section error", e); ret_html = ""
    # 맨 앞 '기업 개요'(연혁·사업 변천·지금의 사업 구성) + '주주 구성'(소유자별 분포·대주주 상위 10) — EDINET 유가증권보고서
    prof_html, pf = "", {}
    try:
        import earn_profile as EP
        pf = EP.get(card, name)
        prof_html = EP.html(pf, esc, fallback_overview=ai.get("overview") or (card.get("ai") or {}).get("bm") or "", fallback_holders=holders, trm=trm)
        card["_prof"] = f"{len(pf.get('overview') or [])}문단·주주구성 {len(pf.get('owners') or [])}·대주주 {len(pf.get('holders') or [])}" if pf else "EDINET 없음(대체)"
    except Exception as e: E.log("profile error", e)
    try: bizdeep_html = biz_section(plt, card, name, code, deep.get("biz"), deep.get("_biz_cached"))
    except Exception as e: E.log("biz section error", e); bizdeep_html = ""
    # 6) IR 자료에서 고른 핵심 데이터
    ins_html = ""
    for it in other_ins:
        ins_html += ins_card(it)
    if ins_html:
        ins_html = (f"<h2>IR 자료에서 고른 핵심 데이터</h2><p class=muted>출처: {esc(src_name)}({esc(mat_src if mat_pdf else 'TDnet')}) — Gemini가 의미 있는 표를 골라 숫자를 뽑고, "
                    "한국어 차트로 다시 그렸습니다. 숫자는 원문 쪽 이미지로 확인하세요."
                    + (" 이번 분기 설명 자료에 없는 구조 자료(캐파·증설·수주잔고·중기 목표 등)는 최근 IR 자료에서 가져왔고, 제목 옆에 그 자료의 발표일을 적었습니다." if used_refs else "")
                    + "</p>" + ins_html)
    qa_html = ""
    qa = [x for x in (ai.get("qa") or []) if isinstance(x, dict) and x.get("q")]
    if qa:
        qa_html = ("<h2>경영진 Q&amp;A</h2><div class=card>" + "".join(
            f"<p><b>Q{i}. {esc(x['q'])}</b><br>{esc(x.get('a'))}" + (f" <small>— {esc(x.get('who'))}</small>" if x.get("who") else "") + "</p>"
            for i, x in enumerate(qa, 1)) + "<p class=muted>출처: " + " · ".join(f"<a href='{esc(u)}'>{esc(n)}</a>" for n, u in rec_links) + "</p></div>")

    # 6-2) 다른 사람들의 의견(note·야후 게시판·가부탄 뉴스)
    try:
        import earn_opinion
        op_html = earn_opinion.build_section(card)
    except Exception as e:
        E.log("opinion error", e); op_html = ""
    # 7) 실적 표·차트
    charts = []
    try:
        bars = E.naver_daily(f"{code}.T", days=380)
        ds = [dt.date.fromisoformat(b["d"]) for b in bars]; cs = [b["c"] for b in bars]
        fig, ax = plt.subplots(figsize=(8, 3.6))
        ax.plot(ds, cs, color=E.BLUE, lw=1.4)
        top = max(cs)
        for qq in allq:
            a = qq.get("ann")
            if not a or a < bars[0]["d"]: continue
            nxt = [i for i, b in enumerate(bars) if b["d"] > a]
            ax.axvline(dt.date.fromisoformat(a), color=E.GRAY, lw=0.8, ls="--")
            if nxt and nxt[0] > 0:
                i = nxt[0]; ch = (bars[i]["c"] / bars[i - 1]["c"] - 1) * 100
                ax.text(dt.date.fromisoformat(a), top * 1.01, f"{ch:+.1f}%", fontsize=9, ha="center", va="bottom", fontweight="bold",
                        color="#c0392b" if ch < 0 else "#1e8449")
        ax.set_title("주가 1년과 실적 발표일 (점선=발표일, 숫자=다음 날 종가 변화)", loc="left", fontsize=12, fontweight="bold")
        ax.set_ylabel("엔", fontsize=9)
        for sp in ("top", "right"): ax.spines[sp].set_visible(False)
        ax.grid(color="#eee"); fig.tight_layout()
        charts.append(b64png(fig))
    except Exception as e:
        E.log("price chart error", e)
    labs = []
    for qq in s:
        fy, n = E.fy_of(qq["end"], fem); labs.append(f"{n}Q\n{fy}")
    for key, title in (("rev", "분기 매출과 YoY"), ("op", "분기 영업이익 · YoY · 영업이익률")):
        fig, ax = plt.subplots(figsize=(8, 3.6))
        vals = [(qq.get(key) or 0) / 100 for qq in s]
        ax.bar(range(len(s)), vals, color=[E.BLUE if i == len(s) - 1 else "#9dbbea" for i in range(len(s))], width=0.6)
        for i in (len(s) - 1, len(s) - 5):
            if 0 <= i < len(s): ax.text(i, vals[i], f"{vals[i]:,.1f}" if abs(vals[i]) < 100 else f"{vals[i]:,.0f}", ha="center", va="bottom", fontsize=9, fontweight="bold")
        ax.set_xticks(range(len(s))); ax.set_xticklabels(labs, fontsize=8)
        ax.set_ylabel("억 엔", fontsize=8)
        ax2 = ax.twinx()
        ys = [E.yoy(qq.get(key), (full.get(E.ym_add(qq["end"], -12)) or {}).get(key))[0] for qq in s]
        xs = [i for i, v in enumerate(ys) if v is not None]
        ax2.plot(xs, [min(max(ys[i], -100), 300) for i in xs], color=E.ORANGE, marker="o", ms=4, label="YoY(%)")
        for i in xs:
            if ys[i] > 300: ax2.annotate(f"{ys[i]:+,.0f}%", (i, 300), textcoords="offset points", xytext=(0, 5), ha="center", fontsize=7, color=E.ORANGE)
        if key == "op":
            opm = [(qq["op"] / qq["rev"] * 100) if qq.get("op") is not None and qq.get("rev") else None for qq in s]
            xm = [i for i, v in enumerate(opm) if v is not None]
            ax2.plot(xm, [opm[i] for i in xm], color="#2e7d32", marker="s", ms=3, ls="--", label="영업이익률(%)")
        ax2.legend(fontsize=8, loc="upper left", frameon=False)
        ax.set_title(title, loc="left", fontsize=12, fontweight="bold")
        ax.spines["top"].set_visible(False); ax2.spines["top"].set_visible(False)
        fig.tight_layout(); charts.append(b64png(fig))
    qrows = []
    for qq in s:
        lab, months = E.qlabel(qq, fem)
        p = full.get(E.ym_add(qq["end"], -12)) or {}
        ry, rs = E.yoy(qq.get("rev"), p.get("rev")); oy, os_ = E.yoy(qq.get("op"), p.get("op")); ny, ns = E.yoy(qq.get("ni"), p.get("ni"))
        opm = qq["op"] / qq["rev"] * 100 if qq.get("op") is not None and qq.get("rev") else None
        cls = " class=cur" if qq is s[-1] else ""
        qrows.append(f"<tr{cls}><td>{lab}<br><small>{months}</small></td><td>{qq.get('ann') or ''}</td><td>{E.oku(qq.get('rev'))}</td><td>{E.yoy_txt(ry, rs)}</td>"
                     f"<td>{E.oku(qq.get('op'))}</td><td>{E.yoy_txt(oy, os_)}</td><td>{fmt(opm)}%</td><td>{E.oku(qq.get('ni'))}</td><td>{E.yoy_txt(ny, ns)}</td></tr>")
    pl_html = ""
    pl_src = qsrc
    if not qpl and q.get("rev") is not None:   # 누적 − 직전 누적이 안 맞으면 가부탄 3개월 실적(결산단신 기준)으로 주요 항목만
        mm = lambda k: [q[k] * 1e6 if q.get(k) is not None else None, py[k] * 1e6 if py.get(k) is not None else None]
        qpl = {"매출": mm("rev"), "영업이익": mm("op"), "경상이익": mm("ord"), "지배주주 순이익": mm("ni")}
        pl_src = "가부탄 3개월 실적(주요 항목만)"
    if qpl:
        rows = []
        rev = qpl.get("매출") or [None, None]
        ratio = lambda a, b: f"{a / b * 100:.1f}%" if a is not None and b else "—"
        for lab, _ in PL_ROWS:
            v = qpl.get(lab)
            if not v or (v[0] is None and v[1] is None): continue
            rows.append(f"<tr><td>{lab}</td><td>{E.oku(v[1] / 1e6 if v[1] is not None else None)}</td><td>{ratio(v[1], rev[1])}</td>"
                        f"<td>{E.oku(v[0] / 1e6 if v[0] is not None else None)}</td><td>{ratio(v[0], rev[0])}</td><td>{fmt(pct(v[0], v[1]))}%</td></tr>")
        pl_html = (f"<details open><summary>분기 손익 상세 ({esc(lab_q)} {esc(months_q)} 3개월" + (f" · {esc(pl_src)}" if (r.get('q') or 1) > 1 else "") + ")</summary><div class=scroll><table>"
                   "<tr><th>항목</th><th>전년 분기</th><th>매출 대비</th><th>이번 분기</th><th>매출 대비</th><th>YoY</th></tr>" + "".join(rows) + "</table></div></details>")
    if att["pl"] and (r.get("q") or 1) > 1:
        rows = []
        rev = pl_pick(att["pl"], *PL_ROWS[0][1])
        for lab, pats in PL_ROWS:
            v = pl_pick(att["pl"], *pats)
            if v[0] is None and v[1] is None: continue
            ratio = lambda a, b: f"{a / b * 100:.1f}%" if a is not None and b else "—"
            rows.append(f"<tr><td>{lab}</td><td>{E.oku(v[1] / 1e6 if v[1] is not None else None)}</td><td>{ratio(v[1], rev[1])}</td>"
                        f"<td>{E.oku(v[0] / 1e6 if v[0] is not None else None)}</td><td>{ratio(v[0], rev[0])}</td><td>{fmt(pct(v[0], v[1]))}%</td></tr>")
        pl_html += (f"<details><summary>참고: 누적 손익 상세 ({esc(r.get('start', '')[:7])}~{esc(r.get('end', '')[:7])}, 결산단신 손익계산서)</summary><div class=scroll><table>"
                   "<tr><th>항목</th><th>작년 누적</th><th>매출 대비</th><th>올해 누적</th><th>매출 대비</th><th>YoY</th></tr>" + "".join(rows) + "</table></div></details>")
    yrows = []
    for y in kb["y"][-6:]:
        yrows.append(f"<tr><td>{'(예상) ' if y['fc'] else ''}FY{int(y['end'][5:])}/{y['end'][2:4]}</td><td>{E.oku(y['rev'])}</td><td>{E.oku(y['op'])}</td>"
                     f"<td>{fmt(y['op'] / y['rev'] * 100 if y.get('op') is not None and y.get('rev') else None)}%</td><td>{E.oku(y['ni'])}</td>"
                     f"<td>{fmt(y.get('eps'))}엔</td><td>{fmt(y.get('dps'))}엔</td></tr>")
    last_y = next((y for y in reversed(kb["y"]) if not y["fc"]), None)
    per = card["cap"] * 1e8 / fc["ni"] if card.get("cap") and fc.get("ni") and fc["ni"] > 0 else None
    dps = next((y.get("dps") for y in reversed(kb["y"]) if y["fc"]), None)
    price = None
    try: price = E.naver_daily(f"{code}.T", days=10)[-1]["c"]
    except Exception: pass
    kpi = [("시가총액", f"{card['cap']:,.0f}억 엔" if card.get("cap") else "—"),
           ("최근 연도 매출", f"{E.oku(last_y['rev'])} 엔<small> FY{int(last_y['end'][5:])}/{last_y['end'][2:4]}</small>" if last_y else "—"),
           ("PER(회사 예상)", f"{per:.1f}배" if per else "—"),
           ("배당(예상)", (f"{dps:,.1f}엔" + (f"<small> 수익률 {dps / price * 100:.1f}%</small>" if price else "")) if dps else "—")]
    cardai = card.get("ai") or {}
    why = "".join(f"<li><b>{'기타' if k == 'etc' else k}</b> {esc(cardai[k])}</li>" for k in ("P", "Q", "C", "etc") if cardai.get(k) and "원문 설명 없음" not in cardai[k])
    holder_html = ""
    if holders:
        holder_html = ("<details><summary>대주주 (가부탄, 최근 보고 기준)</summary><table><tr><th>주주</th><th>지분율</th></tr>" +
                       "".join(f"<tr><td>{esc(trm.get(h[0], h[0]))}</td><td>{esc(h[1])}%</td></tr>" for h in holders) + "</table></details>")
    rx = card.get("rx") if isinstance(card.get("rx"), dict) else None
    rx_html = f"<p class=rx>{E.reaction_line(rx)}</p>" if rx else ""
    watch = "".join(f"<li>{esc(w)}</li>" for w in (ai.get("watch") or []))
    links = [f"<a href='{card['pdf']}'>결산단신(PDF)</a>" if card.get("pdf") else f"<a href='https://s.kabutan.jp/stocks/{code}/finance/'>가부탄 실적</a>"]
    if mat_url: links.append(f"<a href='{esc(mat_url)}'>결산 설명 자료({esc(mat_src)})</a>")
    links += [f"<a href='{esc(u_)}'>참고: {esc(t_[:30])}({esc(d_)})</a>" for _, u_, t_, d_ in refs]
    links += [f"<a href='{esc(u)}'>{esc(n)}</a>" for n, u in rec_links]
    headline = ai.get("headline") or cardai.get("summary") or ""
    lab, months = E.qlabel(s[-1], fem)
    tail = ""
    if ai.get("guidance") or ai.get("risks") or watch:
        tail = ("<h2>가이던스 · 리스크 · 다음에 확인할 점</h2><div class=card>" + (f"<p><b>가이던스</b> {esc(ai.get('guidance'))}</p>" if ai.get("guidance") else "")
                + (f"<p><b>리스크</b> {esc(ai.get('risks'))}</p>" if ai.get("risks") else "") + (f"<ul>{watch}</ul>" if watch else "") + "</div>")
    # 한눈에 보기: 실적 한 줄 · 가이던스 · 주주환원 · 핵심 포인트 3개 (휴대폰에서 먼저 읽을 것)
    glance = []
    try:
        ry, rst = E.yoy(q.get("rev"), py.get("rev")); oy, ost = E.yoy(q.get("op"), py.get("op"))
        glance.append(f"<b>실적</b> {esc(lab_q)}({esc(months_q)}) 매출 {E.oku(q.get('rev'))}({E.yoy_txt(ry, rst)}) · 영업이익 {E.oku(q.get('op'))}({E.yoy_txt(oy, ost)})")
        if fc.get("op"):
            ly = E.fy_base(card["series"], r, "op") or E.fy_base(allq, r, "op")
            rv_ = card.get("rev") or {}
            o0 = (rv_.get("op") or (None, None))[0]
            glance.append(f"<b>가이던스</b> {E.fy_label_end(r.get('fc_end'))} 영업이익 예상 {E.oku(fc['op'] / 1e6)}"
                          + (f"(전년 대비 {(fc['op'] / 1e6 / ly - 1) * 100:+.0f}%)" if ly and ly > 0 else "")
                          + (f" · 이번에 {E.oku(o0)}에서 {'상향' if fc['op'] / 1e6 > o0 else '하향'}" if o0 and abs(fc['op'] / 1e6 - o0) > 0.5 else ""))
        rl = E.ret_line(card.get("ret"))
        if rl: glance.append(rl.replace("▶️ ", ""))
        pts = [x for x in (ai.get("insights") or []) if isinstance(x, dict) and (x.get("takeaway") or x.get("title"))][:3]
        if pts: glance.append("<b>핵심 포인트</b><ul>" + "".join(f"<li>{esc(x.get('takeaway') or x.get('title'))}</li>" for x in pts) + "</ul>")
    except Exception as e:
        E.log("glance error", e)
    # 분기 컨센서스 대비(매출: 발표 전 Yahoo 평균, EPS: Yahoo 분기 실적 기록)
    cq_html = ""
    try:
        import earn_diff as ED_
        cq_html, cq_short = ED_.cons_quarter(card, q, esc)
        if cq_short: glance.insert(1, f"<b>컨센서스</b> {esc(cq_short)}")
        card["_cq"] = cq_short or "없음"
    except Exception as e: E.log("cons quarter error", e)
    # 지난 리포트 대비 달라진 점(같은 종목의 이전 분기 리포트와 비교, 이번 값 저장)
    diff_html = ""
    try:
        import earn_diff as ED_
        cp_ = deep.get("capa") if isinstance(deep.get("capa"), dict) and deep["capa"].get("found") else None
        def _f(x):
            try: return float(str(x).replace("%", "").replace(",", ""))
            except Exception: return None
        hs_ = [(n, None, p_) for n, _, p_ in (pf.get("holders") or [])] if pf.get("holders") else [(h[0], None, _f(h[1])) for h in holders]
        snap = ED_.snapshot(card, fc, card.get("ret"), cp_, deep.get("policy"), hs_, ai.get("headline") or (card.get("ai") or {}).get("summary") or "",
                            f"{lab_q}({months_q})", "edinet" if pf.get("holders") else "kabutan")
        diff_html, n_ = ED_.diff_html(code, snap, esc)
        card["_diff"] = (f"{n_}건" if diff_html else "첫 리포트")
    except Exception as e: E.log("report diff error", e)
    glance_html = ("<div class=glance><div class=gt>한눈에 보기</div>" + "".join(f"<div>{g}</div>" for g in glance) + "</div>") if glance else ""

    tables_html = (f"<h2>분기·연간 실적 표와 차트</h2><h3>분기 실적 (3개월 단독, {esc(r.get('scope') or '')})</h3><div class=scroll><table>"
                   "<tr><th>분기</th><th>발표일</th><th>매출</th><th>YoY</th><th>영업이익</th><th>YoY</th><th>영업이익률</th><th>순이익</th><th>YoY</th></tr>" + "".join(qrows) + "</table></div>"
                   + pl_html + "<h3>연간 실적</h3><div class=scroll><table><tr><th>회계연도</th><th>매출</th><th>영업이익</th><th>영업이익률</th><th>순이익</th><th>EPS</th><th>배당</th></tr>"
                   + "".join(yrows) + "</table></div>" + "".join(f'<figure><img src="{c}"></figure>' for c in charts))
    try: EP_CSS = __import__("earn_profile").CSS
    except Exception: EP_CSS = ""
    # 차례: 회사 이해(개요·주주) → 이번 분기(요약·사업부·지역·가이던스·IR 자료·Q&A) → 구조(비즈니스 심층·주주환원) → 시장 반응 → 리스크·체크포인트 → 부록(표)
    secs = []
    def add(h, open_=False, title=None):
        if not h: return
        m = re.match(r"\s*<h2>(.*?)</h2>", h, re.S)
        secs.append((title or (m.group(1) if m else ""), h[m.end():] if m else h, open_))
    ov_txt = ai.get("overview") or cardai.get("bm")
    add(diff_html, True)   # 같은 종목을 다시 볼 때: 달라진 점을 맨 먼저
    add(prof_html or (("<h2>기업 개요</h2>" + (f"<div class=card>{esc(ov_txt)}</div>" if ov_txt else "") + holder_html) if (ov_txt or holder_html) else ""), True, "기업 개요 · 주주 구성")
    add("<h2>이번 분기 요약</h2>" + f"<div class=kpi>{''.join(f'<div><span>{k}</span><b>{v}</b></div>' for k, v in kpi)}</div>" + glance_html
        + (f"<div class=head><b>{esc(headline)}</b><p>{esc(ai.get('narrative'))}</p></div>" if headline else "")
        + cq_html + (f"<div class=card><ul>{why}</ul>{rx_html}</div>" if (why or rx_html) else ""), True)
    add(biz_html, True); add(reg_html); add(guide_html, True); add(ins_html, True); add(qa_html)
    add(bizdeep_html); add(ret_html); add(op_html)
    add(tail, True, "리스크 · 다음에 확인할 점")
    add(tables_html, False, "부록 — 분기·연간 실적 표와 차트")
    toc, body, k = [], [], 0
    for i, (ti, bd, op_) in enumerate(secs):
        num = "" if ti.startswith("부록") else f"{(k := k + 1)}. "
        plain = re.sub(r"<[^>]+>", "", ti)
        toc.append(f"<li><a href='#s{i}'>{num}{esc(plain)}</a></li>")
        body.append(f"<details class=sec id=s{i} open><summary><span class=h2>{num}{ti}</span></summary>{bd}</details>")
    toc_html = "<nav class=toc><b>차례</b><ol>" + "".join(toc) + "</ol></nav>" if len(secs) > 2 else ""
    body_html = "\n".join(body)
    TOC_CSS = (".toc{background:#fff;border-radius:12px;padding:10px 16px;margin:12px 0;box-shadow:0 1px 2px rgba(0,0,0,.06)} .toc ol{list-style:none;margin:6px 0 0;padding:0;columns:2;column-gap:20px;font-size:13.5px}"
               " .toc li{margin:3px 0;break-inside:avoid} .toc a{text-decoration:none} @media (max-width:600px){.toc ol{columns:1}}")
    TOC_JS = ("<script>document.querySelectorAll('.toc a').forEach(function(a){a.addEventListener('click',function(){var d=document.querySelector(a.getAttribute('href'));if(d)d.open=true;});});"
              "if(location.hash){var d0=document.querySelector(location.hash);if(d0)d0.open=true;}</script>")
    doc = f"""<!doctype html><html lang=ko><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>{esc(name)}({code}) 실적 리포트</title><style>
body{{font-family:-apple-system,'Apple SD Gothic Neo','Noto Sans KR','Malgun Gothic',sans-serif;margin:0;background:#f6f7f9;color:#1f2328;line-height:1.55}}
main{{max-width:880px;margin:0 auto;padding:16px}} h1{{font-size:22px;margin:8px 0 2px}} h2{{font-size:17px;margin:28px 0 8px;border-left:4px solid #2f6fd6;padding-left:8px}}
h3{{font-size:15px;margin:2px 0 6px}} .card{{background:#fff;border-radius:12px;padding:14px 16px;margin:10px 0;box-shadow:0 1px 2px rgba(0,0,0,.06)}} .card img{{width:100%}}
.kpi{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:8px;margin:12px 0}} .kpi div{{background:#fff;border-radius:10px;padding:10px 12px;box-shadow:0 1px 2px rgba(0,0,0,.05)}}
.kpi small{{color:#6b7280;font-weight:400}} .kpi span{{color:#6b7280;font-size:12px}} .kpi b{{display:block;font-size:18px;margin-top:2px}}
.head{{background:#1f2328;color:#fff;border-radius:12px;padding:14px 16px;margin:12px 0}} .head b{{font-size:17px}} .head p{{margin:6px 0 0;color:#d0d4da}}
table{{border-collapse:collapse;width:100%;font-size:13px;background:#fff}} th,td{{border-bottom:1px solid #eee;padding:6px 7px;text-align:right;white-space:nowrap}}
th:first-child,td:first-child{{text-align:left}} th{{background:#f0f3f8;font-weight:600}} tr.cur td{{background:#eef4ff;font-weight:600}} .scroll{{overflow-x:auto;border-radius:10px}}
details{{background:#fff;border-radius:10px;margin:10px 0;padding:8px 12px}} summary{{cursor:pointer;font-weight:600}} details img{{width:100%;margin-top:6px;border:1px solid #eee}}
figure{{margin:10px 0;background:#fff;border-radius:10px;padding:8px}} figure img{{width:100%}} figcaption{{font-size:12px;color:#6b7280;padding:2px 6px}}
.muted,small{{color:#6b7280}} a{{color:#2f6fd6}} .rx{{background:#fff7ed;border-radius:8px;padding:6px 10px}}
.glance{{background:#fff;border:2px solid #2f6fd6;border-radius:12px;padding:12px 16px;margin:12px 0}} .glance .gt{{font-weight:700;color:#2f6fd6;margin-bottom:6px}} .glance>div{{margin:4px 0}} .glance ul{{margin:4px 0 0 0;padding-left:18px}}
details.sec{{padding:0 12px 8px}} details.sec>summary{{list-style:none;padding:10px 0}} details.sec>summary::-webkit-details-marker{{display:none}}
.h2{{font-size:17px;font-weight:700;border-left:4px solid #2f6fd6;padding-left:8px}} details.sec>summary .h2:after{{content:" ▸";color:#6b7280;font-size:13px}} details.sec[open]>summary .h2:after{{content:" ▾"}}
.flow{{padding:6px 10px;margin:4px 0;background:#f0f6ff;border-left:3px solid #2f6fd6;border-radius:4px}}
.fchain{{display:flex;flex-wrap:wrap;align-items:center;gap:4px;margin:6px 0}} .fstep{{background:#f0f6ff;border:1px solid #c9dafb;border-radius:8px;padding:4px 8px;font-size:13px}} .farr{{color:#2f6fd6;font-weight:700}}
.vchain{{display:flex;align-items:stretch;gap:6px;overflow-x:auto;padding:4px 0 10px}} .varr{{align-self:center;color:#2f6fd6;font-size:20px;font-weight:700}}
.vc{{flex:0 0 150px;background:#fff;border:1px solid #dde3ec;border-radius:10px;padding:8px 10px;font-size:12.5px;box-shadow:0 1px 2px rgba(0,0,0,.05)}}
.vc.me{{border:2px solid #2f6fd6;background:#eef4ff}} .vn{{font-size:11px;color:#6b7280}} .vs{{font-weight:700;font-size:14px;margin:2px 0 4px}} .vw{{color:#374151}} .vp{{color:#6b7280;margin-top:6px;font-size:12px}} .vr{{margin-top:6px;color:#1d4ed8;font-weight:600}}
.vmh{{margin:4px 0 0;font-size:15px}} .vmg{{display:grid;column-gap:18px;row-gap:8px;align-items:center;min-width:620px;padding:6px 2px}}
.vml{{grid-column:1;font-size:12px;color:#6b7280}} .vml.vco{{color:#1f2328;font-size:13px;line-height:1.2}} .vml.vco small{{display:block;color:#6b7280;font-size:11px}} .vml.vco.me b{{color:#b33a2e}}
.vst{{position:relative;background:#fff;border:1px solid #d6dbe3;border-radius:8px;padding:6px 4px;text-align:center;font-size:13px;line-height:1.25}} .vst small{{display:block;color:#6b7280;font-size:11px;font-weight:400}}
.vst:after{{content:"➜";position:absolute;right:-16px;top:50%;transform:translateY(-50%);color:#6b7280;font-size:13px}} .vst.la:after{{content:none}}
.vcx{{background:#e8eef5;border:1px solid #d6dbe3;border-radius:8px;padding:6px;text-align:center;font-size:12.5px;font-weight:600}}
.vbar{{border-radius:8px;padding:7px 6px;text-align:center;font-size:12.5px;font-weight:600;line-height:1.25}} .vbar.mn{{background:#fbe4de;border:2px solid #b33a2e}} .vbar.sub{{background:#e8eef5;border:1px solid #9aa4b2;font-weight:500}}
.vbar.me{{box-shadow:0 0 0 2px #fde68a}} .vsep{{grid-column:1/-1;border-top:1px dashed #c3c9d2;height:0}} .vrowend{{display:none}}
@media (max-width:600px){{.vchain{{flex-direction:column}} .vc{{flex:1 1 auto}} .varr{{transform:rotate(90deg);align-self:center}}}} .tag{{font-size:11px;background:#fde68a;color:#78350f;border-radius:4px;padding:0 4px;margin-left:4px}}
{EP_CSS}
{TOC_CSS}
</style></head><body><main>
<h1>{esc(name)} <small>({code} JP · {esc(E.SECTOR_KO.get(card['sector'], card['sector']))})</small></h1>
<p class=muted>{lab}({months}) 실적 · 발표 {card['d'][:4]}-{card['d'][4:6]}-{card['d'][6:]} {card['t']} JST · {' · '.join(card.get('hit') or []) or '요청 분석'}</p>
{toc_html}
{body_html}
<h2>원문</h2><p>{' · '.join(links)}</p>
<p class=muted>출처: TDnet(결산단신 XBRL·PDF, 설명 자료), 회사 IR 홈페이지, 로그미 파이낸스, 가부탄(실적·시가총액·대주주), 네이버 증권(주가). 금액 억 엔(원자료 백만 엔 환산).
결론·요약·번역과 설명 자료에서 뽑은 숫자는 Gemini가 원문만 읽고 작성 — 숫자는 원문 쪽 이미지·링크로 확인하세요. 작성 {E.now():%Y-%m-%d %H:%M} JST</p>
</main>{TOC_JS}</body></html>"""
    card["_stats"] = {"mat": int(bool(mat_pdf)), "refs": len(refs), "capa": int(capa_hit), "ai_fail": int(not ai),
                      "qfail": int(bool(att["seg"]) and (r.get("q") or 1) > 1 and not seg_q)}
    try:   # 실행 기록용 섹션별 결과(텔레그램을 안 열어도 빠진 항목을 알 수 있게)
        bz = deep.get("biz") or {}
        vm = (bz.get("vc_map") or {}) if isinstance(bz, dict) else {}
        card["_stats"]["_sec"] = {
            "설명자료": "O" if mat_pdf else "X", "이전IR": len(refs), "분기3개월": "X" if card["_stats"]["qfail"] else "O",
            "캐파": "O" if capa_hit else "X", "사업분석": ("저장분" if deep.get("_biz_cached") else "O") if bz else "X",
            "밸류체인지도": f"{len(vm.get('players') or [])}개사", "환원목표": len(deep.get("policy") or []),
            "EDINET": f"{card.pop('_edinet_n', 0)}개년", "기업개요": card.pop("_prof", "X"), "분기컨센서스": card.pop("_cq", "X"), "지난대비": card.pop("_diff", "X"), "배당자료": len(div_ins), "컨센서스": "O" if card.pop("_cons", 0) else "X"}
    except Exception as e: E.log("rep sec stats error", e)
    return f"{code}_{card['d']}_report.html", doc.encode("utf-8")
