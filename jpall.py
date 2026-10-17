#!/usr/bin/env python3
"""JP_ALL: 도쿄증시 전 종목 적시공시 → 텔레그램 채널 자동 게시 (GitHub Actions에서 한 번에 약 28분씩 이어서 실행)

- 공시 목록: TDnet (https://www.release.tdnet.info/inbs/)
- 결산·실적수정·배당수정 수치: TDnet XBRL
- 최근 분기 실적 추이: 가부탄(kabutan.jp) 3개월 실적표
- 컨센서스: Yahoo Finance 애널리스트 추정치(매출·EPS, 발표 며칠 전에 미리 저장)
- 제목 번역: Google 번역(키 없는 공개 주소) + 용어 보정
환경 변수: TELEGRAM_TOKEN, CHANNEL_ID(@채널아이디 또는 -100...), RUN_SECONDS(기본 1680), DRY_RUN(1이면 게시 안 하고 출력)
"""
import os, sys, re, io, json, time, datetime as dt, unicodedata, urllib.request, urllib.parse, traceback

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from jp2ko import jp2ko
import xbrl as X

JST = dt.timezone(dt.timedelta(hours=9))
TDNET = "https://www.release.tdnet.info/inbs/"
STATE_DIR = os.environ.get("STATE_DIR", "state")
RUN_SECONDS = int(os.environ.get("RUN_SECONDS", "1680"))
DRY = os.environ.get("DRY_RUN") == "1"
TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHANNEL = os.environ.get("CHANNEL_ID", "")
SEND_GAP = 3.3          # 채널 게시 간격(초) — 텔레그램 채널 한도(분당 약 20건) 대응
UA = {"User-Agent": "Mozilla/5.0 (JP_ALL disclosure bot)"}
SKIP = ["日々の開示事項", "基準価額と市場価格"]  # ETF·ETN 일일 공시(수십 건/일, 내용 없음)
DIGEST_HOUR = int(os.environ.get("DIGEST_HOUR", "19"))  # 하이라이트 요약 발송 시각(JST=KST)
HL = {}  # 공시 1건 처리 중 본문 함수가 남기는 핵심 수치(하이라이트 판정용)

def now(): return dt.datetime.now(JST)
def nfkc(s): return unicodedata.normalize("NFKC", s or "")
def http(url, timeout=30, data=None, headers=None):
    req = urllib.request.Request(url, data=data, headers={**UA, **(headers or {})})
    return urllib.request.urlopen(req, timeout=timeout).read()
def log(*a): print(now().strftime("%H:%M:%S"), *a, flush=True)

# ───────────────────────── 상태 ─────────────────────────
def load(name, default):
    try: return json.load(open(os.path.join(STATE_DIR, name)))
    except Exception: return default
def save(name, obj):
    os.makedirs(STATE_DIR, exist_ok=True)
    json.dump(obj, open(os.path.join(STATE_DIR, name), "w"), ensure_ascii=False, separators=(",", ":"))

# ───────────────────────── 종목 목록(JPX) ─────────────────────────
PLACE = {"東": "도쿄", "名": "나고야", "福": "후쿠오카", "札": "삿포로"}
MKT = {"プライム": "프라임", "スタンダード": "스탠다드", "グロース": "그로스"}
def load_names(st):
    names = load("names.json", {})
    if names and st.get("names_date") == now().strftime("%Y-%m-%d"):
        return names
    try:
        page = http("https://www.jpx.co.jp/markets/statistics-equities/misc/01.html").decode("utf-8", "ignore")
        m = re.search(r'href="([^"]*data_j\.xlsx?)"', page)
        df = pd.read_excel(io.BytesIO(http("https://www.jpx.co.jp" + m.group(1))), dtype={"コード": str})
        names = {}
        for r in df.itertuples():
            code = str(r[2]); nm = nfkc(str(r[3])); seg = str(r[4])
            mk = next((v for k, v in MKT.items() if k in seg), "리츠" if "REIT" in seg else "ETF" if "ETF" in seg or "ETN" in seg else seg[:6])
            names[code] = {"jp": nm, "ko": jp2ko(nm), "mkt": mk}
        save("names.json", names); st["names_date"] = now().strftime("%Y-%m-%d")
        log("names refreshed", len(names))
    except Exception as e:
        log("names error", e)
    return names

# ───────────────────────── 번역 ─────────────────────────
FIX = [(r"자기 ?주식", "자사주"), (r"에 관한 (통지|공지|알림|안내|공고)(서)?$", " 안내"), (r"에 관한 (통지|공지|알림)", " 안내"), (r"실적 예상", "실적 전망"),
       (r"배당 예상", "배당 전망"), (r"통기", "연간"), (r"부정적인? 영업권 발생 ?이익|음의 영업권 발생 ?이익", "염가매수차익"), (r"결산 ?단신", "결산단신"),
       (r"상향 수정", "상향"), (r"하향 수정", "하향"), (r"FY ?(\d{1,2}) ?/ ?(\d{2})", r"FY\1/\2"), (r"공개 ?(매매|매입|매수)", "공개매수"), (r"자회사의 이동", "자회사 변경"), (r"주권 등", "주식"), (r"양도 ?제한 ?(첨부|이 ?있는|부|付) ?주식 ?보상", "양도제한부 주식보상(RS)"), (r"새로운 주식 발행|신주식 발행", "신주 발행"), (r"지불 완료", "납입 완료"), (r"\s+", " ")]
def pre_tr(t):
    t = nfkc(t)
    t = re.sub(r"(\d{4})\s*年\s*(\d{1,2})\s*月期", lambda m: f"FY{int(m.group(2))}/{m.group(1)[2:]} ", t)
    t = re.sub(r"第\s*([1-4])\s*四半期", lambda m: f"{m.group(1)}Q ", t)
    return t
def translate(st, titles):
    cache = st.setdefault("tr", {})
    need = [t for t in dict.fromkeys(titles) if t and t not in cache]
    for i in range(0, len(need), 20):
        part = need[i:i + 20]
        q = "\n".join(pre_tr(t) for t in part)
        try:
            url = "https://translate.googleapis.com/translate_a/single?client=gtx&sl=ja&tl=ko&dt=t&q=" + urllib.parse.quote(q)
            r = json.loads(http(url, timeout=20))
            out = "".join(x[0] for x in r[0]).split("\n")
            if len(out) == len(part):
                for src, ko in zip(part, out):
                    for a, b in FIX: ko = re.sub(a, b, ko).strip()
                    cache[src] = ko
        except Exception as e:
            log("translate error", e); break
        time.sleep(0.5)
    if len(cache) > 4000:
        for k in list(cache)[:len(cache) - 3000]: cache.pop(k, None)
    return {t: cache.get(t) for t in titles}

