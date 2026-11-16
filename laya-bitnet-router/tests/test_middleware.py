"""Tests for Laya BitNet Router middleware."""

import sys
import os

# Add plugin directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from middleware import (
    route_with_rules,
    route_request,
    laya_llm_request_middleware,
    MODEL_ENDPOINTS,
    ROUTING_RULES,
)


class TestRoutingRules:
    """Test explicit routing rules."""

    @pytest.mark.parametrize("text,expected_model", [
        # Simple facts
        ("What is 2+2?", "bitnet_local"),
        ("What is the capital of France?", "bitnet_local"),
        ("Define photosynthesis", "bitnet_local"),
        ("Who is the president?", "bitnet_local"),
        
        # Coding
        ("Write a Python function to parse JSON", "bitnet_local"),
        ("How to debug TypeError?", "bitnet_local"),
        ("How to center a div in CSS?", "bitnet_local"),
        
        # Analysis/Comparison
        ("Analyze microservices vs monolith", "nemotron_local"),
        ("Compare tradeoffs of databases", "nemotron_local"),
        ("Explain quantum entanglement in detail", "nemotron_local"),
        
        # Creative
        ("Write a creative story about a robot", "cloud_nvidia"),
        ("Brainstorm innovative startup ideas", "cloud_nvidia"),
    ])
    def test_routing_rules(self, text, expected_model):
        """Test that explicit rules match expected models."""
        result = route_with_rules(text)
        assert result == expected_model, f"Failed for: {text}"


class TestMiddleware:
    """Test the LLM request middleware."""

    def test_no_messages(self):
        """Test middleware with no messages."""
        request = {"model": "test-model", "messages": []}
        result = laya_llm_request_middleware(request)
        assert result == request

    def test_empty_user_content(self):
        """Test middleware with empty user message."""
        request = {
            "model": "test-model",
            "messages": [{"role": "user", "content": ""}]
        }
        result = laya_llm_request_middleware(request)
        assert result == request

    def test_simple_fact_routes_to_bitnet(self):
        """Test simple factual question routes to bitnet_local."""
        request = {
            "model": "nvidia/nemotron-3-ultra-550b-a55b",
            "messages": [{"role": "user", "content": "What is 2+2?"}]
        }
        result = laya_llm_request_middleware(request)
        assert result["model"] == "bitnet_local"
        assert "_laya_routing" in result
        assert result["_laya_routing"]["selected_model"] == "bitnet_local"

    def test_coding_routes_to_bitnet(self):
        """Test coding question routes to bitnet_local."""
        request = {
            "model": "nvidia/nemotron-3-ultra-550b-a55b",
            "messages": [{"role": "user", "content": "Write a Python function"}]
        }
        result = laya_llm_request_middleware(request)
        assert result["model"] == "bitnet_local"

    def test_analysis_routes_to_nemotron(self):
        """Test analysis question routes to nemotron_local."""
        request = {
            "model": "nvidia/nemotron-3-ultra-550b-a55b",
            "messages": [{"role": "user", "content": "Analyze microservices vs monolith"}]
        }
        result = laya_llm_request_middleware(request)
        assert result["model"] == "local_nemotron"

    def test_creative_routes_to_cloud(self):
        """Test creative request routes to cloud_nvidia."""
        request = {
            "model": "nvidia/nemotron-3-ultra-550b-a55b",
            "messages": [{"role": "user", "content": "Write a creative story"}]
        }
        result = laya_llm_request_middleware(request)
        assert result["model"] == "nvidia/nemotron-3-ultra-550b-a55b"

    def test_preserves_original_model(self):
        """Test that original model is stored in routing info."""
        request = {
            "model": "original-model",
            "messages": [{"role": "user", "content": "What is 2+2?"}]
        }
        result = laya_llm_request_middleware(request)
        assert result["_laya_routing"]["original_model"] == "original-model"

    def test_routing_info_structure(self):
        """Test routing info has correct structure."""
        request = {
            "model": "test-model",
            "messages": [{"role": "user", "content": "What is 2+2?"}]
        }
        result = laya_llm_request_middleware(request)
        routing = result["_laya_routing"]
        assert "selected_model" in routing
        assert "endpoint" in routing
        assert "original_model" in routing
        assert routing["endpoint"] == "http://localhost:8081"


class TestModelEndpoints:
    """Test model endpoints configuration."""

    def test_endpoints_exist(self):
        """Test all required endpoints are configured."""
        assert "bitnet_local" in MODEL_ENDPOINTS
        assert "nemotron_local" in MODEL_ENDPOINTS
        assert "cloud_nvidia" in MODEL_ENDPOINTS

    def test_endpoints_valid(self):
        """Test endpoints are valid URLs."""
        for endpoint in MODEL_ENDPOINTS.values():
            assert endpoint.startswith("http")


class TestRoutingConfig:
    """Test routing rules configuration."""

    def test_rules_not_empty(self):
        """Test that routing rules are defined."""
        assert len(ROUTING_RULES) > 0

    def test_rules_format(self):
        """Test each rule is a (pattern, model) tuple."""
        for pattern, model in ROUTING_RULES:
            assert isinstance(pattern, str)
            assert isinstance(model, str)
            assert model in ["bitnet_local", "nemotron_local", "cloud_nvidia"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])