"""Tool schemas for the camofox toolset.

Names match the browser tools Hermes core used to ship, so skills and prompts that call
``browser_navigate`` / ``browser_snapshot`` / ... keep working. Descriptions are core's text,
trimmed only where Camofox cannot do what core's text promised: snapshots have no compact/full
switch, screenshots have no numbered-label overlay and are always analysed by the auxiliary vision
model, and the Camofox REST API exposes no console log capture.
"""

BROWSER_NAVIGATE = {
    "name": "browser_navigate",
    "description": "Navigate to a URL in the browser. Initializes the session and loads the page. Must be called before other browser tools. For simple information retrieval, prefer a lightweight retrieval tool when one is available (faster, cheaper). For plain-text endpoints — URLs ending in .md, .txt, .json, .yaml, .yml, .csv, .xml, raw.githubusercontent.com, or any documented API endpoint — prefer an available text-extraction or terminal-fetch tool; the browser stack is overkill and much slower for these. Use browser tools when you need to interact with a page (click, fill forms, dynamic content). Returns a compact page snapshot with interactive elements and ref IDs — no need to call browser_snapshot separately after navigating.",
    "parameters": {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "The URL to navigate to (e.g., 'https://example.com')"
            }
        },
        "required": ["url"]
    }
}

BROWSER_SNAPSHOT = {
    "name": "browser_snapshot",
    "description": "Get a text-based snapshot of the current page's accessibility tree. Returns interactive elements with ref IDs (like @e1, @e2) for browser_click and browser_type. Snapshots over 15000 chars are truncated; when that happens the complete snapshot is saved to a file and the output includes its path so you can page through the rest with read_file. Requires browser_navigate first. Note: browser_navigate already returns a snapshot — use this to refresh after interactions that change the page.",
    "parameters": {"type": "object", "properties": {}, "required": []}
}

BROWSER_CLICK = {
    "name": "browser_click",
    "description": "Click on an element identified by its ref ID from the snapshot (e.g., '@e5'). The ref IDs are shown in square brackets in the snapshot output. Requires browser_navigate and browser_snapshot to be called first.",
    "parameters": {
        "type": "object",
        "properties": {
            "ref": {
                "type": "string",
                "description": "The element reference from the snapshot (e.g., '@e5', '@e12')"
            }
        },
        "required": ["ref"]
    }
}

BROWSER_TYPE = {
    "name": "browser_type",
    "description": "Type text into an input field identified by its ref ID. Clears the field first, then types the new text. Requires browser_navigate and browser_snapshot to be called first.",
    "parameters": {
        "type": "object",
        "properties": {
            "ref": {
                "type": "string",
                "description": "The element reference from the snapshot (e.g., '@e3')"
            },
            "text": {
                "type": "string", "description": "The text to type into the field"
            }
        },
        "required": ["ref", "text"]
    }
}

BROWSER_SCROLL = {
    "name": "browser_scroll",
    "description": "Scroll the page in a direction. Use this to reveal more content that may be below or above the current viewport. Requires browser_navigate to be called first.",
    "parameters": {
        "type": "object",
        "properties": {
            "direction": {
                "type": "string", "enum": ["up", "down"], "description": "Direction to scroll"
            }
        },
        "required": ["direction"]
    }
}

BROWSER_BACK = {
    "name": "browser_back",
    "description": "Navigate back to the previous page in browser history. Requires browser_navigate to be called first.",
    "parameters": {"type": "object", "properties": {}, "required": []}
}

BROWSER_PRESS = {
    "name": "browser_press",
    "description": "Press a keyboard key. Useful for submitting forms (Enter), navigating (Tab), or keyboard shortcuts. Requires browser_navigate to be called first.",
    "parameters": {
        "type": "object",
        "properties": {
            "key": {
                "type": "string",
                "description": "Key to press (e.g., 'Enter', 'Tab', 'Escape', 'ArrowDown')"
            }
        },
        "required": ["key"]
    }
}

BROWSER_GET_IMAGES = {
    "name": "browser_get_images",
    "description": "Get a list of all images on the current page with their URLs and alt text. Useful for finding images to analyze with the vision tool. Requires browser_navigate to be called first.",
    "parameters": {"type": "object", "properties": {}, "required": []}
}

BROWSER_VISION = {
    "name": "browser_vision",
    "description": "Take a screenshot of the current page so you can inspect it visually. Use this when you need to understand what the page looks like - especially for CAPTCHAs, visual verification challenges, complex layouts, or cases where the text snapshot misses important visual information. An auxiliary vision model analyzes the screenshot and returns a text answer to your question. Includes a screenshot_path that you can share with the user by including MEDIA:<screenshot_path> in your response. Requires browser_navigate to be called first.",
    "parameters": {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "What you want to know about the page visually. Be specific about what you're looking for."
            }
        },
        "required": ["question"]
    }
}

BROWSER_CONSOLE = {
    "name": "browser_console",
    "description": "Evaluate JavaScript in the page context and return the result when 'expression' is provided — use this for DOM inspection, reading page state, or extracting data programmatically. Console log and JavaScript error capture are not available with the Camofox backend, so a call without 'expression' returns an empty result. Requires browser_navigate to be called first.",
    "parameters": {
        "type": "object",
        "properties": {
            "expression": {
                "type": "string",
                "description": "JavaScript expression to evaluate in the page context. Runs in the browser like DevTools console — full access to DOM, window, document. Return values are serialized to JSON. Example: 'document.title' or 'document.querySelectorAll(\"a\").length'"
            }
        },
        "required": []
    }
}

BROWSER_IMPORT_COOKIES = {
    "name": "browser_import_cookies",
    "description": "Import cookies from a Netscape-format cookies.txt file into the current browser session. Use to authenticate to sites without interactive login. Files must live under browser.camofox.cookies_dir (default ~/.camofox/cookies/). Requires CAMOFOX_API_KEY for bearer-token authentication.",
    "parameters": {
        "type": "object",
        "properties": {
            "cookies_path": {
                "type": "string",
                "description": "Path to the Netscape cookies.txt file, relative to browser.camofox.cookies_dir (e.g., 'linkedin.txt')"
            },
            "domain_suffix": {
                "type": "string",
                "description": "Optional. Only import cookies whose domain ends with this suffix (e.g., '.linkedin.com')"
            }
        },
        "required": ["cookies_path"]
    }
}

ALL_SCHEMAS = (
    BROWSER_NAVIGATE, BROWSER_SNAPSHOT, BROWSER_CLICK, BROWSER_TYPE, BROWSER_SCROLL, BROWSER_BACK,
    BROWSER_PRESS, BROWSER_GET_IMAGES, BROWSER_VISION, BROWSER_CONSOLE, BROWSER_IMPORT_COOKIES,
)
