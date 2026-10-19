"""
arch-sidecar: Universal Architectural Sidecar & Context Decoupling Engine
Exposed as an MCP Server + Local CLI Daemon
"""

from .digest import generate_digest
from .context import locate_surgical_context
from .decouple import prefactor_decouple

__all__ = [
    "generate_digest",
    "locate_surgical_context",
    "prefactor_decouple",
]

__version__ = "0.1.0"
