"""실적 스크리닝 카드의 [리포트] 버튼 → 한 파일짜리 HTML 리포트(차트 내장)

구성: 회사 개요 · KPI(시총, 최근 연도 매출, PER, 배당) · 분기 실적 표(최근 8개 분기) · 연간 실적 표(가이던스 포함)
     · 차트(주가 1년과 실적 발표일 반응, 매출·YoY, 영업이익·영업이익률) · 결산 설명 자료 요약(Gemini, 원문 PDF 기준)
"""
import io, os, re, json, time, base64, html, datetime as dt

import earn as E
from jpall import http, gemini_json, GEMINI_MODELS

def b64png(fig):
    buf = io.BytesIO(); fig.savefig(buf, format="png", dpi=140, facecolor="white"); E._plt().close(fig)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()

def gemini_pdf(prompt, pdf):
    key = os.environ.get("GEMINI_API_KEY")
    if not key or not pdf: return None
    body = json.dumps({"contents": [{"parts": [{"inline_data": {"mime_type": "application/pdf", "data": base64.b64encode(pdf).decode()}}, {"text": prompt}]}],
                       "generationConfig": {"temperature": 0.2, "maxOutputTokens": 8000, "responseMimeType": "application/json"}}).encode()
    for model in GEMINI_MODELS:
        try:
            r = json.loads(http(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}",
                                timeout=180, data=body, headers={"content-type": "application/json"}))
            txt = "".join(p.get("text", "") for p in r["candidates"][0]["content"]["parts"])
            return json.loads(re.sub(r"^```(json)?|```$", "", txt.strip()))
        except Exception as e:
            E.log("gemini pdf error", model, str(e)[:150])
            if "429" in str(e): time.sleep(3)
    return None

MAT_PROMPT = """이 PDF는 일본 상장사의 결산 설명 자료(또는 결산단신)다. 한국 애널리스트용으로 한국어 JSON을 써라.
- 자료에 있는 내용만. 숫자는 자료 그대로(단위 표기). 일본어는 모두 한국어로 번역(고유명사는 한글 발음 또는 통용 영문).
출력: {"title": "자료 제목(한국어)", "one_line": "한 줄 요약", "points": ["주요 내용 5개 이내, 각 80자 이내"],
 "segments": "제품·지역·용도별 매출/이익 흐름 요약(있으면, 150자 이내)", "orders": "수주·수주잔고·BB 등(있으면)",
 "guidance": "실적 예상·배당 변경과 그 이유(있으면)", "risks": "회사가 언급한 리스크·하방 요인(있으면)"}"""

OVERVIEW_PROMPT = """다음 일본 회사의 개요(일본어)를 한국어로 2~3문장으로 설명하라. 무엇을 만들어/팔아 누구에게 파는지, 주요 제품·고객·지역이 드러나게.
일본어를 그대로 쓰지 말 것. JSON: {{"overview": "..."}}
회사: {name}({code}), 업종 {sector}
개요: {profile}
결산단신 본문 일부: {qual}"""

def fmt(v, d=1):
    return "—" if v is None else f"{v:,.{d}f}"

