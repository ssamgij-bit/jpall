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
    "R5_rev_only_yoy": 15.0,  # R5 매출 고성장: 이익과 상관없이 분기 매출 YoY ≥ 15%
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

def sector_norm(s):
    """업종 이름을 도쿄증권거래소 33업종 공식 이름으로(가부탄은 '電気・ガス'처럼 '業'을 빼고 표기 → 번역·제외 업종 판정이 빠지던 문제)"""
    s = nfkc(s or "").replace("、", "・").strip()
    if s in SECTOR_KO: return s
    for k in SECTOR_KO:
        kk = nfkc(k).replace("、", "・")
        if kk == s or kk.rstrip("業") == s.rstrip("業"): return k
    return s

def now(): return dt.datetime.now(JST)
def log(*a): print(now().strftime("%H:%M:%S"), *a, flush=True)
# 상태 파일은 공개 저장소에 올라가므로 사용자 정보(텔레그램 계정 번호·관심 종목·요청 기록)는 암호화해서 저장
PRIVATE_KEYS = ("owner", "subs", "watch_sent", "ask_q", "ask_recent", "jps_codes", "jps_map", "users", "blocked", "rep_q", "ir_q", "pending_start", "prefs", "alerts", "ai_wait")
def _skey():
    import hashlib
    return hashlib.sha256(("earn-state-v1:" + (os.environ.get("STATE_KEY") or TOKEN or "")).encode()).digest()
def _stream(key, nonce, n):
    import hashlib
    out, i = bytearray(), 0
    while len(out) < n:
        out += hashlib.sha256(key + nonce + i.to_bytes(8, "big")).digest(); i += 1
    return bytes(out[:n])
def _enc(obj):
    import hmac, hashlib
    raw = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode()
    nonce = os.urandom(16); k = _skey()
    ct = bytes(a ^ b for a, b in zip(raw, _stream(k, nonce, len(raw))))
    mac = hmac.new(k, nonce + ct, hashlib.sha256).digest()
    return base64.b64encode(nonce + mac + ct).decode()
def _dec(s):
    import hmac, hashlib
    b = base64.b64decode(s); nonce, mac, ct = b[:16], b[16:48], b[48:]; k = _skey()
    if not hmac.compare_digest(mac, hmac.new(k, nonce + ct, hashlib.sha256).digest()): raise ValueError("state key mismatch")
    return json.loads(bytes(a ^ b for a, b in zip(ct, _stream(k, nonce, len(ct)))).decode())
def load():
    try: st = json.load(open(os.path.join(STATE_DIR, "earn.json")))
    except Exception: return {}
    if st.get("_enc"):
        try:
            priv = _dec(st.pop("_enc"))
            for cid, pv in (priv.pop("card_priv", {}) or {}).items():
                if cid in st.get("cards", {}): st["cards"][cid].update(pv)
            st.update(priv)
        except Exception as e:
            log("state decrypt error", e); st.pop("_enc", None)
    return st
def save(st):
    os.makedirs(STATE_DIR, exist_ok=True)
    out = {k: v for k, v in st.items() if k not in PRIVATE_KEYS}
    priv = {k: st[k] for k in PRIVATE_KEYS if k in st}
    cp, cards = {}, {}
    for cid, c in (st.get("cards") or {}).items():
        pv = {k: c[k] for k in ("chat", "copies", "qa_to") if c.get(k) and not (k == "chat" and str(c[k]) == str(CHANNEL))}
        if pv: cp[cid] = pv
        cards[cid] = {k: v for k, v in c.items() if k not in ("chat", "copies", "qa_to")}
    if "cards" in st: out["cards"] = cards
    priv["card_priv"] = cp
    out["_enc"] = _enc(priv)
    json.dump(out, open(os.path.join(STATE_DIR, "earn.json"), "w"), ensure_ascii=False, separators=(",", ":"))
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
    out["sector"] = sector_norm(text_of(g.group(1))) if g else ""
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
    if rg is not None and rg >= RULES["R5_rev_only_yoy"] and not (r1 and r2): hit.append("매출 고성장")
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
class _SafePlt:
    """pyplot 대신 쓰는 얇은 감싸개: 그림을 pyplot 전역 상태 없이 만들어 여러 작업 스레드가 동시에 그려도 안전"""
    def __init__(self, plt): self._p = plt
    def subplots(self, nrows=1, ncols=1, **kw):
        from matplotlib.figure import Figure
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        fkw = {k: kw.pop(k) for k in ("figsize", "dpi", "facecolor") if k in kw}
        fig = Figure(**fkw); FigureCanvasAgg(fig)
        return fig, fig.subplots(nrows, ncols, **kw)
    def close(self, *a): pass
    def __getattr__(self, n): return getattr(self._p, n)

import threading as _th
_PLT = {"p": None}; _PLT_LOCK = _th.Lock()
def _plt():
    with _PLT_LOCK:
        if _PLT["p"] is None: _PLT["p"] = _SafePlt(_plt_init())
        return _PLT["p"]

def _plt_init():
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
    v = a.get("val") or {}
    if v.get("pbr"): per += f" · PBR <b>{v['pbr']:.2f}배</b>"
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
    if "매출 고성장" in a["hit"]: tags.append("매출 고성장")
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
    if v.get("cons_rev") and q.get("rev"):
        cr = v["cons_rev"] / 1e6
        L.append(f" 매출 컨센서스 {oku(cr)} 대비 <b>{(q['rev'] / cr - 1) * 100:+.1f}%</b> (Yahoo 애널리스트 {v.get('cons_n') or '?'}명 평균)")
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
LAST_ERR = {"d": ""}
BLOCK_RE = re.compile(r"blocked by the user|user is deactivated|chat not found|bot can't initiate conversation", re.I)
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
            LAST_ERR["d"] = b
            return {"ok": False, "description": b}
        except Exception as e:
            log("telegram", method, e); time.sleep(3)
    return {"ok": False}

def keyboard(cid, card=None):
    card = card or {}
    row = [{"text": "📄 리포트", "callback_data": f"rep:{cid}"}, {"text": "🗣 IR·Q&A", "callback_data": f"ir:{cid}"}]
    return json.dumps({"inline_keyboard": [row]})

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
GATEWAY_URL = os.environ.get("GATEWAY_URL", "").strip().rstrip("/")   # 즉시 응답 관문(Cloudflare Worker). "off" 면 예전 방식
if GATEWAY_URL.lower() in ("off", "none", "0"): GATEWAY_URL = ""
def _gw_key():
    import hashlib
    return os.environ.get("GATEWAY_KEY") or hashlib.sha256(("earn-gateway-v1:" + TOKEN).encode()).hexdigest()
GATEWAY_KEY = _gw_key() if TOKEN else ""
ALLOWED = ["callback_query", "my_chat_member", "channel_post", "message"]
GW_ERRS = [0]
def gateway_off(st, why):
    if st.get("hook") and tg("deleteWebhook", {}).get("ok"):
        st.pop("hook", None); log("webhook removed:", why)
def gateway_setup(st):
    """관문에 봇 토큰·키를 등록(관문은 getMe 로 진짜 봇인지 확인)하고 텔레그램 웹훅을 관문으로. 실패하면 예전 방식(getUpdates)"""
    if DRY or not TOKEN: return
    if not GATEWAY_URL: return gateway_off(st, "no gateway")
    try:
        r = http(GATEWAY_URL + "/setup", timeout=20, data=json.dumps({"token": TOKEN, "key": GATEWAY_KEY}).encode(), headers={"content-type": "application/json"}).decode()
        if r.strip() != "ok": raise ValueError(r[:80])
    except Exception as e:
        log("gateway setup error", e); return gateway_off(st, "setup failed")
    if st.get("hook") != GATEWAY_URL:
        r = tg("setWebhook", {"url": GATEWAY_URL + "/tg", "secret_token": GATEWAY_KEY, "allowed_updates": ALLOWED, "drop_pending_updates": False})
        if r.get("ok"): st["hook"] = GATEWAY_URL; log("webhook set", GATEWAY_URL)
        else: log("setWebhook failed", r)

def fetch_updates(st, wait):
    if GATEWAY_URL and st.get("hook"):
        try:
            res = json.loads(http(GATEWAY_URL + "/updates", timeout=20, headers={"x-key": GATEWAY_KEY}))
            GW_ERRS[0] = 0
        except Exception as e:
            log("gateway error", e); res = []; GW_ERRS[0] += 1
            if GW_ERRS[0] >= 5: gateway_off(st, "gateway unreachable")   # 관문이 계속 안 되면 예전 방식으로(다음 실행 때 다시 시도)
        if not res and wait: time.sleep(min(wait, 6))   # 관문이 즉시 답하므로 6초마다만 확인
        return res
    return tg("getUpdates", {"offset": st.get("off", 0), "timeout": wait, "allowed_updates": ALLOWED}, timeout=wait + 20).get("result") or []