# ───────────────────────── TDnet 목록 ─────────────────────────
ROW = re.compile(r'kjTime"[^>]*>([^<]*)<.*?kjCode"[^>]*>([^<]*)<.*?kjName"[^>]*>([^<]*)<.*?kjTitle"[^>]*><a href="([^"]+)"[^>]*>(.*?)</a>.*?kjXbrl"[^>]*>(.*?)</td>.*?kjPlace"[^>]*>([^<]*)<', re.S)
def scan_tdnet(day):
    rows = []
    for pg in range(1, 60):
        try:
            html = http(f"{TDNET}I_list_{pg:03d}_{day}.html").decode("utf-8", "ignore")
        except Exception:
            break
        got = ROW.findall(html)
        if not got: break
        for t, code, name, href, title, xb, place in got:
            x = re.search(r'href="([^"]+)"', xb)
            rows.append({"id": href, "d": day, "t": t.strip(), "code": code.strip()[:4], "name": nfkc(name).strip(),
                         "title": nfkc(re.sub(r"<[^>]+>", "", title)).strip(), "pdf": TDNET + href,
                         "xbrl": TDNET + x.group(1) if x else None, "place": nfkc(place).strip()})
    return rows  # 최신 순

# ───────────────────────── 시가총액(Yahoo) ─────────────────────────
_YF = None
def yf_quotes(codes):
    global _YF
    out = {}
    try:
        if _YF is None:
            from yfinance.data import YfData
            _YF = YfData()
        codes = list(dict.fromkeys(codes))
        for i in range(0, len(codes), 150):
            r = _YF.get("https://query1.finance.yahoo.com/v7/finance/quote", params={"symbols": ",".join(c + ".T" for c in codes[i:i + 150])})
            if r.status_code == 200:
                for q in r.json()["quoteResponse"]["result"]:
                    out[q["symbol"][:-2]] = q
    except Exception as e:
        log("quote error", e)
    return out

# ───────────────────────── 컨센서스(Yahoo) 미리 저장 ─────────────────────────
def consensus_scan(st, names):
    """하루 한 번: 4일 안에 실적 발표 예정인 종목을 찾아 대기열에 넣음"""
    today = now().strftime("%Y-%m-%d")
    if st.get("cons_scan") == today or now().hour < 6: return
    codes = [c for c, v in names.items() if v["mkt"] in ("프라임", "스탠다드", "그로스")]
    q = yf_quotes(codes)
    t0, t1 = time.time() - 86400, time.time() + 86400 * 4
    pend = [c for c, v in q.items() if v.get("earningsTimestampStart") and t0 <= v["earningsTimestampStart"] <= t1]
    st["cons_q"] = list(dict.fromkeys(st.get("cons_q", []) + pend))
    st["cons_scan"] = today
    log("consensus queue", len(st["cons_q"]))
def consensus_work(st, limit=60):
    import yfinance as yf
    cons = st.setdefault("cons", {})
    qd = st.get("cons_q", [])
    done = 0
    while qd and done < limit:
        c = qd.pop(0); done += 1
        try:
            tk = yf.Ticker(c + ".T")
            re_ = tk.revenue_estimate; ee = tk.earnings_estimate
            rec = {"ts": time.time()}
            if re_ is not None and "0q" in re_.index and re_.loc["0q", "avg"] == re_.loc["0q", "avg"]:
                rec["rev"] = float(re_.loc["0q", "avg"]); rec["rev_n"] = int(re_.loc["0q", "numberOfAnalysts"] or 0)
            if ee is not None and "0q" in ee.index and ee.loc["0q", "avg"] == ee.loc["0q", "avg"]:
                rec["eps"] = float(ee.loc["0q", "avg"]); rec["eps_n"] = int(ee.loc["0q", "numberOfAnalysts"] or 0)
            if re_ is not None and "+1y" in re_.index and re_.loc["+1y", "avg"] == re_.loc["+1y", "avg"]:
                rec["rev_y1"] = float(re_.loc["+1y", "avg"])
            if ee is not None and "+1y" in ee.index and ee.loc["+1y", "avg"] == ee.loc["+1y", "avg"]:
                rec["eps_y1"] = float(ee.loc["+1y", "avg"]); rec["eps_y1_n"] = int(ee.loc["+1y", "numberOfAnalysts"] or 0)
            if len(rec) > 1: cons[c] = rec
        except Exception as e:
            log("cons error", c, e)
    for k in [k for k, v in cons.items() if time.time() - v["ts"] > 86400 * 10]: cons.pop(k)

