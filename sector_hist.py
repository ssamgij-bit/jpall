#!/usr/bin/env python3
"""TSE 33업종 PER·순이익 월별 이력 → STATE_DIR/sectorhist.json (도쿄 증시 보드 섹터 추이용)

- 출처: JPX「規模別・業種別PER・PBR」(월말, 연결). 2010~2012 PDF는 단순평균 PER만, 2013년부터 가중 PER·순이익 합계 제공
- 시장: ~2022/03 東証1部, 2022/04~ プライム
- 실행: 기존 sectorhist.json(없으면 저장소의 sector_seed.json)에 JPX 페이지의 새 월 파일만 더한다
- 처음부터 다시 만들기: python sector_hist.py --full <압축 푼 폴더>  (2010-2012 PDF는 pdftotext 필요)
"""
import os, re, io, sys, json, glob, subprocess, urllib.request, datetime as dt

STATE = os.environ.get("STATE_DIR", "state")
HERE = os.path.dirname(os.path.abspath(__file__))
PAGE = "https://www.jpx.co.jp/markets/statistics-equities/misc/04.html"
UA = {"User-Agent": "Mozilla/5.0 (jpall-sector)"}
SEC_KO = {"水産・農林業":"수산·농림","鉱業":"광업","建設業":"건설","食料品":"식료품","繊維製品":"섬유","パルプ・紙":"펄프·종이",
"化学":"화학","医薬品":"의약품","石油・石炭製品":"석유·석탄","ゴム製品":"고무","ガラス・土石製品":"유리·토석","鉄鋼":"철강",
"非鉄金属":"비철금속","金属製品":"금속제품","機械":"기계","電気機器":"전기기기","輸送用機器":"수송용기기","精密機器":"정밀기기",
"その他製品":"기타제품","電気・ガス業":"전기·가스","陸運業":"육운","海運業":"해운","空運業":"항공","倉庫・運輸関連業":"창고·운수","倉庫・運輸関連":"창고·운수",
"情報・通信業":"정보·통신","卸売業":"도매(상사)","小売業":"소매","銀行業":"은행","証券、商品先物取引業":"증권·선물","保険業":"보험",
"その他金融業":"기타금융","不動産業":"부동산","サービス業":"서비스",
"総合":"전체","総合(金融業を除く)":"전체(금융 제외)","総合（金融業を除く）":"전체(금융 제외)","製造業":"제조업","非製造業":"비제조업"}

def key(label):
    s = re.sub(r"[\s　\xa0]+", "", str(label or ""))
    s = re.sub(r"^\d+", "", s)
    return SEC_KO.get(s) or SEC_KO.get(s.replace("(", "（").replace(")", "）"))

def num(v):
    if v is None: return None
    if isinstance(v, (int, float)): return float(v)
    s = str(v).strip().lstrip("=").replace(",", "")
    try: return float(s)
    except ValueError: return None

def rec(n, sper, wper, ni_oku):
    return {"n": None if n is None else int(n), "sper": sper, "per": wper, "ni": None if ni_oku is None else round(ni_oku)}

# ── 2020~ xlsx (값이 "=123" 수식 문자열로 들어 있는 달도 있음) ──
def parse_xlsx(blob):
    import openpyxl
    out, mkt = {}, None
    ws = openpyxl.load_workbook(io.BytesIO(blob)).worksheets[0]
    for r in ws.iter_rows(values_only=True):
        if not r or not r[0] or not re.match(r"\d{4}/\d{2}", str(r[0])): continue
        if r[1] not in ("市場一部", "プライム市場"): continue
        k = key(r[3])
        if not k: continue
        mkt = "1부" if r[1] == "市場一部" else "프라임"
        ni = num(r[12])
        out[k] = rec(num(r[5]), num(r[6]), num(r[10]), None if ni is None else ni / 1e8)
    return out, mkt

# ── 2013~2019 xls (시트 '連結一部', 순이익 억엔) ──
def parse_xls(path):
    import xlrd
    wb = xlrd.open_workbook(path)
    name = next((n for n in wb.sheet_names() if "連結" in n and "一部" in n), None)
    if not name: return {}
    sh, out = wb.sheet_by_name(name), {}
    for i in range(sh.nrows):
        r = sh.row_values(i); k = key(r[0])
        if not k: continue
        v = list(r[1:])
        while v and v[-1] in ("", None): v.pop()
        if len(v) >= 10: v = v[1:]          # 맨 앞 '단순 주가 평균' 열이 있는 달
        if len(v) < 9: continue
        out[k] = rec(num(v[0]), num(v[1]), num(v[5]), num(v[7]))
    return out

# ── 2010~2012 PDF (연결 페이지, 第一部 열: 단순 PER만) ──
def parse_pdf(path):
    txt = subprocess.run(["pdftotext", "-layout", "-l", "1", path, "-"], capture_output=True, text=True).stdout
    out = {}
    for line in txt.splitlines():
        t = line.split()
        if len(t) < 19: continue
        vals, label = t[-18:], "".join(t[:-18])
        k = key(label)
        if not k or k in out: continue
        out[k] = rec(num(vals[6]), num(vals[7]), None, None)
    return out

