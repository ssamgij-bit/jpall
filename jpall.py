#!/usr/bin/env python3
"""JP_ALL: 도쿄증시 전 종목 적시공시 → 텔레그램 채널 자동 게시 (GitHub Actions에서 한 번에 약 28분씩 이어서 실행)

- 공시 목록: TDnet (https://www.release.tdnet.info/inbs/)
- 결산·실적수정·배당수정 수치: TDnet XBRL
- 최근 분기 실적 추이: 가부탄(kabutan.jp) 3개월 실적표
- 컨센서스: Yahoo Finance 애널리스트 추정치(매출·EPS, 발표 며칠 전에 미리 저장)
- 제목 번역: Google 번역(키 없는 공개 주소) + 용어 보정
환경 변수: TELEGRAM_TOKEN, CHANNEL_ID(@채널아이디 또는 -100...), RUN_SECONDS(기본 1680), DRY_RUN(1이면 게시 안 하고 출력)
"""
import os, sys, re, io, json, time, html, datetime as dt, unicodedata, urllib.request, urllib.parse, traceback

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
MIN_CAP = float(os.environ.get("MIN_CAP_OKU", "300")) * 1e8  # 이보다 작은 시가총액(엔) 종목 공시는 채널에 올리지 않음(관심 종목은 예외)

def muted(r, names, quotes, st):
    """채널에 올리지 않을 공시: 리츠·ETF·인프라펀드, 시총 300억 엔 미만, 일상 공시(관심 종목은 항상 게시)"""
    code = r["code"]
    if code in (st.get("watch") or {}): return None
    mk = (names.get(code) or {}).get("mkt", "")
    if mk in ("리츠", "ETF") or "投資法人" in r["name"] or re.search(r"ETF|ETN|REIT|ファンド", r["name"] + r["title"]):
        return "리츠·ETF"
    mc = (quotes.get(code) or {}).get("marketCap")
    if mc and mc < MIN_CAP: return f"시총 {MIN_CAP / 1e8:,.0f}억 엔 미만"
    t = nfkc(r["title"])
    if is_mat(t):  # 설명·보충 자료·질의응답 → 같은 날 결산단신 메시지에 링크로
        ml = st.setdefault("matlinks", {})
        cur = ml.get(code) if isinstance(ml.get(code), dict) and ml[code].get("d") == r["d"] else {"d": r["d"], "items": []}
        if r["pdf"] not in [u for _, u in cur["items"]]: cur["items"].append([mat_label(t), r["pdf"]])
        ml[code] = cur
        if len(ml) > 600:
            for k in sorted(ml, key=lambda k: (ml[k].get("d", "") if isinstance(ml[k], dict) else ""))[:200]: ml.pop(k, None)
        return "설명 자료(결산단신에 링크)"
    for pat, keep, why in PRE:  # 중요 키워드(자사주·합병 등)가 들어 있어도 빼는 진행·내부 공시
        if re.search(pat, t) and not (keep and re.search(keep, t)): return why
    if re.search(KEEP_ALWAYS, t): return None
    for pat, keep, why in ROUTINE:
        if re.search(pat, t) and not (keep and re.search(keep, t)): return why
    return None

MAT_RE = re.compile(r"決算説明|説明会資料|説明資料|補足資料|補足説明資料|プレゼンテーション資料|ファクトブック|データブック|決算概要|決算の概要|決算概況|中間決算の概況|決算.{0,6}参考資料|決算報告資料")
QA_RE = re.compile(r"質疑応答|Q&A|Q&amp;A|QA集|よくあるご質問")
def is_mat(t):
    """결산 설명·보충 자료(결산단신 본문 제외). 제목에 '결산단신'이 들어간 보충자료, 'n분기 결산'·'결산 자료'만 있는 제목, 결산 질의응답도 포함"""
    if re.search(r"決算短信", t) and not re.search(r"補足|説明|参考|プレゼン", t): return False
    if MAT_RE.search(t) or re.search(r"決算(説明)?資料", t): return True
    if re.search(r"決算", t) and QA_RE.search(t): return True
    core = re.sub(r"\d{4}\s*年\s*\d{1,2}\s*月期|FY\S+|[\s　()（）〔〕\[\]]", "", t)
    return bool(re.fullmatch(r"(第[1-4]四半期|中間期?|通期|上期|下期|期末)*(連結)?決算(発表|報告)?", core))
def mat_label(t):
    return "질의응답" if QA_RE.search(t) else "설명 자료"
# KEEP_ALWAYS보다 먼저 보는 제외 규칙
PRE = [
    (r"^\(?開示事項の経過\)?.*自己株式|自己株式.{0,12}(取得状況|取得結果|取得終了|取得の終了|消却完了|消却の完了)",
     None, "자사주 진행·결과(신규 결정은 게시)"),
    (r"子会社(の)?設立|子会社.{0,20}設立|子会社間の吸収合併|完全子会社(と)?の吸収合併|子会社との吸収合併|商号(の)?変更|解散及び清算|解散および清算|清算結了|孫会社|中間持株会社|グループ内(の)?組織再編",
     r"株式(の)?取得|公開買付|株式交換|事業譲渡|持分譲渡|株式譲渡|破産|特別損失|特別利益|合弁|共同出資|基本合意", "그룹 내부 재편"),
    (r"TOKYO PRO Market", None, "TOKYO PRO Market(기관 전용 시장)"),
    (r"^\(?訂正\)?|訂正報告書|一部訂正",
     r"決算短信", "정정(결산단신 정정은 게시)"),
]
# 아래 일상 공시 규칙에 걸려도 항상 게시하는 제목(중요 이벤트가 함께 들어간 공시)
KEEP_ALWAYS = r"自己株式(の)?取得|自己株式取得|株式分割|公開買付|業績予想|決算短信|合併|子会社化|株式交換|株式移転|完全子会社|資本業務提携|上場廃止|特別損失|減損"
# (빼는 제목, 그래도 남길 예외, 사유) — 통상·일상 공시
ROUTINE = [
    (r"譲渡制限付株式|株式報酬|ストック・?オプション|新株予約権|払込完了|従業員持株会|株式給付信託|業績連動型株式",
     r"第三者割当|行使価額修正|大量行使|MSCB|転換社債", "주식보상·신주예약권"),
    (r"役員|人事|執行役員|組織変更|機構改革|担当(の)?変更|取締役候補|監査役候補",
     r"代表取締役|社長|CEO|訴訟|損害賠償|不正|辞任.*代表", "임원·인사(대표이사 교체 외)"),
    (r"剰余金の配当|配当金の支払|期末配当|中間配当",
     r"増配|減配|無配|復配|初配|記念配当|特別配当|予想の修正|配当方針|累進", "정기 배당(변화 없음)"),
    (r"定款の一部変更|株主総会.{0,10}(開催|招集|基準日)|基準日(の)?設定|定時株主総会",
     r"株式併合|臨時株主総会|スクイーズ|非公開|上場廃止|買収防衛", "정관·주총 일정"),
    (r"資金の借入|借入(れ)?の実行|シンジケートローン|コミットメントライン|所在不明株主|本店(所在地)?(の)?移転|事務所(の)?移転|支店(の)?(開設|移転)",
     None, "일상 공시(차입·소재불명 주주·사무소 이전)"),
    (r"支配株主等に関する事項|実効性|外国人等の議決権|貸借銘柄|決算発表日|社債の発行|社債発行|発行登録|財務会計基準機構|有価証券報告書の提出|IFRS.{0,10}任意適用|投資単位の引|期限前償還",
     r"転換社債|新株予約権付社債|劣後", "통상 행정 공시"),
    (r"含み損", None, "유가증권 평가손(含み損)"),
    (r"売出価格等の決定|発行価格及び売出価格", None, "공모 가격 결정"),
    (r"財務上の特約|当座貸越|金利スワップ|タームローン|金銭消費貸借|コミットメント型", r"転換社債|新株予約権", "금융 계약(일상 차입)"),
    (r"個別業績の前期実績|個別実績値と前期実績|個別業績.{0,6}前期", r"連結|予想", "개별 실적 전기 차이"),
    (r"法人税等(調整額)?.{0,12}計上", r"業績予想|差異|特別損失|特別利益", "법인세 계상"),
]

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
    chunks, cur = [], []
    for t in need:  # 주소 길이 한도 때문에 20건·1,500자 단위로 나눔
        if cur and (len(cur) >= 20 or sum(len(x) for x in cur) + len(t) > 1500): chunks.append(cur); cur = []
        cur.append(t)
    if cur: chunks.append(cur)
    for part in chunks:
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

# ───────────────────────── JPX 실적 발표 예정일 ─────────────────────────
JPX_SCHED = "https://www.jpx.co.jp/listing/event-schedules/financial-announcement/index.html"
def jpx_schedule(st):
    """JPX '결산 발표 예정일' 엑셀(1차 자료) → st['sched'] = {코드: [YYYY-MM-DD, 업종]} (하루 1회 갱신)"""
    today = now().strftime("%Y-%m-%d")
    if st.get("jpx_date") == today and st.get("sched"): return st["sched"]
    st["jpx_date"] = today  # 실패해도 오늘은 다시 시도하지 않음
    html = http(JPX_SCHED, timeout=30).decode("utf-8", "ignore")
    sched = {}
    for link in dict.fromkeys(re.findall(r'href="([^"]+\.xlsx?)"', html)):
        try:
            x = pd.read_excel(io.BytesIO(http("https://www.jpx.co.jp" + link, timeout=60)), header=None)
        except Exception as e:
            log("jpx xlsx error", link, e); continue
        for _, r in x.iterrows():
            d = r.iloc[0]
            if not isinstance(d, (pd.Timestamp, dt.datetime)): continue
            ds = d.strftime("%Y-%m-%d")
            if ds < today: continue
            code = nfkc(str(r.iloc[1])).strip()[:4]
            if re.fullmatch(r"[0-9A-Z]{4}", code): sched[code] = [ds, nfkc(str(r.iloc[5])).strip()]
    if sched: st["sched"] = sched
    log("jpx schedule", len(sched))
    return st.get("sched", {})

# ───────────────────────── 컨센서스(Yahoo) 미리 저장 ─────────────────────────
def consensus_scan(st, names):
    """하루 한 번: 4일 안에 실적 발표 예정인 종목을 찾아 대기열에 넣음"""
    today = now().strftime("%Y-%m-%d")
    if st.get("cons_scan") == today or now().hour < 6: return
    codes = [c for c, v in names.items() if v["mkt"] in ("프라임", "스탠다드", "그로스")]
    q = yf_quotes(codes)
    t0, t1 = time.time() - 86400, time.time() + 86400 * 4
    pend = [c for c, v in q.items() if v.get("earningsTimestampStart") and t0 <= v["earningsTimestampStart"] <= t1]
    d0, d1 = (now() - dt.timedelta(days=1)).strftime("%Y-%m-%d"), (now() + dt.timedelta(days=4)).strftime("%Y-%m-%d")
    pend += [c for c, (d, _) in (st.get("sched") or {}).items() if d0 <= d <= d1 and ((q.get(c) or {}).get("marketCap") or 0) >= 1e10]
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
_KB = {}
def kabutan_html(code):
    c = _KB.get(code)
    if c and time.time() - c[0] < 3600: return c[1]
    s = http(f"https://kabutan.jp/stock/finance?code={code}", timeout=20).decode("utf-8", "ignore")
    _KB[code] = (time.time(), s)
    if len(_KB) > 400:
        for k in sorted(_KB, key=lambda k: _KB[k][0])[:100]: _KB.pop(k, None)
    return s
