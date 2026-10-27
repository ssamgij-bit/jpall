"""실적 스크리닝 카드의 [📄 리포트] 버튼 → 한 파일짜리 HTML 리포트(차트 내장)

구성
 1. 머리말: 회사 한 줄 설명, KPI(시총, 최근 연도 매출, PER(가이던스), 배당·배당수익률)
 2. 대표자·대주주(접기)
 3. 이번 실적 한 줄 결론 + 설명(Gemini, 결산단신 본문·수치 근거)
 4. 분기 실적 표(최근 8개 분기, 3개월 단독) · 누적 손익 상세(매출총이익·판관비·영업외·특별손익, 접기) · 연간 실적 표(가이던스 포함)
 5. 차트: 주가 1년과 실적 발표일 반응 / 분기 매출·YoY / 분기 영업이익·YoY·영업이익률
          / 부문(지역)별 매출 성장 기여(%p) / 부문(지역)별 영업이익 성장 기여(%p)  ← 결산단신 XBRL 세그먼트 정보
 6. 결산 설명 자료: 핵심 표(제품·지역·용도별 매출, 수주·수주잔고·BB, 실적 예상 수정, 배당)를 한국어 표로 옮기고 요약(Gemini, PDF 원문)
 7. 원문 링크·출처
"""
import io, os, re, json, time, base64, html, zipfile, datetime as dt

import earn as E
import xbrl as X
from jpall import http, gemini_json, GEMINI_MODELS, nfkc

def b64png(fig):
    buf = io.BytesIO(); fig.savefig(buf, format="png", dpi=150, facecolor="white", bbox_inches="tight"); E._plt().close(fig)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()

def esc(s): return html.escape(str(s if s is not None else ""), quote=False)

def gemini_pdf(prompt, pdfs, max_tokens=12000):
    key = os.environ.get("GEMINI_API_KEY")
    pdfs = [p for p in pdfs if p]
    if not key or not pdfs: return None
    parts = [{"inline_data": {"mime_type": "application/pdf", "data": base64.b64encode(p).decode()}} for p in pdfs] + [{"text": prompt}]
    body = json.dumps({"contents": [{"parts": parts}],
                       "generationConfig": {"temperature": 0.2, "maxOutputTokens": max_tokens, "responseMimeType": "application/json"}}).encode()
    for model in GEMINI_MODELS:
        try:
            r = json.loads(http(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}",
                                timeout=240, data=body, headers={"content-type": "application/json"}))
            txt = "".join(p.get("text", "") for p in r["candidates"][0]["content"]["parts"])
            out = json.loads(re.sub(r"^```(json)?|```$", "", txt.strip()))
            if isinstance(out, list): out = out[0] if out and isinstance(out[0], dict) else None
            return out
        except Exception as e:
            E.log("gemini pdf error", model, str(e)[:150])
            if "429" in str(e) or "503" in str(e): time.sleep(4)
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
TEXT_PROMPT = """너는 일본 중소형주를 보는 한국 애널리스트다. 아래 자료로 한국어 JSON을 써라. 자료에 없는 내용은 쓰지 말 것. 일본어를 그대로 쓰지 말 것.
1) overview: 회사가 무엇을 만들어/팔아 누구에게 파는지 2~3문장(주요 제품·고객·지역).
2) headline: 이번 실적의 핵심 결론 한 줄(40자 이내, 숫자 하나 포함). 예: "물량·판가가 원가 상승을 넘음 — 2Q 영업이익률 12.7%"
3) narrative: 왜 그런 실적이 나왔는지 3~5문장. 부문·지역 숫자, 가격·물량·비용 요인, 가이던스 변화와 이유.
4) watch: 다음 분기에 확인할 점 2~3개(각 40자 이내), 리스크 포함.
5) tr: 아래 '번역할 말' 각각의 한국어(회사명·은행명은 통용 한국어 표기, 예: 日本マスタートラスト信託銀行(信託口) → 일본마스터트러스트신탁은행(신탁계정)).
출력: {{"overview": "", "headline": "", "narrative": "", "watch": [""], "tr": {{"원문": "한국어"}}}}

회사: {name}({code}) · 업종 {sector}
회사 개요(일본어): {profile}
수치(억 엔): {nums}
부문(누적): {segs}
번역할 말: {words}
결산단신 본문:
{qual}
"""