def full_build(root):
    data = {}
    for f in sorted(glob.glob(os.path.join(root, "201[012]", "*.pdf"))):
        m = re.search(r"(\d{6})\.pdf", f); ym = m.group(1)
        data[ym] = {"mkt": "1부", "s": parse_pdf(f)}
    for f in sorted(glob.glob(os.path.join(root, "201[3-9]", "*.xls"))):
        ym = re.search(r"(\d{6})\.xls", f).group(1)
        data[ym] = {"mkt": "1부", "s": parse_xls(f)}
    for f in sorted(glob.glob(os.path.join(root, "x", "*.xlsx"))):
        ym = os.path.basename(f)[:6]
        s, mk = parse_xlsx(open(f, "rb").read())
        data[ym] = {"mkt": mk, "s": s}
    return data

def to_doc(data):
    months = sorted(m for m in data if m >= "201001" and data[m]["s"])
    secs = [k for k in dict.fromkeys(v for v in SEC_KO.values())]
    doc = {"asof": dt.datetime.utcnow().strftime("%Y-%m-%d"), "months": [m[:4] + "-" + m[4:] for m in months],
           "mkt": [data[m]["mkt"] for m in months],
           "src": "JPX 規模別・業種別PER・PBR(연결, 월말) · 2022/03까지 東証1部, 2022/04부터 프라임 · 2010~2012는 단순평균 PER만 제공",
           "sec": {}}
    for k in secs:
        doc["sec"][k] = {f: [data[m]["s"].get(k, {}).get(f) for m in months] for f in ("n", "per", "sper", "ni")}
    return doc

def from_doc(doc):
    data = {}
    for i, m in enumerate(doc["months"]):
        ym = m.replace("-", "")
        data[ym] = {"mkt": doc["mkt"][i], "s": {k: {f: v[f][i] for f in ("n", "per", "sper", "ni")} for k, v in doc["sec"].items() if v["n"][i] is not None or v["sper"][i] is not None}}
    return data

def get(url): return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60).read()

ARCH = ["/markets/statistics-equities/misc/tvdivq00000015r6-att/2013-2019(PDF&Excel).zip",
        "/markets/statistics-equities/misc/tvdivq00000015r6-att/1999.11-2012(PDF).zip"]

def bootstrap(page):
    """처음 한 번: JPX 과거 압축 파일(2010~2019)과 월별 xlsx로 전체 이력 생성"""
    import tempfile, zipfile
    root = tempfile.mkdtemp()
    for a in ARCH:
        m = re.search(r'href="([^"]*' + re.escape(a.split("/")[-1]) + ')"', page)
        url = "https://www.jpx.co.jp" + (m.group(1) if m else a)
        with zipfile.ZipFile(io.BytesIO(get(url.replace(" ", "%20").replace("&", "%26")))) as z:
            for n in z.namelist():
                if re.match(r"20(1\d)/", n) and n.endswith((".pdf", ".xls")): z.extract(n, root)
    return full_build(root)

def update():
    path = os.path.join(STATE, "sectorhist.json")
    try: doc = json.load(open(path))
    except Exception: doc = None
    today = dt.datetime.utcnow().strftime("%Y-%m-%d")
    if doc and doc.get("asof") == today:
        print("sectorhist: 오늘 이미 갱신"); return
    page = get(PAGE).decode("utf-8", "ignore")
    data = from_doc(doc) if doc else bootstrap(page)
    links = {}
    for u, ym in re.findall(r'href="([^"]+perpbr(\d{6})\.xlsx)"', page):
        if "tvdivq" not in u: links.setdefault(ym, u)   # tvdivq…는 시장 재편 참고용 별도 파일
    latest = sorted(links)[-2:]  # 최근 2개월은 정정 가능성 있어 다시 받음
    n = 0
    for ym in sorted(links):
        if ym in data and ym not in latest: continue
        try:
            s, mk = parse_xlsx(get("https://www.jpx.co.jp" + links[ym]))
            if s: data[ym] = {"mkt": mk, "s": s}; n += 1
        except Exception as e:
            print("sector xlsx error", ym, e)
    os.makedirs(STATE, exist_ok=True)
    out = to_doc(data)
    json.dump(out, open(path, "w"), ensure_ascii=False, separators=(",", ":"))
    print(f"sectorhist: {len(out['months'])}개월 ({out['months'][0]}~{out['months'][-1]}), 이번에 {n}개 파일 반영")

if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--full":
        doc = to_doc(full_build(sys.argv[2]))
        json.dump(doc, open(os.path.join(HERE, "sector_seed.json"), "w"), ensure_ascii=False, separators=(",", ":"))
        print("seed", len(doc["months"]), doc["months"][0], doc["months"][-1])
    else:
        update()