# ───────────────────────── 가부탄 분기 실적 ─────────────────────────
def kabutan_quarters(code):
    """[(시작일, 종료일, 매출, 영업익, 경상익, 순이익, EPS)] 백만엔 단위, 오래된 순"""
    try:
        s = http(f"https://kabutan.jp/stock/finance?code={code}", timeout=20).decode("utf-8", "ignore")
    except Exception:
        return []
    i = s.find("3ヵ月決算");
    if i < 0: return []
    seg = s[i:i + 20000]
    j = seg.find("前年同期比")
    seg = seg[:j] if j > 0 else seg
    out = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", seg, re.S):
        cells = [nfkc(re.sub(r"<[^>]+>", "", c)).replace("\xa0", " ").strip() for c in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", tr, re.S)]
        if not cells: continue
        m = re.search(r"(\d{2})\.(\d{2})-(\d{2})", cells[0])
        if not m: continue
        y, m1, m2 = 2000 + int(m.group(1)), int(m.group(2)), int(m.group(3))
        y2 = y + (1 if m2 < m1 else 0)
        nums = []
        for c in cells[1:7]:
            c = c.replace(",", "").replace("+", "")
            try: nums.append(float(c))
            except Exception: nums.append(None)
        while len(nums) < 6: nums.append(None)
        out.append((dt.date(y, m1, 1), dt.date(y2, m2, 28), *nums[:5]))
    return out

# ───────────────────────── 서식 ─────────────────────────
def oku(v):
    """엔 → 억엔 문자열"""
    if v is None: return "—"
    x = v / 1e8
    if abs(x) >= 100: return f"{x:,.0f}억"
    if abs(x) >= 1: return f"{x:,.1f}억"
    return f"{x:,.2f}억"
def mo(v):  # 백만엔 → 억엔
    return oku(v * 1e6) if v is not None else "—"
def pct(a, b):
    if a is None or b is None or b == 0: return None
    return (a / b - 1) * 100
def chg_txt(cur, prev, given=None):
    if cur is None or prev is None: return ""
    if prev < 0 <= cur: return "흑자전환"
    if prev >= 0 > cur: return "적자전환"
    if prev < 0 and cur < 0: return "적자지속"
    p = given * 100 if given is not None else pct(cur, prev)
    if p is None: return ""
    return f"{p / 100 + 1:.1f}배" if p >= 900 else f"{p:+.1f}%"
def fy_label(end):
    try:
        d = dt.date.fromisoformat(end); return f"FY{d.month}/{str(d.year)[2:]}"
    except Exception: return ""
def mcap_txt(q):
    mc = (q or {}).get("marketCap")
    if not mc: return ""
    x = mc / 1e8
    return f"{x/1e4:,.1f}조엔" if x >= 1e4 else f"{x:,.0f}억엔"

def hist_progress(kq, start, q, idx):
    """가부탄 분기표로 과거 회계연도의 같은 분기 누적 진척률(누적 이익 ÷ 연간 실적, %) 목록"""
    out = []
    for back in (1, 2):
        s0 = dt.date(start.year - back, start.month, 1); s1 = dt.date(start.year - back + 1, start.month, 1)
        qs = sorted([k for k in kq if s0 <= k[0] < s1], key=lambda k: k[0])
        if len(qs) != 4 or any(k[idx] is None for k in qs): continue
        full = sum(k[idx] for k in qs)
        if full <= 0: continue
        out.append(sum(k[idx] for k in qs[:q]) / full * 100)
    return out

# ───────────────────────── 공시 종류별 본문 ─────────────────────────
def body_results(row, st):
    blob = http(row["xbrl"], timeout=40)
    kind, facts, ctx = X.open_zip(blob)
    if not kind or not (kind.endswith("sm") or kind.endswith("sy")): return None
    r = X.summarize_results(kind, facts, ctx)
    if not r: return None
    ordlab = "세전이익" if kind[4:6] in ("if", "us") else "경상익"
    q = r["q"]; start = dt.date.fromisoformat(r["start"]); end = dt.date.fromisoformat(r["end"])
    kq = kabutan_quarters(row["code"])
    # 같은 회계연도의 이전 분기(가부탄)로 이번 분기 단독 실적 계산
    prev = [k for k in kq if k[0] >= start and k[1] < end.replace(day=1)]
    single = None
    if q == 1:
        single = {m: r[m] for m in ["rev", "op", "ord", "ni", "eps"]}
    elif len(prev) == q - 1 and all(k[2] is not None for k in prev):
        single = {}
        for idx, m in [(2, "rev"), (3, "op"), (4, "ord"), (5, "ni"), (6, "eps")]:
            vals = [k[idx] for k in prev]
            cur = r[m]
            if cur is None or any(v is None for v in vals): single[m] = None; continue
            single[m] = cur - (sum(vals) * (1 if m == "eps" else 1e6))
    # 전년 같은 분기(가부탄)
    py = next((k for k in kq if k[1].month == end.month and k[1].year == end.year - 1), None)
    lines = []
    qlabel = f"{q}Q" if q < 4 else "4Q"
    cons = st.get("cons", {}).get(row["code"])
    if cons and time.time() - cons["ts"] > 86400 * 8: cons = None
    HL.update(cons_all=cons or {})
    if single:
        m0 = (end.month - 2 - 1) % 12 + 1
        lines.append(f"<b>[{qlabel} 단독 · {m0}~{end.month}월 · {r['scope']}]</b>")
        for m, lab in [("rev", "매출액"), ("op", "영업익"), ("ord", ordlab), ("ni", "순이익")]:
            v = single.get(m)
            if v is None and m == "ord": continue
            extra = []
            if py:
                pv = {"rev": py[2], "op": py[3], "ord": py[4], "ni": py[5]}[m]
                c = chg_txt(v, pv * 1e6 if pv is not None else None)
                if c: extra.append(f"YoY {c}")
            if m == "rev" and cons and cons.get("rev") and v is not None:
                extra.append(f"예상치 {oku(cons['rev'])} / {pct(v, cons['rev']):+.0f}%")
            lines.append(f"{lab} : {oku(v)}" + (f" ({' · '.join(extra)})" if extra else ""))
        if single.get("eps") is not None:
            e = single["eps"]; ex = ""
            if cons and cons.get("eps"):
                ex = f" (예상치 {cons['eps']:,.1f}엔 / {pct(e, cons['eps']):+.0f}%)" if cons["eps"] > 0 else f" (예상치 {cons['eps']:,.1f}엔)"
            lines.append(f"EPS : {e:,.1f}엔{ex}")
        HL.update(kind="res", q=q, single=single, cons=cons, end=r["end"])
        if py and py[3] is not None: HL["op_py"] = py[3] * 1e6
        if cons and (cons.get("rev") or cons.get("eps")):
            lines.append(f"<i>예상치: Yahoo Finance 애널리스트 평균(매출 {cons.get('rev_n', 0)}명·EPS {cons.get('eps_n', 0)}명)</i>")
        lines.append("")
    # 누적 실적
    cum_lab = "연간" if q == 4 else f"누적 {q}Q"
    if not (q == 1 and single): lines.append(f"<b>[{cum_lab} · {start.strftime('%y.%m')}~{end.strftime('%y.%m')} · {r['scope']}]</b>")
    for m, lab in [("rev", "매출액"), ("op", "영업익"), ("ord", ordlab), ("ni", "순이익")]:
        v = r.get(m)
        if v is None or (q == 1 and single): continue
        c = chg_txt(v, r.get(m + "_py"), r.get(m + "_yoy"))
        lines.append(f"{lab} : {oku(v)}" + (f" (YoY {c})" if c else ""))
    # 회사 가이던스와 진척률
    fc = r.get("fc")
    if fc and any(fc.get(m) for m in ["rev", "op", "ni"]):
        fyl = fy_label(r.get("fc_end"))
        parts = []
        for m, lab in [("rev", "매출"), ("op", "영업익"), ("ord", ordlab), ("ni", "순이익")]:
            if fc.get(m) is None: continue
            c = chg_txt(fc[m], r.get(m)) if q == 4 else ""
            parts.append(f"{lab} {oku(fc[m])}" + (f"({c})" if c else ""))
        HL.update(fc=fc, fc_fy=fyl, op_cum=r.get("op"), ord_cum=r.get("ord"), q=q, kind="res")
        if lines[-1]: lines.append("")
        lines.append(f"<b>회사 가이던스({fyl}{', ' + r['fc_kind'] if r.get('fc_kind') else ''})</b> : " + " / ".join(parts))
        if q < 4:
            key = "op" if fc.get("op") and r.get("op") is not None else ("ord" if fc.get("ord") and r.get("ord") is not None else None)
            if key and fc[key] > 0 and r[key] >= 0:
                lab = {"op": "영업익", "ord": ordlab}[key]
                prog = r[key] / fc[key] * 100
                hp = hist_progress(kq, start, q, 3 if key == "op" else 4)
                avg = sum(hp) / len(hp) if hp else None
                lines.append(f"{lab} 진척률 : {prog:.0f}% (누적 {q}Q 기준" + (f" · 과거 {len(hp)}년 평균 {avg:.0f}%" if avg is not None else "") + ")")
                HL.update(prog=prog, prog_hist=avg, prog_n=len(hp), prog_lab=lab)
    # 최근 분기 추이
    hist = []
    if single and single.get("rev") is not None:
        m0 = (end.month - 2 - 1) % 12 + 1
        hist.append(f"{str(end.year)[2:]}.{m0:02d}-{end.month:02d} {oku(single['rev'])}/ {oku(single.get('op'))}/ {oku(single.get('ni'))} (이번)")
    for k in reversed([k for k in kq if k[1] < end.replace(day=1)][-4:]):
        hist.append(f"{k[0].strftime('%y.%m')}-{k[1].month:02d} {mo(k[2])}/ {mo(k[3])}/ {mo(k[5])}")
    if hist:
        if lines[-1]: lines.append("")
        lines.append("<b>최근 분기 실적 추이</b> (매출/ 영업익/ 순이익)")
        lines += hist
    return "\n".join(lines)

def body_revision(row):
    kind, facts, ctx = X.open_zip(http(row["xbrl"], timeout=40))
    if kind != "rvfc": return None
    r = X.summarize_revision(facts, ctx)
    if not r: return None
    per = {"CurrentYearDuration": "연간", "CurrentAccumulatedQ2Duration": "상반기 누적", "CurrentAccumulatedQ1Duration": "1Q 누적", "CurrentAccumulatedQ3Duration": "3Q 누적"}[r["per"]]
    fy = fy_label(r["end"])
    try:
        d = dt.date.fromisoformat(r["end"]); add = {"CurrentAccumulatedQ1Duration": 9, "CurrentAccumulatedQ2Duration": 6, "CurrentAccumulatedQ3Duration": 3}.get(r["per"], 0)
        mm = d.month + add; fy = f"FY{(mm - 1) % 12 + 1}/{str(d.year + (mm - 1) // 12)[2:]}"
    except Exception: pass
    lines = [f"<b>[실적 전망 수정 · {fy} {per} · {r['scope']}]</b>"]
    HL.update(kind="rev", fy=fy, per=per, rows=r["rows"])
    for m, lab in [("rev", "매출액"), ("op", "영업익"), ("ord", "경상익"), ("ni", "순이익")]:
        a, b = r["rows"][m]
        if b is None: continue
        c = chg_txt(b, a)
        lines.append(f"{lab} : {oku(a)} → {oku(b)}" + (f" ({c})" if c else ""))
    a, b = r["rows"]["eps"]
    if b is not None: lines.append(f"EPS : {a if a is not None else '—'}엔 → {b:,.2f}엔")
    return "\n".join(lines)

def body_dividend(row):
    kind, facts, ctx = X.open_zip(http(row["xbrl"], timeout=40))
    if kind != "rvdf": return None
    r = X.summarize_dividend(facts, ctx)
    if not r: return None
    f = lambda v: f"{v:,.2f}엔" if v is not None else "미정"
    HL.update(kind="div", prev=r["prev"], curr=r["curr"])
    s = f"<b>[배당 전망 수정 · 연간 주당배당]</b>\n{f(r['prev'])} → {f(r['curr'])}"
    if r["prev"] and r["curr"] is not None: s += f" ({pct(r['curr'], r['prev']):+.1f}%)"
    if r["last"] is not None: s += f"\n전기 실적 : {f(r['last'])}"
    return s

def pdf_text(url):
    from pdfminer.high_level import extract_text
    return nfkc(extract_text(io.BytesIO(http(url, timeout=40)), maxpages=3))

DATE = r"(\d{4})年(\d{1,2})月(\d{1,2})日(?:\([^)]*\))?"
def body_buyback(row):
    t = re.sub(r"\s+", "", pdf_text(row["pdf"]))
    k = max(t.rfind("取得に係る事項の決定"), t.rfind("取得に関する事項の決定"))
    if k > 0: t = t[k:]  # 취득 종료 보고와 새 결정이 함께 있으면 새 결정 부분만
    last = lambda p: (re.findall(p, t) or [None])[0]
    n = last(r"株式の総数([\d,.]+)(千)?株"); ratio = last(r"割合([\d.]+)%"); amt = last(r"総額([\d,.]+)(百万|千|億)?円")
    per = last(DATE + r"[~〜～から]+" + DATE)
    if not (n or amt): return None
    parts = []
    if n:
        shares = float(n[0].replace(",", "")) * (1000 if n[1] else 1)
        parts.append(f"한도 {shares:,.0f}주" + (f"(자사주 제외 발행주식의 {ratio}%)" if ratio else ""))
    if amt:
        yen = float(amt[0].replace(",", "")) * {"": 1, "千": 1e3, "百万": 1e6, "億": 1e8}[amt[1]]
        parts.append(f"{oku(yen)}엔")
    HL.update(kind="bb", ratio=float(ratio) if ratio else None, yen=yen if amt else None)
    s = "<b>[자사주 취득 결정]</b>\n" + " / ".join(parts)
    if per: s += f"\n기간 : {per[0]}.{int(per[1]):02d}.{int(per[2]):02d}~{per[3]}.{int(per[4]):02d}.{int(per[5]):02d}"
    return s

def body_tob(row):
    t = re.sub(r"\s+", "", pdf_text(row["pdf"]))
    p = re.search(r"1株につき、?金?([\d,]+)円", t) or re.search(r"買付け?等の価格.{0,40}?金([\d,]+)円", t)
    per = re.search(r"買付け?等の期間" + DATE + r".{0,4}?" + DATE, t)
    prem = re.search(r"([\d.]+)%(?:\([^)]*\))?のプレミアム", t)
    if not p: return None
    HL.update(kind="tob", price=p.group(1), prem=prem.group(1) if prem else None)
    s = f"<b>[공개매수]</b>\n매수가격 : 주당 {p.group(1)}엔"
    if prem: s += f" (공표 전일 종가 대비 프리미엄 {prem.group(1)}%)"
    if per: s += f"\n기간 : {per.group(1)}.{int(per.group(2)):02d}.{int(per.group(3)):02d}~{per.group(4)}.{int(per.group(5)):02d}.{int(per.group(6)):02d}"
    return s

def build(row, st, names, quotes, trmap):
    code = row["code"]; n = names.get(code)
    name_ko = n["ko"] if n else jp2ko(row["name"])
    mk = n["mkt"] if n else PLACE.get(row.get("place", ""), row.get("place", ""))
    mc = mcap_txt(quotes.get(code))
    d = row["d"]
    head = [f"{d[:4]}.{d[4:6]}.{d[6:]} {row['t']}",
            f"기업명: {name_ko}" + (f"(시가총액: {mc})" if mc else "") + f" {code}" + (f" [{mk}]" if mk else ""),
            f"보고서명: {trmap.get(row['title']) or row['title']}"]
    body = None
    title = row["title"]
    HL.clear()
    try:
        if row.get("xbrl"):
            if "決算短信" in title and "訂正" not in title: body = body_results(row, st)
            elif "配当予想" in title and "業績予想" not in title: body = body_dividend(row)
            elif "予想" in title or "修正" in title: body = body_revision(row) or body_dividend(row)
        elif re.search(r"自己株式(の)?取得に係る事項の決定|自己株式取得に係る事項の決定|自己株式の取得及び", title):
            body = body_buyback(row)
        elif re.search(r"公開買付けの開始|公開買付けに関する意見表明|MBO", title) and not re.match(r"[(（](訂正|変更|経過開示)", title):
            body = body_tob(row)
    except Exception as e:
        log("body error", code, title[:30], e)
    try:
        if body: record_hl(row, st, name_ko, quotes.get(code), trmap.get(title) or title)
    except Exception as e:
        log("highlight error", code, e)
    msg = "\n".join(head)
    if body: msg += "\n\n" + body
    msg += f"\n\n공시링크: {row['pdf']}\n회사정보: https://kabutan.jp/stock/?code={code}"
    return msg

# ───────────────────────── 하이라이트(매일 19시 요약) ─────────────────────────
# 판정 기준
BEAT_EPS = 10      # 분기 EPS가 컨센서스 대비 +10% 이상
BEAT_REV = 5       # 분기 매출이 컨센서스 대비 +5% 이상
NEWFC_OP = 20      # 연간 결산 때 새 회계연도 영업익 가이던스가 전기 대비 +20% 이상
NEWFC_CONS = 5     # …또는 새 회계연도 EPS 가이던스가 컨센서스 대비 +5% 이상 (-10% 이하면 하향 섹션)
PROG_GAP = 10      # 누적 영업익 진척률이 과거 같은 분기 평균보다 +10%p 이상
HIGROWTH = 50      # 컨센서스 없는 종목: 분기 영업익 YoY +50% 이상
HIGROWTH_MCAP = 3e10  # …이면서 시가총액 300억엔 이상
BB_RATIO = 1.0     # 자사주 취득 한도가 발행주식의 1% 이상
MISS_EPS = -10     # 분기 EPS가 컨센서스 대비 -10% 이하(또는 예상 흑자인데 적자)
MISS_REV = -5      # 분기 매출이 컨센서스 대비 -5% 이하
SECTIONS = [("beat", "① 컨센서스 상회 실적"), ("up", "② 실적 가이던스 상향"), ("prog", "③ 상향 후보 (진척률이 과거 평균보다 빠름)"),
            ("newfc", "④ 결산 + 새 회계연도 가이던스 (고성장·컨센 상회)"), ("grow", "⑤ 고성장 실적 (컨센서스 없음, 시총 300억엔↑)"),
            ("ret", "⑥ 증배 · 자사주 · 공개매수"), ("miss", "⑦ 컨센서스 하회 실적"), ("down", "⑧ 실적 가이던스 하향")]

def _p(x): return f"{x:+.0f}%"
def per_txt(q, eps, lab="가이던스"):
    px = (q or {}).get("regularMarketPrice")
    if not px or not eps or eps <= 0: return ""
    return f" · {lab} PER {px / eps:.1f}배"
def record_hl(row, st, name_ko, q, title_ko):
    """방금 처리한 공시가 하이라이트 기준에 맞으면 st['hl']에 저장. 항목: (섹션, 문구, 점수)"""
    h = dict(HL); items = []
    if h.get("kind") == "res":
        qq = h.get("q"); sg = h.get("single") or {}; cons = h.get("cons") or {}; fc = h.get("fc") or {}
        ql = f"{qq}Q" if qq and qq < 4 else "4Q"
        e, ce = sg.get("eps"), cons.get("eps"); rv, cr = sg.get("rev"), cons.get("rev")
        es = pct(e, ce) if (e is not None and ce and ce > 0) else None
        rs = pct(rv, cr) if (rv is not None and cr) else None
        op, opp = sg.get("op"), h.get("op_py")
        oy = chg_txt(op, opp) if op is not None and opp is not None else ""
        pe = per_txt(q, fc.get("eps"), "가이던스" if qq < 4 else "새 가이던스")
        beat = (es is not None and es >= BEAT_EPS and e > 0) or (rs is not None and rs >= BEAT_REV and not (es is not None and es <= MISS_EPS))
        miss = not beat and ((es is not None and (es <= MISS_EPS or e < 0)) or (rs is not None and rs <= MISS_REV))
        if beat or miss:
            parts = []
            if es is not None: parts.append(f"EPS {e:,.1f}엔(예상 {ce:,.1f}엔, " + ("적자" if e < 0 else _p(es)) + ")")
            if rs is not None: parts.append(f"매출 {oku(rv)}엔(예상 대비 {_p(rs)})")
            if oy: parts.append(f"영업익 {oku(op)}엔(YoY {oy})")
            n = max(cons.get("eps_n", 0), cons.get("rev_n", 0))
            txt = f"{ql} 단독 " + " · ".join(parts) + (f" [애널 {n}명]" if n else "") + pe
            # 일회성 의심: 컨센은 넘었는데 본업(영업익)은 역성장·적자
            if beat and op is not None and (op < 0 or (opp is not None and opp > 0 and op < opp)):
                txt += " ⚠ 영업익은 감소(일회성 이익 가능성)"
            sc = min(max(es or -999, (rs or -999) * 2), 100)
            items.append(("beat", txt, sc) if beat else ("miss", txt, -min(es if es is not None else 999, (rs if rs is not None else 999) * 2)))
        elif not cons and op is not None and opp and opp > 0 and op > 0 and pct(op, opp) >= HIGROWTH \
                and ((q or {}).get("marketCap") or 0) >= HIGROWTH_MCAP:
            items.append(("grow", f"{ql} 단독 영업익 {oku(op)}엔(YoY {oy})" + (f" · 매출 {oku(rv)}엔" if rv is not None else "") + pe, min(pct(op, opp), 200)))
        # 진척률 → 상향 후보
        pr, ph = h.get("prog"), h.get("prog_hist")
        # 과거 평균이 0~100% 사이(계절성 정상)이고, 이번 분기 영업익이 전년보다 줄지 않았을 때만
        if qq and qq < 4 and pr is not None and ph is not None and 5 <= ph <= 100 and pr - ph >= PROG_GAP and pr >= qq * 25 + 5 \
                and not (op is not None and opp is not None and op < opp):
            items.append(("prog", f"누적 {qq}Q {h.get('prog_lab', '영업익')} 진척률 {pr:.0f}% (과거 {h.get('prog_n')}년 같은 분기 평균 {ph:.0f}%, +{pr - ph:.0f}%p)"
                          + (" · 이미 연간 가이던스 초과" if pr >= 100 else "") + (f" · 이번 분기 영업익 YoY {oy}" if oy else "") + pe, min(pr - ph, 60)))
        # 결산: 새 회계연도 가이던스
        if qq == 4 and fc:
            ca = h.get("cons_all") or {}
            key = "op" if fc.get("op") is not None and h.get("op_cum") else ("ord" if fc.get("ord") is not None and h.get("ord_cum") else None)
            g = None; txt = ""
            if key:
                a, b = h[key + "_cum"], fc[key]
                g = pct(b, a) if a > 0 else None
                lab = {"op": "영업익", "ord": "경상익"}[key]
                txt = f"{h.get('fc_fy','')} {lab} 가이던스 {oku(b)}엔(전기 {oku(a)}엔 대비 {chg_txt(b, a)})"
                if a <= 0 < b: g = 100
            cg = pct(fc.get("eps"), ca.get("eps_y1")) if fc.get("eps") and ca.get("eps_y1") and ca["eps_y1"] > 0 else None
            if cg is not None:
                txt += (" · " if txt else f"{h.get('fc_fy','')} ") + f"EPS 가이던스 {fc['eps']:,.1f}엔(컨센 {ca['eps_y1']:,.1f}엔 대비 {_p(cg)}, 애널 {ca.get('eps_y1_n', 0)}명)"
            if (g is not None and g >= NEWFC_OP) or (cg is not None and cg >= NEWFC_CONS):
                items.append(("newfc", txt + pe, min(max(g or -999, (cg or -999) * 2), 100)))
            elif cg is not None and cg <= -10:
                items.append(("down", "새 회계연도 가이던스가 컨센 하회 : " + txt + pe, -cg))
    elif h.get("kind") == "rev":
        rows = h["rows"]
        for key, lab in [("op", "영업익"), ("ord", "경상익"), ("ni", "순이익")]:
            a, b = rows.get(key, (None, None))
            if a is None or b is None: continue
            if b != a and (a > 0 or b > 0):
                extra = []
                for k2, l2 in [("rev", "매출"), ("ni", "순이익")]:
                    a2, b2 = rows.get(k2, (None, None))
                    if k2 != key and a2 is not None and b2 is not None and chg_txt(b2, a2): extra.append(f"{l2} {chg_txt(b2, a2)}")
                g = pct(b, a) if a > 0 else 100
                eb = rows.get("eps", (None, None))[1]
                items.append(("up" if b > a else "down", f"{h['fy']} {h['per']} {lab} {oku(a)}→{oku(b)}엔({chg_txt(b, a)})"
                              + (f" · {' · '.join(extra)}" if extra else "") + (per_txt(q, eb, "수정 후") if h["per"] == "연간" else ""), min(abs(g), 100)))
            break  # 영업익 기준(없으면 경상익→순이익)으로 한 번만 판정 — 순이익만 바뀐 수정(일회성)은 제외
    elif h.get("kind") == "div":
        a, b = h.get("prev"), h.get("curr")
        if a and b is not None and b > a: items.append(("ret", f"증배 : 연간 배당 {a:,.2f}→{b:,.2f}엔({pct(b, a):+.1f}%)", 0))
    elif h.get("kind") == "bb":
        if h.get("ratio") and h["ratio"] >= BB_RATIO:
            items.append(("ret", f"자사주 취득 : 발행주식의 {h['ratio']:g}%" + (f" / {oku(h['yen'])}엔" if h.get("yen") else ""), 0))
    elif h.get("kind") == "tob":
        items.append(("ret", f"공개매수 : 주당 {h['price']}엔" + (f"(프리미엄 {h['prem']}%)" if h.get("prem") else "") + f" — {esc(title_ko)[:40]}", 0))
    if not items: return
    src = ""
    try: src = reason_src(row, h.get("kind"))
    except Exception as e: log("reason src error", row["code"], e)
    hl = st.setdefault("hl", [])
    have = {(x["id"], x["sec"]) for x in hl}
    mc = (q or {}).get("marketCap") or 0
    for sec, txt, sc in items:
        if (row["id"], sec) in have: continue
        hl.append({"id": row["id"], "sec": sec, "code": row["code"], "name": name_ko, "mc": mc, "mct": mcap_txt(q),
                   "txt": txt, "pdf": row["pdf"], "ts": time.time(), "sent": False, "src": src, "score": sc or 0})

# 공시 PDF에서 '이유'가 적힌 부분(일본어 원문)을 잘라 둠 → 19시 요약 때 AI가 한 줄로 정리
REASON_HEAD = {
    "res": ["経営成績に関する説明", "経営成績等の概況", "経営成績の概況", "当四半期決算に関する定性的情報", "経営成績の状況", "業績の状況"],
    "rev": ["修正の理由", "修正理由", "差異の理由", "理由"],
    "div": ["修正の理由", "理由"],
    "bb": ["取得を行う理由", "取得する理由", "理由"],
    "tob": ["公開買付けの目的", "買付け等の目的", "目的", "理由"],
}
def reason_src(row, kind):
    heads = REASON_HEAD.get(kind)
    if not heads: return ""
    from pdfminer.high_level import extract_text
    t = nfkc(extract_text(io.BytesIO(http(row["pdf"], timeout=40)), maxpages=6 if kind == "res" else 3))
    t = re.sub(r"[ \t\u3000]+", "", t); t = re.sub(r"\n+", "\n", t)
    n = 2500 if kind == "res" else 1200
    out = ""
    for hd in heads:
        best = ""
        for m in re.finditer(re.escape(hd), t):
            seg = t[m.end():m.end() + n]
            if "」" in seg[:25] or "目次" in seg[:60]: continue  # 다른 절 참조·목차
            if re.match(r"[.…‥・\s\d]{0,6}[.…‥]{3}", seg) or re.match(r"[.…‥\s]*\d+\s*\n", seg): continue  # 목차 줄
            if len(re.sub(r"[\d,.%△▲\s]", "", seg[:300])) < 80: continue  # 숫자 표
            best = seg; break
        if best: out = best.replace("\n", ""); break
    if kind == "res":  # 결산이면 '향후 전망'(새 가이던스 근거)도 덧붙임
        for m in re.finditer(r"今後の見通し", t):
            seg = t[m.end():m.end() + 800]
            if "」" in seg[:25] or re.match(r"[.…‥\s\d]{0,6}[.…‥]{3}", seg): continue
            out += " [今後の見通し] " + seg.replace("\n", ""); break
    return out

GEMINI_MODELS = [m.strip() for m in os.environ.get("GEMINI_MODELS", "gemini-3.8-flash,gemini-3.7-flash,gemini-3.5-flash,gemini-3.1-flash-lite").split(",") if m.strip()]
def gemini_json(prompt):
    key = os.environ.get("GEMINI_API_KEY")
    if not key: return None
    for model in GEMINI_MODELS:
        try:
            body = json.dumps({"contents": [{"parts": [{"text": prompt}]}],
                               "generationConfig": {"temperature": 0.2, "maxOutputTokens": 8000, "responseMimeType": "application/json"}}).encode()
            r = json.loads(http(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}",
                                timeout=150, data=body, headers={"content-type": "application/json"}))
            txt = "".join(p.get("text", "") for p in r["candidates"][0]["content"]["parts"])
            return json.loads(re.sub(r"^```(json)?|```$", "", txt.strip()))
        except Exception as e:
            log("gemini error", model, str(e)[:150])
    return None

