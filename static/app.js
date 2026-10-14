/*
 * Adda front end. Vanilla JS, no build step, loaded with `defer`.
 *
 * Regions of this file, top to bottom:
 *   1. Constants and URL helpers
 *   2. State and element lookup
 *   3. Formatting helpers (time, cost, latency, colour)
 *   4. Network: fetchJson, REST loaders, WebSocket with reconnect
 *   5. Frame handlers (one per server frame type, SPEC section 6)
 *   6. Render functions (one per region of the page)
 *   7. Message body rendering (paragraphs, lists, code, @mentions)
 *   8. Composer, @ autocomplete, search, drawers, keyboard
 *   9. Startup
 *
 * All server text reaches the DOM through textContent or text nodes. Nothing here uses
 * innerHTML, so a message can never inject markup.
 */

'use strict';

// ---------------------------------------------------------------------------
// 1. Constants and URL helpers
// ---------------------------------------------------------------------------

const pageParameters = new URLSearchParams(location.search);
const API_BASE = pageParameters.get('api') || '';
const WS_BASE =
  pageParameters.get('ws') ||
  (location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host;

// Mirrors the server default ADDA_COOLDOWN_S. The server does not send cooldowns, so the
// client estimates them from the time a seat message arrived.
const COOLDOWN_SECONDS = 20;
// Mirrors the server default ADDA_BID_THRESHOLD; used for the meter marker and quiet notice.
const BID_THRESHOLD = 5.0;
const MAX_BID_SCORE = 10;

const RECONNECT_BASE_MS = 1000;
const RECONNECT_MAX_MS = 30000;
const RECONNECT_JITTER_FRACTION = 0.25;

const NEAR_BOTTOM_PIXELS = 80;
const CONTINUED_MESSAGE_WINDOW_MS = 5 * 60 * 1000;
const HIGHLIGHT_DURATION_MS = 2500;
const BID_ANIMATION_MS = 900;
const SEARCH_DEBOUNCE_MS = 250;
const COMPOSER_MAX_VIEWPORT_FRACTION = 0.4;
const FALLBACK_SEAT_COLOR = '#888888';

const MODE_LABELS = {
  bidding: 'Bidding',
  roundrobin: 'Round-robin',
  manual: 'Manual',
};

const ENDED_HINT = 'This discussion has ended.';
const DISCONNECTED_HINT = "Not connected. Messages can't be sent.";

/** Keep ?api= and ?ws= when the page changes thread, so a dev override survives navigation. */
function buildThreadUrl(threadId, hash = '') {
  const parameters = new URLSearchParams();
  for (const preservedName of ['api', 'ws']) {
    const value = pageParameters.get(preservedName);
    if (value) {
      parameters.set(preservedName, value);
    }
  }
  parameters.set('thread', threadId);
  return '?' + parameters.toString() + hash;
}

// ---------------------------------------------------------------------------
// 2. State and element lookup
// ---------------------------------------------------------------------------

const state = {
  threads: [],
  currentThreadId: null,
  thread: null,
  seats: [],
  messages: [],
  typingSeats: new Set(),
  lastBids: {}, // seat name -> {score, confidence}
  chosenSeat: null,
  router: null, // "jev" | "heuristic" | null before the first bids frame
  cooldownEndsAt: {}, // seat name -> epoch ms
  connection: { state: 'closed', retryInSeconds: 0 },
  ownerScrolledUp: false,
  unseenCount: 0,
  mentionMenu: { open: false, items: [], activeIndex: 0, tokenStart: 0 },
  // Additions beyond the minimum: socket bookkeeping and small UI flags.
  socket: null,
  reconnectAttempt: 0,
  reconnectTimerId: null,
  countdownTimerId: null,
  quietNotice: false,
  endRequested: false,
  pendingHighlightMessageId: null,
  localMessageCounter: 0,
};

const elements = {
  threadList: document.getElementById('thread-list'),
  newThreadForm: document.getElementById('new-thread-form'),
  newThreadTitle: document.getElementById('new-thread-title'),
  roomTitle: document.getElementById('room-title'),
  roomMeta: document.getElementById('room-meta'),
  connectionState: document.getElementById('connection-state'),
  messageScroller: document.getElementById('message-scroller'),
  messageList: document.getElementById('message-list'),
  jumpLatest: document.getElementById('jump-latest'),
  typingLine: document.getElementById('typing-line'),
  seatStrip: document.getElementById('seat-strip'),
  seatList: document.getElementById('seat-list'),
  routerName: document.getElementById('router-name'),
  modeSwitch: document.getElementById('mode-switch'),
  composer: document.getElementById('composer'),
  composerInput: document.getElementById('composer-input'),
  composerHint: document.getElementById('composer-hint'),
  sendButton: document.getElementById('send-button'),
  stopButton: document.getElementById('stop-button'),
  endButton: document.getElementById('end-button'),
  mentionMenu: document.getElementById('mention-menu'),
  searchForm: document.getElementById('search-form'),
  searchInput: document.getElementById('search-input'),
  searchResults: document.getElementById('search-results'),
  toggleThreads: document.getElementById('toggle-threads'),
  toggleSeats: document.getElementById('toggle-seats'),
  drawerScrim: document.getElementById('drawer-scrim'),
  endedDialog: document.getElementById('ended-dialog'),
  endedNotes: document.getElementById('ended-notes'),
};

const defaultComposerHint = elements.composerHint.textContent;

// Per-seat DOM references, created once per seat list so updates keep CSS transitions alive.
let seatCardReferences = {};
let seatChipReferences = {};
let cooldownIntervalId = null;
let searchDebounceTimerId = null;
let searchRequestCounter = 0;

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

// ---------------------------------------------------------------------------
// 3. Formatting helpers
// ---------------------------------------------------------------------------

const messageTimeFormatter = new Intl.DateTimeFormat(undefined, {
  hour: '2-digit',
  minute: '2-digit',
  hourCycle: 'h23',
});

const threadDateFormatter = new Intl.DateTimeFormat(undefined, {
  month: 'short',
  day: 'numeric',
  hour: '2-digit',
  minute: '2-digit',
  hourCycle: 'h23',
});

const searchDateFormatter = new Intl.DateTimeFormat(undefined, {
  month: 'short',
  day: 'numeric',
});

function formatCost(costUsd) {
  return '$' + costUsd.toFixed(3);
}

function formatLatency(latencyMs) {
  if (latencyMs < 1000) {
    return Math.round(latencyMs) + ' ms';
  }
  return (latencyMs / 1000).toFixed(1) + ' s';
}

/** Server colours go into a CSS custom property; accept only hex so nothing else slips in. */
function safeColor(color) {
  if (typeof color === 'string' && /^#[0-9a-f]{3,8}$/i.test(color)) {
    return color;
  }
  return FALLBACK_SEAT_COLOR;
}

function seatByName(seatName) {
  return state.seats.find((seat) => seat.name === seatName) || null;
}

function seatColorFor(seatName) {
  const seat = seatByName(seatName);
  return seat ? safeColor(seat.color) : FALLBACK_SEAT_COLOR;
}

function isThreadEnded() {
  return Boolean(state.thread && state.thread.ended_at);
}

function isSocketOpen() {
  return Boolean(state.socket && state.socket.readyState === WebSocket.OPEN);
}

function prefersReducedMotion() {
  return window.matchMedia('(prefers-reduced-motion: reduce)').matches;
}

// ---------------------------------------------------------------------------
// 4. Network
// ---------------------------------------------------------------------------

async function fetchJson(path, options) {
  const response = await fetch(API_BASE + path, options);
  if (!response.ok) {
    const bodyText = await response.text();
    throw new Error(`HTTP ${response.status} for ${path}: ${bodyText}`);
  }
  return response.json();
}

/** Show a failure in the room and in the console; never swallow it. */
function reportError(context, error) {
  console.error(context, error);
  const detail = error instanceof Error ? error.message : String(error);
  addLocalMessage(`${context}: ${detail}`, true);
}

async function loadThreads() {
  state.threads = await fetchJson('/api/threads');
  renderThreadList();
}

/** Seats are global, not per thread, so the panel is filled before any thread is opened. */
async function loadSeats() {
  state.seats = await fetchJson('/api/seats');
  renderSeats();
}

async function createThread(title) {
  const thread = await fetchJson('/api/threads', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ title }),
  });
  state.threads.unshift(thread);
  renderThreadList();
  openThread(thread.id, { pushHistory: true });
}

