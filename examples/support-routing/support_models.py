"""Local, deterministic example "models" for the support-routing demo.

``keyword_router`` is a real (if simple) classifier: it reads the prompt and answers.
No API account is needed.
"""

from astrometron.providers import Request, Response, Usage

KEYWORDS = {
    "billing": ["charged", "refund", "invoice", "card", "price", "bill"],
    "technical": ["crash", "error", "sync", "upload", "api", "timeout"],
    "account": ["log in", "password", "email address", "delete my", "two-factor"],
    "shipping": ["arrived", "tracking", "delivery", "parcel", "order of", "damaged"],
}


def keyword_router(request: Request) -> Response:
    text = request.messages[-1].content.lower()
    scores = {label: sum(k in text for k in kws) for label, kws in KEYWORDS.items()}
    best = max(sorted(scores), key=lambda k: scores[k])
    answer = best if scores[best] else "unknown"
    return Response(
        text=answer,
        usage=Usage(input_tokens=len(text) // 4, output_tokens=1),
        served_model="keyword-router-v1",
    )