def poll_buttons(st, wait=0):
    if DRY or not TOKEN: time.sleep(wait); return
    res = fetch_updates(st, wait)
    if GATEWAY_URL and st.get("hook") and res:
        try: http(GATEWAY_URL + "/updates", timeout=20, data=json.dumps([u["update_id"] for u in res]).encode(), headers={"x-key": GATEWAY_KEY, "content-type": "application/json"})
        except Exception as e: log("gateway ack error", e)
    for u in res:
        st["off"] = u["update_id"] + 1
        mcm = u.get("my_chat_member") or {}
        if (mcm.get("chat") or {}).get("type") in ("group", "supergroup") and (mcm.get("new_chat_member") or {}).get("status") in ("member", "administrator"):
            tg("leaveChat", {"chat_id": mcm["chat"]["id"]}); log("left group", mcm["chat"].get("id")); continue  # 단체방은 지원하지 않음
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
            acked = u.get("_acked")   # 관문(Cloudflare)이 이미 답한 버튼
            if acked == "link": continue   # 시작 화면을 연 사람: /start 로 다시 들어옴
            if not card:
                if not acked: tg("answerCallbackQuery", {"callback_query_id": cq["id"], "text": "카드 정보가 만료됐습니다."})
                continue
            uid = (cq.get("from") or {}).get("id")
            mchat = (msg.get("chat") or {})
            if mchat.get("type") == "private":
                dest, reply = mchat["id"], msg.get("message_id")
            else:  # 채널 카드: 결과는 누른 사람 개인 대화로
                dest, reply = uid, None
                if not acked and not tg("sendChatAction", {"chat_id": uid, "action": "typing"}).get("ok"):  # 봇과 대화를 시작하지 않은 사람 → 시작 링크
                    me = bot_name(st)
                    tg("answerCallbackQuery", {"callback_query_id": cq["id"], "url": f"https://t.me/{me}?start={act}_{cid}"}); continue
            enqueue_out(st, act, cid, dest, reply); log("버튼", act, (card or {}).get("code"), dest, "관문 응답" if acked else "")
            if not acked: tg("answerCallbackQuery", {"callback_query_id": cq["id"], "text": ("리포트" if act == "rep" else "IR·Q&A 요약") + "를 개인 대화로 보내드립니다(처음 만들 땐 1~3분)."})

def bot_name(st):
    if not st.get("me"):
        st["me"] = (tg("getMe").get("result") or {}).get("username", "")
    return st["me"]

def enqueue_out(st, act, cid, dest, reply=None):
    key = "rep_q" if act == "rep" else "ir_q"
    q = st.setdefault(key, [])
    if not any(isinstance(x, dict) and x["cid"] == cid and str(x["chat"]) == str(dest) for x in q):
        q.append({"cid": cid, "chat": dest, "reply": reply})
    bump(st, "rep" if act == "rep" else "ir")

def _job(x, st):
    """대기열 항목(예전 형식은 카드 번호 문자열) → (카드 번호, 받는 곳, 답장할 메시지)"""
    if isinstance(x, dict): return x["cid"], x["chat"], x.get("reply")
    c = st.get("cards", {}).get(x) or {}
    return x, c.get("chat") or CHANNEL, c.get("msg")

def bump(st, k, n=1):
    d = now().strftime("%Y%m%d"); s_ = st.setdefault("stats", {})
    s_.setdefault(d, {}); s_[d][k] = s_[d].get(k, 0) + n
    for old in sorted(s_)[:-14]: s_.pop(old)

def send_texts(card, texts, dest=None, reply=None):
    ok = False
    dest = dest or card.get("chat") or CHANNEL
    if reply is None and dest == (card.get("chat") or CHANNEL): reply = card["msg"]
    for t in texts:
        base = {"chat_id": dest, "disable_web_page_preview": True, **({"reply_to_message_id": reply, "allow_sending_without_reply": True} if reply else {})}
        r = tg("sendMessage", {**base, "text": t, "parse_mode": "HTML"})
        if not r.get("ok"):
            r = tg("sendMessage", {**base, "text": re.sub(r"<[^>]+>", "", t)})
        ok = ok or bool(r.get("ok")); time.sleep(1)
    return ok

def send_qa(card, texts):
    """Q&A가 뒤늦게 올라오면: IR 요약을 받아 간 사람들에게 다시 보내고, 이후 요청부터는 Q&A 포함본을 씀"""
    card["ir_texts"] = [texts[0].split("\n\n", 1)[-1]] + texts[1:]
    for dest in card.get("qa_to") or [card.get("chat") or CHANNEL]:
        try: send_texts(card, texts, dest)
        except Exception: traceback.print_exc()
    card.pop("qa_watch", None)

def qa_followup(st, day, rows):
    """IR 요약을 보냈는데 Q&A 자료가 없던 종목 → 14일 안에 TDnet에 질의응답이 올라오면 다시 요약해 보냄"""
    import earn_ir
    site_checks = [0]
    for cid, card in st.get("cards", {}).items():
        w = card.get("qa_watch")
        if not w or card.get("deleted"): continue
        if time.time() - w > 86400 * 14: card.pop("qa_watch", None); continue
        hits = [r for r in rows if r["code"] == card["code"] and earn_ir.QA_RE.search(r["title"]) and r["id"] not in card.get("qa_seen", [])]
        if not hits:  # TDnet에 없으면 6시간마다 회사 IR 홈페이지·로그미도 다시 확인(한 번에 2종목까지: 브라우저로 여는 확인은 시간이 걸림)
            if time.time() - card.get("qa_site_ts", 0) < 6 * 3600: continue
            if site_checks[0] >= 2: continue
            site_checks[0] += 1
            card["qa_site_ts"] = time.time()
            try:
                if not earn_ir.other_qa(card): continue
                texts, has_qa = earn_ir.build(card, qa_rows=[])
                if texts and has_qa:
                    texts[0] = "📞 <b>회사 IR 홈페이지에 질의응답 자료가 올라와 Q&amp;A를 추가했습니다</b>\n\n" + texts[0]
                    send_qa(card, texts)
            except Exception:
                traceback.print_exc()
            continue
        card.setdefault("qa_seen", []).extend(r["id"] for r in hits)
        try:
            texts, has_qa = earn_ir.build(card, qa_rows=hits)
            if texts and has_qa:
                texts[0] = "📞 <b>질의응답 자료가 올라와 Q&amp;A를 추가했습니다</b>\n\n" + texts[0]
                send_qa(card, texts)
        except Exception:
            traceback.print_exc()

# ── AI 무료 한도: 429(한도 초과)가 난 모델은 2시간 쉬고, 모든 모델이 막히면 리포트·IR을 미뤘다가 풀리면 자동으로 만듦
import jpall as _jp
AI_OUT = _jp.__dict__.setdefault("EARN_AI_OUT", {})   # "키 해시:모델" → 다시 써 볼 시각. earn_report·earn_ir 가 따로 불러온 earn 과도 같은 기록을 쓰도록 jpall 모듈에 둠
def _kh():
    import hashlib
    import jpall
    return hashlib.sha256((jpall.gem_key() or "").encode()).hexdigest()[:8]
def ai_skip(model): return AI_OUT.get(_kh() + ":" + model, 0) > time.time()
def ai_429(model):
    AI_OUT[_kh() + ":" + model] = time.time() + 2 * 3600
    log("gemini 429 → 2시간 쉼", model, key_label())

def key_label():
    """지금 이 스레드가 쓰는 Gemini 키 이름(값은 기록하지 않음)"""
    import jpall
    k = jpall.gem_key() or ""
    for n, lab in (("GEMINI_API_KEY_3", "보조 키2"), ("GEMINI_API_KEY_2", "보조 키1"), ("GEMINI_API_KEY", "기본 키")):
        if k and os.environ.get(n) == k: return lab
    return "키 미상"

BAD_KEYS = {}   # 거부된 키 이름 → 구글 응답 일부(이번 실행 동안). 주인에게 하루 한 번 알림
def ai_err(e, model, where):
    """Gemini 오류를 기록·분류 → quota(한도) / busy(서버 혼잡·응답 없음) / bad(자료 문제) / key(키 거부) / other.
    분류는 이 작업 스레드의 실패 사유(jpall.AI_TL.fails)에 쌓아 실패 안내에 씀"""
    import jpall
    from jpall import GEMINI_MODELS
    body = ""
    try: body = e.read()[:400].decode("utf-8", "ignore") if hasattr(e, "read") else ""
    except Exception: pass
    s, lab = str(e), key_label()
    if "429" in s: kind = "quota"
    elif re.search(r"timed out|50[0234]|reset|closed", s): kind = "busy"
    elif re.search(r"API_KEY_INVALID|API key not valid|API key expired", body) or "403" in s: kind = "key"
    elif "400" in s: kind = "bad"
    else: kind = "other"
    log(f"gemini {where} error", model, lab, s[:80], re.sub(r"\s+", " ", body)[:160])
    if getattr(jpall.AI_TL, "fails", None) is None: jpall.AI_TL.fails = []
    jpall.AI_TL.fails.append(kind)
    if kind == "key" and lab not in BAD_KEYS:
        BAD_KEYS[lab] = re.sub(r"\s+", " ", body)[:120] or s[:80]
        for m in GEMINI_MODELS: AI_OUT[_kh() + ":" + m] = time.time() + 6 * 3600   # 거부된 키는 6시간 쉼 → 다른 보조 키로
    return kind