def build(card):
    code = card["code"]; fem = card["fem"]
    name = card.get("name_ko") or card["name"]
    kb = E.kb_finance(code)
    series = card["series"]
    by_end = {q["end"]: q for q in kb["q"]}
    for q in series:  # 가부탄이 나중에 갱신됐으면 발표일 등 보완
        k = by_end.get(q["end"]);
        if k:
            for f in ("ann",): q[f] = k.get(f) or q.get(f)
    full = {q["end"]: q for q in kb["q"]}; full.update({q["end"]: q for q in series})
    allq = [full[k] for k in sorted(full)]
    plt = E._plt()

    # 주가 1년 + 실적 발표일
    charts = []
    try:
        bars = E.naver_daily(f"{code}.T", days=380)
        ds = [dt.date.fromisoformat(b["d"]) for b in bars]; cs = [b["c"] for b in bars]
        fig, ax = plt.subplots(figsize=(8, 3.6))
        ax.plot(ds, cs, color=E.BLUE, lw=1.4)
        for q in allq:
            a = q.get("ann")
            if not a or a < bars[0]["d"]: continue
            ad = dt.date.fromisoformat(a)
            nxt = [i for i, b in enumerate(bars) if b["d"] > a]
            ax.axvline(ad, color=E.GRAY, lw=0.8, ls="--")
            if nxt and nxt[0] > 0:
                i = nxt[0]; ch = (bars[i]["c"] / bars[i - 1]["c"] - 1) * 100
                ax.text(ad, max(cs), f"{ch:+.1f}%", fontsize=8, ha="center", va="bottom", color="#c0392b" if ch < 0 else "#1e8449")
        ax.set_title("주가 1년과 실적 발표일(점선, 숫자=다음 날 종가 변화)", loc="left", fontsize=11, fontweight="bold")
        for sp in ("top", "right"): ax.spines[sp].set_visible(False)
        ax.grid(color="#eee"); fig.tight_layout()
        charts.append(b64png(fig))
    except Exception as e:
        E.log("price chart error", e)
    # 매출 · 영업이익(이익률)
    s = allq[-8:]
    labs = []
    for q in s:
        fy, n = E.fy_of(q["end"], fem); labs.append(f"{n}Q\n{fy}")
    for key, title in (("rev", "분기 매출과 YoY"), ("op", "분기 영업이익 · YoY · 영업이익률")):
        fig, ax = plt.subplots(figsize=(8, 3.4))
        vals = [(q.get(key) or 0) / 100 for q in s]
        ax.bar(range(len(s)), vals, color=[E.BLUE if i == len(s) - 1 else "#9dbbea" for i in range(len(s))], width=0.6)
        ax.set_xticks(range(len(s))); ax.set_xticklabels(labs, fontsize=8)
        ax.set_ylabel("억 엔", fontsize=8)
        ax2 = ax.twinx()
        ys = [E.yoy(q.get(key), (full.get(E.ym_add(q["end"], -12)) or {}).get(key))[0] for q in s]
        xs = [i for i, v in enumerate(ys) if v is not None]
        ax2.plot(xs, [min(max(ys[i], -100), 300) for i in xs], color=E.ORANGE, marker="o", ms=4, label="YoY(%, 300% 초과는 숫자 표시)")
        for i in xs:
            if ys[i] > 300: ax2.annotate(f"{ys[i]:+,.0f}%", (i, 300), textcoords="offset points", xytext=(0, 5), ha="center", fontsize=7, color=E.ORANGE)
        if key == "op":
            opm = [(q["op"] / q["rev"] * 100) if q.get("op") is not None and q.get("rev") else None for q in s]
            xm = [i for i, v in enumerate(opm) if v is not None]
            ax2.plot(xm, [opm[i] for i in xm], color="#2e7d32", marker="s", ms=3, ls="--", label="영업이익률(%)")
        ax2.legend(fontsize=7, loc="upper left", frameon=False)
        ax.set_title(title, loc="left", fontsize=11, fontweight="bold")
        for sp in ("top",): ax.spines[sp].set_visible(False); ax2.spines[sp].set_visible(False)
        fig.tight_layout(); charts.append(b64png(fig))

    # 회사 개요·설명 자료 요약
    ov = gemini_json(OVERVIEW_PROMPT.format(name=name, code=code, sector=E.SECTOR_KO.get(card["sector"], card["sector"]),
                                            profile=card.get("profile", ""), qual=card.get("qual", "")[:2500]), timeout=90) or {}
    if isinstance(ov, list): ov = ov[0] if ov else {}
    mat_src = card.get("mat") or card.get("pdf")
    mat = None
    try:
        mat = gemini_pdf(MAT_PROMPT, http(mat_src, timeout=60)) if mat_src else None
        if isinstance(mat, list): mat = mat[0] if mat else None
    except Exception as e:
        E.log("material error", e)

    # 표
    qrows = []
    for q in s:
        lab, months = E.qlabel(q, fem)
        py = full.get(E.ym_add(q["end"], -12)) or {}
        ry, rs = E.yoy(q.get("rev"), py.get("rev")); oy, os_ = E.yoy(q.get("op"), py.get("op")); ny, ns = E.yoy(q.get("ni"), py.get("ni"))
        opm = q["op"] / q["rev"] * 100 if q.get("op") is not None and q.get("rev") else None
        qrows.append(f"<tr><td>{lab}<br><small>{months}</small></td><td>{q.get('ann') or ''}</td><td>{E.oku(q.get('rev'))}</td><td>{E.yoy_txt(ry, rs)}</td>"
                     f"<td>{E.oku(q.get('op'))}</td><td>{E.yoy_txt(oy, os_)}</td><td>{fmt(opm)}%</td><td>{E.oku(q.get('ni'))}</td><td>{E.yoy_txt(ny, ns)}</td></tr>")
    yrows = []
    for y in kb["y"][-6:]:
        yrows.append(f"<tr><td>{'(예상) ' if y['fc'] else ''}FY{int(y['end'][5:])}/{y['end'][2:4]}</td><td>{E.oku(y['rev'])}</td><td>{E.oku(y['op'])}</td>"
                     f"<td>{fmt(y['op'] / y['rev'] * 100 if y.get('op') is not None and y.get('rev') else None)}%</td><td>{E.oku(y['ni'])}</td>"
                     f"<td>{fmt(y.get('eps'))}엔</td><td>{fmt(y.get('dps'))}엔</td></tr>")
    r = card["r"]; fc = r.get("fc") or {}
    last_y = next((y for y in reversed(kb["y"]) if not y["fc"]), None)
    per = card["cap"] * 1e8 / fc["ni"] if card.get("cap") and fc.get("ni") and fc["ni"] > 0 else None
    dps = next((y.get("dps") for y in reversed(kb["y"]) if y["fc"]), None)
    kpi = [("시가총액", f"{card['cap']:,.0f}억 엔" if card.get("cap") else "—"),
           ("최근 연도 매출", f"{E.oku(last_y['rev'])} 엔 (FY{int(last_y['end'][5:])}/{last_y['end'][2:4]})" if last_y else "—"),
           ("PER(가이던스)", f"{per:.1f}배" if per else "—"), ("배당(예상)", f"{dps:,.1f}엔" if dps else "—")]
    ai = card.get("ai") or {}
    why = "".join(f"<li><b>{'기타' if k == 'etc' else k}</b> {html.escape(ai[k])}</li>" for k in ("P", "Q", "C", "etc") if ai.get(k) and "원문 설명 없음" not in ai[k])
    mat_html = ""
    if mat:
        pts = "".join(f"<li>{html.escape(p)}</li>" for p in (mat.get("points") or []))
        extra = "".join(f"<p><b>{lab}</b> {html.escape(mat[k])}</p>" for k, lab in (("segments", "부문·지역"), ("orders", "수주"), ("guidance", "가이던스·배당"), ("risks", "리스크"))
                        if mat.get(k))
        mat_html = (f"<h2>결산 설명 자료 요약</h2><p class=muted>{html.escape(mat.get('title') or '')}</p>"
                    f"<p><b>한 줄 요약</b> {html.escape(mat.get('one_line') or '')}</p><ol>{pts}</ol>{extra}")
    rx = card.get("rx") if isinstance(card.get("rx"), dict) else None
    rx_html = f"<p>{E.reaction_line(rx)}</p>" if rx else ""
    links = f"<a href='{card['pdf']}'>결산단신(원문)</a>" + (f" · <a href='{card['mat']}'>설명 자료(원문)</a>" if card.get("mat") else "")
    doc = f"""<!doctype html><html lang=ko><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>{html.escape(name)}({code}) 실적 리포트</title><style>
body{{font-family:-apple-system,'Apple SD Gothic Neo','Noto Sans KR',sans-serif;margin:0;background:#f6f7f9;color:#1f2328}}
main{{max-width:860px;margin:0 auto;padding:16px}} h1{{font-size:22px;margin:8px 0}} h2{{font-size:17px;margin:24px 0 8px;border-left:4px solid #2f6fd6;padding-left:8px}}
.card{{background:#fff;border-radius:12px;padding:14px 16px;margin:10px 0;box-shadow:0 1px 2px rgba(0,0,0,.06)}}
.kpi{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:8px}} .kpi div{{background:#fff;border-radius:10px;padding:10px 12px}}
.kpi small{{color:#6b7280}} .kpi b{{display:block;font-size:18px;margin-top:2px}}
table{{border-collapse:collapse;width:100%;font-size:13px;background:#fff}} th,td{{border-bottom:1px solid #eee;padding:6px 6px;text-align:right;white-space:nowrap}}
th:first-child,td:first-child{{text-align:left}} th{{background:#f0f3f8;font-weight:600}} .scroll{{overflow-x:auto}}
img{{width:100%;border-radius:10px;background:#fff;margin:6px 0}} .muted,small{{color:#6b7280}} a{{color:#2f6fd6}}
</style></head><body><main>
<h1>{html.escape(name)} <small>({code} JP · {html.escape(E.SECTOR_KO.get(card['sector'], card['sector']))})</small></h1>
<p class=muted>실적 발표 {card['d'][:4]}-{card['d'][4:6]}-{card['d'][6:]} {card['t']} JST · 기준 충족: {' · '.join(card.get('hit') or [])}</p>
{f"<div class=card>{html.escape(ov.get('overview') or ai.get('bm'))}</div>" if (ov.get('overview') or ai.get('bm')) else ''}
<div class=kpi>{''.join(f'<div><small>{k}</small><b>{v}</b></div>' for k, v in kpi)}</div>
{f"<h2>왜 좋았나</h2><div class=card><p><b>{html.escape(ai.get('summary') or '')}</b></p><ul>{why}</ul>{rx_html}</div>" if (ai.get('summary') or why or rx_html) else ''}
<h2>분기 실적 (3개월 단독, {r.get('scope') or ''})</h2><div class=scroll><table>
<tr><th>분기</th><th>발표일</th><th>매출</th><th>YoY</th><th>영업이익</th><th>YoY</th><th>영업이익률</th><th>순이익</th><th>YoY</th></tr>{''.join(qrows)}</table></div>
<h2>연간 실적</h2><div class=scroll><table><tr><th>회계연도</th><th>매출</th><th>영업이익</th><th>영업이익률</th><th>순이익</th><th>EPS</th><th>배당</th></tr>{''.join(yrows)}</table></div>
<h2>차트</h2>{''.join(f'<img src="{c}">' for c in charts)}
{mat_html}
<h2>원문</h2><p>{links}</p>
<p class=muted>출처: TDnet 결산단신 XBRL(실적·가이던스), 가부탄(3개월·연간 실적, 시가총액), 네이버 증권(주가·TOPIX 일봉). 금액 단위 억 엔(원자료 백만 엔 환산).
요약·번역은 Gemini가 원문만 읽고 작성 — 숫자는 원문 링크로 확인하세요. 작성 {E.now():%Y-%m-%d %H:%M} JST</p>
</main></body></html>"""
    return f"{code}_{card['d']}_report.html", doc.encode("utf-8")