def kabutan_fc(s):
    """가부탄 연간 실적표의 회사 예상(予) 행 → {end, rev, op, ord, ni, eps} (백만엔·엔)"""
    i = s.find("fin_year_result_d")
    if i < 0: return None
    seg = s[i:s.find("</table>", i)]
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", seg, re.S):
        cells = [nfkc(re.sub(r"<[^>]+>", "", c)).replace("&nbsp;", " ").replace("\xa0", " ").strip() for c in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", tr, re.S)]
        if not cells or "予" not in cells[0]: continue
        m = re.search(r"(\d{4})\.(\d{2})", cells[0])
        if not m: continue
        def f(x):
            try: return float(x.replace(",", "").replace("+", ""))
            except Exception: return None
        v = [f(c) for c in cells[1:6]] + [None] * 5
        return {"end": dt.date(int(m.group(1)), int(m.group(2)), 28), "rev": v[0], "op": v[1], "ord": v[2], "ni": v[3], "eps": v[4]}
    return None
def kabutan_lastfy(s, before=None):
    """가부탄 연간 실적표의 가장 최근 확정 실적(予 아닌 행) → {end, rev, op, ord, ni, eps} (백만엔·엔). before(날짜) 이전 결산만"""
    i = s.find("fin_year_result_d")
    if i < 0: return None
    seg = s[i:s.find("</table>", i)]
    best = None
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", seg, re.S):
        cells = [nfkc(re.sub(r"<[^>]+>", "", c)).replace("&nbsp;", " ").replace("\xa0", " ").strip() for c in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", tr, re.S)]
        if not cells or "予" in cells[0]: continue
        m = re.search(r"(\d{4})\.(\d{2})", cells[0])
        if not m: continue
        end = dt.date(int(m.group(1)), int(m.group(2)), 28)
        if before and end >= before: continue
        def f(x):
            try: return float(x.replace(",", "").replace("+", ""))
            except Exception: return None
        v = [f(c) for c in cells[1:6]] + [None] * 5
        if best is None or end > best["end"]:
            best = {"end": end, "rev": v[0], "op": v[1], "ord": v[2], "ni": v[3], "eps": v[4]}
    return best
def kabutan_quarters(code, s=None):
    """[(시작일, 종료일, 매출, 영업익, 경상익, 순이익, EPS)] 백만엔 단위, 오래된 순"""
    try:
        s = s or kabutan_html(code)
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
        lastfy = None
        if q < 4:  # 진행 중인 회계연도 가이던스 → 전기 실적(가부탄 연간표) 대비 성장률
            try:
                lastfy = kabutan_lastfy(kabutan_html(row["code"]), before=dt.date.fromisoformat(r["fc_end"]) - dt.timedelta(days=200) if r.get("fc_end") else start)
            except Exception as e:
                log("lastfy error", row["code"], e)
        parts = []
        for m, lab in [("rev", "매출"), ("op", "영업익"), ("ord", ordlab), ("ni", "순이익")]:
            if fc.get(m) is None: continue
            if q == 4: c = chg_txt(fc[m], r.get(m))
            else: c = chg_txt(fc[m], lastfy[m] * 1e6) if lastfy and lastfy.get(m) is not None else ""
            parts.append(f"{lab} {oku(fc[m])}" + (f"(전기 대비 {c})" if c else ""))
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
    try:
        HL["chart"] = res_chart(row, kq, single, end)
    except Exception as e:
        log("res chart error", row.get("code"), e)
    return "\n".join(lines)

def growth_txt(last, a, b):
    """전기(또는 전년 동기) 실적 대비 성장률: 수정 전 → 수정 후. 예) '+5.3% → +26.3%'"""
    if last is None or b is None or last == 0: return ""
    f = lambda v: (chg_txt(v, last) or "") if v is not None else ""
    ga, gb = f(a), f(b)
    if not gb: return ""
    return f"{ga} → {gb}" if ga else gb

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
    last = r.get("last") or {}
    plab = "전기" if r["per"] == "CurrentYearDuration" else "전년 동기"
    for m, lab in [("rev", "매출액"), ("op", "영업익"), ("ord", "경상익"), ("ni", "순이익")]:
        a, b = r["rows"][m]
        if b is None: continue
        c = chg_txt(b, a)
        lines.append(f"{lab} : {oku(a)} → {oku(b)}" + (f" ({c})" if c else ""))
        g = growth_txt(last.get(m), a, b)
        if g: lines.append(f"   └ {plab} {oku(last.get(m))} 대비 {g}")
    a, b = r["rows"]["eps"]
    if b is not None:
        lines.append(f"EPS : {a if a is not None else '—'}엔 → {b:,.2f}엔")
        g = growth_txt(last.get("eps"), a, b)
        if g: lines.append(f"   └ {plab} {last['eps']:,.2f}엔 대비 {g}")
    try:
        items = [{"label": lab, "unit": "백만엔", "prev": (r["rows"][m][0] / 1e6 if r["rows"][m][0] is not None else None),
                  "new": r["rows"][m][1] / 1e6, "new_hi": None, "last": (last[m] / 1e6 if last.get(m) is not None else None)}
                 for m, lab in [("rev", "매출액"), ("op", "영업이익"), ("ord", "경상이익"), ("ni", "순이익")] if r["rows"][m][1] is not None]
        HL["chart"] = fc_chart(row, items, f"실적 전망 수정 · {fy} {per} · {r['scope']}")
    except Exception as e:
        log("rev chart error", row.get("code"), e)
    return "\n".join(lines)

# ───────────────────────── 차트(텔레그램 이미지) ─────────────────────────
CH = dict(INK="#0b0b0b", INK2="#52514e", MUT="#8a8983", GRID="#e6e5e0", SURF="#fcfcfb", BAR="#7d8b99", HI="#2a78d6", ALT="#eb6834", LIGHT="#c3cbd3")
_FONT_OK = False
def _plt():
    """matplotlib + 한글 글꼴. koreanize-matplotlib 패키지는 import하면 파이썬 3.12에서 실패(distutils 없음)해서
    글꼴이 기본(DejaVu)으로 남아 한글이 네모로 깨졌음 → 패키지에 든 나눔고딕 파일만 직접 등록"""
    global _FONT_OK
    import matplotlib; matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    if _FONT_OK: return plt
    from matplotlib import font_manager as fm
    try:
        import importlib.util, glob
        sp = importlib.util.find_spec("koreanize_matplotlib")
        for d in (sp.submodule_search_locations or []) if sp else []:
            for f in glob.glob(os.path.join(d, "fonts", "*.ttf")): fm.fontManager.addfont(f)
    except Exception as e:
        log("font error", e)
    for f in fm.findSystemFonts():
        if "NotoSansCJK" in f or "NanumGothic" in f:
            try: fm.fontManager.addfont(f)
            except Exception: pass
    names = {f.name for f in fm.fontManager.ttflist}
    for nm in ("NanumGothic", "Noto Sans CJK KR", "Noto Sans CJK JP"):
        if nm in names:
            plt.rcParams["font.family"] = nm; _FONT_OK = True; break
    else:
        log("한글 글꼴을 찾지 못했습니다")
    plt.rcParams["axes.unicode_minus"] = False
    return plt
def _fmt_k():
    import matplotlib.ticker as tk
    return tk.FuncFormatter(lambda v, _: f"{v:,.0f}" if abs(v) >= 10 or v == 0 else f"{v:,.1f}")
def _ax_style(ax):
    ax.set_facecolor(CH["SURF"]); ax.grid(axis="y", color=CH["GRID"], lw=0.7, zorder=0)
    for sp in ax.spines.values(): sp.set_visible(False)
    ax.tick_params(length=0, colors=CH["MUT"], labelsize=8)
def _save(fig, row, tag):
    path = os.path.join(os.environ.get("RUNNER_TEMP", "/tmp"), f"{tag}_{row.get('code')}_{row.get('d')}_{abs(hash(row.get('pdf', ''))) % 10**6}.png")
    fig.savefig(path, facecolor=CH["SURF"]); _plt().close(fig)
    return path
def _title(fig, row, sub):
    fig.suptitle(f"{row.get('_name') or row.get('code')}({row.get('code')}) {sub}", x=0.02, ha="left", fontsize=11, fontweight="bold", color=CH["INK"])

def fc_chart(row, items, head):
    """실적 전망(신규·수정): 항목별 작은 막대 — 전기 실적 / 이전 예상 / 이번 예상(범위면 위아래 표시)"""
    its = [i for i in items if i.get("new") is not None][:6]
    if not its: return None
    plt = _plt()
    n = len(its); cols = min(n, 3); rows_ = (n + cols - 1) // cols
    fig, axes = plt.subplots(rows_, cols, figsize=(3.0 * cols + 0.4, 2.7 * rows_ + 0.5), dpi=150, squeeze=False)
    fig.patch.set_facecolor(CH["SURF"])
    for ax in axes.flat[n:]: ax.set_visible(False)
    for ax, it in zip(axes.flat, its):
        _ax_style(ax)
        ax.yaxis.set_major_formatter(_fmt_k())
        unit = it.get("unit") or ""
        conv = (lambda v: v / 100) if unit == "백만엔" else (lambda v: v)  # 백만엔 → 억엔
        ul = "억엔" if unit == "백만엔" else unit
        bars = [("전기 실적", it.get("last"), CH["LIGHT"]), ("이전 예상", it.get("prev"), CH["BAR"]), ("이번 예상", it.get("new"), CH["HI"])]
        bars = [(l, conv(v), c) for l, v, c in bars if v is not None]
        xs = list(range(len(bars)))
        ax.bar(xs, [b[1] for b in bars], color=[b[2] for b in bars], width=0.6, zorder=3)
        hi = it.get("new_hi")
        if hi is not None:  # 범위 예상: 하단~상단을 선으로
            ax.plot([xs[-1], xs[-1]], [conv(it["new"]), conv(hi)], color=CH["INK"], lw=2, zorder=4)
        nf = lambda v: f"{v:,.0f}" if abs(v) >= 100 else f"{v:,.1f}"
        for x, (l, v, c) in zip(xs, bars):
            txt = nf(v) + (f"~{nf(conv(hi))}" if (hi is not None and l == "이번 예상") else "")
            ax.text(x, max(v, conv(hi)) if (hi is not None and l == "이번 예상") else v, txt, ha="center", va="bottom" if v >= 0 else "top", fontsize=8, color=CH["INK2"], zorder=5)
        ax.set_xticks(xs); ax.set_xticklabels([b[0] for b in bars], fontsize=8, color=CH["INK2"])
        base = it.get("prev") if it.get("prev") is not None else it.get("last")
        it = dict(it, new=(it["new"] + hi) / 2 if hi is not None else it["new"])  # 범위 예상은 중간값으로 변화율
        ch = f" ({pct(it['new'], base):+.1f}%)" if base and unit != "%" and pct(it["new"], base) is not None else (f" ({it['new'] - base:+.1f}%p)" if base is not None and unit == "%" else "")
        ax.set_title(f"{it.get('label', '')} ({ul}){ch}", loc="left", fontsize=9, color=CH["INK2"])
        lo = min(0, *[b[1] for b in bars]); top = max([b[1] for b in bars] + [conv(hi)] if hi is not None else [b[1] for b in bars])
        ax.set_ylim(lo * 1.15, top * 1.18 if top > 0 else 1)
    _title(fig, row, head)
    fig.tight_layout(rect=(0, 0, 1, 0.92))
    return _save(fig, row, "fc")

def res_chart(row, kq, single, end, ordlab="경상익"):
    """결산단신: 최근 분기 단독 매출·영업익 추이(이번 분기 강조)"""
    qs = [(k[0], k[1], k[2], k[3]) for k in kq if k[1] < end.replace(day=1) and k[2] is not None][-7:]
    if single and single.get("rev") is not None:
        qs.append((None, end, single["rev"] / 1e6, (single.get("op") / 1e6) if single.get("op") is not None else None))
    if len(qs) < 3: return None
    plt = _plt()
    fig, axes = plt.subplots(2, 1, figsize=(8, 5.2), dpi=150, sharex=True)
    fig.patch.set_facecolor(CH["SURF"])
    xs = list(range(len(qs))); labs = [f"{str(q[1].year)[2:]}.{q[1].month:02d}" for q in qs]
    for ax, idx, lab in ((axes[0], 2, "분기 매출(억엔)"), (axes[1], 3, "분기 영업이익(억엔)")):
        _ax_style(ax)
        vals = [(q[idx] / 100 if q[idx] is not None else None) for q in qs]
        cols = [CH["HI"] if (i == len(qs) - 1 and single) else CH["BAR"] for i in xs]
        ax.bar([x for x, v in zip(xs, vals) if v is not None], [v for v in vals if v is not None],
               color=[c for c, v in zip(cols, vals) if v is not None], width=0.62, zorder=3)
        for x, v in zip(xs, vals):
            if v is None: continue
            yoy = ""
            if x >= 4 and vals[x - 4] not in (None, 0) and vals[x - 4] > 0 and (qs[x][1].year - qs[x - 4][1].year == 1):
                yoy = f"\n{(v / vals[x - 4] - 1) * 100:+.0f}%"
            ax.text(x, v, f"{v:,.0f}" + yoy, ha="center", va="bottom" if v >= 0 else "top", fontsize=7.5, color=CH["INK2"], linespacing=1.0)
        ax.axhline(0, color=CH["INK2"], lw=0.6)
        top = max([v for v in vals if v is not None] + [0]); ax.set_ylim(min([v for v in vals if v is not None] + [0]) * 1.2, top * 1.3 if top > 0 else 1)
        ax.set_title(lab + " · 숫자 아래 %는 전년 동기 대비", loc="left", fontsize=9, color=CH["INK2"])
        ax.yaxis.set_major_formatter(_fmt_k())
    axes[1].set_xticks(xs); axes[1].set_xticklabels(labs, fontsize=8, color=CH["INK2"])
    _title(fig, row, "분기 실적 추이" + (" (파란 막대 = 이번 분기)" if single else ""))
    fig.tight_layout(rect=(0, 0, 1, 0.94))
    return _save(fig, row, "res")

FC_PROMPT = """너는 일본 주식 애널리스트다. 아래는 '실적 예상(업적 예상) 발표·수정' 적시공시 원문이다(표 모양 유지 텍스트). 공시일: {date}

할 일: 표의 수치를 뽑아라.
- kind: "수정"(이전 예상 A가 있고 바뀜) 또는 "신규"(처음 발표·범위 발표).
- period: 대상 기간을 한국어로(예: "FY3/27 연간", "FY12/26 연간", "FY3/27 상반기 누적"). 회계연도 표기는 FY결산월/연도2자리.
- scope: "연결" 또는 "개별". basis: Non-GAAP·IFRS 등 기준이 표에 명시되면 짧게, 아니면 "".
- items: 표의 열(지표)마다 하나. label은 한국어(매출액·영업이익·경상이익·순이익(지배주주)·EPS·EBITDA·매출총이익률·영업이익률 등).
  key: rev(매출)/op(영업이익)/ord(경상·세전이익)/ni(순이익)/eps/null.
  unit: 금액은 "백만엔"(천엔·억엔이면 백만엔으로 환산), 비율은 "%", 주당 금액은 "엔".
  prev = 이전 예상(A), new = 이번 예상(B, 범위면 하단), new_hi = 범위 상단(없으면 null), last = 참고 전기 실적. 없거나 "－"면 null. 숫자만(쉼표·부호 문자 제거, 마이너스는 음수).
- reason: 수정·발표 사유를 한국어 한 문장(80자 이내, 원문에 있는 내용만, 고유명사 한글). 없으면 "".
- 여러 기간 표가 있으면 연간(통기) 표를 우선.
- 실적 예상 수치가 없는 공시면 ok=false.
출력 JSON만: {{"ok": true, "kind": "수정", "period": "FY3/27 연간", "scope": "연결", "basis": "", "items": [{{"label": "매출액", "key": "rev", "unit": "백만엔", "prev": 1030000, "new": 1060000, "new_hi": null, "last": 982804}}], "reason": "..."}}

원문:
{text}
"""
def _num(v):
    try: return float(str(v).replace(",", ""))
    except (TypeError, ValueError): return None
def body_forecast_pdf(row, st):
    """XBRL이 없는 실적 예상 발표·수정 공시: 원문 표를 AI로 읽어 숫자 + 차트"""
    raw = pdf_layout(row["pdf"], pages=2)
    if len(re.findall(r"\d", raw)) < 10: return None
    cap = st.setdefault("mon_ai", {"d": row["d"], "n": 0})
    if cap.get("d") != row["d"]: cap.update(d=row["d"], n=0)
    if not os.environ.get("GEMINI_API_KEY") or cap["n"] >= MON_AI_CAP: return None
    cap["n"] += 1
    t = re.sub(r"[ \u3000]{3,}", "   ", raw); t = re.sub(r"\n\s*\n+", "\n", t)
    j = gemini_json(FC_PROMPT.format(date=f"{row['d'][:4]}-{row['d'][4:6]}-{row['d'][6:]}", text=t[:7000]), timeout=90, models=MON_MODELS)
    if not (j and j.get("ok") and j.get("items")): return None
    items = []
    for it in j["items"][:8]:
        d = {k: _num(it.get(k)) for k in ("prev", "new", "new_hi", "last")}
        if d["new"] is None and d["prev"] is None: continue
        d.update(label=str(it.get("label") or "")[:14], key=it.get("key"), unit=str(it.get("unit") or "")[:4])
        items.append(d)
    if not items: return None
    kind = "수정" if j.get("kind") == "수정" else "발표"
    head = f"실적 전망 {kind} · {j.get('period') or ''}".strip() + (f" · {j.get('scope')}" if j.get("scope") else "") + (f" · {j['basis']}" if j.get("basis") else "")
    def fmt(v, u, hi=None):
        if v is None: return "—"
        if u == "백만엔": s_ = oku(v * 1e6) + ("~" + oku(hi * 1e6) if hi is not None else "")
        elif u == "%": s_ = f"{v:.1f}%" + (f"~{hi:.1f}%" if hi is not None else "")
        else: s_ = f"{v:,.2f}엔" + (f"~{hi:,.2f}엔" if hi is not None else "")
        return s_
    lines = [f"<b>[{esc(head)}]</b>"]
    rows_hl = {}
    for it in items:
        u = it["unit"]
        if it["prev"] is not None and it["new"] is not None:
            c = (f"{it['new'] - it['prev']:+.1f}%p" if u == "%" else (f"{pct(it['new'], it['prev']):+.1f}%" if it["prev"] else ""))
            ln = f"{esc(it['label'])} : {fmt(it['prev'], u)} → {fmt(it['new'], u, it['new_hi'])}" + (f" ({c})" if c else "")
        else:
            ln = f"{esc(it['label'])} : {fmt(it['new'], u, it['new_hi'])}"
        if it["last"] is not None and it["new"] is not None:
            mid = it["new"] if it["new_hi"] is None else (it["new"] + it["new_hi"]) / 2
            gr = lambda v: (f"{v - it['last']:+.1f}%p" if u == "%" else (f"{pct(v, it['last']):+.1f}%" if it["last"] > 0 else "")) if v is not None else ""
            c1, c2 = gr(it["prev"]), gr(mid)
            if c2: ln += f"\n   └ 전기 {fmt(it['last'], u)} 대비 " + (f"{c1} → {c2}" if c1 else c2)
        lines.append(ln)
        if it.get("key") in ("rev", "op", "ord", "ni") and u == "백만엔" and it["new"] is not None:
            rows_hl[it["key"]] = (it["prev"] * 1e6 if it["prev"] is not None else None, it["new"] * 1e6)
    if j.get("reason"): lines.append(f"사유 : {esc(str(j['reason'])[:120])}")
    lines.append("<i>※ 원문 표를 AI로 읽은 값 — 중요 수치는 원문 확인</i>")
    if kind == "수정" and rows_hl:
        for m in ("rev", "op", "ord", "ni", "eps"): rows_hl.setdefault(m, (None, None))
        HL.update(kind="rev", fy=j.get("period") or "", per="", rows=rows_hl)
    try:
        HL["chart"] = fc_chart(row, items, head)
    except Exception as e:
        log("fc chart error", row.get("code"), e)
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
    if r["last"] is not None:
        s += f"\n전기 실적 : {f(r['last'])}"
        if r["last"] and r["curr"] is not None:
            s += f" → 전기 대비 " + (f"{pct(r['prev'], r['last']):+.1f}% → " if r["prev"] else "") + f"{pct(r['curr'], r['last']):+.1f}%"
    return s

def pdf_text(url):
    from pdfminer.high_level import extract_text
    return nfkc(extract_text(io.BytesIO(http(url, timeout=40)), maxpages=3))

DATE = r"(\d{4})年(\d{1,2})月(\d{1,2})日(?:\([^)]*\))?"
def body_buyback(row, q=None):
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
        mc = (q or {}).get("marketCap")
        parts.append(f"{oku(yen)}엔" + (f"(시가총액의 {yen / mc * 100:.1f}%)" if mc else ""))
    HL.update(kind="bb", ratio=float(ratio) if ratio else None, yen=yen if amt else None)
    s = "<b>[자사주 취득 결정]</b>\n" + " / ".join(parts)
    if per: s += f"\n기간 : {per[0]}.{int(per[1]):02d}.{int(per[2]):02d}~{per[3]}.{int(per[4]):02d}.{int(per[5]):02d}"
    return s

def body_tostnet(row):
    """ToSTNeT-3(장 시작 전 8:45 시간외 거래)로 자사주를 사는 공시: 매수 가격·주식 수·금액·비율, 근거가 된 자사주 취득 결의"""
    t = re.sub(r"\s+", "", pdf_text(row["pdf"]))
    p = re.search(r"終値[^0-9]{0,20}([\d,]+(?:\.\d+)?)円", t)
    n = re.search(r"(?:取得し得る|取得予定|買付け?予定)株式(?:の)?総?数[^0-9]{0,6}([\d,.]+)(万|千)?株", t)
    when = re.search(DATE + r"午前(\d{1,2})時(\d{1,2})分", t)
    if not (p and n): return None
    price = float(p.group(1).replace(",", ""))
    sh = float(n.group(1).replace(",", "")) * {"万": 1e4, "千": 1e3, None: 1}[n.group(2)]
    lines = ["<b>[자사주 매입 · ToSTNeT-3(장 시작 전 시간외 거래)]</b>",
             f"매수 : {sh:,.0f}주 × {price:,.1f}엔(공시일 종가) = 약 {oku(price * sh)}엔"]
    ref = t[t.find("参考"):] if "参考" in t else ""
    rn = re.search(r"株式の総数([\d,.]+)(万|千)?株", ref); rr = re.search(r"割合([\d.]+)%", ref); ra = re.search(r"総額([\d,.]+)(百万|千|億)?円", ref)
    ratio = None
    if rn and rr:
        tot = float(rn.group(1).replace(",", "")) * {"万": 1e4, "千": 1e3, None: 1}[rn.group(2)]
        if tot: ratio = sh / tot * float(rr.group(1)); lines[-1] += f" (자사주 제외 발행주식의 약 {ratio:.1f}%)"
    if when: lines.append(f"매수일 : {when.group(1)}.{int(when.group(2)):02d}.{int(when.group(3)):02d} {int(when.group(4)):02d}:{int(when.group(5)):02d}")
    if rn:
        prog = f"근거 결의 : 최대 {tot:,.0f}주" + (f"(발행주식의 {rr.group(1)}%)" if rr else "")
        if ra: prog += f" · {oku(float(ra.group(1).replace(',', '')) * {None: 1, '千': 1e3, '百万': 1e6, '億': 1e8}[ra.group(2)])}엔"
        lines.append(prog)
    if ra:
        cap_yen = float(ra.group(1).replace(',', '')) * {None: 1, '千': 1e3, '百万': 1e6, '億': 1e8}[ra.group(2)]
        if price * sh > cap_yen * 1.001: lines.append(f"※ 결의 금액 한도({oku(cap_yen)}엔) 안에서만 매수")
    lines.append("※ 미리 나온 매도 물량(대주주·정책보유 해소 등)을 공시일 종가로 한 번에 사는 방식. 실제 매수량은 다음 날 결과 공시로 확정")
    HL.update(kind="bb", ratio=round(ratio, 2) if ratio else None, yen=price * sh)
    return "\n".join(lines)

def body_cancel(row, q=None):
    """자사주 소각: 소각 주식 수·발행주식 대비 비율·시가 환산 금액·소각일"""
    t = re.sub(r"\s+", "", pdf_text(row["pdf"]))
    k = t.find("消却する株式の"); t2 = t[k:] if k >= 0 else t
    n = re.search(r"消却する株式の(?:総)?数[^0-9]{0,20}([\d,.]+)(万|千)?株", t2)
    if not n:
        m0 = re.search(r"消却する株式の(?:総)?数(.{0,100}?)(全株式|全数)", t2)
        if m0:
            d = re.search(r"消却(?:予定)?日[^0-9]{0,4}" + DATE, t2)
            return "<b>[자사주 소각]</b>\n소각 : 이번에 취득하는 자사주 전부" + (" (일부는 제외 — 원문 확인)" if "除" in m0.group(1) else "") + (f"\n소각일 : {d.group(1)}.{int(d.group(2)):02d}.{int(d.group(3)):02d}" if d else "")
        return None
    sh = float(n.group(1).replace(",", "")) * {"万": 1e4, "千": 1e3, None: 1}[n.group(2)]
    r = re.search(r"割合[^0-9]{0,4}([\d.]+)%", t2)
    d = re.search(r"消却(?:予定)?日[^0-9]{0,4}" + DATE, t2)
    after = re.search(r"消却後の(?:当社の)?発行済株式(?:の)?総数[^0-9]{0,12}([\d,]+)株", t2)
    line = f"소각 : {sh:,.0f}주" + (f" (소각 전 발행주식의 {r.group(1)}%)" if r else "")
    mc = (q or {}).get("marketCap")
    if mc and r: line += f" · 현재 시가 약 {oku(mc * float(r.group(1)) / 100)}엔"
    lines = ["<b>[자사주 소각]</b>", line]
    if d: lines.append(f"소각일 : {d.group(1)}.{int(d.group(2)):02d}.{int(d.group(3)):02d}")
    if after: lines.append(f"소각 후 발행주식 : {after.group(1)}주")
    if re.search(r"保有する自己株式の(全部|全て|すべて)", t): lines.append("※ 보유 자사주 전부 소각")
    if re.search(r"株式併合|承認可決されることを条件", t): lines.append("※ 조건부(주주총회 승인·주식병합 등) — 원문 확인")
    return "\n".join(lines)

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

# ───────────────────────── 월차(월별 매출) → 전년비 추출 ─────────────────────────
def pdf_layout(url, pages=3):
    """표 모양을 유지한 텍스트(pdftotext -layout). 없으면 pdfminer 텍스트"""
    import subprocess, tempfile
    data = http(url, timeout=40)
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
            f.write(data); f.flush()
            out = subprocess.run(["pdftotext", "-layout", "-l", str(pages), f.name, "-"], capture_output=True, timeout=30).stdout.decode("utf-8", "ignore")
        if out.strip(): return nfkc(out)
    except Exception as e:
        log("pdftotext error", e)
    from pdfminer.high_level import extract_text
    return nfkc(extract_text(io.BytesIO(data), maxpages=pages))

# ── 월차 표 규칙 파서(pdftotext -layout 텍스트) — AI가 없을 때·교차 확인용 ──
MP_NUM = re.compile(r"(?<![\d.])([-+±▲△]?\s?\d{1,3}(?:,\d{3})*(?:\.\d+)?)\s?%?(?![\d,]*\.?\d*\s?(?:年|日|店|円|人|名|坪|席|台|社|件))")
MP_MON = re.compile(r"(?:(\d{2,4})\s?[/.年]\s?)?(\d{1,2})\s?月")
MP_STOP = re.compile(r"^\s*(?:[0-9０-９]{1,2}\s*[\.．、]\s*\S|[■◆●□【]|[（(]\s*(?:ご参考|参考|注))")
MP_GRP = [("ex", r"既存"), ("all", r"全店|全社|合計|全体|連結|総計|全館|全事業")]

def _tok(line):
    """2칸 이상 공백으로 나뉜 덩어리 → [(시작, 끝, 글자)]"""
    return [(m.start(), m.end(), m.group(0).strip()) for m in re.finditer(r"\S+(?:\s\S+)*", line)]

def _nums(line, start=0):
    out = []
    for m in MP_NUM.finditer(line, start):
        s = m.group(1).replace(" ", "").replace(",", "")
        neg = s[:1] in "-▲△"
        s = s.lstrip("-+±▲△")
        try: v = float(s)
        except ValueError: continue
        out.append(((m.start(1) + m.end(1)) / 2, m.start(1), m.end(1), -v if neg else v))
    return out

def _ym(m, y, ref):
    """월 → 'YYYY-MM' (연도 없으면 기준월 이전의 가장 가까운 해)"""
    if y:
        y = int(y); y = y + 2000 if y < 100 else y
    else:
        y = ref.year if m <= ref.month else ref.year - 1
    return f"{y}-{m:02d}"

def _grp(label):
    l = re.sub(r"\s", "", label)
    for g, p in MP_GRP:
        if re.search(p, l): return g
    return None

def _metric(label):
    l = re.sub(r"\s", "", label)
    if re.search(r"客単価|単価", l): return "price"
    if re.search(r"客数|来店|人数", l): return "cust"
    return "sales"

def _finish(cells, ref):
    """cells: {(ym, grp, metric): [값]} → months dict. 비율(100 기준)이면 증감률로 변환"""
    vals = [v for vs in cells.values() for v in vs]
    if not vals: return None
    if all(50 <= v <= 250 for v in vals): ratio = True
    elif all(-60 < v < 60 for v in vals): ratio = False
    else: return None  # 비율·증감률이 섞였거나 금액 → 신뢰 불가
    months = {}
    for (ym, g, met), vs in cells.items():
        v = vs[0]
        if ratio: v = round(v - 100, 1)
        if not -100 < v < 500: continue
        key = {"sales": "ex" if g == "ex" else "all", "cust": "cust", "price": "price"}[met]
        if met in ("cust", "price") and g == "all":
            key = key + "_all"
        months.setdefault(ym, {})
        months[ym].setdefault(key, v)
    out = {}
    for ym, d in months.items():
        r = {"all": d.get("all"), "ex": d.get("ex"),
             "cust": d.get("cust", d.get("cust_all") if d.get("ex") is None else None),
             "price": d.get("price", d.get("price_all") if d.get("ex") is None else None)}
        if r["all"] is not None or r["ex"] is not None: out[ym] = r
    k = lambda ym: (ref.year - int(ym[:4])) * 12 + ref.month - int(ym[5:])
    out = {ym: v for ym, v in out.items() if 0 <= k(ym) <= 14}
    return out or None

def parse_cols(lines, ref):
    """월이 가로(열)로 놓인 표"""
    cells = {}; started = False; i = 0
    while i < len(lines):
        ln = lines[i]
        if started and MP_STOP.match(ln): break
        bare = re.search(r"[（(]\s*月\s*[)）]", ln)
        mons = [(m.start(), m.end(), int(m.group(2)), m.group(1)) for m in MP_MON.finditer(ln) if 1 <= int(m.group(2)) <= 12]
        if bare and len(mons) < 3:
            mons = [(m.start(), m.end(), int(m.group(0)), None) for m in re.finditer(r"(?<![\d.])\d{1,2}(?![\d.])", ln[bare.end():]) if 1 <= int(m.group(0)) <= 12]
            mons = [(a + bare.end(), b + bare.end(), mm, y) for a, b, mm, y in mons]
        if len(mons) < 3:
            i += 1; continue
        # 이 헤더 아래의 데이터 줄
        rows = []; j = i + 1; ctx = None
        while j < len(lines) and j < i + 30:
            l2 = lines[j]
            if MP_STOP.match(l2) or (len([1 for m in MP_MON.finditer(l2)]) >= 3): break
            ns = _nums(l2)
            first = ns[0][1] if ns else len(l2)
            label = l2[:first].strip()
            if label and not ns and _grp(label): ctx = _grp(label)
            if ns and not re.search(r"前年(?!.{0,3}比)|金額|人数|店舗数|出店|退店|天候|気温|日数|曜日|平年|^\s*\d", re.sub(r"\s", "", label) or "x"):
                rows.append([label, ns, _grp(label), j])
            elif ns and not label:
                rows.append(["", ns, None, j])
            j += 1
        # 그룹이 없는 줄은 가까운 줄(±2, 위쪽 우선)의 그룹을 따름
        for r in rows:
            if r[2]: continue
            best = None
            for d in (1, 2):
                for r2 in rows:
                    if r2[2] and abs(r2[3] - r[3]) == d and r2[3] < r[3] and best is None: best = r2[2]
                for r2 in rows:
                    if r2[2] and abs(r2[3] - r[3]) == d and best is None: best = r2[2]
            r[2] = best or ctx
        for label, ns, g, _ in rows:
            met = _metric(label)
            for c, a, b, v in ns:
                hit = [mm for mm in mons if mm[0] - 2 <= c <= mm[1] + 2 or (a <= mm[1] and b >= mm[0])]
                if len(hit) != 1: continue
                _, _, mo, y = hit[0]
                cells.setdefault((_ym(mo, y, ref), g, met), []).append(v)
        started = started or bool(cells)
        i = j
    return _finish(cells, ref)

def parse_rows(lines, ref):
    """월이 세로(행 머리)로 놓인 표"""
    cells = {}; hdr = None; started = False
    rowre = re.compile(r"^\s*(?:(\d{2,4})\s?[/.年]\s?)?(\d{1,2})\s?月(?:度|分)?\s")
    for i, ln in enumerate(lines):
        if started and MP_STOP.match(ln): break
        m = rowre.match(ln)
        if not m:
            if started and hdr is not None and not ln.strip(): continue
            continue
        ns = _nums(ln, m.end())
        if not ns: continue
        mo = int(m.group(2))
        if not 1 <= mo <= 12: continue
        if hdr is None:
            hdr = []
            for l in reversed(lines[max(0, i - 6):i]):
                if not l.strip(): continue
                if _nums(l) or re.search(r"[、。]|について|お知らせ|単位", l): break
                hdr.insert(0, _tok(l))
            hdr = [h for h in hdr if h][-4:]
            if not hdr: return None
            ncol = len(ns)
        for c, a, b, v in ns:
            lab = []
            for toks in hdr:
                t = [t for t in toks if t[0] - 2 <= c <= t[1] + 2]
                if t: lab.append(t[0][2])
                elif len(toks) <= ncol / 2:  # 여러 열을 묶는 머리글: 가장 가까운 것
                    lab.append(min(toks, key=lambda t: abs((t[0] + t[1]) / 2 - c))[2])
            L = " ".join(lab)
            if re.search(r"前年(?!.{0,3}比)|金額|人数|店舗|出店|退店|月末|期末", re.sub(r"\s", "", L)): continue
            g = _grp(L)
            if g is None and re.search(r"既存", " ".join(t[2] for h in hdr for t in h)) and not re.search(r"全", L): continue
            cells.setdefault((_ym(mo, m.group(1), ref), g, _metric(L)), []).append(v)
        started = True
    return _finish(cells, ref)

def mon_parse(text, title, d):
    """d: 공시일 'YYYYMMDD'. 반환 {ym: {all, ex, cust, price}} 또는 None"""
    t = nfkc(text).replace("　", " ")
    t = re.sub(r"[ ]?([▲△])\s?", r" -", t)
    lines = t.split("\n")
    ref = dt.date(int(d[:4]), int(d[4:6]), 1)
    m = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月", nfkc(title)) or re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月度?", t[:3000])
    if m and 1 <= int(m.group(2)) <= 12:
        r2 = dt.date(int(m.group(1)), int(m.group(2)), 1)
        if 0 <= (ref.year - r2.year) * 12 + ref.month - r2.month <= 3: ref = r2
    if re.search(r"単位\s*[:：]?\s*(?:百万|千|億)?円", t[:6000]) and not re.search(r"前年比|前年同月比|対前年|増減率|伸び率|YoY", t[:6000]): return None
    best = None
    for f in (parse_cols, parse_rows):
        try: r = f(lines, ref)
        except Exception: r = None
        if r and (best is None or len(r) > len(best)): best = r
    if not best: return None
    tm = re.search(r"(\d{1,2})\s*月", re.sub(r"\d{4}\s*年\s*\d{1,2}\s*月期", "", nfkc(title)))
    last = max(best)
    if tm and int(tm.group(1)) != int(last[5:]): return None  # 제목의 달과 표의 최신 달이 다르면 버림
    gap = (int(d[:4]) * 12 + int(d[4:6])) - (int(last[:4]) * 12 + int(last[5:]))  # 공시월 - 최신 데이터월
    if gap < 0 or gap > 2 or (gap == 0 and int(d[6:]) < 15): return None
    return best

MON_PROMPT = """너는 일본 소매·외식 기업 월차 공시를 읽는 애널리스트다. 아래는 월차(월별 실적) 공시 원문(표 모양 유지 텍스트)이다. 공시일: {date}

할 일: 이번 회계연도 표에서 월별 "전년 동월 대비 증감률(%)"을 뽑아라.
- all: 전사·전체(全店·全社·合計) 매출 증감률 / ex: 기존점(既存店) 매출 / cust: 기존점 객수(客数) / price: 기존점 객단가(客単価). 기존점 객수·객단가가 없고 전점 것만 있으면 그것을 쓴다.
- 102.3(%)처럼 전년=100 비율이면 +2.3으로, 이미 증감률(12.2%増, -0.9)이면 그대로. 금액만 있고 전년 금액도 있으면 계산, 아니면 null.
- '前年' 열(작년의 전년비)·전전년 비교·참고용 지난 회계연도 표는 쓰지 마라. 아직 안 나온 미래 달은 넣지 마라.
- 사업이 여럿이면 회사 전체 합계, 합계가 없으면 가장 큰 주력 사업(국내 직영 등). metric은 지표 이름(보통 "매출", 발전량·수주 등이면 그 이름).
- ym은 "YYYY-MM". 연도는 제목·공시일로 판단(예: 2026년 9월 공시의 4~9월 = 2026-04~2026-09, 회계연도가 해를 넘기면 주의).
- cum: 회계연도 누계(累計·上期 등)가 있으면 {{"label":"상반기 누계" 같은 한국어, "all":.., "ex":..}}, 없으면 null.
- amt: 그 달 매출 금액(전사 기준)이 표에 있으면 백만엔 단위 숫자로(천엔·억엔이면 환산), 없으면 null. 증감률을 금액으로 착각하지 말 것.
- stores: 그 달 월말 점포수(직영 기준 합계, 표에 있으면), 없으면 null. 출점·퇴점 수가 아니라 월말 잔고.
- prev: 표에 '前年' 열(작년 같은 달의 전년 동월 대비)이 있으면 그 값을 작년 달(ym에서 12개월 전)로 [{{"ym": "2025-09", "all": .., "ex": ..}}]. 이번 회계연도 표 아래 하반기 '前年' 값도 포함. 없으면 [].
- 월차 매출 성격이 아니면(암호자산 보유 현황, 수치 없는 안내 등) ok=false.
출력 JSON만: {{"ok": true, "metric": "매출", "scope": "국내 직영점 등 대상 한국어 짧게(없으면 빈 문자열)", "months": [{{"ym": "2026-09", "all": 11.8, "ex": 8.3, "cust": 6.0, "price": 2.2, "amt": null, "stores": 2140}}], "prev": [{{"ym": "2025-09", "all": 4.7, "ex": 1.8}}], "cum": null}}

원문:
{text}
"""
MON_COLS = [("all", "전체"), ("ex", "기존점"), ("cust", "객수"), ("price", "객단가")]
def _f(v):
    try:
        v = float(v)
        return v if -100 < v < 1000 else None
    except (TypeError, ValueError):
        return None
def _fp(v):
    try:
        v = float(str(v).replace(",", ""))
        return v if 0 < v < 1e9 else None
    except (TypeError, ValueError):
        return None
def _w(s): return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)
def _pad(s, n): return " " * max(n - _w(s), 0) + s
MON_AI_CAP = int(os.environ.get("MON_AI_CAP", "80"))  # 하루 AI 호출 상한(다른 봇과 키 공유 대비)
def mon_extract(row, st=None):
    raw = pdf_layout(row["pdf"])
    if len(re.findall(r"\d", raw)) < 10: return None  # 이미지 PDF 등
    d = row["d"]
    try: det = mon_parse(raw, row.get("title", ""), d)
    except Exception as e: det = None; log("mon parse error", e)
    t = re.sub(r"[ \u3000]{3,}", "   ", raw); t = re.sub(r"\n\s*\n+", "\n", t)
    j = None
    st = st if st is not None else {}
    cap = st.setdefault("mon_ai", {"d": d, "n": 0})
    if cap.get("d") != d: cap.update(d=d, n=0)
    if os.environ.get("GEMINI_API_KEY") and cap["n"] < MON_AI_CAP:
        cap["n"] += 1
        j = gemini_json(MON_PROMPT.format(date=f"{d[:4]}-{d[4:6]}-{d[6:]}", text=t[:6000]), timeout=90, models=MON_MODELS)
    if not (j and j.get("ok")):
        if j and not j.get("ok"): return None  # AI가 월차 매출이 아니라고 판단
        if not det: return None
        # AI 없이 규칙 파서만: 매출(전체·기존점)만 사용
        det = {k: {"all": v.get("all"), "ex": v.get("ex"), "cust": None, "price": None} for k, v in det.items()}
        return {"metric": "매출", "scope": "", "months": det, "cum": None, "src": "rule"}
    ref = dt.date(int(d[:4]), int(d[4:6]), 1)
    months = {}
    for m in j.get("months") or []:
        ym = str(m.get("ym") or "")
        if not re.fullmatch(r"\d{4}-\d{2}", ym): continue
        k = (ref.year - int(ym[:4])) * 12 + ref.month - int(ym[5:])
        if not 0 <= k <= 14: continue  # 공시일 기준 미래·너무 오래된 달 제외
        v = {c: _f(m.get(c)) for c, _ in MON_COLS}
        if any(x is not None for x in v.values()):
            v["amt"], v["stores"] = _fp(m.get("amt")), _fp(m.get("stores"))
            months[ym] = v
    if not months: return None
    prev = {}
    for m in j.get("prev") or []:  # 표의 '前年' 열 = 작년 같은 달의 전년비(차트 과거 구간용)
        ym = str(m.get("ym") or "")
        if not re.fullmatch(r"\d{4}-\d{2}", ym) or ym in months: continue
        k = (ref.year - int(ym[:4])) * 12 + ref.month - int(ym[5:])
        if not 1 <= k <= 26: continue
        v = {"all": _f(m.get("all")), "ex": _f(m.get("ex"))}
        if any(x is not None for x in v.values()): prev[ym] = v
    cum = j.get("cum") if isinstance(j.get("cum"), dict) else None
    src = "ai"
    if det:  # 규칙 파서와 최신 달 값 교차 확인
        last = max(months); a, b = months[last], det.get(last) or {}
        diff = [c for c in ("all", "ex") if a.get(c) is not None and b.get(c) is not None and abs(a[c] - b[c]) > 0.15]
        src = "mismatch" if diff else ("ai+rule" if any(b.get(c) is not None for c in ("all", "ex")) else "ai")
        if diff:  # 값이 다르면 표 자동 인식값(매출)을 쓰고 객수·객단가·누계·前年 열은 뺌 — 점검 결과 매출은 규칙 인식이 더 정확했음
            months = {k: {"all": v.get("all"), "ex": v.get("ex"), "cust": None, "price": None} for k, v in det.items()}
            cum = None; prev = {}
    return {"metric": str(j.get("metric") or "매출")[:8], "scope": str(j.get("scope") or "")[:30], "months": months, "prev": prev, "cum": cum, "src": src}