def ai_reset():
    import jpall
    jpall.AI_TL.fails = []

def ai_fail_reason():
    import jpall
    f = getattr(jpall.AI_TL, "fails", None) or []
    for k in ("nodoc", "key", "bad", "busy", "quota"):
        if k in f: return k
    return f[-1] if f else None

FAIL = {}   # 종목코드 → 마지막 리포트·IR 실패 사유(작업 스레드에서 기록, 안내 문구에 씀)
FAIL_TXT = {"quota": "AI 무료 한도가 찼습니다. 한도가 풀리면 다시 눌러 주세요.",
            "busy": "구글 AI 서버가 붐벼 응답이 없었습니다. 몇 분 뒤 다시 눌러 주세요.",
            "bad": "결산 자료(PDF)를 AI가 읽지 못했습니다(자료 형식 문제). 기록해 두었습니다.",
            "key": "AI 키가 거부됐습니다(키 확인 필요). 주인에게 알렸습니다.",
            "nodoc": "요약할 결산 자료를 찾지 못했습니다(TDnet·회사 IR 홈페이지 모두 없음).",
            None: "원인을 특정하지 못했습니다. 잠시 뒤 다시 눌러 주세요."}
def ai_out_until(heavy=False):
    """모든 모델이 한도 초과면 가장 먼저 풀리는 시각(아니면 0).
    heavy(리포트·IR: PDF 여러 개를 읽는 큰 요청): 쓸 수 있는 모델이 1개만 남아도 미룸 — 마지막 남은 가벼운 모델은 큰 PDF에서 거의 실패하거나 품질이 낮고, 시도할수록 한도만 더 씀"""
    from jpall import GEMINI_MODELS
    ts = [AI_OUT.get(_kh() + ":" + m, 0) for m in GEMINI_MODELS]
    out = sorted(t for t in ts if t > time.time())
    if not ts or not out: return 0
    if len(out) == len(ts): return out[0]
    if heavy and len(ts) >= 2 and len(ts) - len(out) <= 1: return out[0]
    return 0

def defer_ai(st, act, cid, dest, reply, until):
    w = st.setdefault("ai_wait", [])
    if any(x["act"] == act and x["cid"] == cid and str(x["chat"]) == str(dest) for x in w): return
    w.append({"act": act, "cid": cid, "chat": dest, "reply": reply, "ts": time.time()})
    t = dt.datetime.fromtimestamp(until, JST).strftime("%H:%M")
    what = "리포트" if act == "rep" else "IR·Q&A 요약"
    tg("sendMessage", {"chat_id": dest, "text": f"오늘 AI 무료 한도가 끝나 {what}를 바로 만들 수 없습니다. {t}(한국 시간) 이후 다시 시도해 자동으로 보내드립니다.",
                       **({"reply_to_message_id": reply, "allow_sending_without_reply": True} if reply else {})})
    log("ai deferred", act, cid)

def release_ai(st):
    w = st.get("ai_wait") or []
    w[:] = [x for x in w if time.time() - x.get("ts", 0) < 2 * 86400]
    if not w or ai_out_until(heavy=True): return
    for x in w:
        q = st.setdefault("rep_q" if x["act"] == "rep" else "ir_q", [])
        q.append({"cid": x["cid"], "chat": x["chat"], "reply": x.get("reply")})
    log("ai waits released", len(w)); w.clear()

def _heavy_keys():
    return [k for k in (os.environ.get("GEMINI_API_KEY_2"), os.environ.get("GEMINI_API_KEY_3")) if k]

class _key2:
    """리포트·IR·시장 반응은 보조 Gemini 키(GEMINI_API_KEY_2, _3)로 — 카드용 한도와 분리(이 스레드에만 적용).
    보조 키가 여럿이면 한도가 남은 키를 고름(앞의 키가 막히면 다음 키)"""
    def __enter__(self):
        import jpall
        self.k = getattr(jpall.AI_TL, "key", None)
        ks = _heavy_keys()
        if ks:
            pick = ks[0]
            for k in ks:
                jpall.AI_TL.key = k
                if not ai_out_until(heavy=True): pick = k; break
            jpall.AI_TL.key = pick
            if pick != ks[0]: log("보조 키 전환", key_label())   # 첫 보조 키가 한도·거부로 쉬는 중
    def __exit__(self, *a):
        import jpall
        jpall.AI_TL.key = self.k

# ── 작업 스레드: 오래 걸리는 일(종목 분석·카드 준비·리포트·IR 요약)은 따로 돌리고, 메인 루프는 계속 요청·버튼을 받음
import concurrent.futures as _cf, copy as _copy
POOL = _cf.ThreadPoolExecutor(max_workers=5)
JOBS = []   # {"kind", "fut", ...}
MAX_ASK, MAX_HEAVY = 2, 3   # 종목 요청(카드)은 리포트·IR과 따로 2자리 → 리포트가 몰려도 종목 요청은 바로 처리

def _bg(fn, *a, key2=False):
    """작업 스레드에서 실행. 예외는 결과로 돌려줌"""
    try:
        if key2:
            with _key2(): return ("ok", fn(*a))
        return ("ok", fn(*a))
    except Exception as e:
        traceback.print_exc()
        return ("err", e)

def _submit(kind, fn, *a, key2=False, **meta):
    JOBS.append({"kind": kind, "fut": POOL.submit(_bg, fn, *a, key2=key2), "t0": time.time(), **meta})

def _merge(card, cp):
    for k, v in cp.items():
        if not k.startswith("_"): card[k] = v

def _rep_build(cp):
    import earn_report
    ai_reset()
    fn, blob = earn_report.build(cp)
    ok_ai = cp.pop("_ai_ok", True)
    if not ok_ai: FAIL[cp["code"]] = ai_fail_reason()
    log("리포트 AI", cp["code"], key_label(), "성공" if ok_ai else f"실패({ai_fail_reason()})")
    return fn, blob, ok_ai

def _ir_build(cp):
    import earn_ir
    ai_reset()
    r = earn_ir.build(cp)
    if not r or not r[0]: FAIL[cp["code"]] = ai_fail_reason()
    log("IR AI", cp["code"], key_label(), "성공" if r and r[0] else f"실패({ai_fail_reason()})")
    return r

def _ir_refresh(cp):
    """예전 요약에 Q&A가 없던 카드: 회사 IR 홈페이지 등에 질의응답이 새로 올라왔으면 다시 만들고, 없으면 예전 요약"""
    import earn_ir
    if not earn_ir.other_qa(cp): return "cached"
    ai_reset()
    r = earn_ir.build(cp, qa_rows=[])
    if not r or not r[0]: FAIL[cp["code"]] = ai_fail_reason()
    return r

# ── japan stock 봇(Cloudflare) 공시 요약 대행: Cloudflare 무료 플랜 CPU 한도(실행당 약 30ms) 안에서 PDF를 읽고 요약하기 어려워 여기서 대신 함
JPS_URL = "https://jp-disclosure-bot.ssamgij.workers.dev"
JPS_SYS = """당신은 한국 자산운용사 애널리스트를 돕는 리서치 보조입니다. 일본어로 된 공시 본문을 읽고 한국어로 요약합니다.
출력 규칙:
- 반드시 한국어로만 쓴다. 일본어 문장·가나를 남기지 않는다. 회사명·인명도 한국어 음역 또는 영문으로.
- 정확히 3줄. 각 줄은 70자 이내의 한 문장.
- 첫 줄에는 가장 중요한 사실(무엇을, 얼마나, 언제).
- 숫자·금액·비율·날짜는 원문 그대로(億円→억엔, 百万円→백만엔, 前年同期比→전년 동기 대비, 自己株式→자사주).
- 원문에 없는 해석·전망·투자 판단은 쓰지 않는다.
- 본문이 비었거나 의미가 없으면 lines를 빈 목록으로.
출력은 JSON만: {{"lines": ["첫 줄", "둘째 줄", "셋째 줄"]}}

[공시] 제목: {title}
본문:
{body}"""
_JPS = {"t": 0, "run": None}
def _jps_job(key):
    from jpall import pdf_text, MON_MODELS
    try:
        items = json.loads(http(JPS_URL + "/pend", timeout=20, headers={"x-watch-key": key}))
    except Exception as e:
        log("jps pend error", str(e)[:80]); return
    out = []
    for x in items[:6]:
        ko = ""
        try:
            body = re.sub(r"\s+", " ", pdf_text(x["url"]))
            k = body.replace(" ", "").find(re.sub(r"\s", "", x["title"])[:12])
            if k > 0:  # 머리말(회사명·연락처) 건너뛰기
                n = i = 0
                while i < len(body) and n < k:
                    if body[i] != " ": n += 1
                    i += 1
                body = body[i:]
            if len(body) > 80:
                j = gemini_json(JPS_SYS.format(title=x["title"], body=body[:3500]), timeout=60, models=MON_MODELS) or {}
                lines = [re.sub(r"^\s*(?:[•\-*・]|\d{1,2}[.)])\s*", "", str(l)).strip() for l in (j.get("lines") or [])][:3]
                lines = [l for l in lines if re.search(r"[가-힣]", l) and len(re.findall(r"[\u3040-\u30ff]", l)) <= len(l) * 0.2]
                ko = "\n".join("• " + l for l in lines)
        except Exception as e:
            log("jps sum error", x.get("code"), str(e)[:80])
        out.append({"id": x["id"], "ko": ko})
    if out:
        try:
            http(JPS_URL + "/sum", timeout=20, data=json.dumps(out, ensure_ascii=False).encode(), headers={"x-watch-key": key, "content-type": "application/json"})
            log("japan stock 공시 요약 대행", len(out), "건(요약 성공", sum(1 for o in out if o["ko"]), ")")
        except Exception as e:
            log("jps sum post error", str(e)[:80])

