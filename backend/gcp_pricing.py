"""
gcp_pricing.py - Gemini mapping + deterministic GCP price lookup
-----------------------------------------------------------------
Same shape as aws_pricing.py / oci_pricing.py: run_gcp_estimate(file_bytes)
returns {"total", "report", "warnings"} so the frontend renders it with the
same table as OCI and AWS.

Gemini is used ONCE to map each sheet row to a GCP machine family / disk type.
The prices themselves come from the SKU list (same CPU/RAM matching rules as
the original cost_calculator.py), so the maths stays deterministic.
"""

import json
import os
import re
from decimal import Decimal
from typing import Any, Dict, List, Optional

import requests
from dotenv import load_dotenv
from google import genai
from google.genai import types

from gemini_retry import generate_with_retry, make_client
from oci_pricing import excel_to_requirements  # same sheet -> requirements parser

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

GCP_API_KEY = os.getenv("GCP_API_KEY")
GCP_REGION = os.getenv("GCP_REGION", "us-central1")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
HOURS_PER_MONTH = 744  # 31-day month, same as AWS engine

COMPUTE_ENGINE_SERVICE_ID = "6F81-5844-456A"
SKU_URL = f"https://cloudbilling.googleapis.com/v1/services/{COMPUTE_ENGINE_SERVICE_ID}/skus"
SKU_CACHE_FILE = os.getenv("GCP_SKU_CACHE", "gcp_skus_cache.json")
SKU_SEED_FILE = os.path.join(os.path.dirname(__file__), "gcp_skus_seed.json")  # first page only

SUPPORTED_FAMILIES = {
    "E2", "N2", "N2D", "N4", "N4D", "C2", "C2D", "C3", "C3D", "C4",
    "M1", "M2", "M3", "M4", "T2D", "T2A",
}
EXCLUDED_TERMS = [
    "sole tenancy", "sole-tenant", "spot", "preemptible", "commitment",
    "committed use", "custom", "license", "gpu",
]
# disk type -> (description must contain, must not contain)
DISK_RULES = {
    "pd-ssd": ("ssd backed pd capacity", ["regional", "hyperdisk"]),
    "pd-balanced": ("balanced pd capacity", ["regional", "hyperdisk"]),
    "pd-standard": ("storage pd capacity", ["regional", "hyperdisk"]),
}
GB_PER_GIB = Decimal("1.073741824")

_skus_cache: List[Dict[str, Any]] = []
_using_seed = False

# ---------------------------------------------------------------------------
# SKU loading (mirrors fetch_products in oci_pricing.py)
# ---------------------------------------------------------------------------