def mon_chart(row, hist, met, scope):
    """월차 차트 PNG: 위 = 절대 규모(월 매출 금액, 없으면 월말 점포수), 아래 = 전년 동월 대비(전체·기존점)"""
    ks = sorted(hist)[-18:]
    yo = [k for k in ks if hist[k].get("all") is not None or hist[k].get("ex") is not None]
    if len(yo) < 4: return None
    ks = ks[ks.index(yo[0]):]
    plt = _plt()
    import matplotlib
    absk, absl = None, None
    for key, lab in (("amt", f"월 {met} (백만엔)"), ("stores", "월말 점포수")):
        if sum(1 for k in ks if hist[k].get(key)) >= 3: absk, absl = key, lab; break
    INK, INK2, MUT, GRID, SURF = "#0b0b0b", "#52514e", "#8a8983", "#e6e5e0", "#fcfcfb"
    C_ALL, C_EX = "#2a78d6", "#eb6834"
    n = 2 if absk else 1
    fig, axes = plt.subplots(n, 1, figsize=(8, 5.4 if n == 2 else 3.4), dpi=150, sharex=True,
                             gridspec_kw={"height_ratios": [1, 1.25]} if n == 2 else None)
    axes = list(axes) if n == 2 else [axes]
    fig.patch.set_facecolor(SURF)
    x = list(range(len(ks))); labs = [f"{k[2:4]}/{k[5:]}" for k in ks]
    for ax in axes:
        ax.set_facecolor(SURF); ax.grid(axis="y", color=GRID, lw=0.7, zorder=0)
        for sp in ax.spines.values(): sp.set_visible(False)
        ax.tick_params(length=0, colors=MUT, labelsize=8)
    if absk:
        ax = axes[0]; vals = [hist[k].get(absk) for k in ks]
        px = [i for i, v in zip(x, vals) if v]; py = [v for v in vals if v]
        if absk == "stores":  # 점포수는 변화가 작아 막대(0부터) 대신 선으로 추이를 보이게
            ax.plot(px, py, color="#7d8b99", lw=2, marker="o", ms=4, zorder=3)
            lo, hi = min(py), max(py); pad = max((hi - lo) * 0.3, 1)
            ax.set_ylim(lo - pad, hi + pad * 1.5)
        else:
            ax.bar(px, py, width=0.62, color="#7d8b99", zorder=3)
        last = max(px)
        ax.text(last, vals[last], f"{vals[last]:,.0f}", ha="center", va="bottom", fontsize=8, color=INK2)
        ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:,.0f}"))
        ax.set_title(absl, loc="left", fontsize=9, color=INK2)
    ax = axes[-1]
    for c, lab, col in (("all", "전체", C_ALL), ("ex", "기존점", C_EX)):
        pts = [(i, hist[k][c]) for i, k in zip(x, ks) if hist[k].get(c) is not None]
        if len(pts) < 2: continue
        ax.plot([p[0] for p in pts], [p[1] for p in pts], color=col, lw=2, marker="o", ms=4, label=lab, zorder=3)
        ax.text(pts[-1][0] + 0.15, pts[-1][1], f"{lab} {pts[-1][1]:+.1f}%", va="center", fontsize=8, color=INK2)
    ax.axhline(0, color=INK2, lw=0.8, zorder=2)
    ax.set_title(f"{met} 전년 동월 대비(%)", loc="left", fontsize=9, color=INK2)
    ax.legend(loc="upper left", frameon=False, fontsize=8, ncol=2)
    ax.set_xticks(x); ax.set_xticklabels(labs, fontsize=8, color=INK2)
    ax.set_xlim(-0.6, len(ks) - 0.2 + 1.2)
    name = row.get("_name") or row.get("code")
    fig.suptitle(f"{name}({row.get('code')}) 월차" + (f" · {scope}" if scope else ""), x=0.02, ha="left", fontsize=11, fontweight="bold", color=INK)
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    path = os.path.join(os.environ.get("RUNNER_TEMP", "/tmp"), f"mon_{row.get('code')}_{row.get('d')}.png")
    fig.savefig(path, facecolor=SURF); plt.close(fig)
    return path