function sendFrame(frame) {
  if (!isSocketOpen()) {
    reportError('Cannot send', new Error('the connection is not open'));
    return false;
  }
  state.socket.send(JSON.stringify(frame));
  return true;
}

function clearReconnectTimers() {
  clearTimeout(state.reconnectTimerId);
  clearInterval(state.countdownTimerId);
  state.reconnectTimerId = null;
  state.countdownTimerId = null;
}

function setConnectionState(connectionState, retryInSeconds = 0) {
  state.connection = { state: connectionState, retryInSeconds };
  renderConnectionState();
  renderComposerState();
  renderSeats();
}

/** Close the current socket on purpose; its close event must not trigger a reconnect. */
function closeSocketOnPurpose() {
  clearReconnectTimers();
  const oldSocket = state.socket;
  state.socket = null;
  if (oldSocket) {
    oldSocket.close();
  }
}

function connectSocket() {
  clearReconnectTimers();
  if (!state.currentThreadId) {
    return;
  }
  setConnectionState('connecting');

  let socket;
  try {
    socket = new WebSocket(`${WS_BASE}/ws/${encodeURIComponent(state.currentThreadId)}`);
  } catch (error) {
    console.error('WebSocket construction failed', error);
    scheduleReconnect();
    return;
  }
  state.socket = socket;

  // Every handler compares against state.socket so events from a socket we replaced are ignored.
  socket.addEventListener('open', () => {
    if (socket !== state.socket) {
      return;
    }
    state.reconnectAttempt = 0;
    setConnectionState('open');
  });
  socket.addEventListener('message', (event) => {
    if (socket !== state.socket) {
      return;
    }
    handleRawFrame(event.data);
  });
  socket.addEventListener('error', (event) => {
    if (socket === state.socket) {
      console.error('WebSocket error', event);
    }
  });
  socket.addEventListener('close', () => {
    if (socket !== state.socket) {
      return;
    }
    state.socket = null;
    // Nothing is typing on a dead connection; the next hello restores the truth.
    state.typingSeats.clear();
    renderTypingLine();
    scheduleReconnect();
  });
}

/** Exponential backoff, 1 s doubling to 30 s, plus up to 25 % random jitter. */
function computeBackoffMs(attempt) {
  const exponentialMs = Math.min(RECONNECT_MAX_MS, RECONNECT_BASE_MS * 2 ** attempt);
  const jitterMs = Math.random() * exponentialMs * RECONNECT_JITTER_FRACTION;
  return exponentialMs + jitterMs;
}

function scheduleReconnect() {
  clearReconnectTimers();
  const delayMs = computeBackoffMs(state.reconnectAttempt);
  state.reconnectAttempt += 1;
  const retryAtMs = Date.now() + delayMs;

  const updateCountdown = () => {
    const secondsLeft = Math.max(0, Math.ceil((retryAtMs - Date.now()) / 1000));
    setConnectionState('reconnecting', secondsLeft);
  };
  updateCountdown();
  state.countdownTimerId = setInterval(updateCountdown, 1000);
  state.reconnectTimerId = setTimeout(connectSocket, delayMs);
}

function reconnectNow() {
  clearReconnectTimers();
  state.reconnectAttempt = 0;
  connectSocket();
}

// ---------------------------------------------------------------------------
// 5. Frame handlers
// ---------------------------------------------------------------------------

function handleRawFrame(rawText) {
  let frame;
  try {
    frame = JSON.parse(rawText);
  } catch (error) {
    reportError('Received an unreadable frame', error);
    return;
  }

  const wasQuiet = state.quietNotice;
  if (frame.type !== 'quiet') {
    state.quietNotice = false;
  }

  switch (frame.type) {
    case 'hello':
      handleHello(frame);
      break;
    case 'message':
      handleMessageFrame(frame.message);
      break;
    case 'typing':
      handleTypingFrame(frame);
      break;
    case 'bids':
      handleBidsFrame(frame);
      break;
    case 'mode':
      handleModeFrame(frame);
      break;
    case 'quiet':
      state.quietNotice = true;
      renderTypingLine();
      break;
    case 'ended':
      handleEndedFrame(frame);
      break;
    case 'error':
      state.endRequested = false;
      reportError('Server error', new Error(frame.text));
      renderComposerState();
      break;
    default:
      console.warn('Unknown frame type', frame);
  }

  if (wasQuiet && !state.quietNotice) {
    renderTypingLine();
  }
}

