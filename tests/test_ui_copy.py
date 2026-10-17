"""Gate from docs/copy.md: no visible string in the interface uses a banned word.

Visible strings are the ones the owner reads: text between tags and the ``placeholder``,
``title`` and ``aria-label`` attributes in ``static/*.html``; the strings the scripts hand to
the DOM (``createElement`` text, ``createButton`` labels, toasts, confirms, ``textContent``,
``placeholder`` and ``title`` assignments, ``aria-label`` attributes, label tables and the
return values of ``describe*`` functions) in ``static/*.js``; and the chat messages the server
posts (``_post_system(...)`` and ``{"text": ...}`` frames) in ``adda/scheduler.py`` and
``adda/server.py``. Comments are stripped first, and template placeholders (``${...}``,
f-string fields) are not text, so code names inside them never count.
"""

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
SERVER_FILES = (ROOT / "adda" / "scheduler.py", ROOT / "adda" / "server.py")

# Words that describe how the code works rather than what the owner does (docs/copy.md).
BANNED_WORDS = (
    "harness",
    "router",
    "jev",
    "bid",
    "score",
    "session",
    "system prompt",
    "PATH",
    "mode 600",
    "shell variable",
    "provider default",
    "heuristic",
    "frame",
    "patch",
    "slug",
    "threshold",
    "automatic turn",
)
# Shown on purpose: the settings file path, and the one place Jev is named, the option that
# picks it as the turn-taking service.
ALLOWED_TEXTS = ("~/.config/adda/config.toml", "Automatic (Jev)")

BANNED_PATTERN = re.compile(
    r"\b(?:" + "|".join(re.escape(word) for word in BANNED_WORDS) + r")(?:s|es|d|ed)?\b",
    re.IGNORECASE,
)

# JS calls whose argument at the given index is text the owner reads.
JS_TEXT_CALLS = {
    "createElement": 2,
    "createButton": 0,
    "showToast": 0,
    "reportError": 0,
    "confirm": 0,
    "addLocalMessage": 0,
    "createSeatField": 2,
    "labelledField": 0,
    "wrapCell": 0,
    "fillAccessSelect": 2,
}
JS_TEXT_PROPERTIES = ("textContent", "placeholder", "title")
JS_TEXT_ATTRIBUTES = ("aria-label", "title")
JS_TEXT_KEYS = ("label", "description")
QUOTES = "'\"`"


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------