def body_monthly(row, st):
    r = mon_extract(row, st)
    if not r: return None
    code = row["code"]
    h = st.setdefault("monh", {}).setdefault(code, {})
    for k, v in (r.get("prev") or {}).items():  # 작년 달: 이미 받은 값이 있으면 그대로
        if k not in h: h[k] = v
    h.update(r["months"]); h["_t"] = time.time()
    for k in sorted(k for k in h if k[:1].isdigit())[:-24]: h.pop(k)  # 최근 24개월만 보관(차트용)
    if len(st["monh"]) > 1500:
        for c in sorted(st["monh"], key=lambda c: st["monh"][c].get("_t", 0))[:200]: st["monh"].pop(c)
    hist = {k: v for k, v in h.items() if k[:1].isdigit()}
    last = max(r["months"]); lv = r["months"][last]
    met = r["metric"]; mm = int(last[5:])
    fx = lambda v: f"{v:+.1f}%"
    main = []
    if lv.get("all") is not None: main.append(f"전체 {met} {fx(lv['all'])}")
    if lv.get("ex") is not None: main.append(f"기존점 {fx(lv['ex'])}")
    sub = [f"{l} {fx(lv[c])}" for c, l in MON_COLS[2:] if lv.get(c) is not None]
    line = f"{mm}월 " + " · ".join(main) + (f" ({' · '.join(sub)})" if sub else "")
    s = f"<b>[월차 · 전년 동월 대비]</b>" + (f" {esc(r['scope'])}" if r["scope"] else "") + f"\n<b>{line}</b>"
    cum = r.get("cum") or {}
    cs = [f"{l} {fx(_f(cum.get(c)))}" for c, l in MON_COLS[:2] if _f(cum.get(c)) is not None]
    if cs: s += f"\n{esc(str(cum.get('label') or '누계'))[:12]} : " + " · ".join(cs)
    ks = sorted(hist)[-6:]
    cols = [(c, l) for c, l in MON_COLS if any(hist[k].get(c) is not None for k in ks)]
    trend = ""
    if len(ks) >= 2 and cols:
        rows = ["월   " + "".join(_pad(l, 7) for _, l in cols)]
        for k in ks:
            rows.append(f"{k[2:4]}/{k[5:]}" + "".join(_pad("-" if hist[k].get(c) is None else f"{hist[k][c]:+.1f}", 7) for c, _ in cols))
        s += "\n최근 추이(전년 동월 대비 %)\n<pre>" + esc("\n".join(rows)) + "</pre>"
        tc = "ex" if any(hist[k].get("ex") is not None for k in ks) else "all"
        vals = [f"{hist[k][tc]:+.1f}" for k in ks if hist[k].get(tc) is not None]
        if len(vals) >= 2: trend = f" | {'기존점' if tc == 'ex' else '전체'} 추이 {int(ks[0][5:])}~{mm}월 " + "→".join(vals)
    note = {"rule": "※ AI 미사용 — 표 자동 인식값(매출만), 원문 확인 권장", "mismatch": "⚠ AI 판독값과 표 자동 인식값이 달라 표 인식값(매출)만 표시 — 원문 확인 권장"}.get(r.get("src"))
    if note: s += f"\n<i>{note}</i>"
    try:
        HL["chart"] = mon_chart(row, hist, met, r.get("scope") or "")
    except Exception as e:
        log("mon chart error", code, e)
    HL.update(kind="mon", mon=line + trend + (" ⚠원문 확인" if r.get("src") == "mismatch" else ""))
    return s

