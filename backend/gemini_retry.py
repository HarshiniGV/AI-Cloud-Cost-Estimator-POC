"""
gemini_retry.py - one place for every LLM call: provider switch + retry + fallback.

LLM_PROVIDER=gemini (default)  -> Google Gemini API (GEMINI_API_KEY, GEMINI_MODEL)
LLM_PROVIDER=oci               -> OCI Generative AI (OpenAI-compatible endpoint + API key)
"""
import json
import os
import random
import re
import time

from dotenv import load_dotenv

load_dotenv()

_RETRYABLE_TEXT = ("503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED", "overloaded",
                   "high demand", "500", "INTERNAL", "504", "DEADLINE", "Too Many Requests")


class _Resp:
    """Mimics the google-genai response: callers only use .text"""

    def __init__(self, text: str):
        self.text = text


def use_oci() -> bool:
    return os.getenv("LLM_PROVIDER", "gemini").strip().lower() == "oci"


def make_client(api_key):
    """Google client, or None when LLM_PROVIDER=oci (no Google key needed)."""
    if use_oci():
        return None
    from google import genai
    return genai.Client(api_key=api_key)


def _retryable(err: Exception) -> bool:
    code = getattr(err, "code", None) or getattr(err, "status_code", None)
    if code in (429, 500, 502, 503, 504):
        return True
    return any(s in str(err) for s in _RETRYABLE_TEXT)


_oci_client = None


def _oci():
    global _oci_client
    if _oci_client is None:
        from openai import OpenAI

        endpoint = os.getenv("OCI_GENAI_ENDPOINT", "").strip().rstrip("/")
        key = os.getenv("OCI_GENAI_API_KEY", "").strip()
        if not endpoint or not key:
            raise RuntimeError(
                "LLM_PROVIDER=oci needs OCI_GENAI_ENDPOINT and OCI_GENAI_API_KEY in .env"
            )
        kwargs = {}
        compartment = os.getenv("OCI_COMPARTMENT_ID", "").strip()
        project = os.getenv("OCI_GENAI_PROJECT_ID", "").strip()
        if compartment:
            kwargs["default_headers"] = {"opc-compartment-id": compartment}
        if project:
            kwargs["project"] = project
        _oci_client = OpenAI(base_url=endpoint, api_key=key, timeout=120, **kwargs)
    return _oci_client


def _clean_json(text: str) -> str:
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.I).strip()
    try:
        json.loads(t)
        return t
    except Exception:  # noqa: BLE001
        pass
    for o, c in (("{", "}"), ("[", "]")):
        i, j = t.find(o), t.rfind(c)
        if i != -1 and j > i:
            seg = t[i:j + 1]
            try:
                json.loads(seg)
                return seg
            except Exception:  # noqa: BLE001
                continue
    return t


def _schema_text(schema) -> str:
    if hasattr(schema, "model_dump"):
        schema = schema.model_dump(exclude_none=True, mode="json")
    return json.dumps(schema, default=str)


def _oci_generate(model: str, contents, config) -> _Resp:
    system = []
    si = getattr(config, "system_instruction", None)
    if si:
        system.append(str(si))
    rule = "Respond with ONLY valid JSON - no markdown fences, no commentary."
    schema = getattr(config, "response_schema", None)
    if schema:
        rule += " The JSON must match this schema: " + _schema_text(schema)
    system.append(rule)
    kwargs = {}
    temp = getattr(config, "temperature", None)
    if temp is not None:
        kwargs["temperature"] = temp
    r = _oci().chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": "\n\n".join(system)},
            {"role": "user", "content": contents if isinstance(contents, str) else str(contents)},
        ],
        **kwargs,
    )
    return _Resp(_clean_json(r.choices[0].message.content or ""))


def generate_with_retry(client, model, contents, config, sleep=time.sleep):
    retries = int(os.getenv("GEMINI_RETRIES", "4"))

    def split(name, default=""):
        return [m.strip() for m in os.getenv(name, default).split(",") if m.strip()]

    if use_oci():
        primary = os.getenv("OCI_GENAI_MODEL_ID", "google.gemini-2.5-flash").strip()
        fallbacks = split("OCI_GENAI_FALLBACK_MODELS")
        call = lambda m: _oci_generate(m, contents, config)  # noqa: E731
    else:
        primary = model
        fallbacks = split("GEMINI_FALLBACK_MODELS", "gemini-2.5-flash")
        call = lambda m: client.models.generate_content(  # noqa: E731
            model=m, contents=contents, config=config)

    models = [primary] + [m for m in fallbacks if m != primary]
    first_err = None
    for m in models:
        for attempt in range(retries):
            try:
                return call(m)
            except Exception as e:  # noqa: BLE001
                first_err = first_err or e
                if not _retryable(e):
                    break
                if attempt < retries - 1:
                    sleep(min(2 ** attempt, 16) + random.random())
    raise first_err
