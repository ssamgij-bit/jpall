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

QA_RE = re.compile(r"質疑応答|質疑|Q&A|Q＆A|主な質問|説明会.*(要旨|内容)|書き起こし|スモールミーティング")

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

def gemini_pdfs(prompt, pdfs):
    key = os.environ.get("GEMINI_API_KEY")
    if not key or not pdfs: return None
    parts = [{"inline_data": {"mime_type": "application/pdf", "data": base64.b64encode(b).decode()}} for b in pdfs] + [{"text": prompt}]
    body = json.dumps({"contents": [{"parts": parts}],
                       "generationConfig": {"temperature": 0.2, "maxOutputTokens": 12000, "responseMimeType": "application/json"}}).encode()
    for model in GEMINI_MODELS + GEMINI_MODELS:  # 구글 서버가 붐비면(503) 한 바퀴 더
        try:
            r = json.loads(http(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}",
                                timeout=240, data=body, headers={"content-type": "application/json"}))
            txt = "".join(p.get("text", "") for p in r["candidates"][0]["content"]["parts"])
            out = json.loads(re.sub(r"^```(json)?|```$", "", txt.strip()))
            if isinstance(out, list): out = out[0] if out and isinstance(out[0], dict) else None
            return out
        except Exception as e:
            E.log("gemini ir error", model, str(e)[:150])
            if "429" in str(e) or "503" in str(e) or "timed out" in str(e): time.sleep(8)
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
QA_LINK = re.compile(r"質疑応答|質疑|Q&A|Q＆A|QA|主な質問|書き起こし|書起こし|文字起こし|Transcript|説明会[^<]{0,12}(要旨|内容|概要|記録|動画)|スモールミーティング", re.I)

def company_site(code):
    s = E.mget(f"https://s.kabutan.jp/stocks/{code}/")
    for u in re.findall(r'href="(https?://[^"]+)"', s):
        host = urllib.parse.urlparse(u).netloc.lower()
        if host and not any(k in host for k in SKIP_HOST): return u
    return None

def _get(url, timeout=25):
    return http(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 Safari/605.1.15",
                                                 "Accept-Language": "ja,en;q=0.8"})

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

def ir_site_docs(code, since_day, limit=2, pat=None):
    """회사 IR 홈페이지에서 이번 결산 이후의 질의응답·설명회 기록(pat 지정 시 그 자료) 링크 → [(이름, url, 날짜)]"""
    pat = pat or QA_LINK
    home = company_site(code)
    if not home: return []
    seen, pages, hits = set(), [home], []
    host = urllib.parse.urlparse(home).netloc
    for depth in range(2):
        nxt = []
        for p in pages[:8]:
            if p in seen: continue
            seen.add(p)
            try: body = E.nfkc(_get(p).decode("utf-8", "ignore"))
            except Exception: continue
            for u, t, d in _links(p, body):
                h = urllib.parse.urlparse(u).netloc
                if pat.search(t) or pat.search(urllib.parse.unquote(u)):
                    if re.search(r"\.(mp4|mp3|m4a|wav)($|\?)", u, re.I): continue  # 음성·영상
                    if d and d < since_day: continue
                    if not d and stale_years(t + " " + urllib.parse.unquote(u), since_day): continue  # 날짜 없이 옛 연도만 보이는 자료(예: 2024년 설명회)
                    hits.append((t[:60] or "질의응답 자료", u, d))
                elif depth == 0 and h == host and re.search(r"/ir|investor|kabunushi|stock|library|news", u, re.I) and \
                        re.search(r"IR|投資家|株主|ライブラリ|資料|ニュース|news|library", t + u, re.I):
                    nxt.append(u)
        pages = list(dict.fromkeys(nxt))
    out, urls = [], set()
    # 날짜가 있는 것(최신 순) → PDF 직링크 → 나머지(목록 페이지 등)
    for t, u, d in sorted(hits, key=lambda x: (x[2] or "0", bool(re.search(r"\.pdf($|\?)", x[1], re.I))), reverse=True):
        if not d and any(h[2] for h in hits if h[1] != u) and len(out) >= 1: continue  # 날짜 확인된 자료가 있으면 날짜 모르는 것은 뒤로(채우지 않음)
        if u in urls: continue
        urls.add(u); out.append((t, u, d))
    return out[:limit]

def logmi_docs(code, since_day, limit=1):
    """로그미 파이낸스의 같은 종목 결산 설명회 기록 기사"""
    try:
        s = E.nfkc(re.sub(r"<!--.*?-->", "", _get(f"https://finance.logmi.jp/search?q={code}").decode("utf-8", "ignore"), flags=re.S))
    except Exception as e:
        E.log("logmi error", e); return []
    out = []
    for m in re.finditer(r'<a[^>]+href="(/articles/\d+)"[^>]*>(.*?)</a>', s, re.S):
        t = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", m.group(2)))).strip()
        if f"({code})" not in t: continue
        d = re.search(r"開催日\s*(20\d\d)/(\d{1,2})/(\d{1,2})", t)
        d = f"{d.group(1)}{int(d.group(2)):02d}{int(d.group(3)):02d}" if d else None
        if d and d < since_day: continue
        out.append(("로그미 파이낸스 설명회 기록" + (" (질의응답 포함)" if "質疑応答" in t else ""), "https://finance.logmi.jp" + m.group(1), d))
    return out[:limit]

def fetch_doc(url, depth=0, pat=None):
    """→ ('pdf', bytes) 또는 ('text', 본문 문자열). 안내 글만 있는 페이지면 그 안의 PDF를 따라감"""
    b = _get(url, timeout=60)
    if b[:4] == b"%PDF" or url.lower().endswith(".pdf"): return "pdf", b
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

def other_qa(card):
    """TDnet 밖(회사 IR 홈페이지, 로그미)의 질의응답·설명회 기록 → [(이름, 종류, 내용, url)]"""
    since = card["d"]
    found = []
    for fn in (ir_site_docs, logmi_docs):
        try: found += fn(card["code"], since)
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
            try: docs.append(http(url, timeout=60)); names.append(lab)
            except Exception as e: E.log("ir pdf error", e)
    if not card.get("mat"):  # TDnet에 설명 자료가 없으면 회사 IR 홈페이지에서
        try:
            for t, u, d in ir_site_docs(card["code"], card["d"], limit=4, pat=MAT_LINK):
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
