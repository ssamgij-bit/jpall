#!/usr/bin/env python3
"""일본 대량보유 보고(5%룰) 수집 → state/holdings.json (도쿄 증시 보드용)

- 출처: 일본 금융청 EDINET API v2 (Public Data License 1.0)
  목록  https://api.edinet-fsa.go.jp/api/v2/documents.json?date=YYYY-MM-DD&type=2
  본문  https://api.edinet-fsa.go.jp/api/v2/documents/{docID}?type=5  (XBRL → CSV, UTF-16 탭 구분, zip)
- 대상: docTypeCode 350(大量保有報告書·変更報告書), 360(訂正報告書)
- 환경 변수: EDINET_API_KEY(필수), STATE_DIR(기본 state), HOLD_DAYS(기본 30), HOLD_BUDGET(초, 기본 480)
"""
import os, re, io, json, time, zipfile, datetime as dt, urllib.request, urllib.parse, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import jpall as J

KEY = os.environ.get("EDINET_API_KEY", "")
DAYS = int(os.environ.get("HOLD_DAYS", "30"))
BUDGET = int(os.environ.get("HOLD_BUDGET", "480"))
API = "https://api.edinet-fsa.go.jp/api/v2/"
PDF = "https://disclosure2dl.edinet-fsa.go.jp/searchdocument/pdf/{}.pdf"
T0 = time.time()

