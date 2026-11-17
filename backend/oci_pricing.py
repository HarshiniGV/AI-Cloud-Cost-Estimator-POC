import os
import io
import re
import json
from decimal import Decimal
from typing import List, Dict, Any, Optional
import requests
from openpyxl import load_workbook
from dotenv import load_dotenv
from google import genai
from google.genai import types
from gemini_retry import generate_with_retry, make_client, use_oci

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
ORACLE_PRICE_URL = "https://apexapps.oracle.com/pls/apex/cetools/api/v1/products?currencyCode=USD"

if not GEMINI_API_KEY and not use_oci():
    raise RuntimeError(
        "GEMINI_API_KEY not set. Copy backend/.env.example to backend/.env and fill it in "
        "(or set LLM_PROVIDER=oci)."
    )

client = make_client(GEMINI_API_KEY)

# ---------------------------------------------------------------------------
# The model only decides WHICH product + metric and HOW MUCH (qty).
# It must copy productName / metricName EXACTLY from Available Products.
# Python then looks up the real unitPrice (full precision, tier-aware) and
# does the multiplication, so prices can never be rounded or invented.
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """You are an Oracle Cloud Infrastructure pricing expert.
You will be given a list of Requirements (each with a "category" and
"details") and a list of Available Products (each with displayName,
metricName, unitPrice, and the "category" it was matched against).

For EACH requirement, work out which product(s)/metric(s) are needed and how
much of each metric is consumed. Return ONLY a JSON array, no markdown
fences, no commentary, covering ALL requirements together:

[
  {
    "category": "<the requirement's category, copied exactly>",
    "label": "<descriptive label for this line item, e.g. 'VM 1 - OCPU (E5)'>",
    "productName": "<displayName copied EXACTLY, character for character, from the Available Products entry you used>",
    "metricName": "<metricName copied EXACTLY from that same entry>",
    "qty": <numeric quantity of the metric consumed over a month>,
    "calculation": "<plain-English derivation of qty, e.g. '744 hrs/month * 2 OCPUs = 1488'>"
  }
]

Rules:
- productName and metricName MUST be an exact pair from Available Products
  for that requirement's category. Never invent or reword them. Do NOT
  output prices or costs; they are computed by the system.
- "qty" is the raw consumption of the metric for one month (744 hours per
  month for hourly metrics). Do NOT subtract free-tier amounts - free
  tiers are applied automatically by the system. Do not round qty.
- Multiply by the "Count" of each requirement row where present.
- VM SHAPE SELECTION (fixed procedure, so the same input always gives the
  same answer):
    A. If a requirement row has a "Shape" value (e.g. "E5", "E4"), you MUST
       use that family: the product whose displayName contains
       "Standard - <Shape>" for the OCPU line and the same family's Memory
       product for the Memory line. Never substitute another family for a
       row that specifies a shape. If the specified family does not exist
       in Available Products, still pick the closest same-letter family
       and say so in "calculation".
    B. If the row has NO Shape (blank/missing), use ONLY E-series (AMD,
       "Standard - E<n>") flexible shapes:
         1. List the E-series families that appear in Available Products
            with BOTH an OCPU metric and a Memory metric.
         2. Pick the family with the LOWEST OCPU unitPrice; on a tie pick
            the HIGHEST generation number.
         3. Use that same family for the OCPU and Memory lines of every
            row without a Shape.
    C. Never use Micro, Dense I/O, GPU, Bare Metal, VMware, Ax or other
       non-standard shapes.
- Each VM needs: one OCPU line (qty = 744 * OCPU * Count), one Memory line
  (qty = 744 * Memory_GB * Count), and one Block Volume Storage line
  (qty = Storage_GB * Count) plus one Block Volume Performance Units line
  (qty = Performance_Units * Count if given, otherwise Storage_GB * 10
  * Count, or * 20 for high performance). Label storage lines clearly as
  VM storage in "label".
- Include an OS image / licensing line item only when the requirement
  states an OS that has a matching product in Available Products (pick the
  cheapest matching one). Linux has no charge - add no line for it.
- Whenever more than one Available Product fits equally well, choose the
  lowest unitPrice; if still tied, the displayName that sorts first
  alphabetically.
- Object Storage: use "Object Storage - Storage" with qty = Storage_GB and
  "Object Storage - Requests" with qty = Requests_Per_Month / 10000.
- Avoid Cloud@Customer products. Avoid Exadata unless specified.
- Do NOT apply or mention tax/GST anywhere.
- Every requirement must be addressed by at least one item in the output
  (unless truly no candidate product fits it).
"""

