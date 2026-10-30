"""[🗣 IR·Q&A] 버튼 → 결산 설명 자료·질의응답(Q&A) 중심 요약(텔레그램 글)

자료(찾는 순서)
 1. TDnet: 결산단신 PDF, 결산 설명 자료 PDF, 질의응답 요지 PDF
 2. 회사 IR 홈페이지: IR 첫 화면·IR 자료실·IR 뉴스에서 '질의응답·Q&A·書き起こし(기록)·Transcript·説明会 要旨' 링크(PDF 또는 웹페이지)
 3. 로그미 파이낸스(finance.logmi.jp): 결산 설명회 기록 기사(질의응답 포함인 경우가 많음)
질의응답 자료가 아직 없으면 그렇게 표시하고, 14일 동안 위 세 곳을 다시 확인해 올라오면 Q&A를 추가로 보냄.
"""
import os, re, json, time, base64, html, datetime as dt

import earn as E
from jpall import http, GEMINI_MODELS

QA_RE = re.compile(r"質疑応答|質疑|Q&A|Q＆A|主な質問|説明会.*(要旨|内容|スクリプト|記録)|書き起こし|書起こし|文字起こし|スクリプト|Transcript|スモールミーティング", re.I)

IR_PROMPT = """첨부 PDF는 일본 상장사 {name}({code})의 이번 결산 관련 공식 자료다(결산단신, 결산 설명 자료, 질의응답 요지 중 있는 것).
한국 애널리스트용 '실적 발표 IR·경영진 Q&A 요약'을 한국어 JSON으로 써라.
규칙
- 자료에 있는 내용만 쓴다. 추측·외부 지식 금지. 자료에 없으면 빈 문자열 또는 빈 배열.
- 일본어를 그대로 쓰지 말고 전부 한국어로 번역(인명·제품명은 한글 발음 또는 통용 영문).
- 금액은 자료 단위 그대로 쓰되 억 엔으로 바꿀 수 있으면 억 엔(1억 엔=100백만 엔). 전년 대비는 YoY.
- quality: 실적 퀄리티 1~10점(매출·이익 성장의 질, 일회성 여부, 가이던스 변화를 종합)과 한 줄 근거.
- qa: 질의응답 자료가 있을 때만 3~8개. 질문자 소속이 있으면 asker에, 답변자 직함이 있으면 answerer에.
출력:
{{"quality": 8, "quality_why": "...", "overview": "회사가 무엇으로 돈을 버는지 1~2문장",
 "key": ["핵심 실적 3~5줄: 매출·영업이익·순이익과 YoY, 부문 숫자"], "message": "경영진 핵심 메시지 3~4문장",
 "surprise": [{{"title": "짧은 제목", "body": "1~2문장"}}], "kpi": ["특이사항·핵심 KPI(수주잔고, 점포 수, 단가, 환율 영향 등) 3~6줄"],
 "guidance": "가이던스와 수정 이유, 배당·주주환원 2~4문장", "qa": [{{"asker": "", "answerer": "", "q": "질문", "a": "답변 2~3문장"}}],
 "qa_source": "질의응답 출처 자료 이름(없으면 빈 문자열)"}}
"""

