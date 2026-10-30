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
    key = os.environ.get("GEMINI_API_KEY")
    pdfs = [p for p in pdfs if p]
    if not key or not pdfs: return None
    parts = [{"inline_data": {"mime_type": "application/pdf", "data": base64.b64encode(p).decode()}} for p in pdfs] + [{"text": prompt}]
    body = json.dumps({"contents": [{"parts": parts}],
                       "generationConfig": {"temperature": 0.2, "maxOutputTokens": max_tokens, "responseMimeType": "application/json"}}).encode()
    for model in GEMINI_MODELS + GEMINI_MODELS:  # 구글 서버가 붐비면(503) 한 바퀴 더
        if SLOW.get(model, 0) > time.time() or E.ai_skip(model): continue  # 방금 응답 없던 모델·한도 초과 모델은 잠시 건너뜀
        try:
            r = json.loads(http(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}",
                                timeout=240, data=body, headers={"content-type": "application/json"}))
            txt = "".join(p.get("text", "") for p in r["candidates"][0]["content"]["parts"])
            out = json.loads(re.sub(r"^```(json)?|```$", "", txt.strip()))
            if isinstance(out, list): out = out[0] if out and isinstance(out[0], dict) else None
            return out
        except Exception as e:
            E.log("gemini pdf error", model, str(e)[:150])
            if "429" in str(e): E.ai_429(model)
            if "timed out" in str(e) or "503" in str(e): SLOW[model] = time.time() + 1200  # 응답 지연·과부하 → 20분간 건너뜀
            if "429" in str(e) or "503" in str(e) or "timed out" in str(e): time.sleep(8)
    return None

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

def attachments(xbrl_url):
    """→ {'pl': {항목: (당기누적, 전기누적)}, 'seg': [{name, rev, rev_py, op, op_py}], 'per': '누적 기간 이름'}"""
    blob = http(xbrl_url, timeout=60)
    zf = zipfile.ZipFile(io.BytesIO(blob))
    labels = _labels(zf)
    out = {"pl": {}, "seg": [], "kind": ""}
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
- 금액 단위는 억 엔으로 통일(백만 엔 ÷ 100). 기간 표기는 자료 그대로(예: "2Q 누적(4~9월)", "2Q(7~9월)").
- business: 사업부·제품군별 매출·영업이익(전년 같은 기간과 이번). 자료에 사업부 구분이 여러 개면 매출 비중이 가장 큰 구분 하나.
- region: 지역별 매출(·이익). 자료에 없으면 빈 배열.
  · 두 표 모두 business/region 항목 값은 숫자(억 엔), 모르면 null. period는 두 숫자의 기간.
- insights: 투자 판단에 의미 있는 자료를 네가 골라 3~6개. 우선순위: 수주·수주잔고·BB, 용도·고객 산업별 매출, 가격·물량·환율 요인(영업이익 증감 분석),
  핵심 KPI 추이(점포 수·단가·가동률·고객 수·ARR 등), 가이던스 수정 내역, 주주환원. 매출/이익 합계를 다시 보여주는 것은 제외.
  각 항목: title, page(원문 PDF 쪽 번호, 첫 PDF 기준 1부터), why(왜 의미 있는지 한 줄), takeaway(숫자로 된 결론 한 줄),
  chart: {{"type": "bar" 또는 "line" 또는 "waterfall", "unit": "", "labels": [], "series": [{{"name": "", "values": [숫자]}}]}}
  (waterfall은 labels=[시작, 요인1, 요인2, ..., 끝], series 하나, 시작·끝은 수준값, 가운데는 증감값)
