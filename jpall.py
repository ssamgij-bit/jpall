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
        if lines[-1]: lines.append("")
        lines.append(f"<b>회사 가이던스({fyl}{', ' + r['fc_kind'] if r.get('fc_kind') else ''})</b> : " + " / ".join(parts))
        if q < 4:
            key = "op" if fc.get("op") and r.get("op") is not None else ("ord" if fc.get("ord") and r.get("ord") is not None else None)
            if key and fc[key] > 0 and r[key] >= 0:
                lab = {"op": "영업익", "ord": ordlab}[key]
                lines.append(f"{lab} 진척률 : {r[key] / fc[key] * 100:.0f}% (누적 {q}Q 기준)")
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
    s = "<b>[자사주 취득 결정]</b>\n" + " / ".join(parts)
    if per: s += f"\n기간 : {per[0]}.{int(per[1]):02d}.{int(per[2]):02d}~{per[3]}.{int(per[4]):02d}.{int(per[5]):02d}"
    return s

def body_tob(row):
    t = re.sub(r"\s+", "", pdf_text(row["pdf"]))
    p = re.search(r"1株につき、?金?([\d,]+)円", t) or re.search(r"買付け?等の価格.{0,40}?金([\d,]+)円", t)
    per = re.search(r"買付け?等の期間" + DATE + r".{0,4}?" + DATE, t)
    prem = re.search(r"([\d.]+)%(?:\([^)]*\))?のプレミアム", t)
    if not p: return None
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
    msg = "\n".join(head)
    if body: msg += "\n\n" + body
    msg += f"\n\n공시링크: {row['pdf']}\n회사정보: https://kabutan.jp/stock/?code={code}"
    return msg

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
