"""리포트 맨 앞 '기업 개요'와 '주주 구성' — EDINET 최신 유가증권보고서(有価証券報告書) 기준

- 연혁(沿革)·사업 내용(事業の内容)·세그먼트 정보 본문 → Gemini가 한국어 개요 3문단(설립·상장 → 사업 변천 → 지금의 사업 구성과 숫자)
- 대주주 상위 10(大株主の状況, XBRL 개별 값) + 소유자별 분포(所有者別状況 표 → Gemini가 비율만 읽음)
- 문서 ID별로 STATE_DIR/edinet/prof/종목코드.json 에 저장(다음 유가증권보고서가 나올 때까지 재사용, AI 호출 없음)
"""
import os, re, io, csv, json, time, zipfile, html as _html

import earn as E
import holdings as Hd
import earn_edinet as ED

VER = 1
DIR = os.path.join(E.STATE_DIR, "edinet", "prof")

PROMPT = """너는 일본 중소형주를 보는 한국 바이사이드 애널리스트다. 아래는 {name}({code})의 최신 유가증권보고서({pe} 결산) 일부(일본어)다.
이것만 근거로 한국어 JSON을 써라. 자료에 없는 사실·숫자는 쓰지 않는다. 일본어를 그대로 쓰지 않는다(회사·제품명은 한국어 통용 표기, 필요하면 괄호에 영문).

1) overview: 기업 개요 3문단(각 2~4문장, 문자열 배열).
   - 1문단: 설립 연도와 배경(어디서 분리·독립했는지 등), 상장 연혁(처음 상장한 시장·연도 → 이후 시장 이전 → 지금 시장 구분).
   - 2문단: 무엇으로 시작해 어떤 사업으로 넓혀 왔는지(연도와 함께). 핵심 제품이 무엇에 쓰이는지 한 문장으로 풀어 설명.
   - 3문단: 지금의 사업(세그먼트) 구성과 주력 제품. 최근 회계연도 매출·영업이익과 세그먼트별 비중(매출 비중 %, 영업이익 비중), 세그먼트 이익률을 세그먼트 표 숫자로 계산해 쓴다.
     예) "FY3/26 기준으로 매출액 769억 엔 가운데 전자재료가 54%, 영업이익 189억 엔 가운데 약 4분의 3을 차지해 사실상 반도체 소재 기업으로 바뀌었습니다. 전자재료 사업의 영업이익률은 38.4%에 이릅니다."
     금액은 억 엔(1억 엔 = 100백만 엔), 회계연도는 FY결산월/연도 두 자리(예: FY3/26). 문체는 '~습니다'.
2) owners: 소유자별 분포(所有者別状況)의 '소유 주식 수 비율(%)' 행. [{{"cat": "금융기관|증권회사|기타 법인|외국 법인 등|개인·기타|정부·지자체", "pct": 숫자}}]. 자기주식은 개인·기타에 포함돼 있으면 그대로 둔다. 표가 없으면 [].
3) owners_note: 소유자별 분포 기준일(예: 2026-03-31)과 특이사항 한 줄(예: 자기주식 비율). 없으면 "".
4) names: 아래 대주주 이름 목록의 한국어 표기 {{"원문": "한국어"}}. 신탁은행 신탁 계정은 '(신탁 계정)'으로.

대주주 이름 목록: {names}

[연혁]
{hist}

[사업 내용]
{biz}

[세그먼트 정보]
{seg}

[소유자별 분포]
{own}

출력(JSON 하나): {{"overview": ["", "", ""], "owners": [], "owners_note": "", "names": {{}}}}
"""


def _csv(blob):
    """EDINET CSV zip → {요소명: [(컨텍스트, 값)]} — 문단(텍스트 블록) 안의 줄바꿈까지 살리도록 csv 모듈로 읽음"""
    out = {}
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        for n in z.namelist():
            if not n.lower().endswith(".csv"): continue
            raw = z.read(n)
            try: txt = raw.decode("utf-16")
            except Exception: txt = raw.decode("utf-8", "ignore")
            rows = list(csv.reader(io.StringIO(txt), delimiter="\t"))
            if not rows: continue
            head = rows[0]
            ie = next((i for i, h in enumerate(head) if "要素ID" in h), 0)
            ic = next((i for i, h in enumerate(head) if "コンテキストID" in h), 2)
            iv = next((i for i, h in enumerate(head) if h.strip() == "値"), len(head) - 1)
            for r in rows[1:]:
                if len(r) <= max(ie, ic, iv): continue
                out.setdefault(r[ie].split(":")[-1], []).append((r[ic], r[iv]))
    return out