def jp_summaries():
    """1분마다: japan stock 봇의 요약 대기 공시를 가져와 작업 스레드에서 요약(메인 루프는 막지 않음)"""
    key = os.environ.get("WATCH_KEY")
    if not key or time.time() - _JPS["t"] < 60 or (_JPS["run"] and not _JPS["run"].done()): return
    _JPS["t"] = time.time()
    _JPS["run"] = POOL.submit(_jps_job, key)

def do_reports(st):
    AI_OUT.update(st.get("ai_out") or {})
    try:
        with _key2(): release_ai(st)
        _collect(st)
        for lab, why in list(BAD_KEYS.items()):   # 거부된 Gemini 키(잘못 붙여넣음·만료·삭제) → 하루 한 번 알림
            alert_owner(st, f"Gemini {lab}가 구글에서 거부됐습니다({why[:80]}). GitHub 비밀값의 키를 확인해 주세요.", "badkey:" + lab, every=86400)
        _start_jobs(st)
    finally:
        st["ai_out"] = {k: v for k, v in AI_OUT.items() if v > time.time()}

def _running(kind=None, cid=None):
    return [j for j in JOBS if (kind is None or j["kind"] == kind) and (cid is None or j.get("cid") == cid)]

def _start_jobs(st):
    """대기열 → 작업 스레드(동시에 최대 MAX_JOBS개). 바로 끝나는 일(이미 만든 결과 재전송)은 여기서 처리"""
    # 1) 종목 요청(카드)
    q = st.get("ask_q") or []
    while q and len([x for x in JOBS if x["kind"] in ("analyze", "card")]) < MAX_ASK:
        job = q.pop(0)
        _submit("analyze", analyze_code, job["code"], job=job)
    # 2) IR·Q&A 요약
    q = st.get("ir_q") or []
    while q:
        cid, dest, reply = _job(q[0], st)
        card = st.get("cards", {}).get(cid)
        if not card: q.pop(0); continue
        if card.get("ir_texts") and card.get("qa_watch") and time.time() - card.get("qa_site_ts", 0) > 3600 and not _running("ir", cid) \
                and len([x for x in JOBS if x["kind"] in ("rep", "ir")]) < MAX_HEAVY:  # Q&A 없던 요약 → 그사이 올라왔는지 확인(1시간에 한 번)
            q.pop(0); card["qa_site_ts"] = time.time()
            _submit("ir", _ir_refresh, _copy.deepcopy(card), key2=True, cid=cid, waiters=[(dest, reply)]); log("IR Q&A 다시 확인", card["code"], dest)
            continue
        if card.get("ir_texts") and not _running("ir", cid):  # 이미 만든 요약은 바로
            q.pop(0)
            try:
                send_texts(card, card["ir_texts"], dest, reply)
                if card.get("qa_watch") and str(dest) not in [str(x) for x in card.setdefault("qa_to", [])]: card["qa_to"].append(dest)
            except Exception: traceback.print_exc()
            continue
        same = _running("ir", cid)
        if same: q.pop(0); same[0]["waiters"].append((dest, reply)); continue   # 같은 카드 요약을 만드는 중이면 끝나면 같이 보냄
        with _key2(): out = ai_out_until(heavy=True)
        if out: q.pop(0); defer_ai(st, "ir", cid, dest, reply, out); continue
        if len([x for x in JOBS if x["kind"] in ("rep", "ir")]) >= MAX_HEAVY: break
        q.pop(0)
        _submit("ir", _ir_build, _copy.deepcopy(card), key2=True, cid=cid, waiters=[(dest, reply)]); log("IR 요약 시작", card["code"], dest)
    # 3) 리포트
    q = st.get("rep_q") or []
    while q:
        cid, dest, reply = _job(q[0], st)
        card = st.get("cards", {}).get(cid)
        if not card: q.pop(0); continue
        rp = {"reply_to_message_id": reply, "allow_sending_without_reply": True} if reply else {}
        cap = f"📄 {card.get('name_ko') or card['name']}({card['code']}) 리포트 — 파일을 열어 보세요"
        if card.get("rep_file"):  # 이미 만든 리포트는 텔레그램 파일을 그대로 다시 보냄
            r = tg("sendDocument", {"chat_id": dest, "caption": cap, "document": card["rep_file"], **rp})
            log("리포트 재전송", card["code"], dest, "성공" if r.get("ok") else f"실패 {LAST_ERR['d'][:80]}")
            if r.get("ok"): q.pop(0); continue
        same = _running("rep", cid)
        if same: q.pop(0); same[0]["waiters"].append((dest, reply)); continue
        with _key2(): out = ai_out_until(heavy=True)
        if out: q.pop(0); defer_ai(st, "rep", cid, dest, reply, out); continue
        if len([x for x in JOBS if x["kind"] in ("rep", "ir")]) >= MAX_HEAVY: break
        q.pop(0)
        _submit("rep", _rep_build, _copy.deepcopy(card), key2=True, cid=cid, waiters=[(dest, reply)]); log("리포트 만들기 시작", card["code"], dest)

def _collect(st, wait=0):
    """끝난 작업의 결과를 메인 스레드에서 반영(게시·상태 저장)"""
    if wait and JOBS: _cf.wait([j["fut"] for j in JOBS], timeout=wait)
    for j in [j for j in JOBS if j["fut"].done()]:
        JOBS.remove(j)
        try:
            ok, res = j["fut"].result()
            getattr(_DONE, j["kind"])(st, j, ok, res)
        except Exception:
            traceback.print_exc()