function handleHello(frame) {
  const isFirstHelloForThread = state.thread === null;
  state.thread = frame.thread;
  state.seats = frame.seats;
  state.messages = frame.messages;
  state.typingSeats.clear();
  state.endRequested = false;
  if (isFirstHelloForThread) {
    state.ownerScrolledUp = false;
  }

  replaceThreadInList(frame.thread);
  renderThreadList();
  renderRoomHeader();
  renderMessages();
  renderTypingLine();
  renderSeats();
  renderModeSwitch();
  renderComposerState();

  const highlightId = state.pendingHighlightMessageId;
  state.pendingHighlightMessageId = null;
  if (highlightId && scrollToMessage(highlightId)) {
    return;
  }
  // On a reconnect the owner may be reading older messages; do not yank them down.
  if (!state.ownerScrolledUp) {
    scrollMessagesToBottom(false);
    hideJumpLatest();
  }
}

function handleMessageFrame(message) {
  if (state.messages.some((existing) => existing.id === message.id)) {
    return;
  }
  if (message.role === 'seat') {
    state.cooldownEndsAt[message.author] = Date.now() + COOLDOWN_SECONDS * 1000;
    ensureCooldownTicker();
  }
  state.chosenSeat = null;
  state.messages.push(message);
  appendMessageElement(message);
  renderRoomHeader();
  renderSeats();

  if (message.role === 'owner') {
    state.ownerScrolledUp = false;
  }
  if (state.ownerScrolledUp) {
    state.unseenCount += 1;
    showJumpLatest();
  } else {
    scrollMessagesToBottom(!prefersReducedMotion());
  }
}

function handleTypingFrame(frame) {
  if (frame.state === 'start') {
    state.typingSeats.add(frame.seat);
    if (state.chosenSeat === frame.seat) {
      state.chosenSeat = null;
    }
  } else {
    state.typingSeats.delete(frame.seat);
  }
  renderTypingLine();
  renderSeats();
  renderComposerState();
}

function handleBidsFrame(frame) {
  const bidsBySeat = {};
  for (const bid of frame.bids) {
    bidsBySeat[bid.seat] = { score: bid.score, confidence: bid.confidence };
  }
  state.lastBids = bidsBySeat;
  state.chosenSeat = frame.chosen;
  state.router = frame.router;
  renderSeats();
  renderModeSwitch();
  animateBidCards(Object.keys(bidsBySeat));
}

function handleModeFrame(frame) {
  if (state.thread) {
    state.thread.mode = frame.mode;
  }
  renderModeSwitch();
  renderRoomHeader();
}

async function handleEndedFrame(frame) {
  state.endRequested = false;
  if (state.thread && !state.thread.ended_at) {
    state.thread.ended_at = new Date().toISOString();
  }
  renderRoomHeader();
  renderComposerState();
  renderEndedNotes(frame.notes || {});
  elements.endedDialog.showModal();
  try {
    await loadThreads();
  } catch (error) {
    reportError('Could not refresh the thread list', error);
  }
}

function replaceThreadInList(thread) {
  const index = state.threads.findIndex((existing) => existing.id === thread.id);
  if (index >= 0) {
    state.threads[index] = thread;
  }
}

/** Local messages (errors, empty-state hints) are never sent by the server and are not persisted. */
function addLocalMessage(text, isError) {
  state.localMessageCounter += 1;
  const message = {
    id: `local-${state.localMessageCounter}`,
    role: 'system',
    author: 'system',
    text,
    created_at: new Date().toISOString(),
    mentions: [],
    cost_usd: null,
    meta: {},
    isLocal: true,
    isError,
  };
  state.messages.push(message);
  appendMessageElement(message);
  scrollMessagesToBottom(false);
}

// ---------------------------------------------------------------------------
// 6. Render functions
// ---------------------------------------------------------------------------

function renderThreadList() {
  elements.threadList.replaceChildren();
  for (const thread of state.threads) {
    const isEnded = Boolean(thread.ended_at);
    const isCurrent = thread.id === state.currentThreadId;

    const link = createElement('a', isEnded ? 'thread-link is-ended' : 'thread-link');
    link.href = buildThreadUrl(thread.id);
    if (isCurrent) {
      link.setAttribute('aria-current', 'page');
    }
    link.append(
      createElement('span', 'thread-title', thread.title),
      createElement('span', 'thread-date', threadDateFormatter.format(new Date(thread.created_at))),
    );
    if (isEnded) {
      link.append(createElement('span', 'thread-ended', 'Ended'));
    }
    link.addEventListener('click', (event) => {
      event.preventDefault();
      closeDrawers();
      openThread(thread.id, { pushHistory: true });
    });

    const item = document.createElement('li');
    item.append(link);
    elements.threadList.append(item);
  }
}

function renderRoomHeader() {
  if (!state.thread) {
    elements.roomTitle.textContent = 'Adda';
    elements.roomMeta.textContent = '';
    document.title = 'Adda';
    return;
  }
  const thread = state.thread;
  const persistedMessages = state.messages.filter((message) => !message.isLocal);
  const totalCost = persistedMessages.reduce(
    (sum, message) => sum + (message.cost_usd || 0),
    0,
  );
  const messageWord = persistedMessages.length === 1 ? 'message' : 'messages';

  let meta = [
    MODE_LABELS[thread.mode] || thread.mode,
    `${persistedMessages.length} ${messageWord}`,
    `${formatCost(totalCost)} spent`,
    `started ${messageTimeFormatter.format(new Date(thread.created_at))}`,
  ].join(' · ');
  if (thread.ended_at) {
    meta += ` · ended ${messageTimeFormatter.format(new Date(thread.ended_at))}`;
  }

  elements.roomTitle.textContent = thread.title;
  elements.roomMeta.textContent = meta;
  document.title = `${thread.title} · Adda`;
}

/** Full render of the message list; used on hello and when a thread is opened. */
function renderMessages() {
  elements.messageList.replaceChildren();
  let previousMessage = null;
  for (const message of state.messages) {
    elements.messageList.append(createMessageElement(message, previousMessage));
    previousMessage = message;
  }
}

