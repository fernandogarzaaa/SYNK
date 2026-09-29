# WebMCP package: real model-context gateway (Stage E) + legacy pieces.
from .schema import WebMCPTool, WebMCPSite, ToolCall, ToolResult, ToolAnnotation
from .registry import Capability, CapabilityRegistry, REGISTRY
from .discovery import WebMCPDiscovery, load_mock_sites
from .adapter import WebMCPAdapter, ADAPTER
from .policy import PolicyEngine, CAPABILITY_POLICY, CapabilityPolicy, PolicyDecision
from .verifier import WebMCPVerifier, VERIFIER, VerificationError
from .scope import WebMCPScope, scope_for_tab, current_document_id
from .transport import (ModelContextTransport, WebMCPDiscoveryResult,
                        CdpModelContextTransport, ExtensionSnapshotTransport,
                        FakeModelContextTransport, WebMCPTransportError,
                        WebMCPUnavailable, normalize_tool_dict,
                        MODEL_CONTEXT_PROBE_JS, MODEL_CONTEXT_INVOKE_JS)
from .selection import CapabilitySelector, SelectionRecord, CandidateScore
from .fallback import LEGACY_FALLBACK_TABLE, FALLBACK_CAVEAT
from .gateway import (WebMCPGateway, WebMCPHandle, GatewayInvocation,
                      default_fallback_table,
                      WEBMCP_TOOL_NOT_ADVERTISED, WEBMCP_UNAVAILABLE,
                      WEBMCP_STALE_HANDLE, WEBMCP_SCOPE_VIOLATION,
                      WEBMCP_SCHEMA_INVALID)

__all__ = [
    "WebMCPTool", "WebMCPSite", "ToolCall", "ToolResult", "ToolAnnotation",
    "Capability", "CapabilityRegistry", "REGISTRY",
    "WebMCPDiscovery", "load_mock_sites",
    "WebMCPAdapter", "ADAPTER",
    "PolicyEngine", "CAPABILITY_POLICY", "CapabilityPolicy", "PolicyDecision",
    "WebMCPVerifier", "VERIFIER", "VerificationError",
    "WebMCPScope", "scope_for_tab", "current_document_id",
    "ModelContextTransport", "WebMCPDiscoveryResult",
    "CdpModelContextTransport", "ExtensionSnapshotTransport",
    "FakeModelContextTransport", "WebMCPTransportError",
    "WebMCPUnavailable", "normalize_tool_dict",
    "MODEL_CONTEXT_PROBE_JS", "MODEL_CONTEXT_INVOKE_JS",
    "CapabilitySelector", "SelectionRecord", "CandidateScore",
    "LEGACY_FALLBACK_TABLE", "FALLBACK_CAVEAT",
    "WebMCPGateway", "WebMCPHandle", "GatewayInvocation",
    "default_fallback_table",
    "WEBMCP_TOOL_NOT_ADVERTISED", "WEBMCP_UNAVAILABLE",
    "WEBMCP_STALE_HANDLE", "WEBMCP_SCOPE_VIOLATION",
    "WEBMCP_SCHEMA_INVALID",
]