def _fetch_from_api() -> List[Dict[str, Any]]:
    skus, token = [], None
    while True:
        params = {"key": GCP_API_KEY, "pageSize": 5000, "currencyCode": "USD"}
        if token:
            params["pageToken"] = token
        r = requests.get(SKU_URL, params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        skus.extend(data.get("skus", []))
        token = data.get("nextPageToken")
        if not token:
            return skus


def fetch_gcp_skus(force: bool = False) -> List[Dict[str, Any]]:
    """Memory cache -> disk cache -> live API (needs GCP_API_KEY) -> seed file."""
    global _skus_cache, _using_seed
    if _skus_cache and not force:
        return _skus_cache
    if not force and os.path.exists(SKU_CACHE_FILE):
        with open(SKU_CACHE_FILE, encoding="utf-8") as f:
            _skus_cache = json.load(f)["skus"]
        return _skus_cache
    if GCP_API_KEY:
        _skus_cache = _fetch_from_api()
        with open(SKU_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({"skus": _skus_cache}, f)
        _using_seed = False
        return _skus_cache
    with open(SKU_SEED_FILE, encoding="utf-8") as f:
        _skus_cache = json.load(f)["skus"]
    _using_seed = True
    return _skus_cache


def _latest_price(sku: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    infos = sku.get("pricingInfo") or []
    if not infos:
        return None
    info = max(infos, key=lambda i: i.get("effectiveTime", ""))
    expr = info.get("pricingExpression", {})
    tiers = expr.get("tieredRates", [])
    if len(tiers) != 1 or Decimal(str(tiers[0].get("startUsageAmount", 0))) != 0:
        return None
    money = tiers[0].get("unitPrice", {})
    if money.get("currencyCode", "USD") != "USD":
        return None
    rate = (
        Decimal(str(money.get("units", "0")))
        + Decimal(str(money.get("nanos", "0"))) / Decimal("1000000000")
    )
    return {"rate": rate, "unit": expr.get("usageUnit", "")}


def _family_matches(description: str, family: str) -> bool:
    # Exact token match so E2 does not match C2 / N2
    return bool(
        re.search(r"(?<![A-Z0-9])" + re.escape(family) + r"(?![A-Z0-9])", description.upper())
    )


def _candidates(group: str, ok) -> List[Dict[str, Any]]:
    found = {}
    for sku in fetch_gcp_skus():
        cat = sku.get("category", {})
        if cat.get("resourceGroup") != group or cat.get("usageType") != "OnDemand":
            continue
        if GCP_REGION not in sku.get("serviceRegions", []):
            continue
        desc = sku.get("description", "")
        if not ok(desc.lower(), desc):
            continue
        price = _latest_price(sku)
        if price:
            found[sku["skuId"]] = {"sku_id": sku["skuId"], "description": desc, **price}
    return list(found.values())


def _one(cands: List[Dict[str, Any]], what: str) -> Dict[str, Any]:
    if not cands:
        raise ValueError(f"No {what} SKU found in {GCP_REGION}")
    if len(cands) > 1:
        raise ValueError(
            f"Multiple {what} SKUs found in {GCP_REGION}: {[c['description'] for c in cands]}"
        )
    return cands[0]


# ---------------------------------------------------------------------------
# Gemini (one batched call): sheet row -> GCP machine family / disk type
# ---------------------------------------------------------------------------

MAPPING_SCHEMA = {
    "type": "object",
    "properties": {
        "mappings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "item_index": {"type": "integer"},
                    "kind": {"type": "string", "description": "vm, disk, or unsupported"},
                    "machine_family": {
                        "type": "string",
                        "description": "For vm: one of " + ", ".join(sorted(SUPPORTED_FAMILIES)),
                    },
                    "vcpu": {"type": "number"},
                    "ram_gib": {"type": "number"},
                    "disk_type": {
                        "type": "string",
                        "description": "For disk: pd-ssd, pd-balanced or pd-standard",
                    },
                    "size_gb": {"type": "number"},
                    "reasoning": {"type": "string"},
                },
                "required": ["item_index", "kind", "reasoning"],
            },
        },
    },
    "required": ["mappings"],
}

SYSTEM_INSTRUCTIONS = f"""You map infrastructure requirement items (any vendor's terminology) to Google Cloud Compute Engine.
Each item has item_index, category and its original fields. Return one mapping per item, same item_index.
- VM/instance -> kind "vm". Choose machine_family from: {", ".join(sorted(SUPPORTED_FAMILIES))} (default E2, or N2 for higher performance).
  Give vcpu and ram_gib. 1 OCPU = 2 vCPUs. Windows/licensed workloads are still priced as Linux compute.
- Block storage / disks -> kind "disk", disk_type pd-balanced (default), pd-ssd for high performance, pd-standard for cold. size_gb = capacity.
- Object/file storage, databases, networking, anything else -> kind "unsupported".
Respond ONLY via the schema."""