/** Fast path: the new message is already the last entry of state.messages. */
function appendMessageElement(message) {
  const previousMessage = state.messages[state.messages.length - 2] || null;
  const item = createMessageElement(message, previousMessage);
  // Only live arrivals animate; a full render of history should appear at once.
  item.classList.add('message--arriving');
  elements.messageList.append(item);
}

function isContinuation(message, previousMessage) {
  if (!previousMessage || message.isLocal || previousMessage.isLocal) {
    return false;
  }
  if (message.role !== previousMessage.role || message.author !== previousMessage.author) {
    return false;
  }
  const gapMs = new Date(message.created_at) - new Date(previousMessage.created_at);
  return gapMs >= 0 && gapMs <= CONTINUED_MESSAGE_WINDOW_MS;
}

function authorLabelFor(message) {
  if (message.role === 'owner') {
    return 'You';
  }
  if (message.role === 'system') {
    return 'Adda';
  }
  return message.author;
}

function createMessageElement(message, previousMessage) {
  const classNames = ['message', `message--${message.role}`];
  if (message.isError) {
    classNames.push('message--error');
  }
  if (isContinuation(message, previousMessage)) {
    classNames.push('message--continued');
  }

  const item = createElement('li', classNames.join(' '));
  item.id = `m-${message.id}`;
  item.dataset.author = message.author;
  if (message.role === 'seat') {
    item.style.setProperty('--seat', seatColorFor(message.author));
  }

  const head = createElement('div', 'message-head');
  head.append(createElement('span', 'message-author', authorLabelFor(message)));

  const time = createElement('time', 'message-time', messageTimeFormatter.format(new Date(message.created_at)));
  time.dateTime = message.created_at;
  head.append(time);

  if (typeof message.cost_usd === 'number') {
    head.append(createElement('span', 'message-cost', formatCost(message.cost_usd)));
  }
  const meta = message.meta || {};
  if (typeof meta.latency_ms === 'number') {
    head.append(createElement('span', 'message-latency', formatLatency(meta.latency_ms)));
  }
  if (typeof meta.bid === 'number') {
    head.append(createElement('span', 'message-bid', `bid ${meta.bid.toFixed(1)}`));
  }

  item.append(head, renderMessageBody(message.text));
  return item;
}

function renderTypingLine() {
  elements.typingLine.replaceChildren();
  for (const seatName of state.typingSeats) {
    const typing = createElement('span', 'typing');
    typing.style.setProperty('--seat', seatColorFor(seatName));

    const dots = createElement('span', 'typing-dots');
    dots.setAttribute('aria-hidden', 'true');
    dots.append(document.createElement('i'), document.createElement('i'), document.createElement('i'));

    typing.append(dots, createElement('span', 'typing-name', seatName), document.createTextNode(' is thinking'));
    elements.typingLine.append(typing);
  }
  if (state.quietNotice && state.typingSeats.size === 0) {
    elements.typingLine.append(
      createElement(
        'span',
        'typing typing--quiet',
        // `quiet` also ends a round-robin pass or the auto-turn cap, so it does not always mean low bids.
        'The room went quiet. Your turn.',
      ),
    );
  }
}

function computeCooldownSecondsLeft(seatName) {
  const endsAt = state.cooldownEndsAt[seatName];
  if (!endsAt) {
    return 0;
  }
  return Math.max(0, Math.ceil((endsAt - Date.now()) / 1000));
}

function computeSeatState(seat) {
  if (!seat.available) {
    return 'unavailable';
  }
  if (state.typingSeats.has(seat.name)) {
    return 'speaking';
  }
  if (computeCooldownSecondsLeft(seat.name) > 0) {
    return 'cooldown';
  }
  if (state.chosenSeat === seat.name) {
    return 'chosen';
  }
  return 'idle';
}

function describeSeatState(seat, seatState) {
  switch (seatState) {
    case 'unavailable':
      return 'Not installed';
    case 'speaking':
      return 'Speaking';
    case 'cooldown':
      return `Cooling down ${computeCooldownSecondsLeft(seat.name)} s`;
    case 'chosen':
      return 'Chosen';
    default:
      break;
  }
  const hasBid = Boolean(state.lastBids[seat.name]);
  const isManual = state.thread && state.thread.mode === 'manual';
  return hasBid || isManual ? 'Idle' : 'Waiting';
}

function seatNamesMatchBuiltCards() {
  const builtNames = Object.keys(seatCardReferences);
  if (builtNames.length !== state.seats.length) {
    return false;
  }
  return state.seats.every((seat) => builtNames.includes(seat.name));
}

function buildSeatCards() {
  seatCardReferences = {};
  seatChipReferences = {};
  elements.seatList.replaceChildren();
  elements.seatStrip.replaceChildren();

  for (const seat of state.seats) {
    const card = createElement('li', 'seat-card');
    card.dataset.seat = seat.name;
    card.style.setProperty('--seat', safeColor(seat.color));
    card.style.setProperty('--threshold', String(BID_THRESHOLD / MAX_BID_SCORE));

    const head = createElement('div', 'seat-head');
    const swatch = createElement('span', 'seat-swatch');
    swatch.setAttribute('aria-hidden', 'true');
    // Without a model the program's own default is used; say so rather than show a bare name.
    const modelText = seat.model || 'default model';
    const programText = `${seat.program} · ${modelText}`;
    head.append(swatch, createElement('span', 'seat-name', seat.name), createElement('span', 'seat-program', programText));

    const role = createElement('p', 'seat-role', seat.role);

    const meter = createElement('div', 'seat-meter');
    meter.setAttribute('role', 'meter');
    meter.setAttribute('aria-label', `${seat.name}'s last bid`);
    meter.setAttribute('aria-valuemin', '0');
    meter.setAttribute('aria-valuemax', String(MAX_BID_SCORE));
    meter.setAttribute('aria-valuenow', '0');
    meter.append(createElement('span', 'seat-meter-fill'), createElement('span', 'seat-meter-threshold'));

    const scoreText = createElement('span', 'seat-score', '–');
    const statusText = createElement('span', 'seat-status');
    const wakeButton = createElement('button', 'button button--seat seat-wake', 'Wake');
    wakeButton.type = 'button';
    wakeButton.addEventListener('click', () => sendFrame({ type: 'wake', seat: seat.name }));
    const foot = createElement('div', 'seat-foot');
    foot.append(scoreText, statusText, wakeButton);

    card.append(head, role, meter, foot);
    elements.seatList.append(card);

    const chip = createElement('li', 'seat-chip');
    chip.dataset.seat = seat.name;
    chip.style.setProperty('--seat', safeColor(seat.color));
    const chipMeter = createElement('span', 'seat-chip-meter');
    chipMeter.setAttribute('aria-hidden', 'true');
    chipMeter.append(createElement('span', 'seat-chip-fill'));
    chip.append(createElement('span', 'seat-chip-name', seat.name), chipMeter);
    chip.addEventListener('click', () => toggleDrawer('seats'));
    elements.seatStrip.append(chip);

    seatCardReferences[seat.name] = { card, meter, scoreText, statusText, wakeButton };
    seatChipReferences[seat.name] = chip;
  }
}

