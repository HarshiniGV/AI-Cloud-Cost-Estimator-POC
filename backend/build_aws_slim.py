"""
build_aws_slim.py - one-time prep.
Streams the big AWS price-list files in aws-raw/ and writes small files to
aws-cost/ containing ONLY VM + storage, for one region, On-Demand only:
    EC2.json  - EC2 instances (VM)
    EBS.json  - EBS volumes (block storage, taken out of EC2.json)
    S3.json   - S3 storage (object storage)
    EFS.json  - EFS storage (file storage)
Same AWS Price List JSON format, so aws_pricing.py loads them unchanged.

Run:  python build_aws_slim.py
"""
import json
import os
from decimal import Decimal

import ijson

RAW_DIR = os.getenv("AWS_RAW_DIR", "aws-raw")
OUT_DIR = os.getenv("AWS_COST_DIR", "aws-cost")
REGION = os.getenv("AWS_REGION", "us-east-1").strip().lower()


def bucket(name: str, p: dict):
    """Return the output file a product belongs in, or None to drop it."""
    a = p.get("attributes", {})
    if str(a.get("regionCode", "")).lower() != REGION:
        return None
    fam = p.get("productFamily")
    if name.upper() == "EC2":
        if fam == "Compute Instance":
            ok = (a.get("tenancy") == "Shared"
                  and a.get("capacitystatus") == "Used"
                  and a.get("preInstalledSw") == "NA"
                  and a.get("licenseModel") != "Bring your own license"
                  and not str(a.get("operation", "")).endswith(":CB"))  # capacity blocks
            return "EC2" if ok else None
        if fam == "Storage" and a.get("volumeApiName"):
            return "EBS"
        return None
    return name if fam == "Storage" else None  # S3 / EFS: storage only


def _num(o):
    if isinstance(o, Decimal):
        return float(o)
    raise TypeError(type(o))


def _has_price(offers: dict) -> bool:
    return any(float((d.get("pricePerUnit") or {}).get("USD", 0) or 0) > 0
               for o in offers.values() for d in o.get("priceDimensions", {}).values())


def slim(path: str, name: str) -> dict:
    kept = {}  # sku -> (bucket, product)
    with open(path, "rb") as f:
        for sku, prod in ijson.kvitems(f, "products"):
            b = bucket(name, prod)
            if b:
                kept[sku] = (b, prod)
    out = {}
    with open(path, "rb") as f:
        for sku, offers in ijson.kvitems(f, "terms.OnDemand"):
            if sku in kept and _has_price(offers):
                b, prod = kept[sku]
                d = out.setdefault(b, {"products": {}, "terms": {"OnDemand": {}}})
                d["products"][sku] = prod
                d["terms"]["OnDemand"][sku] = offers
    return out


def build(raw_dir: str = RAW_DIR, out_dir: str = OUT_DIR) -> None:
    os.makedirs(out_dir, exist_ok=True)
    for fn in sorted(os.listdir(raw_dir)):
        if not fn.lower().endswith(".json"):
            continue
        print(f"{fn}: streaming ...", flush=True)
        for b, data in slim(os.path.join(raw_dir, fn), os.path.splitext(fn)[0]).items():
            out = os.path.join(out_dir, f"{b}.json")
            with open(out, "w", encoding="utf-8") as f:
                json.dump(data, f, default=_num)
            print(f"  {len(data['products'])} products -> {out} ({os.path.getsize(out) / 1e6:.1f} MB)")


if __name__ == "__main__":
    build()