"""Bible parser core package."""

from bible_parser_core.parser import ParsedReference, normalize_text, parse_live_reference
from bible_parser_core.version import __version__

__all__ = ["ParsedReference", "__version__", "normalize_text", "parse_live_reference"]
