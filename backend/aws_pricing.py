"""
Generic AWS cost estimator.

Input : a requirements list (same shape as the OCI estimator):
        [{"category": "...", "details": [{...}, ...]}, ...]
Prices: every supported file in the aws-cost directory
        (.json, .jsonl, .ndjson, .csv, .tsv, .xlsx, .xls, .parquet).
Output: OCI-style line items printed to the console.

Nothing here knows about specific services, shapes, OSes or categories:
  1. Every price file is flattened into generic price records.
  2. Gemini routes each requirement category to the relevant files (from a catalog
     built from the files themselves).
  3. Candidates are narrowed by token overlap with the requirement (ties broken
     by lowest price), then Gemini picks the cheapest closest products and does
     the arithmetic.
  4. Python only cross-checks (price exists in candidates, qty * unitPrice == cost).

Config (env, all optional):
  GEMINI_API_KEY (required), GEMINI_MODEL, AWS_COST_DIR (default ./aws-cost),
  AWS_REGION (filters records that carry a matching location/region value),
  AWS_TERM_TYPES (default "OnDemand"), HOURS_PER_MONTH (default 730),
  MAX_CANDIDATES_PER_REQ (default 150)
"""
import os
import re
import csv
import json
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from dotenv import load_dotenv
from google import genai
from google.genai import types

from gemini_retry import generate_with_retry, make_client, use_oci
from oci_pricing import excel_to_requirements  # same sheet -> requirements parser

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
AWS_COST_DIR = Path(os.getenv("AWS_COST_DIR", str(Path(__file__).parent / "aws-cost")))
AWS_REGION = os.getenv("AWS_REGION", "us-east-1").strip().lower()
AWS_TERM_TYPES = [t.strip() for t in os.getenv("AWS_TERM_TYPES", "OnDemand").split(",") if t.strip()]
HOURS_PER_MONTH = float(os.getenv("HOURS_PER_MONTH", "744"))
MAX_CANDIDATES_PER_REQ = int(os.getenv("MAX_CANDIDATES_PER_REQ", "150"))
SMALL_FILE_MAX = int(os.getenv("SMALL_FILE_MAX", "200"))  # storage files (EBS/S3/EFS) are tiny

if not GEMINI_API_KEY and not use_oci():
    raise RuntimeError("GEMINI_API_KEY not set (put it in .env), or set LLM_PROVIDER=oci.")

client = make_client(GEMINI_API_KEY)

# ---------------------------------------------------------------------------
# Prompts (generic: no service names, shapes, OS names or storage rules)
# ---------------------------------------------------------------------------
ROUTER_PROMPT = """You route cloud requirements to price-list files.
You are given Requirements (each with a "category" and "details") and a Catalog
of price-list files (name, record count, sample descriptions, sample attributes).

For EACH requirement category, list the file names whose products are needed to
price ALL of its details (a category may need several files, e.g. compute plus
the OS licensing or disks that go with it, if those live in other files).
Return ONLY JSON, no commentary:
{"<category copied exactly>": ["<file name>", ...]}
Use only file names that appear in the Catalog.
"""

