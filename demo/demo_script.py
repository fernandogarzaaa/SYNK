"""End-to-end demo against the local harness (no browser needed).

Simulates: content.js capture -> /snapshot -> /plan -> /act with bulk fill.
Run:  python -m harness.server --port 18080   (terminal 1)
      python demo/demo_script.py              (terminal 2)
"""
import json
import urllib.request

BASE = "http://127.0.0.1:18080"


def post(path, body):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())


nodes = [  # what content.js would capture from sample_page.html
    {"role": "textbox", "name": "Email address", "tag": "input",
     "selector": "#email", "interactive": True},
    {"role": "textbox", "name": "Shipping address", "tag": "input",
     "selector": "#addr", "interactive": True},
    {"role": "button", "name": "Submit order", "tag": "button",
     "selector": "#submit", "interactive": True},
    {"role": "navigation", "name": "main nav menu", "tag": "nav", "selector": "nav"},
    {"role": "contentinfo", "name": "footer links", "tag": "footer",
     "selector": "footer"},
]

print("== register origin (Stage F: default-deny policy) ==")
reg = post("/policy/origin", {"origin": "demo.shop",
                              "allow": ["read", "navigate", "interact"],
                              "description": "demo shop"})
print(json.dumps(reg, indent=1)[:400], "\n")

print("== snapshot ==")
view = post("/snapshot", {"url": "https://demo.shop/checkout",
                          "nodes": nodes, "goal": "Fill the checkout form"})
print(f"nodes kept: {len(view['nodes'])}/{len(nodes)}, "
      f"tokens_est ~{view['tokens_est']}")
print(view["prompt"][:600], "...\n")

print("== plan ==")
plan = post("/plan", {"goal": "Fill the checkout form"})
print(json.dumps(plan, indent=1)[:800], "\n")

print("== act ==")
res = post("/act", {"actions": plan["actions"],
                    "page_url": "https://demo.shop/checkout",
                    "note": "demo bulk fill"})
print(json.dumps(res, indent=1)[:1000])

print("\n== audit ==")
audit = post("/nope", {}) if False else None
import urllib.error
try:
    req = urllib.request.Request(BASE + "/audit")
    with urllib.request.urlopen(req, timeout=10) as r:
        a = json.loads(r.read().decode())
    print(f"chain_valid={a['chain_valid']}, entries={len(a['trail'])}")
except urllib.error.HTTPError:
    pass
print("demo complete.")
