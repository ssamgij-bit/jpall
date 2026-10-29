#!/usr/bin/env python3
"""일본 실적 스크리닝 봇: 결산단신이 올라오면 규칙으로 판별 → 통과 종목만 카드(차트 + 요약)로 별도 채널에 게시.

- 공시 목록·실적 수치: TDnet 결산단신 XBRL(1차 자료). 가이던스 수정 전후 값은 같은 날 '업적 예상 수정' 공시 XBRL.
- 최근 8개 분기(3개월) 실적·업종·시가총액: 가부탄 모바일(s.kabutan.jp)
- 주가 반응: 네이버 증권 일봉(종목, TOPIX)
- 비즈니스 모델·'왜 좋았나': Gemini가 결산단신 본문(경영성적 개황)만 읽고 작성(원문에 없는 내용은 쓰지 않음)
- 버튼: [리포트] 누르면 상세 리포트(HTML 파일) 전송, [삭제] 누르면 카드 삭제
환경 변수: EARN_TOKEN, EARN_CHANNEL(없으면 봇이 관리자로 추가된 채널을 자동으로 찾음), GEMINI_API_KEY,
          RUN_SECONDS(기본 1680), DRY_RUN=1(게시 안 하고 출력), TEST_DAY=YYYYMMDD(그날 결산 전체를 다시 판별)
"""
import os, sys, re, io, json, time, html, base64, zipfile, datetime as dt, urllib.request, urllib.parse, urllib.error, traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import xbrl as X
from jpall import http, nfkc, scan_tdnet, gemini_json, JST
from jp2ko import jp2ko

STATE_DIR = os.environ.get("STATE_DIR", "estate")
RUN_SECONDS = int(os.environ.get("RUN_SECONDS", "1680"))
DRY = os.environ.get("DRY_RUN") == "1"
TOKEN = os.environ.get("EARN_TOKEN", "")
CHANNEL = os.environ.get("EARN_CHANNEL", "")
TEST_DAY = os.environ.get("TEST_DAY", "")
TEST_REPORT = os.environ.get("TEST_REPORT", "")  # 종목 코드: 요청 분석 카드 + 리포트를 바로 만들어 봄(테스트용)
OUT_DIR = os.environ.get("OUT_DIR", "earn_out")  # DRY_RUN일 때 카드 이미지·리포트 저장 위치
MUA = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148"}

# ───────────────────────── 스크리닝 규칙(사용자와 합의, 2026-10-27) ─────────────────────────
RULES = {
    "R1_rev_yoy": 10.0,       # R1 매출 성장: 분기 매출 YoY ≥ 10%
    "R2_op_yoy": 10.0,        # R2 이익 성장: 분기 영업이익 YoY ≥ 10% 그리고 흑자
    "R3_accel_pp": 5.0,       # R3 성장 가속: 매출 또는 영업이익 YoY가 직전 분기보다 +5%p 이상(둘 다 플러스)
    "R4_turn_yoy": 20.0,      # R4 턴어라운드: 전년 동기 영업적자→흑자, 또는 2개 분기 연속 감익 뒤 영업이익 YoY ≥ 20%
    "X5_min_cap_oku": 300.0,  # X5 시가총액 300억 엔 미만 제외
}
# X4 제외 업종(도쿄증권거래소 33업종)
EXCL_SECTORS = {"建設業", "不動産業", "電気・ガス業", "海運業", "石油・石炭製品", "鉱業", "銀行業", "証券、商品先物取引業", "証券・商品先物取引業", "保険業", "その他金融業", "医薬品", "鉄鋼", "パルプ・紙", "化学"}
SECTOR_KO = {
    "水産・農林業": "수산·농림", "鉱業": "광업", "建設業": "건설", "食料品": "식품", "繊維製品": "섬유",
    "パルプ・紙": "펄프·종이", "化学": "화학", "医薬品": "의약품", "石油・石炭製品": "석유·석탄", "ゴム製品": "고무",
    "ガラス・土石製品": "유리·토석", "鉄鋼": "철강", "非鉄金属": "비철금속", "金属製品": "금속제품", "機械": "기계",
    "電気機器": "전기기기", "輸送用機器": "수송용기기", "精密機器": "정밀기기", "その他製品": "기타제품",
    "電気・ガス業": "전기·가스", "陸運業": "육운", "海運業": "해운", "空運業": "항공", "倉庫・運輸関連業": "창고·운수",
    "情報・通信業": "정보·통신", "卸売業": "도매", "小売業": "소매", "銀行業": "은행", "証券、商品先物取引業": "증권",
    "証券・商品先物取引業": "증권", "保険業": "보험", "その他金融業": "기타금융", "不動産業": "부동산", "サービス業": "서비스",
}

def now(): return dt.datetime.now(JST)
def log(*a): print(now().strftime("%H:%M:%S"), *a, flush=True)
def load():
    try: return json.load(open(os.path.join(STATE_DIR, "earn.json")))
    except Exception: return {}
def save(st):
    os.makedirs(STATE_DIR, exist_ok=True)
    json.dump(st, open(os.path.join(STATE_DIR, "earn.json"), "w"), ensure_ascii=False, separators=(",", ":"))
def esc(s): return html.escape(s or "", quote=False)
def text_of(s): return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()
def mget(url, timeout=25):
    for wait in (0, 3, 8):
        time.sleep(wait)
        try:
            return http(url, timeout=timeout, headers=MUA).decode("utf-8", "ignore")
        except Exception as e:
            err = e
    raise err

# ───────────────────────── 가부탄 모바일: 분기 실적·연간 실적·업종·시총 ─────────────────────────
def _num(c):
    c = nfkc(c).replace(",", "").replace("+", "").replace("円", "").replace("%", "").strip()
    try: return float(c)
    except Exception: return None