PRICING_PROMPT = """You are a cloud pricing expert.
You are given Requirements (each with a "category" and "details") and Available
Products (each with category, source, displayName, metricName, unitPrice,
optional beginRange/endRange tier bounds, and attributes).

For EACH requirement, work out which product(s)/metric(s) are needed, how much
of each metric is consumed, and the resulting pre-tax cost. Return ONLY a JSON
array, no markdown fences, no commentary, covering ALL requirements together:

[
  {
    "category": "<requirement category, copied exactly>",
    "displayName": "<descriptive label for this line item>",
    "metricName": "<metric consumed, taken from the product's metricName>",
    "unitPrice": <unitPrice copied EXACTLY from the Available Products entry used>,
    "qty": <numeric quantity of the metric consumed>,
    "calculation": "<plain-English arithmetic showing the full derivation>",
    "cost": <qty * unitPrice, computed by you, numeric, pre-tax>
  }
]

Rules:
- Only use unitPrice values from the Available Products supplied for that
  requirement's category. Never invent a price. Copy it exactly, then multiply.
- Interpret every field in the requirement details from its name and value
  (counts, sizes, types, OS, performance, request volumes, etc.) and match them
  against product attributes. Multiply by Count where present.
- Usage billed per hour is billed for HOURS_PER_MONTH hours per month.
- Respect tiers: if beginRange/endRange exist, or a free allowance exists, show
  the subtraction in "calculation" and only charge the billable part.
- If a requirement implies several billable components (e.g. compute plus
  licensing, storage, requests, data transfer) emit one line item for each, as
  long as a matching product exists in Available Products.

Product selection (closest match ):
1. Find the products that match the requested specs most closely. A product
   that matches every stated attribute (type, size, OS, performance tier, etc.)
   beats one that matches fewer.
2. If several products match equally well, choose the one with the LOWEST
   unitPrice. Never pick a more expensive product when an equally matching,
   cheaper one exists. Also avoid Reservation/Reserve types. If products are
   still tied on price, choose the one whose displayName sorts first
   alphabetically, so the same input always gives the same choice.
   Requirements with identical specs must get the identical product; never
   vary the instance type / shape between them, and only use products that
   appear in Available Products.
3. Do not choose a variant that differs only by a purchase or usage option the
   requirement did not ask for (e.g. dedicated, reserved or pre-paid capacity,
   bundled software, premium support) when a plain standard variant exists.
4. If no product matches exactly, choose the nearest option that meets or
   exceeds the requested specs (never a smaller one), and among equally near
   options the cheapest. State in "calculation" that it is a nearest match and
   what differs.
5. Do not skip a requirement just because the match is imperfect; only omit it
   if truly no candidate product fits.
- Do NOT apply or mention tax. qty, unitPrice and cost are all pre-tax.
- Every requirement must be addressed by at least one item, unless truly no
  candidate product fits it.
"""

# ---------------------------------------------------------------------------
# Loading: flatten any price file into generic records
# ---------------------------------------------------------------------------
_PRICE_KEY_HINTS = ("price", "rate", "cost", "usd")
_NOISE_VALUES = {"", "na", "n/a", "none", "null", "nan"}
SUPPORTED_EXTS = {".json", ".jsonl", ".ndjson", ".csv", ".tsv", ".xlsx", ".xls", ".parquet"}
_MIN_HEADER_COLS = 5  # AWS bulk CSVs have 2-column metadata lines before the real header
_ROW_SKIP = {"sku", "offertermcode", "ratecode", "effectivedate", "priceperunit",
             "pricedescription", "unit", "startingrange", "endingrange",
             "currency", "termtype"}


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def _scalar_attrs(d: Dict[str, Any], max_len: int = 60) -> Dict[str, Any]:
    out = {}
    for k, v in d.items():
        if isinstance(v, (str, int, float)) and not isinstance(v, bool):
            if str(v).strip().lower() in _NOISE_VALUES or len(str(v)) > max_len:
                continue
            out[k] = v
    return out


def _region_ok(attrs: Dict[str, Any]) -> bool:
    """Only filters when the record actually carries a location/region field."""
    if not AWS_REGION:
        return True
    region_vals = [str(v).lower() for k, v in attrs.items()
                   if "location" in k.lower() or "region" in k.lower()]
    return not region_vals or any(AWS_REGION in v for v in region_vals)


def _records_from_price_list(data: Dict[str, Any], source: str) -> Iterator[Dict[str, Any]]:
    """AWS Price List JSON: {"products": {sku: {...}}, "terms": {type: {sku: {...}}}}"""
    products = data.get("products", {})
    terms = data.get("terms", {})
    for term_type in (AWS_TERM_TYPES or list(terms.keys())):
        for sku, offers in terms.get(term_type, {}).items():
            prod = products.get(sku, {})
            attrs = _scalar_attrs(prod.get("attributes", {}))
            if prod.get("productFamily"):
                attrs["productFamily"] = prod["productFamily"]
            if not _region_ok(attrs):
                continue
            for offer in offers.values():
                for dim in offer.get("priceDimensions", {}).values():
                    usd = (dim.get("pricePerUnit") or {}).get("USD")
                    if usd is None:
                        continue
                    try:
                        price = float(usd)
                    except ValueError:
                        continue
                    rec = {
                        "source": source,
                        "displayName": dim.get("description") or prod.get("sku", sku),
                        "metricName": dim.get("unit", ""),
                        "unitPrice": price,
                        "attributes": attrs,
                    }
                    if dim.get("beginRange") not in (None, "", "0"):
                        rec["beginRange"] = dim["beginRange"]
                    if dim.get("endRange") not in (None, "", "Inf"):
                        rec["endRange"] = dim["endRange"]
                    yield rec