_products_cache: List[Dict[str, Any]] = []


def fetch_products(force: bool = False) -> List[Dict[str, Any]]:
    global _products_cache
    if force or not _products_cache:
        resp = requests.get(ORACLE_PRICE_URL, timeout=30)
        resp.raise_for_status()
        _products_cache = resp.json()["items"]
    return _products_cache


def _tiers(p) -> List[Dict[str, Any]]:
    locs = p.get("currencyCodeLocalizations") or []
    if not locs:
        return []
    return locs[0].get("prices") or []


def build_lookup(products) -> Dict[tuple, List[Dict[str, Any]]]:
    """(displayName, metricName) -> full list of price tiers (not just tier 0)."""
    lk = {}
    for p in products:
        tiers = _tiers(p)
        if tiers:
            lk[(p["displayName"], p["metricName"])] = tiers
    return lk


def headline_price(tiers) -> float:
    """Price shown to the model: first PAID tier if there is a free tier."""
    paid = [t["value"] for t in tiers if t["value"] > 0]
    return paid[0] if paid else tiers[0]["value"]


def tiered_cost(tiers, qty) -> Decimal:
    """Exact (Decimal) cost, honouring free tiers / volume tiers."""
    q = Decimal(str(qty))
    if len(tiers) == 1 and "rangeMin" not in tiers[0]:
        return q * Decimal(str(tiers[0]["value"]))
    total = Decimal(0)
    for t in tiers:
        lo = Decimal(str(t.get("rangeMin", 0)))
        hi = Decimal(str(t.get("rangeMax", 10 ** 15)))
        used = min(q, hi) - lo
        if used > 0:
            total += used * Decimal(str(t["value"]))
    return total


_E_FAMILY = re.compile(r"standard\s*-\s*(e\d+)\s*-", re.I)


def _family(display_name: str) -> Optional[str]:
    m = _E_FAMILY.search(display_name)
    return m.group(1).upper() if m else None


def _shapes_in(req) -> set:
    return {
        str(d.get("Shape")).strip().upper()
        for d in req.get("details", [])
        if d.get("Shape")
    }


def relevant_products(category_name: str, products, lookup, shapes=None, limit: int = None):
    target = category_name.lower().strip()
    results = []
    for p in products:
        svc_cat = p.get("serviceCategory", "").lower().strip()
        if target in svc_cat and "cloud@customer" not in svc_cat:
            key = (p["displayName"], p["metricName"])
            if key in lookup:
                results.append({
                    "displayName": p["displayName"],
                    "metricName": p["metricName"],
                    "unitPrice": headline_price(lookup[key]),
                })

    # Prioritise what matters so the `limit` can never cut it off:
    # requested shape families first, then other E-series, then the rest.
    shapes = shapes or set()

    def rank(c):
        fam = _family(c["displayName"])
        if fam and fam in shapes:
            return 0
        if fam:
            return 1
        return 2

    results.sort(key=lambda c: (rank(c), c["displayName"]))
    return results[:limit] if limit else results


VM_STORAGE_KEYWORDS = ("vm", "virtual machine", "compute", "ec2", "storage",
                       "block volume", "disk", "s3", "efs", "file system")


def _only_vm_and_storage(reqs):
    return [r for r in reqs
            if any(k in r["category"].lower() for k in VM_STORAGE_KEYWORDS)]