def get(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (jpall-holdings)"})
    return urllib.request.urlopen(req, timeout=timeout).read()

def doc_list(day):
    q = urllib.parse.urlencode({"date": day, "type": 2, "Subscription-Key": KEY})
    j = json.loads(get(API + "documents.json?" + q))
    if j.get("metadata", {}).get("status") not in ("200", 200):
        raise RuntimeError(f"list {day}: {j.get('metadata')}")
    return [r for r in j.get("results") or [] if r.get("docTypeCode") in ("350", "360") and r.get("csvFlag") == "1" and not r.get("withdrawalStatus") in ("1", "2")]

def num(v):
    try: return float(str(v).replace(",", "").replace("%", "").strip())
    except Exception: return None

def parse_csv(blob):
    """zip 안 CSV(UTF-16, 탭) → {요소명: [(컨텍스트, 값)]}"""
    out = {}
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        for n in z.namelist():
            if not n.lower().endswith(".csv"): continue
            raw = z.read(n)
            try: txt = raw.decode("utf-16")
            except Exception: txt = raw.decode("utf-8", "ignore")
            rows = [r.split("\t") for r in txt.splitlines() if r.strip()]
            if not rows: continue
            head = [h.strip('"') for h in rows[0]]
            ie = next((i for i, h in enumerate(head) if "要素ID" in h), 0)
            ic = next((i for i, h in enumerate(head) if "コンテキストID" in h), 2)
            iv = next((i for i, h in enumerate(head) if h == "値"), len(head) - 1)
            for r in rows[1:]:
                r = [c.strip('"') for c in r]
                if len(r) <= max(ie, ic, iv): continue
                el = r[ie].split(":")[-1]
                out.setdefault(el, []).append((r[ic], r[iv]))
    return out

def pick(d, el, agg=True):
    """집계 컨텍스트(FilingDateInstant, 보유자 멤버 없음)를 우선"""
    vals = d.get(el) or []
    if not vals: return None
    if agg:
        for c, v in vals:
            if "Holder" not in c and v not in ("", "－", "-"): return v
    for c, v in vals:
        if v not in ("", "－", "-"): return v
    return None

PURPOSE = [("重要提案", "경영 제안(행동주의)"), ("経営参加", "경영 참여"), ("支配", "경영권"), ("政策投資", "정책투자"), ("純投資", "순투자"),
           ("担保", "담보"), ("業務", "사업상"), ("事業", "사업상"), ("貸株", "대차"), ("運用", "운용"), ("トレーディング", "트레이딩")]
def purpose_ko(p):
    p = J.nfkc(p or "")
    got = [k for j, k in PURPOSE if j in p]
    return "·".join(dict.fromkeys(got))[:20] if got else ("기타" if p else "")

def record(meta, d):
    cur = num(pick(d, "HoldingRatioOfShareCertificatesEtc"))
    prev = num(pick(d, "HoldingRatioOfShareCertificatesEtcPerLastReport"))
    code = J.nfkc(pick(d, "SecurityCodeOfIssuer") or meta.get("secCode") or "").strip()[:4]
    desc = J.nfkc(meta.get("docDescription") or "")
    kind = "fix" if meta.get("docTypeCode") == "360" or "訂正" in desc else ("change" if "変更" in desc else "new")
    holders = {c.split("_", 1)[1] for c, _ in (d.get("Name") or []) if "_" in c and "Holder" in c}
    if cur is not None and cur <= 1.0 and (prev is None or prev <= 1.0): cur = round(cur * 100, 2); prev = round(prev * 100, 2) if prev is not None else None  # 소수(0.0523)로 온 경우
    return {
        "id": meta["docID"], "d": (meta.get("submitDateTime") or "")[:16], "kind": kind, "code": code,
        "issuer": J.nfkc(pick(d, "NameOfIssuer") or ""), "holder": J.nfkc(pick(d, "Name", agg=False) or meta.get("filerName") or ""),
        "joint": max(1, len(holders)), "cur": cur, "prev": prev,
        "delta": round(cur - prev, 2) if cur is not None and prev is not None else None,
        "purpose": purpose_ko(pick(d, "PurposeOfHolding")), "url": PDF.format(meta["docID"]),
    }

ASCII_IN_PAREN = re.compile(r"[（(]\s*([A-Za-z0-9][A-Za-z0-9 .,&'’\-/]+?)\s*[)）]")
def holder_label(st, names):
    """보유자명 한국어/영문 표기: 괄호 안 영문이 있으면 그것, 없으면 번역"""
    out, need = {}, []
    for n in names:
        m = ASCII_IN_PAREN.search(n)
        if m: out[n] = m.group(1).strip()
        elif re.fullmatch(r"[A-Za-z0-9 .,&'’\-/]+", n): out[n] = n
        else: need.append(n)
    tr = J.translate(st, need) if need else {}
    for n in need: out[n] = tr.get(n) or n
    return out

def main():
    if not KEY:
        print("EDINET_API_KEY 없음 → 대량보유 수집 건너뜀"); return
    st = J.load("state.json", {})
    names = J.load_names(st)
    db = J.load("holdings_db.json", {})           # docID → record
    prog = J.load("holdings_prog.json", {})       # 날짜별 목록 처리 상태
    today = J.now().date()
    days = [(today - dt.timedelta(days=i)).isoformat() for i in range(DAYS)]
    todo = []
    for day in days:
        # 오늘·어제는 매번 다시 보고, 그 이전 날짜는 한 번 다 받으면 끝
        if prog.get(day) == "done" and day not in days[:2]: continue
        try:
            lst = doc_list(day)
        except Exception as e:
            print("list error", day, e); break
        todo += [m for m in lst if m["docID"] not in db]
        if day not in days[:2]: prog[day] = "listed"
        if time.time() - T0 > BUDGET / 3: break
        time.sleep(0.3)
    n = 0
    for m in todo:
        if time.time() - T0 > BUDGET: break
        try:
            blob = get(f"{API}documents/{m['docID']}?type=5&Subscription-Key={urllib.parse.quote(KEY)}")
            db[m["docID"]] = record(m, parse_csv(blob)); n += 1
        except Exception as e:
            print("doc error", m.get("docID"), e)
        time.sleep(0.3)
    left = [m for m in todo if m["docID"] not in db]
    for day in days[2:]:
        if prog.get(day) == "listed" and not any((m.get("submitDateTime") or "").startswith(day) for m in left): prog[day] = "done"
    # 오래된 기록 정리
    cut = days[-1]
    db = {k: v for k, v in db.items() if v["d"][:10] >= cut}
    prog = {k: v for k, v in prog.items() if k >= cut}
    J.save("holdings_db.json", db); J.save("holdings_prog.json", prog)

    # ── 보드용 요약 ──
    recs = sorted(db.values(), key=lambda r: r["d"], reverse=True)
    hl = holder_label(st, list(dict.fromkeys(r["holder"] for r in recs if r["holder"])))
    J.save("state.json", st)  # 번역 캐시 저장
    def slim(r):
        nm = names.get(r["code"]) or {}
        return {**r, "issuer_ko": nm.get("ko") or J.jp2ko(r["issuer"].replace("株式会社", "")), "holder_ko": hl.get(r["holder"], r["holder"])}
    main_recs = [r for r in recs if r["kind"] != "fix"]
    ups = sorted([r for r in main_recs if (r["delta"] or 0) > 0], key=lambda r: -r["delta"])[:10]
    downs = sorted([r for r in main_recs if (r["delta"] or 0) < 0], key=lambda r: r["delta"])[:10]
    new5 = [r for r in main_recs if r["kind"] == "new"]
    by_holder = {}
    for r in main_recs:
        h = by_holder.setdefault(r["holder"], {"holder": r["holder"], "holder_ko": hl.get(r["holder"], r["holder"]), "codes": set(), "ratios": []})
        h["codes"].add(r["code"]); h["ratios"].append(r["cur"] or 0)
    tops = sorted(by_holder.values(), key=lambda h: -len(h["codes"]))[:10]
    out = {
        "asof": J.now().strftime("%Y-%m-%d %H:%M"), "days": DAYS, "source": "EDINET(금융청) · Public Data License 1.0",
        "kpi": {"total": len(main_recs), "new5": len(new5), "up": sum(1 for r in main_recs if (r["delta"] or 0) > 0), "down": sum(1 for r in main_recs if (r["delta"] or 0) < 0)},
        "up": [slim(r) for r in ups], "down": [slim(r) for r in downs],
        "holders": [{"holder": h["holder"], "holder_ko": h["holder_ko"], "n": len(h["codes"]), "avg": round(sum(h["ratios"]) / max(1, len(h["ratios"])), 2)} for h in tops],
        "recent": [slim(r) for r in recs[:250]],
        "pending": len(left),
    }
    J.save("holdings.json", out)
    print(f"holdings: 신규 처리 {n}건, 대기 {len(left)}건, 30일 보고 {len(main_recs)}건 (신규5% {len(new5)})")

if __name__ == "__main__":
    main()
