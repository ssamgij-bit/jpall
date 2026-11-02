"""리포트 보강 두 가지
1) 분기 컨센서스 대비 — 발표 전에 받아 둔 Yahoo 애널리스트 평균(이번 분기 매출) + Yahoo 분기 EPS 예상·실적
2) 지난 리포트 대비 달라진 점 — 리포트를 만들 때마다 핵심 값(가이던스·주당배당 예상·캐파·주주환원 목표·대주주·결론)을 종목별로 저장하고,
   같은 종목의 다음 분기 리포트에서 바뀐 것만 맨 앞에 보여줌 (STATE_DIR/repsnap/종목코드.json)
"""
import os, json, time, datetime as dt

import earn as E

SNAP_DIR = os.path.join(E.STATE_DIR, "repsnap")


def _announce_ts(card):
    try:
        hh, mm = (card.get("t") or "15:00").split(":")[:2]
        d = dt.datetime(int(card["d"][:4]), int(card["d"][4:6]), int(card["d"][6:8]), int(hh), int(mm), tzinfo=E.JST)
        return d.timestamp()
    except Exception:
        return None


def cons_quarter(card, q, esc):
    """→ (HTML, 한 줄 요약) — 없으면 ("", "")"""
    rows, short = [], []
    # 매출: 발표 전에 받아 둔 이번 분기(0q) 평균만 씀(발표 뒤에 받은 값은 다음 분기 예상일 수 있음)
    try:
        c = (E.jp_state().get("cons") or {}).get(card["code"]) or {}
        ts0 = _announce_ts(card)
        if c.get("rev") and q.get("rev") and ts0 and c.get("ts", 0) < ts0 and ts0 - c["ts"] < 86400 * 8:
            cr = c["rev"] / 1e6; d_ = (q["rev"] / cr - 1) * 100
            rows.append(f"<tr><td>매출</td><td>{E.oku(q['rev'])}</td><td>{E.oku(cr)}</td><td><b>{d_:+.1f}%</b></td><td>{c.get('rev_n') or '?'}명 · {time.strftime('%Y-%m-%d', time.localtime(c['ts']))} 수집</td></tr>")
            short.append(f"매출 컨센서스 대비 {d_:+.1f}%")
    except Exception as e:
        E.log("cons q rev error", e)
    # EPS: Yahoo 분기 실적 기록(예상·실제 모두 Yahoo 값) — 분기 끝 달이 같은 것만
    try:
        import yfinance as yf
        eh = yf.Ticker(card["code"] + ".T").earnings_history
        if eh is not None and len(eh):
            end = (q.get("end") or card["r"].get("end") or "")[:7]
            for idx, row in eh.iterrows():
                if str(idx)[:7] != end: continue
                est, act = row.get("epsEstimate"), row.get("epsActual")
                if est == est and act == act and est is not None and act is not None and est:
                    d_ = (act / est - 1) * 100 if est > 0 else None
                    rows.append(f"<tr><td>EPS(주당순이익)</td><td>{act:,.1f}엔</td><td>{est:,.1f}엔</td><td><b>{f'{d_:+.1f}%' if d_ is not None else '—'}</b></td><td>Yahoo 분기 실적 기록</td></tr>")
                    if d_ is not None: short.append(f"EPS 컨센서스 대비 {d_:+.1f}%")
                break
    except Exception as e:
        E.log("cons q eps error", e)
    if not rows:
        return ("<div class=card><b>분기 컨센서스 대비</b> <span class=muted>— 발표 전 이번 분기 애널리스트 예상(Yahoo)이 없습니다(커버리지가 없거나 적은 종목).</span></div>", "")
    tone = ""
    try:
        ds = [float(x.split()[-1].rstrip("%")) for x in short]
        tone = " — 시장 기대 상회" if min(ds) > 3 else " — 시장 기대 하회" if max(ds) < -3 else " — 시장 기대 부합" if all(abs(x) <= 3 for x in ds) else " — 항목별로 엇갈림"
    except Exception: pass
    html_ = (f"<div class=card><b>분기 컨센서스 대비{tone}</b><div class=scroll><table><tr><th>항목</th><th>실제</th><th>컨센서스</th><th>차이</th><th>기준</th></tr>"
             + "".join(rows) + "</table></div><p class=muted>컨센서스: Yahoo Finance 애널리스트 평균. 매출은 발표 전에 받아 둔 이번 분기 예상, EPS는 Yahoo 분기 실적 기록의 예상·실제. ±3% 안이면 부합으로 봄. 영업이익 컨센서스는 무료 출처가 없어 빠짐</p></div>")
    return html_, " · ".join(short)


# ───────────────────────── 지난 리포트 대비 달라진 점 ─────────────────────────
def snapshot(card, fc, ret, capa, policy, holders, headline, label, hsrc=""):
    """이번 리포트의 핵심 값(비교용) — 금액 백만 엔"""
    return {"d": card["d"], "label": label, "t": time.time(), "headline": headline or "",
            "fc": {k: (fc.get(k) / 1e6 if fc.get(k) is not None else None) for k in ("rev", "op", "ni")}, "fc_end": (card["r"].get("fc_end") or "")[:7],
            "dps_fc": (ret or {}).get("dps_fc"),
            "capa": ((capa or {}).get("title") or "") + (" — " + capa["takeaway"] if (capa or {}).get("takeaway") else ""),
            "policy": sorted({f"{x.get('metric')} {x.get('target')}".strip() for x in (policy or []) if isinstance(x, dict) and x.get("target")}),
            "holders": [[str(h[0]), (float(h[2]) if h[2] is not None else None)] for h in (holders or [])][:10], "hsrc": hsrc}