AI_PROMPT = """너는 일본 주식 애널리스트다. 아래는 오늘 도쿄증시 적시공시 중 하이라이트로 뽑힌 항목들이다.
각 항목에는 [분류], 기업, 핵심 수치, 그리고 공시 원문(일본어)에서 이유·설명 부분을 잘라낸 글이 있다.

할 일
1. reasons: 항목마다 "왜 이런 결과/결정이 나왔는지"를 한국어 한 문장(60자 이내)으로. 원문에 적힌 사유만 쓰고, 원문에 사유가 없으면 "공시에 사유 기재 없음". 추측 금지. 일본어 고유명사는 한글로.
2. top: 투자자 관점에서 오늘 가장 중요한 공시 최대 3개를 골라, 왜 중요한지 한국어 한 문장(80자 이내)으로. 시가총액, 서프라이즈·수정 폭, 사유의 지속성(일회성인지 구조적인지)을 고려.

출력은 JSON만: {{"reasons": {{"번호": "사유"}}, "top": [{{"i": 번호, "why": "이유"}}]}}

항목:
{items}
"""
def ai_reasons(items):
    """{item 위치: 사유}, [(위치, 중요한 이유)]"""
    reasons, top = {}, []
    for i0 in range(0, len(items), 25):
        part = items[i0:i0 + 25]
        lines = []
        for j, x in enumerate(part):
            lines.append(f"#{i0 + j + 1} [{dict(SECTIONS)[x['sec']]}] {x['name']} {x['code']} (시총 {x['mct'] or '?'}) : {x['txt']}\n원문: {x.get('src') or '(없음)'}")
        out = gemini_json(AI_PROMPT.format(items="\n\n".join(lines)))
        if not out: break
        for k, v in (out.get("reasons") or {}).items():
            try: reasons[int(str(k).strip("#")) - 1] = str(v).strip()
            except ValueError: pass
        for t in out.get("top") or []:
            try: top.append((int(str(t["i"]).strip("#")) - 1, str(t["why"]).strip()))
            except Exception: pass
    if len(items) > 25: top = top[:3]
    return reasons, top

