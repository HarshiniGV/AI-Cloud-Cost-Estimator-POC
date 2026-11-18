"""
gcp_pricing.py - Gemini mapping + deterministic GCP price lookup
-----------------------------------------------------------------
Same shape as aws_pricing.py / oci_pricing.py: run_gcp_estimate(file_bytes)
returns {"total", "report", "warnings"} so the frontend renders it with the
same table as OCI and AWS.

Gemini is used ONCE to map each sheet row to a GCP machine family / disk type /
Cloud Storage class. The prices themselves come from the SKU lists (same CPU/RAM
matching rules as the original cost_calculator.py), so the maths stays
deterministic.

Supported: Compute Engine (vm), Persistent Disk (disk), Cloud Storage (object_storage).

Console test:  python gcp_pricing.py path/to/bom.xlsx
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

# Uses the GEMINI_API_KEY environment variable if set, otherwise the hard-coded
# value below. TEMPORARY: paste your key here for now, and do not commit or
# share this file while a real key is in it.
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY") or "PASTE_YOUR_GEMINI_API_KEY_HERE"

GCP_API_KEY = os.getenv("GCP_API_KEY")
GCP_REGION = os.getenv("GCP_REGION", "us-central1")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
HOURS_PER_MONTH = 744  # 31-day month, same as AWS engine

COMPUTE_ENGINE_SERVICE_ID = "6F81-5844-456A"
SKU_URL = f"https://cloudbilling.googleapis.com/v1/services/{COMPUTE_ENGINE_SERVICE_ID}/skus"
SKU_CACHE_FILE = os.getenv("GCP_SKU_CACHE", "gcp_skus_cache.json")
SKU_SEED_FILE = os.path.join(os.path.dirname(__file__), "gcp_skus_seed.json")  # first page only

# Cloud Storage SKU configuration
GCS_SERVICE_ID = os.getenv("GCS_SERVICE_ID", "95FF-2EF5-5EA1")  # Cloud Storage
GCS_SKU_URL = f"https://cloudbilling.googleapis.com/v1/services/{GCS_SERVICE_ID}/skus"
GCS_SKU_CACHE_FILE = os.getenv("GCS_SKU_CACHE", "gcs_skus_cache.json")

GCS_STORAGE_CLASSES = {"standard", "nearline", "coldline", "archive"}
# When the sheet gives one request count with no Class A/B split, price it all as this class
GCS_DEFAULT_REQUEST_CLASS = os.getenv("GCS_DEFAULT_REQUEST_CLASS", "A").upper()
GCS_FALLBACK_REGIONS = ["us-east1", "us-east4", "us-west1", "us-central1", "europe-west1", "asia-south1"]
GCS_EXCLUDED_TERMS = [
    "early delete", "autoclass", "dual-region", "dual region", "multi-region",
    "multi region", "multiregion", "turbo", "rapid", "anywhere cache", "replication",
    "durable reduced availability",
    "tagging", "hns", "hierarchical",  # tagging ops and hierarchical-namespace bucket variants
]

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
_gcs_skus_cache: List[Dict[str, Any]] = []

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


def fetch_gcs_skus(force: bool = False) -> List[Dict[str, Any]]:
    """Memory cache -> disk cache -> live Cloud Billing API (needs GCP_API_KEY)."""
    global _gcs_skus_cache

    if _gcs_skus_cache and not force:
        return _gcs_skus_cache

    if not force and os.path.exists(GCS_SKU_CACHE_FILE):
        with open(GCS_SKU_CACHE_FILE, encoding="utf-8") as f:
            _gcs_skus_cache = json.load(f)["skus"]
        return _gcs_skus_cache

    if not GCP_API_KEY:
        raise ValueError("GCP_API_KEY is required to fetch Cloud Storage SKUs.")

    skus, token = [], None
    while True:
        params = {"key": GCP_API_KEY, "pageSize": 5000, "currencyCode": "USD"}
        if token:
            params["pageToken"] = token
        r = requests.get(GCS_SKU_URL, params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        skus.extend(data.get("skus", []))
        token = data.get("nextPageToken")
        if not token:
            break

    if not skus:
        raise ValueError("No Cloud Storage SKUs were returned by Google.")

    _gcs_skus_cache = skus
    with open(GCS_SKU_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump({"skus": skus}, f)
    return _gcs_skus_cache


def _latest_price(sku: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Extract the latest USD pricing information from a SKU.

    Preserves all pricing tiers so tiered services such as
    Cloud Storage can calculate charges accurately.

    The 'rate' field is the first tier's rate, retained for compatibility
    with the existing Compute Engine and Persistent Disk pricing code.

    Note: unitPrice is per ONE usageUnit (e.g. one operation); displayQuantity
    (e.g. 1000) is only how the console displays it, so usage x rate is correct.
    """
    infos = sku.get("pricingInfo") or []
    if not infos:
        return None

    # Select the most recently effective pricing information.
    info = max(infos, key=lambda i: i.get("effectiveTime", ""))

    expr = info.get("pricingExpression", {})
    tiers = expr.get("tieredRates", [])
    if not tiers:
        return None

    extracted_tiers = []
    for tier in tiers:
        money = tier.get("unitPrice", {})
        if money.get("currencyCode", "USD") != "USD":
            return None

        rate = (
            Decimal(str(money.get("units", "0") or "0"))
            + Decimal(str(money.get("nanos", "0") or "0")) / Decimal("1000000000")
        )
        extracted_tiers.append({
            "start_usage_amount": Decimal(str(tier.get("startUsageAmount", 0) or 0)),
            "rate": rate,
        })

    # Ensure tiers are in ascending usage order.
    extracted_tiers.sort(key=lambda t: t["start_usage_amount"])

    return {
        # Backward compatibility for existing VM and disk pricing.
        "rate": extracted_tiers[0]["rate"],
        # Billing unit used by the SKU.
        "unit": expr.get("usageUnit", ""),
        # Every pricing tier, for tiered calculations.
        "tiers": extracted_tiers,
        "base_unit": expr.get("baseUnit", ""),
        "base_unit_conversion_factor": Decimal(
            str(expr.get("baseUnitConversionFactor", 1) or 1)
        ),
        "usage_unit_description": expr.get("usageUnitDescription", ""),
    }


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