class _DONE:
    @staticmethod
    def analyze(st, j, ok, res):
        job = j["job"]; code = job["code"]
        if ok != "ok":
            tg("sendMessage", {"chat_id": job["chat"], "text": f"{label_of(code)} 분석 실패: {str(res)[:120]}"}); return
        a, row = res
        personal = job.get("watch") or str(job["chat"]) not in (str(CHANNEL), str(st.get("owner")))
        direct = personal or str(job["chat"]) != str(CHANNEL)   # 개인 대화로 요청했으면(주인 포함) 그 대화로
        hit = reuse_card(st, code, row.get("d"), job["chat"] if direct else None, force=True)   # 요청이면 같은 자리에 있어도 다시 보여 줌
        if hit:
            bump(st, "cache"); log("카드 재사용", code, job["chat"] if direct else "channel"); return
        if hit is False:
            drop_user(st, job["chat"]); return
        _submit("card", prepare_card, a, row, job=job, a=a, row=row, personal=personal)

    @staticmethod
    def card(st, j, ok, res):
        job, a, row, personal = j["job"], j["a"], j["row"], j["personal"]; code = job["code"]
        if ok != "ok":
            tg("sendMessage", {"chat_id": job["chat"], "text": f"{label_of(code)} 분석 실패: {str(res)[:120]}"}); return
        cid = f"q{code}{int(time.time()) % 100000000}"
        LAST_ERR["d"] = ""
        msg = finish_card(st, cid, a, row, res, job["chat"] if personal else None)  # 다른 사용자 요청은 그 사람 개인 대화로
        if msg is None and personal and BLOCK_RE.search(LAST_ERR["d"] or ""):
            drop_user(st, job["chat"]); return
        if msg is not None: bump(st, "cards")
        if msg is not None and job.get("report"):
            enqueue_out(st, "rep", cid, job["chat"] if personal else CHANNEL, msg)
            if job.get("ir"): enqueue_out(st, "ir", cid, job["chat"] if personal else CHANNEL, msg)
        if msg is None:
            tg("sendMessage", {"chat_id": job["chat"], "text": f"{label_of(code)} 카드 게시에 실패했습니다."})
        elif str(job["chat"]) != str(CHANNEL) and not personal:
            tg("sendMessage", {"chat_id": job["chat"], "text": f"{label_of(code)} 카드를 채널에 올렸습니다."})
        log("요청 분석", code, "통과" if a["pass"] else a["why"])

    @staticmethod
    def ir(st, j, ok, res):
        card = st.get("cards", {}).get(j["cid"])
        if not card: return
        if ok == "ok" and res == "cached":
            texts, has_qa = card.get("ir_texts"), False
        else:
            texts, has_qa = res if ok == "ok" else (None, False)
            if texts:
                card["ir_texts"] = texts
                if not has_qa: card["qa_watch"] = time.time(); card.setdefault("qa_to", [])
                else: card.pop("qa_watch", None); log("IR 요약에 Q&A 포함", card["code"])
        for dest, reply in j["waiters"]:
            if not texts:
                with _key2(): out = ai_out_until(heavy=True)
                if out: defer_ai(st, "ir", j["cid"], dest, reply, out); continue
                why = FAIL_TXT.get(FAIL.get(card["code"]), FAIL_TXT[None]) if ok == "ok" else "처리 중 오류가 났습니다(기록해 두었습니다). 잠시 뒤 다시 눌러 주세요."
                tg("sendMessage", {"chat_id": dest, "text": f"IR·Q&A 요약 생성 실패({card['code']}). {why}", **({"reply_to_message_id": reply} if reply else {})}); continue
            try:
                send_texts(card, texts, dest, reply)
                if card.get("qa_watch") and str(dest) not in [str(x) for x in card.setdefault("qa_to", [])]: card["qa_to"].append(dest)
            except Exception: traceback.print_exc()

    @staticmethod
    def rep(st, j, ok, res):
        card = st.get("cards", {}).get(j["cid"])
        if not card: return
        for dest, reply in j["waiters"]:
            rp = {"reply_to_message_id": reply, "allow_sending_without_reply": True} if reply else {}
            if ok != "ok":
                tg("sendMessage", {"chat_id": dest, "text": f"리포트 생성 실패({card['code']}). 처리 중 오류가 났습니다(기록해 두었습니다). 잠시 뒤 다시 눌러 주세요.", **rp}); continue
            fn, blob, ok_ai = res
            if not ok_ai:
                with _key2(): out = ai_out_until(heavy=True)
                if out: defer_ai(st, "rep", j["cid"], dest, reply, out); continue   # 만드는 도중 한도가 끝남 → 풀린 뒤 다시
            cap = f"📄 {card.get('name_ko') or card['name']}({card['code']}) 리포트 — 파일을 열어 보세요"
            if not ok_ai: cap += "\n⚠️ AI 분석(결론·사업부·IR 핵심 데이터·Q&A)이 빠졌습니다. " + FAIL_TXT.get(FAIL.get(card["code"]), FAIL_TXT[None]) + " 📄 리포트를 다시 누르면 새로 만듭니다."
            if card.get("rep_file"):
                r = tg("sendDocument", {"chat_id": dest, "caption": cap, "document": card["rep_file"], **rp})
                if r.get("ok"): continue
            r = tg("sendDocument", {"chat_id": dest, "caption": cap, **rp}, files={"document": (fn, blob, "text/html")}, timeout=90)
            log("리포트 보냄", card["code"], dest, "AI 포함" if ok_ai else "AI 빠짐", "성공" if r.get("ok") else f"실패 {LAST_ERR['d'][:80]}")
            if r.get("ok") and ok_ai:
                card["rep_file"] = ((r.get("result") or {}).get("document") or {}).get("file_id")

def finish_jobs(st, timeout=300):
    """실행을 마치기 전: 돌던 작업을 최대 timeout초 기다려 반영, 그래도 안 끝난 건 대기열에 되돌려 다음 실행이 이어서 처리"""
    _collect(st, wait=timeout)
    for j in list(JOBS):
        if j["kind"] in ("analyze", "card"): st.setdefault("ask_q", []).insert(0, j["job"])
        else:
            for dest, reply in j["waiters"]:
                st.setdefault("rep_q" if j["kind"] == "rep" else "ir_q", []).insert(0, {"cid": j["cid"], "chat": dest, "reply": reply})
        log("미완료 작업 되돌림", j["kind"])
    JOBS.clear()
    POOL.shutdown(wait=False, cancel_futures=True)

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
        if c == code: return f"{ko or en or jp}({c})"
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
            "• /del 6857 — 관심 종목 삭제 · /list — 내 관심 종목과 다음 실적 발표일\n"
            "• /alert on — 채널 통과 카드를 개인 대화로도 받기, /mincap 1000 · /exsector 은행 · /onlyrule 매출 고성장 으로 내 조건만 · /myrules 확인\n"
            "• 카드의 📄 리포트·🗣 IR·Q&A 버튼으로 상세 자료도 받을 수 있습니다.\n"
            "스크리닝 기준을 통과한 종목은 지금처럼 채널에 올라갑니다.")


GUIDE_VER = 5  # 공지 문구를 바꾸면 올림 → 고정 공지를 새 내용으로 수정
def guide_text(me):
    R = RULES
    sec = ", ".join(sorted(dict.fromkeys(SECTOR_KO.get(x, x) for x in EXCL_SECTORS)))
    return ("📌 <b>실적 스크리닝 채널 사용법</b>\n\n"
            "일본 상장사 결산단신이 나오면 자동으로 판별해서, 조건을 통과한 종목만 이 채널에 카드로 올립니다.\n\n"
            "<b>① 어떤 종목이 올라오나요?</b>\n"
            "분기(3개월) 실적을 전년 같은 분기와 비교해, 아래 4개 중 하나라도 맞으면 올라옵니다.\n"
            f"✅ 매출·이익 성장: 매출 +{R['R1_rev_yoy']:.0f}% 이상, 영업이익 +{R['R2_op_yoy']:.0f}% 이상(흑자)\n"
            f"✅ 매출 고성장: 이익과 상관없이 매출 +{R['R5_rev_only_yoy']:.0f}% 이상\n"
            f"✅ 성장 가속: 매출·영업이익 모두 플러스이고, 둘 중 하나의 증가율이 직전 분기보다 {R['R3_accel_pp']:.0f}%p 이상 높아짐\n"
            f"✅ 턴어라운드: 영업적자에서 흑자 전환, 또는 2개 분기 연속 감익 뒤 영업이익 +{R['R4_turn_yoy']:.0f}% 이상\n\n"
            "다만 아래 경우는 제외합니다.\n"
            "❌ 매출·영업이익 증가율이 둘 다 직전 분기보다 낮아짐\n"
            "❌ 매출이 2개 분기 연속 전년보다 감소\n"
            f"❌ 시가총액 {R['X5_min_cap_oku']:,.0f}억 엔 미만\n"
            f"❌ 업종: {sec}\n\n"
            "<b>② 카드 버튼</b>\n"
            "📄 리포트: 사업부·지역별 상세 분석, 시장 반응(note·야후 게시판·증권사 목표주가)\n"
            "🗣 IR·Q&amp;A: 실적 설명회 질의응답 요약\n"
            "누구나 누를 수 있고, 결과는 누른 사람의 개인 대화로 갑니다.\n\n"
            "<b>③ 내 종목만 따로 받기</b>\n"
            f"채널에는 글을 쓸 수 없으니 봇 @{me} 과 1:1 대화를 열고 /start 를 누르세요.\n"
            "• 종목 코드나 이름 보내기(예: 6857, 어드반테스트): 최근 분기 실적 카드\n"
            "• /add 6857 7203: 관심 종목 등록. 결산단신이 나오면 통과 여부와 상관없이 카드를 보내 줌\n"
            "• /del 6857: 삭제 · /list: 내 목록과 다음 실적 발표일\n\n"
            "<b>④ 내 조건으로 알림 받기(선택)</b>\n"
            "• /alert on: 채널에 올라온 카드를 개인 대화로도 받기\n"
            "• /mincap 1000: 시가총액 1,000억 엔 이상만\n"
            "• /exsector 은행 건설: 해당 업종 빼기\n"
            "• /onlyrule 매출 고성장: 원하는 규칙만\n"
            "• /myrules: 내 설정 보기\n\n"
            "<b>⑤ 주간 정리</b>\n"
            "매주 토요일 15시에 이번 주 통과 종목과, 규칙별로 다음 날·1주·1개월 주가가 TOPIX보다 나았는지를 정리해 올립니다.")