MAT_PROMPT = """첨부 PDF는 일본 상장사의 결산 설명 자료(없으면 결산단신)다. 한국 애널리스트가 읽을 수 있게 한국어 JSON으로 정리하라.
규칙: 자료에 있는 내용·숫자만. 일본어는 모두 한국어로(제품·지역명은 한국어, 통용 영문은 영문). 단위는 자료 그대로 쓰되 백만 엔이면 억 엔으로 바꿔도 됨(1억=100백만).
- tables: 자료의 핵심 표를 최대 6개까지 옮긴다. 우선순위: ①결산 하이라이트(매출·이익 전년 대비) ②제품·부문별 매출 ③지역별 매출 ④용도·고객 산업별 매출
  ⑤수주액·수주잔고·BB 레시오 ⑥실적 예상 수정(이전→수정) ⑦배당. 표마다 title, unit, columns(열 이름), rows(각 행은 [항목, 값...] 문자열).
- charts: tables 중 '항목별 매출 전년 대비'처럼 막대로 그리기 좋은 것 최대 2개를 숫자로: {{"title", "unit", "labels": [], "prev": [숫자], "cur": [숫자], "prev_label": "전년", "cur_label": "올해"}}
- points: 경영진이 강조한 내용 5개 이내(각 80자 이내). risks: 회사가 언급한 하방 요인(있으면).
출력: {{"title": "자료 제목(한국어)", "one_line": "", "points": [""], "tables": [{{"title": "", "unit": "", "columns": [""], "rows": [[""]]}}], "charts": [], "risks": ""}}
"""

def fmt(v, d=1):
    return "—" if v is None else f"{v:,.{d}f}"

def pct(a, b):
    return (a / b - 1) * 100 if a is not None and b not in (None, 0) and b > 0 else None

# ───────────────────────── 차트 ─────────────────────────
def contrib_chart(plt, segs, key, title, total_label):
    """부문별 성장 기여(%p) = (부문 당기 − 전기) ÷ 전사 전기 합계 × 100"""
    rows = [s for s in segs if s.get(key) is not None and s.get(key + "_py") is not None]
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
    for yi, v in zip(y, vals):
        ax.text(v + (0.3 if v >= 0 else -0.3), yi, f"{v:+.1f}%p", va="center", ha="left" if v >= 0 else "right", fontsize=10, fontweight="bold")
    ax.set_yticks(y); ax.set_yticklabels(labels, fontsize=11)
    ax.axvline(0, color="#999", lw=0.8)
    ax.set_title(title, loc="left", fontsize=12, fontweight="bold")
    for sp in ("top", "right"): ax.spines[sp].set_visible(False)
    lo, hi = min(vals + [0]), max(vals + [0]); pad = (hi - lo) * 0.25 + 1
    ax.set_xlim(lo - pad, hi + pad)
    ax.grid(axis="x", color="#eee")
    return b64png(fig)

def compare_chart(plt, ch):
    try:
        labels = [str(x) for x in ch["labels"]]; prev = [float(x) for x in ch["prev"]]; cur = [float(x) for x in ch["cur"]]
        if not labels or len(labels) != len(prev) or len(labels) != len(cur): return None
    except Exception:
        return None
    import numpy as np
    x = np.arange(len(labels))
    fig, ax = plt.subplots(figsize=(8, 3.6))
    ax.bar(x - 0.2, prev, 0.4, color="#b9b6ae", label=ch.get("prev_label") or "전년")
    ax.bar(x + 0.2, cur, 0.4, color="#2f6fd6", label=ch.get("cur_label") or "올해")
    for i, (a, b) in enumerate(zip(prev, cur)):
        if a: ax.text(i + 0.2, b, f"{(b / a - 1) * 100:+.0f}%", ha="center", va="bottom", fontsize=9, color="#e8743b", fontweight="bold")
    ax.set_xticks(x); ax.set_xticklabels(labels, fontsize=9, rotation=0 if len(labels) <= 6 else 30)
    ax.set_title(f"{ch.get('title', '')} ({ch.get('unit', '')})", loc="left", fontsize=12, fontweight="bold")
    ax.legend(frameon=False, fontsize=9)
    for sp in ("top", "right"): ax.spines[sp].set_visible(False)
    ax.grid(axis="y", color="#eee")
    return b64png(fig)