function renderSeats() {
  if (!seatNamesMatchBuiltCards()) {
    buildSeatCards();
  }
  for (const seat of state.seats) {
    const references = seatCardReferences[seat.name];
    const seatState = computeSeatState(seat);
    const bid = state.lastBids[seat.name];
    const scoreFraction = bid ? bid.score / MAX_BID_SCORE : 0;

    references.card.dataset.state = seatState;
    references.card.style.setProperty('--score', String(scoreFraction));
    references.meter.setAttribute('aria-valuenow', bid ? String(bid.score) : '0');
    references.scoreText.textContent = bid ? bid.score.toFixed(1) : '–';
    references.statusText.textContent = describeSeatState(seat, seatState);
    references.wakeButton.disabled =
      seatState === 'unavailable' || !isSocketOpen() || isThreadEnded();

    const chip = seatChipReferences[seat.name];
    chip.dataset.state = seatState;
    chip.style.setProperty('--score', String(scoreFraction));
  }
}

/** Remove then re-add the class, with a forced reflow between, so the CSS animation restarts. */
function animateBidCards(seatNames) {
  for (const seatName of seatNames) {
    const references = seatCardReferences[seatName];
    if (!references) {
      continue;
    }
    const card = references.card;
    card.classList.remove('is-bidding');
    void card.offsetWidth;
    card.classList.add('is-bidding');
    setTimeout(() => card.classList.remove('is-bidding'), BID_ANIMATION_MS);
  }
}

function ensureCooldownTicker() {
  if (cooldownIntervalId !== null) {
    return;
  }
  cooldownIntervalId = setInterval(() => {
    renderSeats();
    const anyCooldownActive = state.seats.some((seat) => computeCooldownSecondsLeft(seat.name) > 0);
    if (!anyCooldownActive) {
      clearInterval(cooldownIntervalId);
      cooldownIntervalId = null;
    }
  }, 1000);
}

function renderModeSwitch() {
  const currentMode = state.thread ? state.thread.mode : null;
  for (const radio of elements.modeSwitch.querySelectorAll('input[name="mode"]')) {
    radio.checked = radio.value === currentMode;
  }

  if (state.router === 'jev') {
    elements.routerName.textContent = 'Router: Jev';
  } else if (state.router === 'heuristic') {
    elements.routerName.textContent = 'Router: heuristic fallback';
  } else {
    elements.routerName.textContent = 'Router: waiting for the first bid';
  }
}

function renderConnectionState() {
  const { state: connectionState, retryInSeconds } = state.connection;
  let text;
  switch (connectionState) {
    case 'connecting':
      text = 'Connecting';
      break;
    case 'open':
      text = 'Live';
      break;
    case 'reconnecting':
      text = `Reconnecting in ${retryInSeconds} s`;
      break;
    default:
      text = 'Offline';
  }
  elements.connectionState.dataset.state = connectionState;
  elements.connectionState.textContent = text;
}

function renderComposerState() {
  const ended = isThreadEnded();
  const connected = isSocketOpen();

  elements.composerInput.disabled = ended;
  elements.sendButton.disabled = ended || !connected;
  elements.stopButton.disabled = ended || !connected || state.typingSeats.size === 0;
  elements.endButton.disabled = ended || !connected || state.endRequested || state.thread === null;

  if (ended) {
    elements.composerHint.textContent = ENDED_HINT;
  } else if (!connected) {
    elements.composerHint.textContent = DISCONNECTED_HINT;
  } else {
    elements.composerHint.textContent = defaultComposerHint;
  }
}

function renderEndedNotes(notesBySeat) {
  elements.endedNotes.replaceChildren();
  const seatNames = Object.keys(notesBySeat);
  if (seatNames.length === 0) {
    elements.endedNotes.append(createElement('p', 'ended-empty', 'No seat wrote a note.'));
    return;
  }
  for (const seatName of seatNames) {
    const section = createElement('section', 'ended-seat');
    section.style.setProperty('--seat', seatColorFor(seatName));
    section.append(createElement('h3', 'ended-seat-name', seatName));
    const list = document.createElement('ul');
    for (const note of notesBySeat[seatName]) {
      list.append(createElement('li', '', note));
    }
    section.append(list);
    elements.endedNotes.append(section);
  }
}

function renderSearchResults(query, results) {
  elements.searchResults.replaceChildren();
  elements.searchResults.hidden = false;

  if (results.length === 0) {
    elements.searchResults.append(createElement('p', 'search-summary', 'No messages match.'));
    return;
  }
  const resultWord = results.length === 1 ? 'result' : 'results';
  elements.searchResults.append(
    createElement('p', 'search-summary', `${results.length} ${resultWord} for "${query}"`),
  );

  const list = createElement('ul', 'search-result-list');
  for (const result of results) {
    const message = result.message;
    const link = createElement('a', 'search-result');
    link.href = buildThreadUrl(message.thread_id, `#m-${message.id}`);

    const snippet = createElement('span', 'search-snippet');
    snippet.append(...buildHighlightedNodes(result.snippet, query));

    const authorLabel = message.role === 'owner' ? 'You' : message.author;
    const dateText = searchDateFormatter.format(new Date(message.created_at));
    link.append(
      createElement('span', 'search-thread', result.thread_title),
      snippet,
      createElement('span', 'search-meta', `${authorLabel} · ${dateText}`),
    );
    link.addEventListener('click', (event) => {
      event.preventDefault();
      closeDrawers();
      // The results cover the thread list; once one is chosen the owner is back in a thread.
      elements.searchInput.value = '';
      hideSearchResults();
      openThread(message.thread_id, {
        pushHistory: true,
        highlightMessageId: message.id,
      });
    });

    const item = document.createElement('li');
    item.append(link);
    list.append(item);
  }
  elements.searchResults.append(list);
}