def _text(d, rx, n=3000):
    for el, vals in d.items():
        if re.search(rx, el) and el.endswith("TextBlock"):
            for _, v in vals:
                v = re.sub(r"<[^>]+>", " ", _html.unescape(v or ""))
                v = re.sub(r"[ \t　]+", " ", v)
                v = re.sub(r"\n\s*\n+", "\n", v).strip()
                if len(v) > 30: return v[:n]
    return ""


def _holders(d):
    """대주주 상위 10: [(이름, 주식 수, 비율%)] — 컨텍스트 'No1MajorShareholdersMember' 등"""
    rows = {}
    for el, vals in d.items():
        for ctx, v in vals:
            m = re.search(r"No(\d+)MajorShareholdersMember", ctx)
            if not m or "Prior" in ctx: continue
            k = int(m.group(1)); r = rows.setdefault(k, {})
            if el.startswith("Name"): r["name"] = E.nfkc(v).strip()
            elif "Ratio" in el: r["pct"] = Hd.num(v)
            elif "NumberOfShares" in el: r["sh"] = Hd.num(v)
    out = []
    for k in sorted(rows):
        r = rows[k]
        if not r.get("name"): continue
        p = r.get("pct")
        if p is not None and p < 1: p *= 100   # 0.123 처럼 소수로 온 경우
        out.append((r["name"], r.get("sh"), p))
    return out[:10]


def get(card, name, budget_s=60):
    """→ {"overview": [...], "owners": [...], "owners_note": "", "holders": [(이름 한국어, 주식 수, %)], "pe": 기준 결산, "doc": 문서 ID} 또는 {}"""
    code = card["code"]
    if not ED.KEY(): return {}
    try:
        ED.history(code, card.get("fem") or 3, budget_s=budget_s)   # 유가증권보고서 목록(저장분 우선) — 주주환원 10년과 같은 저장분
        c = json.load(open(os.path.join(ED.DIR, "ret", f"{code}.json")))
    except Exception as e:
        E.log("profile edinet list error", code, e); return {}
    docs = sorted((k, v) for k, v in (c.get("docs") or {}).items() if v and not str(v).startswith("none:"))
    if not docs: return {}
    fy, doc = docs[-1]
    p = os.path.join(DIR, f"{code}.json")
    try:
        old = json.load(open(p))
        if old.get("doc") == doc and old.get("v") == VER and old.get("overview"): return old
    except Exception: pass
    try:
        d = _csv(Hd.get(ED.API + f"documents/{doc}?type=5&Subscription-Key={ED.KEY()}", timeout=120))
    except Exception as e:
        E.log("profile edinet csv error", code, doc, e); return {}
    hist = _text(d, r"CompanyHistory", 3500)
    biz = _text(d, r"DescriptionOfBusiness", 3000)
    seg = _text(d, r"SegmentInformation", 3500)
    own = _text(d, r"ShareholdingByShareholderCategory|ShareholderCategory|ShareholdersByCategory", 2000)
    hs = _holders(d)
    E.log("기업 개요 자료", code, doc, f"연혁 {len(hist)}자 · 사업 {len(biz)}자 · 세그먼트 {len(seg)}자 · 소유자별 {len(own)}자 · 대주주 {len(hs)}명")
    if not (hist or biz): return {}
    from jpall import gemini_json
    out = gemini_json(PROMPT.format(name=name, code=code, pe=fy, names=json.dumps([h[0] for h in hs], ensure_ascii=False),
                                    hist=hist or "(없음)", biz=biz or "(없음)", seg=seg or "(없음)", own=own or "(없음)"), timeout=150) or {}
    if isinstance(out, list): out = out[0] if out and isinstance(out[0], dict) else {}
    ov = [str(x).strip() for x in (out.get("overview") or []) if str(x).strip()]
    if not ov: return {}
    nm = out.get("names") or {}
    res = {"v": VER, "doc": doc, "pe": fy, "t": time.time(), "overview": ov[:4],
           "owners": [x for x in (out.get("owners") or []) if isinstance(x, dict) and Hd.num(x.get("pct")) is not None],
           "owners_note": str(out.get("owners_note") or ""),
           "holders": [(nm.get(n) or n, sh, pct) for n, sh, pct in hs]}
    if not E.ai_used_light():   # 가벼운 모델 결과는 저장하지 않음(다음에 상위 모델로 다시)
        try:
            os.makedirs(DIR, exist_ok=True); json.dump(res, open(p, "w"), ensure_ascii=False)
        except Exception as e: E.log("profile save error", e)
    return res