def diff_html(code, cur, esc):
    """같은 종목의 이전 리포트(이번과 다른 발표일 중 가장 최근)와 비교 → (HTML, 바뀐 항목 수). 저장도 함께"""
    p = os.path.join(SNAP_DIR, f"{code}.json")
    try: hist = json.load(open(p))
    except Exception: hist = []
    prev = next((h for h in reversed(hist) if h.get("d") < cur["d"]), None)
    hist = [h for h in hist if h.get("d") != cur["d"]] + [cur]
    try:
        os.makedirs(SNAP_DIR, exist_ok=True); json.dump(hist[-8:], open(p, "w"), ensure_ascii=False)
    except Exception as e: E.log("repsnap save error", e)
    if not prev: return "", 0
    L = []
    # 가이던스(같은 회계연도면 수정, 다른 회계연도면 새 연도)
    same_fy = prev.get("fc_end") == cur.get("fc_end")
    for k, nm in (("rev", "매출"), ("op", "영업이익"), ("ni", "순이익")):
        a, b = (prev.get("fc") or {}).get(k), (cur.get("fc") or {}).get(k)
        if a and b and same_fy and abs(b / a - 1) > 0.005:
            L.append(f"<li><b>가이던스 {nm}</b> {E.oku(a)} → {E.oku(b)} (<b>{(b / a - 1) * 100:+.1f}%</b>, {'상향' if b > a else '하향'})</li>")
    if not same_fy and cur.get("fc_end") and (cur.get("fc") or {}).get("op"):
        L.append(f"<li><b>새 회계연도 가이던스</b> {E.fy_label_end(cur['fc_end'])} 영업이익 {E.oku(cur['fc']['op'])}"
                 + (f" (직전 회계연도 가이던스 {E.oku(prev['fc']['op'])})" if (prev.get('fc') or {}).get('op') else "") + "</li>")
    a, b = prev.get("dps_fc"), cur.get("dps_fc")
    if a is not None and b is not None and abs(b - a) > 0.01:
        L.append(f"<li><b>주당배당 예상</b> {a:,.1f}엔 → {b:,.1f}엔 ({'증배' if b > a else '감배'})</li>")
    if cur.get("capa") and cur["capa"] != prev.get("capa"):
        L.append(f"<li><b>캐파·증설</b> {esc(cur['capa'])}" + (f"<br><small class=muted>지난번: {esc(prev['capa'])}</small>" if prev.get("capa") else "") + "</li>")
    add_p = [x for x in cur.get("policy") or [] if x not in (prev.get("policy") or [])]
    gone_p = [x for x in prev.get("policy") or [] if x not in (cur.get("policy") or [])]
    if (add_p or gone_p) and prev.get("policy") is not None and (cur.get("policy") or prev.get("policy")):
        L.append("<li><b>주주환원 목표</b> " + (("새로: " + esc(" · ".join(add_p))) if add_p else "") + (("  빠짐: " + esc(" · ".join(gone_p))) if gone_p else "") + "</li>")
    ph = {n: v for n, v in prev.get("holders") or []}; ch = {n: v for n, v in cur.get("holders") or []}
    if ph and ch and prev.get("hsrc") == cur.get("hsrc"):   # 출처(EDINET·가부탄)가 같을 때만 비교 — 이름 표기가 달라 생기는 가짜 변화 방지
        ch_l = []
        for n, v in ch.items():
            if n not in ph: ch_l.append(f"새로 진입 {esc(n)}" + (f" {v:.2f}%" if v is not None else ""))
            elif v is not None and ph[n] is not None and abs(v - ph[n]) >= 0.5: ch_l.append(f"{esc(n)} {ph[n]:.2f}% → {v:.2f}% ({v - ph[n]:+.2f}%p)")
        ch_l += [f"상위 10에서 빠짐 {esc(n)}" for n in ph if n not in ch]
        if ch_l: L.append("<li><b>대주주</b> " + " · ".join(ch_l[:6]) + "</li>")
    head = (f"<p class=muted>비교 대상: {esc(prev.get('label'))} 리포트(발표 {prev['d'][:4]}-{prev['d'][4:6]}-{prev['d'][6:]})</p>")
    if prev.get("headline"):
        head += f"<p><b>지난번 결론</b> {esc(prev['headline'])}</p>"
    if cur.get("headline"):
        head += f"<p><b>이번 결론</b> {esc(cur['headline'])}</p>"
    body = ("<ul>" + "".join(L) + "</ul>") if L else "<p>가이던스·주당배당 예상·캐파·주주환원 목표·대주주에서 바뀐 점이 없습니다.</p>"
    return "<h2>지난 리포트 대비 달라진 점</h2><div class=card>" + head + body + "</div>", len(L)
