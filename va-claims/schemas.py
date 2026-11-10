"""Tool schemas for the va-claims plugin."""

VA_CLAIM_STATUS = {
    "name": "va_claim_status",
    "description": (
        "Fetch a veteran's VA benefits claim list and per-claim detail from the VA "
        "Benefits Claims API (Lighthouse). Works against the sandbox with test ICNs "
        "out of the box; production needs VA-issued client credentials. Returns claim "
        "status (Claim received / Initial review / Evidence gathering / Preparation "
        "for notification / Complete), contention list, estimated completion dates, "
        "and whether documents are needed."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "icn": {
                "type": "string",
                "description": "Veteran ICN (Integrated Control Number). Omit to use the configured default ICN.",
            },
            "claim_id": {
                "type": "string",
                "description": "Optional specific claim ID to fetch detail for. Omit to list all claims.",
            },
            "environment": {
                "type": "string",
                "enum": ["sandbox", "production"],
                "description": "Which VA environment to call. Defaults to the configured environment (sandbox).",
            },
        },
        "required": [],
    },
}

VA_CLAIM_TRACK = {
    "name": "va_claim_track",
    "description": (
        "Track a VA claim over time. Saves a snapshot of the claim's current status "
        "and event timeline to a local tracker file, diffs it against the previous "
        "snapshot, and reports what changed (new phase, documents needed flag, "
        "decision letter sent, estimated date shift). Call it repeatedly (e.g. from "
        "a cron job) to build a history. Use va_claim_status for a raw fetch instead."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "icn": {"type": "string", "description": "Veteran ICN. Omit to use the configured default."},
            "claim_id": {"type": "string", "description": "Claim ID to track. Omit to track every claim returned."},
            "environment": {
                "type": "string",
                "enum": ["sandbox", "production"],
                "description": "Which VA environment to call. Defaults to the configured environment.",
            },
        },
        "required": [],
    },
}

VA_DISABILITY_LOOKUP = {
    "name": "va_disability_lookup",
    "description": (
        "Look up VA disability contentions (the list of conditions VA recognizes for "
        "compensation claims) from the free Benefits Reference Data API. Search by "
        "keyword, e.g. 'tinnitus', 'knee', 'PTSD'. Returns official contention names "
        "and codes to use when filing form 21-526EZ. No auth required."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Keyword to search disability contentions, e.g. 'hearing'."},
            "limit": {"type": "integer", "description": "Max results to return (default 20)."},
        },
        "required": ["query"],
    },
}

VA_INTENT_TO_FILE = {
    "name": "va_intent_to_file",
    "description": (
        "Save a LOCAL planning note about an intent to file. This tool NEVER submits "
        "an intent or claim to VA, in sandbox or production, even with credentials. "
        "It does not establish an effective date or start a 12-month filing period. "
        "Submit separately through VA's official channels and confirm VA receipt "
        "and the applicable deadline. Success means only that the local note was saved."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "conditions": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Conditions you intend to claim, e.g. ['tinnitus', 'left knee']. Use va_disability_lookup to match official names.",
            },
            "note": {"type": "string", "description": "Optional note (evidence plan, doctor appts, etc.)."},
            "icn": {"type": "string", "description": "Veteran ICN. Omit to use the configured default."},
        },
        "required": ["conditions"],
    },
}

VA_BENEFIT_TRACK = {
    "name": "va_benefit_track",
    "description": (
        "Track a VA benefit program beyond disability claims: education (Post-9/11 "
        "GI Bill), VA health care enrollment, CHAMPVA, life insurance (SGLI/VGLI), "
        "pension, or VR&E. Records status, detail, and expiration dates locally and "
        "auto-adds a deadline when an expiration is given (e.g. the 120-day VGLI "
        "conversion window after separation, CHAMPVA renewal, GI Bill use-by date)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "program": {
                "type": "string",
                "enum": ["disability", "education", "health", "champva", "life_insurance", "pension", "voc_rehab"],
                "description": "Which benefit program to record.",
            },
            "status": {"type": "string", "description": "Current status, e.g. 'enrolled', 'approved', '36 months remaining', 'priority group 3'."},
            "detail": {"type": "string", "description": "Free-form detail (school, rating, priority group, coverage notes)."},
            "expires_on": {"type": "string", "description": "Expiration/use-by date YYYY-MM-DD if any. Creates a tracked deadline."},
            "note": {"type": "string", "description": "Optional note attached to the deadline."},
            "icn": {"type": "string", "description": "Veteran ICN. Omit to use the configured default."},
        },
        "required": ["program"],
    },
}

VA_BENEFIT_SUMMARY = {
    "name": "va_benefit_summary",
    "description": (
        "One-screen summary of everything tracked for a veteran: all benefit programs "
        "(disability, GI Bill, health, CHAMPVA, insurance, pension, VR&E), open and "
        "closed claims with latest status, local intent-to-file plans, and upcoming deadlines "
        "with days remaining. The starting point for 'where am I with my VA benefits'."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "icn": {"type": "string", "description": "Veteran ICN. Omit to use the configured default."},
        },
        "required": [],
    },
}

VA_CLAIM_GUIDE = {
    "name": "va_claim_guide",
    "description": (
        "Get a step-by-step guide for filing a VA disability compensation claim "
        "(form 21-526EZ), grounded in federal standards (38 CFR Parts 3 and 4, "
        "38 USC 5107/5902): what evidence to gather (service records, treatment records, "
        "nexus evidence, buddy statements), the at-least-as-likely-as-not standard, "
        "how the rating phases and combined-ratings table work, what each "
        "claim status means, the AMA appeal lanes, and common mistakes. Takes an optional topic: 'file', "
        "'evidence', 'status', 'ratings', 'representatives', 'timeline', or 'appeals'."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "topic": {
                "type": "string",
                "enum": ["file", "evidence", "status", "ratings", "representatives", "timeline", "appeals"],
                "description": "Which part of the process to explain. Omit for the full overview.",
            },
        },
        "required": [],
    },
}