# ───────────────────────── 공시 메시지 보강: 회사 한 줄 소개 · 사유 ─────────────────────────
def biz_line(st, code):
    """카부탄 '개요'(회사 한 줄 소개) → 한국어. 30일 캐시"""
    pc = st.setdefault("prof", {})
    c = pc.get(code)
    if c and time.time() - c[0] < 30 * 86400: return c[1]
    ko = ""
    try:
        h = http(f"https://kabutan.jp/stock/?code={code}", timeout=20).decode("utf-8", "ignore")
        m = re.search(r"<th[^>]*>概要</th>\s*<td[^>]*>(.*?)</td>", h, re.S)
        jp = nfkc(re.sub(r"<[^>]+>", "", m.group(1))).strip() if m else ""
        if jp: ko = (translate(st, [jp]).get(jp) or "").strip()
    except Exception as e:
        log("biz error", code, e)
    if ko or not c: pc[code] = [time.time(), ko]
    if len(pc) > 3000:
        for k in sorted(pc, key=lambda k: pc[k][0])[:500]: pc.pop(k, None)
    return ko or (c[1] if c else "")

WHY_PROMPT = """아래는 일본 기업 적시공시 원문(일본어)에서 이유·설명 부분을 잘라낸 글이다. 공시 종류: {kind}. 공시 제목: {title}
투자자가 "왜 이렇게 됐나"를 바로 알 수 있게 한국어로 1~2문장(합계 120자 이내)으로 요약하라.
- 원문에 적힌 사유만 쓴다. 추측·일반론 금지. 사유가 없으면 빈 문자열.
- 무엇이 얼마나 좋아/나빠졌는지(사업·제품·지역·수요 요인), 일회성 요인(특별손익·환율·세금)이 있으면 그것도.
- 배당·자사주 변경이 함께 있으면 그 이유도 짧게.
- 일본어 고유명사는 한글로, 일본어 문자는 쓰지 않는다.
출력 JSON만: {{"why": "..."}}

원문:
{src}"""
WHY_KIND = {"res": "결산 실적", "rev": "실적 전망 수정", "div": "배당 전망 수정", "bb": "자사주 취득", "tob": "공개매수", "evt": "정책·구조 변화"}
def why_line(st, row, kind, src):
    if not src: return ""
    out = gemini_json(WHY_PROMPT.format(kind=WHY_KIND.get(kind, kind), title=row["title"], src=src[:3000]), timeout=60, models=MON_MODELS)
    w = str((out or {}).get("why") or "").strip()
    if w and not re.search(r"[\u3040-\u30ff]", w): return w
    sents = re.findall(r"[^。]{15,200}。", src.split(" [今後の見通し] ")[0])  # AI 실패: 사유가 담긴 첫 문장 번역
    pick = [z for z in sents if re.search(r"増収|減収|増益|減益|上回|下回|増加|減少|伸長|好調|低調|不振|寄与|影響|ため|により", z) and "経済" not in z]
    z = (pick or sents or [""])[0]
    return (translate(st, [z]).get(z) or "") if z else ""

FACTS_PROMPT = """너는 일본 주식 애널리스트다. 아래는 도쿄증시 적시공시 원문(일본어, 앞부분)이다. 제목: {title}
투자자가 메시지만 보고 내용을 알 수 있게 핵심을 뽑아라.
- facts: 핵심 사실 1~3줄(각 70자 이내). 숫자를 반드시 넣는다: 금액(억 엔 단위로 환산), 비율, 주식 수, 일정(YYYY.MM.DD), 대상 회사.
  · 배당: 중간·기말·연간 주당배당을 "이전 → 수정(전기 실적)" 형식으로, 증배·감배·특별배당 여부.
  · 특별손익·비용 계상: 금액, 반영 기간, 실적 전망 반영 여부(원문에 있으면).
  · 설비투자·자산 취득·매각: 금액, 대상, 일정, 목적 사업.
  · M&A·경영통합·완전자회사화·주식교환: 상대방, 방식, 통합(교환) 비율, 취득 지분율·가격, 일정.
  · 대표이사 교체: 누가 누구로, 날짜, 새 대표의 이전 직책.
  · 주식분할·병합: 비율, 기준일·효력일.
  · 그 밖: 가장 중요한 숫자와 일정.
- why: 원문에 적힌 이유·목적을 1문장(80자 이내). 원문에 없으면 빈 문자열.
- 원문에 없는 내용·추측 금지. 일본어 문자는 쓰지 말고 고유명사는 한글로. 숫자가 전혀 없는 공시면 facts에 내용 요약 1줄.
출력 JSON만: {{"facts": ["..."], "why": "..."}}

원문:
{src}"""
def facts_src(row):
    t = re.sub(r"[ \t\u3000]+", "", pdf_text(row["pdf"])); t = re.sub(r"\n+", "\n", t)
    k = t.find("記")
    return (t[:600] + "\n" + t[k:k + 2900]) if k > 600 else t[:3500]

BATCH_PROMPT = """너는 일본 주식 애널리스트다. 아래는 도쿄증시 적시공시 여러 건의 원문(일본어, 일부)이다. 항목마다 mode에 따라 정리하라.

mode "why" (수치는 이미 메시지에 있음 → 이유만):
- why: 투자자가 "왜 이렇게 됐나"를 바로 알 수 있게 1~2문장(합계 120자 이내). 무엇이 얼마나 좋아/나빠졌는지(사업·제품·지역·수요 요인),
  일회성 요인(특별손익·환율·세금)이 있으면 그것도. 배당·자사주 변경이 함께 있으면 그 이유도 짧게. facts는 빈 배열.

mode "facts" (메시지에 수치가 없음 → 핵심 사실 + 이유):
- facts: 핵심 사실 1~3줄(각 70자 이내). 숫자를 반드시 넣는다: 금액(억 엔 단위로 환산), 비율, 주식 수, 일정(YYYY.MM.DD), 대상 회사.
  · 배당: 중간·기말·연간 주당배당을 "이전 → 수정(전기 실적)" 형식으로, 증배·감배·특별배당 여부.
  · 특별손익·비용 계상: 금액, 반영 기간, 실적 전망 반영 여부(원문에 있으면).
  · 설비투자·자산 취득·매각: 금액, 대상, 일정, 목적 사업.
  · M&A·경영통합·완전자회사화·주식교환: 상대방, 방식, 통합(교환) 비율, 취득 지분율·가격, 일정.
  · 대표이사 교체: 누가 누구로, 날짜, 새 대표의 이전 직책.
  · 주식분할·병합: 비율, 기준일·효력일.
  · 그 밖: 가장 중요한 숫자와 일정. 숫자가 전혀 없는 공시면 내용 요약 1줄.
- why: 원문에 적힌 이유·목적 1문장(80자 이내).

공통: 원문에 적힌 내용만 쓴다(추측·일반론 금지). 원문에 이유가 없으면 why는 빈 문자열. 일본어 문자는 쓰지 말고 고유명사는 한글로.
다른 항목의 내용을 섞지 마라. 모든 항목의 id를 빠짐없이 출력.
출력 JSON만: {{"items": [{{"id": "a1", "facts": ["..."], "why": "..."}}]}}

항목:
{items}"""
AI_MARK = "\x00AI:{}\x00"
AI_BATCH = 8
def _nojp(x): return not re.search(r"[\u3040-\u30ff]", x or "")
def _fallback_why(st, src):
    """AI 실패: 원문에서 사유가 담긴 첫 문장을 무료 번역"""
    sents = re.findall(r"[^。]{15,200}。", (src or "").split(" [今後の見通し] ")[0])
    pick = [z for z in sents if re.search(r"増収|減収|増益|減益|上回|下回|増加|減少|伸長|好調|低調|不振|寄与|影響|ため|により", z) and "経済" not in z]
    z = (pick or sents or [""])[0]
    return (translate(st, [z]).get(z) or "") if z else ""
def ai_batch(st, tasks):
    """사유·핵심 수치 AI를 최대 AI_BATCH건씩 묶어 호출. 결과는 공시 id별로 저장해 재처리·19시 요약에서 재사용 → {id: {"facts", "why"}}"""
    cache = st.setdefault("aic", {})
    out = {t["id"]: cache[t["id"]] for t in tasks if t["id"] in cache}
    todo = [t for t in tasks if t["id"] not in cache]
    for i0 in range(0, len(todo), AI_BATCH):
        part = todo[i0:i0 + AI_BATCH]
        lines = [json.dumps({"id": f"a{k + 1}", "mode": t["mode"], "kind": t["kind"], "title": t["title"], "src": t["src"]}, ensure_ascii=False)
                 for k, t in enumerate(part)]
        res = gemini_json(BATCH_PROMPT.format(items="\n".join(lines)), timeout=150, models=MON_MODELS) or {}
        by = {str(x.get("id")): x for x in (res.get("items") or []) if isinstance(x, dict)}
        log("ai batch", len(part), "ok" if by else "fail")
        for k, t in enumerate(part):
            x = by.get(f"a{k + 1}")
            if x:
                facts = [str(f).strip().lstrip("•·- ") for f in (x.get("facts") or []) if str(f).strip() and _nojp(str(f))][:3] if t["mode"] == "facts" else []
                why = str(x.get("why") or "").strip()
                r = {"facts": facts, "why": why if _nojp(why) else "", "ts": time.time()}
            else:  # AI 실패: 사유만 번역으로(저장하지 않아 다음에 다시 시도하지는 않음 — 게시는 한 번뿐)
                r = {"facts": [], "why": _fallback_why(st, t["src"]) if t["mode"] == "why" else "", "ts": time.time(), "fb": True}
            out[t["id"]] = r
            if not r.get("fb"): cache[t["id"]] = r
    if len(cache) > 800:
        for k in sorted(cache, key=lambda k: cache[k].get("ts", 0))[:300]: cache.pop(k, None)
    for x in st.get("hl", []):  # 19시 요약에서 같은 사유를 다시 만들지 않도록
        r = out.get(x["id"])
        if r and r.get("why") and not r.get("fb"): x["why0"] = r["why"]
    return out
def ai_block(r):
    if not r: return ""
    s = ""
    if r.get("facts"): s += "\n\n<b>[핵심]</b>\n" + "\n".join(esc(x) for x in r["facts"])
    if r.get("why"): s += ("\n" if r.get("facts") else "\n\n") + f"<b>사유</b> : {esc(r['why'])}"
    return s
def ai_fill(msg, res):
    return re.sub(r"\x00AI:(.*?)\x00", lambda m: ai_block(res.get(m.group(1))), msg)

