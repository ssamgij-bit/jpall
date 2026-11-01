"""밴드 하단 진입 감지 — 도쿄 증시 보드 장 마감 갱신(Claude 정기 작업)이 실행.

12MF PER의 최근 10년 위치(백분위)가 하단(기본 10% 이하)에 새로 들어온 종목을 찾는다.
계산은 도쿄 증시 보드·밸류 데스크와 같은 기준(보드 fwdOf·pctOf를 파이썬으로 옮김):
  12MF EPS = 진행 중 회계연도(FY1)와 다음 회계연도(FY2) EPS를 FY1 경과 비중으로 가중평균(내 입력 추정치 → LSEG 컨센서스 → 회사 예상 → 앞 해 유지)
  위치 = 밸류 데스크 기본 추정치로 만든 최근 10년 주간 12MF PER 분포(21개 분위수)에서 현재 12MF PER의 백분위(선형 보간)

사용: python3 band_alert.py <dbread 폴더> <out 폴더> [vdest.json] [--th 10] [--cap 300]
  dbread/vd/fwd-*.json, dbread/vd/fwdpct-*.json, dbread/snap/band.json(지난번 결과, 없으면 첫 실행)
  out/uni-*.json(현재가·이름·시총), out/meta.json(거래일)
결과: out/band.json(보드 db snap/band로 저장됨) — list(현재 하단 종목), new(이번에 새로 들어온 종목), out(이번에 벗어난 종목)
"""
import glob
import json
import os
import sys
from datetime import datetime, timezone

DAY = 86400.0


def load(p, default=None):
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return default


def end_ts(lab):  # "2027-03" → 그 달 말일(UTC) — JS Date.UTC(y, m, 0)
    y, m = int(lab[:4]), int(lab[5:7])
    y2, m2 = (y + 1, 1) if m == 12 else (y, m + 1)
    return datetime(y2, m2, 1, tzinfo=timezone.utc).timestamp() - DAY


def isn(v):
    return isinstance(v, (int, float)) and v == v and abs(v) != float("inf")


def fwd_of(f, px, t, U=None):
    """보드 fwdOf와 같은 계산 → (12MF PER 또는 None)"""
    U = U or {}
    est, ov = U.get("est") or {}, U.get("ov") or {}
    ay, aeps, abps, adps = (f.get("a") or [None, None, None, None])[:4]
    if not ay:
        return None
    p_eps = ov.get(ay, {}).get("eps") if isn(ov.get(ay, {}).get("eps")) else aeps
    p_bps = ov.get(ay, {}).get("bps") if isn(ov.get(ay, {}).get("bps")) else abps
    p_dps = adps if isn(adps) else 0
    vm = {ay: p_eps}
    for row in f.get("e") or []:
        y, e0, sc, d = (list(row) + [None] * 4)[:4]
        u = est.get(y) or {}
        if isn(u.get("eps")):
            eps = u["eps"]
        elif isn(e0):
            eps = e0
        else:
            eps = p_eps
        dps = d if isn(d) else p_dps
        bps = u["bps"] if isn(u.get("bps")) else (p_bps + eps - (dps or 0) if isn(p_bps) and isn(eps) else None)
        vm[y] = eps
        p_eps, p_bps, p_dps = eps, bps, dps
    labs = sorted(vm)
    ends = [end_ts(x) for x in labs]
    i = 0
    while i < len(ends) and ends[i] < t:
        i += 1
    if i >= len(ends) - 1:
        return None
    e1, e2 = ends[i], ends[i + 1]
    if not (300 * DAY <= e2 - e1 <= 400 * DAY):
        return None
    e0 = ends[i - 1] if i > 0 and 300 * DAY <= e1 - ends[i - 1] <= 400 * DAY else e1 - 365 * DAY
    w = min(1, max(0, (t - e0) / (e1 - e0)))
    v1, v2 = vm[labs[i]], vm[labs[i + 1]]
    if not (isn(v1) and isn(v2)):
        return None
    E = (1 - w) * v1 + w * v2
    lp = f.get("lp")
    ok_px = px and (not lp or 0.55 < px / lp < 1.8)
    return px / E if ok_px and E > 0 else None


def pct_of(q, v):
    """보드 pctOf: 21개 분위수에서 v의 백분위"""
    a = q.get("q") or []
    if not isn(v) or v <= 0 or len(a) < 21:
        return None
    if v <= a[0]:
        return 0.0
    if v >= a[20]:
        return 100.0
    for i in range(20):
        if v <= a[i + 1]:
            return (i + ((v - a[i]) / (a[i + 1] - a[i]) if a[i + 1] > a[i] else 0)) * 5
    return 100.0


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    opt = {sys.argv[i][2:]: sys.argv[i + 1] for i in range(len(sys.argv) - 1) if sys.argv[i].startswith("--")}
    db, out = args[0], args[1]
    th, mincap = float(opt.get("th", 10)), float(opt.get("cap", 300))
    vdest = (load(args[2], {}) or {}).get("d", {}) if len(args) > 2 else {}
    FWD, PCT = {}, {}
    for p in sorted(glob.glob(os.path.join(db, "vd", "fwd-*.json"))):
        FWD.update((load(p, {}) or {}).get("d") or {})
    for p in sorted(glob.glob(os.path.join(db, "vd", "fwdpct-*.json"))):
        PCT.update((load(p, {}) or {}).get("d") or {})
    meta = load(os.path.join(out, "meta.json"), {}) or {}
    td = meta.get("tradeDate") or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    t = datetime.strptime(td, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
    rows = []
    for p in sorted(glob.glob(os.path.join(out, "uni-*.json"))):
        d = load(p, {}) or {}
        cols = d.get("cols") or []
        for r in d.get("rows") or []:
            rows.append(dict(zip(cols, r)))
    cur = []
    for r in rows:
        c = r.get("code")
        f, q = FWD.get(c), PCT.get(c)
        if not f or not q or (r.get("mcap") or 0) < mincap:
            continue
        fpe = fwd_of(f, r.get("px"), t, vdest.get(c))
        p = pct_of(q, fpe)
        if p is None or p > th:
            continue
        cur.append({"code": c, "ko": r.get("ko") or r.get("nm") or c, "sec": r.get("sec"), "px": r.get("px"), "mcap": r.get("mcap"),
                    "fpe": round(fpe, 1), "p": round(p, 1), "mean": q.get("mean"), "lo": (q.get("q") or [None])[0], "yr": q.get("yr"),
                    "c52": r.get("c52")})
    cur.sort(key=lambda x: (x["p"], -(x["mcap"] or 0)))
    prev = load(os.path.join(db, "snap", "band.json"), None)
    prev_codes = {x["code"] for x in (prev or {}).get("list", [])}
    now_codes = {x["code"] for x in cur}
    res = {"asOf": meta.get("asOf"), "tradeDate": td, "th": th, "cap": mincap, "n": len(cur), "list": cur[:300],
           "new": [x for x in cur if x["code"] not in prev_codes][:30] if prev is not None else [],
           "out": sorted(prev_codes - now_codes)[:30], "prevDate": (prev or {}).get("tradeDate"), "first": prev is None,
           "by": "12MF PER 10년 위치(밸류 데스크 기본 추정치 분포) ≤ %g%%, 시총 %g억엔↑" % (th, mincap)}
    json.dump(res, open(os.path.join(out, "band.json"), "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    print(f"band: 하단 {len(cur)}종목 · 신규 {len(res['new'])} · 이탈 {len(res['out'])}" + (" (첫 실행: 기준선만 저장)" if prev is None else ""))


if __name__ == "__main__":
    main()
