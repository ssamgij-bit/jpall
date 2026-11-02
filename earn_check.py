"""AI(Gemini)가 쓴 문장 속 금액을 원자료와 자동 대조

- 대상: 리포트 '기업 개요'(최근 회계연도 연간 실적), 리포트 결론·설명과 IR 요약 핵심 실적(이번 3개월 분기)
- 원자료: 가부탄 연간 실적(결산단신 기준) · 결산단신 XBRL로 만든 분기 시계열
- '매출 769억 엔'처럼 항목 이름 바로 뒤에 붙은 억 엔 금액만 본다. 예상·계획·전년·누적 같은 말이 앞에 있으면 다른 기간 숫자라 건너뜀
- 1%를 넘게 다르면(그리고 0.5억 엔 넘게) 문장 뒤에 '⚠️ 원자료와 다름(원자료 …)'을 붙임
"""
import re

KEYS = [("매출", r"매출(?:액|수익)?"), ("영업이익", r"영업\s?이익"), ("경상이익", r"경상\s?이익"), ("순이익", r"(?:지배주주\s?)?(?:당기\s?)?순이익")]
NUM = r"(-?[\d,]+(?:\.\d+)?)\s*억\s*엔"
SKIP_Q = re.compile(r"예상|계획|가이던스|목표|전년|전기|작년|누적|연간|통기|상반기|반기|컨센서스|중기|FY\d")
SKIP_Y = re.compile(r"예상|계획|가이던스|목표|전년|전기|작년|분기|상반기|반기|컨센서스|중기")


def _f(x):
    try: return float(str(x).replace(",", ""))
    except Exception: return None


SEG_WORD = re.compile(r"부문|사업부|세그먼트|사업\s?(?:의\s?)?$")


def check(text, facts, mode="q", fy=None, others=(), names=()):
    """others: 다른 정당한 값들(세그먼트·전년 동기, 억 엔) — 이것과 맞으면 전사 합계가 아닌 숫자로 보고 건너뜀
    names: 세그먼트 이름 — 숫자 앞 25자 안에 있으면 세그먼트 숫자로 보고 건너뜀"""
    """text 안 금액을 facts({항목: 억 엔})와 대조 → (표시를 붙인 문장, 일치 수, 불일치 목록)"""
    if not text or not facts: return text, 0, []
    ok, bad = 0, []
    for name, kx in KEYS:
        if facts.get(name) is None: continue
        for m in re.finditer(kx + r"[^0-9。\n]{0,14}?" + NUM, text):
            v = _f(m.group(1))
            if v is None: continue
            pre = text[max(0, m.start() - 18):m.start()] + text[m.start():m.start(1)]
            if mode == "q" and SKIP_Q.search(pre): continue
            near = re.split(r"[.。,，;]", text[max(0, m.start() - 25):m.start()])[-1]   # 같은 구절 안만
            if SEG_WORD.search(near) or any(n_ and n_ in near for n_ in names): continue   # 세그먼트(부문) 숫자
            if any(o_ is not None and abs(v - o_) <= max(0.5, abs(o_) * 0.01) for o_ in others): continue
            if mode == "y":
                if SKIP_Y.search(pre): continue
                fys = re.findall(r"FY\d{1,2}/\d{2}", text[max(0, m.start() - 60):m.start()])
                if fys and fy and fys[-1] != fy: continue   # 다른 회계연도 숫자
            a = facts[name]
            if abs(v - a) <= max(0.5, abs(a) * 0.01): ok += 1; continue
            bad.append((m.end(), f"{name} {v:,.1f}억 엔 ↔ 원자료 {a:,.1f}억 엔"))
    if not bad: return text, ok, []
    # 문장 끝(마침표·줄 끝)에 표시
    marks = {}
    for end, msg in bad:
        j = min([x for x in (text.find(".", end), text.find("。", end), text.find("\n", end)) if x >= 0] or [len(text)])
        marks.setdefault(j, []).append(msg)
    for j in sorted(marks, reverse=True):
        text = text[:j + 1 if j < len(text) else j] + f" ⚠️ 원자료와 다름({'; '.join(marks[j])})" + text[j + 1 if j < len(text) else j:]
    return text, ok, [m for _, m in bad]


def facts_quarter(card):
    """이번 3개월 분기 실적(억 엔) — 카드 분기 시계열(결산단신 XBRL·가부탄)"""
    try:
        q = (card.get("series") or [])[-1]
        return {"매출": q.get("rev") / 100 if q.get("rev") is not None else None,
                "영업이익": q.get("op") / 100 if q.get("op") is not None else None,
                "순이익": q.get("ni") / 100 if q.get("ni") is not None else None}
    except Exception:
        return {}


def facts_year(kb, end):
    """가부탄 연간 실적 중 회계연도 끝(YYYY-MM)이 end인 해(억 엔) → (facts, 'FY3/26')"""
    try:
        y = next((y for y in reversed(kb.get("y") or []) if not y.get("fc") and str(y.get("end", ""))[:7] == str(end)[:7]), None)
        if not y: return {}, None
        lab = f"FY{int(str(y['end'])[5:7])}/{str(y['end'])[2:4]}"
        g = lambda k: y.get(k) / 100 if y.get(k) is not None else None
        return {"매출": g("rev"), "영업이익": g("op"), "경상이익": g("ord") if "ord" in y else None, "순이익": g("ni")}, lab
    except Exception:
        return {}, None
