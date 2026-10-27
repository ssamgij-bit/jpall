"""[🗣 IR·Q&A] 버튼 → 결산 설명 자료·질의응답(Q&A) 중심 요약(텔레그램 글)

자료: TDnet에 올라온 결산단신 PDF, 결산 설명 자료 PDF, 질의응답 요지 PDF(발표 후 며칠 뒤 올라오는 경우가 많음).
질의응답 자료가 아직 없으면 그렇게 표시하고, 14일 안에 TDnet에 올라오면 Q&A 부분만 따로 다시 보냄.
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
    for model in GEMINI_MODELS:
        try:
            r = json.loads(http(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}",
                                timeout=240, data=body, headers={"content-type": "application/json"}))
            txt = "".join(p.get("text", "") for p in r["candidates"][0]["content"]["parts"])
            out = json.loads(re.sub(r"^```(json)?|```$", "", txt.strip()))
            if isinstance(out, list): out = out[0] if out and isinstance(out[0], dict) else None
            return out
        except Exception as e:
            E.log("gemini ir error", model, str(e)[:150])
            if "429" in str(e): time.sleep(5)
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

def build(card, qa_rows=None):
    """→ (텔레그램 글 목록, Q&A 포함 여부)"""
    name = card.get("name_ko") or card["name"]
    docs, names = [], []
    for url, lab in ((card.get("mat"), "결산 설명 자료"), (card.get("pdf"), "결산단신")):
        if url:
            try: docs.append(http(url, timeout=60)); names.append(lab)
            except Exception as e: E.log("ir pdf error", e)
    qa_rows = qa_rows if qa_rows is not None else find_qa(card["code"], card["d"])
    for r in qa_rows[:2]:
        try: docs.append(http(r["pdf"], timeout=60)); names.append("질의응답")
        except Exception as e: E.log("qa pdf error", e)
    out = gemini_pdfs(IR_PROMPT.format(name=name, code=card["code"]), docs)
    if not out: return None, False
    return render(card, out, bool(qa_rows), names), bool(out.get("qa"))

def _e(s): return html.escape(str(s or ""), quote=False)

def render(card, o, has_qa, names):
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
        L += ["📞 <b>Q&amp;A</b> 아직 TDnet에 질의응답 자료가 없습니다. 14일 안에 올라오면 Q&amp;A만 따로 보내드립니다.", ""]
    L.append(f"<i>자료: {', '.join(names)} (TDnet). 투자 자문이 아닌 정보 정리입니다.</i>")
    return split("\n".join(L))

def split(text, n=3900):
    parts, cur = [], ""
    for block in text.split("\n\n"):
        if len(cur) + len(block) + 2 > n and cur:
            parts.append(cur); cur = ""
        cur += ("\n\n" if cur else "") + block
    if cur: parts.append(cur)
    return parts
