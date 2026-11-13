"""Fixed desktop MCP provider namespaces; never derive paths from user text."""
from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True)
class MCPNamespace:
    provider: str
    display_name: str
    connector_name: str
    server_name: str
    bridge_file: str
    bridge_lock: str
    bridge_prefix: str
    probe_directory: str
    probe_workspaces: str


_NAMESPACES = MappingProxyType({
    'workbuddy': MCPNamespace('workbuddy', 'WorkBuddy', 'todo-dispatcher', 'todo-workbuddy-worker',
        'workbuddy-bridge.json', 'workbuddy-bridge.lock', 'workbuddy-bridge', 'workbuddy-probe', 'workbuddy-probes'),
    'doubao': MCPNamespace('doubao', 'Doubao', 'todo-dispatcher-doubao', 'todo-dispatcher-doubao',
        'doubao-bridge.json', 'doubao-bridge.lock', 'doubao-bridge', 'doubao-probe', 'doubao-probes'),
})
SUPPORTED_PROVIDERS = tuple(_NAMESPACES)


def mcp_namespace(provider='workbuddy'):
    if not isinstance(provider, str) or provider not in _NAMESPACES:
        raise ValueError('Unsupported desktop MCP provider')
    return _NAMESPACES[provider]
