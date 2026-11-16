"""Laya BitNet Router: System-1 fast model routing using Laya's typed decision engine.

Automatically routes each request to the best model:
- bitnet_local (BitNet b1.58 2B via bitnet.cpp) - Simple tasks, offline, low latency
- nemotron_local (Nemotron 3 Ultra 4B via Ollama) - Medium complexity, local
- cloud_nvidia (NVIDIA Nemotron 3 Ultra 550B) - Complex reasoning, requires internet
"""

from __future__ import annotations

import logging
import sys
import os

logger = logging.getLogger(__name__)

# Add venv to path for laya import
sys.path.insert(0, os.path.expanduser("~/.hermes/venv/lib/python3.14/site-packages"))

def register(ctx) -> None:
    """Register the Laya BitNet router middleware."""
    try:
        import sys
        import os
        # Add plugin directory to path for absolute import
        plugin_dir = os.path.dirname(__file__)
        if plugin_dir not in sys.path:
            sys.path.insert(0, plugin_dir)
        from middleware import laya_llm_request_middleware
    except ImportError as exc:
        logger.warning(f"Laya BitNet Router: Failed to import middleware: {exc}")
        return
    
    ctx.register_middleware("llm_request", laya_llm_request_middleware)
    logger.info("Laya BitNet Router: Middleware registered successfully")