def gemini_map(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    client = make_client(os.getenv("GEMINI_API_KEY"))
    resp = generate_with_retry(
        client, GEMINI_MODEL, json.dumps({"items": items}, default=str),
        types.GenerateContentConfig(
            system_instruction=SYSTEM_INSTRUCTIONS,
            response_mime_type="application/json",
            response_schema=MAPPING_SCHEMA,
            temperature=0.1,
        ),
    )
    return json.loads(resp.text).get("mappings", [])


# ---------------------------------------------------------------------------
# Deterministic pricing
# ---------------------------------------------------------------------------


def _fmt(d: Decimal) -> str:
    """80 -> '80' (Decimal.normalize() would give '8E+1')."""
    return format(d.normalize(), "f")


def price_vm(m: Dict[str, Any]) -> Dict[str, Any]:
    family = str(m.get("machine_family", "")).upper()
    if family not in SUPPORTED_FAMILIES:
        raise ValueError(f"Unsupported machine family: {family}")
    vcpu, ram = Decimal(str(m["vcpu"])), Decimal(str(m["ram_gib"]))

    def ok(low, desc):
        return not any(t in low for t in EXCLUDED_TERMS) and _family_matches(desc, family)

    cpu = _one(_candidates("CPU", ok), f"{family} CPU")
    mem = _one(_candidates("RAM", ok), f"{family} RAM")
    if cpu["unit"] != "h":
        raise ValueError(f"Unexpected CPU billing unit: {cpu['unit']}")
    if mem["unit"] == "GBy.h":
        billable_ram = ram * GB_PER_GIB  # sheet RAM is GiB, SKU bills GB
    elif mem["unit"] == "GiBy.h":
        billable_ram = ram
    else:
        raise ValueError(f"Unsupported RAM billing unit: {mem['unit']}")

    hrs = Decimal(HOURS_PER_MONTH)
    cost = vcpu * cpu["rate"] * hrs + billable_ram * mem["rate"] * hrs
    return {
        "product": f"Compute Engine {family} ({_fmt(vcpu)} vCPU, {_fmt(ram)} GiB)",
        "metric": f"{HOURS_PER_MONTH} hrs/month",
        "calculation": (
            f"{_fmt(vcpu)} vCPU x ${cpu['rate']:.6f}/hr x {HOURS_PER_MONTH} + "
            f"{billable_ram:.2f} x ${mem['rate']:.6f}/hr x {HOURS_PER_MONTH} = ${cost:.2f}"
        ),
        "cost": float(round(cost, 2)),
    }


def price_disk(m: Dict[str, Any]) -> Dict[str, Any]:
    dtype = str(m.get("disk_type", "pd-balanced")).lower()
    if dtype not in DISK_RULES:
        raise ValueError(f"Unsupported disk type: {dtype}")
    must, banned = DISK_RULES[dtype]

    def ok(low, desc):
        return must in low and not any(b in low for b in banned)

    sku = _one(_candidates("SSD", ok) + _candidates("PDStandard", ok), f"{dtype} capacity")
    size = Decimal(str(m["size_gb"]))
    cost = size * sku["rate"]
    return {
        "product": f"Persistent Disk ({dtype})",
        "metric": f"{_fmt(size)} GB-month",
        "calculation": f"{_fmt(size)} GB x ${sku['rate']:.4f}/GB-month = ${cost:.2f}",
        "cost": float(round(cost, 2)),
    }


# ---------------------------------------------------------------------------
# Public entry point - matches run_oci_estimate / run_aws_estimate
# ---------------------------------------------------------------------------


def run_gcp_estimate(file_bytes: bytes) -> Dict[str, Any]:
    reqs = excel_to_requirements(file_bytes)
    if not reqs:
        return {"total": 0, "report": [], "warnings": ["No requirements found in the uploaded sheet"]}

    items = []
    for block in reqs:
        for detail in block["details"]:
            items.append({"item_index": len(items), "category": block["category"], **detail})

    by_idx = {m["item_index"]: m for m in gemini_map(items)}
    report, warnings, total = [], [], 0.0

    fetch_gcp_skus()
    if _using_seed:
        warnings.append(
            "GCP_API_KEY not set - using the partial seed SKU file, so some prices may be missing"
        )

    for item in items:
        m = by_idx.get(item["item_index"])
        cat = item["category"]
        if not m or m.get("kind") == "unsupported":
            continue  # object/file storage etc. are not priced on GCP; stay silent
        try:
            row = price_vm(m) if m["kind"] == "vm" else price_disk(m)
        except Exception as e:
            warnings.append(f"{cat}: {e}")
            continue
        report.append({"category": cat, **row})
        total += row["cost"]

    return {"total": round(total, 2), "report": report, "warnings": warnings}