function renderMentionMenu() {
  const menu = state.mentionMenu;
  elements.mentionMenu.replaceChildren();
  elements.mentionMenu.hidden = !menu.open;
  elements.composerInput.setAttribute('aria-expanded', String(menu.open));

  if (!menu.open) {
    elements.composerInput.removeAttribute('aria-activedescendant');
    return;
  }
  menu.items.forEach((seat, index) => {
    const option = createElement('li', 'mention-option');
    option.id = `mention-option-${seat.name}`;
    option.setAttribute('role', 'option');
    option.setAttribute('aria-selected', String(index === menu.activeIndex));
    option.style.setProperty('--seat', safeColor(seat.color));
    const swatch = createElement('span', 'seat-swatch');
    swatch.setAttribute('aria-hidden', 'true');
    option.append(
      swatch,
      createElement('span', 'mention-option-name', seat.name),
      createElement('span', 'mention-option-role', seat.role),
    );
    // mousedown, not click: click fires after the textarea has already lost focus.
    option.addEventListener('mousedown', (event) => {
      event.preventDefault();
      insertMention(seat);
    });
    elements.mentionMenu.append(option);
  });
  const activeSeat = menu.items[menu.activeIndex];
  elements.composerInput.setAttribute('aria-activedescendant', `mention-option-${activeSeat.name}`);
}

// ---------------------------------------------------------------------------
// 7. Message body rendering
// ---------------------------------------------------------------------------

/**
 * Minimal safe markup: blank-line paragraphs, "- " bullet lists, `inline code`, line breaks,
 * and @mentions. Every piece of text goes in as a text node.
 */
function renderMessageBody(text) {
  const body = createElement('div', 'message-body');
  const blocks = String(text).replace(/\r\n/g, '\n').split(/\n[ \t]*\n/);

  for (const block of blocks) {
    if (block.trim() === '') {
      continue;
    }
    appendBlock(body, block.split('\n'));
  }
  return body;
}

function appendBlock(body, lines) {
  let paragraphLines = [];
  let bulletLines = [];

  const flushParagraph = () => {
    if (paragraphLines.length === 0) {
      return;
    }
    const paragraph = document.createElement('p');
    paragraphLines.forEach((line, index) => {
      if (index > 0) {
        paragraph.append(document.createElement('br'));
      }
      appendInline(paragraph, line);
    });
    body.append(paragraph);
    paragraphLines = [];
  };
  const flushBullets = () => {
    if (bulletLines.length === 0) {
      return;
    }
    const list = document.createElement('ul');
    for (const line of bulletLines) {
      const item = document.createElement('li');
      appendInline(item, line);
      list.append(item);
    }
    body.append(list);
    bulletLines = [];
  };

  for (const line of lines) {
    if (line.startsWith('- ')) {
      flushParagraph();
      bulletLines.push(line.slice(2));
    } else {
      flushBullets();
      paragraphLines.push(line);
    }
  }
  flushParagraph();
  flushBullets();
}

function appendInline(parent, text) {
  // Splitting with a capture group puts the code contents at odd indexes.
  const pieces = text.split(/`([^`]+)`/);
  pieces.forEach((piece, index) => {
    if (index % 2 === 1) {
      parent.append(createElement('code', '', piece));
    } else {
      appendTextWithMentions(parent, piece);
    }
  });
}

function appendTextWithMentions(parent, text) {
  const mentionPattern = /@([a-z0-9_-]+)/gi;
  let cursor = 0;
  for (const match of text.matchAll(mentionPattern)) {
    const characterBefore = match.index > 0 ? text[match.index - 1] : '';
    const startsWord = characterBefore !== '' && /\w/.test(characterBefore);
    const mentionElement = startsWord ? null : createMentionElement(match[1]);
    if (!mentionElement) {
      continue;
    }
    if (match.index > cursor) {
      parent.append(document.createTextNode(text.slice(cursor, match.index)));
    }
    parent.append(mentionElement);
    cursor = match.index + match[0].length;
  }
  if (cursor < text.length) {
    parent.append(document.createTextNode(text.slice(cursor)));
  }
}

function createMentionElement(name) {
  const lowerName = name.toLowerCase();
  if (lowerName === 'owner' || lowerName === 'you') {
    return createElement('span', 'mention mention--owner', `@${name}`);
  }
  const seat = state.seats.find((candidate) => candidate.name.toLowerCase() === lowerName);
  if (!seat) {
    return null;
  }
  const element = createElement('span', 'mention', `@${name}`);
  element.style.setProperty('--seat', safeColor(seat.color));
  return element;
}

/** Wrap case-insensitive matches of each query word in <mark>, using text nodes only. */
function buildHighlightedNodes(text, query) {
  const words = query.split(/\s+/).filter((word) => word.length > 0);
  if (words.length === 0) {
    return [document.createTextNode(text)];
  }
  const escapedWords = words.map((word) => word.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'));
  const pattern = new RegExp(`(${escapedWords.join('|')})`, 'gi');

  // With one capture group, matches land at odd indexes of the split result.
  return text.split(pattern).flatMap((piece, index) => {
    if (piece === '') {
      return [];
    }
    if (index % 2 === 1) {
      return [createElement('mark', '', piece)];
    }
    return [document.createTextNode(piece)];
  });
}

// ---------------------------------------------------------------------------
// 8. Scrolling, thread switching, composer, search, drawers, keyboard
// ---------------------------------------------------------------------------

function isNearBottom() {
  const scroller = elements.messageScroller;
  const distanceFromBottom = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight;
  return distanceFromBottom <= NEAR_BOTTOM_PIXELS;
}

function scrollMessagesToBottom(smooth) {
  const scroller = elements.messageScroller;
  scroller.scrollTo({ top: scroller.scrollHeight, behavior: smooth ? 'smooth' : 'instant' });
}

function showJumpLatest() {
  const count = state.unseenCount;
  elements.jumpLatest.textContent = count === 1 ? '1 new message below' : `${count} new messages below`;
  elements.jumpLatest.hidden = false;
}

function hideJumpLatest() {
  state.unseenCount = 0;
  elements.jumpLatest.hidden = true;
}

function scrollToMessage(messageId) {
  const element = document.getElementById(`m-${messageId}`);
  if (!element) {
    return false;
  }
  element.scrollIntoView({ block: 'center', behavior: 'instant' });
  element.classList.add('message--highlight');
  setTimeout(() => element.classList.remove('message--highlight'), HIGHLIGHT_DURATION_MS);
  return true;
}

function parseMessageIdFromHash(hash) {
  const match = /^#m-(.+)$/.exec(hash);
  return match ? match[1] : null;
}

function resetRoomState() {
  state.thread = null;
  // seats stay: they are global and the next hello frame replaces them anyway
  state.messages = [];
  state.typingSeats.clear();
  state.lastBids = {};
  state.chosenSeat = null;
  state.router = null;
  state.cooldownEndsAt = {};
  state.quietNotice = false;
  state.endRequested = false;
  state.ownerScrolledUp = false;
  hideJumpLatest();
  hideMentionMenu();
}

function openThread(threadId, options = {}) {
  const { pushHistory = false, highlightMessageId = null } = options;

  if (threadId === state.currentThreadId && state.thread) {
    // Same thread: no reconnect needed, only move to the message.
    if (highlightMessageId) {
      scrollToMessage(highlightMessageId);
    }
    return;
  }

  closeSocketOnPurpose();
  resetRoomState();
  state.currentThreadId = threadId;
  state.reconnectAttempt = 0;

  const hash = highlightMessageId ? `#m-${highlightMessageId}` : '';
  if (pushHistory) {
    history.pushState({ threadId }, '', buildThreadUrl(threadId, hash));
  }
  state.pendingHighlightMessageId = highlightMessageId || parseMessageIdFromHash(location.hash);

  renderThreadList();
  renderRoomHeader();
  renderMessages();
  renderTypingLine();
  renderSeats();
  renderModeSwitch();
  renderComposerState();
  connectSocket();
}