def html_visible_strings(html: str) -> list[str]:
    html = re.sub(r"<!--.*?-->", " ", html, flags=re.DOTALL)
    html = re.sub(r"<(script|style)\b.*?</\1>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    strings = [text.strip() for text in re.findall(r">([^<]+)<", html)]
    for attribute in ("placeholder", "title", "aria-label"):
        strings += re.findall(rf'\b{attribute}="([^"]*)"', html)
    return [text for text in strings if text.strip()]


# ---------------------------------------------------------------------------
# JavaScript
# ---------------------------------------------------------------------------


def strip_js_comments(source: str) -> str:
    """Drop block comments, whole-line comments and ` // ` tails; `://` in URLs survives."""
    source = re.sub(r"/\*.*?\*/", " ", source, flags=re.DOTALL)
    source = re.sub(r"(?m)^\s*//.*$", "", source)
    return re.sub(r"(?m)\s//\s.*$", "", source)


def skip_string(source: str, index: int) -> int:
    """Return the index just past the string literal that starts at `index`."""
    quote = source[index]
    index += 1
    while index < len(source):
        character = source[index]
        if character == "\\":
            index += 2
            continue
        if character == quote:
            return index + 1
        if quote == "`" and source.startswith("${", index):
            index = skip_balanced(source, index + 1, "{", "}")
            continue
        index += 1
    raise ValueError(f"unterminated string literal near: {source[index - 40 : index]!r}")


def skip_balanced(source: str, index: int, opening: str, closing: str) -> int:
    """`index` is at `opening`; return the index just past its matching `closing`."""
    depth = 0
    while index < len(source):
        character = source[index]
        if character in QUOTES:
            index = skip_string(source, index)
            continue
        if character == opening:
            depth += 1
        elif character == closing:
            depth -= 1
            if depth == 0:
                return index + 1
        index += 1
    raise ValueError(f"unbalanced {opening}{closing} near: {source[index - 40 : index]!r}")


def call_arguments(source: str, open_paren_index: int) -> list[str]:
    """Split the argument list that starts at `open_paren_index` on its top-level commas."""
    end = skip_balanced(source, open_paren_index, "(", ")")
    inner = source[open_paren_index + 1 : end - 1]
    arguments, depth, start, index = [], 0, 0, 0
    while index < len(inner):
        character = inner[index]
        if character in QUOTES:
            index = skip_string(inner, index)
            continue
        if character in "([{":
            depth += 1
        elif character in ")]}":
            depth -= 1
        elif character == "," and depth == 0:
            arguments.append(inner[start:index])
            start = index + 1
        index += 1
    arguments.append(inner[start:])
    return [argument.strip() for argument in arguments]


def expression_until_semicolon(source: str, index: int) -> str:
    start = index
    depth = 0
    while index < len(source):
        character = source[index]
        if character in QUOTES:
            index = skip_string(source, index)
            continue
        if character in "([{":
            depth += 1
        elif character in ")]}":
            if depth == 0:
                break
            depth -= 1
        elif character == ";" and depth == 0:
            break
        index += 1
    return source[start:index]


def string_literals(expression: str) -> list[str]:
    """The text of every string literal in `expression`; `${...}` fields become a space."""
    literals = []
    index = 0
    while index < len(expression):
        if expression[index] in QUOTES:
            end = skip_string(expression, index)
            body = expression[index + 1 : end - 1]
            if expression[index] == "`":
                body = remove_template_fields(body)
            literals.append(body)
            index = end
        else:
            index += 1
    return literals


def remove_template_fields(body: str) -> str:
    pieces, index = [], 0
    while index < len(body):
        if body.startswith("${", index):
            index = skip_balanced(body, index + 1, "{", "}")
            pieces.append(" ")
        else:
            pieces.append(body[index])
            index += 1
    return "".join(pieces)


def js_visible_strings(source: str) -> list[str]:
    source = strip_js_comments(source)
    strings = []
    for name, argument_index in JS_TEXT_CALLS.items():
        for match in re.finditer(rf"(?<![\w.]){name}\(|\.{name}\(", source):
            arguments = call_arguments(source, match.end() - 1)
            if argument_index < len(arguments):
                strings += string_literals(arguments[argument_index])
    for attribute in JS_TEXT_ATTRIBUTES:
        for match in re.finditer(rf"setAttribute\('{attribute}',", source):
            arguments = call_arguments(source, match.start() + len("setAttribute"))
            strings += string_literals(arguments[1])
    for property_name in JS_TEXT_PROPERTIES:
        for match in re.finditer(rf"\.{property_name}\s*=(?!=)", source):
            strings += string_literals(expression_until_semicolon(source, match.end()))
    for key in JS_TEXT_KEYS:
        for match in re.finditer(rf"\b{key}:\s*", source):
            strings += string_literals(expression_until_comma(source, match.end()))
    # Text tables such as QUIET_REASON_TEXT and ACCESS_LABELS, and local `labels`/`names` maps.
    for match in re.finditer(r"const (?:[A-Z][A-Z_]+|labels|names) = \{", source):
        block = source[match.end() - 1 : skip_balanced(source, match.end() - 1, "{", "}")]
        strings += string_literals(block)
    # Words picked into a sentence: `const harnessWord = count === 1 ? 'harness' : 'harnesses'`.
    # An element built by a call is covered by the call patterns; its class names are not text.
    for match in re.finditer(r"const \w+(?:Word|Label|Text|Name) = (?![\w.]+\()", source):
        strings += string_literals(expression_until_semicolon(source, match.end()))
    for match in re.finditer(r"function describe\w*\([^)]*\)\s*\{", source):
        body = source[match.end() - 1 : skip_balanced(source, match.end() - 1, "{", "}")]
        for return_match in re.finditer(r"\breturn\b", body):
            strings += string_literals(expression_until_semicolon(body, return_match.end()))
    # Bare text nodes: `.append('No messages yet.')`.
    for match in re.finditer(r"\.append\(", source):
        for argument in call_arguments(source, match.end() - 1):
            if argument[:1] in QUOTES:
                strings += string_literals(argument)
    return [text for text in strings if text.strip()]


def expression_until_comma(source: str, index: int) -> str:
    expression = expression_until_semicolon(source, index)
    return call_arguments("(" + expression + ")", 0)[0]


# ---------------------------------------------------------------------------
# Python server messages
# ---------------------------------------------------------------------------


def python_visible_strings(source: str) -> list[str]:
    strings = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            function = node.func
            if isinstance(function, ast.Attribute) and function.attr == "_post_system":
                strings += [text for argument in node.args for text in constant_texts(argument)]
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=True):
                if isinstance(key, ast.Constant) and key.value == "text":
                    strings += constant_texts(value)
    return [text for text in strings if text.strip()]


