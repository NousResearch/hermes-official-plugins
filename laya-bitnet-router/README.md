# Laya BitNet Router for Hermes

[![Hermes Plugin](https://img.shields.io/badge/Hermes-Plugin-blue)](https://github.com/NousResearch/hermes)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Version](https://img.shields.io/badge/version-1.0.0-green.svg)](plugin.yaml)

System-1 fast model routing using Laya's typed decision engine with BitNet b1.58 2B local inference. Automatically routes each request to the best available model based on request content.

## Features

- **Automatic routing** - Runs on every request via Hermes `LLM_REQUEST_MIDDLEWARE`
- **Three-tier model selection**:
  - `bitnet_local` - BitNet b1.58 2B via bitnet.cpp (~7 tok/s, offline, low latency)
  - `nemotron_local` - Nemotron 3 Ultra 4B via Ollama (medium complexity, local)
  - `cloud_nvidia` - NVIDIA Nemotron 3 Ultra 550B (complex reasoning, requires internet)
- **Fast-path rules** - Explicit regex patterns for instant decisions
- **Laya fallback** - Typed decisions for unmatched requests
- **Zero-config** - Works out of the box with sensible defaults

## Installation

### Via Hermes (recommended)
```bash
# Add to ~/.hermes/config.yaml
plugins:
  enabled:
    - laya-bitnet-router
```

### Manual
```bash
# Clone to Hermes plugins directory
git clone https://github.com/ValoLoco/laya-bitnet-router.git ~/.hermes/plugins/laya-bitnet-router

# Enable in config
hermes config set plugins.enabled '["laya-bitnet-router", "..."]'
```

## Requirements

- Hermes >= 0.22.0
- Laya (`pip install laya`)
- bitnet.cpp server running on `localhost:8081` (for `bitnet_local`)
- Ollama running on `localhost:11434` (for `nemotron_local`)
- NVIDIA API key configured (for `cloud_nvidia`)

## Routing Logic

### Fast-Path Rules (instant)
| Pattern | Model |
|---------|-------|
| `what is X?`, `capital of X`, `define X` | `bitnet_local` |
| `write python function`, `debug error`, `how to css` | `bitnet_local` |
| `analyze X`, `compare A vs B`, `tradeoffs` | `nemotron_local` |
| `explain X in detail` | `nemotron_local` |
| `write story`, `creative writing`, `brainstorm` | `cloud_nvidia` |

### Laya Fallback
For unmatched requests, Laya's typed decision engine analyzes:
- Task complexity
- Internet requirement
- Offline capability needed
- Latency sensitivity

## Configuration

The middleware uses these defaults (customize in `middleware.py`):

```python
MODEL_ENDPOINTS = {
    "bitnet_local": "http://localhost:8081",
    "nemotron_local": "http://localhost:11434/v1",
    "cloud_nvidia": "https://integrate.api.nvidia.com/v1"
}

MODEL_ALIASES = {
    "bitnet_local": "bitnet_local",
    "nemotron_local": "local_nemotron",
    "cloud_nvidia": "nvidia/nemotron-3-ultra-550b-a55b"
}
```

## Testing

```bash
# Test middleware directly
cd laya-bitnet-router
python -c "
from middleware import laya_llm_request_middleware

test = {
    'model': 'nvidia/nemotron-3-ultra-550b-a55b',
    'messages': [{'role': 'user', 'content': 'Analyze microservices vs monolith'}]
}
result = laya_llm_request_middleware(test)
print(f'Model: {result[\"model\"]}')
print(f'Routing: {result[\"_laya_routing\"]}')
"
```

Expected output:
```
[Laya Router] 'Analyze microservices vs monolith...' -> nemotron_local (local_nemotron)
Model: local_nemotron
Routing: {'selected_model': 'nemotron_local', 'endpoint': 'http://localhost:11434/v1', 'original_model': 'nvidia/nemotron-3-ultra-550b-a55b'}
```

## How It Works

1. **Middleware Registration** - `__init__.py` registers `laya_llm_request_middleware` as `LLM_REQUEST_MIDDLEWARE`
2. **Request Interception** - Every LLM request passes through the middleware
3. **Content Analysis** - Extracts last user message
4. **Fast-Path Matching** - Checks explicit regex rules first
5. **Laya Decision** - Falls back to Laya's typed decisions for complex cases
6. **Model Rewrite** - Updates request `model` field to selected alias
7. **Routing Info** - Adds `_laya_routing` metadata for debugging

## Architecture

```
User Request
    ↓
Hermes LLM_REQUEST_MIDDLEWARE chain
    ↓
laya_llm_request_middleware
    ├─ Extract user message
    ├─ Fast-path regex rules → model
    ├─ Laya Agent.decide() → model (fallback)
    ├─ Rewrite request.model to alias
    ├─ Add _laya_routing metadata
    ↓
Hermes sends to selected provider
```

## Contributing

1. Fork the repo
2. Add routing rules in `middleware.py:ROUTING_RULES`
3. Add tests in `tests/`
4. Submit PR

## License

MIT License - see [LICENSE](LICENSE)

## Credits

- [Laya](https://github.com/convai/laya) - Typed decision engine
- [bitnet.cpp](https://github.com/microsoft/BitNet) - 1-bit LLM inference
- [Hermes](https://github.com/NousResearch/hermes) - AI agent framework