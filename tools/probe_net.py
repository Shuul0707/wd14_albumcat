"""Connectivity probe: can this machine (via Python) reach model hosts and PyPI?

Run:  python tools\\probe_net.py
"""
import json
import time
import urllib.request

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


def probe(name, url, want_json=False):
    t = time.time()
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=15) as r:
            body = r.read(2000)
        dt = time.time() - t
        extra = ""
        if want_json:
            try:
                j = json.loads(body.decode("utf-8", "replace"))
                extra = " keys=" + ",".join(list(j)[:6])
            except Exception:
                extra = " (json parse failed)"
        print(f"[OK]   {name:12s} {r.status}  {len(body):5d}B  {dt:5.1f}s{extra}")
        return body
    except Exception as e:
        print(f"[FAIL] {name:12s} {type(e).__name__}: {str(e)[:110]}  ({time.time()-t:.1f}s)")
        return None


if __name__ == "__main__":
    print("== model hosts ==")
    probe("huggingface", "https://huggingface.co/api/models/SmilingWolf/wd-v1-4-vit-tagger-v2", True)
    probe("hf-mirror", "https://hf-mirror.com/api/models/SmilingWolf/wd-v1-4-vit-tagger-v2", True)
    probe("modelscope", "https://www.modelscope.cn/api/v1/models/AI-ModelScope/wd-v1-4-vit-tagger-v2", True)
    print("== package index ==")
    probe("pypi", "https://pypi.org/pypi/onnxruntime-directml/json", True)
    probe("tuna", "https://pypi.tuna.tsinghua.edu.cn/simple/onnxruntime-directml/", False)
    print("== observed redirects / final host ==")
    for name, url in [
        ("hf", "https://huggingface.co/SmilingWolf/wd-v1-4-vit-tagger-v2/resolve/main/model.onnx"),
    ]:
        try:
            req = urllib.request.Request(url, headers=UA, method="HEAD")
            with urllib.request.urlopen(req, timeout=15) as r:
                print(f"[OK]   {name:12s} {r.status} final={r.url[:120]} len={r.headers.get('Content-Length')}")
        except Exception as e:
            print(f"[FAIL] {name:12s} {type(e).__name__}: {str(e)[:110]}")
