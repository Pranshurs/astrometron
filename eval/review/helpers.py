from astrometron.providers import Response, Usage
def nan_cost(req):
    return Response(text="a0", usage=Usage(1,1,float("nan")), served_model="p")
def nan_scorer(output, case):
    return float("nan")
import random
def noisy(req):
    return "a"+req.case_id[1:] if random.random()<0.5 else "zz"
def always_right(req):
    return "a"+req.case_id[1:]
def sleepy(req):
    import time; time.sleep(0.5); return "a"+req.case_id[1:]
from astrometron.solver import ToolFailure
def bad_tool(**kw):
    raise RuntimeError("tool backend down")
def swallow_tool_solver(ctx):
    try:
        ctx.tool("bad", x=1)
    except ToolFailure:
        pass
    return ctx.model().text
def tool_after_model(ctx):
    t = ctx.model().text
    try: ctx.tool("bad", x=1)
    except ToolFailure: pass
    return t
def nan_latency(req):
    return Response(text="a0", latency_ms=float("nan"), served_model="p")
def surrogate(req):
    return Response(text="a\ud800", served_model="p")
def inf_tokens(req):
    return Response(text="a0", usage=Usage(float("inf"),1,None), served_model="p")