SLOW = {}  # 모델 → 다시 시도할 시각(응답 지연·과부하일 때)
def gemini_pdfs(prompt, pdfs):
    import jpall; key = jpall.gem_key()
    bad = [p for p in pdfs if not (p and b"%PDF" in p[:1024])]
    if bad: E.log("not-pdf docs dropped", len(bad))
    pdfs = [p for p in pdfs if p and b"%PDF" in p[:1024]]   # PDF가 아닌 파일(웹페이지)을 PDF로 보내면 모든 모델이 400 오류
    if not pdfs:
        import jpall as _j
        if getattr(_j.AI_TL, "fails", None) is not None: _j.AI_TL.fails.append("nodoc")
    if not key or not pdfs: return None
    import aicache
    ck = aicache.key(prompt, pdfs); hit = aicache.get(ck)
    if hit: E.log("ai cache hit(IR)"); return hit   # 같은 자료·같은 지시문 → 저장해 둔 결과(한도 절약)
    parts = [{"inline_data": {"mime_type": "application/pdf", "data": base64.b64encode(b).decode()}} for b in pdfs] + [{"text": prompt}]
    body = json.dumps({"contents": [{"parts": parts}],
                       "generationConfig": {"temperature": 0.2, "maxOutputTokens": 12000, "responseMimeType": "application/json"}}).encode()
    for model in GEMINI_MODELS + GEMINI_MODELS:  # 구글 서버가 붐비면(503) 한 바퀴 더
        if SLOW.get(model, 0) > time.time() or E.ai_skip(model): continue  # 방금 응답 없던 모델·한도 초과 모델은 잠시 건너뜀
        try:
            r = json.loads(http(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}",
                                timeout=240, data=body, headers={"content-type": "application/json"}))
            txt = "".join(p.get("text", "") for p in r["candidates"][0]["content"]["parts"])
            out = json.loads(re.sub(r"^```(json)?|```$", "", txt.strip()))
            if isinstance(out, list): out = out[0] if out and isinstance(out[0], dict) else None
            aicache.put(ck, out)
            return out
        except Exception as e:
            kind = E.ai_err(e, model, "ir")   # 키 이름·구글 응답 사유까지 기록, 실패 사유 분류
            if kind == "quota": E.ai_429(model)
            if kind == "busy": SLOW[model] = time.time() + 1200  # 응답 지연·과부하 → 20분간 건너뜀
            if kind in ("quota", "busy"): time.sleep(8)
            if kind in ("bad", "key"): break   # 자료·키 문제는 모델을 바꿔도 같음 → 한도 낭비 없이 중단
    return None

def find_qa(code, since_day, days=14):
    """since_day(YYYYMMDD)부터 오늘까지 TDnet에서 같은 종목의 질의응답 자료"""
    d0 = dt.datetime.strptime(since_day, "%Y%m%d").date()
    out = []
    for k in range(days + 1):
        d = d0 + dt.timedelta(days=k)
        if d > E.now().date(): break
        try:
            for r in E.scan_tdnet(d.strftime("%Y%m%d")):
                if r["code"] == code and QA_RE.search(r["title"]): out.append(r)
        except Exception as e:
            E.log("qa scan error", e)
    return out

# ───────────────────────── 회사 IR 홈페이지·로그미 ─────────────────────────
import urllib.parse
SKIP_HOST = ("kabutan", "minkabu", "japannext", "apple.com", "hrmos", "google", "sbisec", "twitter", "x.com", "facebook", "youtube", "line.me")
QA_LINK = re.compile(r"質疑応答|質疑|Q&A|Q＆A|(?<![A-Za-z])QA(?![A-Za-z])|主な質問|書き起こし|書起こし|文字起こし|スクリプト|Transcript|説明会[^<]{0,12}(要旨|内容|概要|記録)|スモールミーティング", re.I)

def company_site(code):
    s = E.mget(f"https://s.kabutan.jp/stocks/{code}/")
    for u in re.findall(r'href="(https?://[^"]+)"', s):
        host = urllib.parse.urlparse(u).netloc.lower()
        if host and not any(k in host for k in SKIP_HOST): return u
    return None

VIA_GW, NO_GW = set(), set()   # 관문으로 열리는 호스트 / 관문으로도 안 열리는 호스트
def _get(url, timeout=25):
    """회사 페이지 열기. GitHub 실행 환경(해외 데이터센터)의 접속을 막는 사이트(403·응답 없음)는
    즉시 응답 관문(Cloudflare Worker)의 /fetch 로 대신 열고, 그 호스트는 이번 실행 동안 계속 관문으로"""
    h = urllib.parse.urlparse(url).netloc.lower()
    if h not in VIA_GW:
        try:
            return http(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 Safari/605.1.15",
                                                         "Accept-Language": "ja,en;q=0.8"})
        except Exception as e:
            code = getattr(e, "code", None)
            blocked = code in (403, 451) or (code is None and re.search(r"timed out|reset|refused|closed|unreachable", str(e), re.I))
            if not blocked or h in NO_GW or not (E.GATEWAY_URL and E.GATEWAY_KEY): raise
            E.log("site blocked → gateway", h, str(e)[:60])
    try:
        b = http(E.GATEWAY_URL + "/fetch?u=" + urllib.parse.quote(url, safe=""), timeout=timeout + 20, headers={"x-key": E.GATEWAY_KEY})
    except Exception as e:
        NO_GW.add(h); VIA_GW.discard(h); E.log("gateway fetch error", h, str(e)[:60]); raise
    if h not in VIA_GW: VIA_GW.add(h); E.log("gateway fetch ok", h)
    return b