function showEmptyState() {
  state.currentThreadId = null;
  setConnectionState('closed');
  renderRoomHeader();
  addLocalMessage('No threads yet. Start one from the left.', false);
}

function readThreadIdFromLocation() {
  return new URLSearchParams(location.search).get('thread');
}

function autoGrowComposer() {
  const input = elements.composerInput;
  input.style.height = 'auto';
  const maxHeight = window.innerHeight * COMPOSER_MAX_VIEWPORT_FRACTION;
  input.style.height = Math.min(input.scrollHeight, maxHeight) + 'px';
}

function submitComposer() {
  const text = elements.composerInput.value.trim();
  if (text === '' || isThreadEnded()) {
    return;
  }
  // No optimistic render: the server echoes a message frame to every tab, so the
  // transcript has one source of truth and a second open tab stays in sync.
  const wasSent = sendFrame({ type: 'say', text });
  if (!wasSent) {
    return;
  }
  elements.composerInput.value = '';
  autoGrowComposer();
  hideMentionMenu();
}

function requestStop() {
  if (state.typingSeats.size > 0) {
    sendFrame({ type: 'stop' });
  }
}

function requestEnd() {
  const confirmed = confirm('End this discussion? Each seat that spoke will write memory notes.');
  if (!confirmed) {
    return;
  }
  if (sendFrame({ type: 'end' })) {
    state.endRequested = true;
    renderComposerState();
  }
}

// ----- @ autocomplete -----

function findMentionToken() {
  const input = elements.composerInput;
  const textBeforeCaret = input.value.slice(0, input.selectionStart);
  const match = /(^|\s)@([a-z0-9_-]*)$/i.exec(textBeforeCaret);
  if (!match) {
    return null;
  }
  return { partial: match[2], tokenStart: match.index + match[1].length };
}

function updateMentionMenu() {
  const token = findMentionToken();
  if (!token) {
    hideMentionMenu();
    return;
  }
  const lowerPartial = token.partial.toLowerCase();
  const items = state.seats.filter((seat) => seat.name.toLowerCase().startsWith(lowerPartial));
  if (items.length === 0) {
    hideMentionMenu();
    return;
  }
  const previousActive = state.mentionMenu.items[state.mentionMenu.activeIndex];
  const keptIndex = previousActive ? items.findIndex((seat) => seat.name === previousActive.name) : -1;
  state.mentionMenu = {
    open: true,
    items,
    activeIndex: keptIndex >= 0 ? keptIndex : 0,
    tokenStart: token.tokenStart,
  };
  renderMentionMenu();
}

function hideMentionMenu() {
  if (!state.mentionMenu.open && elements.mentionMenu.hidden) {
    return;
  }
  state.mentionMenu = { open: false, items: [], activeIndex: 0, tokenStart: 0 };
  renderMentionMenu();
}

function moveMentionSelection(step) {
  const menu = state.mentionMenu;
  menu.activeIndex = (menu.activeIndex + step + menu.items.length) % menu.items.length;
  renderMentionMenu();
}

function insertMention(seat) {
  const input = elements.composerInput;
  const caret = input.selectionStart;
  const before = input.value.slice(0, state.mentionMenu.tokenStart);
  const after = input.value.slice(caret);
  const insertion = `@${seat.name} `;
  input.value = before + insertion + after;
  const newCaret = before.length + insertion.length;
  input.setSelectionRange(newCaret, newCaret);
  hideMentionMenu();
  autoGrowComposer();
  input.focus();
}

function handleComposerKeydown(event) {
  if (state.mentionMenu.open) {
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      moveMentionSelection(event.key === 'ArrowDown' ? 1 : -1);
      return;
    }
    if ((event.key === 'Enter' || event.key === 'Tab') && !event.isComposing) {
      event.preventDefault();
      insertMention(state.mentionMenu.items[state.mentionMenu.activeIndex]);
      return;
    }
    if (event.key === 'Escape') {
      // Closing the menu must not also reach the document-level Esc that stops a speaker.
      event.preventDefault();
      event.stopPropagation();
      hideMentionMenu();
      return;
    }
  }
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    submitComposer();
  }
}

// ----- search -----

function hideSearchResults() {
  clearTimeout(searchDebounceTimerId);
  searchRequestCounter += 1; // invalidates any request still in flight
  elements.searchResults.hidden = true;
  elements.searchResults.replaceChildren();
}

