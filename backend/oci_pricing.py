import io
import json
import os
import re
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any, Dict, List, Optional, Set, Tuple

import requests
from dotenv import load_dotenv
from openpyxl import load_workbook
from google.genai import types

from gemini_retry import generate_with_retry, make_client, use_oci


load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
ORACLE_PRICE_URL = (
    "https://apexapps.oracle.com/pls/apex/cetools/api/v1/products"
    "?currencyCode=USD"
)

if not GEMINI_API_KEY and not use_oci():
    raise RuntimeError(
        "GEMINI_API_KEY not set. Copy backend/.env.example to backend/.env "
        "and fill it in (or set LLM_PROVIDER=oci)."
    )

client = make_client(GEMINI_API_KEY)


# Gemini identifies the products and determines consumption quantities.
# Python validates the exact product/metric and performs all arithmetic with
# Decimal. Gemini is NOT trusted to calculate or return prices/costs.
SYSTEM_PROMPT = """You are an Oracle Cloud Infrastructure pricing expert.

You will receive:
1. Requirements parsed from an Excel workbook. Each requirement has a
   category and one or more detail rows.
2. Available Products. Each product has requirementIndex, category,
   displayName, metricName, and unitPrice.

For every requirement detail row, identify every required billable product
and return the quantity consumed for each metric.

Return ONLY a valid JSON array. Do not include markdown or commentary.
Each output item must have this structure:

[
  {
    "requirementIndex": 0,
    "category": "exact requirement category",
    "displayName": "human-readable line-item label",
    "sourceDisplayName": "EXACT displayName copied from Available Products",
    "metricName": "EXACT metricName copied from Available Products",
    "qty": "exact numeric quantity as a string"
  }
]

RULES:
- Use only products supplied in Available Products for the matching
  requirementIndex and category.
- sourceDisplayName and metricName MUST be copied exactly from the selected
  Available Products entry. Do not paraphrase, rename, or reconstruct them.
- Return qty as a numeric string. Do not return unitPrice or cost; Python
  retrieves the authoritative unitPrice and calculates cost.
- Do not round, truncate, or approximate quantities.
- Do not invent products, metrics, prices, or quantities.
- If a required product is not available, do not substitute an unrelated
  product. Python will report products/families that could not be priced.

VM SHAPE RULES:
- The VM shape/family specified in the Excel requirement MUST be followed.
  For example, E5 means use only E5-family pricing products; E4 means only
  E4-family pricing products.
- Never choose a different E-series family because it is cheaper.
- Use the same requested E-series family for the VM's OCPU and Memory lines.
- Never mix OCPU from one family with Memory from another family.
- Available VM products are filtered by Python for the requested family.
  Select only from those supplied candidates.
- Do not use Micro, Dense I/O, GPU, Bare Metal, or other non-requested shapes.
- If the Excel requirement contains multiple VMs with different requested
  families, price each VM using its own specified family.
- For VM storage, use Block Storage products and give the output line a clear
  label such as "VM Storage". Keep sourceDisplayName exact.
- Include an OS image/licensing line only when the requirement specifies an
  OS and a matching product is supplied in Available Products. If multiple
  matching OS products are supplied, choose the lowest unit-price product.

OTHER RULES:
- When multiple products fit equally well, choose the lowest unit price;
  if tied, choose the alphabetically first displayName.
- Avoid Cloud@Customer products whenever a non-Cloud@Customer alternative
  exists. Avoid Exadata unless explicitly specified.
- All prices and costs are pre-tax. Do not apply or mention tax/GST.
- Address every requirement detail row that has matching available products.
- Block Storage PU = GB * 20 for high performance, otherwise GB * 10.
- Show the consumption quantity only. Python calculates all costs exactly.
"""


_products_cache: List[Dict[str, Any]] = []