# ───────────────────────── 본체 ─────────────────────────
def build(card):
    code = card["code"]; fem = card["fem"]
    name = card.get("name_ko") or card["name"]
    kb = E.kb_finance(code)
    plt = E._plt()
    series = card["series"]
    full = {q["end"]: q for q in kb["q"]}; full.update({q["end"]: q for q in series})
    allq = [full[k] for k in sorted(full)]
    s = allq[-8:]
    r = card["r"]; fc = r.get("fc") or {}

    # 1) XBRL 첨부(손익 상세·세그먼트)
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
    # 3) 결론·번역(Gemini)
    q = s[-1]; py = full.get(E.ym_add(q["end"], -12)) or {}
    nums = (f"이번 분기 매출 {E.oku(q.get('rev'))}(전년 {E.oku(py.get('rev'))}), 영업이익 {E.oku(q.get('op'))}(전년 {E.oku(py.get('op'))}), "
            f"순이익 {E.oku(q.get('ni'))}; 연간 예상 영업이익 {E.oku(fc['op'] / 1e6) if fc.get('op') else '없음'}")
    seg_txt = "; ".join(f"{x['name']} 매출 {E.oku((x['rev'] or 0) / 1e6)}(전년 {E.oku((x['rev_py'] or 0) / 1e6)}) 영익 {E.oku((x['op'] or 0) / 1e6)}(전년 {E.oku((x['op_py'] or 0) / 1e6)})" for x in segs)
    words = [x["name"] for x in segs] + [h[0] for h in holders]
    ai = gemini_json(TEXT_PROMPT.format(name=name, code=code, sector=E.SECTOR_KO.get(card["sector"], card["sector"]), profile=card.get("profile", "")[:500],
                                        nums=nums, segs=seg_txt or "없음", words=json.dumps(words, ensure_ascii=False), qual=card.get("qual", "")[:6000]), timeout=150) or {}
    if isinstance(ai, list): ai = ai[0] if ai and isinstance(ai[0], dict) else {}
    trm = {w: KO_COMMON[w] for w in words if w in KO_COMMON}
    trm.update({k: v for k, v in (ai.get("tr") or {}).items() if v})
    miss = [w for w in words if w not in trm or re.search(r"[぀-ヿ一-鿿]", trm.get(w) or "")]
    if miss: trm.update(tr_google(miss))
    for x in segs: x["ko"] = trm.get(x["name"], x["name"])
    # 4) 설명 자료(Gemini, PDF)
    mat = None
    try:
        src = card.get("mat") or card.get("pdf")
        pdfs = [http(src, timeout=60)] if src else []
        mat = gemini_pdf(MAT_PROMPT, pdfs)
    except Exception as e:
        E.log("material error", e)

    # ── 차트
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
        ax.set_title("① 주가 1년과 실적 발표일 (점선=발표일, 숫자=다음 날 종가 변화)", loc="left", fontsize=12, fontweight="bold")
        ax.set_ylabel("엔", fontsize=9)
        for sp in ("top", "right"): ax.spines[sp].set_visible(False)
        ax.grid(color="#eee"); fig.tight_layout()
        charts.append(("", b64png(fig)))
    except Exception as e:
        E.log("price chart error", e)
    labs = []
    for qq in s:
        fy, n = E.fy_of(qq["end"], fem); labs.append(f"{n}Q\n{fy}")
    for key, title in (("rev", "② 분기 매출과 YoY"), ("op", "③ 분기 영업이익 · YoY · 영업이익률")):
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
        fig.tight_layout(); charts.append(("", b64png(fig)))
    region = segs and sum(bool(re.search(r"日本|国内|アジア|中国|米|欧|北米|南米|海外|欧州|アメリカ|ヨーロッパ|Japan|Asia|America|Europe", x["name"])) for x in segs) >= len(segs) / 2
    unit = "지역" if region else "부문"
    per_lab = "누적" if r.get("q", 4) > 1 else "분기"
    if segs:
        c1 = contrib_chart(plt, segs, "rev", f"④ {unit}별 매출 성장 기여 ({per_lab} 전년 대비, %p)", "전사 매출 YoY")
        c2 = contrib_chart(plt, segs, "op", f"⑤ {unit}별 영업이익 성장 기여 ({per_lab} 전년 대비, %p)", "부문 합계 YoY")
        if c1: charts.append((f"막대 = (그 {unit}의 올해 − 작년 매출) ÷ 작년 전사 매출. 합계 = 외부 고객 매출 기준 YoY.", c1))
        if c2: charts.append((f"막대 = (그 {unit}의 올해 − 작년 영업이익) ÷ 작년 {unit} 영업이익 합계(본사 비용·조정 전).", c2))
    for ch in (mat or {}).get("charts") or []:
        img = compare_chart(plt, ch) if isinstance(ch, dict) else None
        if img: charts.append(("설명 자료 표를 옮겨 그린 것(Gemini 추출) — 숫자는 원문으로 확인하세요.", img))

    # ── 표
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
            yy = pct(v[0], v[1])
            rows.append(f"<tr><td>{lab}</td><td>{E.oku(v[1] / 1e6 if v[1] is not None else None)}</td><td>{ratio(v[1], rev[1])}</td>"
                        f"<td>{E.oku(v[0] / 1e6 if v[0] is not None else None)}</td><td>{ratio(v[0], rev[0])}</td><td>{fmt(yy)}%</td></tr>")
        pl_html = (f"<details open><summary>누적 손익 상세 ({esc(r.get('start', '')[:7])}~{esc(r.get('end', '')[:7])}, 결산단신 손익계산서)</summary><div class=scroll><table>"
                   "<tr><th>항목</th><th>작년 누적</th><th>매출 대비</th><th>올해 누적</th><th>매출 대비</th><th>YoY</th></tr>" + "".join(rows) + "</table></div></details>")
    seg_html = ""
    if segs:
        rows = []
        for x in segs:
            m0 = x["op"] / x["rev"] * 100 if x.get("op") is not None and x.get("rev") else None
            m1 = x["op_py"] / x["rev_py"] * 100 if x.get("op_py") is not None and x.get("rev_py") else None
            rows.append(f"<tr><td>{esc(x['ko'])}</td><td>{E.oku((x['rev_py'] or 0) / 1e6)}</td><td>{E.oku((x['rev'] or 0) / 1e6)}</td><td>{fmt(pct(x['rev'], x['rev_py']))}%</td>"
                        f"<td>{E.oku((x['op'] or 0) / 1e6) if x.get('op') is not None else '—'}</td><td>{fmt(pct(x['op'], x['op_py']))}%</td><td>{fmt(m1)}% → {fmt(m0)}%</td></tr>")
        seg_html = (f"<details open><summary>{unit}별 실적 ({per_lab}, 외부 고객 매출 기준, 결산단신 세그먼트 정보)</summary><div class=scroll><table>"
                    f"<tr><th>{unit}</th><th>작년 매출</th><th>올해 매출</th><th>YoY</th><th>올해 영업이익</th><th>YoY</th><th>영업이익률</th></tr>" + "".join(rows) + "</table></div></details>")
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
    mat_html = ""
    if mat:
        pts = "".join(f"<li>{esc(p)}</li>" for p in (mat.get("points") or []))
        tbls = ""
        for t in (mat.get("tables") or [])[:6]:
            if not isinstance(t, dict) or not t.get("rows"): continue
            cols = "".join(f"<th>{esc(c)}</th>" for c in (t.get("columns") or []))
            body = "".join("<tr>" + "".join(f"<td>{esc(c)}</td>" for c in (row if isinstance(row, list) else [row])) + "</tr>" for row in t["rows"])
            tbls += f"<h3>{esc(t.get('title'))} <small>{esc(t.get('unit'))}</small></h3><div class=scroll><table><tr>{cols}</tr>{body}</table></div>"
        mat_html = (f"<h2>결산 설명 자료 요약</h2><div class=card><p class=muted>{esc(mat.get('title'))}</p><p><b>{esc(mat.get('one_line'))}</b></p><ol>{pts}</ol>"
                    + (f"<p><b>리스크</b> {esc(mat.get('risks'))}</p>" if mat.get("risks") else "") + f"</div>{tbls}")
    rx = card.get("rx") if isinstance(card.get("rx"), dict) else None
    rx_html = f"<p class=rx>{E.reaction_line(rx)}</p>" if rx else ""
    watch = "".join(f"<li>{esc(w)}</li>" for w in (ai.get("watch") or []))
    links = (f"<a href='{card['pdf']}'>결산단신(원문 PDF)</a>" if card.get("pdf") else f"<a href='https://s.kabutan.jp/stocks/{code}/finance/'>가부탄 실적 페이지</a>") + (f" · <a href='{card['mat']}'>설명 자료(원문 PDF)</a>" if card.get("mat") else "")
    headline = ai.get("headline") or cardai.get("summary") or ""
    lab, months = E.qlabel(s[-1], fem)
    doc = f"""<!doctype html><html lang=ko><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>{esc(name)}({code}) 실적 리포트</title><style>
body{{font-family:-apple-system,'Apple SD Gothic Neo','Noto Sans KR','Malgun Gothic',sans-serif;margin:0;background:#f6f7f9;color:#1f2328;line-height:1.55}}
main{{max-width:880px;margin:0 auto;padding:16px}} h1{{font-size:22px;margin:8px 0 2px}} h2{{font-size:17px;margin:28px 0 8px;border-left:4px solid #2f6fd6;padding-left:8px}}
h3{{font-size:14px;margin:16px 0 6px}} .card{{background:#fff;border-radius:12px;padding:14px 16px;margin:10px 0;box-shadow:0 1px 2px rgba(0,0,0,.06)}}
.kpi{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:8px;margin:12px 0}} .kpi div{{background:#fff;border-radius:10px;padding:10px 12px;box-shadow:0 1px 2px rgba(0,0,0,.05)}}
.kpi small{{color:#6b7280;font-weight:400}} .kpi span{{color:#6b7280;font-size:12px}} .kpi b{{display:block;font-size:18px;margin-top:2px}}
.head{{background:#1f2328;color:#fff;border-radius:12px;padding:14px 16px;margin:12px 0}} .head b{{font-size:17px}} .head p{{margin:6px 0 0;color:#d0d4da}}
table{{border-collapse:collapse;width:100%;font-size:13px;background:#fff}} th,td{{border-bottom:1px solid #eee;padding:6px 7px;text-align:right;white-space:nowrap}}
th:first-child,td:first-child{{text-align:left}} th{{background:#f0f3f8;font-weight:600}} tr.cur td{{background:#eef4ff;font-weight:600}} .scroll{{overflow-x:auto;border-radius:10px}}
details{{background:#fff;border-radius:10px;margin:10px 0;padding:8px 12px}} summary{{cursor:pointer;font-weight:600}}
figure{{margin:10px 0;background:#fff;border-radius:10px;padding:8px}} figure img{{width:100%}} figcaption{{font-size:12px;color:#6b7280;padding:2px 6px}}
.muted,small{{color:#6b7280}} a{{color:#2f6fd6}} .rx{{background:#fff7ed;border-radius:8px;padding:6px 10px}}
</style></head><body><main>
<h1>{esc(name)} <small>({code} JP · {esc(E.SECTOR_KO.get(card['sector'], card['sector']))})</small></h1>
<p class=muted>{lab}({months}) 실적 · 발표 {card['d'][:4]}-{card['d'][4:6]}-{card['d'][6:]} {card['t']} JST · 기준 충족: {' · '.join(card.get('hit') or [])}</p>
{f"<div class=card>{esc(ai.get('overview') or cardai.get('bm'))}</div>" if (ai.get('overview') or cardai.get('bm')) else ''}
<div class=kpi>{''.join(f'<div><span>{k}</span><b>{v}</b></div>' for k, v in kpi)}</div>
{holder_html}
{f"<div class=head><b>{esc(headline)}</b><p>{esc(ai.get('narrative'))}</p></div>" if headline else ''}
{f"<div class=card><ul>{why}</ul>{rx_html}</div>" if (why or rx_html) else ''}
<h2>분기 실적 (3개월 단독, {esc(r.get('scope') or '')})</h2><div class=scroll><table>
<tr><th>분기</th><th>발표일</th><th>매출</th><th>YoY</th><th>영업이익</th><th>YoY</th><th>영업이익률</th><th>순이익</th><th>YoY</th></tr>{''.join(qrows)}</table></div>
{seg_html}{pl_html}
<h2>연간 실적</h2><div class=scroll><table><tr><th>회계연도</th><th>매출</th><th>영업이익</th><th>영업이익률</th><th>순이익</th><th>EPS</th><th>배당</th></tr>{''.join(yrows)}</table></div>
<h2>차트</h2>{''.join(f'<figure><img src="{c}">' + (f'<figcaption>{esc(cap)}</figcaption>' if cap else '') + '</figure>' for cap, c in charts)}
{mat_html}
{f"<h2>다음에 확인할 점</h2><div class=card><ul>{watch}</ul></div>" if watch else ''}
<h2>원문</h2><p>{links}</p>
<p class=muted>출처: TDnet 결산단신 XBRL(실적·손익계산서·세그먼트·가이던스), TDnet 결산 설명 자료, 가부탄(3개월·연간 실적, 시가총액, 대주주), 네이버 증권(주가 일봉).
금액 단위 억 엔(원자료 백만 엔 환산). 결론·요약·번역과 설명 자료 표는 Gemini가 원문만 읽고 작성 — 숫자는 원문 링크로 확인하세요. 작성 {E.now():%Y-%m-%d %H:%M} JST</p>
</main></body></html>"""
    return f"{code}_{card['d']}_report.html", doc.encode("utf-8")