_REGION_RE = re.compile(r"[a-z]+-[a-z]+\d+")


def _gcs_candidates(
    include: List[str],
    expected_unit: str,
    exclude: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """All on-demand Cloud Storage SKUs matching the text/unit filters, in any region.

    Multi-region / dual-region / Autoclass SKUs are removed by description
    (GCS_EXCLUDED_TERMS). We deliberately do NOT drop SKUs by geoTaxonomy.type:
    the plain "Standard Storage US Regional" SKU can cover several regions and
    must stay in the pool.
    """
    exclude = [e.lower() for e in (exclude or [])]
    out = []
    for sku in fetch_gcs_skus():
        if sku.get("category", {}).get("usageType") != "OnDemand":
            continue

        desc = sku.get("description", "")
        low = desc.lower()
        if not all(t.lower() in low for t in include):
            continue
        if any(t in low for t in GCS_EXCLUDED_TERMS):
            continue
        if any(t in low for t in exclude):
            continue

        price = _latest_price(sku)
        if not price or price["unit"] != expected_unit:
            continue

        geo = sku.get("geoTaxonomy", {})
        regions = set(geo.get("regions", [])) | set(sku.get("serviceRegions", []))
        out.append({"sku_id": sku["skuId"], "description": desc, "regions": regions, **price})
    return out


def _gcs_find_sku(
    include: List[str],
    location: str,
    expected_unit: str,
    exclude: Optional[List[str]] = None,
    allow_global: bool = False,
) -> Dict[str, Any]:
    """Find exactly one on-demand Cloud Storage SKU for the given region.

    allow_global=True also accepts SKUs whose region is "global" (Cloud Storage
    operations SKUs are global: geoTaxonomy GLOBAL, serviceRegions ["global"]).
    """
    cands = [
        c for c in _gcs_candidates(include, expected_unit, exclude)
        if location in c["regions"] or (allow_global and "global" in c["regions"])
    ]
    if not cands:
        raise ValueError(f"No Cloud Storage SKU {include} found in {location}")
    if len(cands) > 1:
        raise ValueError(
            f"Multiple Cloud Storage SKUs {include} found in {location}: "
            f"{[c['description'] for c in cands]}"
        )
    return cands[0]


def _gcs_pick_location(storage_class: str, requested: str) -> str:
    """Use the requested region if it has a capacity SKU, otherwise fall back to any region that does."""
    cands = _gcs_candidates(
        [storage_class, "storage"], "GiBy.mo",
        exclude=["retrieval", "operations", "class a", "class b"],
    )
    regions = set()
    for c in cands:
        regions |= {r for r in c["regions"] if _REGION_RE.fullmatch(r)}

    if requested in regions:
        return requested
    if not regions:
        all_skus = fetch_gcs_skus()
        sample = sorted({
            s.get("description", "") for s in all_skus
            if storage_class in s.get("description", "").lower()
        })[:10]
        raise ValueError(
            f"No {storage_class} storage capacity SKU matched in any region "
            f"({len(all_skus)} Cloud Storage SKUs loaded). Sample descriptions: {sample}"
        )
    for r in GCS_FALLBACK_REGIONS:
        if r in regions:
            return r
    return sorted(regions)[0]


def _tiered_cost(usage: Decimal, tiers: List[Dict[str, Any]]) -> Decimal:
    """Apply each tier's rate to the slice of usage that falls inside it."""
    cost = Decimal("0")
    for i, t in enumerate(tiers):
        start = t["start_usage_amount"]
        end = tiers[i + 1]["start_usage_amount"] if i + 1 < len(tiers) else None
        if usage <= start:
            break
        upper = usage if end is None else min(usage, end)
        cost += (upper - start) * t["rate"]
    return cost


# ---------------------------------------------------------------------------
# Gemini (one batched call): sheet row -> GCP machine family / disk type / GCS class
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
                    "kind": {
                        "type": "string",
                        "description": "vm, disk, object_storage, or unsupported",
                    },
                    "machine_family": {
                        "type": "string",
                        "description": "For vm: one of " + ", ".join(sorted(SUPPORTED_FAMILIES)),
                    },
                    "vcpu": {
                        "type": "number",
                        "description": "For vm: the item's OCPU multiplied by 2 (OCPU 1 -> 2, OCPU 4 -> 8). Never the raw OCPU value.",
                    },
                    "ram_gib": {"type": "number"},
                    "disk_type": {
                        "type": "string",
                        "description": "For disk: pd-ssd, pd-balanced or pd-standard",
                    },
                    "size_gb": {"type": "number"},

                    # Object Storage fields
                    "storage_class": {"type": "string"},
                    "capacity_gib": {"type": "number"},
                    "class_a_ops": {"type": "number"},
                    "class_b_ops": {"type": "number"},
                    "requests_per_month": {"type": "number"},
                    "retrieval_gib": {"type": "number"},
                    "storage_location": {"type": "string"},

                    "reasoning": {
                        "type": "string",
                        "description": "Short explanation. For vm, show the arithmetic, e.g. 'OCPU 1 x 2 = 2 vCPUs'.",
                    },
                },
                "required": ["item_index", "kind", "reasoning"],
            },
        },
    },
    "required": ["mappings"],
}

