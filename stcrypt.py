"""공개 저장소에 올라가는 상태 파일 중 개인 정보(주인 텔레그램 ID·관심 종목)를 암호화.
키는 저장소 비밀값 WATCH_KEY(공시 채널 봇·실적 봇·상태 감시가 함께 씀). 키가 없으면 암호화하지 않음."""
import os, json, hmac, base64, hashlib

PRIV = ("owner", "watch")   # 공시 채널 봇(jpall) state.json 에서 가리는 항목

def _key():
    k = os.environ.get("WATCH_KEY", "")
    return hashlib.sha256(("jp-state-v1:" + k).encode()).digest() if k else None

def _stream(k, nonce, n):
    out, i = b"", 0
    while len(out) < n:
        out += hashlib.sha256(k + nonce + i.to_bytes(8, "big")).digest(); i += 1
    return out[:n]

def seal(obj):
    k = _key()
    if not k: return None
    nonce = os.urandom(16)
    pt = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode()
    ct = bytes(a ^ b for a, b in zip(pt, _stream(k, nonce, len(pt))))
    mac = hmac.new(k, nonce + ct, hashlib.sha256).digest()
    return base64.b64encode(nonce + mac + ct).decode()

def unseal(blob):
    k = _key()
    if not k: raise ValueError("WATCH_KEY 없음")
    raw = base64.b64decode(blob)
    nonce, mac, ct = raw[:16], raw[16:48], raw[48:]
    if not hmac.compare_digest(mac, hmac.new(k, nonce + ct, hashlib.sha256).digest()): raise ValueError("키가 맞지 않음")
    return json.loads(bytes(a ^ b for a, b in zip(ct, _stream(k, nonce, len(ct)))))

def hide(st):
    """저장용: 개인 항목을 _priv 로 암호화한 사본(키가 없으면 그대로). 풀지 못했던 _priv 는 그대로 유지"""
    if st.get("_priv_locked"):
        out = {k: v for k, v in st.items() if k not in PRIV and k != "_priv_locked"}
        out["_priv"] = st["_priv_locked"]; return out
    blob = seal({k: st[k] for k in PRIV if k in st})
    if not blob: return st
    out = {k: v for k, v in st.items() if k not in PRIV}
    out["_priv"] = blob
    return out

def reveal(st):
    """읽기용: _priv 를 풀어 원래 항목으로. 못 풀면 주인을 비워 두지 않고 잠금(아무나 주인이 되지 않도록)"""
    if "_priv" not in st: return st
    blob = st.pop("_priv")
    try:
        st.update(unseal(blob))
    except Exception as e:
        print("state 개인 항목을 풀지 못함:", e, flush=True)
        st["_priv_locked"] = blob
        st["owner"] = -1
    return st