def kb_finance(code):
    """{'q': [{start, end, rev, op, ord, ni, eps, ann}], 'y': [...], 'sector': '電気機器'} 금액은 백만엔"""
    s = mget(f"https://s.kabutan.jp/stocks/{code}/finance/")
    out = {"q": [], "y": [], "sector": ""}
    g = re.search(r"業種\s*</span>\s*<a[^>]*>(.*?)</a>", s, re.S)
    out["sector"] = nfkc(text_of(g.group(1))) if g else ""
    for tb in re.findall(r"<table.*?</table>", s, re.S):
        head = text_of(tb[:3000])
        rows = []
        for tr in re.findall(r"<tr.*?</tr>", tb, re.S):
            rows.append([text_of(x) for x in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", tr, re.S)])
        if "発表日" not in head or "売上高" not in head: continue
        quarterly = "損益率" in head
        annual = "1株配" in head and "損益率" not in head
        for c in rows:
            if len(c) < 8: continue
            lab = nfkc(c[0]).replace(" ", "")
            ann = re.search(r"\d{4}-\d{2}-\d{2}", " ".join(c))
            if quarterly:
                m = re.match(r"(\d{2})\.(\d{2})-(\d{2})$", lab)
                if not m: continue
                y, m1, m2 = 2000 + int(m.group(1)), int(m.group(2)), int(m.group(3))
                if (m2 - m1) % 12 != 2: continue  # 3개월 표만
                y2 = y + (1 if m2 < m1 else 0)
                out["q"].append({"start": f"{y}-{m1:02d}", "end": f"{y2}-{m2:02d}", "rev": _num(c[1]), "op": _num(c[2]),
                                 "ord": _num(c[3]), "ni": _num(c[4]), "eps": _num(c[5]), "ann": ann.group(0) if ann else ""})
            elif annual and out.get("_ytb") in (None, id(tb)):
                m = re.match(r"(予)?(\d{4})\.(\d{2})$", lab)
                if not m: continue
                out["_ytb"] = id(tb)
                out["y"].append({"fc": bool(m.group(1)), "end": f"{m.group(2)}-{m.group(3)}", "rev": _num(c[1]), "op": _num(c[2]),
                                 "ord": _num(c[3]), "ni": _num(c[4]), "eps": _num(c[5]), "dps": _num(c[6]), "ann": ann.group(0) if ann else ""})
    out.pop("_ytb", None)
    return out

def kb_profile(code):
    """시가총액(억엔), 회사 개요(일본어 원문)"""
    s = mget(f"https://s.kabutan.jp/stocks/{code}/")
    m = re.search(r"時価総額</div>.*?>\s*(?:([\d,]+)兆)?([\d,.]+)?\s*<span[^>]*>億円", s, re.S)
    cap = (int((m.group(1) or "0").replace(",", "")) * 10000 + float((m.group(2) or "0").replace(",", ""))) if m else None
    o = re.search(r"概要\s*</div>\s*<div[^>]*>(.*?)</div>", s, re.S)
    return cap, (nfkc(text_of(o.group(1))) if o else "")

# ───────────────────────── 영문 회사명(JPX 상장 종목 목록, 하루 1회) ─────────────────────────
_EN = None
def en_name(code):
    """AI 한도 초과 등으로 한국어 표기가 없을 때 쓰는 영문명(일본어 그대로 내보내지 않기 위해)"""
    global _EN
    if _EN is None:
        _EN = {}
        path = os.path.join(STATE_DIR, "names_en.json")
        try:
            c = json.load(open(path))
            if c.get("d") == now().strftime("%Y-%m-%d"): _EN = c["n"]
        except Exception: pass
        if not _EN:
            try:
                import pandas as pd
                page = http("https://www.jpx.co.jp/english/markets/statistics-equities/misc/01.html").decode("utf-8", "ignore")
                m = re.search(r'href="([^"]*data_e\.xlsx?)"', page)
                df = pd.read_excel(io.BytesIO(http("https://www.jpx.co.jp" + m.group(1), timeout=60)), dtype=str)
                for c, n in zip(df["Local Code"], df["Name (English)"]):
                    n = re.sub(r"[\s,.]*(co\.?,?\s*ltd\.?|inc\.?|corporation|corp\.?|limited|k\.k\.)$", "", str(n).strip(), flags=re.I)
                    _EN[str(c)[:4]] = n.replace("・", " ").strip()
                os.makedirs(STATE_DIR, exist_ok=True)
                json.dump({"d": now().strftime("%Y-%m-%d"), "n": _EN}, open(path, "w"), ensure_ascii=False)
            except Exception as e:
                log("english names error", e)
    return _EN.get(code)

def name_of(code, jp, ai=None):
    return (ai or {}).get("name_ko") or en_name(code) or jp2ko(jp)

# ───────────────────────── 네이버 일봉 ─────────────────────────
def naver_daily(sym, days=400, index=False):
    end = now() + dt.timedelta(days=1); start = end - dt.timedelta(days=days)
    kind = "index" if index else "item"
    url = (f"https://api.stock.naver.com/chart/foreign/{kind}/{sym}/day?startDateTime={start:%Y%m%d}0000&endDateTime={end:%Y%m%d}0000")
    r = json.loads(http(url, timeout=20))
    return [{"d": f"{x['localDate'][:4]}-{x['localDate'][4:6]}-{x['localDate'][6:]}", "o": x["openPrice"], "c": x["closePrice"],
             "v": x.get("accumulatedTradingVolume") or 0} for x in r]

# ───────────────────────── 분기 시계열과 규칙 판별 ─────────────────────────
def ym_add(ym, k):
    y, m = int(ym[:4]), int(ym[5:7]); t = y * 12 + (m - 1) + k
    return f"{t // 12}-{t % 12 + 1:02d}"

def yoy(cur, prev):
    """(YoY %, 상태) — 상태: 'turn'(적자→흑자), 'loss'(흑자→적자 또는 적자 지속), None"""
    if cur is None or prev is None: return None, None
    if prev <= 0 < cur: return None, "turn"
    if cur <= 0: return None, "loss"
    return (cur / prev - 1) * 100, None

def build_series(kb_q, r):
    """가부탄 3개월 실적 + 이번 결산단신(누적)으로 분기 시계열(오래된 순, 백만엔) 구성"""
    end = r["end"][:7]; start = r["start"][:7]
    qs = {q["end"]: dict(q) for q in kb_q if q["end"] <= end}
    if end not in qs or qs[end].get("rev") is None:
        # 가부탄에 아직 없으면 누적 − 같은 회계연도 이전 분기 합으로 단독 분기를 계산
        prev = [qs[k] for k in sorted(qs) if start <= qs[k]["start"] and k < end]
        cur = {"start": ym_add(end, -2), "end": end, "ann": now().strftime("%Y-%m-%d")}
        ok = len(prev) == r["q"] - 1
        for m in ("rev", "op", "ord", "ni"):
            v = r.get(m)
            if v is None or not ok or any(p.get(m) is None for p in prev): cur[m] = None; continue
            cur[m] = v / 1e6 - sum(p[m] for p in prev)
        qs[end] = cur
    return [qs[k] for k in sorted(qs)]

def metrics(series, i):
    """i번째 분기의 YoY와 직전 분기 YoY"""
    q = series[i]
    by_end = {s["end"]: s for s in series}
    py = by_end.get(ym_add(q["end"], -12))
    out = {"q": q, "py": py}
    for m in ("rev", "op", "ni"):
        out[m + "_yoy"], out[m + "_st"] = yoy(q.get(m), (py or {}).get(m))
    return out

def judge(series, i):
    """규칙 판별 → (통과 여부, 충족 규칙 목록, 제외 사유)"""
    if i < 1: return False, [], "이력 부족"
    a, b = metrics(series, i), metrics(series, i - 1)
    c = metrics(series, i - 2) if i >= 2 else {}
    rg, og = a["rev_yoy"], a["op_yoy"]
    rg1, og1 = b["rev_yoy"], b["op_yoy"]
    op = a["q"].get("op")
    if rg is None and a["rev_st"] is None: return False, [], "전년 동기 실적 없음"
    # 제외
    if rg is not None and rg1 is not None and og is not None and og1 is not None and rg < rg1 and og < og1:
        return False, [], "X1 매출·영익 증가율 동반 둔화"
    if rg is not None and rg1 is not None and rg < 0 and rg1 < 0:
        return False, [], "X2 매출 2개 분기 연속 감소"
    hit = []
    r1 = rg is not None and rg >= RULES["R1_rev_yoy"]
    r2 = og is not None and og >= RULES["R2_op_yoy"] and (op or 0) > 0
    if r1 and r2: hit += ["매출 성장", "이익 성장"]
    if rg is not None and og is not None and rg > 0 and og > 0 and (
            (rg1 is not None and rg - rg1 >= RULES["R3_accel_pp"]) or (og1 is not None and og - og1 >= RULES["R3_accel_pp"])):
        hit.append("성장 가속")
    two_down = (og1 is not None and og1 < 0 or b.get("op_st") == "loss") and (c.get("op_yoy") is not None and c["op_yoy"] < 0 or c.get("op_st") == "loss")
    if a["op_st"] == "turn" or (two_down and og is not None and og >= RULES["R4_turn_yoy"]):
        hit.append("턴어라운드")
    return bool(hit), hit, ""

# ───────────────────────── 결산단신 XBRL ─────────────────────────
def read_tanshin(row):
    blob = http(row["xbrl"], timeout=60)
    kind, facts, ctx = X.open_zip(blob)
    if not kind or not (kind.endswith("sm") or kind.endswith("sy")): return None, ""
    r = X.summarize_results(kind, facts, ctx)
    if not r: return None, ""
    r["kind"] = kind
    sh = [v for n, cid, v in facts if n.startswith("NumberOfIssuedAndOutstandingShares") and cid.startswith("Current")]
    tr = [v for n, cid, v in facts if n.startswith("NumberOfTreasuryStock") and cid.startswith("Current")]
    r["shares"] = (max(sh) - (max(tr) if tr else 0)) if sh else None
    # 본문(경영성적 개황 등) — 첨부의 qualitative.htm
    qual = ""
    try:
        zf = zipfile.ZipFile(io.BytesIO(blob))
        n = next((n for n in zf.namelist() if n.endswith("qualitative.htm")), None)
        if n:
            qual = nfkc(text_of(zf.read(n).decode("utf-8", "ignore")))
    except Exception as e:
        log("qualitative error", e)
    return r, qual

def find_revision(rows, code):
    """같은 날 같은 종목의 '업적 예상 수정' 공시 XBRL → 연간 영업이익 (이전, 수정)"""
    for x in rows:
        if x["code"] != code or not x.get("xbrl"): continue
        if "予想" in x["title"] and "修正" in x["title"] and "決算短信" not in x["title"]:
            try:
                kind, facts, ctx = X.open_zip(http(x["xbrl"], timeout=40))
                rv = X.summarize_revision(facts, ctx)
                if rv and rv["per"] == "CurrentYearDuration": return rv
            except Exception as e:
                log("revision error", code, e)
    return None

NUMTOK = re.compile(r"^[△▲\-]?[\d,]+(\.\d+)?$|^[－\-―ー]$")
def _toks(seg, n=5):
    out = []
    for t in seg.split():
        t = nfkc(t)
        if t in ("-", "―", "ー", "－"): out.append(None)
        elif re.fullmatch(r"[△▲-]?[\d,]+(\.\d+)?", t):
            v = float(t.lstrip("△▲-").replace(",", "")); out.append(-v if t[0] in "△▲-" else v)
        elif out: break
        if len(out) >= n: break
    return out

def revision_from_pdf(url):
    """업적 예상 수정 공시 PDF(XBRL 없는 경우) → 연간 {'op': (이전, 수정), 'rev': (...)} (백만엔)"""
    import subprocess
    pdf = http(url, timeout=40)
    t = subprocess.run(["pdftotext", "-layout", "-", "-"], input=pdf, capture_output=True, timeout=60).stdout.decode("utf-8", "ignore")
    t = nfkc(t)
    unit = 1e-3 if "千円" in t and "百万円" not in t else 1.0
    best = None
    for m in re.finditer(r"今回(修正|発表)?予想", t):
        a = t.rfind("前回", 0, m.start())
        if a < 0 or m.start() - a > 800: continue
        prev = _toks(re.sub(r"^[^\n]*?\)", "", t[a:m.start()].split("予想", 1)[-1], count=1))
        cur = _toks(t[m.end():].split(")", 1)[-1])
        if len(prev) >= 2 and len(cur) >= 2 and prev[0] and cur[0]:
            if best is None or cur[0] > best[1][0]: best = (prev, cur)
    if not best: return None
    (p, c) = best
    return {"rev": (p[0] * unit, c[0] * unit), "op": ((p[1] or 0) * unit if p[1] is not None else None, (c[1] or 0) * unit if c[1] is not None else None)}

def find_revision_pdf(rows, code):
    for x in rows:
        if x["code"] == code and "予想" in x["title"] and "修正" in x["title"] and "決算短信" not in x["title"] and "配当予想の修正に関する" not in x["title"][:14]:
            if not re.search(r"通期|業績予想", x["title"]): continue
            try:
                return revision_from_pdf(x["pdf"])
            except Exception as e:
                log("revision pdf error", code, e)
    return None

def find_material(rows, code):
    """같은 날 같은 종목의 결산 설명 자료 PDF"""
    for x in rows:
        if x["code"] == code and re.search(r"説明(会)?資料|決算補足|プレゼンテーション|決算説明|補足資料|Presentation", x["title"]):
            return x
    return None

# ───────────────────────── 서식 ─────────────────────────
def oku(mil):
    """백만엔 → '227억' 형식"""
    if mil is None: return "—"
    x = mil / 100
    if abs(x) >= 100: return f"{x:,.0f}억"
    if abs(x) >= 10: return f"{x:,.1f}억"
    return f"{x:,.2f}억"
def yoy_txt(v, stt):
    if stt == "turn": return "흑자전환"
    if stt == "loss": return "적자"
    if v is None: return "—"
    return f"{v / 100 + 1:.1f}배" if v >= 900 else f"{v:+.1f}%"
def fy_of(end_ym, fy_end_month):
    """분기 말 → (FY 라벨, 분기 번호) 예: ('FY3/27', 2)"""
    y, m = int(end_ym[:4]), int(end_ym[5:7])
    k = (m - fy_end_month - 1) % 12  # 0..11 (회계연도 시작 후 몇 달째)
    fy_y = y if m <= fy_end_month else y + 1
    return f"FY{fy_end_month}/{str(fy_y)[2:]}", k // 3 + 1
def qlabel(q, fy_end_month):
    fy, n = fy_of(q["end"], fy_end_month)
    m1, m2 = int(q["start"][5:7]), int(q["end"][5:7])
    return f"{fy} {n}Q", f"{m1}~{m2}월"

# ───────────────────────── 차트(카드 이미지) ─────────────────────────
def _plt():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager as fm
    try:  # koreanize-matplotlib 패키지에 든 나눔고딕 파일만 직접 등록(패키지 import는 파이썬 3.12에서 실패함)
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
            plt.rcParams["font.family"] = nm; break
    else:
        log("한글 글꼴을 찾지 못했습니다")
    plt.rcParams["axes.unicode_minus"] = False
    return plt

BLUE, GRAY, ORANGE, INK, MUTED = "#2f6fd6", "#b9b6ae", "#e8743b", "#1f2328", "#6b7280"

def card_chart(name, series, fy_end_month, path):
    plt = _plt()
    s = series[-8:]
    labs = []
    for q in s:
        fy, n = fy_of(q["end"], fy_end_month)
        labs.append(f"{n}Q\n{fy}")
    fig, axes = plt.subplots(2, 1, figsize=(7.2, 7.6), dpi=150)
    fig.patch.set_facecolor("white")
    for ax, key, title in ((axes[0], "op", "① 분기 영업이익과 YoY"), (axes[1], "rev", "② 분기 매출과 YoY")):
        vals = [(q.get(key) or 0) / 100 for q in s]
        cols = [BLUE if i == len(s) - 1 else "#9dbbea" for i in range(len(s))]
        cols = [("#d9534f" if v < 0 else c) for v, c in zip(vals, cols)]
        ax.bar(range(len(s)), vals, color=cols, width=0.62, zorder=2)
        for i, v in enumerate(vals):
            if i == len(s) - 1 or i == len(s) - 5:
                ax.text(i, v, f"{v:,.1f}" if abs(v) < 100 else f"{v:,.0f}", ha="center", va="bottom" if v >= 0 else "top",
                        fontsize=9, fontweight="bold", color=INK)
        ax.axhline(0, color="#999", lw=0.6)
        ax.set_xticks(range(len(s))); ax.set_xticklabels(labs, fontsize=8, color=MUTED)
        ax.set_title(title, loc="left", fontsize=11, fontweight="bold", color=INK)
        ax.set_ylabel("억 엔", fontsize=8, color=MUTED)
        ax.grid(axis="y", color="#eee", zorder=0)
        for sp in ("top", "right"): ax.spines[sp].set_visible(False)
        ax2 = ax.twinx()
        by_end = {q["end"]: q for q in series}
        ys = []
        for q in s:
            py = by_end.get(ym_add(q["end"], -12))
            v, stt = yoy(q.get(key), (py or {}).get(key))
            ys.append(v)
        xs = [i for i, v in enumerate(ys) if v is not None]
        if xs:
            cap = 300.0  # 기저 효과로 수천 % 가 나오면 축이 눌리므로 300%에서 자르고 숫자로 표시
            yc = [min(max(ys[i], -100), cap) for i in xs]
            ax2.plot(xs, yc, color=ORANGE, marker="o", ms=4, lw=1.6, zorder=3)
            for i, v in zip(xs, yc):
                if ys[i] > cap: ax2.annotate(f"{ys[i]:+,.0f}%", (i, v), textcoords="offset points", xytext=(0, 6), ha="center", fontsize=8, color=ORANGE)
            last = len(s) - 1
            if ys[last] is not None and ys[last] <= cap:
                ax2.annotate(f"{ys[last]:+.0f}%", (last, min(ys[last], cap)), textcoords="offset points", xytext=(-16, 6), ha="right",
                             fontsize=9, color=ORANGE, fontweight="bold")
            ax2.axhline(0, color=ORANGE, lw=0.5, ls=":")
        ax2.set_ylabel("YoY(%)", fontsize=8, color=ORANGE)
        ax2.tick_params(axis="y", labelsize=8, colors=ORANGE)
        for sp in ("top",): ax2.spines[sp].set_visible(False)
        ax.tick_params(axis="y", labelsize=8, colors=MUTED)
    fig.suptitle(f"{name} · 분기 실적(3개월 단독)", x=0.02, ha="left", fontsize=13, fontweight="bold", color=INK)
    fig.text(0.02, 0.005, "출처: TDnet 결산단신 XBRL, 가부탄 3개월 실적 · 막대=억 엔(진한 색=이번 분기), 선=전년 동기 대비", fontsize=7, color=MUTED)
    fig.tight_layout(rect=(0, 0.02, 1, 0.97))
    fig.savefig(path, facecolor="white"); plt.close(fig)

# ───────────────────────── Gemini: 비즈니스 모델·왜 좋았나 ─────────────────────────
CARD_PROMPT = """너는 일본 중소형주를 보는 한국 애널리스트다. 첨부 PDF(결산 설명 자료=IR 발표 자료, 있으면)와 아래 결산단신 본문(일본어)·수치를 읽고 한국어로 JSON을 써라.
규칙
- 자료에 적힌 내용만 쓴다. 추측 금지. 원인이 자료 어디에도 없으면 그 항목은 "원문 설명 없음".
- 원인은 IR 발표 자료를 먼저 본다: 영업이익 증감 요인(가격·물량·원가·환율 브리지), 사업부·제품·용도·지역별 매출, 수주·KPI 슬라이드의 숫자를 우선 인용.
  발표 자료에 없을 때 결산단신 본문으로 보완. 숫자가 있으면 꼭 숫자로(예: "AI 서버용 매출 +80%, 판가 인상 효과 +8억 엔").
- 일본어를 그대로 쓰지 말고 전부 한국어로. 회사명·제품명은 한글 발음이나 통용 영문명으로.
- 짧게: bm 45자, summary 60자, P/Q/C/etc 각 70자 이내. 숫자는 본문 그대로(억 엔 환산은 1억 엔=100백만 엔).
- P=가격·환율·제품 믹스, Q=물량·수주·고객 수·점포 수, C=원가·판관비·감가상각, etc=일회성·영업외·특별손익 등 그 밖의 요인.
출력: {{"name_ko": "회사명(한국어 표기)", "bm": "무엇을 만들어/팔아 누구에게 파는 회사인지", "summary": "이번 분기 실적이 좋았던 핵심 이유 한 줄",
 "P": "...", "Q": "...", "C": "...", "etc": "..."}}

회사: {name} ({code}) · 업종: {sector}
회사 개요(가부탄): {profile}
수치: {nums}
결산단신 본문:
{qual}
"""

def find_mat_pdf(a, row):
    """카드 '왜 좋았나'용 IR 발표 자료: TDnet 설명 자료 → 회사 IR 홈페이지. → (bytes, url, 출처) 또는 (None, None, None)"""
    url = (a.get("mat") or {}).get("pdf")
    if url:
        try: return http(url, timeout=60), url, "TDnet"
        except Exception as e: log("material download error", e)
    try:
        import earn_ir
        for t, u, d in earn_ir.ir_site_docs(a["code"], row["d"], limit=4, pat=earn_ir.MAT_LINK):
            if earn_ir.QA_LINK.search(t): continue
            kind, body = earn_ir.fetch_doc(u, pat=earn_ir.MAT_LINK)
            if kind == "pdf" and len(body) < 12_000_000: return body, u, "회사 IR 홈페이지"
    except Exception as e:
        log("ir site material error", a["code"], e)
    return None, None, None

def ai_card(info, pdf=None):
    p = CARD_PROMPT.format(name=info["name"], code=info["code"], sector=info["sector"], profile=info["profile"][:400],
                           nums=info["nums"], qual=info["qual"][:6000])
    out = None
    if pdf:
        try:
            import earn_report
            out = earn_report.gemini_pdf(p, [pdf], max_tokens=4000)
        except Exception as e:
            log("card pdf ai error", e)
    if not out: out = gemini_json(p, timeout=120)
    if isinstance(out, list): out = out[0] if out and isinstance(out[0], dict) else None
    return out if isinstance(out, dict) else None

# ───────────────────────── 카드 ─────────────────────────
def analyze(row, rows, st):
    """결산단신 1건 → None(판별 불가) 또는 dict(pass, hit, reason, 카드 재료)"""
    r, qual = read_tanshin(row)
    if not r: return None
    code = row["code"]
    kb = kb_finance(code)
    sector = kb["sector"]
    series = build_series(kb["q"], r)
    i = len(series) - 1
    ok, hit, why = judge(series, i)
    res = {"code": code, "name": row["name"], "sector": sector, "pass": ok, "hit": hit, "why": why, "series": series, "r": r}
    if sector in EXCL_SECTORS:
        res.update(pass_=False); res["pass"] = False; res["why"] = f"X4 제외 업종({SECTOR_KO.get(sector, sector)})"
        return res
    if not ok: return res
    cap, profile = kb_profile(code)
    if cap is None and r.get("shares"):  # 가부탄에 시총이 비어 있으면 발행주식수(XBRL) × 최근 종가(네이버)
        try:
            bars = naver_daily(f"{code}.T", days=10)
            if bars: cap = r["shares"] * bars[-1]["c"] / 1e8
        except Exception as e:
            log("cap fallback error", code, e)
    res["cap"] = cap
    if cap is None:
        res["pass"] = False; res["why"] = "X5 시총 확인 불가"
        return res
    if cap is not None and cap < RULES["X5_min_cap_oku"]:
        res["pass"] = False; res["why"] = f"X5 시총 {cap:,.0f}억 엔"
        return res
    res["prev_ok"] = judge(series, i - 1)[0] if i >= 2 else False
    rv = find_revision(rows, code)
    if rv:
        rv = {"rev": (rv["rows"]["rev"][0] / 1e6 if rv["rows"]["rev"][0] else None, rv["rows"]["rev"][1] / 1e6 if rv["rows"]["rev"][1] else None),
              "op": (rv["rows"]["op"][0] / 1e6 if rv["rows"]["op"][0] is not None else None, rv["rows"]["op"][1] / 1e6 if rv["rows"]["op"][1] is not None else None)}
    else:
        rv = find_revision_pdf(rows, code)
    res.update(profile=profile, qual=qual, rev=rv, mat=find_material(rows, code))
    return res

def fy_end_month_of(r):
    """회계연도 결산월 = 누적 기간 시작월의 전달"""
    return (int(r["start"][5:7]) - 2) % 12 + 1

def card_text(a, ai, row):
    r, series = a["r"], a["series"]
    fem = fy_end_month_of(r)
    q = series[-1]
    m = metrics(series, len(series) - 1)
    py = m["py"] or {}
    name = name_of(a["code"], a["name"], ai)
    t = row["t"]; d = row["d"]
    L = [f"{'🔎' if a.get('ondemand') else '📌'} <b>{esc(name)} ({a['code']})</b> · {int(d[4:6])}/{int(d[6:])}" + (f" {t}" if t else "")]
    fc = r.get("fc") or {}
    per = ""
    if a.get("cap") and fc.get("ni") and fc["ni"] > 0:
        per = f" · PER <b>{a['cap'] * 1e8 / fc['ni']:.1f}배</b> ({fy_label_end(r.get('fc_end'))} 순이익 예상 기준)"
    L.append(f"시총 <b>{a['cap']:,.0f}억 엔</b>{per}" if a.get("cap") else "시총 —")
    if a.get("ondemand"):
        L.append("요청 분석 · 스크리닝 " + (("통과: " + " · ".join(a["hit"])) if a.get("pass") else ("미통과" + (f"({a['why']})" if a.get("why") else ""))))
    else:
        L.append("✅ " + " · ".join(a["hit"]) + (" · <b>2개 분기 연속 충족</b>" if a.get("prev_ok") else ""))
    L.append("")
    L.append(f"▶️ <b>섹터</b> {esc(SECTOR_KO.get(a['sector'], a['sector']))}")
    if ai and ai.get("bm"): L.append(f"▶️ <b>비즈니스 모델</b> {esc(ai['bm'])}")
    L.append("")
    # 실적 유형
    tags = []
    if "매출 성장" in a["hit"]: tags.append("실적 호조")
    if "성장 가속" in a["hit"]: tags.append("성장 가속")
    if "턴어라운드" in a["hit"]: tags.append("턴어라운드")
    rv = a.get("rev")
    g_line = ""
    if rv:
        o0, o1 = rv["op"]
        if o0 and o1:
            tags.append("가이던스 상향" if o1 > o0 else "가이던스 하향" if o1 < o0 else "가이던스 유지")
            g_line = f"{fy_label_end(r.get('fc_end'))} 영업이익 예상 {oku(o0)} → <b>{oku(o1)}</b>({(o1 / o0 - 1) * 100:+.0f}%)"
    lab, months = qlabel(q, fem)
    base_tag = (("증수" if (m["rev_yoy"] or 0) >= 0 else "감수") + "·" +
                ({"turn": "흑자전환", "loss": "적자"}.get(m["op_st"]) or ("증익" if (m["op_yoy"] or 0) >= 0 else "감익")))
    tags = [base_tag] + tags if (a.get("ondemand") or not tags) else tags
    L.append("▶️ <b>실적 유형</b> " + " · ".join(dict.fromkeys(tags)))
    L.append(f"{lab}({months}) 매출/영익/순익")
    L.append(f" {oku(q.get('rev'))}/{oku(q.get('op'))}/{oku(q.get('ni'))} "
             f"({yoy_txt(m['rev_yoy'], m['rev_st'])}/{yoy_txt(m['op_yoy'], m['op_st'])}/{yoy_txt(m['ni_yoy'], m['ni_st'])})")
    if py:
        L.append(f" 전년 동기 {oku(py.get('rev'))}/{oku(py.get('op'))}/{oku(py.get('ni'))}")
    if not g_line and fc.get("op") and r["q"] < 4:
        g_line = f"{fy_label_end(r.get('fc_end'))} 영업이익 예상 {oku(fc['op'] / 1e6)}"
    elif not g_line and fc.get("op") and r["q"] == 4:
        base = r.get("op")
        g_line = f"{fy_label_end(r.get('fc_end'))} 영업이익 예상 {oku(fc['op'] / 1e6)}" + (f"(올해 대비 {(fc['op'] / base - 1) * 100:+.0f}%)" if base and base > 0 else "")
    if g_line:
        prog = progress(series, r, fem)
        L.append(g_line + (f" · 진척률 {prog[0]:.0f}%" + (f"(작년 {prog[1]:.0f}%)" if prog[1] is not None else "") if prog else ""))
    L.append("")
    if ai:
        L.append(("▶️ <b>왜 좋았나</b>" if not a.get("ondemand") or a.get("pass") else "▶️ <b>실적 요인</b>")
                 + (" <i>(IR 발표 자료 기준)</i>" if ai.get("_src") else ""))
        if ai.get("summary"): L.append(esc(ai["summary"]))
        for k in ("P", "Q", "C", "etc"):
            v = (ai.get(k) or "").strip()
            if v and "원문 설명 없음" not in v: L.append(f"<b>{'기타' if k == 'etc' else k}:</b> {esc(v)}")
        L.append("")
    L.append("▶️ <b>주가</b> 다음 거래일 장 마감 후 업데이트")
    return "\n".join(L)

def fy_label_end(end):
    try: return f"FY{int(end[5:7])}/{end[2:4]}"
    except Exception: return "연간"

def progress(series, r, fem):
    """누적 영업이익 ÷ 연간 예상(%), 작년 같은 시점 진척률(실적 기준)"""
    fc = r.get("fc") or {}
    if r["q"] == 4 or not fc.get("op") or fc["op"] <= 0 or r.get("op") is None: return None
    now_p = r["op"] / fc["op"] * 100
    by_end = {s["end"]: s for s in series}
    end = r["end"][:7]
    ly_q = [by_end.get(ym_add(end, -12 - 3 * k)) for k in range(r["q"])]
    fy_end = ym_add(end, (4 - r["q"]) * 3 - 12)
    ly_full = [by_end.get(ym_add(fy_end, -3 * k)) for k in range(4)]
    last = None
    if all(x and x.get("op") is not None for x in ly_q + ly_full):
        full = sum(x["op"] for x in ly_full)
        if full > 0: last = sum(x["op"] for x in ly_q) / full * 100
    return now_p, last

# ───────────────────────── 텔레그램 ─────────────────────────
def tg(method, data=None, files=None, timeout=40):
    url = f"https://api.telegram.org/bot{TOKEN}/{method}"
    if files:
        bnd = "----earnbot" + str(int(time.time() * 1000))
        body = io.BytesIO()
        for k, v in (data or {}).items():
            body.write(f"--{bnd}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
        for k, (fn, blob, ctype) in files.items():
            body.write(f"--{bnd}\r\nContent-Disposition: form-data; name=\"{k}\"; filename=\"{fn}\"\r\nContent-Type: {ctype}\r\n\r\n".encode())
            body.write(blob); body.write(b"\r\n")
        body.write(f"--{bnd}--\r\n".encode())
        payload, ctype = body.getvalue(), f"multipart/form-data; boundary={bnd}"
    else:
        payload, ctype = json.dumps(data or {}).encode(), "application/json"
    for _ in range(4):
        try:
            return json.loads(http(url, timeout=timeout, data=payload, headers={"content-type": ctype}))
        except urllib.error.HTTPError as e:
            b = e.read().decode("utf-8", "ignore")
            if e.code == 429:
                time.sleep(json.loads(b).get("parameters", {}).get("retry_after", 5) + 1); continue
            log("telegram", method, e.code, b[:300])
            return {"ok": False, "description": b}
        except Exception as e:
            log("telegram", method, e); time.sleep(3)
    return {"ok": False}

def keyboard(cid, card=None):
    card = card or {}
    row = [] if card.get("report_sent") else [{"text": "📄 리포트", "callback_data": f"rep:{cid}"}]
    if not card.get("ir_sent"): row.append({"text": "🗣 IR·Q&A", "callback_data": f"ir:{cid}"})
    return json.dumps({"inline_keyboard": [row] if row else []})

def find_channel(st):
    global CHANNEL
    if CHANNEL: return CHANNEL
    if st.get("channel"): CHANNEL = st["channel"]; return CHANNEL
    res = tg("getUpdates", {"timeout": 0, "allowed_updates": ["my_chat_member", "channel_post", "callback_query", "message"]}).get("result") or []
    for u in res:
        for k in ("my_chat_member", "channel_post"):
            c = (u.get(k) or {}).get("chat") or {}
            if c.get("type") == "channel": CHANNEL = str(c["id"])
    if CHANNEL:
        st["channel"] = CHANNEL; log("channel found", CHANNEL)
    return CHANNEL

def post_card(st, cid, caption, png, chat=None):
    if DRY:
        os.makedirs(OUT_DIR, exist_ok=True)
        open(os.path.join(OUT_DIR, f"{cid}.png"), "wb").write(png)
        open(os.path.join(OUT_DIR, f"{cid}.txt"), "w").write(caption)
        print("-" * 60 + "\n" + re.sub(r"</?b>", "", caption), f"\n[{len(caption)}자]"); return 0
    if len(caption) > 1024:  # 사진 설명 글 한도 → '왜 좋았나' 세부 줄부터 줄임
        caption = shrink(caption)
    r = tg("sendPhoto", {"chat_id": chat or CHANNEL, "caption": caption, "parse_mode": "HTML", "reply_markup": keyboard(cid)},
           files={"photo": (f"{cid}.png", png, "image/png")})
    return (r.get("result") or {}).get("message_id")

def shrink(c):
    lines = c.split("\n")
    order = ["<b>기타:</b>", "<b>C:</b>", "<b>Q:</b>", "<b>P:</b>", " 전년 동기", "▶️ <b>비즈니스 모델</b>"]
    for key in order:
        if len("\n".join(lines)) <= 1024: break
        lines = [x for x in lines if not x.startswith(key)]
    out = "\n".join(lines)
    return out[:1020] + "…" if len(out) > 1024 else out

# ───────────────────────── 발표 후 주가 반응(다음 거래일 마감 후 카드 수정) ─────────────────────────
def reaction(card):
    """{'day', 'open', 'close', 'mkt', 'vol'} 또는 None(아직 이름)"""
    d = f"{card['d'][:4]}-{card['d'][4:6]}-{card['d'][6:]}"
    bars = naver_daily(f"{card['code']}.T", days=200)
    if not bars or bars[0]["d"] > d: return None
    intraday = card["t"] < "15:30"
    pick = [b for b in bars if (b["d"] >= d if intraday else b["d"] > d)]
    if not pick: return None
    day = pick[0]
    if now().strftime("%Y-%m-%d") == day["d"] and now().strftime("%H:%M") < "15:45": return None
    k = bars.index(day)
    if k < 1: return None
    base = bars[k - 1]["c"]
    vols = [b["v"] for b in bars[max(0, k - 20):k] if b["v"]]
    tp = naver_daily(".TOPX", days=60, index=True)
    tk = next((j for j, b in enumerate(tp) if b["d"] == day["d"]), None)
    mkt = (tp[tk]["c"] / tp[tk - 1]["c"] - 1) * 100 if tk else None
    return {"day": day["d"], "base": "발표일 종가" if not intraday else "전일 종가", "open": (day["o"] / base - 1) * 100,
            "close": (day["c"] / base - 1) * 100, "mkt": mkt, "vol": day["v"] / (sum(vols) / len(vols)) if vols else None}

def reaction_line(rx):
    s = f"다음 날({int(rx['day'][5:7])}/{int(rx['day'][8:])}) 시가 {rx['open']:+.1f}% → 종가 <b>{rx['close']:+.1f}%</b>\n({rx['base']} 대비"
    if rx["mkt"] is not None: s += f", TOPIX {rx['mkt']:+.1f}%"
    if rx["vol"]: s += f", 거래량 평소의 {rx['vol']:.1f}배"
    return s + ")"

def update_reactions(st):
    for cid, c in list(st.get("cards", {}).items()):
        if c.get("rx") or not c.get("msg"): continue
        if time.time() - c.get("ts", 0) > 86400 * 10: c["rx"] = "skip"; continue
        try:
            rx = reaction(c)
        except Exception as e:
            log("reaction error", cid, e); continue
        if not rx: continue
        cap = c["caption"].replace("▶️ <b>주가</b> 다음 거래일 장 마감 후 업데이트", "▶️ <b>주가</b>\n" + reaction_line(rx))
        cap = shrink(cap) if len(cap) > 1024 else cap
        r = tg("editMessageCaption", {"chat_id": c.get("chat") or CHANNEL, "message_id": c["msg"], "caption": cap, "parse_mode": "HTML",
                                      "reply_markup": keyboard(cid, c)})
        if r.get("ok") or "not modified" in str(r.get("description", "")) or "not found" in str(r.get("description", "")):
            c["rx"] = rx; c["caption"] = cap
            log("reaction updated", c["code"], f"{rx['close']:+.1f}%")

# ───────────────────────── 버튼 처리 ─────────────────────────
def poll_buttons(st, wait=0):
    if DRY or not TOKEN: time.sleep(wait); return
    res = tg("getUpdates", {"offset": st.get("off", 0), "timeout": wait, "allowed_updates": ["callback_query", "my_chat_member", "channel_post", "message"]},
             timeout=wait + 20).get("result") or []
    for u in res:
        st["off"] = u["update_id"] + 1
        m = u.get("channel_post") or u.get("message")
        if m and m.get("text") and not (m.get("from") or {}).get("is_bot"):
            handle_text(st, m.get("chat") or {}, m["text"]); continue
        cq = u.get("callback_query")
        if not cq: continue
        data = cq.get("data") or ""
        act, _, cid = data.partition(":")
        card = st.get("cards", {}).get(cid)
        msg = cq.get("message") or {}
        if act == "del" and (cq.get("from") or {}).get("id") != st.get("owner"):  # 삭제 버튼은 없앰(예전 카드의 버튼은 주인만)
            tg("answerCallbackQuery", {"callback_query_id": cq["id"], "text": "삭제 기능은 제공하지 않습니다."}); continue
        if act == "del":
            tg("answerCallbackQuery", {"callback_query_id": cq["id"], "text": "삭제했습니다."})
            tg("deleteMessage", {"chat_id": msg.get("chat", {}).get("id", CHANNEL), "message_id": msg.get("message_id")})
            if card: card["deleted"] = True; card["rx"] = card.get("rx") or "deleted"
            log("deleted", cid)
        elif act in ("rep", "ir"):
            if not card:
                tg("answerCallbackQuery", {"callback_query_id": cq["id"], "text": "카드 정보가 만료됐습니다."}); continue
            tg("answerCallbackQuery", {"callback_query_id": cq["id"], "text": "리포트를 만드는 중입니다(1~2분)." if act == "rep" else "IR·Q&A 요약을 만드는 중입니다(1~3분)."})
            key = "rep_q" if act == "rep" else "ir_q"
            st.setdefault(key, [])
            if cid not in st[key]: st[key].append(cid)

def send_texts(card, texts):
    ok = False
    for t in texts:
        r = tg("sendMessage", {"chat_id": card.get("chat") or CHANNEL, "text": t, "parse_mode": "HTML", "disable_web_page_preview": True, "reply_to_message_id": card["msg"]})
        if not r.get("ok"):
            r = tg("sendMessage", {"chat_id": card.get("chat") or CHANNEL, "text": re.sub(r"<[^>]+>", "", t), "reply_to_message_id": card["msg"]})
        ok = ok or bool(r.get("ok")); time.sleep(1)
    return ok

def do_ir(st):
    q = st.get("ir_q") or []
    while q:
        cid = q.pop(0)
        card = st.get("cards", {}).get(cid)
        if not card: continue
        try:
            import earn_ir
            texts, has_qa = earn_ir.build(card)
            if not texts:
                tg("sendMessage", {"chat_id": card.get("chat") or CHANNEL, "text": f"IR·Q&A 요약 생성 실패({card['code']}). AI 한도일 수 있으니 잠시 뒤 다시 눌러 주세요.", "reply_to_message_id": card["msg"]}); continue
            if send_texts(card, texts):
                card["ir_sent"] = True
                if not has_qa: card["qa_watch"] = time.time()
                tg("editMessageReplyMarkup", {"chat_id": card.get("chat") or CHANNEL, "message_id": card["msg"], "reply_markup": keyboard(cid, card)})
        except Exception:
            traceback.print_exc()

def qa_followup(st, day, rows):
    """IR 요약을 보냈는데 Q&A 자료가 없던 종목 → 14일 안에 TDnet에 질의응답이 올라오면 다시 요약해 보냄"""
    import earn_ir
    for cid, card in st.get("cards", {}).items():
        w = card.get("qa_watch")
        if not w or card.get("deleted"): continue
        if time.time() - w > 86400 * 14: card.pop("qa_watch", None); continue
        hits = [r for r in rows if r["code"] == card["code"] and earn_ir.QA_RE.search(r["title"]) and r["id"] not in card.get("qa_seen", [])]
        if not hits:  # TDnet에 없으면 6시간마다 회사 IR 홈페이지·로그미도 다시 확인
            if time.time() - card.get("qa_site_ts", 0) < 6 * 3600: continue
            card["qa_site_ts"] = time.time()
            try:
                if not earn_ir.other_qa(card): continue
                texts, has_qa = earn_ir.build(card, qa_rows=[])
                if texts and has_qa:
                    texts[0] = "📞 <b>회사 IR 홈페이지에 질의응답 자료가 올라와 Q&amp;A를 추가했습니다</b>\n\n" + texts[0]
                    send_texts(card, texts); card.pop("qa_watch", None)
            except Exception:
                traceback.print_exc()
            continue
        card.setdefault("qa_seen", []).extend(r["id"] for r in hits)
        try:
            texts, has_qa = earn_ir.build(card, qa_rows=hits)
            if texts and has_qa:
                texts[0] = "📞 <b>질의응답 자료가 올라와 Q&amp;A를 추가했습니다</b>\n\n" + texts[0]
                send_texts(card, texts); card.pop("qa_watch", None)
        except Exception:
            traceback.print_exc()

def do_reports(st):
    do_asks(st)
    do_ir(st)
    q = st.get("rep_q") or []
    while q:
        cid = q.pop(0)
        card = st.get("cards", {}).get(cid)
        if not card: continue
        try:
            import earn_report
            fn, blob = earn_report.build(card)
            ok_ai = card.pop("_ai_ok", True)
            cap = f"📄 {card.get('name_ko') or card['name']}({card['code']}) 리포트 — 파일을 열어 보세요"
            if not ok_ai: cap += "\n⚠️ AI 분석(결론·사업부·IR 핵심 데이터·Q&A)은 구글 서버 혼잡으로 빠졌습니다. 잠시 뒤 📄 리포트를 다시 누르면 새로 만듭니다."
            res = tg("sendDocument", {"chat_id": card.get("chat") or CHANNEL, "caption": cap,
                                      "reply_to_message_id": card["msg"]}, files={"document": (fn, blob, "text/html")}, timeout=90)
            if res.get("ok") and ok_ai:
                card["report_sent"] = True
                tg("editMessageReplyMarkup", {"chat_id": card.get("chat") or CHANNEL, "message_id": card["msg"], "reply_markup": keyboard(cid, card)})
        except Exception:
            traceback.print_exc()
            tg("sendMessage", {"chat_id": card.get("chat") or CHANNEL, "text": f"리포트 생성 실패({card['code']}). 잠시 뒤 다시 눌러 주세요.", "reply_to_message_id": card["msg"]})

# ───────────────────────── 요청 분석: 종목명·코드를 보내면 최근 분기 실적 카드 ─────────────────────────
_IDX = None
def name_index():
    """[(코드, 일본어명, 영문명, 한글 읽기)] — JPX 상장 종목 목록(하루 1회)"""
    global _IDX
    if _IDX is None:
        import jpall
        names = jpall.load_names({})
        en_name("0000")
        _IDX = [(c, v.get("jp", ""), (_EN or {}).get(c, ""), v.get("ko", "")) for c, v in names.items() if v.get("mkt") in ("프라임", "스탠다드", "그로스")]
    return _IDX

def _norm(x):
    x = nfkc(x or "").lower()
    x = re.sub(r"(株式会社|\(株\)|ホールディングス|holdings?|hd|co\.?,?\s*ltd\.?|inc\.?|corporation|corp\.?|group|グループ|홀딩스|그룹)", "", x)
    return re.sub(r"[\s・.,'&\-()（）]", "", x)

def resolve(q):
    """→ (코드 또는 None, 후보 목록)"""
    q = nfkc(q).strip()
    nested = q.startswith("_")
    if nested: q = q[1:]
    m = re.fullmatch(r"(?:/?\w*\s+)?([0-9][0-9A-Za-z]{3})", q)
    idx = name_index()
    codes = {c for c, *_ in idx}
    if m and m.group(1).upper() in codes: return m.group(1).upper(), []
    nq = _norm(re.sub(r"^/?(분석|실적|a)\s+", "", q))
    if len(nq) < 2: return None, []
    exact = [c for c, jp, en, ko in idx if nq in (_norm(jp), _norm(en), _norm(ko))]
    if len(exact) == 1: return exact[0], []
    part = [c for c, jp, en, ko in idx if any(nq in _norm(x) for x in (jp, en, ko) if x)]
    cands = exact or part
    if not cands and re.search(r"[가-힣]", q) and not nested:
        # 한글 이름(예: 토요타) → 일본어·영어로 옮겨 다시 찾기
        alts = []
        for tl in ("ja", "en"):
            try:
                url = f"https://translate.googleapis.com/translate_a/single?client=gtx&sl=ko&tl={tl}&dt=t&q=" + urllib.parse.quote(q)
                alts.append("".join(x[0] for x in json.loads(http(url, timeout=15))[0]))
            except Exception as e:
                log("name translate error", e)
        g = gemini_json('한국어로 쓴 일본 상장사 이름을 일본어 정식 회사명·영문명·4자리 종목코드로 바꿔라. 모르면 빈 문자열. '
                        'JSON {"jp": "", "en": "", "code": ""}. 이름: ' + q, timeout=40) or {}
        if isinstance(g, list): g = g[0] if g and isinstance(g[0], dict) else {}
        if g.get("code") and str(g["code"]).upper() in codes: return str(g["code"]).upper(), []
        alts += [g.get("jp") or "", g.get("en") or ""]
        for alt in [x for x in alts if x]:
            c, cs = resolve("_" + alt)
            if c: return c, []
            if cs: return None, cs
    return None, cands[:8]

def label_of(code):
    for c, jp, en, ko in name_index():
        if c == code: return f"{en or ko or jp}({c})"
    return code

def analyze_code(code):
    """스크리닝과 상관없이 최근 분기 실적 분석 재료(analyze와 같은 모양) + 게시용 row"""
    kb = kb_finance(code)
    if not kb["q"]: raise RuntimeError("가부탄 분기 실적이 없습니다")
    last = kb["q"][-1]
    ann = (last.get("ann") or "").replace("-", "")
    row, rows, r, qual = None, [], None, ""
    if ann and (now().date() - dt.date(int(ann[:4]), int(ann[4:6]), int(ann[6:]))).days <= 30:
        try:
            rows = scan_tdnet(ann)
            row = next((x for x in rows if x["code"] == code and is_tanshin(x)), None)
            if row: r, qual = read_tanshin(row)
        except Exception as e:
            log("tdnet lookup error", code, e)
    if not r:  # TDnet 보관 기간(약 1개월)이 지났으면 가부탄 표로 누적·가이던스 구성
        fy = [y for y in kb["y"] if not y["fc"]]
        fem = int((fy[-1] if fy else kb["y"][-1])["end"][5:7]) if kb["y"] else int(last["end"][5:7])
        end = last["end"]; k = (int(end[5:7]) - fem - 1) % 12 // 3 + 1
        start = ym_add(end, -3 * k + 1)
        cur = [q for q in kb["q"] if start <= q["start"] and q["end"] <= end]
        fcy = next((y for y in kb["y"] if y["fc"]), None)
        r = {"q": k, "start": start + "-01", "end": end + "-28", "scope": "",
             "fc_end": (fcy["end"] + "-28") if fcy else None,
             "fc": {m: (fcy[m] * 1e6 if fcy and fcy.get(m) is not None else None) for m in ("rev", "op", "ni")} if fcy else None,
             "eps": None}
        for m in ("rev", "op", "ni"):
            r[m] = sum(q[m] for q in cur) * 1e6 if cur and all(q.get(m) is not None for q in cur) else None
        if r["fc"] and fcy and fcy.get("eps"): r["fc"]["eps"] = fcy["eps"]
        row = {"code": code, "d": ann or now().strftime("%Y%m%d"), "t": "", "pdf": None, "xbrl": None}
    series = build_series(kb["q"], r)
    i = len(series) - 1
    ok, hit, why = judge(series, i)
    sector = kb["sector"]
    if sector in EXCL_SECTORS: ok, why = False, f"X4 제외 업종({SECTOR_KO.get(sector, sector)})"
    if not ok and not why: why = "R1~R4 미충족"
    cap, profile = kb_profile(code)
    if cap is None and r.get("shares"):
        try: cap = r["shares"] * naver_daily(f"{code}.T", days=10)[-1]["c"] / 1e8
        except Exception: pass
    if ok and cap is not None and cap < RULES["X5_min_cap_oku"]: ok, why = False, f"X5 시총 {cap:,.0f}억 엔"
    jp = next((x[1] for x in name_index() if x[0] == code), code)
    a = {"code": code, "name": jp, "sector": sector, "pass": ok, "hit": hit, "why": why if not ok else "", "series": series, "r": r,
         "cap": cap, "profile": profile, "qual": qual, "ondemand": True, "prev_ok": judge(series, i - 1)[0] if i >= 2 else False}
    if rows:
        rv = find_revision(rows, code)
        if rv:
            rv = {"rev": (rv["rows"]["rev"][0] / 1e6 if rv["rows"]["rev"][0] else None, rv["rows"]["rev"][1] / 1e6 if rv["rows"]["rev"][1] else None),
                  "op": (rv["rows"]["op"][0] / 1e6 if rv["rows"]["op"][0] is not None else None, rv["rows"]["op"][1] / 1e6 if rv["rows"]["op"][1] is not None else None)}
        else:
            rv = find_revision_pdf(rows, code)
        a.update(rev=rv, mat=find_material(rows, code))
    if "name" not in row: row["name"] = jp
    return a, row

HELP_TXT = ("일본 실적 스크리닝 봇 사용법\n"
            "• 종목 코드(예: 6857)나 이름(예: 어드반테스트, 화낙)을 보내면 최근 분기 실적 카드를 이 대화로 보내드립니다.\n"
            "• /add 6857 7203 — 관심 종목 등록: 결산단신이 나오면 스크리닝 통과 여부와 상관없이 카드를 이 대화로 보내드립니다.\n"
            "• /del 6857 — 관심 종목 삭제 · /list — 내 관심 종목 보기\n"
            "• 카드의 📄 리포트·🗣 IR·Q&A 버튼으로 상세 자료도 받을 수 있습니다.\n"
            "스크리닝 기준을 통과한 종목은 지금처럼 채널에 올라갑니다.")


def user_cmd(st, chat, text):
    """개인 대화 명령: /add /del /list (사용자마다 관심 종목 따로 보관)"""
    m = re.match(r"^/(add|del|list|추가|삭제|목록)\b(.*)$", text.strip(), re.I)
    if not m: return False
    cmd, rest = m.group(1).lower(), m.group(2)
    subs = st.setdefault("subs", {}); uid = str(chat["id"]); mine = subs.setdefault(uid, [])
    codes = []
    for tok in re.split(r"[,\s]+", rest.strip()):
        if not tok: continue
        try: c, _ = resolve(tok)
        except Exception: c = None
        if c: codes.append(c)
    if cmd in ("add", "추가"):
        new = [c for c in codes if c not in mine]
        mine += new
        msg = (f"추가: {', '.join(label_of(c) for c in new)}" if new else "추가할 종목을 찾지 못했습니다. 예) /add 6857 7203") + f"\n(관심 종목 {len(mine)}개)"
    elif cmd in ("del", "삭제"):
        gone = [c for c in codes if c in mine]
        for c in gone: mine.remove(c)
        msg = (f"삭제: {', '.join(label_of(c) for c in gone)}" if gone else "목록에 없는 종목입니다.") + f"\n(관심 종목 {len(mine)}개)"
    else:
        msg = f"관심 종목 {len(mine)}개\n" + ("\n".join(label_of(c) for c in mine) if mine else "(없음) /add 6857 처럼 추가하세요.")
    if not mine: subs.pop(uid, None)
    tg("sendMessage", {"chat_id": chat["id"], "text": msg})
    return True

def notify_watchers(st, rows):
    """관심 종목으로 등록한 사용자에게 결산단신 카드를 개인 대화로(스크리닝 통과 여부 무관)"""
    subs = st.get("subs") or {}
    if not subs: return
    sent = st.setdefault("watch_sent", {})
    for r in rows:
        for uid, codes in subs.items():
            if r["code"] not in codes: continue
            key = f"{uid}:{r['id']}"
            if key in sent: continue
            sent[key] = r["d"]
            st.setdefault("ask_q", []).append({"code": r["code"], "chat": int(uid) if uid.lstrip("-").isdigit() else uid, "ts": time.time(), "watch": True})
    if len(sent) > 5000:
        for k in sorted(sent, key=sent.get)[:1000]: sent.pop(k)

def handle_text(st, chat, text):
    """채널 글·봇 개인 메시지로 온 종목명/코드 → 요청 분석 대기열"""
    text = (text or "").strip()
    if not text or len(text) > 30 or "\n" in text: return
    if chat.get("type") == "channel" and str(chat.get("id")) != str(CHANNEL): return
    if chat.get("type") == "private":
        if st.get("owner") is None: st["owner"] = chat["id"]
        if user_cmd(st, chat, text): return
    if text.startswith("/start") or text.startswith("/help"):
        tg("sendMessage", {"chat_id": chat["id"], "text": HELP_TXT}); return
    try:
        code, cands = resolve(text)
    except Exception as e:
        log("resolve error", e); return
    if code:
        recent = st.setdefault("ask_recent", {})
        rk = f"{chat['id']}:{code}"
        if time.time() - recent.get(rk, 0) < 180 or any(j["code"] == code and str(j["chat"]) == str(chat["id"]) for j in st.get("ask_q", [])): return  # 같은 사람의 같은 요청 중복
        recent[rk] = time.time()
        st.setdefault("ask_q", []).append({"code": code, "chat": chat["id"], "ts": time.time()})
        tg("sendMessage", {"chat_id": chat["id"], "text": f"{label_of(code)} 최근 분기 실적을 분석하고 있습니다(1~2분)."})
    elif cands:
        tg("sendMessage", {"chat_id": chat["id"], "text": "여러 종목이 맞습니다. 코드로 보내 주세요:\n" + "\n".join(label_of(c) for c in cands)})
    elif re.search(r"[0-9A-Za-z가-힣ぁ-んァ-ン一-龥]", text) and (chat.get("type") == "private" or re.fullmatch(r"[0-9][0-9A-Za-z]{3}", text)):
        tg("sendMessage", {"chat_id": chat["id"], "text": f"'{text}'에 맞는 상장 종목을 찾지 못했습니다. 종목 코드로 보내 주세요."})

def do_asks(st):
    q = st.get("ask_q") or []
    while q:
        job = q.pop(0)
        code = job["code"]
        try:
            a, row = analyze_code(code)
            cid = f"q{code}{int(time.time()) % 100000000}"
            personal = job.get("watch") or str(job["chat"]) not in (str(CHANNEL), str(st.get("owner")))
            msg = make_card(st, cid, a, row, job["chat"] if personal else None)  # 다른 사용자 요청은 그 사람 개인 대화로
            if msg is not None and job.get("report"):
                st.setdefault("rep_q", []).append(cid)
                if job.get("ir"): st.setdefault("ir_q", []).append(cid)
            if msg is None:
                tg("sendMessage", {"chat_id": job["chat"], "text": f"{label_of(code)} 카드 게시에 실패했습니다."})
            elif str(job["chat"]) != str(CHANNEL) and not personal:
                tg("sendMessage", {"chat_id": job["chat"], "text": f"{label_of(code)} 카드를 채널에 올렸습니다."})
            log("요청 분석", code, "통과" if a["pass"] else a["why"])
        except Exception as e:
            traceback.print_exc()
            tg("sendMessage", {"chat_id": job["chat"], "text": f"{label_of(code)} 분석 실패: {str(e)[:120]}"})

# ───────────────────────── 메인 처리 ─────────────────────────
def is_tanshin(r):
    t = r["title"]
    return ("決算短信" in t and r.get("xbrl") and "訂正" not in t and "REIT" not in t.upper() and "ETF" not in t.upper()
            and not re.search(r"投資法人|ファンド", r["name"]))

def post_selected(st, spec):
    """지정한 날짜·종목의 결산단신만 판별해 통과하면 카드 게시(기준 변경 등으로 놓친 카드 보충용, 이미 있는 카드는 건너뜀)"""
    done = {(c.get("d"), c.get("code")) for c in st.get("cards", {}).values()}
    for part in spec.split(";"):
        day, _, codes = part.partition(":")
        day = day.strip(); want = [c.strip().upper() for c in codes.split(",") if c.strip()]
        if not re.fullmatch(r"\d{8}", day) or not want: continue
        rows = scan_tdnet(day)
        for row in reversed([r for r in rows if is_tanshin(r) and r["code"] in want]):
            if (day, row["code"]) in done: log("이미 카드 있음", day, row["code"]); continue
            try:
                a = analyze(row, rows, st)
            except Exception:
                traceback.print_exc(); continue
            if not a or not a["pass"]: log("탈락", row["code"], (a or {}).get("why")); continue
            cid = re.sub(r"\D", "", row["id"])[-12:]
            st.setdefault("seen", {})[row["id"]] = day
            if make_card(st, cid, a, row) is not None or DRY:
                done.add((day, row["code"])); log("보충 카드", day, row["code"], row["name"])
            save(st); time.sleep(3.5)

def process_day(st, day, test=False):
    rows = scan_tdnet(day)
    seen = st.setdefault("seen", {})
    if not test and not DRY:
        try: qa_followup(st, day, rows)
        except Exception: traceback.print_exc()
    todo = [r for r in rows if is_tanshin(r) and (test or r["id"] not in seen)]
    if not test:
        try: notify_watchers(st, todo)
        except Exception: traceback.print_exc()
    if not todo: return 0
    log(day, "결산단신", len(todo))
    passed = 0
    for row in reversed(todo):  # 오래된 순
        cid = re.sub(r"\D", "", row["id"])[-12:]
        try:
            a = analyze(row, rows, st)
        except Exception as e:
            log("analyze error", row["code"], e); traceback.print_exc()
            n = st.setdefault("retry", {}).get(row["id"], 0) + 1
            st["retry"][row["id"]] = n
            if n >= 3: seen[row["id"]] = day
            continue
        seen[row["id"]] = day
        if not a: continue
        st.setdefault("log", []).append([day, row["code"], row["name"], a["pass"], " ".join(a["hit"]), a["why"]])
        if not a["pass"]:
            log("탈락", row["code"], row["name"], a["why"] or "기준 미달"); continue
        passed += 1
        log("통과", row["code"], row["name"], a["hit"])
        if make_card(st, cid, a, row) is None and not DRY: continue
        time.sleep(3.5)
    return passed

def make_card(st, cid, a, row, chat=None):
    """분석 결과 → Gemini 요약 → 카드(차트+글) 게시 → 상태에 저장. 게시 실패면 None"""
    if True:
        fem = fy_end_month_of(a["r"])
        m = metrics(a["series"], len(a["series"]) - 1)
        nums = (f"이번 분기 매출 {oku(m['q'].get('rev'))}엔({yoy_txt(m['rev_yoy'], m['rev_st'])}), 영업이익 {oku(m['q'].get('op'))}엔"
                f"({yoy_txt(m['op_yoy'], m['op_st'])}), 순이익 {oku(m['q'].get('ni'))}엔({yoy_txt(m['ni_yoy'], m['ni_st'])})")
        mat_pdf, mat_url, mat_src = find_mat_pdf(a, row)
        ai = ai_card({"name": a["name"], "code": a["code"], "sector": a["sector"], "profile": a.get("profile", ""), "nums": nums, "qual": a.get("qual", "")}, mat_pdf)
        if ai is not None and mat_pdf: ai["_src"] = mat_src
        cap = card_text(a, ai, row)
        buf = io.BytesIO()
        chart_name = name_of(a["code"], a["name"], ai) + f"({a['code']})"
        card_chart(chart_name, a["series"], fem, buf)
        msg = post_card(st, cid, cap, buf.getvalue(), chat)
        if msg is None and not DRY: return None
        st.setdefault("cards", {})[cid] = {
            "code": a["code"], "name": a["name"], "name_ko": name_of(a["code"], a["name"], ai), "d": row["d"], "t": row.get("t") or "15:30", "msg": msg,
            "caption": cap, "ts": time.time(), "sector": a["sector"], "cap": a.get("cap"), "pdf": row.get("pdf"), "xbrl": row.get("xbrl"),
            "mat": mat_url, "mat_src": mat_src, "fem": fem, "series": a["series"][-8:], "hit": a["hit"],
            "r": {k: a["r"].get(k) for k in ("q", "start", "end", "fc", "fc_end", "rev", "op", "ni", "scope")},
            "ai": ai, "profile": a.get("profile", "")[:600], "qual": a.get("qual", "")[:8000], "chat": str(chat or CHANNEL)}
        return msg

def cleanup(st):
    cut = (now() - dt.timedelta(days=5)).strftime("%Y%m%d")
    st["seen"] = {k: v for k, v in st.get("seen", {}).items() if v >= cut}
    old = time.time() - 86400 * 30
    st["cards"] = {k: v for k, v in st.get("cards", {}).items() if v.get("ts", 0) > old}
    st["log"] = st.get("log", [])[-3000:]

def main():
    t0 = time.time()
    st = load()
    if not DRY and not TOKEN:
        log("EARN_TOKEN이 없습니다. 저장소 Settings → Secrets에 봇 토큰을 등록해 주세요."); return
    if not DRY and not find_channel(st):
        log("채널을 아직 못 찾았습니다. 채널에 봇을 관리자로 추가하고 글을 하나 올려 주세요."); save(st); return
    if TEST_REPORT:
        for c in re.split(r"[,\s]+", TEST_REPORT.strip()):
            if c: st.setdefault("ask_q", []).append({"code": c.upper(), "chat": CHANNEL, "ts": time.time(), "report": True, "ir": True})
    if TEST_DAY and ":" in TEST_DAY:  # 놓친 카드 보충: '20261015:6532,7730;20261022:6146'
        post_selected(st, TEST_DAY); save(st)
        if DRY: return
    elif TEST_DAY:
        n = process_day(st, TEST_DAY, test=True)
        log("test done, 통과", n); save(st)
        if DRY: return
    if not st.get("init"):
        # 처음 실행: 이미 지난 공시로 카드를 쏟아내지 않도록 오늘·어제 결산은 본 것으로 처리
        for back in (0, 1):
            d = (now() - dt.timedelta(days=back)).strftime("%Y%m%d")
            for r in scan_tdnet(d):
                if is_tanshin(r): st.setdefault("seen", {})[r["id"]] = d
        st["init"] = now().isoformat(); save(st)
        tg("sendMessage", {"chat_id": CHANNEL, "text": "실적 스크리닝 봇이 연결됐습니다. 이제부터 올라오는 결산단신을 판별해 기준을 통과한 종목만 카드로 보냅니다."})
    if not st.get("guide_v1") and not DRY:  # 채널 고정 공지: 봇 아이디 + 사용법(한 번)
        try:
            me = (tg("getMe").get("result") or {}).get("username", "")
            txt = ("📌 <b>실적 스크리닝 채널 사용법</b>\n\n"
                   "이 채널에는 일본 상장사 결산단신 중 스크리닝 기준(매출 성장·이익 성장·성장 가속·턴어라운드, 시총 300억 엔 이상)을 통과한 종목 카드가 자동으로 올라옵니다.\n\n"
                   f"<b>내 종목만 따로 받기</b> — 봇 @{me} 과 1:1 대화를 열고(채널 구독자는 채널에 글을 쓸 수 없습니다)\n"
                   "• 종목 코드나 이름 보내기(예: 6857, 어드반테스트) → 최근 분기 실적 카드를 개인 대화로\n"
                   "• /add 6857 7203 → 관심 종목 등록. 결산단신이 나오면 스크리닝 통과 여부와 상관없이 개인 대화로 카드 발송\n"
                   "• /del 6857 삭제 · /list 목록 · /start 사용법\n\n"
                   "카드의 📄 리포트(사업부·지역별 상세 분석, note·야후 게시판·증권사 레이팅 등 시장 반응), 🗣 IR·Q&A(설명회 질의응답 요약) 버튼은 누구나 누를 수 있습니다.\n"
                   "<i>투자 자문이 아닌 공시 정리입니다.</i>")
            r = tg("sendMessage", {"chat_id": CHANNEL, "text": txt, "parse_mode": "HTML", "disable_web_page_preview": True})
            mid = (r.get("result") or {}).get("message_id")
            if mid:
                tg("pinChatMessage", {"chat_id": CHANNEL, "message_id": mid, "disable_notification": True})
                st["guide_v1"] = mid; save(st); log("guide posted", me)
        except Exception:
            traceback.print_exc()
    if not st.get("kb_v2") and not DRY:  # 기존 카드의 🗑 삭제 버튼 없애기(한 번)
        for cid, c in list(st.get("cards", {}).items()):
            if c.get("msg") and not c.get("deleted"):
                tg("editMessageReplyMarkup", {"chat_id": c.get("chat") or CHANNEL, "message_id": c["msg"], "reply_markup": keyboard(cid, c)}); time.sleep(0.5)
        st["kb_v2"] = True; save(st)
    last_scan = 0
    while time.time() - t0 < RUN_SECONDS:
        try:
            poll_buttons(st, wait=0)
            do_reports(st)
            if time.time() - last_scan > 90:
                last_scan = time.time()
                for back in (1, 0):
                    process_day(st, (now() - dt.timedelta(days=back)).strftime("%Y%m%d"))
                update_reactions(st)
                cleanup(st); save(st)
        except Exception:
            traceback.print_exc()
        poll_buttons(st, wait=25)
        save(st)
    save(st)

if __name__ == "__main__":
    main()
