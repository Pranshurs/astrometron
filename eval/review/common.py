import json, tempfile, shutil, sys
from pathlib import Path
from astrometron.config import from_dict
from astrometron.runner import run_eval
from astrometron.store import Store
def env(n=10, name="e"):
    root = Path(tempfile.mkdtemp(prefix="astrometron-review-"))
    data = root/"cases.jsonl"
    data.write_text("".join(json.dumps({"id":f"c{i}","input":f"q{i}","expected":f"a{i}"})+"\n" for i in range(n)))
    return root, data, Store(root/".astrometron")
def cfg(root, data, **over):
    raw = {"name":"t","task":"t","dataset":{"path":str(data),"id":"ds"},
           "provider":{"kind":"mock","model":"m1"},"scorer":{"kind":"exact"},"run":{"seed":1}}
    for k,v in over.items():
        raw[k] = {**raw[k], **v} if isinstance(v,dict) and isinstance(raw.get(k),dict) else v
    return from_dict(raw, root)