async function runSearch(query) {
  searchRequestCounter += 1;
  const requestNumber = searchRequestCounter;
  try {
    const results = await fetchJson('/api/search?q=' + encodeURIComponent(query));
    if (requestNumber !== searchRequestCounter) {
      return; // a newer query was started while this one was loading
    }
    renderSearchResults(query, results);
  } catch (error) {
    if (requestNumber === searchRequestCounter) {
      reportError('Search failed', error);
    }
  }
}

function handleSearchInput() {
  clearTimeout(searchDebounceTimerId);
  const query = elements.searchInput.value.trim();
  if (query === '') {
    hideSearchResults();
    return;
  }
  searchDebounceTimerId = setTimeout(() => runSearch(query), SEARCH_DEBOUNCE_MS);
}

function handleSearchKeydown(event) {
  if (event.key !== 'Escape') {
    return;
  }
  event.stopPropagation();
  elements.searchInput.value = '';
  hideSearchResults();
}

// ----- drawers for narrow layouts -----

function isDrawerOpen() {
  return document.body.classList.contains('show-threads') || document.body.classList.contains('show-seats');
}

function syncDrawerAttributes() {
  const threadsOpen = document.body.classList.contains('show-threads');
  const seatsOpen = document.body.classList.contains('show-seats');
  elements.toggleThreads.setAttribute('aria-expanded', String(threadsOpen));
  elements.toggleSeats.setAttribute('aria-expanded', String(seatsOpen));
  elements.drawerScrim.hidden = !(threadsOpen || seatsOpen);
}

function toggleDrawer(drawerName) {
  const className = drawerName === 'threads' ? 'show-threads' : 'show-seats';
  const otherClassName = drawerName === 'threads' ? 'show-seats' : 'show-threads';
  document.body.classList.remove(otherClassName);
  document.body.classList.toggle(className);
  syncDrawerAttributes();
}

function closeDrawers() {
  document.body.classList.remove('show-threads', 'show-seats');
  syncDrawerAttributes();
}

// ----- global keyboard -----

function handleDocumentKeydown(event) {
  const isSearchShortcut = (event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k';
  if (isSearchShortcut) {
    event.preventDefault();
    // On narrow layouts the search box lives in the hidden threads drawer.
    const threadsToggleIsVisible = elements.toggleThreads.offsetParent !== null;
    if (threadsToggleIsVisible && !document.body.classList.contains('show-threads')) {
      toggleDrawer('threads');
    }
    elements.searchInput.focus();
    elements.searchInput.select();
    return;
  }

  if (event.key !== 'Escape' || elements.endedDialog.open) {
    return;
  }
  if (isDrawerOpen()) {
    closeDrawers();
    return;
  }
  requestStop();
}

function handleVisibilityChange() {
  if (document.visibilityState !== 'visible' || !state.currentThreadId) {
    return;
  }
  // Browsers throttle timers in hidden tabs, so a pending backoff may be long overdue.
  if (!isSocketOpen() && state.connection.state !== 'connecting') {
    reconnectNow();
  }
}

// ---------------------------------------------------------------------------
// 9. Startup
// ---------------------------------------------------------------------------

function bindEvents() {
  elements.messageScroller.addEventListener('scroll', () => {
    state.ownerScrolledUp = !isNearBottom();
    if (!state.ownerScrolledUp) {
      hideJumpLatest();
    }
  });
  elements.jumpLatest.addEventListener('click', () => {
    state.ownerScrolledUp = false;
    hideJumpLatest();
    scrollMessagesToBottom(!prefersReducedMotion());
  });

  elements.newThreadForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    const title = elements.newThreadTitle.value.trim();
    if (title === '') {
      return;
    }
    try {
      await createThread(title);
      elements.newThreadTitle.value = '';
      closeDrawers();
    } catch (error) {
      reportError('Could not create the thread', error);
    }
  });

  elements.composer.addEventListener('submit', (event) => {
    event.preventDefault();
    submitComposer();
  });
  elements.composerInput.addEventListener('keydown', handleComposerKeydown);
  elements.composerInput.addEventListener('input', () => {
    autoGrowComposer();
    updateMentionMenu();
  });
  elements.composerInput.addEventListener('click', updateMentionMenu);
  elements.composerInput.addEventListener('blur', hideMentionMenu);
  elements.stopButton.addEventListener('click', requestStop);
  elements.endButton.addEventListener('click', requestEnd);

  elements.modeSwitch.addEventListener('change', (event) => {
    if (event.target.name !== 'mode') {
      return;
    }
    if (!sendFrame({ type: 'mode', mode: event.target.value })) {
      renderModeSwitch(); // put the radio back on the server's mode
    }
  });

  elements.searchForm.addEventListener('submit', (event) => {
    event.preventDefault();
    const query = elements.searchInput.value.trim();
    if (query !== '') {
      clearTimeout(searchDebounceTimerId);
      runSearch(query);
    }
  });
  elements.searchInput.addEventListener('input', handleSearchInput);
  elements.searchInput.addEventListener('keydown', handleSearchKeydown);

  elements.toggleThreads.addEventListener('click', () => toggleDrawer('threads'));
  elements.toggleSeats.addEventListener('click', () => toggleDrawer('seats'));
  elements.drawerScrim.addEventListener('click', closeDrawers);

  document.addEventListener('keydown', handleDocumentKeydown);
  document.addEventListener('visibilitychange', handleVisibilityChange);
  window.addEventListener('popstate', () => {
    const threadId = readThreadIdFromLocation();
    if (threadId) {
      openThread(threadId, { pushHistory: false });
    }
  });
}

async function start() {
  bindEvents();
  renderConnectionState();
  renderComposerState();

  try {
    await Promise.all([loadThreads(), loadSeats()]);
  } catch (error) {
    reportError('Could not load threads', error);
    return;
  }

  const requestedThreadId = readThreadIdFromLocation();
  if (requestedThreadId) {
    openThread(requestedThreadId, { pushHistory: false });
  } else if (state.threads.length > 0) {
    // threads are newest first (SPEC section 6)
    history.replaceState({}, '', buildThreadUrl(state.threads[0].id));
    openThread(state.threads[0].id, { pushHistory: false });
  } else {
    showEmptyState();
  }
}

start();
