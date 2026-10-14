/*
 * Helpers shared by the rooms, room and settings pages. Plain script, no modules, loaded
 * before the page's own file, so everything here is a global function.
 *
 * Server text reaches the DOM through textContent only; nothing here uses innerHTML.
 */

'use strict';

const FALLBACK_SEAT_COLOR = '#888888';
const RECONNECT_BASE_MS = 1000;
const RECONNECT_MAX_MS = 30000;
const RECONNECT_JITTER_FRACTION = 0.25;
const TOAST_DURATION_MS = 4000;
const THINKING_LEVELS = ['default', 'low', 'medium', 'high'];
// Distinct from each other and readable on both grounds; used when a new seat has no colour.
const SEAT_COLOR_PALETTE = ['#CF5A26', '#2A8F5E', '#D6409F', '#3E7FD6', '#8A6BD1', '#B08900'];

// ---------------------------------------------------------------------------
// DOM
// ---------------------------------------------------------------------------

function createElement(tagName, className, text) {
  const element = document.createElement(tagName);
  if (className) {
    element.className = className;
  }
  if (text !== undefined) {
    element.textContent = text;
  }
  return element;
}

function createButton(label, className, onClick) {
  const button = createElement('button', className, label);
  button.type = 'button';
  button.addEventListener('click', onClick);
  return button;
}

// ---------------------------------------------------------------------------
// Network
// ---------------------------------------------------------------------------

/** GET or send JSON. Throws an Error that carries the server's `detail` text. */
async function fetchJson(path, options) {
  const response = await fetch(path, options);
  if (!response.ok) {
    let detail = await response.text();
    try {
      detail = JSON.parse(detail).detail || detail;
    } catch (parseError) {
      // The body was not JSON; the raw text is the best detail there is.
    }
    throw new Error(`${path}: ${detail} (HTTP ${response.status})`);
  }
  return response.json();
}

