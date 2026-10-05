import sys; sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent))
from common import *
root,data,store = env(4)
for label, over in [("nan cost", {"provider":{"kind":"python","function":"helpers:nan_cost","model":"p"}}),
                    ("nan score", {"scorer":{"kind":"python","function":"helpers:nan_scorer"}})]:
    try:
        r = run_eval(cfg(root,data,**over), store)
        print(label, "OK", r["aggregate"]["counts"])
    except Exception as e:
        print(label, "RUN ABORTED:", type(e).__name__, e)
print([(x["run_id"][-6:],x["status"]) for x in store.list_runs()])
