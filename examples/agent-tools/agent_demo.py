"""A deterministic tool-using "agent" for the trajectory-evidence demo.

The planner model replies ``CALL order_status {"order_id": ...}`` first, then answers
from the tool result. The order backend fails for one order to demonstrate TOOL_ERROR.
"""

import json
import re

from evalledger.providers import Request, Response, Usage

ORDERS = {f"A{n:03d}": ("shipped" if n % 3 else "processing") for n in range(1, 41)}


def order_status(order_id: str) -> str:
    if order_id == "A013":
        raise ConnectionError("order database unreachable")
    if order_id not in ORDERS:
        return "unknown order"
    return ORDERS[order_id]


def planner(request: Request) -> Response:
    last = request.messages[-1].content
    m = re.match(r"TOOL RESULT order_status: (.*)", last)
    if m:
        text = m.group(1).strip()
    else:
        oid = re.search(r"A\d{3}", last)
        text = "CALL order_status " + json.dumps({"order_id": oid.group(0) if oid else ""})
    return Response(
        text=text,
        usage=Usage(input_tokens=len(last) // 4, output_tokens=8),
        served_model="planner-v1",
    )