def constant_texts(node: ast.AST) -> list[str]:
    """Literal text of a string or f-string; formatted fields are left out."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.JoinedStr):
        pieces = []
        for part in node.values:
            if isinstance(part, ast.Constant):
                pieces.append(part.value)
            else:
                pieces.append(" ")
        return ["".join(pieces)]
    return []


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------


def visible_strings_by_file() -> dict[str, list[str]]:
    strings = {}
    for path in sorted(STATIC.glob("*.html")):
        strings[path.name] = html_visible_strings(path.read_text(encoding="utf-8"))
    for path in sorted(STATIC.glob("*.js")):
        strings[path.name] = js_visible_strings(path.read_text(encoding="utf-8"))
    for path in SERVER_FILES:
        strings[path.name] = python_visible_strings(path.read_text(encoding="utf-8"))
    return strings


def banned_words_in(text: str) -> list[str]:
    for allowed in ALLOWED_TEXTS:
        text = text.replace(allowed, " ")
    return BANNED_PATTERN.findall(text)


def test_no_visible_string_uses_a_banned_word():
    offences = [
        f"{file_name}: {text!r} uses {word!r}"
        for file_name, strings in visible_strings_by_file().items()
        for text in strings
        for word in banned_words_in(text)
    ]
    assert offences == [], "\n".join(offences)


def test_the_scanner_finds_strings_in_every_file():
    """Guards the gate itself: an extractor that finds nothing would pass every file."""
    strings_by_file = visible_strings_by_file()
    for path in [*STATIC.glob("*.html"), *STATIC.glob("*.js")]:
        assert strings_by_file[path.name], f"no visible strings found in {path.name}"
    # server.py only forwards error texts, so the server's literal messages are all in scheduler.py.
    assert any(strings_by_file[path.name] for path in SERVER_FILES), "no server messages found"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Harnesses on this machine", ["Harnesses"]),
        ("Router: Jev", ["Router", "Jev"]),
        ("Automatic (Jev)", []),
        ("Saved to ~/.config/adda/config.toml.", []),
        ("Automatic turn-taking is unavailable.", ["Automatic turn"]),
        ("Path to the folder", ["Path"]),
        ("Seats can only talk.", []),
    ],
)
def test_banned_word_matching(text, expected):
    assert banned_words_in(text) == expected


def test_extractors_read_the_patterns_the_gate_promises():
    html = '<!-- harness --><p title="Tip">Text</p><input placeholder="Hint" aria-label="Field">'
    assert html_visible_strings(html) == ["Text", "Hint", "Tip", "Field"]
    script = (
        "// harness note\n"
        "createElement('span', 'seat-score', `Bid ${seat.score}`);\n"
        "node.textContent = ok ? 'Yes' : 'No';\n"
        "function describeThing(x) { return x ? 'One' : 'Two'; }\n"
    )
    assert js_visible_strings(script) == ["Bid  ", "Yes", "No", "One", "Two"]
    python = 'async def f(self):\n    await self._post_system(f"Seat {name} failed")\n'
    assert python_visible_strings(python) == ["Seat   failed"]