def _links(base, page):
    out = []
    for m in re.finditer(r'<a\b[^>]*href="([^"#]+)"[^>]*>(.*?)</a>', page, re.S | re.I):
        u = urllib.parse.urljoin(base, html.unescape(m.group(1)))
        t = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", m.group(2)))).strip()
        # 링크 앞뒤 날짜(YYYY年M月D日 / YYYY.MM.DD / YYYY/MM/DD)
        ctx = page[max(0, m.start() - 200):m.start()] + t + u
        d = None
        for y, mo, da in re.findall(r"(20\d\d)[年./-](\d{1,2})[月./-](\d{1,2})", ctx)[-1:]:
            d = f"{y}{int(mo):02d}{int(da):02d}"
        if not d:
            m8 = re.findall(r"(?<!\d)(20\d\d)(0[1-9]|1[0-2])([0-2]\d|3[01])(?!\d)", urllib.parse.unquote(u))
            if m8: d = "".join(m8[-1])
        if not d:  # 파일 이름의 YYMMDD(예: _261028_, _240625_)
            m6 = re.findall(r"(?<![0-9])(2\d)(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])(?![0-9])", urllib.parse.unquote(u).rsplit("/", 1)[-1])
            if m6: d = "20" + "".join(m6[-1])
        out.append((u, t, d))
    return out

MAT_LINK = re.compile(r"決算説明|説明会資料|説明資料|プレゼンテーション|Presentation|決算補足|補足資料|ファクトブック|Fact ?Book|データブック|Data ?Book|決算資料", re.I)

def stale_years(txt, since_day):
    """글·주소에 나오는 연도(2024, FY2024, 2024年3月期 등)가 모두 결산 연도보다 2년 이상 이전이면 True"""
    ys = [int(y) for y in re.findall(r"(?<!\d)(20[0-4]\d)(?!\d)", txt)]
    return bool(ys) and max(ys) < int(since_day[:4]) - 1

def doc_dates(kind, body):
    """자료 첫 부분에 적힌 날짜들(YYYYMMDD) — 옛 자료 걸러내기용"""
    if kind == "pdf":
        try:
            import io
            from pdfminer.high_level import extract_text
            body = E.nfkc(extract_text(io.BytesIO(body), maxpages=2))
        except Exception:
            return []
    head = body[:4000]
    out = [f"{y}{int(m):02d}{int(d):02d}" for y, m, d in re.findall(r"(20\d\d)\s*[年./-]\s*(\d{1,2})\s*[月./-]\s*(\d{1,2})", head)
           if 1 <= int(m) <= 12 and 1 <= int(d) <= 31]
    return out

_XJ = {}   # 종목코드 → xj-storage 회사 ID(ASxxxxx) 또는 ""
def xj_company(code, pages_html=()):
    """IR 자료 목록을 자바스크립트로 불러오는 xj-storage(QUICK) 회사 ID 찾기 — 페이지 본문·불러오는 스크립트에서"""
    if code in _XJ: return _XJ[code]
    cid = ""
    for body, base in pages_html:
        m = re.search(r"xj-storage\.jp/xcontents/(AS\d{4,6})", body) or re.search(r"company\s*[:=]\s*['\"](AS\d{4,6})['\"]", body)
        if m: cid = m.group(1); break
        for src in re.findall(r'<script[^>]+src="([^"]*(?:xj|Xj|XJ)[^"]*\.js[^"]*)"', body)[:3]:
            if "ProvideImage" in src: continue
            try:
                js = _get(urllib.parse.urljoin(base, src)).decode("utf-8", "ignore")
                m = re.search(r"company\s*:\s*['\"](AS\d{4,6})['\"]", js)
                if m: cid = m.group(1); break
            except Exception:
                pass
        if cid: break
    if cid or pages_html: _XJ[code] = cid
    return cid