def excel_to_requirements(file_bytes):
    wb = load_workbook(io.BytesIO(file_bytes), data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    output = []
    i = 0
    while i < len(rows):
        row = rows[i]
        if row and row[0] and str(row[0]).strip().startswith("Category:"):
            category = str(row[0]).replace("Category:", "").strip()
            if i + 1 >= len(rows):
                break
            headers = [
                str(h).strip() if h is not None else None
                for h in rows[i + 1]
            ]
            details = []
            j = i + 2
            while j < len(rows):
                current = rows[j]
                if current and current[0] and str(current[0]).strip().startswith("Category:"):
                    break
                if not any(cell is not None for cell in current):
                    j += 1
                    continue
                item = {}
                for k, header in enumerate(headers):
                    if header and k < len(current) and current[k] is not None:
                        item[header] = current[k]
                if item:
                    details.append(item)
                j += 1
            output.append({"category": category, "details": details})
            i = j
        else:
            i += 1

    return _only_vm_and_storage(output)


def build_batch_candidates(reqs, products, lookup, limit_per_req: int = 150):
    seen = set()
    batch_candidates = []
    for req in reqs:
        category = req.get("category", "")
        cands = relevant_products(
            category, products, lookup, shapes=_shapes_in(req), limit=limit_per_req
        )
        for c in cands:
            key = (category, c["displayName"], c["metricName"])
            if key in seen:
                continue
            seen.add(key)
            batch_candidates.append({
                "category": category,
                "displayName": c["displayName"],
                "metricName": c["metricName"],
                "unitPrice": c["unitPrice"],
            })
    return batch_candidates


def ask_gemini_batch(reqs: List[Dict[str, Any]], candidates: List[Dict[str, Any]]):
    prompt = (
        SYSTEM_PROMPT
        + "\nRequirements:\n" + json.dumps(reqs, indent=2, default=str)
        + "\nAvailable Products:\n" + json.dumps(candidates, indent=2, default=str)
    )
    r = generate_with_retry(
        client, GEMINI_MODEL, prompt,
        types.GenerateContentConfig(response_mime_type="application/json", temperature=0),
    )
    return json.loads(r.text)


def run_oci_estimate(file_bytes: bytes) -> Dict[str, Any]:
    reqs = excel_to_requirements(file_bytes)

    if not reqs:
        return {"report": [], "total": 0, "warnings": ["No requirements found in the uploaded sheet"]}

    products = fetch_products()
    lookup = build_lookup(products)

    warnings: List[str] = []
    batch_candidates = build_batch_candidates(reqs, products, lookup)

    matched_categories = {c["category"] for c in batch_candidates}
    for req in reqs:
        cat = req.get("category", "")
        if cat not in matched_categories:
            warnings.append(f"No matching OCI products found for '{cat}'")

    if not batch_candidates:
        return {"report": [], "total": 0, "warnings": warnings}

    items = ask_gemini_batch(reqs, batch_candidates)

    report = []
    total = Decimal(0)
    for it in items:
        name = it.get("productName") or it.get("displayName", "")
        metric = it.get("metricName", "")
        tiers = lookup.get((name, metric))
        if not tiers:
            warnings.append(f"Unresolved product/metric '{name}' / '{metric}'; skipped")
            continue
        try:
            qty = Decimal(str(it.get("qty", 0)))
        except Exception:
            warnings.append(f"Could not parse qty for '{name}'; skipped")
            continue

        cost = tiered_cost(tiers, qty)          # exact, tier-aware
        total += cost
        report.append({
            "category": it.get("category", ""),
            "product": it.get("label") or name,
            "productName": name,
            "metric": metric,
            "unitPrice": headline_price(tiers),  # full precision, never rounded
            "qty": float(qty),
            "calculation": it.get("calculation", ""),
            "cost": float(round(cost, 4)),       # keep 4dp; round only for display
        })

    return {"report": report, "total": float(round(total, 2)), "warnings": warnings}