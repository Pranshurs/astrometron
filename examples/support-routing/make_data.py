"""Regenerate support-tickets.jsonl deterministically (synthetic data, no real customers)."""

import itertools
import json
import random
from pathlib import Path

TEMPLATES = {
    "billing": [
        "I was charged twice for {thing} this month.",
        "Can I get a refund for {thing}? The invoice looks wrong.",
        "My card was declined when paying for {thing}.",
        "Why did the price of {thing} go up on my last bill?",
        "Please send me a VAT invoice for {thing}.",
    ],
    "technical": [
        "The app crashes whenever I open {thing}.",
        "{thing} shows error 500 after the latest update.",
        "Sync for {thing} has been stuck for two days.",
        "I can't upload files to {thing}, it just spins.",
        "The API returns a timeout when I call {thing}.",
    ],
    "account": [
        "I can't log in to {thing}, password reset email never arrives.",
        "How do I change the email address on {thing}?",
        "Please delete my {thing} account and all data.",
        "Two-factor codes for {thing} are not working.",
        "I want to add a teammate to {thing}.",
    ],
    "shipping": [
        "My order of {thing} hasn't arrived after ten days.",
        "The tracking number for {thing} doesn't work.",
        "{thing} arrived damaged, the box was crushed.",
        "Can I change the delivery address for {thing}?",
        "I received the wrong {thing} in my parcel.",
    ],
}
THINGS = [
    "the Pro plan",
    "my workspace",
    "the dashboard",
    "order #4471",
    "the starter kit",
    "the mobile app",
    "my subscription",
    "the desk lamp",
    "the team plan",
    "the headset",
]

rows = []
rng = random.Random(7)
for label, temps in TEMPLATES.items():
    for i, (t, thing) in enumerate(itertools.product(temps, THINGS)):
        rows.append(
            {
                "input": t.format(thing=thing),
                "expected": label,
                "split": "test" if i % 5 == 0 else "dev",
                "tags": [label],
            }
        )
rng.shuffle(rows)
for n, r in enumerate(rows):
    r["id"] = f"tkt-{n:04d}"
out = Path(__file__).with_name("support-tickets.jsonl")
out.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))
print(f"wrote {len(rows)} rows to {out}")
