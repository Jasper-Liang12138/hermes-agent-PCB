"""
Unified KiCad parser entry.

Switch between:
- AST parser (slow but complete)
- FAST parser (block-based, much faster)
"""

USE_FAST_PARSER = True

if USE_FAST_PARSER:
    from .kicad_parser_fast import parse_kicad
else:
    from .kicad_parser_ast import parse_kicad