OWN_COL = {"금융기관": "#2f6fd6", "증권회사": "#7aa5ea", "기타 법인": "#e8743b", "외국 법인 등": "#2e7d32", "개인·기타": "#9aa4b2", "정부·지자체": "#6b7280"}


def html(pf, esc, fallback_overview="", fallback_holders=None, trm=None):
    """기업 개요 + 주주 구성 HTML"""
    H = []
    ov = (pf or {}).get("overview") or []
    if ov:
        H.append("<h2>기업 개요</h2><div class=card>" + "".join(f"<p>{esc(x)}</p>" for x in ov)
                 + f"<p class=muted>출처: EDINET 유가증권보고서({esc(pf.get('pe'))} 결산) 연혁·사업 내용·세그먼트 정보, Gemini 요약(원문에 있는 사실·숫자만)</p></div>")
    elif fallback_overview:
        H.append(f"<h2>기업 개요</h2><div class=card><p>{esc(fallback_overview)}</p></div>")
    own = [(str(x.get("cat")), Hd.num(x.get("pct"))) for x in (pf or {}).get("owners") or []]
    own = [(c, v) for c, v in own if v and v > 0]
    hs = (pf or {}).get("holders") or []
    if own or hs or fallback_holders:
        H.append("<h3>주주 구성</h3>")
        if own:
            tot = sum(v for _, v in own) or 100
            H.append("<div class=ownbar>" + "".join(
                f"<div style='width:{v / tot * 100:.2f}%;background:{OWN_COL.get(c, '#b9b6ae')}' title='{esc(c)} {v:.1f}%'>{esc(c) + ' ' + f'{v:.1f}%' if v / tot >= .08 else ''}</div>"
                for c, v in own) + "</div><div class=ownleg>" + "".join(
                f"<span><i style='background:{OWN_COL.get(c, '#b9b6ae')}'></i>{esc(c)} <b>{v:.1f}%</b></span>" for c, v in own) + "</div>"
                + (f"<p class=muted>소유자별 분포(주식 수 비율) · {esc(pf.get('owners_note'))}</p>" if pf.get("owners_note") else "<p class=muted>소유자별 분포(주식 수 비율)</p>"))
        if hs:
            top = sum(p for _, _, p in hs if p) or 0
            H.append("<div class=scroll><table><tr><th>#</th><th>대주주</th><th>보유 주식 수</th><th>지분율</th></tr>" + "".join(
                f"<tr><td>{i}</td><td style='text-align:left;white-space:normal'>{esc(n)}</td><td>{f'{sh / 1000:,.0f}천 주' if sh else '—'}</td><td><b>{f'{p:.2f}%' if p is not None else '—'}</b></td></tr>"
                for i, (n, sh, p) in enumerate(hs, 1)) + "</table></div>"
                f"<p class=muted>상위 {len(hs)}명 합계 {top:.1f}% · 지분율은 자기주식을 뺀 발행주식 기준 · 출처: EDINET 유가증권보고서({esc(pf.get('pe'))} 결산 기준일)</p>")
        elif fallback_holders:
            H.append("<div class=scroll><table><tr><th>대주주</th><th>지분율</th></tr>" + "".join(
                f"<tr><td style='text-align:left'>{esc((trm or {}).get(h[0], h[0]))}</td><td>{esc(h[1])}%</td></tr>" for h in fallback_holders)
                + "</table></div><p class=muted>출처: 가부탄(최근 보고 기준)</p>")
    return "".join(H)


CSS = (".ownbar{display:flex;height:30px;border-radius:8px;overflow:hidden;margin:6px 0;font-size:11.5px;color:#fff;font-weight:600}"
       " .ownbar>div{display:flex;align-items:center;justify-content:center;white-space:nowrap;overflow:hidden}"
       " .ownleg{display:flex;flex-wrap:wrap;gap:4px 14px;font-size:12.5px;margin:4px 0} .ownleg i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:4px}")