def fallback_reasons(st, items):
    """AI 키가 없을 때: 원문 사유의 첫 문장을 번역"""
    srcs = {}
    for i, x in enumerate(items):
        s = (x.get("src") or "").split(" [今後の見通し] ")[0]
        sents = re.findall(r"[^。]{15,200}。", s)
        pick = [z for z in sents if re.search(r"増収|減収|増益|減益|上回|下回|増加|減少|伸長|好調|低調|不振|寄与|影響|理由|ため", z) and "経済" not in z]
        if pick or sents: srcs[i] = (pick or sents)[0]
    tr = translate(st, list(srcs.values()))
    return {i: tr.get(v) for i, v in srcs.items() if tr.get(v)}

def digest_text(items, day, reasons=None, top=None):
    reasons = reasons or {}; top = top or []
    wd = "월화수목금토일"[day.weekday()]
    out = [f"📊 <b>{day.month}/{day.day}({wd}) 일본 공시 하이라이트</b>", f"<i>{DIGEST_HOUR}:00 기준 · 섹션별 서프라이즈·수정 폭 큰 순 · 기업명 누르면 공시 원문</i>"]
    if not items:
        return "\n".join(out + ["", "오늘은 기준에 맞는 공시가 없습니다."])
    if top:
        out += ["", "<b>⭐ 오늘의 핵심</b>"]
        for i, why in top:
            if 0 <= i < len(items):
                x = items[i]; out.append(f"• <a href=\"{x['pdf']}\">{esc(x['name'])}</a> {x['code']} — {esc(why)}")
    idx = {id(x): i for i, x in enumerate(items)}
    up_codes = {x["code"] for x in items if x["sec"] == "up"}
    for sec, lab in SECTIONS:
        xs = [x for x in items if x["sec"] == sec and not (sec == "prog" and x["code"] in up_codes)]
        seen = set(); uniq = []
        for x in sorted(xs, key=lambda x: (-(x.get("score") or 0), -x["mc"])):
            if (x["code"], x["txt"]) in seen: continue
            seen.add((x["code"], x["txt"])); uniq.append(x)
        if not uniq: continue
        out += ["", f"<b>{lab} ({len(uniq)}건)</b>"]
        for x in uniq[:20]:
            out.append(f"• <a href=\"{x['pdf']}\">{esc(x['name'])}</a>" + (f"({x['mct']})" if x["mct"] else "") + f" {x['code']}\n   {x['txt']}")
            rs = reasons.get(idx[id(x)])
            if rs: out.append(f"   └ 이유: {esc(rs)}")
        if len(uniq) > 20: out.append(f"   …외 {len(uniq) - 20}건")
    out += ["", f"<i>기준: 컨센 상회 = 분기 EPS +{BEAT_EPS}%↑ 또는 매출 +{BEAT_REV}%↑ / 하회 = EPS {MISS_EPS}%↓(또는 적자) 또는 매출 {MISS_REV}%↓ (Yahoo Finance 애널리스트 평균, 발표 전 저장) · 가이던스 상·하향 = 회사 실적예상 수정 공시의 영업익(없으면 경상익) 기준 · 상향 후보 = 누적 영업익 진척률이 과거 같은 분기 평균보다 +{PROG_GAP}%p↑ (가부탄 분기표) · PER = 직전 주가 ÷ 회사 가이던스 EPS · 이유 = 공시 원문의 설명 부분을 "
            + ("AI(Gemini)가 요약" if os.environ.get("GEMINI_API_KEY") else "기계 번역") + ", 원문 확인 권장</i>"]
    return "\n".join(out)

