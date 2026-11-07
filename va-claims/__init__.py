"""VA Benefits Claims plugin for Hermes Agent.

Talks to the VA Lighthouse APIs:
  - Benefits Claims API v1 (sandbox/production, OAuth client credentials grant)
  - Benefits Reference Data API v1 (free, apikey header)

Keeps a local per-veteran claim file under the plugin state dir: intent to
file, tracked snapshots, status history. No PII leaves the machine except
the ICN sent to api.va.gov.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

try:
    from . import guide as _guide
    from . import schemas, store
except ImportError:  # Direct-file loading used by Hermes and test runners.
    import guide as _guide  # type: ignore[no-redef]
    import schemas  # type: ignore[no-redef]
    import store  # type: ignore[no-redef]

SANDBOX_BASE = "https://sandbox-api.va.gov"
PROD_BASE = "https://api.va.gov"
TOKEN_AUD = {
    "sandbox": "https://deptva-eval.okta.com/oauth2/ausdg7guis2TYDlFe2p7/v1/token",
    "production": "https://va.okta.com/oauth2/ausajojxqhTsDSVlA297/v1/token",
}
TIMEOUT = 30.0

_token_cache: dict[str, Any] = {"token": None, "expires_at": 0.0, "env": None}


# ---------------------------------------------------------------- helpers

def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, default=str)


def _cfg(ctx) -> dict[str, Any]:
    try:
        return dict(ctx.get_config() or {})
    except Exception:
        return {}


def _env_or_cfg(ctx, key: str, env: str) -> str | None:
    """Config first (plugin settings), then a Hermes-scoped secret read for the
    env fallback (rule 11: never a bare os.environ read under multiplexing)."""
    val = _cfg(ctx).get(key)
    if val:
        return str(val).strip()
    try:
        from agent.secret_scope import get_secret

        val = get_secret(env)
    except Exception:
        val = os.getenv(env)
    return val.strip() if val and val.strip() else None


# ---------------------------------------------------------------- HTTP + token

def _http(url: str, headers: dict[str, str] | None = None, method: str = "GET", body: bytes | None = None) -> dict[str, Any]:
    """One HTTP call; never raises. Returns {ok, status, body} with body parsed as JSON when possible."""
    req = Request(url, data=body, method=method)
    req.add_header("Accept", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return {"ok": False, "status": exc.code, "body": json.loads(raw)}
        except Exception:
            return {"ok": False, "status": exc.code, "body": raw}
    except (URLError, OSError, TimeoutError) as exc:
        return {"ok": False, "status": 0, "body": f"network error: {exc}"}
    try:
        return {"ok": True, "status": 200, "body": json.loads(raw)}
    except Exception:
        return {"ok": True, "status": 200, "body": raw}


def _ccg_token(ctx, environment: str) -> tuple[str | None, str | None]:
    """Client-credentials JWT-bearer token for the Benefits Claims API.

    POST {base}/oauth2/claims/system/v1/token with a signed client_assertion
    (RS256, aud = the Okta URL in TOKEN_AUD). 5-minute expiry, cached
    in-process. Needs PyJWT[crypto]. Returns (token, error)."""
    now = time.time()
    cached = _token_cache
    if cached["token"] and cached["env"] == environment and cached["expires_at"] > now + 30:
        return cached["token"], None

    client_id = _env_or_cfg(ctx, "client_id", "VA_CLAIMS_CLIENT_ID")
    key_path = _env_or_cfg(ctx, "private_key_path", "VA_CLAIMS_PRIVATE_KEY")
    if not client_id or not key_path:
        return None, (
            "Benefits Claims API credentials not configured. Set plugin settings "
            "client_id and private_key_path (or VA_CLAIMS_CLIENT_ID / "
            "VA_CLAIMS_PRIVATE_KEY): an RSA keypair whose public half is "
            "registered as a JWK in your VA Lighthouse sandbox access request."
        )
    try:
        import jwt  # PyJWT[crypto]
    except ImportError:
        return None, "PyJWT[crypto] is not installed in this environment (pip install 'PyJWT[crypto]')."

    try:
        pem = Path(key_path).expanduser().read_text()
    except OSError as exc:
        return None, f"cannot read private key at {key_path}: {exc}"

    base = SANDBOX_BASE if environment == "sandbox" else PROD_BASE
    aud = TOKEN_AUD[environment]
    assertion = jwt.encode(
        {
            "iss": client_id,
            "sub": client_id,
            "aud": aud,
            "jti": uuid.uuid4().hex,
            "exp": int(now) + 300,
        },
        pem,
        algorithm="RS256",
    )
    form = urlencode(
        {"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion}
    ).encode()
    resp = _http(f"{base}/oauth2/claims/system/v1/token", headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST", body=form)
    if not resp["ok"]:
        return None, f"token request failed (HTTP {resp['status']}): {str(resp['body'])[:300]}"
    token = resp["body"].get("access_token") if isinstance(resp["body"], dict) else None
    if not token:
        return None, f"token response carried no access_token: {str(resp['body'])[:200]}"
    _token_cache.update({"token": token, "expires_at": now + float(resp["body"].get("expires_in", 1800)), "env": environment})
    return token, None

# ---------------------------------------------------------------- tools

def va_claim_status(ctx, args: dict[str, Any]) -> str:
    icn = args.get("icn") or _env_or_cfg(ctx, "default_icn", "VA_CLAIMS_ICN")
    environment = args.get("environment") or _env_or_cfg(ctx, "environment", "VA_CLAIMS_ENV") or "sandbox"
    base = SANDBOX_BASE if environment == "sandbox" else PROD_BASE

    token, err = _ccg_token(ctx, environment)
    if err:
        return _json({"success": False, "error": err, "hint": "Sandbox works immediately once credentials are set; test ICNs: https://developer.va.gov/explore/api/benefits-claims/test-users"})

    headers = {"Authorization": f"Bearer {token}"}
    if icn:
        headers["va-profile"] = f"ICN={icn}"

    if args.get("claim_id"):
        resp = _http(f"{base}/services/claims/v1/claims/{args['claim_id']}", headers=headers)
    else:
        resp = _http(f"{base}/services/claims/v1/claims", headers=headers)

    if not resp["ok"]:
        return _json({"success": False, "status": resp["status"], "error": resp["body"]})
    return _json({"success": True, "environment": environment, "icn": icn, "claims": resp["body"].get("data", resp["body"])})


def va_claim_track(ctx, args: dict[str, Any]) -> str:
    raw = va_claim_status(ctx, args)
    fetched = json.loads(raw)
    if not fetched.get("success"):
        return raw
    icn = fetched.get("icn") or "default"
    claims = fetched.get("claims") or []
    if isinstance(claims, dict):
        claims = [claims]

    store_data = store.load(icn)
    prev = {s["claim_id"]: s for s in store_data["snapshots"]} if store_data["snapshots"] else {}
    changes: list[dict[str, Any]] = []
    tracked: list[dict[str, Any]] = []

    for c in claims:
        attrs = c.get("attributes", c) if isinstance(c, dict) else {}
        cid = str(c.get("id") or attrs.get("claimId") or attrs.get("lighthouseId") or "unknown")
        if args.get("claim_id") and cid != str(args["claim_id"]):
            continue
        snap = {
            "claim_id": cid,
            "taken_at": datetime.now(timezone.utc).isoformat(),
            "status": attrs.get("status"),
            "claim_type": attrs.get("claimType") or attrs.get("claim_type"),
            "documents_needed": attrs.get("documentsNeeded") or attrs.get("documents_needed"),
            "decision_letter_sent": attrs.get("decisionLetterSent") or attrs.get("decision_letter_sent"),
            "development_letter_sent": attrs.get("developmentLetterSent") or attrs.get("development_letter_sent"),
            "min_est": attrs.get("minEstClaimDate") or attrs.get("minEstClaimDate"),
            "max_est": attrs.get("maxEstClaimDate") or attrs.get("maxEstClaimDate"),
            "contentions": attrs.get("contentions") or attrs.get("contention_list"),
            "events": attrs.get("eventsTimeline") or attrs.get("events_timeline"),
        }
        tracked.append(snap)
        old = prev.get(cid)
        if old is None:
            changes.append({"claim_id": cid, "change": "new claim tracked", "status": snap["status"]})
        else:
            for field in ("status", "documents_needed", "decision_letter_sent", "development_letter_sent", "max_est"):
                if old.get(field) != snap.get(field):
                    changes.append({"claim_id": cid, "field": field, "was": old.get(field), "now": snap.get(field)})
        store_data["snapshots"] = [s for s in store_data["snapshots"] if s["claim_id"] != cid] + [snap]

    store_data["snapshots"] = store_data["snapshots"][-200:]
    path = store.save(icn, store_data)
    return _json(
        {
            "success": True,
            "icn": icn,
            "tracked_claims": len(tracked),
            "changes_since_last_check": changes if changes else "none",
            "current": tracked,
            "history_file": path,
        }
    )


def va_disability_lookup(ctx, args: dict[str, Any]) -> str:
    query = (args.get("query") or "").strip().lower()
    limit = int(args.get("limit") or 20)
    api_key = _env_or_cfg(ctx, "reference_api_key", "VA_REFERENCE_API_KEY")
    headers = {"apikey": api_key} if api_key else {}
    resp = _http(f"{SANDBOX_BASE}/services/benefits-reference-data/v1/disabilities", headers=headers)
    if not resp["ok"]:
        detail = resp["body"]
        hint = ""
        if resp["status"] == 401 or "API key" in str(detail):
            hint = "The Benefits Reference Data API needs a free API key: request one at https://developer.va.gov/explore/api/benefits-reference-data, then set plugin setting reference_api_key (or VA_REFERENCE_API_KEY)."
        return _json({"success": False, "status": resp["status"], "error": detail, "hint": hint})

    data = resp["body"].get("data", [])
    matches = []
    for d in data:
        name = (d.get("disabilityName") or d.get("name") or "")
        diagnostic = d.get("diagnosticCode") or d.get("code")
        if query and query not in name.lower():
            continue
        matches.append({"name": name, "diagnostic_code": diagnostic})
        if len(matches) >= limit:
            break
    return _json({"success": True, "query": query, "total_contentions": len(data), "matches": matches})


def va_intent_to_file(ctx, args: dict[str, Any]) -> str:
    icn = args.get("icn") or _env_or_cfg(ctx, "default_icn", "VA_CLAIMS_ICN") or "default"
    conditions = args.get("conditions") or []
    entry = {
        "filed_on": datetime.now(timezone.utc).date().isoformat(),
        "conditions": conditions,
        "note": args.get("note") or "",
        "expires_on": None,
    }
    # Intent to file preserves the effective date for 12 months.
    today = datetime.now(timezone.utc)
    entry["expires_on"] = today.replace(year=today.year + 1).date().isoformat()
    store_data = store.load(icn)
    store_data["intent_to_file"].append(entry)
    path = store.save(icn, store_data)
    store_data = store.add_deadline(icn, f"Complete claim for intent filed {entry['filed_on']}", entry["expires_on"], "disability", "38 CFR 3.155(b): miss this and the effective date is lost")
    path = store.save(icn, store_data)
    return _json(
        {
            "success": True,
            "recorded": entry,
            "reminder": "An intent to file preserves your effective date for 12 months. The actual claim (21-526EZ) must be submitted before it expires.",
            "history_file": path,
        }
    )


def va_claim_guide(ctx, args: dict[str, Any]) -> str:
    topic = (args.get("topic") or "").strip().lower()
    out = _guide.guide_dict(topic)
    if "error" in out:
        return _json(out)
    return out["text"]


# ---------------------------------------------------------------- register

def va_benefit_track(ctx, args: dict[str, Any]) -> str:
    icn = args.get("icn") or _env_or_cfg(ctx, "default_icn", "VA_CLAIMS_ICN") or "default"
    program = (args.get("program") or "").strip().lower()
    try:
        store_data = store.set_benefit(icn, program, {
            "status": args.get("status"),
            "detail": args.get("detail"),
            "expires_on": args.get("expires_on"),
        })
    except ValueError as e:
        return _json({"success": False, "error": str(e)})
    path = store.save(icn, store_data)
    if args.get("expires_on"):
        store_data = store.add_deadline(icn, f"{program} expires", args["expires_on"], program, args.get("note") or "")
        path = store.save(icn, store_data)
    return _json({"success": True, "program": program, "record": store_data["benefits"][program], "file": path})


def va_benefit_summary(ctx, args: dict[str, Any]) -> str:
    icn = args.get("icn") or _env_or_cfg(ctx, "default_icn", "VA_CLAIMS_ICN") or "default"
    return _json({"success": True, **store.summary(icn)})




def register(ctx):
    ctx.register_tool(name="va_claim_status", toolset="va_claims", schema=schemas.VA_CLAIM_STATUS, handler=lambda args, **kw: va_claim_status(ctx, args))
    ctx.register_tool(name="va_claim_track", toolset="va_claims", schema=schemas.VA_CLAIM_TRACK, handler=lambda args, **kw: va_claim_track(ctx, args))
    ctx.register_tool(name="va_disability_lookup", toolset="va_claims", schema=schemas.VA_DISABILITY_LOOKUP, handler=lambda args, **kw: va_disability_lookup(ctx, args))
    ctx.register_tool(name="va_intent_to_file", toolset="va_claims", schema=schemas.VA_INTENT_TO_FILE, handler=lambda args, **kw: va_intent_to_file(ctx, args))
    ctx.register_tool(name="va_benefit_track", toolset="va_claims", schema=schemas.VA_BENEFIT_TRACK, handler=lambda args, **kw: va_benefit_track(ctx, args))
    ctx.register_tool(name="va_benefit_summary", toolset="va_claims", schema=schemas.VA_BENEFIT_SUMMARY, handler=lambda args, **kw: va_benefit_summary(ctx, args))
    ctx.register_tool(name="va_claim_guide", toolset="va_claims", schema=schemas.VA_CLAIM_GUIDE, handler=lambda args, **kw: va_claim_guide(ctx, args))