def build(row, st, names, quotes, trmap, tasks=None):
    code = row["code"]; n = names.get(code)
    name_ko = n["ko"] if n else jp2ko(row["name"])
    mk = n["mkt"] if n else PLACE.get(row.get("place", ""), row.get("place", ""))
    mc = mcap_txt(quotes.get(code))
    d = row["d"]
    head = (["⭐ 관심 종목"] if code in st.get("watch", {}) else []) + [f"{d[:4]}.{d[4:6]}.{d[6:]} {row['t']}",
            f"기업명: {name_ko}" + (f"(시가총액: {mc})" if mc else "") + f" {code}" + (f" [{mk}]" if mk else ""),
            f"보고서명: {trmap.get(row['title']) or row['title']}"]
    body = None
    title = row["title"]
    HL.clear()
    row["_name"] = name_ko
    try:
        if row.get("xbrl"):
            if "決算短信" in title and "訂正" not in title: body = body_results(row, st)
            elif "配当予想" in title and "業績予想" not in title: body = body_dividend(row)
            elif "予想" in title or "修正" in title: body = body_revision(row) or body_dividend(row)
        elif re.search(r"自己株式(の)?取得に係る事項の決定|自己株式取得に係る事項の決定|自己株式の取得及び", title):
            body = body_buyback(row, quotes.get(code))
        elif "ToSTNeT" in title and re.search(r"買付", title) and not re.search(r"結果|終了", title):
            body = body_tostnet(row)
        elif re.search(r"公開買付けの開始|公開買付けに関する意見表明|MBO", title) and not re.match(r"[(（](訂正|変更|経過開示)", title):
            body = body_tob(row)
        elif re.search(MON_RE, title) and "訂正" not in title:
            body = body_monthly(row, st)
        elif re.search(r"業績予想|業績見通し|予想(値)?の修正|業績予想の修正", title) and not re.search(r"訂正|配当予想(の修正)?に関する", title):
            body = body_forecast_pdf(row, st)
        if "消却" in title and not re.search(r"完了|結果|訂正|方針", title.replace("消却方針の変更", "")):
            c = body_cancel(row, quotes.get(code))
            if c: body = f"{body}\n\n{c}" if body else c
    except Exception as e:
        log("body error", code, title[:30], e)
    biz = why = ""
    try: biz = biz_line(st, code)
    except Exception as e: log("biz error", code, e)
    kind = HL.get("kind") or (None if body else next(("evt" for pat, _ in EVT_RULES if re.search(pat, title)), None))
    task = None  # AI 작업(사유·핵심 수치): 같은 배치의 공시를 모아 한 번에 호출(ai_batch)
    if body and kind in WHY_KIND and not re.match(r"[(（](訂正|変更)", title):
        try:
            HL["src"] = reason_src(row, kind)
            if HL["src"]: task = {"id": row["id"], "mode": "why", "kind": WHY_KIND[kind], "title": title, "src": HL["src"][:2500]}
        except Exception as e:
            log("why error", code, e)
    elif not body and not re.match(r"[(（]訂正", title):  # 수치 파서가 없는 공시: AI가 원문에서 핵심 수치·사유를 뽑음
        try:
            if kind == "evt": HL["src"] = reason_src(row, "evt")
            task = {"id": row["id"], "mode": "facts", "kind": "", "title": title, "src": facts_src(row)}
        except Exception as e:
            log("facts error", code, e)
    try:
        record_hl(row, st, name_ko, quotes.get(code), trmap.get(title) or title)
    except Exception as e:
        log("highlight error", code, e)
    if biz: head.insert(len(head) - 1, f"사업: {esc(biz)}")
    msg = "\n".join(head)
    if body: msg += "\n\n" + body
    if task:
        if tasks is None:  # 단건 호출(테스트 등): 바로 AI
            res = ai_batch(st, [task])
            msg += ai_block(res.get(task["id"]))
        else:
            tasks.append(task); msg += AI_MARK.format(task["id"])
    msg += f"\n\n공시링크: {row['pdf']}"
    if "決算短信" in title and not is_mat(nfkc(title)):  # 같은 날 올라온 결산 설명·보충 자료·질의응답 링크(따로 보내지 않음)
        mats = [[mat_label(nfkc(x["title"])), x["pdf"]] for x in st.get("queue", []) if x["code"] == code and x["d"] == d and is_mat(nfkc(x["title"]))]
        ml = (st.get("matlinks") or {}).get(code)
        if isinstance(ml, dict) and ml.get("d") == d: mats += ml["items"]
        seen = set()
        for lab, u in mats:
            if u in seen: continue
            seen.add(u); msg += f"\n{lab}: {u}"
    msg += f"\n회사정보: https://kabutan.jp/stock/?code={code}"
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
SECTIONS = [("watch", "⭐ 관심 종목 공시"), ("beat", "① 컨센서스 상회 실적"), ("up", "② 실적 가이던스 상향"), ("prog", "③ 상향 후보 (진척률이 과거 평균보다 빠름)"),
            ("newfc", "④ 결산 + 새 회계연도 가이던스 (고성장·컨센 상회)"), ("grow", "⑤ 고성장 실적 (컨센서스 없음, 시총 300억엔↑)"),
            ("hold", "⑥ 대량보유 (행동주의·주요 투자자·신규 5%)"), ("evt", "⑦ 정책·구조 변화 (중기계획·PBR 대응·배당정책·완전자회사화 등)"),
            ("ret", "⑧ 증배 · 자사주 · 공개매수"), ("mon", "⑨ 월차 매출"), ("miss", "⑩ 컨센서스 하회 실적"), ("down", "⑪ 실적 가이던스 하향")]

MON_RE = r"月次|既存店|売上高?速報|営業速報|営業概況|月間売上|売上高前年|月度売上|月次報告|売上高?推移"
EVT_RULES = [(r"中期経営計画|中期経営方針|長期ビジョン", "중기 경영계획"),
             (r"資本コストや株価を意識|資本コスト.{0,12}株価", "PBR 개선 계획"),
             (r"配当方針|株主還元方針|累進配当|DOE|資本政策の基本方針", "배당·환원 정책"),
             (r"完全子会社化|株式交換|株式売渡請求|株式併合", "완전자회사화·스퀴즈아웃"),
             (r"経営統合|合併契約|資本業務提携", "경영통합·제휴"),
             (r"株式分割", "주식분할")]
def _p(x): return f"{x:+.0f}%"
def per_txt(q, eps, lab="가이던스"):
    px = (q or {}).get("regularMarketPrice")
    if not px or not eps or eps <= 0: return ""
    return f" · {lab} PER {px / eps:.1f}배"
def record_hl(row, st, name_ko, q, title_ko):
    """방금 처리한 공시가 하이라이트 기준에 맞으면 st['hl']에 저장. 항목: (섹션, 문구, 점수)"""
    h = dict(HL); items = []
    t0 = row["title"]
    if h.get("kind") == "mon":  # 월차 수치를 뽑은 경우: 제목 + 전년비 요약
        items.append(("mon", title_ko, 0))
    elif not h.get("kind") and "訂正" not in t0:
        if re.search(MON_RE, t0):
            h["kind"] = "mon"; items.append(("mon", title_ko, 0))
        else:
            lab = next((l for pat, l in EVT_RULES if re.search(pat, t0)), None)
            if lab: h["kind"] = "evt"; items.append(("evt", f"[{lab}] {title_ko}", 0))
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
        items.append(("ret", f"공개매수 : 주당 {h['price']}엔" + (f"(프리미엄 {h['prem']}%)" if h.get("prem") else "") + f" — {title_ko[:40]}", 0))
    if row["code"] in st.get("watch", {}) and not any(s0 == "watch" for s0, _, _ in items):
        items.insert(0, ("watch", title_ko, 0))
    if not items: return
    src = ""
    try: src = h["mon"] if h.get("mon") else (h.get("src") if "src" in h else reason_src(row, h.get("kind")))
    except Exception as e: log("reason src error", row["code"], e)
    hl = st.setdefault("hl", [])
    have = {(x["id"], x["sec"]) for x in hl}
    mc = (q or {}).get("marketCap") or 0
    for sec, txt, sc in items:
        if (row["id"], sec) in have: continue
        hl.append({"id": row["id"], "sec": sec, "code": row["code"], "name": name_ko, "mc": mc, "mct": mcap_txt(q),
                   "txt": txt, "pdf": row["pdf"], "ts": time.time(), "sent": False, "src": src, "score": sc or 0,
                   "noai": sec == "watch" and not src, "mon": h.get("mon") if sec == "mon" else None})

# 공시 PDF에서 '이유'가 적힌 부분(일본어 원문)을 잘라 둠 → 19시 요약 때 AI가 한 줄로 정리
REASON_HEAD = {
    "res": ["経営成績に関する説明", "経営成績等の概況", "経営成績の概況", "当四半期決算に関する定性的情報", "経営成績の状況", "業績の状況"],
    "rev": ["修正の理由", "修正理由", "差異の理由", "理由"],
    "div": ["修正の理由", "理由"],
    "bb": ["取得を行う理由", "取得する理由", "理由"],
    "tob": ["公開買付けの目的", "買付け等の目的", "目的", "理由"],
    "evt": ["目的", "理由", "背景", "趣旨", "概要"],
    "mon": [],
}
def reason_src(row, kind):
    heads = REASON_HEAD.get(kind)
    if heads is None: return ""
    from pdfminer.high_level import extract_text
    t = nfkc(extract_text(io.BytesIO(http(row["pdf"], timeout=40)), maxpages=6 if kind == "res" else 3))
    t = re.sub(r"[ \t\u3000]+", "", t); t = re.sub(r"\n+", "\n", t)
    if kind == "mon": return re.sub(r"\n", " ", t)[:2000]  # 월차: 표 전체(앞부분)를 AI가 읽음
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
    if not out and kind == "evt": out = t.replace("\n", " ")[:1200]
    if kind == "res":  # 결산이면 '향후 전망'(새 가이던스 근거)도 덧붙임
        for m in re.finditer(r"今後の見通し", t):
            seg = t[m.end():m.end() + 800]
            if "」" in seg[:25] or re.match(r"[.…‥\s\d]{0,6}[.…‥]{3}", seg): continue
            out += " [今後の見通し] " + seg.replace("\n", ""); break
    return out

GEMINI_MODELS = [m.strip() for m in os.environ.get("GEMINI_MODELS", "gemini-3.8-flash,gemini-3.7-flash,gemini-3.5-flash,gemini-3.1-flash-lite").split(",") if m.strip()]
MON_MODELS = [m.strip() for m in os.environ.get("MON_MODELS", "gemini-3.1-flash-lite,gemini-3.5-flash,gemini-3.7-flash").split(",") if m.strip()]
def gemini_json(prompt, timeout=150, models=None):
    key = os.environ.get("GEMINI_API_KEY")
    if not key: return None
    for model in models or GEMINI_MODELS:
        t0 = time.time()
        try:
            body = json.dumps({"contents": [{"parts": [{"text": prompt}]}],
                               "generationConfig": {"temperature": 0.2, "maxOutputTokens": 8000, "responseMimeType": "application/json"}}).encode()
            r = json.loads(http(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}",
                                timeout=timeout, data=body, headers={"content-type": "application/json"}))
            txt = "".join(p.get("text", "") for p in r["candidates"][0]["content"]["parts"])
            out = json.loads(re.sub(r"^```(json)?|```$", "", txt.strip()))
            if models: log("gemini ok", model, f"{time.time() - t0:.0f}s")
            return out
        except Exception as e:
            log("gemini error", model, str(e)[:150])
            if "429" in str(e): time.sleep(3)
    return None

AI_PROMPT = """너는 일본 주식 애널리스트다. 아래는 오늘 도쿄증시 적시공시 중 하이라이트로 뽑힌 항목들이다.
각 항목에는 [분류], 기업, 핵심 수치, 그리고 공시 원문(일본어)에서 이유·설명 부분을 잘라낸 글이 있다.

할 일
1. reasons: 항목마다 "왜 이런 결과/결정이 나왔는지"를 한국어 한 문장(60자 이내)으로. 원문에 적힌 사유만 쓰고, 원문에 사유가 없으면 "공시에 사유 기재 없음". 추측 금지. 일본어 고유명사는 한글로.
   - [⑨ 월차 매출] 항목은 사유 대신 원문 표에서 가장 최근 월의 "기존점 매출 전년비"와 "전체 매출 전년비"(있으면 객수·객단가)를 "9월 기존점 +3.2%·전체 +5.1%(객수 +1.0%)" 형식으로. 102.3%처럼 전년 대비 비율이면 +2.3%로 환산. 숫자가 없으면 "수치 확인 불가".
   - [⑦ 정책·구조 변화] 항목은 핵심 내용을 한 문장으로(목표 ROE·DOE·배당성향·교환비율·인수 가격 등 수치가 있으면 포함).
2. top: 투자자 관점에서 오늘 가장 중요한 공시 최대 3개를 골라, 왜 중요한지 한국어 한 문장(80자 이내)으로. 시가총액, 서프라이즈·수정 폭, 사유의 지속성(일회성인지 구조적인지)을 고려.

출력은 JSON만: {{"reasons": {{"번호": "사유"}}, "top": [{{"i": 번호, "why": "이유"}}]}}

항목:
{items}
"""
def ai_reasons(items):
    """{item 위치: 사유}, [(위치, 중요한 이유)]"""
    reasons, top = {}, []
    allx = [(i, x) for i, x in enumerate(items) if not x.get("noai")]
    for i0 in range(0, len(allx), 25):
        part = allx[i0:i0 + 25]
        lines = []
        for _, (j, x) in enumerate(part):
            body = f"사유(확정, 그대로 사용): {x['why0']}" if x.get("why0") else f"원문: {x.get('src') or '(없음)'}"
            lines.append(f"#{j + 1} [{dict(SECTIONS)[x['sec']]}] {x['name']} {x['code']} (시총 {x['mct'] or '?'}) : {x['txt']}\n{body}")
        out = gemini_json(AI_PROMPT.format(items="\n\n".join(lines)))
        if not out: break
        for k, v in (out.get("reasons") or {}).items():
            try: reasons[int(str(k).strip("#")) - 1] = str(v).strip()
            except ValueError: pass
        for t in out.get("top") or []:
            try: top.append((int(str(t["i"]).strip("#")) - 1, str(t["why"]).strip()))
            except Exception: pass
    if len(allx) > 25: top = top[:3]
    for i, x in allx:  # 메시지 때 만든 사유는 그대로
        if x.get("why0") and x["sec"] != "mon": reasons[i] = x["why0"]
    return reasons, top

