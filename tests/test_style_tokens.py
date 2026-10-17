"""Gate from docs/design-system.md: sizes in the stylesheets come from the token block.

Outside the ``:root`` blocks and ``@media`` preludes, no stylesheet under ``static/`` may contain
a ``rem`` value or a ``px`` value above 3. Values of 1 to 3px are borders and outlines, which the
spec allows inline. Tokens are defined once, in ``static/style.css``.
"""

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "static"
STYLESHEET = STATIC / "style.css"

MAX_INLINE_PX = 3

RAW_LENGTH = re.compile(r"(?<![\w.-])(\d*\.?\d+)(px|rem)\b")


def strip_comments(css: str) -> str:
    """Blank out comments but keep newlines so reported line numbers stay right."""

    def keep_newlines(match: re.Match[str]) -> str:
        return "\n" * match.group(0).count("\n")

    return re.sub(r"/\*.*?\*/", keep_newlines, css, flags=re.DOTALL)


def root_block_spans(css: str) -> list[tuple[int, int]]:
    """Character spans of every ``:root { ... }`` block, where raw values are allowed."""
    spans = []
    for match in re.finditer(r":root\s*\{", css):
        depth = 1
        position = match.end()
        while depth and position < len(css):
            if css[position] == "{":
                depth += 1
            elif css[position] == "}":
                depth -= 1
            position += 1
        spans.append((match.start(), position))
    return spans


def media_prelude_spans(css: str) -> list[tuple[int, int]]:
    """Spans of ``@media (...)`` preludes; breakpoints are raw px by nature."""
    return [match.span() for match in re.finditer(r"@media[^{]*", css)]


def inside(position: int, spans: list[tuple[int, int]]) -> bool:
    return any(start <= position < end for start, end in spans)


def raw_length_violations(css: str, name: str = "style.css") -> list[str]:
    css = strip_comments(css)
    allowed = root_block_spans(css) + media_prelude_spans(css)
    violations = []
    for match in RAW_LENGTH.finditer(css):
        if inside(match.start(), allowed):
            continue
        number, unit = match.groups()
        if unit == "px" and float(number) <= MAX_INLINE_PX:
            continue
        line_number = css.count("\n", 0, match.start()) + 1
        violations.append(f"{name}:{line_number}: {match.group(0)}")
    return violations


def test_sizes_outside_tokens_come_from_the_scale():
    violations = []
    for stylesheet in sorted(STATIC.glob("*.css")):
        violations += raw_length_violations(stylesheet.read_text(), stylesheet.name)
    assert not violations, (
        f"{len(violations)} raw lengths outside :root; use a --space-*, --text-* or "
        f"--control-height token:\n" + "\n".join(violations)
    )


def test_root_block_defines_the_spec_tokens():
    css = strip_comments(STYLESHEET.read_text())
    root_css = "".join(css[start:end] for start, end in root_block_spans(css))
    required = [
        "--text-small",
        "--text-body",
        "--text-title",
        "--text-page",
        "--space-1",
        "--space-2",
        "--space-3",
        "--space-4",
        "--space-5",
        "--space-6",
        "--control-height",
    ]
    missing = [token for token in required if f"{token}:" not in root_css]
    assert not missing, f"tokens named in docs/design-system.md but absent from :root: {missing}"