# Matches E-series family references such as E5, E4, E5 Flex, or
# VM.Standard.E5.Flex.
_E_SERIES_PATTERN = re.compile(r"\bE\s*(\d+)\b", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Oracle product and price retrieval
# ---------------------------------------------------------------------------

def fetch_products(force: bool = False) -> List[Dict[str, Any]]:
    """Fetch Oracle USD products, preserving JSON decimal values as Decimal."""
    global _products_cache

    if force or not _products_cache:
        response = requests.get(ORACLE_PRICE_URL, timeout=30)
        response.raise_for_status()
        payload = json.loads(response.text, parse_float=Decimal)
        _products_cache = payload.get("items", [])

    return _products_cache


def build_lookup(products: List[Dict[str, Any]]) -> Dict[Tuple[str, str], Decimal]:
    """Build an exact product/metric -> USD unit price lookup."""
    lookup: Dict[Tuple[str, str], Decimal] = {}

    for product in products:
        localizations = product.get("currencyCodeLocalizations") or []
        if not localizations:
            continue

        prices = localizations[0].get("prices") or []
        if not prices:
            continue

        display_name = product.get("displayName")
        metric_name = product.get("metricName")
        if not display_name or not metric_name:
            continue

        try:
            lookup[(display_name, metric_name)] = Decimal(str(prices[0]["value"]))
        except (KeyError, TypeError, ValueError, InvalidOperation):
            continue

    return lookup


def relevant_products(
    category_name: str,
    products: List[Dict[str, Any]],
    lookup: Dict[Tuple[str, str], Decimal],
    limit: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Return priced products whose serviceCategory matches the requirement."""
    target = category_name.lower().strip()
    results: List[Dict[str, Any]] = []

    for product in products:
        service_category = str(product.get("serviceCategory", "")).lower().strip()

        if target not in service_category:
            continue
        if "cloud@customer" in service_category:
            continue

        display_name = product.get("displayName")
        metric_name = product.get("metricName")
        key = (display_name, metric_name)

        if key not in lookup:
            continue

        results.append(
            {
                "displayName": display_name,
                "metricName": metric_name,
                "unitPrice": str(lookup[key]),
            }
        )

    return results[:limit] if limit else results


# ---------------------------------------------------------------------------
# Requirement parsing
# ---------------------------------------------------------------------------

VM_STORAGE_KEYWORDS = (
    "vm",
    "virtual machine",
    "compute",
    "ec2",
    "storage",
    "block volume",
    "disk",
    "s3",
    "efs",
    "file system",
)

VM_CATEGORY_KEYWORDS = ("vm", "virtual machine", "compute", "ec2")


def _only_vm_and_storage(requirements: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        requirement
        for requirement in requirements
        if any(
            keyword in requirement.get("category", "").lower()
            for keyword in VM_STORAGE_KEYWORDS
        )
    ]


def _is_vm_requirement(category: str) -> bool:
    category_lower = category.lower()
    return any(keyword in category_lower for keyword in VM_CATEGORY_KEYWORDS)


def excel_to_requirements(file_bytes: bytes) -> List[Dict[str, Any]]:
    """Parse the workbook's Category / header / detail-row layout."""
    workbook = load_workbook(io.BytesIO(file_bytes), data_only=True)
    worksheet = workbook.active
    rows = list(worksheet.iter_rows(values_only=True))

    output: List[Dict[str, Any]] = []
    index = 0

    while index < len(rows):
        row = rows[index]

        if (
            row
            and row[0]
            and str(row[0]).strip().startswith("Category:")
        ):
            category = str(row[0]).replace("Category:", "", 1).strip()

            if index + 1 >= len(rows):
                break

            headers = [
                str(header).strip() if header is not None else None
                for header in rows[index + 1]
            ]

            details: List[Dict[str, Any]] = []
            row_index = index + 2

            while row_index < len(rows):
                current_row = rows[row_index]

                if (
                    current_row
                    and current_row[0]
                    and str(current_row[0]).strip().startswith("Category:")
                ):
                    break

                if not any(cell is not None for cell in current_row):
                    row_index += 1
                    continue

                item: Dict[str, Any] = {}
                for column_index, header in enumerate(headers):
                    if (
                        header
                        and column_index < len(current_row)
                        and current_row[column_index] is not None
                    ):
                        item[header] = current_row[column_index]

                if item:
                    details.append(item)

                row_index += 1

            output.append({"category": category, "details": details})
            index = row_index
        else:
            index += 1

    return _only_vm_and_storage(output)


# ---------------------------------------------------------------------------
# VM family detection and candidate construction
# ---------------------------------------------------------------------------

def _extract_e_series_families(value: Any) -> Set[str]:
    """Find E-series family labels (for example E5) in a value or nested data."""
    if isinstance(value, dict):
        text_value = " ".join(str(item) for item in value.values())
    elif isinstance(value, (list, tuple, set)):
        text_value = " ".join(str(item) for item in value)
    else:
        text_value = str(value or "")

    return {
        f"E{match.group(1)}"
        for match in _E_SERIES_PATTERN.finditer(text_value)
    }


def _product_family(product: Dict[str, Any]) -> Optional[str]:
    families = _extract_e_series_families(product.get("displayName", ""))
    if len(families) == 1:
        return next(iter(families))
    return None


def _is_ocpu_metric(metric_name: str) -> bool:
    return "ocpu" in metric_name.lower()


def _is_memory_metric(metric_name: str) -> bool:
    return "memory" in metric_name.lower()


def build_batch_candidates(
    requirements: List[Dict[str, Any]],
    products: List[Dict[str, Any]],
    lookup: Dict[Tuple[str, str], Decimal],
    limit_per_req: int = 150,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """
    Build candidates for each requirement separately.

    For VM requirements, only include the E-series families explicitly found
    in that requirement's Excel detail rows. A family is considered priceable
    only if both OCPU and Memory metrics exist in the available products.
    """
    batch_candidates: List[Dict[str, Any]] = []
    warnings: List[str] = []
    seen = set()

    for requirement_index, requirement in enumerate(requirements):
        category = requirement.get("category", "")
        candidates = relevant_products(
            category,
            products,
            lookup,
            limit=limit_per_req,
        )

        if _is_vm_requirement(category):
            requested_families = _extract_e_series_families(
                requirement.get("details", [])
            )

            if not requested_families:
                warnings.append(
                    f"No E-series VM shape/family (for example E5) was found "
                    f"in the Excel details for '{category}'. VM pricing was skipped."
                )
                continue

            # Keep only products whose displayName explicitly identifies a
            # requested family, then verify that each family has OCPU + Memory.
            family_candidates: Dict[str, List[Dict[str, Any]]] = {}
            for candidate in candidates:
                family = _product_family(candidate)
                if family in requested_families:
                    family_candidates.setdefault(family, []).append(candidate)

            valid_families: Set[str] = set()
            for family in requested_families:
                family_products = family_candidates.get(family, [])
                has_ocpu = any(
                    _is_ocpu_metric(item["metricName"])
                    for item in family_products
                )
                has_memory = any(
                    _is_memory_metric(item["metricName"])
                    for item in family_products
                )

                if has_ocpu and has_memory:
                    valid_families.add(family)
                else:
                    warnings.append(
                        f"Requested VM family {family} for '{category}' does "
                        "not have both OCPU and Memory pricing products available. "
                        "No alternate family was substituted."
                    )

            candidates = [
                candidate
                for family in valid_families
                for candidate in family_candidates.get(family, [])
            ]

        for candidate in candidates:
            key = (
                requirement_index,
                category,
                candidate["displayName"],
                candidate["metricName"],
            )
            if key in seen:
                continue

            seen.add(key)
            batch_candidates.append(
                {
                    "requirementIndex": requirement_index,
                    "category": category,
                    "displayName": candidate["displayName"],
                    "metricName": candidate["metricName"],
                    "unitPrice": candidate["unitPrice"],
                }
            )

    return batch_candidates, warnings


# ---------------------------------------------------------------------------
# Gemini call
# ---------------------------------------------------------------------------

def ask_gemini_batch(
    requirements: List[Dict[str, Any]],
    candidates: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Ask Gemini to map requirements to exact products and determine quantities."""
    indexed_requirements = [
        {"requirementIndex": index, **requirement}
        for index, requirement in enumerate(requirements)
    ]

    prompt = (
        SYSTEM_PROMPT
        + "\nRequirements:\n"
        + json.dumps(indexed_requirements, indent=2, default=str)
        + "\nAvailable Products:\n"
        + json.dumps(candidates, indent=2, default=str)
    )

    response = generate_with_retry(
        client,
        GEMINI_MODEL,
        prompt,
        types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0,
        ),
    )

    print(prompt)
    return json.loads(response.text)


# ---------------------------------------------------------------------------
# Exact arithmetic and final OCI estimate
# ---------------------------------------------------------------------------

def _decimal_value(value: Any, field_name: str) -> Decimal:
    """Convert a value to Decimal without passing through float arithmetic."""
    if value is None or isinstance(value, bool):
        raise ValueError(f"Missing or invalid {field_name}")

    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"Invalid decimal value for {field_name}: {value}") from exc


def run_oci_estimate(file_bytes: bytes) -> Dict[str, Any]:
    """Run the complete OCI estimation pipeline with no rounding."""
    requirements = excel_to_requirements(file_bytes)

    if not requirements:
        return {
            "report": [],
            "total": "0",
            "warnings": ["No requirements found in the uploaded sheet"],
        }

    products = fetch_products()
    price_lookup = build_lookup(products)

    batch_candidates, candidate_warnings = build_batch_candidates(
        requirements,
        products,
        price_lookup,
    )
    warnings: List[str] = list(candidate_warnings)

    # Report categories for which no candidate products were found.
    matched_requirement_indexes = {
        candidate["requirementIndex"] for candidate in batch_candidates
    }
    for requirement_index, requirement in enumerate(requirements):
        if requirement_index not in matched_requirement_indexes:
            category = requirement.get("category", "")
            if not any(
                warning.startswith((
                    f"No E-series VM shape/family")
                ) and f"'{category}'" in warning
                for warning in warnings
            ) and not any(
                f"Requested VM family" in warning and f"'{category}'" in warning
                for warning in warnings
            ):
                warnings.append(
                    f"No matching OCI products found for '{category}'"
                )

    if not batch_candidates:
        return {"report": [], "total": "0", "warnings": warnings}

    items = ask_gemini_batch(requirements, batch_candidates)

    # Only candidate entries supplied to Gemini can be priced. This prevents
    # an LLM-generated or paraphrased product/metric from being priced.
    candidate_lookup: Dict[Tuple[int, str, str], Decimal] = {}
    for candidate in batch_candidates:
        candidate_key = (
            candidate["requirementIndex"],
            candidate["displayName"],
            candidate["metricName"],
        )
        candidate_lookup[candidate_key] = Decimal(candidate["unitPrice"])

    report: List[Dict[str, Any]] = []

    # Decimal multiplication and summation are performed with a high precision
    # context. No quantize(), round(), float(), or fixed decimal formatting is used.
    with localcontext() as decimal_context:
        decimal_context.prec = 1000
        total = Decimal("0")

        for item in items:
            try:
                requirement_index = int(item["requirementIndex"])
                category = str(item.get("category", ""))
                source_display_name = str(item["sourceDisplayName"])
                metric_name = str(item["metricName"])

                if requirement_index < 0 or requirement_index >= len(requirements):
                    raise ValueError("Invalid requirementIndex returned by Gemini")

                expected_category = requirements[requirement_index].get("category", "")
                if category != expected_category:
                    raise ValueError(
                        "Gemini returned a category that does not match requirementIndex"
                    )

                key = (requirement_index, source_display_name, metric_name)
                if key not in candidate_lookup:
                    raise ValueError(
                        "Product/metric was not present in the supplied Oracle candidates"
                    )

                unit_price = candidate_lookup[key]
                quantity = _decimal_value(item.get("qty"), "qty")
                cost = quantity * unit_price
                total += cost

                calculation = (
                    f"{quantity} * {unit_price} = {cost}"
                )

                report.append(
                    {
                        "category": category,
                        "product": item.get("displayName", source_display_name),
                        "sourceDisplayName": source_display_name,
                        "metric": metric_name,
                        "unitPrice": str(unit_price),
                        "qty": str(quantity),
                        "calculation": calculation,
                        "cost": str(cost),
                    }
                )

            except (KeyError, TypeError, ValueError, InvalidOperation, ArithmeticError) as exc:
                warnings.append(
                    f"Could not calculate '{item.get('displayName', '?')}': {exc}"
                )
                continue

    return {
        "report": report,
        "total": str(total),
        "warnings": warnings,
    }