def _find_record_list(data: Any) -> Optional[List[Dict[str, Any]]]:
    if isinstance(data, list) and data and all(isinstance(x, dict) for x in data):
        return data
    if isinstance(data, dict):
        for v in data.values():
            found = _find_record_list(v)
            if found:
                return found
    return None


def _records_from_generic(data: Any, source: str) -> Iterator[Dict[str, Any]]:
    """Fallback: any list of dicts that carries a numeric price-like field."""
    for item in _find_record_list(data) or []:
        price_key = next(
            (k for k, v in item.items()
             if any(h in str(k).lower() for h in _PRICE_KEY_HINTS)
             and isinstance(v, (int, float, str))
             and re.fullmatch(r"-?\d+(\.\d+)?", str(v).strip())),
            None,
        )
        if price_key is None:
            continue
        attrs = _scalar_attrs({k: v for k, v in item.items() if k != price_key})
        if not _region_ok(attrs):
            continue
        name = next((attrs[k] for k in attrs
                     if str(k).lower() in ("displayname", "name", "description", "sku")), "")
        unit = next((attrs[k] for k in attrs
                     if str(k).lower() in ("unit", "metric", "metricname", "uom")), "")
        yield {
            "source": source,
            "displayName": name or source,
            "metricName": unit,
            "unitPrice": float(item[price_key]),
            "attributes": attrs,
        }


# ---- tabular readers: each yields one dict per row ---------------------------
def _rows_from_csv(path: Path, delimiter: str) -> Iterator[Dict[str, Any]]:
    """Skips leading metadata lines (AWS price-list CSVs) until the real header row."""
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f, delimiter=delimiter)
        header = None
        for row in reader:
            if header is None:
                if len([c for c in row if c.strip()]) >= _MIN_HEADER_COLS:
                    header = [c.strip() for c in row]
                continue
            if row:
                yield dict(zip(header, row))


def _rows_from_dataframe(df) -> Iterator[Dict[str, Any]]:
    df = df.dropna(how="all").reset_index(drop=True)
    for i in range(len(df)):
        if df.iloc[i].notna().sum() >= _MIN_HEADER_COLS:  # first "wide" row = header
            body = df.iloc[i + 1:].copy()
            body.columns = [str(c).strip() for c in df.iloc[i]]
            for r in body.to_dict("records"):
                yield {k: v for k, v in r.items() if v == v}  # drop NaN
            return


def _rows_from_excel(path: Path) -> Iterator[Dict[str, Any]]:
    import pandas as pd  # pip install pandas openpyxl
    for _, df in pd.read_excel(path, sheet_name=None, header=None).items():
        yield from _rows_from_dataframe(df)


def _rows_from_parquet(path: Path) -> Iterator[Dict[str, Any]]:
    import pandas as pd  # pip install pandas pyarrow
    for r in pd.read_parquet(path).to_dict("records"):
        yield {k: v for k, v in r.items() if v == v}