FOLLOW_HM = (15, 40)  # 다음 거래일 장 마감 후 주가 반응 발송 시각(JST)
def maybe_followup(st):
    """하이라이트로 보낸 종목의 다음 거래일 주가 반응(종가 등락률)을 한 번 더 발송"""
    t = now(); today = t.strftime("%Y%m%d")
    if t.weekday() >= 5 or (t.hour, t.minute) < FOLLOW_HM or st.get("fu_date") == today: return
    items = [x for x in st.get("hl", []) if x.get("sent") and x.get("dd") and x["dd"] < today and not x.get("fu")]
    st["fu_date"] = today
    if not items: return
    qs = yf_quotes([x["code"] for x in items])
    rows = []
    for x in items:
        qd = qs.get(x["code"]) or {}
        ts = qd.get("regularMarketTime")
        if not ts or dt.datetime.fromtimestamp(ts, JST).strftime("%Y%m%d") != today: continue  # 오늘 거래 없음(휴장 등)
        rows.append((qd.get("regularMarketChangePercent") or 0, x))
    if not rows:
        st["fu_date"] = None if t.weekday() < 5 and not qs else today; return
    wd = "월화수목금토일"[t.weekday()]
    out = [f"📈 <b>{t.month}/{t.day}({wd}) 하이라이트 종목 주가 반응</b>", "<i>직전 하이라이트에 나온 종목의 오늘 종가 등락률 · 섹션별 상승률순</i>"]
    for sec, lab in SECTIONS:
        xs = sorted([(c, x) for c, x in rows if x["sec"] == sec], key=lambda z: -z[0])
        seen = set(); lines = []
        for c, x in xs:
            if x["code"] in seen: continue
            seen.add(x["code"])
            lines.append(f"{'🔺' if c > 0 else '🔻' if c < 0 else '▪'} {c:+.1f}% <a href=\"{x['pdf']}\">{esc(x['name'])}</a> {x['code']}")
        if lines: out += ["", f"<b>{lab}</b>"] + lines[:25] + ([f"   …외 {len(lines) - 25}건"] if len(lines) > 25 else [])
    out += ["", "<i>장중 공시는 공시 당일에 이미 일부 반영됐을 수 있음 · 시세: Yahoo Finance</i>"]
    for part in split_msg("\n".join(out)):
        tg_send(part); time.sleep(SEND_GAP)
    for c, x in rows: x["fu"] = True
    log("followup sent", len(rows))