def pref_cmd(st, chat, text):
    """개인별 규칙: /alert on|off · /mincap 1000 · /exsector 은행 건설 (/exsector 로 초기화) · /onlyrule 매출 고성장 · /myrules"""
    m = re.match(r"^/(alert|mincap|exsector|onlyrule|myrules)\b\s*(.*)$", text.strip(), re.I)
    if not m: return False
    cmd, arg = m.group(1).lower(), m.group(2).strip()
    pr = st.setdefault("prefs", {}).setdefault(str(chat["id"]), {})
    rules = ["매출 성장", "매출 고성장", "성장 가속", "턴어라운드"]
    if cmd == "alert":
        pr["alert"] = arg.lower() not in ("off", "끄기", "0")
    elif cmd == "mincap":
        try: pr["mincap"] = float(re.sub(r"[^\d.]", "", arg) or 0)
        except ValueError: pass
    elif cmd == "exsector":
        known = set(SECTOR_KO.values())
        pr["ex"] = [x for x in re.split(r"[,\s]+", arg) if x in known]
    elif cmd == "onlyrule":
        pr["rules"] = [r for r in rules if r.replace(" ", "") in arg.replace(" ", "") or (r == "매출 성장" and "이익" in arg)]
    if not any(pr.get(k) for k in ("alert", "mincap", "ex", "rules")): st["prefs"].pop(str(chat["id"]), None)
    tg("sendMessage", {"chat_id": chat["id"], "text":
        "내 알림 규칙\n"
        f"• 채널 통과 카드 개인 알림: {'켜짐' if pr.get('alert') else '꺼짐'} (/alert on · /alert off)\n"
        f"• 최소 시총: {pr.get('mincap') or 0:,.0f}억 엔 (/mincap 1000)\n"
        f"• 제외 업종: {', '.join(pr.get('ex') or []) or '없음'} (/exsector 은행 건설 · /exsector 로 초기화)\n"
        f"• 규칙: {', '.join(pr.get('rules') or []) or '전체'} (/onlyrule 매출 고성장 성장 가속 · /onlyrule 로 초기화)\n"
        f"업종 이름 예: {', '.join(sorted(set(SECTOR_KO.values()))[:12])} …"})
    return True

def user_cmd(st, chat, text):
    """개인 대화 명령: /add /del /list (사용자마다 관심 종목 따로 보관)"""
    if pref_cmd(st, chat, text): return True
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
        sched = (jp_state().get("sched") or {})
        def nxt(c):
            d = (sched.get(c) or [None])[0]
            return f" · 다음 실적 {int(d[5:7])}/{int(d[8:10])}" if d else ""
        msg = f"관심 종목 {len(mine)}개\n" + ("\n".join(label_of(c) + nxt(c) for c in sorted(mine, key=lambda c: (sched.get(c) or ["9999"])[0])) if mine else "(없음) /add 6857 처럼 추가하세요.")
    if not mine: subs.pop(uid, None)
    tg("sendMessage", {"chat_id": chat["id"], "text": msg})
    return True

JP_STATE_URL = "https://raw.githubusercontent.com/ssamgij-bit/jpall/state/state.json"
_JPS = {"t": 0, "d": {}}
def jp_state():
    """공시봇 상태(컨센서스 cons·발표 예정일 sched·구독 watch) — 30분마다 새로 읽음"""
    if time.time() - _JPS["t"] > 1800:
        try:
            import stcrypt   # 주인 ID·관심 종목은 암호화돼 있음(WATCH_KEY로 풂)
            _JPS["d"] = stcrypt.reveal(json.loads(http(JP_STATE_URL, timeout=30))); _JPS["t"] = time.time()
        except Exception as e: log("jp state error", e)
    return _JPS["d"]
WATCH_URL = os.environ.get("WATCH_URL", "https://jp-disclosure-bot.ssamgij.workers.dev/watchlist")
WATCH_KEY = os.environ.get("WATCH_KEY", "")
def sync_jp_stock(st):
    """japan stock 봇(@JP_stock_yh_bot) 사용자별 구독 목록 → 이 봇의 같은 사람 관심 종목(하루 한 번).
    두 봇 모두와 대화를 시작한 사람만 연동. 직접 /add 한 종목은 그대로 두고, japan stock 봇에서 빠진 종목만 같이 뺌"""
    d = now().strftime("%Y%m%d")
    if st.get("jps_date") == d: return
    st["jps_date"] = d
    j = jp_state() or {}
    owner = j.get("owner")
    if not isinstance(owner, int) or owner <= 0: owner = None   # 못 풀었으면(-1) 무시
    if owner and st.get("owner") is None: st["owner"] = owner  # 주인 = 두 봇에 같은 텔레그램 계정
    users_map, fetched = {}, False
    if WATCH_KEY:
        try:
            w = json.loads(http(WATCH_URL, timeout=20, headers={"x-watch-key": WATCH_KEY}))
            users_map = {str(k): sorted(v) for k, v in (w.get("users") or {}).items()}
            fetched = "users" in w
        except Exception as e:
            log("jp stock watchlist error", e); st.pop("jps_date", None); return   # 다음 실행 때 다시
    if not WATCH_KEY and owner and str(owner) == str(st.get("owner")):  # 예전 방식: 공시 채널 봇이 받아 둔 주인 목록
        users_map = {str(owner): sorted((j.get("watch") or {}).keys())}
    jm = st.setdefault("jps_map", {})
    if "jps_codes" in st:
        old = st.pop("jps_codes")
        if st.get("owner"): jm.setdefault(str(st["owner"]), old)
    known = set(st.get("users") or {}) | set(st.get("subs") or {}) | ({str(st["owner"])} if st.get("owner") else set())
    subs = st.setdefault("subs", {}); n = 0
    for uid in (set(users_map) | set(jm)) if fetched else set(users_map):
        if uid not in known: continue
        codes = users_map.get(uid) or []
        old = set(jm.get(uid) or [])
        mine = [c for c in subs.get(uid, []) if c not in old or c in codes]
        mine += [c for c in codes if c not in mine]
        if mine: subs[uid] = mine
        else: subs.pop(uid, None)
        if codes: jm[uid] = codes
        else: jm.pop(uid, None)
        n += 1
    log("jp stock watchlist synced", n, "users", sum(len(v) for v in users_map.values()), "codes")

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
            bump(st, "watch")
    if len(sent) > 5000:
        for k in sorted(sent, key=sent.get)[:1000]: sent.pop(k)

def stats_text(st):
    """주인 전용 /stats: 사용자·요청·AI 사용 현황"""
    subs = st.get("subs") or {}; users = set(st.get("users") or {}) | set(subs)
    codes = {c for v in subs.values() for c in v}
    days = sorted((st.get("stats") or {}).items())[-7:]
    tot = lambda k: sum(v.get(k, 0) for _, v in days)
    today = (st.get("stats") or {}).get(now().strftime("%Y%m%d"), {})
    L = ["📊 실적 스크리닝 봇 현황",
         f"사용자 {len(users)}명(관심 종목 등록 {len(subs)}명, 차단·탈퇴 {len(st.get('blocked') or {})}명)",
         f"관심 종목 합계 {len(codes)}개(중복 제외)",
         f"오늘: 요청 {today.get('ask', 0)} · 새 카드 {today.get('cards', 0)} · 카드 재사용 {today.get('cache', 0)} · 리포트 {today.get('rep', 0)} · IR {today.get('ir', 0)} · 관심 종목 알림 {today.get('watch', 0)}",
         f"최근 {len(days)}일: 요청 {tot('ask')} · 새 카드 {tot('cards')} · 재사용 {tot('cache')} · 리포트 {tot('rep')} · IR {tot('ir')}",
         f"지금 대기: 카드 {len(st.get('ask_q') or [])} · 리포트 {len(st.get('rep_q') or [])} · IR {len(st.get('ir_q') or [])}"]
    return "\n".join(L)