def _records_from_rows(rows: Iterator[Dict[str, Any]], source: str) -> Iterator[Dict[str, Any]]:
    it = iter(rows)
    first = next(it, None)
    if first is None:
        return
    keys = {_norm(k): k for k in first}

    def all_rows():
        yield first
        yield from it

    # AWS price-list CSV layout (PricePerUnit / PriceDescription / Unit / TermType ...)
    if "priceperunit" in keys:
        def get(row, n):
            return row.get(keys.get(n, ""), "")

        for row in all_rows():
            term = str(get(row, "termtype")).strip()
            if AWS_TERM_TYPES and term and term not in AWS_TERM_TYPES:
                continue
            cur = str(get(row, "currency")).strip()
            if cur and cur.upper() != "USD":
                continue
            try:
                price = float(get(row, "priceperunit"))
            except (TypeError, ValueError):
                continue
            attrs = _scalar_attrs({k: v for k, v in row.items() if _norm(k) not in _ROW_SKIP})
            if not _region_ok(attrs):
                continue
            rec = {
                "source": source,
                "displayName": str(get(row, "pricedescription")) or source,
                "metricName": str(get(row, "unit")),
                "unitPrice": price,
                "attributes": attrs,
            }
            lo = str(get(row, "startingrange")).strip()
            hi = str(get(row, "endingrange")).strip()
            if lo not in ("", "0"):
                rec["beginRange"] = lo
            if hi not in ("", "Inf"):
                rec["endRange"] = hi
            yield rec
        return

    # Any other table: generic price-field detection
    yield from _records_from_generic(list(all_rows()), source)


def _load_file(path: Path) -> List[Dict[str, Any]]:
    ext, source = path.suffix.lower(), path.stem
    if ext == ".json":
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if isinstance(data, dict) and "products" in data and "terms" in data:
            return list(_records_from_price_list(data, source))
        return list(_records_from_generic(data, source))
    if ext in (".jsonl", ".ndjson"):
        rows = [json.loads(l) for l in path.read_text(encoding="utf-8-sig").splitlines() if l.strip()]
        return list(_records_from_generic(rows, source))
    if ext == ".csv":
        return list(_records_from_rows(_rows_from_csv(path, ","), source))
    if ext == ".tsv":
        return list(_records_from_rows(_rows_from_csv(path, "\t"), source))
    if ext in (".xlsx", ".xls"):
        return list(_records_from_rows(_rows_from_excel(path), source))
    if ext == ".parquet":
        return list(_records_from_rows(_rows_from_parquet(path), source))
    return []


_file_cache: Dict[str, List[Dict[str, Any]]] = {}


def load_price_files(cost_dir: Optional[Path] = None) -> Dict[str, List[Dict[str, Any]]]:
    if _file_cache:
        return _file_cache
    cost_dir = cost_dir or AWS_COST_DIR
    raw_dir = Path(os.getenv("AWS_RAW_DIR", str(Path(__file__).parent / "aws-raw")))
    if not cost_dir.is_dir() and raw_dir.is_dir():
        from build_aws_slim import build  # one-time: shrink raw AWS price files
        build(str(raw_dir), str(cost_dir))
    if not cost_dir.is_dir():
        raise FileNotFoundError(
            f"Cost directory not found: {cost_dir.resolve()}. Run build_aws_slim.py in the "
            f"backend folder so that backend/aws-cost/ exists BEFORE building the Docker image."
        )
    for path in sorted(p for p in cost_dir.iterdir() if p.suffix.lower() in SUPPORTED_EXTS):
        try:
            recs = _load_file(path)
        except Exception as e:
            print(f"[warn] {path.name}: could not be read ({type(e).__name__}: {e}); skipped")
            continue
        if not recs:
            print(f"[warn] {path.name}: no usable price records; skipped")
            continue
        name = path.stem if path.stem not in _file_cache else path.name  # EC2.json + EC2.csv
        _file_cache[name] = recs
    if not _file_cache:
        raise RuntimeError(f"No usable price records found in {cost_dir.resolve()}")
    return _file_cache


