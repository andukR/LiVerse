"""Bible parser core package."""

from bible_parser_core.parser import ParsedReference, normalize_text, parse_live_reference

__version__ = "1.0.0"

__all__ = ["ParsedReference", "__version__", "normalize_text", "parse_live_reference"]