def handle_text(st, chat, text):
    """채널 글·봇 개인 메시지로 온 종목명/코드 → 요청 분석 대기열"""
    text = (text or "").strip()
    if chat.get("type") not in ("private", "channel"): return  # 단체방 대화는 무시
    if not text or len(text) > 40 or "\n" in text: return
    if chat.get("type") == "channel" and str(chat.get("id")) != str(CHANNEL): return
    if chat.get("type") == "private":
        if st.get("owner") is None: st["owner"] = chat["id"]
        us = st.setdefault("users", {})
        if str(chat["id"]) not in us: st.pop("jps_date", None)   # 새 사용자 → japan stock 봇 구독 목록 바로 연동
        us[str(chat["id"])] = now().strftime("%Y%m%d")
        st.get("blocked", {}).pop(str(chat["id"]), None)
        m = re.match(r"^/start\s+(rep|ir)_(\S+)$", text)
        if m:  # 채널 카드 버튼 → 시작 링크로 들어온 경우
            if m.group(2) in st.get("cards", {}):
                enqueue_out(st, m.group(1), m.group(2), chat["id"])
                tg("sendMessage", {"chat_id": chat["id"], "text": ("리포트" if m.group(1) == "rep" else "IR·Q&A 요약") + "를 만들어 보내드립니다(처음 만들 땐 1~3분).\n\n" + HELP_TXT})
            else:
                tg("sendMessage", {"chat_id": chat["id"], "text": "카드 정보가 만료됐습니다.\n\n" + HELP_TXT})
            return
        if text.startswith("/stats"):
            if chat["id"] == st.get("owner"): tg("sendMessage", {"chat_id": chat["id"], "text": stats_text(st)})
            return
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
        bump(st, "ask")
        n = len(st["ask_q"])
        tg("sendMessage", {"chat_id": chat["id"], "text": f"{label_of(code)} 최근 분기 실적을 분석합니다. 대기 {n}번째"
                           + (" — 1~3분 걸립니다." if n <= 1 else f" — 앞 요청이 있어 {n * 2}분 안팎 걸릴 수 있습니다.")
                           + " (이미 만든 카드가 있으면 바로 보내드립니다)"})
    elif cands:
        tg("sendMessage", {"chat_id": chat["id"], "text": "여러 종목이 맞습니다. 코드로 보내 주세요:\n" + "\n".join(label_of(c) for c in cands)})
    elif re.search(r"[0-9A-Za-z가-힣ぁ-んァ-ン一-龥]", text) and (chat.get("type") == "private" or re.fullmatch(r"[0-9][0-9A-Za-z]{3}", text)):
        tg("sendMessage", {"chat_id": chat["id"], "text": f"'{text}'에 맞는 상장 종목을 찾지 못했습니다. 종목 코드로 보내 주세요."})

def reuse_card(st, code, d, chat, force=False):
    """같은 종목·같은 결산일 카드가 있으면 chat(None=채널)으로 복사. 성공 True, 받는 사람이 봇 차단이면 False, 없으면 None"""
    cands = [(cid, c) for cid, c in st.get("cards", {}).items() if c.get("code") == code and c.get("d") == d and c.get("msg") and not c.get("deleted")]
    if not cands: return None
    cid, c = max(cands, key=lambda x: x[1].get("ts", 0))
    src = c.get("chat") or CHANNEL
    dest = chat or CHANNEL
    if str(src) == str(dest) and not force: return True  # 이미 그 자리에 있음(요청이 아닌 자동 처리일 때만 건너뜀)
    LAST_ERR["d"] = ""
    r = tg("copyMessage", {"chat_id": dest, "from_chat_id": src, "message_id": c["msg"], "reply_markup": keyboard(cid, c)})
    if r.get("ok"):
        c.setdefault("copies", {})[str(dest)] = (r.get("result") or {}).get("message_id"); return True
    if chat and BLOCK_RE.search(LAST_ERR["d"] or ""): return False
    return None

def drop_user(st, uid):
    """봇을 차단·탈퇴한 사용자: 관심 종목 지우고 기록"""
    uid = str(uid)
    if uid == str(st.get("owner")): return
    st.get("subs", {}).pop(uid, None)
    st.setdefault("blocked", {})[uid] = now().strftime("%Y%m%d")
    log("user dropped (blocked)", uid)

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

def valuation(code):
    """PBR(Yahoo 시세) + 분기 매출 컨센서스(공시봇이 발표 전에 받아 둔 Yahoo 애널리스트 평균)"""
    out = {}
    try:
        import jpall
        q = jpall.yf_quotes([code]).get(code) or {}
        if q.get("priceToBook") and 0 < q["priceToBook"] < 200: out["pbr"] = float(q["priceToBook"])
    except Exception as e:
        log("pbr error", e)
    c = (jp_state().get("cons") or {}).get(code) or {}
    if c.get("rev") and time.time() - c.get("ts", 0) < 86400 * 8:
        out["cons_rev"] = c["rev"]; out["cons_n"] = c.get("rev_n")
    return out

def make_card(st, cid, a, row, chat=None):
    """분석 결과 → Gemini 요약 → 카드(차트+글) 게시 → 상태에 저장. 게시 실패면 None"""
    return finish_card(st, cid, a, row, prepare_card(a, row), chat)

def prepare_card(a, row):
    """오래 걸리는 부분(밸류에이션·IR 자료·Gemini 요약·차트) — 작업 스레드에서 실행 가능(상태 st 를 건드리지 않음)"""
    if True:
        fem = fy_end_month_of(a["r"])
        m = metrics(a["series"], len(a["series"]) - 1)
        nums = (f"이번 분기 매출 {oku(m['q'].get('rev'))}엔({yoy_txt(m['rev_yoy'], m['rev_st'])}), 영업이익 {oku(m['q'].get('op'))}엔"
                f"({yoy_txt(m['op_yoy'], m['op_st'])}), 순이익 {oku(m['q'].get('ni'))}엔({yoy_txt(m['ni_yoy'], m['ni_st'])})")
        try: a["val"] = valuation(a["code"])
        except Exception as e: log("valuation error", e)
        mat_pdf, mat_url, mat_src = find_mat_pdf(a, row)
        ai = ai_card({"name": a["name"], "code": a["code"], "sector": a["sector"], "profile": a.get("profile", ""), "nums": nums, "qual": a.get("qual", "")}, mat_pdf)
        if ai is not None and mat_pdf: ai["_src"] = mat_src
        cap = card_text(a, ai, row)
        buf = io.BytesIO()
        chart_name = name_of(a["code"], a["name"], ai) + f"({a['code']})"
        card_chart(chart_name, a["series"], fem, buf)
        return {"fem": fem, "ai": ai, "cap": cap, "png": buf.getvalue(), "mat_url": mat_url, "mat_src": mat_src}

def finish_card(st, cid, a, row, prep, chat=None):
    """준비된 카드 게시 + 상태 저장(메인 스레드)"""
    if True:
        fem, ai, cap, mat_url, mat_src = prep["fem"], prep["ai"], prep["cap"], prep["mat_url"], prep["mat_src"]
        msg = post_card(st, cid, cap, prep["png"], chat)
        if msg is None and not DRY: return None
        if not chat and a.get("pass") and not a.get("ondemand"):  # 채널 통과 카드: 성과 추적 + 개인 알림
            st.setdefault("perf_log", []).append({"cid": cid, "code": a["code"], "name": name_of(a["code"], a["name"], ai), "d": row["d"],
                                                 "t": row.get("t") or "15:30", "hit": a["hit"], "cap": a.get("cap"), "sector": a.get("sector")})
        st.setdefault("cards", {})[cid] = {
            "code": a["code"], "name": a["name"], "name_ko": name_of(a["code"], a["name"], ai), "d": row["d"], "t": row.get("t") or "15:30", "msg": msg,
            "caption": cap, "ts": time.time(), "sector": a["sector"], "cap": a.get("cap"), "pdf": row.get("pdf"), "xbrl": row.get("xbrl"),
            "mat": mat_url, "mat_src": mat_src, "fem": fem, "series": a["series"][-8:], "hit": a["hit"],
            "r": {k: a["r"].get(k) for k in ("q", "start", "end", "fc", "fc_end", "rev", "op", "ni", "scope")},
            "ai": ai, "profile": a.get("profile", "")[:600], "qual": a.get("qual", "")[:8000], "chat": str(chat or CHANNEL)}
        return msg

def fanout_personal(st):
    """개인별 규칙(/alert on, /mincap, /exsector)을 켠 사용자에게 새 채널 카드 중 조건에 맞는 것만 개인 대화로 복사"""
    prefs = st.get("prefs") or {}
    if not prefs: return
    done = st.setdefault("fan_done", [])
    for p in st.get("perf_log", [])[-50:]:
        c = st.get("cards", {}).get(p["cid"])
        if p["cid"] in done or not c or not c.get("msg"): continue
        done.append(p["cid"])
        for uid, pr in prefs.items():
            if not pr.get("alert"): continue
            if pr.get("mincap") and (p.get("cap") or 0) < pr["mincap"]: continue
            if SECTOR_KO.get(p.get("sector"), p.get("sector")) in (pr.get("ex") or []): continue
            if pr.get("rules") and not set(pr["rules"]) & set(p.get("hit") or []): continue
            r = tg("copyMessage", {"chat_id": uid, "from_chat_id": CHANNEL, "message_id": c["msg"], "reply_markup": keyboard(p["cid"], c)})
            if not r.get("ok") and BLOCK_RE.search(LAST_ERR["d"] or ""): drop_user(st, uid); pr["alert"] = False
            bump(st, "fan"); time.sleep(0.2)
    st["fan_done"] = done[-300:]