SYSTEM_INSTRUCTIONS = f"""You map infrastructure requirement items (any vendor's terminology) to Google Cloud Compute Engine.
Each item has item_index, category and its original fields. Return one mapping per item, same item_index.
- VM/instance -> kind "vm". Choose machine_family from: {", ".join(sorted(SUPPORTED_FAMILIES))} (always E2, unless the item explicitly asks for high/premium performance, then N2;
  never pick any other family for the same kind of item - identical specs must map identically).
  Sizing: vcpu = the item's OCPU x 2 (1 OCPU = 2 vCPUs, so OCPU 1 -> vcpu 2, OCPU 4 -> vcpu 8...). This multiplication is REQUIRED; never copy the OCPU value into vcpu. ram_gib = the item's memory in GiB, unchanged. Do not otherwise resize or "optimise". Windows/licensed workloads are still priced as Linux compute.
- Block storage / disks -> kind "disk", disk_type pd-balanced (default),
  pd-ssd for high performance, pd-standard for cold. size_gb = capacity.

- Object storage (item Type is Object/Blob/Bucket) -> kind "object_storage".
  storage_class: standard if the item gives no tier or performance; otherwise map
  hot -> standard, infrequent access -> nearline, cool -> coldline, archive -> archive.
  capacity_gib = the item's storage size number, copied unchanged (Storage_GB 62000 -> 62000).
  requests_per_month = the item's total monthly request count when it gives a single number
  (for example Requests_Per_Month). Use class_a_ops / class_b_ops only if the item
  explicitly separates them.
  retrieval_gib = monthly retrieved data in GiB, only if stated.
  storage_location = a GCP region only if the item names one, otherwise leave empty.
  Return 0 for any numeric parameter that is not stated. Never invent usage values.

- File storage, databases, networking and anything else
  -> kind "unsupported"."""