def split_msg(text, limit=3900):
    parts, cur = [], ""
    for blk in text.split("\n"):
        if len(cur) + len(blk) + 1 > limit and cur: parts.append(cur); cur = ""
        cur += ("\n" if cur else "") + blk
    if cur: parts.append(cur)
    return parts

def maybe_digest(st, force=False):
    """매일 DIGEST_HOUR시 이후 1회: 그날(TDnet 날짜) 이전 하이라이트를 모아 발송"""
    t = now(); today = t.strftime("%Y%m%d")
    if not force and (t.hour < DIGEST_HOUR or st.get("digest_date") == today): return
    # 그날 오후 공시 처리가 아직 밀려 있으면 최대 1시간 기다림
    if not force and t.hour < DIGEST_HOUR + 1 and any(r["d"] == today for r in st.get("queue", [])): return
    hl = st.setdefault("hl", [])
    items = [x for x in hl if not x["sent"]]
    has_disc = bool(st.get("seen", {}).get(today))
    if items or (has_disc and t.weekday() < 5):
        ok = True
        reasons, top = {}, []
        if items:
            try:
                reasons, top = ai_reasons(items)
                if not reasons: reasons = fallback_reasons(st, items)
            except Exception: traceback.print_exc()
        for part in split_msg(digest_text(items, t.date(), reasons, top)):
            ok = tg_send(part) and ok; time.sleep(SEND_GAP)
        if not ok: log("digest send failed"); return
        for x in items: x["sent"] = True; x["dd"] = today
        log("digest sent", len(items))
    st["digest_date"] = today
    st["hl"] = [x for x in hl if time.time() - x["ts"] < 86400 * 6]

