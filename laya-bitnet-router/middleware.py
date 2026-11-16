"""
Laya BitNet Router Middleware for Hermes.

Registers LLM_REQUEST_MIDDLEWARE to run Laya's System-1 routing on every request,
selecting the best model based on the request content.
"""

import sys
import os
import re

# Add venv to path for laya import
sys.path.insert(0, os.path.expanduser("~/.hermes/venv/lib/python3.14/site-packages"))

from laya import Agent
from typing import Dict, Any

# Laya routing schema
ROUTING_SCHEMA = {
    "type": "object",
    "properties": {
        "model": {
            "type": "string",
            "enum": ["bitnet_local", "cloud_nvidia", "nemotron_local"]
        },
        "reasoning": {"type": "boolean"}
    }
}

# Model endpoints mapping
MODEL_ENDPOINTS = {
    "bitnet_local": "http://localhost:8081",  # bitnet.cpp server
    "nemotron_local": "http://localhost:11434/v1",
    "cloud_nvidia": "https://integrate.api.nvidia.com/v1"
}

# Explicit routing rules (pattern -> model)
# These run first as a fast-path before Laya
ROUTING_RULES = [
    # Simple facts, quick questions -> bitnet_local
    (r"^(what|who|where|when|how)\s+(is|are|was|were|do|does|did)\s", "bitnet_local"),
    (r"\b(capital|define|definition|meaning of)\b", "bitnet_local"),
    (r"\b(quick|simple|basic)\b", "bitnet_local"),
    
    # Coding, debugging -> bitnet_local (good for syntax, basic logic)
    (r"\b(code|function|script|debug|error|fix|parse|write.*python|write.*javascript)\b", "bitnet_local"),
    (r"\b(how to|how do i)\b.*\b(code|css|html|sql|regex)\b", "bitnet_local"),
    
    # Math, calculations -> bitnet_local
    (r"\b(calculate|compute|math|multiply|divide|add|subtract)\b", "bitnet_local"),
    
    # Complex analysis, reasoning -> nemotron_local
    (r"\b(analyze|analysis|compare|comparison|tradeoff|trade-offs|architecture|design pattern)\b", "nemotron_local"),
    (r"\b(explain in detail|deep dive|comprehensive)\b", "nemotron_local"),
    (r"\bexplain\b.*\bdetail\b", "nemotron_local"),
    
    # Creative, complex reasoning -> cloud_nvidia
    (r"\b(creative|story|write.*story|write.*essay|write.*article|write.*blog)\b", "cloud_nvidia"),
    (r"\b(complex|reasoning|philosophy|consciousness|theoretical)\b", "cloud_nvidia"),
    (r"\b(brainstorm|ideate|innovative|novel)\b", "cloud_nvidia"),
]

# Model capabilities for Laya prompt context
MODEL_INFO = """
Available models:
1. bitnet_local (BitNet b1.58 2B via bitnet.cpp) - Fast, local, offline-capable, 2B params, good for:
   - Simple factual questions
   - Basic coding help
   - Quick responses
   - Offline work
   - Low latency (~7 tok/s on CPU)

2. nemotron_local (Nemotron 3 Ultra 4B via Ollama) - Local, 4B params, good for:
   - Medium complexity tasks
   - Structured output
   - Local processing preferred
   - Better reasoning than BitNet

3. cloud_nvidia (NVIDIA Nemotron 3 Ultra 550B) - Cloud, massive model, good for:
   - Complex reasoning
   - Creative writing
   - Deep analysis
   - Latest knowledge
   - Requires internet
"""

# Initialize Laya agent
_laya_agent = None

def get_laya_agent():
    global _laya_agent
    if _laya_agent is None:
        _laya_agent = Agent()
    return _laya_agent

def route_with_rules(text: str) -> str:
    """Fast path: route using explicit pattern rules."""
    text_lower = text.lower()
    for pattern, model in ROUTING_RULES:
        if re.search(pattern, text_lower):
            return model
    return None

def route_with_laya(request_text: str) -> str:
    """Route using Laya's decision-making (fallback when rules don't match)."""
    agent = get_laya_agent()
    
    routing_prompt = f"""{MODEL_INFO}

Request: "{request_text}"

Analyze this request and choose the BEST model:
- Consider: complexity, need for internet, offline requirement, latency sensitivity
- Simple facts, coding, offline -> bitnet_local
- Medium reasoning, local preferred -> nemotron_local  
- Complex analysis, creative, needs internet -> cloud_nvidia
"""
    
    decision = agent.decide(routing_prompt, schema=ROUTING_SCHEMA)
    return decision.get("model", "bitnet_local")

def route_request(text: str) -> str:
    """Main routing function: try rules first, then Laya."""
    # Fast path: explicit rules
    model = route_with_rules(text)
    if model:
        return model
    
    # Fallback: Laya decision
    return route_with_laya(text)

def laya_llm_request_middleware(request: dict[str, Any], **context: Any) -> dict[str, Any]:
    """
    LLM Request Middleware that runs Laya router to select the best model.
    """
    messages = request.get("messages", [])
    if not messages:
        return request
    
    # Get the last user message
    user_content = ""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            user_content = msg.get("content", "")
            if isinstance(user_content, list):
                text_parts = [part.get("text", "") for part in user_content if part.get("type") == "text"]
                user_content = " ".join(text_parts)
            break
    
    if not user_content:
        return request
    
    # Route the request
    try:
        selected_model = route_request(user_content)
        
        request["_laya_routing"] = {
            "selected_model": selected_model,
            "endpoint": MODEL_ENDPOINTS.get(selected_model),
            "original_model": request.get("model")
        }
        
        # Update model to the alias that Hermes understands
        model_aliases = {
            "bitnet_local": "bitnet_local",
            "nemotron_local": "local_nemotron", 
            "cloud_nvidia": "nvidia/nemotron-3-ultra-550b-a55b"
        }
        
        if selected_model in model_aliases:
            request["model"] = model_aliases[selected_model]
        
        print(f"[Laya BitNet Router] '{user_content[:50]}...' -> {selected_model} ({request['model']})")
        
    except Exception as e:
        print(f"[Laya BitNet Router] Error: {e}, falling back to bitnet_local")
        request["_laya_routing"] = {
            "selected_model": "bitnet_local",
            "endpoint": MODEL_ENDPOINTS["bitnet_local"],
            "original_model": request.get("model"),
            "error": str(e)
        }
        request["model"] = "bitnet_local"
    
    return request