function sendJson(method, path, body) {
  return fetchJson(path, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

/** Exponential backoff, 1 s doubling to 30 s, plus up to 25 % random jitter. */
function computeBackoffMs(attempt) {
  const exponentialMs = Math.min(RECONNECT_MAX_MS, RECONNECT_BASE_MS * 2 ** attempt);
  return exponentialMs + Math.random() * exponentialMs * RECONNECT_JITTER_FRACTION;
}

/**
 * WebSocket that reconnects by itself.
 *   onFrame(frame)            every parsed server frame
 *   onStatus({state, retryInSeconds})   state: connecting | open | reconnecting | closed
 * The server replays a `hello` frame on every connection, so a reconnect needs no catch-up logic.
 */
function createSocketClient({ url, onFrame, onStatus }) {
  let socket = null;
  let attempt = 0;
  let retryTimerId = null;
  let countdownTimerId = null;
  let closedOnPurpose = false;

  function clearTimers() {
    clearTimeout(retryTimerId);
    clearInterval(countdownTimerId);
    retryTimerId = null;
    countdownTimerId = null;
  }

  function scheduleReconnect() {
    clearTimers();
    const delayMs = computeBackoffMs(attempt);
    attempt += 1;
    const retryAtMs = Date.now() + delayMs;
    const announce = () => {
      const secondsLeft = Math.max(0, Math.ceil((retryAtMs - Date.now()) / 1000));
      onStatus({ state: 'reconnecting', retryInSeconds: secondsLeft });
    };
    announce();
    countdownTimerId = setInterval(announce, 1000);
    retryTimerId = setTimeout(connect, delayMs);
  }

  function connect() {
    clearTimers();
    closedOnPurpose = false;
    onStatus({ state: 'connecting', retryInSeconds: 0 });
    const scheme = location.protocol === 'https:' ? 'wss://' : 'ws://';
    const thisSocket = new WebSocket(scheme + location.host + url);
    socket = thisSocket;

    // Handlers compare against `socket` so events from a socket we already replaced are ignored.
    thisSocket.addEventListener('open', () => {
      if (thisSocket !== socket) return;
      attempt = 0;
      onStatus({ state: 'open', retryInSeconds: 0 });
    });
    thisSocket.addEventListener('message', (event) => {
      if (thisSocket !== socket) return;
      try {
        onFrame(JSON.parse(event.data));
      } catch (error) {
        console.error('Could not handle a frame', event.data, error);
      }
    });
    thisSocket.addEventListener('close', () => {
      if (thisSocket !== socket) return;
      socket = null;
      if (closedOnPurpose) {
        onStatus({ state: 'closed', retryInSeconds: 0 });
      } else {
        scheduleReconnect();
      }
    });
  }

  function isOpen() {
    return Boolean(socket && socket.readyState === WebSocket.OPEN);
  }

  function send(frame) {
    if (!isOpen()) {
      return false;
    }
    socket.send(JSON.stringify(frame));
    return true;
  }

  function close() {
    closedOnPurpose = true;
    clearTimers();
    if (socket) {
      socket.close();
    }
  }

  /** Browsers throttle timers in hidden tabs, so a pending backoff may be long overdue. */
  function reconnectNow() {
    if (isOpen() || (socket && socket.readyState === WebSocket.CONNECTING)) {
      return;
    }
    attempt = 0;
    connect();
  }

  return { connect, send, close, isOpen, reconnectNow };
}

// ---------------------------------------------------------------------------
// Colour
// ---------------------------------------------------------------------------

/** Server colours go into a CSS custom property; accept only hex so nothing else slips in. */
function safeColor(color) {
  if (typeof color === 'string' && /^#[0-9a-f]{3,8}$/i.test(color)) {
    return color;
  }
  return FALLBACK_SEAT_COLOR;
}

/**
 * Give an element a seat colour. style.css derives --seat-text (the colour pulled toward the
 * ink so small text reaches contrast) and --seat-field (a faint tint) from --seat, so no
 * contrast maths is needed in script.
 */
function setSeatColor(element, color) {
  element.style.setProperty('--seat', safeColor(color));
}

function paletteColor(index) {
  return SEAT_COLOR_PALETTE[index % SEAT_COLOR_PALETTE.length];
}

// ---------------------------------------------------------------------------
// Formatting
// ---------------------------------------------------------------------------

function slugify(text) {
  return String(text)
    .toLowerCase()
    .normalize('NFKD')
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 48);
}

function formatMoney(amount) {
  const value = Number(amount) || 0;
  // Replies cost fractions of a cent; show enough digits that they do not read as $0.00.
  return '$' + value.toFixed(value !== 0 && Math.abs(value) < 0.1 ? 3 : 2);
}

function formatDuration(milliseconds) {
  if (milliseconds < 1000) {
    return Math.round(milliseconds) + ' ms';
  }
  const seconds = milliseconds / 1000;
  if (seconds < 60) {
    return seconds.toFixed(1) + ' s';
  }
  return Math.floor(seconds / 60) + ' min ' + Math.round(seconds % 60) + ' s';
}

const clockFormatter = new Intl.DateTimeFormat(undefined, {
  hour: '2-digit',
  minute: '2-digit',
  hourCycle: 'h23',
});
const dayFormatter = new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric' });

function formatClock(isoText) {
  return clockFormatter.format(new Date(isoText));
}

function formatDay(isoText) {
  return dayFormatter.format(new Date(isoText));
}

/**
 * Threshold rule of SPEC section 5: a seat speaks when its bid reaches
 * base + ramp * k, base = 9.5 - 0.65 L, ramp = 0.6 - 0.05 L, k = automatic turns since the
 * last owner message or wake.
 */
function computeLivelinessThreshold(liveliness, automaticTurns) {
  const base = 9.5 - 0.65 * liveliness;
  const ramp = 0.6 - 0.05 * liveliness;
  return { base, ramp, current: base + ramp * automaticTurns };
}

function describeLiveliness(liveliness, automaticTurns) {
  if (liveliness === 0) {
    return 'Paused. Seats speak only when mentioned or woken.';
  }
  const { base, ramp, current } = computeLivelinessThreshold(liveliness, automaticTurns);
  const rule = `A seat speaks at a bid of ${base.toFixed(1)} or more, rising ${ramp.toFixed(2)} per automatic turn.`;
  if (automaticTurns > 0 && current > 10) {
    // Bids top out at 10, so past this point only you, a mention or Wake can restart the room.
    return `${rule} After ${automaticTurns} turns no bid can pass; the room waits for you.`;
  }
  if (automaticTurns > 0) {
    return `${rule} After ${automaticTurns} it is ${current.toFixed(1)}.`;
  }
  return rule;
}

// ---------------------------------------------------------------------------
// Toast
// ---------------------------------------------------------------------------

function showToast(text, isError = false) {
  let container = document.getElementById('toasts');
  if (!container) {
    container = createElement('div', 'toasts');
    container.id = 'toasts';
    container.setAttribute('role', 'status');
    document.body.append(container);
  }
  const toast = createElement('div', isError ? 'toast toast--error' : 'toast', text);
  container.append(toast);
  setTimeout(() => toast.remove(), TOAST_DURATION_MS);
}

/** Log with context and tell the owner; never swallow a failure. */
function reportError(context, error) {
  console.error(context, error);
  showToast(`${context}: ${error instanceof Error ? error.message : error}`, true);
}

// ---------------------------------------------------------------------------
// Seat editor fields shared by the new-room table and the room panel
// ---------------------------------------------------------------------------

function findHarness(harnesses, program) {
  return harnesses.find((harness) => harness.program === program) || null;
}

/** Only harnesses found on this machine can be chosen; the others stay visible but disabled. */
function fillHarnessSelect(select, harnesses, selectedProgram) {
  select.replaceChildren();
  for (const harness of harnesses) {
    const label = harness.found ? `${harness.program} ${harness.version || ''}`.trim() : `${harness.program} · not installed`;
    const option = createElement('option', '', label);
    option.value = harness.program;
    option.disabled = !harness.found;
    select.append(option);
  }
  select.value = selectedProgram;
}

function fillDatalist(datalist, harness) {
  datalist.replaceChildren();
  for (const suggestion of harness ? harness.model_suggestions : []) {
    const option = document.createElement('option');
    option.value = suggestion;
    datalist.append(option);
  }
}

function fillThinkingSelect(select, harness, selectedThinking) {
  const supported = Boolean(harness && harness.supports_thinking);
  select.replaceChildren();
  for (const level of supported ? THINKING_LEVELS : ['default']) {
    const label = level === 'default' ? (supported ? 'harness default' : 'not supported') : level;
    const option = createElement('option', '', label);
    option.value = level;
    select.append(option);
  }
  select.disabled = !supported;
  select.value = supported && THINKING_LEVELS.includes(selectedThinking) ? selectedThinking : 'default';
}

function fillPersonaSelect(select, personas, selectedSlug) {
  select.replaceChildren();
  const noneOption = createElement('option', '', 'none');
  noneOption.value = '';
  select.append(noneOption);
  for (const persona of personas) {
    const option = createElement('option', '', `${persona.name} · ${persona.role}`);
    option.value = persona.slug;
    select.append(option);
  }
  select.value = selectedSlug || '';
}

/** A labelled textarea in a card, used for the custom instruction of a seat. */
function createInstructionCard(initialText, onInput, onChange) {
  const card = createElement('div', 'instruction-card');
  const textarea = document.createElement('textarea');
  textarea.rows = 3;
  textarea.placeholder = 'Custom instruction for this seat, added to its persona if it has one.';
  textarea.value = initialText || '';
  textarea.setAttribute('aria-label', 'Custom instruction');
  if (onInput) textarea.addEventListener('input', () => onInput(textarea.value));
  if (onChange) textarea.addEventListener('change', () => onChange(textarea.value));
  card.append(textarea);
  return { card, textarea };
}

function renderHarnessList(container, harnesses) {
  container.replaceChildren();
  for (const harness of harnesses) {
    const row = createElement('tr');
    const name = harness.found ? `${harness.program} ${harness.version || ''}`.trim() : harness.program;
    const status = createElement('span', harness.found ? 'ok' : 'bad', harness.found ? harness.login_hint : 'not installed');
    const statusCell = createElement('td');
    statusCell.append(status);
    row.append(createElement('td', '', name), statusCell);
    container.append(row);
  }
}