def perf_update(st):
    """채널 통과 카드의 1주(5거래일)·1개월(20거래일) 수익률과 TOPIX 대비 초과수익(발표 전 종가 기준) — 하루 한 번(16시 이후)"""
    d = now().strftime("%Y%m%d")
    if st.get("perf_date") == d or now().hour < 16: return
    st["perf_date"] = d
    if not st.get("perf_seeded"):  # 처음 한 번: 이미 채널에 나간 통과 카드도 성과 추적에 넣음
        have = {p["cid"] for p in st.get("perf_log", [])}
        for cid, c in st.get("cards", {}).items():
            if cid in have or cid.startswith("q") or not c.get("hit") or (c.get("chat") and str(c["chat"]) != str(CHANNEL)): continue
            st.setdefault("perf_log", []).append({"cid": cid, "code": c["code"], "name": c.get("name_ko") or c.get("name"), "d": c["d"],
                                                 "t": c.get("t") or "15:30", "hit": c["hit"], "cap": c.get("cap"), "sector": c.get("sector")})
        st["perf_seeded"] = True; st["fan_done"] = [p["cid"] for p in st.get("perf_log", [])]
    tp = None
    for p in st.get("perf_log", []):
        if p.get("m1x") is not None: continue
        try:
            bars = naver_daily(f"{p['code']}.T", days=120)
            if tp is None: tp = {b["d"]: b["c"] for b in naver_daily(".TOPX", days=150, index=True)}
        except Exception as e:
            log("perf error", p["code"], e); continue
        ds = f"{p['d'][:4]}-{p['d'][4:6]}-{p['d'][6:]}"
        intraday = p.get("t", "15:30") < "15:30"
        k0 = next((i for i, b in enumerate(bars) if (b["d"] >= ds if intraday else b["d"] > ds)), None)
        if not k0: continue
        base = bars[k0 - 1]
        for key, n in (("d1", 1), ("w1", 5), ("m1", 20)):
            if p.get(key + "x") is not None or k0 + n - 1 >= len(bars): continue
            e = bars[k0 + n - 1]
            ret = (e["c"] / base["c"] - 1) * 100
            mk = (tp.get(e["d"]) / tp.get(base["d"]) - 1) * 100 if tp.get(e["d"]) and tp.get(base["d"]) else 0.0
            p[key] = round(ret, 2); p[key + "x"] = round(ret - mk, 2)
        time.sleep(0.3)
    cut = (now() - dt.timedelta(days=200)).strftime("%Y%m%d")
    st["perf_log"] = [p for p in st.get("perf_log", []) if p["d"] >= cut]

WEEKLY_DOW, WEEKLY_HOUR = 5, 15   # 토요일 15시
def weekly_post(st):
    """토요일: 이번 주 통과 종목(다음 날 주가 반응 순) + 규칙별 성과표(최근 120일)"""
    n = now(); wk = n.strftime("%G-%V")
    if n.weekday() != WEEKLY_DOW or n.hour < WEEKLY_HOUR or st.get("weekly_done") == wk: return
    st["weekly_done"] = wk
    week0 = (n - dt.timedelta(days=7)).strftime("%Y%m%d")
    pl = st.get("perf_log", [])
    wkp = sorted([p for p in pl if p["d"] > week0], key=lambda p: -(p.get("d1x") if p.get("d1x") is not None else -999))
    L = [f"🗓 <b>이번 주 실적 스크리닝 정리</b> ({(n - dt.timedelta(days=6)).strftime('%m/%d')}~{n.strftime('%m/%d')})", f"통과 {len(wkp)}종목 · 다음 날 TOPIX 대비 주가 반응 순", ""]
    for p in wkp[:25]:
        rx = f"{p['d1x']:+.1f}%p" if p.get("d1x") is not None else "반응 대기"
        L.append(f"• {esc(p.get('name') or p['code'])}({p['code']}) {rx} — {' · '.join(p.get('hit') or [])}")
    if len(wkp) > 25: L.append(f"… 외 {len(wkp) - 25}종목")
    L += ["", "<b>규칙별 성과</b>(최근 120일 통과 종목, 발표 전 종가 기준 TOPIX 대비 초과수익 평균 · 플러스 비율)"]
    cut = (n - dt.timedelta(days=120)).strftime("%Y%m%d")
    rules = ["매출 성장", "매출 고성장", "성장 가속", "턴어라운드"]
    for r in rules + ["전체"]:
        g = [p for p in pl if p["d"] >= cut and (r == "전체" or r in (p.get("hit") or []))]
        def avg(k):
            xs = [p[k] for p in g if p.get(k) is not None]
            return (f"{sum(xs) / len(xs):+.1f}%p({sum(1 for x in xs if x > 0) / len(xs) * 100:.0f}%·{len(xs)}건)" if xs else "—")
        lab = "매출·이익 성장" if r == "매출 성장" else r
        L.append(f"• {lab} {len(g)}건 — 다음 날 {avg('d1x')} · 1주 {avg('w1x')} · 1개월 {avg('m1x')}")
    L.append("<i>투자 자문이 아닌 기록 정리입니다. 표본이 적을 땐 참고만.</i>")
    tg("sendMessage", {"chat_id": CHANNEL, "text": "\n".join(L), "parse_mode": "HTML", "disable_web_page_preview": True})
    log("weekly posted", len(wkp))

ERRS = {"n": 0}
def alert_owner(st, text, key, every=6 * 3600):
    """주인에게 장애 알림(같은 종류는 every초에 한 번)"""
    al = st.setdefault("alerts", {})
    if time.time() - al.get(key, 0) < every or not st.get("owner"): return
    al[key] = time.time()
    tg("sendMessage", {"chat_id": st["owner"], "text": "⚠️ 실적 스크리닝 봇: " + text})

def cleanup(st):
    cut = (now() - dt.timedelta(days=5)).strftime("%Y%m%d")
    st["seen"] = {k: v for k, v in st.get("seen", {}).items() if v >= cut}
    old = time.time() - 86400 * 30
    st["cards"] = {k: v for k, v in st.get("cards", {}).items() if v.get("ts", 0) > old}
    for c in st["cards"].values():  # 1주 지난 카드는 큰 원문 자료를 비워 상태 파일을 가볍게
        if time.time() - c.get("ts", 0) > 86400 * 7: c.pop("qual", None)
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
    if st.get("guide_ver") != GUIDE_VER and not DRY:  # 채널 고정 공지(봇 아이디·사용법·스크리닝 규칙): 처음엔 올리고 고정, 내용이 바뀌면 수정
        try:
            me = (tg("getMe").get("result") or {}).get("username", "")
            txt = guide_text(me)
            mid = st.get("guide_v1")
            r = tg("editMessageText", {"chat_id": CHANNEL, "message_id": mid, "text": txt, "parse_mode": "HTML", "disable_web_page_preview": True}) if mid else {}
            if not r.get("ok") and "not modified" not in str(r.get("description", "")):
                r = tg("sendMessage", {"chat_id": CHANNEL, "text": txt, "parse_mode": "HTML", "disable_web_page_preview": True})
                mid = (r.get("result") or {}).get("message_id")
                if mid: tg("pinChatMessage", {"chat_id": CHANNEL, "message_id": mid, "disable_notification": True})
            if mid:
                st["guide_v1"] = mid; st["guide_ver"] = GUIDE_VER; save(st); log("guide posted/updated", me)
        except Exception:
            traceback.print_exc()
    if not st.get("kb_v2") and not DRY:  # 기존 카드의 🗑 삭제 버튼 없애기(한 번)
        for cid, c in list(st.get("cards", {}).items()):
            if c.get("msg") and not c.get("deleted"):
                tg("editMessageReplyMarkup", {"chat_id": c.get("chat") or CHANNEL, "message_id": c["msg"], "reply_markup": keyboard(cid, c)}); time.sleep(0.5)
        st["kb_v2"] = True; save(st)
    try: sync_jp_stock(st)
    except Exception: traceback.print_exc()
    try: gateway_setup(st)
    except Exception: traceback.print_exc()
    import aicache; aicache.prune()   # 7일 지난 AI 결과 저장분 정리
    last_scan = 0
    while time.time() - t0 < RUN_SECONDS:
        try:
            poll_buttons(st, wait=0)
            do_reports(st)
            jp_summaries()
            if time.time() - last_scan > 90:
                last_scan = time.time()
                for back in (1, 0):
                    process_day(st, (now() - dt.timedelta(days=back)).strftime("%Y%m%d"))
                update_reactions(st)
                fanout_personal(st)
                perf_update(st)
                weekly_post(st)
                cleanup(st); save(st)
        except Exception as e:
            traceback.print_exc()
            ERRS["n"] += 1
            if ERRS["n"] >= 5: alert_owner(st, f"이번 실행에서 오류가 {ERRS['n']}번 났습니다. 마지막 오류: {str(e)[:200]}", "loop_err"); ERRS["n"] = 0
        poll_buttons(st, wait=25)
        save(st)
    try: finish_jobs(st)
    except Exception: traceback.print_exc()
    save(st)

if __name__ == "__main__":
    main()
