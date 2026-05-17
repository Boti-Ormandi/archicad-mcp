"""MCP server for Archicad automation via the Tapir JSON API."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("archicad-mcp")
except PackageNotFoundError:
    __version__ = "0.0.0+unknown"
