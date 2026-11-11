import os
import json
from typing import Any, Dict, List

from google.genai import types
from gemini_retry import generate_with_retry, make_client

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")

CLOUDS = ("azure", "oci", "aws", "gcp")

BASELINE_CAPABILITIES = [
    "Oracle Database (Exadata / Autonomous / RAC)",
    "Oracle licence portability (BYOL)",
    "Price / performance",
    "Data egress cost",
    "Compute flexibility",
    "Service catalogue breadth",
    "Kubernetes / containers",
    "AI / ML & analytics",
    "Microsoft / .NET ecosystem fit",
    "Hybrid / multi-cloud",
    "Compliance & global footprint",
]

RATINGS_PROMPT = """You are a neutral cloud architect comparing Microsoft Azure, Oracle Cloud Infrastructure (OCI), AWS and Google Cloud (GCP) for ONE specific workload.

Rate each cloud from 1 (weak) to 5 (excellent) on each capability, judged on how well it serves THIS workload (the services listed in the input below), using your general knowledge of each provider's current offerings.

Rules:
- Be objective. Do not favour any provider. Only give OCI a 5 where it is genuinely best-in-class for that capability.
- Scores should differ between providers where the real offerings differ; do not give every provider the same score.
- Always keep the two Oracle rows (Oracle Database, Oracle licence portability). Keep the other baseline capabilities that are relevant to this workload, drop ones that clearly do not apply, and add up to 3 workload-specific capabilities (for example a database engine or service that appears in the input).
- Return between 8 and 14 rows.
- "why" is ONE sentence (max 25 words) explaining OCI's score. When OCI scores 3 or lower, the sentence must state the limitation.
- Do not quote exact prices. The costs in the input are context only.

Baseline capabilities:
{baseline}

Return ONLY JSON in exactly this shape, integers only:
{{"ratings": [{{"category": "string", "azure": 1, "oci": 1, "aws": 1, "gcp": 1, "why": "string"}}]}}
"""


def _summarize_estimate(payload: Dict[str, Any], max_rows: int = 40) -> Dict[str, Any]:
    """Boil the /api/estimate response down to a small, model-friendly summary."""
    out: Dict[str, Any] = {}

    for cloud in CLOUDS:
        data = payload.get(cloud)
        if not isinstance(data, dict) or data.get("error"):
            continue

        if cloud == "azure":
            total = data.get("total_monthly_cost")
            items = [
                {
                    "service": r.get("service"),
                    "sku": r.get("sku"),
                    "monthly_cost": r.get("monthly_cost"),
                }
                for r in (data.get("breakdown") or [])
            ]
        else:
            total = data.get("total")
            items = [
                {
                    "product": r.get("product"),
                    "category": r.get("category"),
                    "metric": r.get("metric"),
                    "cost": r.get("cost"),
                }
                for r in (data.get("report") or [])
            ]

        out[cloud] = {"monthly_total": total, "line_items": items[:max_rows]}

    return out


def _clamp(v: Any) -> int:
    try:
        n = int(round(float(v)))
    except (TypeError, ValueError):
        raise ValueError(f"non-numeric rating: {v!r}")
    return max(1, min(5, n))


def _validate(raw: Any) -> List[Dict[str, Any]]:
    rows = raw.get("ratings") if isinstance(raw, dict) else raw
    if not isinstance(rows, list):
        raise ValueError("Model response has no 'ratings' list")

    clean: List[Dict[str, Any]] = []
    for r in rows:
        if not isinstance(r, dict) or not str(r.get("category", "")).strip():
            continue
        try:
            clean.append(
                {
                    "category": str(r["category"]).strip(),
                    **{c: _clamp(r.get(c)) for c in CLOUDS},
                    "why": str(r.get("why", "")).strip(),
                }
            )
        except ValueError:
            continue  # skip a malformed row rather than failing the whole export

    if len(clean) < 3:
        raise ValueError("Model returned too few usable ratings")
    return clean[:14]


def get_ai_ratings(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    # Client is created lazily so a missing key can't break app start-up.
    client = make_client(GEMINI_API_KEY)

    prompt = (
        RATINGS_PROMPT.format(
            baseline="\n".join(f"- {c}" for c in BASELINE_CAPABILITIES)
        )
        + "\nWorkload (services and costs per cloud):\n"
        + json.dumps(_summarize_estimate(payload), indent=2, default=str)
    )

    r = generate_with_retry(
        client,
        GEMINI_MODEL,
        prompt,
        types.GenerateContentConfig(response_mime_type="application/json"),
    )

    return _validate(json.loads(r.text))