def gemini_map(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    client = make_client(GEMINI_API_KEY)
    resp = generate_with_retry(
        client, GEMINI_MODEL, json.dumps({"items": items}, default=str),
        types.GenerateContentConfig(
            system_instruction=SYSTEM_INSTRUCTIONS,
            response_mime_type="application/json",
            response_schema=MAPPING_SCHEMA,
            temperature=0,
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


def price_object_storage(m: Dict[str, Any]) -> Dict[str, Any]:
    """Google Cloud Storage cost: capacity + Class A/B operations + retrieval (egress not included)."""
    storage_class = str(m.get("storage_class") or "standard").strip().lower()
    if storage_class not in GCS_STORAGE_CLASSES:
        raise ValueError(f"Unsupported Cloud Storage class: {storage_class}")

    capacity = Decimal(str(m.get("capacity_gib") or 0))
    class_a = Decimal(str(m.get("class_a_ops") or 0))
    class_b = Decimal(str(m.get("class_b_ops") or 0))
    requests_total = Decimal(str(m.get("requests_per_month") or 0))
    retrieval = Decimal(str(m.get("retrieval_gib") or 0))

    if min(capacity, class_a, class_b, requests_total, retrieval) < 0:
        raise ValueError("Storage usage cannot be negative.")

    loc = str(m.get("storage_location") or "").strip().lower()
    requested = loc if re.fullmatch(r"[a-z]+-[a-z]+\d+", loc) else GCP_REGION.lower()
    location = _gcs_pick_location(storage_class, requested)

    notes = []
    if location != requested:
        notes.append(
            f"No {storage_class} storage SKU found in {requested}; priced in {location} instead (demo fallback)"
        )
    if class_a == 0 and class_b == 0 and requests_total > 0:
        if GCS_DEFAULT_REQUEST_CLASS == "B":
            class_b = requests_total
        else:
            class_a = requests_total

    total = Decimal("0")
    details, parts = [], []

    def add_charge(name, usage, terms, unit, exclude=None, fallback_terms=None,
                   fallback_exclude=None, allow_global=False):
        nonlocal total
        if usage <= 0:
            return
        try:
            sku = _gcs_find_sku(
                include=terms, location=location, expected_unit=unit,
                exclude=exclude, allow_global=allow_global,
            )
        except ValueError:
            if not fallback_terms:
                raise
            sku = _gcs_find_sku(
                include=fallback_terms, location=location, expected_unit=unit,
                exclude=fallback_exclude, allow_global=allow_global,
            )
        cost = _tiered_cost(usage, sku["tiers"])
        total += cost
        parts.append(f"{name}: {_fmt(usage)} x ${sku['rate']:.6f} = ${cost:.2f}")
        details.append({
            "component": name,
            "usage": str(usage),
            "sku_id": sku["sku_id"],
            "description": sku["description"],
            "cost": str(cost),
        })

    other_classes = sorted(GCS_STORAGE_CLASSES - {storage_class})

    # Capacity SKU must not be confused with the retrieval or operations SKUs
    add_charge(
        "Storage", capacity, [storage_class, "storage"], "GiBy.mo",
        exclude=["retrieval", "operations", "class a", "class b"],
    )
    # Operations SKUs are GLOBAL (not per-region), so allow_global=True.
    # SKU wording varies; if the class name is not in the description, match "class a/b" alone.
    add_charge(
        "Class A ops", class_a, [storage_class, "class a"], "count",
        fallback_terms=["class a"], fallback_exclude=other_classes, allow_global=True,
    )
    add_charge(
        "Class B ops", class_b, [storage_class, "class b"], "count",
        fallback_terms=["class b"], fallback_exclude=other_classes, allow_global=True,
    )
    if storage_class != "standard":
        add_charge("Retrieval", retrieval, [storage_class, "retrieval"], "GiBy")

    return {
        "product": f"Cloud Storage ({storage_class}, {location})",
        "metric": f"{_fmt(capacity)} GiB-month",
        "calculation": " + ".join(parts) + f" = ${total:.2f}",
        "cost": float(round(total, 2)),
        "details": details,
        "note": "; ".join(notes),
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
            continue  # file storage, databases etc. are not priced on GCP; stay silent
        try:
            kind = m.get("kind")

            if kind == "vm":
                row = price_vm(m)
            elif kind == "disk":
                row = price_disk(m)
            elif kind == "object_storage":
                row = price_object_storage(m)
            else:
                continue
        except Exception as e:
            warnings.append(f"{cat}: {e}")
            continue
        report.append({"category": cat, **row})
        if row.get("note"):
            warnings.append(f"{cat}: {row['note']}")
        total += row["cost"]

    return {"total": round(total, 2), "report": report, "warnings": warnings}


# ---------------------------------------------------------------------------
# Console test:  python gcp_pricing.py path/to/bom.xlsx
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    path = sys.argv[1] if len(sys.argv) > 1 else "bom.xlsx"

    with open(path, "rb") as f:
        result = run_gcp_estimate(f.read())

    print(f"\n{'Category':<14} {'Product':<45} {'Cost (USD)':>12}")
    print("-" * 74)
    for row in result["report"]:
        print(f"{row['category']:<14} {row['product']:<45} {row['cost']:>12,.2f}")
        print(f"{'':<14} {row['calculation']}")
    print("-" * 74)
    print(f"{'TOTAL':<60} {result['total']:>12,.2f}")

    if result["warnings"]:
        print("\nWarnings:")
        for w in result["warnings"]:
            print(f"  - {w}")
