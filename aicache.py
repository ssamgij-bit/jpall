"""AI(Gemini) 결과 저장: 같은 자료(PDF)·같은 지시문이면 다시 묻지 않고 저장해 둔 결과를 씀(무료 한도 절약).
earn-state 브랜치(STATE_DIR/ai_cache)에 7일 보관. 공개 자료 요약만 담김(개인 정보 없음)."""
import os, json, time, hashlib

DIR = os.path.join(os.environ.get("STATE_DIR", "estate"), "ai_cache")
KEEP = 7 * 86400

def key(prompt, blobs):
    h = hashlib.sha256(prompt.encode())
    for b in blobs:
        h.update(hashlib.sha256(b if isinstance(b, bytes) else str(b).encode()).digest())
    return h.hexdigest()[:24]

def get(k):
    try:
        d = json.load(open(os.path.join(DIR, k + ".json")))
        if time.time() - d["ts"] < KEEP: return d["out"]
    except Exception:
        pass
    return None

def put(k, out):
    if not out: return
    try:
        os.makedirs(DIR, exist_ok=True)
        json.dump({"ts": time.time(), "out": out}, open(os.path.join(DIR, k + ".json"), "w"), ensure_ascii=False, separators=(",", ":"))
    except Exception:
        pass

def prune():
    """7일 지난 결과 지우기(실행 시작 때 한 번)"""
    try:
        for f in os.listdir(DIR):
            p = os.path.join(DIR, f)
            try:
                if time.time() - json.load(open(p)).get("ts", 0) > KEEP: os.remove(p)
            except Exception:
                os.remove(p)
    except Exception:
        pass