# ───────────────────────── 텔레그램 ─────────────────────────
def tg_send(text):
    if DRY:
        print("-" * 60 + "\n" + re.sub(r"</?[bi]>", "", text)); return True
    for _ in range(3):
        try:
            data = json.dumps({"chat_id": CHANNEL, "text": text[:4000], "parse_mode": "HTML", "disable_web_page_preview": True}).encode()
            http(f"https://api.telegram.org/bot{TOKEN}/sendMessage", data=data, headers={"content-type": "application/json"})
            return True
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "ignore")
            if e.code == 429:
                wait = json.loads(body).get("parameters", {}).get("retry_after", 5)
                log("rate limited, wait", wait); time.sleep(wait + 1); continue
            log("telegram error", e.code, body[:200])
            if e.code == 400 and "parse" in body:  # HTML 파싱 오류면 서식 없이 재시도
                text = re.sub(r"</?[bi]>", "", text)
                continue
            return False
        except Exception as e:
            log("telegram error", e); time.sleep(3)
    return False

# ───────────────────────── 메인 ─────────────────────────
def esc(s): return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def collect(st):
    """새 공시를 대기열에 추가"""
    t = now(); days = [t.strftime("%Y%m%d")]
    if t.hour < 1: days.insert(0, (t - dt.timedelta(days=1)).strftime("%Y%m%d"))
    seen = st.setdefault("seen", {})
    first = not st.get("init")
    added = 0
    for d in days:
        s = set(seen.get(d, []))
        rows = [r for r in scan_tdnet(d) if r["id"] not in s]
        rows.reverse()  # 오래된 것부터
        for r in rows:
            s.add(r["id"])
            if first or any(k in r["title"] for k in SKIP): continue
            st.setdefault("queue", []).append(r); added += 1
        seen[d] = list(s)
    for d in list(seen):
        if d < (t - dt.timedelta(days=3)).strftime("%Y%m%d"): seen.pop(d)
    if first:
        st["init"] = True; log("first run: existing disclosures recorded without posting")
    return added

def main():
    t0 = time.time()
    st = load("state.json", {})
    names = load_names(st)
    added = collect(st)
    log("new", added, "queue", len(st.get("queue", [])))
    try:
        consensus_scan(st, names); consensus_work(st)
    except Exception as e:
        log("consensus error", e)
    save("state.json", st)
    last_scan = time.time(); last_send = 0
    quotes = {}
    while time.time() - t0 < RUN_SECONDS:
        try: maybe_digest(st)
        except Exception: traceback.print_exc()
        try: maybe_followup(st)
        except Exception: traceback.print_exc()
        q = st.get("queue", [])
        if not q:
            if time.time() - last_scan > 60 and time.time() - t0 < RUN_SECONDS - 30:
                collect(st); last_scan = time.time(); save("state.json", st); continue
            try: consensus_scan(st, names); consensus_work(st, 20)
            except Exception as e: log("consensus error", e)
            time.sleep(5)
            if time.time() - last_scan > 60: collect(st); last_scan = time.time(); save("state.json", st)
            continue
        batch = q[:15]
        miss = [r["code"] for r in batch if r["code"] not in quotes]
        if miss: quotes.update(yf_quotes(miss))
        trmap = translate(st, [r["title"] for r in batch])
        trmap = {k: esc(v) if v else None for k, v in trmap.items()}
        for r in batch:
            if time.time() - t0 > RUN_SECONDS: break
            r2 = dict(r); r2["title"] = r["title"]
            try:
                msg = build(r2, st, names, quotes, {r["title"]: trmap.get(r["title"]) or esc(r["title"])})
            except Exception:
                traceback.print_exc(); msg = None
            wait = SEND_GAP - (time.time() - last_send)
            if wait > 0: time.sleep(wait)
            ok = tg_send(msg) if msg else True
            last_send = time.time()
            if ok or not msg:
                st["queue"].remove(r)
            else:
                r["try"] = r.get("try", 0) + 1
                if r["try"] >= 3: st["queue"].remove(r); log("drop after 3 failures", r["code"], r["title"][:30])
                break
        save("state.json", st)
        if time.time() - last_scan > 60 and time.time() - t0 < RUN_SECONDS - 30:
            collect(st); last_scan = time.time()
    save("state.json", st)
    log("done, queue left", len(st.get("queue", [])))

if __name__ == "__main__":
    main()