def xj_docs(cid, since_day, pat):
    """xj-storage 공개 목록(JSON)에서 이번 결산 이후 자료 → [(제목, url, 날짜)]"""
    try:
        d = json.loads(_get(f"https://www.xj-storage.jp/public-list/GetList.aspx?company={cid}&len=80&output=json").decode("utf-8", "ignore"))
    except Exception as e:
        E.log("xj list error", cid, e); return []
    out = []
    for it in d.get("items") or []:
        t = E.nfkc(it.get("title") or ""); day = re.sub(r"\D", "", (it.get("publishDate") or ""))[:8]
        if not day or day < since_day or not pat.search(t): continue
        for f in it.get("files") or []:
            u = f.get("url") or ""
            if re.search(r"\.pdf($|\?)", u, re.I): out.append((t[:60], u, day)); break
    return out

IR_PAGE = re.compile(r"/ir|investor|kabunushi|stock|library|news|presentation|event|setsumei|kessan", re.I)
IR_TEXT = re.compile(r"IR|投資家|株主|ライブラリ|資料|ニュース|news|library|説明会|決算|プレゼン|イベント", re.I)

def render_pages(urls, limit=6):
    """자바스크립트로 목록을 그리는 IR 페이지를 실제 브라우저(헤드리스 크롬)로 열어 완성된 HTML → [(html, url)]"""
    try: from playwright.sync_api import sync_playwright
    except Exception: return []
    out = []
    try:
        with sync_playwright() as p:
            try: b = p.chromium.launch(channel="chrome")   # GitHub 실행 환경에 이미 있는 크롬
            except Exception: b = p.chromium.launch()
            pg = b.new_page(user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 Safari/605.1.15", locale="ja-JP")
            for u in urls[:limit]:
                try: pg.goto(u, wait_until="networkidle", timeout=20000)
                except Exception: pass
                try: out.append((E.nfkc(pg.content()), pg.url))
                except Exception: pass
            b.close()
    except Exception as e:
        E.log("render error", e)
    return out

def ir_site_docs(code, since_day, limit=2, pat=None, period=None):
    """회사 IR 홈페이지에서 이번 결산 이후의 질의응답·설명회 기록(pat 지정 시 그 자료) 링크 → [(이름, url, 날짜)]
    ① 일반 페이지 2단계 탐색 ② IR 자료 서비스 목록(xj-storage) ③ 그래도 이번 결산 이후 자료가 없으면 실제 브라우저로 IR 페이지를 열어 다시 탐색"""
    pat = pat or QA_LINK
    home = company_site(code)
    if not home: return []
    seen, pages, hits, bodies, irpages = set(), [home], [], [], []
    host = urllib.parse.urlparse(home).netloc

    def scan(p, body, depth):
        nxt = []
        for u, t, d in _links(p, body):
            h = urllib.parse.urlparse(u).netloc
            if pat.search(t) or pat.search(urllib.parse.unquote(u)):
                lm = re.search(r"finance\.logmi\.jp/(?:articles/)?(\d+)", u)
                if lm and not d:  # 회사 홈페이지의 로그미 기사 링크: 날짜가 안 적혀 있으면 로그미 최신 목록으로 확인
                    d = logmi_date(int(lm.group(1)))
                    if d == "old": continue  # 최신 목록(약 6주)보다 오래된 기사 = 지난 분기
                if d and d < since_day: continue
                if not d and stale_years(t + " " + urllib.parse.unquote(u), since_day): continue  # 날짜 없이 옛 연도만 보이는 자료(예: 2024년 설명회)
                if _bad(t, u, d): continue  # 영상만·채용·FAQ·행사 안내 등
                if not d and period and period_score(t + " " + urllib.parse.unquote(u), period) < 0: continue  # 다른 회계연도 자료
                hits.append((t[:60] or "질의응답 자료", u, d))
            elif depth == 0 and h == host and IR_PAGE.search(u) and IR_TEXT.search(t + u):
                nxt.append(u)
        return nxt

    for depth in range(2):
        nxt = []
        for p in pages[:10]:
            if p in seen: continue
            seen.add(p)
            try: body = E.nfkc(_get(p).decode("utf-8", "ignore"))
            except Exception: continue
            bodies.append((body, p))
            if depth: irpages.append(p)
            nxt += scan(p, body, depth)
        pages = list(dict.fromkeys(nxt))
    # 목록을 자바스크립트로 그리는 IR 페이지(xj-storage): 공개 목록에서 직접 찾음(PDF 직링크·게시일 확실 → 앞에 둠)
    cid = xj_company(code, bodies)
    if cid:
        xj = xj_docs(cid, since_day, pat)
        if xj: E.log("xj-storage", code, cid, len(xj))
        hits = [x for x in xj if not _bad(*x)] + hits
    if not any(d and d >= since_day for _, _, d in hits) and irpages:
        # 이번 결산 이후 날짜가 확인된 자료가 없음 → 자바스크립트로 그리는 목록일 수 있으니 브라우저로 열어 봄(자료실·설명회 페이지 우선)
        cand = sorted(dict.fromkeys(irpages), key=lambda u: -len(re.findall(r"library|presentation|event|setsumei|kessan|material|siryo|shiryo|briefing|meeting", u, re.I)))
        n0 = len(hits)
        cand = [u for u in cand if urllib.parse.urlparse(u).netloc.lower() not in VIA_GW]   # 해외 접속을 막는 사이트는 브라우저로도 안 열림
        for body, p in render_pages(cand, limit=6):
            scan(p, body, 1)
        if len(hits) > n0: E.log("rendered IR pages", code, len(hits) - n0)
    out, urls = [], set()
    # 순서: 이번 결산 이후 날짜 확인 → 이번 회계연도·분기가 제목에 있는 것 → 이번 회계연도 → 나머지 / 같은 순위면 최신·PDF 직링크 먼저
    def rank(x):
        t, u, d = x
        ps = period_score(t + " " + urllib.parse.unquote(u), period) if period else 0
        return (3 if d and d >= since_day else max(ps, 0), d or "0", bool(re.search(r"\.pdf($|\?)", u, re.I)))
    ranked = sorted(hits, key=rank, reverse=True)
    best = rank(ranked[0])[0] if ranked else 0
    for x in ranked:
        t, u, d = x
        if best > 0 and rank(x)[0] == 0 and len(out) >= 1: continue  # 확실한 자료가 있으면 불확실한 것으로 채우지 않음
        fk = urllib.parse.unquote(u).rstrip("/").split("/")[-1].lower()
        if u in urls or fk in urls: continue   # 같은 파일(주소만 조금 다른 링크) 중복
        urls.update((u, fk)); out.append((t, u, d))
    if out: E.log("ir site picks", code, " / ".join(f"{t[:25]}({d or '날짜 없음'})" for t, u, d in out[:limit]))
    return out[:limit]

TEXTISH = re.compile(r"書き起こし|書起こし|文字起こし|スクリプト|質疑|Q&A|Q＆A|Transcript|要旨|記録|ログミー|logmi", re.I)
def _bad(t, u, d):
    """질의응답·설명회 기록이 아닌 링크: 영상만, 채용·FAQ·제품 Q&A, 제목 없는 링크, 행사 개최 안내, (날짜 없는) 주주총회"""
    tu = t + " " + urllib.parse.unquote(u)
    if re.search(r"\.(mp4|mp3|m4a|wav)($|\?)|vimeo\.com|youtube\.com|youtu\.be", u, re.I): return True
    if re.search(r"動画|映像|配信|ムービー", t) and not TEXTISH.search(tu): return True
    if NOISE.search(t) or (not d and re.search(r"recruit|saiyo|/faq|/qa/?$|/q/", u, re.I)): return True
    if not d and (not t.strip() or re.fullmatch(r"\s*(Q&A|Q＆A|QA|質疑応答|よくあるご?質問)\s*", t, re.I)): return True
    if re.search(r"開催(の|に関する)?(お知らせ|ご案内)|開催について|開催予定", t) and not re.search(r"書き起こし|書起こし|文字起こし|スクリプト|要旨|記録|公開", t): return True
    if not d and "株主総会" in t: return True
    return False

def logmi_date(aid):
    """로그미 기사 번호 → 최신 목록에 있으면 개최일(YYYYMMDD), 목록의 가장 오래된 기사보다 번호가 작으면 "old", 모르면 None"""
    try: items = logmi_index()
    except Exception: return None
    ids = {int(re.search(r"(\d+)$", it["url"]).group(1)): it["d"] for it in items}
    if not ids: return None
    if aid in ids: return ids[aid]
    return "old" if aid < min(ids) else None

NOISE = re.compile(r"採用|リクルート|新卒|よくある|FAQ|製品|商品|サービスについて|お客様")
def period_score(txt, period):
    """제목·주소에 적힌 회계연도·분기가 이번 결산과 맞는지: 2=연도·분기 일치, 1=연도 일치, 0=정보 없음, -1=다른 연도"""
    fy_y, fem, n = period
    txt = E.nfkc(txt)
    yrs = [(int(y), int(m)) for y, m in re.findall(r"(20\d\d)\s*年\s*(\d{1,2})\s*月期", txt)]
    yrs += [(2000 + int(y), int(m)) for m, y in re.findall(r"FY\s*(\d{1,2})/(\d\d)", txt, re.I)]
    yrs += [(int(y), fem) for y in re.findall(r"FY\s*(20\d\d)", txt, re.I)]
    if not yrs: return 0
    if (fy_y, fem) not in yrs and fy_y not in [y for y, _ in yrs]: return -1
    qpat = r"通期|期末|第\s*4\s*四半期|4\s*Q|Q\s*4|年度決算|本決算|Full[- ]?Year" if n == 4 else \
           rf"第\s*{n}\s*四半期|{n}\s*Q|Q\s*{n}" + (r"|中間" if n == 2 else "")
    return 2 if re.search(qpat, txt, re.I) else 1

def card_period(card):
    """카드 → (회계연도 연도, 결산월, 분기) 예: 2026년 8월 결산 4분기 → (2026, 8, 4)"""
    try:
        fem = card.get("fem") or E.fy_end_month_of(card["r"])
        end = (card.get("r") or {}).get("end") or card["series"][-1]["end"]
        fy, n = E.fy_of(end[:7], fem)
        return 2000 + int(fy.split("/")[1]), fem, n
    except Exception:
        return None

_LOGMI = {"t": 0, "items": []}
def logmi_index(pages=8):
    """로그미 파이낸스 최신 설명회 기록 목록(약 6주치) — 사이트 검색이 종목으로 걸러지지 않아 최신 목록을 훑어 종목코드로 찾음(6시간 캐시)"""
    if time.time() - _LOGMI["t"] < 6 * 3600 and _LOGMI["items"]: return _LOGMI["items"]
    items = []
    for pg in range(1, pages + 1):
        try: s = E.nfkc(re.sub(r"<!--.*?-->", "", _get(f"https://finance.logmi.jp/search?page={pg}").decode("utf-8", "ignore"), flags=re.S))
        except Exception as e: E.log("logmi error", e); break
        n = 0
        for m in re.finditer(r'<a[^>]+href="(/articles/\d+)"[^>]*>(.*?)</a>', s, re.S):
            t = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", m.group(2)))).strip()
            c = re.search(r"\(\s*([0-9][0-9A-Z]{3})\s*\)", t)
            d = re.search(r"開催日\s*(20\d\d)/(\d{1,2})/(\d{1,2})", t)
            if not c: continue
            items.append({"code": c.group(1), "url": "https://finance.logmi.jp" + m.group(1), "t": t,
                          "d": f"{d.group(1)}{int(d.group(2)):02d}{int(d.group(3)):02d}" if d else None, "qa": "質疑" in t})
            n += 1
        if not n: break
    if items: _LOGMI.update(t=time.time(), items=items)
    return items

def logmi_docs(code, since_day, limit=1):
    """로그미 파이낸스의 같은 종목 결산 설명회 기록 기사"""
    out = []
    for it in logmi_index():
        if it["code"] != code or (it["d"] and it["d"] < since_day): continue
        out.append(("로그미 파이낸스 설명회 기록" + (" (질의응답 포함)" if it["qa"] else ""), it["url"], it["d"]))
    return sorted(out, key=lambda x: ("질의응답" in x[0], x[2] or ""), reverse=True)[:limit]

def fetch_doc(url, depth=0, pat=None):
    """→ ('pdf', bytes) 또는 ('text', 본문 문자열). 안내 글만 있는 페이지면 그 안의 PDF를 따라감"""
    b = _get(url, timeout=60)
    if b"%PDF" in b[:1024]: return "pdf", b   # 주소가 .pdf여도 실제로는 안내·오류 웹페이지일 수 있어 파일 머리로 판별
    raw = b.decode("utf-8", "ignore")
    if depth == 0:
        pdfs = [(u, t) for u, t, d in _links(url, E.nfkc(raw)) if re.search(r"\.pdf($|\?)", u, re.I)]
        pat = pat or QA_LINK
        pick = [p for p in pdfs if pat.search(p[1]) or pat.search(urllib.parse.unquote(p[0]))]
        if not pick: pdfs = []  # 목록 페이지의 엉뚱한 PDF(중기계획 등)를 따라가지 않음
        pick = pick or pdfs
        body = re.sub(r"<[^>]+>", " ", re.sub(r"<(script|style|nav|footer|header)\b.*?</\1>", " ", raw, flags=re.S | re.I))
        if pick and len(re.sub(r"\s+", " ", body)) < 6000:
            try: return fetch_doc(pick[0][0], depth=1, pat=pat)
            except Exception as e: E.log("pdf follow error", e)
    t = raw
    t = re.sub(r"<(script|style|nav|footer|header)\b.*?</\1>", " ", t, flags=re.S | re.I)
    t = E.nfkc(re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", t)))).strip()
    return "text", t[:30000]

def get_pdf(url, pat=None):
    """주소 → PDF 바이트. 웹페이지(예: IR 행사 안내 페이지)면 그 안의 결산 설명 자료 PDF를 따라감. PDF를 못 얻으면 None"""
    kind, body = fetch_doc(url, pat=pat or MAT_LINK)
    return body if kind == "pdf" else None

def other_qa(card):
    """TDnet 밖(회사 IR 홈페이지, 로그미)의 질의응답·설명회 기록 → [(이름, 종류, 내용, url)]"""
    since = card["d"]
    found = []
    per = card_period(card)
    for fn in (ir_site_docs, logmi_docs):
        try: found += fn(card["code"], since, period=per) if fn is ir_site_docs else fn(card["code"], since)
        except Exception as e: E.log("qa source error", fn.__name__, e)
    out, sigs = [], set()
    for name, url, d in found:
        if url in card.get("qa_urls", []): continue
        try:
            kind, body = fetch_doc(url)
            if kind == "text" and len(body) < 500: continue
            ds = [x for x in doc_dates(kind, body) if x <= E.now().strftime("%Y%m%d")]
            lim = (dt.datetime.strptime(since, "%Y%m%d") - dt.timedelta(days=45)).strftime("%Y%m%d")
            if ds and max(ds) < lim:  # 본문 날짜가 이번 결산보다 한참 전 → 옛 자료
                E.log("qa skip old doc", url, max(ds)); continue
            sig = (kind, len(body))
            if sig in sigs: continue  # 같은 PDF로 이어지는 링크 중복
            sigs.add(sig)
            out.append((name, kind, body, url))
        except Exception as e:
            E.log("qa fetch error", url, e)
    return out

def build(card, qa_rows=None):
    """→ (텔레그램 글 목록, Q&A 포함 여부)"""
    name = card.get("name_ko") or card["name"]
    docs, names = [], []
    for url, lab in ((card.get("mat"), "결산 설명 자료"), (card.get("pdf"), "결산단신")):
        if url:
            try:
                b = get_pdf(url)   # 설명 자료 주소가 웹페이지인 경우(회사 IR 홈페이지)가 있어 PDF로 따라감
                if b: docs.append(b); names.append(lab)
                else: E.log("ir pdf skip(not pdf)", url)
            except Exception as e: E.log("ir pdf error", e)
    if not card.get("mat"):  # TDnet에 설명 자료가 없으면 회사 IR 홈페이지에서
        try:
            for t, u, d in ir_site_docs(card["code"], card["d"], limit=4, pat=MAT_LINK, period=card_period(card)):
                if QA_LINK.search(t): continue
                kind, body = fetch_doc(u, pat=MAT_LINK)
                if kind == "pdf" and len(body) < 12_000_000: docs.insert(0, body); names.insert(0, "결산 설명 자료(회사 IR 홈페이지)"); break
        except Exception as e:
            E.log("ir site material error", e)
    qa_rows = qa_rows if qa_rows is not None else find_qa(card["code"], card["d"])
    for r in qa_rows[:2]:
        try: docs.append(http(r["pdf"], timeout=60)); names.append("질의응답(TDnet)")
        except Exception as e: E.log("qa pdf error", e)
    texts, links = [], []
    if not qa_rows:  # TDnet에 없으면 회사 IR 홈페이지·로그미에서 찾음
        for nm, kind, body, url in other_qa(card)[:2]:
            if kind == "pdf": docs.append(body)
            else: texts.append(f"[{nm}] {body}")
            names.append(nm); links.append(url)
            card.setdefault("qa_urls", []).append(url)
    keep, total = [], 0  # Gemini 요청 한도(약 20MB, base64 포함) 안으로: 질의응답·설명 자료 우선
    for d in docs:
        if total + len(d) <= 12_000_000: keep.append(d); total += len(d)
    docs = keep
    prompt = IR_PROMPT.format(name=name, code=card["code"])
    if texts: prompt += "\n\n아래는 회사 IR 홈페이지·설명회 기록 웹페이지 본문(질의응답이 있으면 qa에 반영):\n" + "\n\n".join(texts)
    out = gemini_pdfs(prompt, docs)
    if not out: return None, False
    return render(card, out, bool(qa_rows) or bool(links), names, links), bool(out.get("qa"))

def _e(s): return html.escape(str(s or ""), quote=False)

def render(card, o, has_qa, names, links=None):
    name = card.get("name_ko") or card["name"]
    fem = card.get("fem") or 3
    lab = ""
    try:
        lab, months = E.qlabel(card["series"][-1], fem); lab = f" ({lab}, {months})"
    except Exception: pass
    q = o.get("quality")
    dot = "🟢" if isinstance(q, (int, float)) and q >= 7 else "🟡" if isinstance(q, (int, float)) and q >= 5 else "🔴"
    L = [f"📊 <b>{_e(name)} ({card['code']}) 실적 발표{lab}</b>", f"발표: {card['d'][:4]}-{card['d'][4:6]}-{card['d'][6:]} {card['t']} JST", ""]
    if q: L.append(f"🎯 실적 퀄리티: <b>{q}/10</b> {dot} — {_e(o.get('quality_why'))}")
    rx = card.get("rx") if isinstance(card.get("rx"), dict) else None
    if rx: L.append("주가 반응: " + E.reaction_line(rx).replace("\n", " "))
    L.append("")
    if o.get("overview"): L += ["📮 <b>기업 개요</b>", _e(o["overview"]), ""]
    if o.get("key"): L += ["📊 <b>핵심 실적</b>"] + [f"- {_e(x)}" for x in o["key"]] + ["<i>(TDnet 결산단신·설명 자료 기준, Gemini 추출)</i>", ""]
    if o.get("message"): L += ["🗣 <b>경영진 핵심 메시지</b>", _e(o["message"]), ""]
    if o.get("surprise"): L += ["🔥 <b>서프라이즈 포인트</b>"] + [f"- <b>[{_e(s.get('title'))}]</b> {_e(s.get('body'))}" for s in o["surprise"] if isinstance(s, dict)] + [""]
    if o.get("kpi"): L += ["📝 <b>특이사항 &amp; 핵심 KPI</b>"] + [f"- {_e(x)}" for x in o["kpi"]] + [""]
    if o.get("guidance"): L += ["🚀 <b>가이던스</b>", _e(o["guidance"]), ""]
    qa = [x for x in (o.get("qa") or []) if isinstance(x, dict) and x.get("q")]
    if qa:
        L.append(f"📞 <b>Q&amp;A 요약</b> ({_e(o.get('qa_source') or '질의응답 자료')})")
        for i, x in enumerate(qa, 1):
            who = "·".join(_e(v) for v in (x.get("asker"), x.get("answerer")) if v)
            L += [f"<b>Q{i}</b>" + (f": {who}" if who else ""), f"- 질문: {_e(x['q'])}", f"- 답변: {_e(x['a'])}", ""]
    else:
        L += ["📞 <b>Q&amp;A</b> 아직 질의응답 자료를 찾지 못했습니다(TDnet·회사 IR 홈페이지·로그미 확인). 14일 동안 다시 확인해 올라오면 따로 보내드립니다.", ""]
    L.append(f"<i>자료: {', '.join(names)}. 투자 자문이 아닌 정보 정리입니다.</i>")
    for u in links or []: L.append(f'<a href="{_e(u)}">원문 링크</a>')
    return split("\n".join(L))

def split(text, n=3900):
    parts, cur = [], ""
    for block in text.split("\n\n"):
        if len(cur) + len(block) + 2 > n and cur:
            parts.append(cur); cur = ""
        cur += ("\n\n" if cur else "") + block
    if cur: parts.append(cur)
    return parts
