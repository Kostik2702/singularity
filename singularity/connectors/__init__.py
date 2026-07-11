"""External-source connectors (AUD-22). Off by default; see registry."""
from .base import Connector, ConnectorManifest, NormalizedDoc, validate_manifest

__all__ = ["Connector", "ConnectorManifest", "NormalizedDoc", "validate_manifest"]