# ---------------------------------------------------------------------------
# Routing + candidate narrowing (all derived from data, nothing hardcoded)
# ---------------------------------------------------------------------------
def build_catalog(files: Dict[str, List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    catalog = []
    for name, recs in files.items():
        descs, seen = [], set()
        for r in recs:
            d = str(r["displayName"])[:90]
            if d not in seen:
                seen.add(d)
                descs.append(d)
            if len(descs) >= 6:
                break
        attr_keys = sorted({k for r in recs[:200] for k in r["attributes"]})[:25]
        catalog.append({
            "file": name,
            "records": len(recs),
            "sampleDescriptions": descs,
            "attributeKeys": attr_keys,
        })
    return catalog


def _call_gemini_json(prompt: str) -> Any:
    r = generate_with_retry(
        client, GEMINI_MODEL, prompt,
        types.GenerateContentConfig(response_mime_type="application/json", temperature=0),
    )
    return json.loads(r.text)


def route_requirements(reqs, files) -> Dict[str, List[str]]:
    prompt = (ROUTER_PROMPT
              + "\nRequirements:\n" + json.dumps(reqs, indent=2, default=str)
              + "\nCatalog:\n" + json.dumps(build_catalog(files), indent=2))
    try:
        routing = _call_gemini_json(prompt)
        if not isinstance(routing, dict):
            raise ValueError("router did not return an object")
    except Exception as e:  # fall back to every file rather than fail
        print(f"[warn] routing failed ({e}); using all files for every category")
        routing = {}
    all_files = list(files)
    out = {}
    for req in reqs:
        cat = req["category"]
        chosen = [f for f in routing.get(cat, []) if f in files]
        small = [f for f in files if len(files[f]) <= SMALL_FILE_MAX]
        if any(f in small for f in chosen):  # a storage file was chosen -> offer all storage files
            chosen += [f for f in small if f not in chosen]
        out[cat] = chosen or all_files
    return out


def _tokens(text: str) -> set:
    return set(re.findall(r"[a-z]+|\d+(?:\.\d+)?", text.lower()))


def _match(req_tok: str, prod_toks: set) -> bool:
    if req_tok in prod_toks:
        return True
    if req_tok.isalpha() and len(req_tok) >= 4:  # "window" ~ "windows", "block" ~ "blocks"
        return any(p.startswith(req_tok) or req_tok.startswith(p)
                   for p in prod_toks if p.isalpha() and len(p) >= 4)
    return False


def select_candidates(req: Dict[str, Any], files, routed: List[str], limit: int):
    """Rank records from routed files by token overlap; ties go to the lowest price."""
    req_toks = _tokens(json.dumps(req, default=str))
    scored = []
    for fname in routed:
        for rec in files[fname]:
            text = " ".join([rec["source"], str(rec["displayName"]), str(rec["metricName"]),
                             " ".join(f"{k} {v}" for k, v in rec["attributes"].items())])
            ptoks = _tokens(text)
            score = sum(1 for t in req_toks if _match(t, ptoks))
            scored.append((score, rec["unitPrice"], rec))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [r for _, _, r in scored[:limit]]


def build_batch_candidates(reqs, files, routing, limit=MAX_CANDIDATES_PER_REQ):
    out, seen = [], set()
    for req in reqs:
        cat = req["category"]
        for rec in select_candidates(req, files, routing[cat], limit):
            key = (cat, rec["source"], rec["displayName"], rec["metricName"], rec["unitPrice"],
                   rec.get("beginRange"))
            if key in seen:
                continue
            seen.add(key)
            out.append({"category": cat, **rec})
    return out


# ---------------------------------------------------------------------------
# Estimation
# ---------------------------------------------------------------------------
def ask_gemini_pricing(reqs, candidates):
    prompt = (PRICING_PROMPT.replace("HOURS_PER_MONTH", f"{HOURS_PER_MONTH:g}")
              + "\nRequirements:\n" + json.dumps(reqs, indent=2, default=str)
              + "\nAvailable Products:\n" + json.dumps(candidates, indent=2, default=str))
    return _call_gemini_json(prompt)


def _estimate_from_reqs(reqs: List[Dict[str, Any]]) -> Dict[str, Any]:
    warnings: List[str] = []
    if not reqs:
        return {"report": [], "total": 0, "warnings": ["No requirements supplied"]}

    files = load_price_files()
    routing = route_requirements(reqs, files)
    candidates = build_batch_candidates(reqs, files, routing)

    matched = {c["category"] for c in candidates}
    warnings += [f"No matching AWS products found for '{r['category']}'"
                 for r in reqs if r["category"] not in matched]
    if not candidates:
        return {"report": [], "total": 0, "warnings": warnings}

    valid_prices = {(c["category"], round(c["unitPrice"], 10)) for c in candidates}
    known_prices = {(str(r["displayName"]), round(r["unitPrice"], 10))
                    for recs in files.values() for r in recs}  # every real AWS price we loaded
    items = ask_gemini_pricing(reqs, candidates)
    if isinstance(items, dict):  # tolerate {"items": [...]}
        items = next((v for v in items.values() if isinstance(v, list)), [items])

    report = []
    for it in items:
        name = it.get("displayName", "?")
        try:
            cost = round(float(it.get("cost", 0)), 2)
            qty = float(it.get("qty", 0))
            price = float(it.get("unitPrice", 0))
        except (TypeError, ValueError):
            warnings.append(f"Could not parse numbers for '{name}'; skipped")
            continue
        if ((it.get("category"), round(price, 10)) not in valid_prices
                and (str(name), round(price, 10)) not in known_prices):
            warnings.append(f"unitPrice {price} for '{name}' not found in candidates")
        if abs(qty * price - cost) > max(0.01, 0.001 * abs(cost)):
            warnings.append(f"Cost mismatch for '{name}': qty*unitPrice={qty * price:.2f}, model cost={cost}")
        report.append({
            "category": it.get("category", ""),
            "product": name,
            "metric": it.get("metricName", ""),
            "unitPrice": price,
            "qty": qty,
            "calculation": it.get("calculation", ""),
            "cost": cost,
        })

    total = round(sum(r["cost"] for r in report), 2)
    return {"report": report, "total": total, "warnings": warnings}


def run_aws_estimate(file_bytes: bytes) -> Dict[str, Any]:
    """Public entry point - same signature as run_oci_estimate(file_bytes)."""
    reqs = excel_to_requirements(file_bytes)
    if not reqs:
        return {"report": [], "total": 0,
                "warnings": ["No VM or storage requirements found in the uploaded sheet"]}
    return _estimate_from_reqs(reqs)


def print_estimate(result: Dict[str, Any]) -> None:
    print("\n" + "=" * 100)
    print("AWS COST ESTIMATE (pre-tax, USD)")
    print("=" * 100)
    current = None
    for r in result["report"]:
        if r["category"] != current:
            current = r["category"]
            print(f"\n[{current}]")
        print(f"  - {r['product']}")
        print(f"      metric : {r['metric']}")
        print(f"      price  : {r['unitPrice']}  x  qty {r['qty']:g}")
        print(f"      calc   : {r['calculation']}")
        print(f"      cost   : {r['cost']:,.2f}")
    print("\n" + "-" * 100)
    print(f"TOTAL: {result['total']:,.2f} USD")
    if result["warnings"]:
        print("\nWarnings:")
        for w in result["warnings"]:
            print(f"  ! {w}")


def load_requirements(arg: Optional[str]) -> List[Dict[str, Any]]:
    """
    arg can be:
      - a path to a .json file          -> reads the file
      - a raw JSON string               -> parsed directly
      - "-" or None with piped stdin    -> reads stdin
    """
    import sys
    if arg is None or arg == "-":
        if sys.stdin.isatty():
            raise SystemExit("No requirements given. Pass a JSON string, a .json file path, or pipe JSON via stdin.")
        text = sys.stdin.read()
    elif arg.lstrip().startswith(("[", "{")):  # looks like inline JSON, not a path
        text = arg
    else:
        try:
            is_file = Path(arg).is_file()
        except OSError:  # too long / invalid for a path
            is_file = False
        text = Path(arg).read_text(encoding="utf-8-sig") if is_file else arg
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise SystemExit(f"Requirements are not valid JSON: {e}")
    if isinstance(data, dict):  # allow {"requirements": [...]} or a single requirement
        data = data.get("requirements", [data])
    if not isinstance(data, list):
        raise SystemExit("Requirements must be a JSON list of {category, details} objects.")
    return data


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Generic AWS cost estimator")
    ap.add_argument("requirements", nargs="?",
                    help='JSON string or path to a .json file (or "-" / pipe for stdin)')
    ap.add_argument("--cost-dir", help="Override AWS_COST_DIR")
    args = ap.parse_args()

    if args.cost_dir:
        AWS_COST_DIR = Path(args.cost_dir)
        _file_cache.clear()

    print_estimate(_estimate_from_reqs(load_requirements(args.requirements)))