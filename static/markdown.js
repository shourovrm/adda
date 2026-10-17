// Adda Markdown: a small subset rendered straight into DOM nodes.
// Nothing here uses innerHTML; every piece of text goes through textContent,
// so model output can never inject markup. Modelled on jonaki's MarkdownParser.
(function () {
  "use strict";

  var HEADING_PATTERN = /^(#{1,6})\s+(.*)$/;
  var BULLET_PATTERN = /^\s*[-*+]\s+(.*)$/;
  var ORDERED_PATTERN = /^\s*(\d{1,9})[.)]\s+(.*)$/;
  // "- - -" would otherwise read as a bullet, so the rule is tested first.
  var RULE_PATTERN = /^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$/;
  var FENCE_PATTERN = /^\s*(`{3,}|~{3,})\s*([^\s`]*)/;
  var TABLE_SEPARATOR_CELL = /^:?-+:?$/;
  var MENTION_PATTERN = /(^|[^\w@])@([A-Za-z0-9_]+(?:-[A-Za-z0-9_]+)*)/g;
  var SAFE_LINK_PATTERN = /^(https?:\/\/|mailto:)/i;
  // The owner has no seat entry, so @all uses the owner colour variable.
  var OWNER_COLOUR = "var(--owner, #F2C200)";

  function element(tagName, className) {
    var node = document.createElement(tagName);
    if (className) node.className = className;
    return node;
  }

  function isWordCharacter(character) {
    return character !== undefined && character !== "" && /\w/.test(character);
  }

  // ---------- inline ----------

  function appendTextWithMentions(parent, text, seatsByHandle) {
    var lastEnd = 0;
    var match;
    MENTION_PATTERN.lastIndex = 0;
    while ((match = MENTION_PATTERN.exec(text)) !== null) {
      var handle = match[2].toLowerCase();
      var seat = seatsByHandle ? seatsByHandle[handle] : undefined;
      var colour = null;
      var label = null;
      if (handle === "all") {
        colour = OWNER_COLOUR;
        label = "@all";
      } else if (seat) {
        colour = seat.color;
        label = "@" + (seat.name || handle);
      }
      if (colour === null) continue;
      var mentionStart = match.index + match[1].length;
      if (mentionStart > lastEnd) {
        parent.appendChild(document.createTextNode(text.slice(lastEnd, mentionStart)));
      }
      var span = element("span", "mention");
      span.setAttribute("data-handle", handle);
      span.style.setProperty("--m", colour);
      span.textContent = label;
      parent.appendChild(span);
      lastEnd = match.index + match[0].length;
    }
    if (lastEnd < text.length) {
      parent.appendChild(document.createTextNode(text.slice(lastEnd)));
    }
  }

  function hasTightEdges(inner) {
    return inner.length > 0 && !/\s/.test(inner[0]) && !/\s/.test(inner[inner.length - 1]);
  }

  // Each reader returns {node, end} or null when the syntax at index is not a span.
  function readCodeSpan(text, index) {
    var close = text.indexOf("`", index + 1);
    if (close <= index + 1) return null;
    var code = element("code", "md-inline-code");
    code.textContent = text.slice(index + 1, close);
    return { node: code, end: close + 1 };
  }

  function readStrong(text, index, seatsByHandle) {
    var close = text.indexOf("**", index + 2);
    if (close <= index + 2) return null;
    var inner = text.slice(index + 2, close);
    if (!hasTightEdges(inner)) return null;
    var strong = element("strong");
    renderInline(strong, inner, seatsByHandle);
    return { node: strong, end: close + 2 };
  }

  function readEmphasis(text, index, seatsByHandle) {
    var marker = text[index];
    var close = text.indexOf(marker, index + 1);
    // A "*" that opens a "**" pair belongs to strong, not to this emphasis.
    while (marker === "*" && close !== -1 && text.startsWith("**", close)) {
      close = text.indexOf("*", close + 2);
    }
    if (close <= index + 1) return null;
    var inner = text.slice(index + 1, close);
    if (!hasTightEdges(inner)) return null;
    if (marker === "_") {
      // snake_case_name is a name, not emphasis.
      if (isWordCharacter(text[index - 1]) || isWordCharacter(text[close + 1])) return null;
    }
    var emphasis = element("em");
    renderInline(emphasis, inner, seatsByHandle);
    return { node: emphasis, end: close + 1 };
  }

  function readLink(text, index) {
    var textEnd = text.indexOf("](", index + 1);
    if (textEnd === -1) return null;
    var urlEnd = text.indexOf(")", textEnd + 2);
    if (urlEnd === -1) return null;
    var label = text.slice(index + 1, textEnd);
    var url = text.slice(textEnd + 2, urlEnd).trim();
    if (label === "" || url === "" || /\s/.test(url)) return null;
    // Unsafe schemes (javascript:, data:) fall through and render as plain text.
    if (!SAFE_LINK_PATTERN.test(url)) return null;
    var anchor = element("a");
    anchor.setAttribute("href", url);
    anchor.setAttribute("target", "_blank");
    anchor.setAttribute("rel", "noopener noreferrer");
    anchor.textContent = label;
    return { node: anchor, end: urlEnd + 1 };
  }

  function readSpanAt(text, index, seatsByHandle) {
    var character = text[index];
    if (character === "`") return readCodeSpan(text, index);
    if (text.startsWith("**", index)) return readStrong(text, index, seatsByHandle);
    if (character === "*" || character === "_") return readEmphasis(text, index, seatsByHandle);
    if (character === "[") return readLink(text, index);
    return null;
  }

  function renderInline(parent, text, seatsByHandle) {
    var plain = "";
    var index = 0;
    function flushPlain() {
      if (plain !== "") {
        appendTextWithMentions(parent, plain, seatsByHandle);
        plain = "";
      }
    }
    while (index < text.length) {
      var span = readSpanAt(text, index, seatsByHandle);
      if (span === null) {
        plain += text[index];
        index += 1;
        continue;
      }
      flushPlain();
      parent.appendChild(span.node);
      index = span.end;
    }
    flushPlain();
  }

  // ---------- tables ----------

  function hasUnescapedPipe(line) {
    for (var index = 0; index < line.length; index += 1) {
      if (line[index] === "|" && line[index - 1] !== "\\") return true;
    }
    return false;
  }

  // Splits at unescaped pipes, ignoring the optional outer pipes; "\|" becomes "|".
  function splitCells(line) {
    var row = line.trim();
    if (row.startsWith("|")) row = row.slice(1);
    if (row.endsWith("|") && !row.endsWith("\\|")) row = row.slice(0, -1);
    var cells = [];
    var current = "";
    for (var index = 0; index < row.length; index += 1) {
      var character = row[index];
      if (character === "\\" && row[index + 1] === "|") {
        current += "|";
        index += 1;
      } else if (character === "|") {
        cells.push(current.trim());
        current = "";
      } else {
        current += character;
      }
    }
    cells.push(current.trim());
    return cells;
  }

  function alignmentOf(separatorCell) {
    var colonAtStart = separatorCell[0] === ":";
    var colonAtEnd = separatorCell[separatorCell.length - 1] === ":";
    if (colonAtStart && colonAtEnd) return "center";
    if (colonAtEnd) return "right";
    return "left";
  }

  // Returns {node, nextLineIndex} when a table starts at startIndex, else null.
  function readTableAt(lines, startIndex, seatsByHandle) {
    var headerLine = lines[startIndex];
    var separatorLine = lines[startIndex + 1];
    if (separatorLine === undefined) return null;
    if (!hasUnescapedPipe(headerLine) || !hasUnescapedPipe(separatorLine)) return null;
    var headerCells = splitCells(headerLine);
    var separatorCells = splitCells(separatorLine);
    var separatorIsValid = separatorCells.every(function (cell) {
      return TABLE_SEPARATOR_CELL.test(cell);
    });
    if (!separatorIsValid || separatorCells.length !== headerCells.length) return null;
    var alignments = separatorCells.map(alignmentOf);

    var index = startIndex + 2;
    var bodyRows = [];
    while (index < lines.length && lines[index].trim() !== "" && hasUnescapedPipe(lines[index])) {
      bodyRows.push(splitCells(lines[index]));
      index += 1;
    }

    var table = element("table", "md-table");
    table.setAttribute("data-source", lines.slice(startIndex, index).join("\n"));
    function fillRow(rowElement, cellTag, cells) {
      // Pad short rows and trim long ones to the header's width, as GitHub does.
      headerCells.forEach(function (_header, column) {
        var cell = element(cellTag);
        cell.style.textAlign = alignments[column];
        renderInline(cell, cells[column] || "", seatsByHandle);
        rowElement.appendChild(cell);
      });
    }
    var thead = element("thead");
    var headerRow = element("tr");
    fillRow(headerRow, "th", headerCells);
    thead.appendChild(headerRow);
    table.appendChild(thead);
    var tbody = element("tbody");
    bodyRows.forEach(function (cells) {
      var bodyRow = element("tr");
      fillRow(bodyRow, "td", cells);
      tbody.appendChild(bodyRow);
    });
    table.appendChild(tbody);
    return { node: table, nextLineIndex: index };
  }

  // ---------- code blocks ----------

  function buildCodeBlock(code, language) {
    var pre = element("pre", "md-code");
    var codeElement = element("code");
    if (language) {
      pre.setAttribute("data-language", language);
      codeElement.className = "language-" + language;
    }
    codeElement.textContent = code;
    var button = element("button", "btn md-copy");
    button.type = "button";
    button.textContent = "Copy";
    button.addEventListener("click", function () {
      copyText(code).then(function (copied) {
        button.textContent = copied ? "Copied" : "Copy failed";
        setTimeout(function () {
          button.textContent = "Copy";
        }, 1500);
      });
    });
    pre.appendChild(codeElement);
    pre.appendChild(button);
    return pre;
  }

  // ---------- blocks ----------

  function renderBlocks(text, seatsByHandle) {
    var fragment = document.createDocumentFragment();
    var lines = String(text).replace(/\r\n?/g, "\n").split("\n");
    var paragraphLines = [];
    var listElement = null;
    var listKind = null;

    function flushParagraph() {
      if (paragraphLines.length === 0) return;
      var paragraph = element("p", "md-p");
      renderInline(paragraph, paragraphLines.join(" "), seatsByHandle);
      fragment.appendChild(paragraph);
      paragraphLines = [];
    }
    function flushList() {
      listElement = null;
      listKind = null;
    }
    function flushAll() {
      flushParagraph();
      flushList();
    }
    function addListItem(kind, content, startNumber) {
      flushParagraph();
      if (listKind !== kind) {
        flushList();
        listElement = element(kind === "bullet" ? "ul" : "ol", "md-list");
        if (kind === "ordered" && startNumber !== 1) {
          listElement.setAttribute("start", String(startNumber));
        }
        listKind = kind;
        fragment.appendChild(listElement);
      }
      var item = element("li");
      renderInline(item, content.trim(), seatsByHandle);
      listElement.appendChild(item);
    }

    var index = 0;
    while (index < lines.length) {
      var line = lines[index];

      var fence = FENCE_PATTERN.exec(line);
      if (fence) {
        flushAll();
        var fenceText = fence[1];
        var codeLines = [];
        index += 1;
        // The closing fence repeats the opening character at least as many times.
        var closePattern = new RegExp("^\\s*" + fenceText[0] + "{" + fenceText.length + ",}\\s*$");
        while (index < lines.length && !closePattern.test(lines[index])) {
          codeLines.push(lines[index]);
          index += 1;
        }
        fragment.appendChild(buildCodeBlock(codeLines.join("\n"), fence[2]));
        index += 1; // skip the closing fence; an unterminated fence ran to the end
        continue;
      }

      var table = readTableAt(lines, index, seatsByHandle);
      if (table) {
        flushAll();
        fragment.appendChild(table.node);
        index = table.nextLineIndex;
        continue;
      }

      if (line.trim() === "") {
        flushAll();
      } else if (HEADING_PATTERN.test(line)) {
        flushAll();
        var heading = HEADING_PATTERN.exec(line);
        var headingElement = element("h3", "md-h md-h" + heading[1].length);
        renderInline(headingElement, heading[2].trim(), seatsByHandle);
        fragment.appendChild(headingElement);
      } else if (RULE_PATTERN.test(line)) {
        flushAll();
        fragment.appendChild(element("hr", "md-rule"));
      } else if (BULLET_PATTERN.test(line)) {
        addListItem("bullet", BULLET_PATTERN.exec(line)[1], 1);
      } else if (ORDERED_PATTERN.test(line)) {
        var ordered = ORDERED_PATTERN.exec(line);
        addListItem("ordered", ordered[2], parseInt(ordered[1], 10));
      } else {
        flushList();
        paragraphLines.push(line.trim());
      }
      index += 1;
    }
    flushAll();
    return fragment;
  }

  function renderMarkdown(text, seatsByHandle) {
    var rawText = text === null || text === undefined ? "" : String(text);
    try {
      return renderBlocks(rawText, seatsByHandle || {});
    } catch (error) {
      console.error("AddaMarkdown: render failed, showing raw text", error);
      var fallback = document.createDocumentFragment();
      var paragraph = element("p", "md-p");
      paragraph.textContent = rawText;
      fallback.appendChild(paragraph);
      return fallback;
    }
  }

  // ---------- copy ----------

  function copyWithHiddenTextarea(text) {
    var textarea = document.createElement("textarea");
    textarea.value = text;
    textarea.setAttribute("readonly", "");
    textarea.style.position = "fixed";
    textarea.style.top = "-1000px";
    document.body.appendChild(textarea);
    textarea.select();
    var copied = false;
    try {
      copied = document.execCommand("copy");
    } catch (error) {
      console.error("AddaMarkdown: execCommand copy failed", error);
    }
    document.body.removeChild(textarea);
    return copied;
  }

  function copyText(text) {
    var value = String(text);
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(value).then(
        function () {
          return true;
        },
        function () {
          // Permission denied or insecure context: try the older route.
          return copyWithHiddenTextarea(value);
        }
      );
    }
    return Promise.resolve(copyWithHiddenTextarea(value));
  }

  // ---------- transcript ----------

  function formatClock(createdAt) {
    var date = new Date(createdAt);
    if (isNaN(date.getTime())) return "";
    var hours = String(date.getHours()).padStart(2, "0");
    var minutes = String(date.getMinutes()).padStart(2, "0");
    return hours + ":" + minutes;
  }

  function toMarkdown(messages, owner) {
    return (messages || [])
      .map(function (message) {
        var author = message.role === "owner" && owner ? owner : message.author;
        var clock = formatClock(message.created_at);
        var heading = "**" + author + "**" + (clock ? " · " + clock : "");
        return heading + "\n" + message.text;
      })
      .join("\n\n");
  }

  window.AddaMarkdown = {
    renderMarkdown: renderMarkdown,
    copyText: copyText,
    toMarkdown: toMarkdown,
  };
})();