def fallback_reasons(st, items):
    """AI 키가 없을 때: 원문 사유의 첫 문장을 번역"""
    srcs = {}
    for i, x in enumerate(items):
        if x.get("noai") or x["sec"] == "mon": continue
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
        def mon_key(x):
            s = (x.get("mon") or reasons.get(idx[id(x)]) or "").split("|")[0]
            m = re.search(r"기존점\s*([+-]?\d+(?:\.\d+)?)%", s) or re.search(r"전체[^+\-\d]*([+-]?\d+(?:\.\d+)?)%", s)
            return float(m.group(1)) if m else -999
        key = (lambda x: (-mon_key(x), -x["mc"])) if sec == "mon" else (lambda x: (-(x.get("score") or 0), -x["mc"]))
        for x in sorted(xs, key=key):
            if (x["code"], x["txt"]) in seen: continue
            seen.add((x["code"], x["txt"])); uniq.append(x)
        if not uniq: continue
        out += ["", f"<b>{lab} ({len(uniq)}건)</b>"]
        for x in uniq[:20]:
            out.append(f"• <a href=\"{x['pdf']}\">{esc(x['name'])}</a>" + (f"({x['mct']})" if x["mct"] else "") + f" {x['code']}\n   {x['txt']}")
            rs = (x.get("mon") if sec == "mon" else None) or reasons.get(idx[id(x)])
            if rs and sec != "watch": out.append(f"   └ {'수치' if sec == 'mon' else '내용' if sec == 'evt' else '이유'}: {esc(rs)}")
        if len(uniq) > 20: out.append(f"   …외 {len(uniq) - 20}건")
    out += ["", f"<i>기준: 컨센 상회 = 분기 EPS +{BEAT_EPS}%↑ 또는 매출 +{BEAT_REV}%↑ / 하회 = EPS {MISS_EPS}%↓(또는 적자) 또는 매출 {MISS_REV}%↓ (Yahoo Finance 애널리스트 평균, 발표 전 저장) · 가이던스 상·하향 = 회사 실적예상 수정 공시의 영업익(없으면 경상익) 기준 · 상향 후보 = 누적 영업익 진척률이 과거 같은 분기 평균보다 +{PROG_GAP}%p↑ (가부탄 분기표) · PER = 직전 주가 ÷ 회사 가이던스 EPS · 대량보유 = EDINET 5% 보고 중 행동주의·주요 투자자 및 대형 운용사·증권사 재고를 뺀 신규 5%·+2%p 이상 · 이유 = 공시 원문의 설명 부분을 "
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
    qs = yf_quotes([x["code"] for x in items] + [BENCH])
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
    try: perf_add(st, rows, qs, today)
    except Exception: traceback.print_exc()
    log("followup sent", len(rows))

# ───────────────────────── 실적 발표 전날 예고 ─────────────────────────
PREVIEW_HM = (20, 30)   # 발표 전날 예고 발송 시각(JST)
def next_bday(d):
    d += dt.timedelta(days=1)
    while d.weekday() >= 5: d += dt.timedelta(days=1)
    return d
def preview_target():
    t = now()
    if t.weekday() in (4, 5): return None  # 금·토는 쉬고 일요일에 월요일분 예고
    return next_bday(t.date()).isoformat()
def preview_work(st, limit=15):
    """다음 영업일 발표 예정 종목의 가부탄 진척률을 조금씩 미리 계산(12시 이후, 대기열이 빌 때)"""
    tgt = preview_target()
    if not tgt or now().hour < 12: return
    pv = st.setdefault("pv", {})
    if pv.get("tgt") != tgt: pv.clear(); pv.update(tgt=tgt, data={}, sent=False)
    if pv["sent"]: return
    codes = [c for c, (d, _) in (st.get("sched") or {}).items() if d == tgt]
    for c in [c for c in codes if c not in pv["data"]][:limit]:
        rec = {}
        try:
            h = kabutan_html(c); kq = kabutan_quarters(c, h); fc = kabutan_fc(h)
            if fc and fc.get("op"):
                end = fc["end"]; start = dt.date(end.year - (0 if end.month == 12 else 1), end.month % 12 + 1, 1)
                qs = [k for k in kq if k[0] >= start and k[1] <= end]
                rec = {"fc_op": fc["op"], "fc_eps": fc.get("eps")}
                if 1 <= len(qs) <= 3 and all(k[3] is not None for k in qs) and fc["op"] > 0:
                    hp = hist_progress(kq, start, len(qs), 3)
                    rec.update(q=len(qs), prog=sum(k[3] for k in qs) / fc["op"] * 100,
                               hist=sum(hp) / len(hp) if hp else None, n=len(hp),
                               yoy=chg_txt(qs[-1][3], next((k[3] for k in kq if k[0] == dt.date(qs[-1][0].year - 1, qs[-1][0].month, 1)), None)))
        except Exception as e:
            log("preview error", c, e)
        pv["data"][c] = rec
        time.sleep(0.3)

def maybe_preview(st, names):
    pv = st.get("pv") or {}
    tgt = preview_target(); t = now()
    if not tgt or pv.get("tgt") != tgt or pv.get("sent") or (t.hour, t.minute) < PREVIEW_HM: return
    codes = [c for c, (d, _) in (st.get("sched") or {}).items() if d == tgt]
    if any(c not in pv["data"] for c in codes) and t.hour < 22: return  # 계산이 덜 끝났으면 22시까지 기다림
    pv["sent"] = True
    if not codes: return
    qs = yf_quotes(codes); data = pv["data"]; cons = st.get("cons", {})
    def nm(c):
        n = names.get(c) or {}; q = qs.get(c) or {}
        return f"<b>{esc(n.get('ko') or c)}</b>" + (f"({mcap_txt(q)})" if mcap_txt(q) else "") + f" {c}"
    def pe(c):
        q = qs.get(c) or {}; e = (data.get(c) or {}).get("fc_eps"); px = q.get("regularMarketPrice")
        return f" · 예상 PER {px / e:.1f}배" if px and e and e > 0 else ""
    d = dt.date.fromisoformat(tgt); wd = "월화수목금토일"[d.weekday()]
    out = [f"🗓 <b>{d.month}/{d.day}({wd}) 실적 발표 예정 {len(codes)}개사 미리보기</b>",
           "<i>JPX 발표 예정일 기준 · 진척률 = 직전 분기까지 누적 영업익 ÷ 회사 연간 예상(가부탄) · 컨센서스 = Yahoo Finance</i>"]
    w = [c for c in codes if c in st.get("watch", {})]
    if w:
        out += ["", f"<b>⭐ 관심 종목 ({len(w)}개)</b>"]
        for c in w:
            r = data.get(c) or {}; k = cons.get(c) or {}; parts = []
            if r.get("prog") is not None: parts.append(f"{r['q']}Q 누적 진척률 {r['prog']:.0f}%" + (f"(과거 평균 {r['hist']:.0f}%)" if r.get("hist") is not None else ""))
            if k.get("eps") is not None: parts.append(f"분기 EPS 예상 {k['eps']:,.1f}엔(애널 {k.get('eps_n', 0)}명)")
            out.append(f"• {nm(c)}" + ("\n   " + " · ".join(parts) if parts else "") + pe(c))
    ahead = []
    for c in codes:
        r = data.get(c) or {}
        if r.get("prog") is not None and r.get("hist") is not None and 5 <= r["hist"] <= 100 and r["prog"] - r["hist"] >= PROG_GAP:
            ahead.append((r["prog"] - r["hist"], c, r))
    ahead.sort(reverse=True)
    if ahead:
        out += ["", f"<b>🔎 진척률이 과거보다 빠른 종목 ({len(ahead)}개) — 발표 때 상향·서프라이즈 후보</b>"]
        for g, c, r in ahead[:20]:
            out.append(f"• {nm(c)}\n   {r['q']}Q 누적 진척률 {r['prog']:.0f}% (과거 {r['n']}년 평균 {r['hist']:.0f}%, +{g:.0f}%p)"
                       + (f" · 직전 분기 영업익 YoY {r['yoy']}" if r.get("yoy") else "") + pe(c))
        if len(ahead) > 20: out.append(f"   …외 {len(ahead) - 20}개")
    cs = [c for c in codes if (cons.get(c) or {}).get("eps") or (cons.get(c) or {}).get("rev")]
    cs.sort(key=lambda c: -((qs.get(c) or {}).get("marketCap") or 0))
    if cs:
        out += ["", f"<b>📋 컨센서스 있는 종목 ({len(cs)}개, 시총순)</b>"]
        for c in cs[:20]:
            k = cons[c]; r = data.get(c) or {}; parts = []
            if k.get("eps") is not None: parts.append(f"분기 EPS 예상 {k['eps']:,.1f}엔(애널 {k.get('eps_n', 0)}명)")
            if k.get("rev"): parts.append(f"매출 예상 {oku(k['rev'])}엔")
            if r.get("prog") is not None: parts.append(f"진척률 {r['prog']:.0f}%")
            out.append(f"• {nm(c)}\n   " + " · ".join(parts) + pe(c))
        if len(cs) > 20: out.append(f"   …외 {len(cs) - 20}개")
    if not ahead and not cs: out += ["", "눈에 띄는 종목이 없습니다."]
    for part in split_msg("\n".join(out)):
        tg_send(part); time.sleep(SEND_GAP)
    log("preview sent", tgt, len(codes))

# ───────────────────────── 대량보유(5%) 알림 ─────────────────────────
# holdings 워크플로가 30분마다 EDINET에서 모아 data 브랜치에 저장한 holdings.json을 읽음
HOLD_URL = f"https://raw.githubusercontent.com/{os.environ.get('GITHUB_REPOSITORY', 'ssamgij-bit/jpall')}/data/holdings.json"
KNOWN = re.compile(r"オアシス|Oasis|ダルトン|Dalton|ニッポン・?アクティブ|Nippon Active|NAVF|ストラテジック|Strategic Capital|シティインデックス|エスグラント|南青山不動産|村上|野村絢|レノ|エフィッシモ|Effissimo|エリオット|Elliott|バリューアクト|ValueAct|パリサー|Palliser|3D Investment|スリーディー|ひびき|Hibiki|カナメ|Kaname|シルチェスター|Silchester|光通信|インダス|Indus|タイヨウ|Taiyo|アセット・?バリュー|Asset Value|ファラロン|Farallon|ケイマン|Cayman|フランクリン|Franklin|サード・?ポイント|Third Point|キング・?ストリート|King Street|アルジェ|Argyle|イチゴ|Ichigo|シンプレクス|Simplex", re.I)
BIG = re.compile(r"ブラックロック|BlackRock|野村アセット|Nomura Asset|日興アセット|アモーヴァ|三井住友トラスト|三井住友DS|三菱UFJ|大和アセット|アセットマネジメントOne|みずほ|Vanguard|バンガード|ステート・?ストリート|State Street|ノルウェー|Norges|モルガン|Morgan|ゴールドマン|Goldman|JPモルガン|J\.?P\.? ?Morgan|UBS|シティグループ|Citigroup|バークレイズ|Barclays|BNP|ソシエテ|Soci[ée]t[ée]|野村證券|野村証券|SBI|楽天|大和証券|SMBC|日本証券金融|マネックス|Fidelity|フィデリティ|キャピタル・リサーチ|Capital Research|ウエリントン|Wellington|ノムラ|インベスコ|Invesco|アバディーン|abrdn|ニッセイ|りそな|東京海上", re.I)
def hold_work(st, names):
    if time.time() - st.get("hold_ts", 0) < 600: return
    st["hold_ts"] = time.time()
    recs = json.loads(http(HOLD_URL, timeout=30)).get("recent") or []
    if "hold_seen" not in st:
        st["hold_seen"] = [r["id"] for r in recs]; log("holdings: 기존 보고 기록만 함", len(recs)); return
    seen = set(st["hold_seen"]); new = [r for r in reversed(recs) if r["id"] not in seen]
    if not new: return
    picks = []
    for r in new:
        st["hold_seen"].append(r["id"])
        if r.get("kind") == "fix" or not r.get("code"): continue
        p = r.get("purpose") or ""; who = f"{r.get('holder', '')} {r.get('holder_ko', '')}"
        act = "행동주의" in p; known = bool(KNOWN.search(who)) and not BIG.search(who)
        fund = ("순투자" in p or "운용" in p) and "증권사 재고" not in p and not BIG.search(who)
        moved = r.get("kind") == "new" or abs(r.get("delta") or 0) >= 0.5
        # 행동주의 표시는 지분 20% 미만(모회사·전략적 보유 제외)이거나 알려진 투자자일 때만
        if (known or (act and (r.get("cur") or 0) < 20)) and moved: picks.append((r, True))
        elif fund and (r.get("kind") == "new" or (r.get("delta") or 0) >= 2): picks.append((r, False))
    st["hold_seen"] = st["hold_seen"][-3000:]
    if not picks: return
    qs = yf_quotes([r["code"] for r, _ in picks]); hl = st.setdefault("hl", [])
    for r, alert in picks:
        q = qs.get(r["code"]); n = names.get(r["code"]) or {}
        nm = n.get("ko") or r.get("issuer_ko") or r["code"]
        ch = "신규 5%" if r.get("kind") == "new" else (f"{r['delta']:+.2f}%p" if r.get("delta") is not None else "변경")
        ratio = f"{r['prev']:.2f}% → {r['cur']:.2f}%" if r.get("prev") is not None and r.get("cur") is not None else (f"{r['cur']:.2f}%" if r.get("cur") is not None else "—")
        tag = "🦈 " if alert else ""
        hl.append({"id": r["id"], "sec": "hold", "code": r["code"], "name": nm, "mc": (q or {}).get("marketCap") or 0, "mct": mcap_txt(q),
                   "txt": f"{tag}{esc(r.get('holder_ko') or '')} {ratio} ({ch}) · 목적: {r.get('purpose') or '—'}", "pdf": r["url"], "ts": time.time(),
                   "sent": False, "src": "", "score": 100 if alert else (r.get("delta") or 5), "noai": True})
        if alert:
            msg = (f"🦈 <b>대량보유 보고 · {'행동주의(경영 제안 목적)' if '행동주의' in (r.get('purpose') or '') else '주요 투자자'}</b>\n"
                   f"{r['d']}\n기업명: {esc(nm)}" + (f"(시가총액: {mcap_txt(q)})" if mcap_txt(q) else "") + f" {r['code']}" + (f" [{n['mkt']}]" if n.get("mkt") else "") +
                   f"\n보유자: {esc(r.get('holder_ko') or '')}" + (f" 외 공동보유 {r['joint'] - 1}" if (r.get("joint") or 1) > 1 else "") +
                   f"\n보유 비율: {ratio} ({ch})\n보유 목적: {r.get('purpose') or '—'}\n\n공시링크: {r['url']}\n회사정보: https://kabutan.jp/stock/?code={r['code']}")
            tg_send(msg); time.sleep(SEND_GAP)
    log("holdings picks", len(picks))

# ───────────────────────── 신호 적중률 추적 ─────────────────────────
BENCH = "1306"  # TOPIX 연동 ETF(1306)를 시장 기준으로 사용
def perf_add(st, rows, qs, today):
    """다음 거래일 반응을 보낼 때: 기준가(공시 전 종가)와 TOPIX ETF 기준가를 기록"""
    b = qs.get(BENCH) or {}
    perf = st.setdefault("perf", []); have = {(r["code"], r["sec"], r["d0"]) for r in perf}
    for c, x in rows:
        q = qs.get(x["code"]) or {}
        if (x["code"], x["sec"], today) in have or not q.get("regularMarketPreviousClose"): continue
        perf.append({"code": x["code"], "sec": x["sec"], "d0": today, "base": q["regularMarketPreviousClose"], "p1": q.get("regularMarketPrice"),
                     "bb": b.get("regularMarketPreviousClose"), "b1": b.get("regularMarketPrice")})
def perf_update(st):
    """매 거래일 장 마감 후: 5거래일(약 7일)·20거래일(약 28일) 지난 신호의 종가를 기록"""
    t = now(); today = t.strftime("%Y%m%d")
    if t.weekday() >= 5 or (t.hour, t.minute) < (15, 45) or st.get("pu_date") == today: return
    perf = st.get("perf") or []
    def age(r): return (t.date() - dt.datetime.strptime(r["d0"], "%Y%m%d").date()).days
    need = [r for r in perf if (age(r) >= 7 and r.get("p5") is None) or (age(r) >= 28 and r.get("p20") is None)]
    st["pu_date"] = today
    if need:
        qs = yf_quotes(list({r["code"] for r in need}) + [BENCH]); b = qs.get(BENCH) or {}
        if not b.get("regularMarketTime") or dt.datetime.fromtimestamp(b["regularMarketTime"], JST).strftime("%Y%m%d") != today: return  # 휴장
        for r in need:
            px = (qs.get(r["code"]) or {}).get("regularMarketPrice")
            if not px: continue
            if age(r) >= 7 and r.get("p5") is None: r["p5"], r["b5"] = px, b.get("regularMarketPrice")
            if age(r) >= 28 and r.get("p20") is None: r["p20"], r["b20"] = px, b.get("regularMarketPrice")
    st["perf"] = [r for r in perf if age(r) <= 70]
def maybe_weekly(st):
    """토요일 10시: 섹션별 신호 성과(TOPIX 대비 초과수익·적중률) 주간 보고"""
    t = now(); today = t.strftime("%Y%m%d")
    if t.weekday() != 5 or t.hour < 10 or st.get("wk_date") == today: return
    st["wk_date"] = today
    perf = st.get("perf") or []
    def ex(r, k, bk):
        try: return ((r[k] / r["base"]) - (r[bk] / r["bb"])) * 100
        except Exception: return None
    out = [f"📐 <b>{t.month}/{t.day} 주간 신호 성과 점검</b>", "<i>하이라이트 신호의 공시 전 종가 대비 수익률 − 같은 기간 TOPIX(1306) 수익률 · 최근 10주 · 적중률 = 초과수익 > 0 비율</i>", ""]
    rows = 0
    for sec, lab in SECTIONS:
        rs = [r for r in perf if r["sec"] == sec]
        if not rs: continue
        line = [f"<b>{lab}</b>"]
        for k, bk, nm in (("p1", "b1", "다음날"), ("p5", "b5", "5일"), ("p20", "b20", "20일")):
            v = [e for e in (ex(r, k, bk) for r in rs) if e is not None]
            if v: line.append(f"   {nm}: 평균 {sum(v) / len(v):+.1f}%p · 적중 {sum(1 for e in v if e > 0) / len(v) * 100:.0f}% (n={len(v)})")
        if len(line) > 1: out += line; rows += 1
    if not rows: return
    out += ["", "<i>표본이 작을 때(n<20)는 우연일 수 있음 · 시세: Yahoo Finance</i>"]
    for part in split_msg("\n".join(out)):
        tg_send(part); time.sleep(SEND_GAP)
    log("weekly perf sent")

IDEAS_HM = (14, 0)   # 토요일 14시(KST): 지난 7일 하이라이트 공시 중 투자 아이디어 정리
IDEAS_PROMPT = """너는 일본 주식을 커버하는 한국 자산운용사 애널리스트의 리서치 보조다.
아래는 지난 7일 동안 일본 공시 채널에서 하이라이트로 뽑힌 공시 {n}건이다(번호, 분류, 기업, 시총, 핵심 수치, 이유).
이 중에서 '일본 투자 아이디어'로 괜찮아 보이는 것을 골라 한국어로 정리하라. 종목·섹터·테마 모두 가능(예: 여러 기업이 같은 이유로 상향하면 섹터 아이디어).
""" + """선정 기준: 구체적 근거(실적 서프라이즈·가이던스·밸류에이션·촉매·수급·주주환원·업황 변화)가 있고, 투자 판단에 쓸 만한 것. 같은 종목·테마는 하나로 묶고 출처 번호를 모두 표시.
규칙: 입력에 없는 사실·수치를 지어내지 말 것(촉매·리스크는 입력 근거에서 도출하고, 일반론이면 짧게). 일본 종목은 한국어 이름(4자리 코드), 미국은 이름(티커). 일본어 원문 표기 금지. 단정적 매수 권유 대신 '검토할 만함' 수준.
출력 JSON: {{"overview": "이번 주 한 줄 총평", "ideas": [{{"title": "아이디어 이름", "target": "종목명(코드) 또는 섹터/테마", "logic": "왜 괜찮아 보이는지 2~3문장, 수치 포함", "catalyst": "촉매·확인할 것 1문장", "risk": "리스크·반론 1문장", "refs": [번호]}}], "others": [{{"target": "종목명(코드)", "line": "한 줄", "refs": [번호]}}]}}
ideas는 5~10개(입력이 적으면 그보다 적게), others는 3~5개.""" + """

{items}"""
def fmt_ideas(out, title, links):
    """주간 아이디어 JSON → 텔레그램 HTML. links: {번호: (url, 라벨)}"""
    e = lambda s: html.escape(str(s or ""), quote=False)
    def refs(rs):
        rs = [int(r) for r in (rs or []) if str(r).isdigit() and int(r) in links]
        return " ".join(f'<a href="{e(links[r][0])}">[{r}]</a>' for r in rs)
    lines = [title, "", e(out.get("overview"))]
    for x in out.get("ideas") or []:
        lines += ["", f"▸ <b>{e(x.get('title'))}</b> — {e(x.get('target'))}",
                  f"• 논리: {e(x.get('logic'))}", f"• 촉매·확인: {e(x.get('catalyst'))}",
                  f"• 리스크·반론: {e(x.get('risk'))}", f"• 출처: {refs(x.get('refs'))}"]
    if out.get("others"):
        lines += ["", "<b>그 밖에 눈여겨볼 언급</b>"]
        for x in out["others"]:
            lines.append(f"• {e(x.get('target'))} — {e(x.get('line'))} {refs(x.get('refs'))}")
    lines += ["", "<i>AI(Gemini)가 지난 7일 게시물에서 고른 것 · 투자 판단 전 원문 확인 권장</i>"]
    return "\n".join(lines)

def maybe_ideas(st):
    t = now(); today = t.strftime("%Y%m%d")
    if t.weekday() != 5 or (t.hour, t.minute) < IDEAS_HM or st.get("ideas_date") == today: return
    xs = [x for x in st.get("hl", []) if time.time() - x["ts"] < 86400 * 7 and x["sec"] != "watch"]
    if not xs:
        st["ideas_date"] = today; return
    seen, uniq = set(), []
    for x in sorted(xs, key=lambda x: (-(x.get("score") or 0), -x["mc"])):
        if (x["code"], x["txt"]) in seen: continue
        seen.add((x["code"], x["txt"])); uniq.append(x)
    uniq = uniq[:250]
    lines, links = [], {}
    for i, x in enumerate(uniq, 1):
        why = x.get("why") or x.get("mon") or ""
        lines.append(f"[{i}] [{dict(SECTIONS).get(x['sec'], x['sec'])}] {x['name']} {x['code']} (시총 {x['mct'] or '?'}) : {x['txt']}"
                     + (f" | 이유: {why}" if why else "") + (f" | 당일 핵심 선정: {x['top']}" if x.get("top") else ""))
        links[i] = (x["pdf"], x["name"])
    out = gemini_json(IDEAS_PROMPT.format(n=len(uniq), items="\n".join(lines)), timeout=240)
    if not out or not out.get("ideas"):
        log("ideas: AI 실패 → 다음 실행에 재시도"); return
    t0 = t - dt.timedelta(days=7)
    msg = fmt_ideas(out, f"💡 <b>주간 일본 투자 아이디어 — 공시편</b> ({t0.month}/{t0.day}~{t.month}/{t.day}, 하이라이트 {len(uniq)}건 중 선별)", links)
    ok = True
    for part in split_msg(msg):
        ok = tg_send(part) and ok; time.sleep(SEND_GAP)
    if ok:
        st["ideas_date"] = today; log("weekly ideas sent")

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
        for i, x in enumerate(items):
            x["sent"] = True; x["dd"] = today
            if reasons.get(i): x["why"] = reasons[i]
        for i, why in top:
            if 0 <= i < len(items): items[i]["top"] = why
        log("digest sent", len(items))
    st["digest_date"] = today
    st["hl"] = [x for x in hl if time.time() - x["ts"] < 86400 * 9]   # 주간 아이디어(토 14시)용으로 9일 보관

# ───────────────────────── 관심 종목: 봇 개인 대화 명령 ─────────────────────────
HELP = ("관심 종목 명령\n/add 7203 6758 — 추가(여러 개 가능)\n/del 7203 — 삭제\n/list — 목록\n\n"
        "등록한 종목은 공시가 나오면 채널 메시지 맨 위에 ⭐ 표시가 붙고, 19시 요약 맨 위 '⭐ 관심 종목 공시'와 발표 전날 예고에 따로 나옵니다.")
# japan stock 봇(@JP_stock_yh_bot, Cloudflare Worker)의 구독 목록을 관심 종목으로 그대로 사용
WATCH_URL = os.environ.get("WATCH_URL", "https://jp-disclosure-bot.ssamgij.workers.dev/watchlist")
WATCH_KEY = os.environ.get("WATCH_KEY", "")
def watch_sync(st):
    today = now().strftime("%Y%m%d")
    if not (WATCH_URL and WATCH_KEY) or st.get("watch_date") == today: return  # 하루 한 번만 읽음(키가 없으면 /add 목록 사용)
    st["watch_date"] = today
    j = json.loads(http(WATCH_URL, timeout=20, headers={"x-watch-key": WATCH_KEY, **UA}))
    codes = [nfkc(str(c)).upper()[:4] for c in (j.get("codes") or [])]
    old = st.get("watch", {})
    st["watch"] = {c: old.get(c, time.time()) for c in codes}
    if set(old) != set(codes): log("watchlist synced", len(codes))
def tg_reply(chat, text):
    if DRY: print(f"[reply to {chat}] {text}"); return
    try:
        http(f"https://api.telegram.org/bot{TOKEN}/sendMessage", data=json.dumps({"chat_id": chat, "text": text[:4000]}).encode(),
             headers={"content-type": "application/json"})
    except Exception as e:
        log("reply error", e)
def tg_commands(st, names):
    """봇에게 온 개인 메시지(/add /del /list)를 20초마다 확인. 처음 명령을 보낸 사람만 주인으로 인정"""
    if DRY or not TOKEN or time.time() - st.get("cmd_ts", 0) < 20: return
    st["cmd_ts"] = time.time()
    q = urllib.parse.urlencode({"offset": st.get("tg_off", 0), "timeout": 0, "allowed_updates": json.dumps(["message"])})
    res = json.loads(http(f"https://api.telegram.org/bot{TOKEN}/getUpdates?{q}", timeout=20)).get("result") or []
    for u in res:
        st["tg_off"] = u["update_id"] + 1
        m = u.get("message") or {}
        chat = (m.get("chat") or {}); text = (m.get("text") or "").strip()
        if chat.get("type") != "private" or not text.startswith("/"): continue
        if st.get("owner") is None: st["owner"] = chat["id"]; log("owner set", chat["id"])
        if chat["id"] != st["owner"]: tg_reply(chat["id"], "이 봇은 개인용입니다."); continue
        cmd, *args = text.split()
        cmd = cmd.split("@")[0].lower()
        codes = [nfkc(a).upper()[:4] for a in args if re.fullmatch(r"[0-9A-Za-z]{4}", nfkc(a))]
        watch = st.setdefault("watch", {})
        def label(c): return f"{(names.get(c) or {}).get('ko') or '?'}({c})"
        if WATCH_URL and WATCH_KEY and cmd in ("/add", "/추가", "/del", "/삭제", "/remove"):
            tg_reply(chat["id"], "관심 종목은 japan stock 봇(@JP_stock_yh_bot)의 구독 목록을 그대로 씁니다. 그쪽에서 구독/구독해지 해주세요. (하루 한 번 반영)"); continue
        if cmd in ("/add", "/추가"):
            ok = [c for c in codes if c in names]; bad = [c for c in codes if c not in names]
            for c in ok: watch[c] = time.time()
            msg = (f"추가: {', '.join(label(c) for c in ok)}" if ok else "추가할 종목 코드가 없습니다. 예) /add 7203")
            if bad: msg += f"\n상장 종목 목록에 없는 코드: {', '.join(bad)}"
            tg_reply(chat["id"], msg + f"\n(현재 {len(watch)}개)")
        elif cmd in ("/del", "/삭제", "/remove"):
            gone = [c for c in codes if watch.pop(c, None)]
            tg_reply(chat["id"], (f"삭제: {', '.join(label(c) for c in gone)}" if gone else "목록에 없는 코드입니다.") + f"\n(현재 {len(watch)}개)")
        elif cmd in ("/list", "/목록"):
            tg_reply(chat["id"], f"관심 종목 {len(watch)}개\n" + ("\n".join(label(c) for c in sorted(watch)) if watch else "(없음) /add 7203 처럼 추가하세요."))
        else:
            tg_reply(chat["id"], HELP)

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

def tg_photo(path, caption=""):
    if DRY: print(f"[chart] {path}"); return True
    try:
        import uuid
        b = uuid.uuid4().hex
        parts = [f"--{b}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n{CHANNEL}\r\n".encode()]
        if caption: parts.append(f"--{b}\r\nContent-Disposition: form-data; name=\"caption\"\r\n\r\n{caption}\r\n".encode())
        parts.append(f"--{b}\r\nContent-Disposition: form-data; name=\"photo\"; filename=\"chart.png\"\r\nContent-Type: image/png\r\n\r\n".encode()
                     + open(path, "rb").read() + f"\r\n--{b}--\r\n".encode())
        http(f"https://api.telegram.org/bot{TOKEN}/sendPhoto", data=b"".join(parts), headers={"content-type": f"multipart/form-data; boundary={b}"}, timeout=60)
        return True
    except Exception as e:
        log("photo error", e); return False

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

def mon_test(ids):
    """수동 점검용: MON_TEST=공시PDF id(쉼표 구분)의 월차 추출 결과만 출력"""
    st = {}
    for i in ids.split(","):
        i = i.strip().replace(".pdf", "")
        if not i: continue
        row = {"pdf": TDNET + i + ".pdf", "d": i[4:12], "code": "TEST", "title": ""}
        try: print("규칙 파서:", mon_parse(pdf_layout(row["pdf"]), "", row["d"]), flush=True)
        except Exception as e: print("규칙 파서 오류", e)
        try: print("=" * 60, "\n" + i, "\n" + str(body_monthly(row, st)), "\n요약:", HL.get("mon"), flush=True)
        except Exception: traceback.print_exc()
        if HL.get("chart") and os.environ.get("MON_TEST_SEND") and TOKEN and CHANNEL: tg_photo(HL["chart"], f"[월차 차트 점검] {i}")
        st.pop("monh", None); HL.clear()

def main():
    if os.environ.get("MON_TEST"): return mon_test(os.environ["MON_TEST"])
    t0 = time.time()
    st = load("state.json", {})
    names = load_names(st)
    added = collect(st)
    log("new", added, "queue", len(st.get("queue", [])))
    try: jpx_schedule(st)
    except Exception as e: log("jpx error", e)
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
        for fn in (lambda: watch_sync(st), lambda: tg_commands(st, names), lambda: maybe_followup(st), lambda: hold_work(st, names), lambda: maybe_preview(st, names),
                   lambda: perf_update(st), lambda: maybe_weekly(st), lambda: maybe_ideas(st)):
            try: fn()
            except Exception as e: log("extra error", e)
        q = st.get("queue", [])
        if not q:
            if time.time() - last_scan > 60 and time.time() - t0 < RUN_SECONDS - 30:
                collect(st); last_scan = time.time(); save("state.json", st); continue
            try: jpx_schedule(st)
            except Exception as e: log("jpx error", e)
            try: consensus_scan(st, names); consensus_work(st, 20)
            except Exception as e: log("consensus error", e)
            try: preview_work(st)
            except Exception as e: log("preview error", e)
            time.sleep(5)
            if time.time() - last_scan > 60: collect(st); last_scan = time.time(); save("state.json", st)
            continue
        batch = q[:15]
        miss = [r["code"] for r in batch if r["code"] not in quotes]
        if miss: quotes.update(yf_quotes(miss))
        trmap = translate(st, [r["title"] for r in batch])
        trmap = {k: esc(v) if v else None for k, v in trmap.items()}
        prepared, tasks = [], []
        for r in batch:  # ① 메시지 준비(본문·차트) — AI 작업은 모아 둠
            if time.time() - t0 > RUN_SECONDS: break
            why = muted(r, names, quotes, st)
            if why:
                st["queue"].remove(r)
                m = st.setdefault("muted", {}); d = now().strftime("%Y-%m-%d")
                if m.get("d") != d: m.clear(); m["d"] = d
                m[why] = m.get(why, 0) + 1
                continue
            r2 = dict(r); r2["title"] = r["title"]
            try:
                msg = build(r2, st, names, quotes, {r["title"]: trmap.get(r["title"]) or esc(r["title"])}, tasks)
            except Exception:
                traceback.print_exc(); msg = None
            prepared.append((r, msg, HL.get("chart")))
        res = {}
        if tasks:  # ② 사유·핵심 수치 AI를 묶어서 한 번에
            try: res = ai_batch(st, tasks)
            except Exception: traceback.print_exc()
        for r, msg, chart in prepared:  # ③ 발송
            if msg: msg = ai_fill(msg, res)
            wait = SEND_GAP - (time.time() - last_send)
            if wait > 0: time.sleep(wait)
            ok = tg_send(msg) if msg else True
            if ok and msg and chart:  # 차트는 본문 바로 뒤에 이미지로
                tg_photo(chart)
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