- qa: 설명회 기록·질의응답이 있으면 투자 판단에 중요한 것 3~6개 {{"q": "", "a": "2~3문장", "who": "답변자 직함(있으면)"}}
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
결산단신 XBRL 세그먼트(누적, 억 엔): {segs}
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

    # 1) 결산단신 XBRL 첨부(손익 상세·세그먼트)
    att = {"pl": {}, "seg": []}
    if not card.get("xbrl") and card.get("pdf"):  # 예전 카드: TDnet 파일 이름 규칙(1401…pdf ↔ 0812…zip)으로 XBRL 주소 복원
        card["xbrl"] = re.sub(r"/1401(\d+)\.pdf$", r"/0812\1.zip", card["pdf"])
    if card.get("xbrl"):
        try: att = attachments(card["xbrl"])
        except Exception as e: E.log("attachment error", e)
    segs = att["seg"]
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
            for t, u, d in earn_ir.ir_site_docs(code, card["d"], limit=4, pat=earn_ir.MAT_LINK):
                if earn_ir.QA_LINK.search(t): continue
                kind, body = earn_ir.fetch_doc(u, pat=earn_ir.MAT_LINK)
                if kind == "pdf" and len(body) < 12_000_000: mat_pdf, mat_url, mat_src = body, u, "회사 IR 홈페이지"; break
        except Exception as e:
            E.log("ir site material error", e)
    elif mat_url:
        try: mat_pdf = http(mat_url, timeout=60)
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
    pdfs, total = [], 0
    for p_ in [mat_pdf, rec_pdfs[0] if rec_pdfs else None, tan_pdf if not mat_pdf else None, tan_pdf if mat_pdf else None]:
        if p_ and p_ not in pdfs and total + len(p_) <= 12_000_000:  # Gemini 요청 한도(약 20MB, base64 포함) 안으로
            pdfs.append(p_); total += len(p_)
    # 4) Gemini 분석(설명 자료 + 결산단신 + 기록)
    q = s[-1]; py = full.get(E.ym_add(q["end"], -12)) or {}
    nums = (f"이번 분기 매출 {E.oku(q.get('rev'))}(전년 {E.oku(py.get('rev'))}), 영업이익 {E.oku(q.get('op'))}(전년 {E.oku(py.get('op'))}), "
            f"순이익 {E.oku(q.get('ni'))}; 누적 매출 {E.oku(r['rev'] / 1e6) if r.get('rev') else '—'}; 연간 예상 영업이익 {E.oku(fc['op'] / 1e6) if fc.get('op') else '없음'}")
    seg_txt = "; ".join(f"{x['name']} 매출 {fmt((x['rev'] or 0) / 1e8)}(전년 {fmt((x['rev_py'] or 0) / 1e8)}) 영익 {fmt((x['op'] or 0) / 1e8)}(전년 {fmt((x['op_py'] or 0) / 1e8)})" for x in segs)
    words = [x["name"] for x in segs] + [h[0] for h in holders]
    prompt = IR_PROMPT.format(name=name, code=code, sector=E.SECTOR_KO.get(card["sector"], card["sector"]), profile=card.get("profile", "")[:500],
                              nums=nums, segs=seg_txt or "없음", words=json.dumps(words, ensure_ascii=False), qual=card.get("qual", "")[:5000],
                              extra=("설명회 기록(웹):\n" + "\n\n".join(rec_texts)) if rec_texts else "")
    ai = (gemini_pdf(prompt, pdfs, max_tokens=16000) if pdfs else gemini_json(prompt, timeout=180)) or {}
    card["_ai_ok"] = bool(ai)
    if isinstance(ai, list): ai = ai[0] if ai and isinstance(ai[0], dict) else {}
    trm = {w: KO_COMMON[w] for w in words if w in KO_COMMON}
    trm.update({k: v for k, v in (ai.get("tr") or {}).items() if v})
    miss = [w for w in words if w not in trm or re.search(r"[぀-ヿ一-鿿]", trm.get(w) or "")]
    if miss: trm.update(tr_google(miss))

    # 5) 사업부별·지역별: XBRL 세그먼트가 어느 쪽인지 판별해 배치, 나머지는 설명 자료
    xrows = [{"ko": trm.get(x["name"], x["name"]), "rev": x["rev"] / 1e8 if x.get("rev") is not None else None,
              "rev_py": x["rev_py"] / 1e8 if x.get("rev_py") is not None else None, "op": x["op"] / 1e8 if x.get("op") is not None else None,
              "op_py": x["op_py"] / 1e8 if x.get("op_py") is not None else None} for x in segs]
    per_lab = f"{'누적' if r.get('q', 4) > 1 else ''} {r.get('start', '')[:7]}~{r.get('end', '')[:7]}".strip()
    total_cum = r["rev"] / 1e8 if r.get("rev") else None
    biz_rows, biz_src, biz_per = [], "", ""
    reg_rows, reg_src, reg_per = [], "", ""
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
        if "누적" in (per or "") and total_cum: return total_cum
        if per and re.search(r"[1-4]Q\s*\(|3개월|분기", per) and q.get("rev"): return q["rev"] / 100
        return total_cum
    biz_html, _ = seg_section(plt, "사업부별 실적", biz_rows, biz_per, biz_src, total_for(biz_per))
    reg_html, _ = seg_section(plt, "지역별 실적", reg_rows, reg_per, reg_src, total_for(reg_per))
    if not biz_html: biz_html = "<h2>사업부별 실적</h2><p class=muted>결산단신·설명 자료에 사업부 구분 숫자가 없습니다(단일 사업 회사일 수 있음).</p>"
    if not reg_html: reg_html = "<h2>지역별 실적</h2><p class=muted>결산단신·설명 자료에 지역 구분 숫자가 없습니다.</p>"

    # 6) IR 자료에서 고른 핵심 데이터
    ins_html = ""
    base_pdf = mat_pdf or tan_pdf
    for it in (ai.get("insights") or [])[:6]:
        if not isinstance(it, dict) or not it.get("title"): continue
        img = insight_chart(plt, it.get("chart") or {}, it["title"])
        pg = page_image(base_pdf, it.get("page")) if base_pdf and it.get("page") else None
        ins_html += (f"<div class=card><h3>{esc(it['title'])}</h3>" + (f"<p><b>{esc(it.get('takeaway'))}</b></p>" if it.get("takeaway") else "")
                     + (f"<p class=muted>왜 중요한가: {esc(it.get('why'))}</p>" if it.get("why") else "")
                     + (f'<img src="{img}">' if img else "")
                     + (f"<details><summary>원문 {esc(it.get('page'))}쪽 보기</summary><img src=\"{pg}\"></details>" if pg else "") + "</div>")
    if ins_html:
        ins_html = (f"<h2>IR 자료에서 고른 핵심 데이터</h2><p class=muted>출처: {esc(src_name)}({esc(mat_src if mat_pdf else 'TDnet')}) — Gemini가 의미 있는 표를 골라 숫자를 뽑고, "
                    "한국어 차트로 다시 그렸습니다. 숫자는 원문 쪽 이미지로 확인하세요.</p>" + ins_html)
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
    if att["pl"]:
        rows = []
        rev = pl_pick(att["pl"], *PL_ROWS[0][1])
        for lab, pats in PL_ROWS:
            v = pl_pick(att["pl"], *pats)
            if v[0] is None and v[1] is None: continue
            ratio = lambda a, b: f"{a / b * 100:.1f}%" if a is not None and b else "—"
            rows.append(f"<tr><td>{lab}</td><td>{E.oku(v[1] / 1e6 if v[1] is not None else None)}</td><td>{ratio(v[1], rev[1])}</td>"
                        f"<td>{E.oku(v[0] / 1e6 if v[0] is not None else None)}</td><td>{ratio(v[0], rev[0])}</td><td>{fmt(pct(v[0], v[1]))}%</td></tr>")
        pl_html = (f"<details><summary>누적 손익 상세 ({esc(r.get('start', '')[:7])}~{esc(r.get('end', '')[:7])}, 결산단신 손익계산서)</summary><div class=scroll><table>"
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
    links += [f"<a href='{esc(u)}'>{esc(n)}</a>" for n, u in rec_links]
    headline = ai.get("headline") or cardai.get("summary") or ""
    lab, months = E.qlabel(s[-1], fem)
    tail = ""
    if ai.get("guidance") or ai.get("risks") or watch:
        tail = ("<h2>가이던스 · 리스크 · 다음에 확인할 점</h2><div class=card>" + (f"<p><b>가이던스</b> {esc(ai.get('guidance'))}</p>" if ai.get("guidance") else "")
                + (f"<p><b>리스크</b> {esc(ai.get('risks'))}</p>" if ai.get("risks") else "") + (f"<ul>{watch}</ul>" if watch else "") + "</div>")
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
</style></head><body><main>
<h1>{esc(name)} <small>({code} JP · {esc(E.SECTOR_KO.get(card['sector'], card['sector']))})</small></h1>
<p class=muted>{lab}({months}) 실적 · 발표 {card['d'][:4]}-{card['d'][4:6]}-{card['d'][6:]} {card['t']} JST · {' · '.join(card.get('hit') or []) or '요청 분석'}</p>
{f"<div class=card>{esc(ai.get('overview') or cardai.get('bm'))}</div>" if (ai.get('overview') or cardai.get('bm')) else ''}
<div class=kpi>{''.join(f'<div><span>{k}</span><b>{v}</b></div>' for k, v in kpi)}</div>
{f"<div class=head><b>{esc(headline)}</b><p>{esc(ai.get('narrative'))}</p></div>" if headline else ''}
{f"<div class=card><ul>{why}</ul>{rx_html}</div>" if (why or rx_html) else ''}
{biz_html}
{reg_html}
{ins_html}
{qa_html}
{op_html}
<h2>분기 실적 (3개월 단독, {esc(r.get('scope') or '')})</h2><div class=scroll><table>
<tr><th>분기</th><th>발표일</th><th>매출</th><th>YoY</th><th>영업이익</th><th>YoY</th><th>영업이익률</th><th>순이익</th><th>YoY</th></tr>{''.join(qrows)}</table></div>
{pl_html}
<h2>연간 실적</h2><div class=scroll><table><tr><th>회계연도</th><th>매출</th><th>영업이익</th><th>영업이익률</th><th>순이익</th><th>EPS</th><th>배당</th></tr>{''.join(yrows)}</table></div>
{''.join(f'<figure><img src="{c}"></figure>' for c in charts)}
{tail}
{holder_html}
<h2>원문</h2><p>{' · '.join(links)}</p>
<p class=muted>출처: TDnet(결산단신 XBRL·PDF, 설명 자료), 회사 IR 홈페이지, 로그미 파이낸스, 가부탄(실적·시가총액·대주주), 네이버 증권(주가). 금액 억 엔(원자료 백만 엔 환산).
결론·요약·번역과 설명 자료에서 뽑은 숫자는 Gemini가 원문만 읽고 작성 — 숫자는 원문 쪽 이미지·링크로 확인하세요. 작성 {E.now():%Y-%m-%d %H:%M} JST</p>
</main></body></html>"""
    return f"{code}_{card['d']}_report.html", doc.encode("utf-8")
