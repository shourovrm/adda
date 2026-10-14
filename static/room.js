/*
 * Room page (SPEC section 8). Regions, top to bottom:
 *   1. Constants and state
 *   2. Small lookups (seats, colours, labels)
 *   3. Frame handlers, one per server frame type (SPEC section 6)
 *   4. Render functions, one per region of the page
 *   5. Seat cards and the seat editor
 *   6. Composer, @ menu, search, drawer, keyboard
 *   7. Startup
 *
 * Seats are identified by id everywhere; names are only shown. Message text reaches the DOM
 * through AddaMarkdown (which never uses innerHTML) or textContent.
 */

'use strict';

// ---------------------------------------------------------------------------
// 1. Constants and state
// ---------------------------------------------------------------------------

const MAX_BID_SCORE = 10;
const DEFAULT_COOLDOWN_SECONDS = 20; // used only if /api/settings cannot be read
const DEFAULT_RESUME_LIVELINESS = 6;
const NEAR_BOTTOM_PIXELS = 80;
const HIGHLIGHT_DURATION_MS = 2500;
const BID_ANIMATION_MS = 900;
const SEARCH_DEBOUNCE_MS = 250;
const COMPOSER_MAX_VIEWPORT_FRACTION = 0.4;
const MODE_LABELS = { bidding: 'Bidding', roundrobin: 'Round-robin', manual: 'Manual' };
const WOKEN_BY_LABELS = {
  mention: 'woken by mention',
  all: 'woken by @all',
  wake: 'woken by Wake',
  roundrobin: 'round-robin',
};
const QUIET_REASONS = {
  nobody: 'The room went quiet. Nobody scored high enough. Your turn.',
  paused: 'The room is paused. Mention a seat, press Wake all, or resume.',
  safety: 'Safety stop: too many automatic turns without you. Say something to continue.',
};

const state = {
  roomId: decodeURIComponent(location.pathname.split('/').filter(Boolean).pop() || ''),
  room: null,
  messages: [],
  ownerName: 'Owner',
  router: null,
  harnesses: [],
  personas: [],
  cooldownSeconds: DEFAULT_COOLDOWN_SECONDS,
  typingSeatIds: new Set(),
  lastBids: {}, // seat id -> {score, confidence}
  threshold: 5,
  chosenSeatId: null,
  cooldownEndsAt: {}, // seat id -> epoch ms
  quietReason: null,
  connection: { state: 'connecting', retryInSeconds: 0 },
  ownerScrolledUp: false,
  unseenCount: 0,
  mentionMenu: { open: false, items: [], activeIndex: 0, tokenStart: 0 },
  expandedSeatIds: new Set(),
  instructionOpenSeatIds: new Set(),
  endRequested: false,
  pendingHighlightId: null,
  localMessageCounter: 0,
};

const elements = {
  ownerName: document.getElementById('owner-name'),
  title: document.getElementById('room-title'),
  meta: document.getElementById('room-meta'),
  connection: document.getElementById('connection'),
  copyThreadButton: document.getElementById('copy-thread-button'),
  panelToggle: document.getElementById('panel-toggle'),
  drawerScrim: document.getElementById('drawer-scrim'),
  searchForm: document.getElementById('search-form'),
  searchInput: document.getElementById('search-input'),
  searchResults: document.getElementById('search-results'),
  messageScroller: document.getElementById('message-scroller'),
  messageList: document.getElementById('message-list'),
  jumpLatest: document.getElementById('jump-latest'),
  seatStrip: document.getElementById('seat-strip'),
  statusLine: document.getElementById('status-line'),
  composer: document.getElementById('composer'),
  composerInput: document.getElementById('composer-input'),
  composerHint: document.getElementById('composer-hint'),
  mentionMenu: document.getElementById('mention-menu'),
  endButton: document.getElementById('end-button'),
  stopButton: document.getElementById('stop-button'),
  sendButton: document.getElementById('send-button'),
  liveliness: document.getElementById('liveliness-input'),
  livelinessValue: document.getElementById('liveliness-value'),
  livelinessSentence: document.getElementById('liveliness-sentence'),
  budget: document.getElementById('budget-input'),
  spendText: document.getElementById('spend-text'),
  modeOptions: document.getElementById('mode-options'),
  pauseButton: document.getElementById('pause-button'),
  wakeAllButton: document.getElementById('wake-all-button'),
  routerName: document.getElementById('router-name'),
  seatList: document.getElementById('seat-list'),
  addSeatButton: document.getElementById('add-seat-button'),
  addSeatForm: document.getElementById('add-seat-form'),
  endedDialog: document.getElementById('ended-dialog'),
  endedNotes: document.getElementById('ended-notes'),
};

const defaultComposerHint = elements.composerHint.textContent;
let socketClient = null;
let seatCardReferences = {}; // seat id -> DOM references, rebuilt when the set of seat ids changes
let seatChipReferences = {};
let cooldownIntervalId = null;
let searchDebounceTimerId = null;
let searchRequestCounter = 0;

// ---------------------------------------------------------------------------
// 2. Small lookups
// ---------------------------------------------------------------------------

function seatsOfRoom() {
  return state.room ? state.room.seats : [];
}

function seatById(seatId) {
  return seatsOfRoom().find((seat) => seat.id === seatId) || null;
}

function isRoomEnded() {
  return Boolean(state.room && state.room.ended_at);
}

function isConnected() {
  return socketClient !== null && socketClient.isOpen();
}

function prefersReducedMotion() {
  return window.matchMedia('(prefers-reduced-motion: reduce)').matches;
}

/** The map AddaMarkdown uses to colour @mentions: handle -> seat. */
function buildSeatsByHandle() {
  const byHandle = {};
  for (const seat of seatsOfRoom()) {
    byHandle[seat.handle] = seat;
  }
  return byHandle;
}

function personaRoleOf(seat) {
  const persona = state.personas.find((candidate) => candidate.slug === seat.persona);
  return persona ? persona.role : seat.program;
}

function removeWhitespace(text) {
  return text.replace(/\s+/g, '');
}

/** Seats that spoke because of an automatic bid since the last owner message or wake. */
function countAutomaticTurns() {
  let turns = 0;
  for (let index = state.messages.length - 1; index >= 0; index--) {
    const message = state.messages[index];
    if (message.role === 'system') continue;
    const wokenBy = (message.meta || {}).woken_by;
    if (message.role !== 'seat' || (wokenBy !== 'bid' && wokenBy !== 'roundrobin')) break;
    turns += 1;
  }
  return turns;
}

function addLocalMessage(text, isError, isPassed = false) {
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
    isPassed,
  };
  state.messages.push(message);
  appendMessageElement(message);
  scrollMessagesToBottom(false);
}

function sendFrame(frame) {
  if (socketClient && socketClient.send(frame)) {
    return true;
  }
  showToast('Not connected. Try again when the room is live.', true);
  return false;
}

// ---------------------------------------------------------------------------
// 3. Frame handlers
// ---------------------------------------------------------------------------

function handleFrame(frame) {
  if (frame.type !== 'quiet' && frame.type !== 'typing' && frame.type !== 'bids' && frame.type !== 'room' && frame.type !== 'passed') {
    state.quietReason = null;
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
    case 'room':
      handleRoomFrame(frame.room);
      break;
    case 'passed':
      handlePassedFrame(frame);
      break;
    case 'quiet':
      state.quietReason = frame.reason;
      renderStatusLine();
      break;
    case 'ended':
      handleEndedFrame(frame);
      break;
    case 'error':
      state.endRequested = false;
      addLocalMessage(frame.text, true);
      showToast(frame.text, true);
      renderSeats(true);
      renderComposerState();
      break;
    default:
      console.warn('Unknown frame type', frame);
  }
}

function handleStatus(status) {
  state.connection = status;
  if (status.state !== 'open') {
    state.typingSeatIds.clear();
  }
  renderConnection();
  renderStatusLine();
  renderComposerState();
  renderSeats();
}

function handleHello(frame) {
  state.room = frame.room;
  state.messages = frame.messages;
  state.ownerName = frame.owner.name;
  state.router = frame.router;
  state.typingSeatIds.clear();
  state.quietReason = null;
  state.endRequested = false;
  elements.ownerName.textContent = state.ownerName;

  renderEverything();

  if (state.pendingHighlightId !== null) {
    const highlightId = state.pendingHighlightId;
    state.pendingHighlightId = null;
    if (scrollToMessage(highlightId)) return;
  }
  // On a reconnect the owner may be reading older messages; do not yank them down.
  if (!state.ownerScrolledUp) {
    scrollMessagesToBottom(false);
    hideJumpLatest();
  }
}

function handleMessageFrame(message) {
  if (state.messages.some((existing) => existing.id === message.id)) return;
  // Cooldown 0 means off: no countdown, no cooling state.
  if (message.role === 'seat' && message.seat_id && state.cooldownSeconds > 0) {
    state.cooldownEndsAt[message.seat_id] = Date.now() + state.cooldownSeconds * 1000;
    ensureCooldownTicker();
  }
  state.chosenSeatId = null;
  state.quietReason = null;
  state.messages.push(message);
  appendMessageElement(message);
  renderRoomMeta();
  renderPanel();
  renderSeats();
  renderStatusLine();

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
    state.typingSeatIds.add(frame.seat);
    if (state.chosenSeatId === frame.seat) state.chosenSeatId = null;
    state.quietReason = null;
  } else {
    state.typingSeatIds.delete(frame.seat);
  }
  renderStatusLine();
  renderSeats();
  renderComposerState();
}

/** A pass is not a message: it is shown as a muted line and is not stored or counted. */
function handlePassedFrame(frame) {
  const seat = seatById(frame.seat);
  addLocalMessage(`${seat ? seat.name : 'A seat'} passed`, false, true);
}

function handleBidsFrame(frame) {
  const bidsBySeat = {};
  for (const bid of frame.bids) {
    bidsBySeat[bid.seat] = { score: bid.score, rawScore: bid.raw_score ?? bid.score, confidence: bid.confidence };
  }
  state.lastBids = bidsBySeat;
  state.chosenSeatId = frame.chosen;
  state.threshold = frame.threshold;
  state.router = frame.router;
  renderSeats();
  renderRouterName();
  animateBidCards(Object.keys(bidsBySeat));
}

function handleRoomFrame(room) {
  const namesChanged = renameMessageAuthors(room);
  state.room = room;
  renderRoomMeta();
  renderPanel();
  renderSeats();
  renderComposerState();
  renderStatusLine();
  if (namesChanged) {
    renderMessages();
  }
  if (document.activeElement !== elements.composerInput) {
    hideMentionMenu();
  }
}

/** A renamed seat keeps its id, and the server rewrites old authors; mirror that in the loaded thread. */
function renameMessageAuthors(room) {
  let changed = false;
  for (const message of state.messages) {
    if (message.role !== 'seat') continue;
    const seat = room.seats.find((candidate) => candidate.id === message.seat_id);
    if (seat && seat.name !== message.author) {
      message.author = seat.name;
      changed = true;
    }
  }
  return changed;
}

function handleEndedFrame(frame) {
  state.endRequested = false;
  if (state.room && !state.room.ended_at) {
    state.room.ended_at = new Date().toISOString();
  }
  renderEverything();
  renderEndedNotes(frame.notes || {});
  elements.endedDialog.showModal();
}

// ---------------------------------------------------------------------------
// 4. Render functions
// ---------------------------------------------------------------------------

function renderEverything() {
  renderRoomMeta();
  renderConnection();
  renderMessages();
  renderPanel();
  renderRouterName();
  renderSeats(true);
  renderStatusLine();
  renderComposerState();
}

function describeRoomState(room) {
  if (room.ended_at) return 'Ended';
  if (room.liveliness === 0) return 'Paused';
  return 'Live';
}

function renderRoomMeta() {
  const room = state.room;
  if (!room) return;
  elements.title.textContent = room.title;
  document.title = `${room.title} · Adda`;
  const persisted = state.messages.filter((message) => !message.isLocal);
  const budgetText = room.budget_usd > 0 ? ` of ${formatMoney(room.budget_usd)}` : ' (no budget limit)';
  const parts = [
    MODE_LABELS[room.mode] || room.mode,
    room.folder || 'no folder',
    `${persisted.length} ${persisted.length === 1 ? 'message' : 'messages'}`,
    `${formatMoney(room.spent_usd)}${budgetText}`,
    describeRoomState(room),
  ];
  elements.meta.textContent = parts.join(' · ');
}

function renderConnection() {
  const { state: connectionState, retryInSeconds } = state.connection;
  const labels = {
    connecting: 'Connecting',
    open: 'Live',
    reconnecting: `Reconnecting in ${retryInSeconds} s`,
    closed: 'Offline',
  };
  elements.connection.dataset.state = connectionState;
  elements.connection.textContent = labels[connectionState] || 'Offline';
}

// ----- messages -----

function renderMessages() {
  elements.messageList.replaceChildren();
  for (const message of state.messages) {
    elements.messageList.append(createMessageItem(message));
  }
}

function appendMessageElement(message) {
  const item = createMessageItem(message);
  item.firstElementChild.classList.add('msg--arriving');
  elements.messageList.append(item);
}

function describeMessageMeta(message) {
  const parts = [formatClock(message.created_at)];
  if (typeof message.cost_usd === 'number') parts.push(formatMoney(message.cost_usd));
  const meta = message.meta || {};
  if (typeof meta.latency_ms === 'number') parts.push(formatDuration(meta.latency_ms));
  if (meta.woken_by === 'bid' && typeof meta.bid_score === 'number') {
    parts.push(`bid ${meta.bid_score.toFixed(1)}`);
  } else if (WOKEN_BY_LABELS[meta.woken_by]) {
    parts.push(WOKEN_BY_LABELS[meta.woken_by]);
  }
  return parts.join(' · ');
}

function messageColor(message) {
  const bySeatId = seatById(message.seat_id);
  if (bySeatId) return bySeatId.color;
  const byName = seatsOfRoom().find((seat) => seat.name === message.author);
  return byName ? byName.color : null;
}

/** "@all woke Ravi, Tuli and Pi": shown under an owner message that woke more than one seat. */
function describeWake(message) {
  const woken = message.mentions.map((seatId) => seatById(seatId)).filter(Boolean);
  if (message.role !== 'owner' || woken.length < 2) return null;
  const names = woken.map((seat) => seat.name);
  const list = names.length > 2 ? names.slice(0, -1).join(', ') + ' and ' + names[names.length - 1] : names.join(' and ');
  const trigger = /(^|[^\w])@all\b/i.test(message.text) ? '@all' : 'Mentions';
  return `${trigger} woke ${list}`;
}

function createMessageItem(message) {
  const item = createElement('li', 'message-item');
  item.id = `m-${message.id}`;

  if (message.role === 'system') {
    const lineClass = message.isError ? 'msg msg--system is-error' : message.isPassed ? 'msg msg--system msg--passed' : 'msg msg--system';
    const line = createElement('div', lineClass, message.text);
    item.append(line);
    return item;
  }

  const isOwner = message.role === 'owner';
  const article = createElement('article', isOwner ? 'msg msg--owner' : 'msg');
  const color = isOwner ? null : messageColor(message);
  if (color) setSeatColor(article, color);

  const authorName = isOwner ? state.ownerName : message.author;
  const head = createElement('div', 'msg-head');
  head.append(createElement('span', 'msg-who', authorName), createElement('span', 'msg-meta', describeMessageMeta(message)));

  const tools = createElement('div', 'msg-tools');
  tools.append(createButton('Copy', '', (event) => copyMessage(message, event.currentTarget)));
  tools.append(createButton('Quote', '', () => quoteMessage(message, authorName)));

  const body = createElement('div', 'msg-body');
  body.append(AddaMarkdown.renderMarkdown(message.text, buildSeatsByHandle()));

  article.append(head, tools, body);
  item.append(article);

  const wakeText = describeWake(message);
  if (wakeText) {
    item.append(createElement('p', 'wake-line', wakeText));
  }
  return item;
}

async function copyMessage(message, button) {
  const copied = await AddaMarkdown.copyText(message.text);
  button.textContent = copied ? 'Copied' : 'Copy failed';
  setTimeout(() => {
    button.textContent = 'Copy';
  }, 1500);
}

function quoteMessage(message, authorName) {
  const quoted = message.text
    .split('\n')
    .map((line) => `> ${line}`)
    .join('\n');
  const input = elements.composerInput;
  const separator = input.value && !input.value.endsWith('\n') ? '\n' : '';
  input.value = `${input.value}${separator}> ${authorName}:\n${quoted}\n\n`;
  autoGrowComposer();
  input.focus();
  input.setSelectionRange(input.value.length, input.value.length);
}

async function copyThread() {
  const persisted = state.messages.filter((message) => !message.isLocal);
  const copied = await AddaMarkdown.copyText(AddaMarkdown.toMarkdown(persisted, state.ownerName));
  showToast(copied ? 'Thread copied as Markdown.' : 'Copy failed.', !copied);
}

// ----- scrolling -----

function isNearBottom() {
  const scroller = elements.messageScroller;
  return scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight <= NEAR_BOTTOM_PIXELS;
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
  if (!element) return false;
  element.scrollIntoView({ block: 'center', behavior: 'instant' });
  element.classList.add('msg--highlight');
  setTimeout(() => element.classList.remove('msg--highlight'), HIGHLIGHT_DURATION_MS);
  return true;
}

// ----- status line (typing and quiet notice) -----

function describeQuiet(reason) {
  if (reason === 'budget') {
    const room = state.room;
    return `Budget reached (${formatMoney(room.spent_usd)} of ${formatMoney(room.budget_usd)}). Raise it to continue.`;
  }
  return QUIET_REASONS[reason] || 'The room went quiet. Your turn.';
}

function renderStatusLine() {
  elements.statusLine.replaceChildren();
  for (const seatId of state.typingSeatIds) {
    const seat = seatById(seatId);
    if (!seat) continue;
    const typing = createElement('span', 'typing');
    setSeatColor(typing, seat.color);
    const dots = createElement('span', 'typing-dots');
    dots.setAttribute('aria-hidden', 'true');
    dots.append(document.createElement('i'), document.createElement('i'), document.createElement('i'));
    typing.append(dots, createElement('span', 'typing-name', seat.name), ' is thinking');
    elements.statusLine.append(typing);
  }
  if (state.quietReason && state.typingSeatIds.size === 0 && state.room) {
    elements.statusLine.append(createElement('span', 'quiet-notice', describeQuiet(state.quietReason)));
  }
}

// ----- side panel -----

function resumeStorageKey() {
  return `adda.resumeLiveliness.${state.roomId}`;
}

function renderPanel() {
  const room = state.room;
  if (!room) return;
  // Never overwrite a field the owner is editing.
  if (document.activeElement !== elements.liveliness) elements.liveliness.value = room.liveliness;
  elements.livelinessValue.textContent = room.liveliness;
  elements.livelinessSentence.textContent = describeLiveliness(room.liveliness, countAutomaticTurns());
  if (document.activeElement !== elements.budget) elements.budget.value = room.budget_usd;
  elements.spendText.textContent = `${formatMoney(room.spent_usd)} used`;
  for (const radio of elements.modeOptions.querySelectorAll('input[name="mode"]')) {
    radio.checked = radio.value === room.mode;
    radio.disabled = isRoomEnded();
  }
  elements.pauseButton.textContent = room.liveliness === 0 ? 'Resume' : 'Pause';
  elements.pauseButton.disabled = isRoomEnded();
  elements.wakeAllButton.disabled = isRoomEnded() || !isConnected();
  elements.addSeatButton.disabled = isRoomEnded();
}

function renderRouterName() {
  const names = { jev: 'Router: Jev', heuristic: 'Router: heuristic fallback' };
  elements.routerName.textContent = names[state.router] || 'Router: waiting for the first bid';
}

function sendRoomPatch(patch) {
  sendFrame({ type: 'room', patch });
}

function togglePause() {
  const room = state.room;
  if (room.liveliness === 0) {
    const remembered = Number(localStorage.getItem(resumeStorageKey()));
    sendRoomPatch({ liveliness: remembered > 0 ? remembered : DEFAULT_RESUME_LIVELINESS });
    return;
  }
  localStorage.setItem(resumeStorageKey(), String(room.liveliness));
  sendRoomPatch({ liveliness: 0 });
}

function renderEndedNotes(notesBySeatId) {
  elements.endedNotes.replaceChildren();
  const seatIds = Object.keys(notesBySeatId);
  if (seatIds.length === 0) {
    elements.endedNotes.append(createElement('p', 'mute', 'No seat wrote a note.'));
    return;
  }
  for (const seatId of seatIds) {
    const seat = seatById(seatId);
    const section = createElement('section', 'ended-seat');
    if (seat) setSeatColor(section, seat.color);
    section.append(createElement('h3', '', seat ? seat.name : 'Removed seat'));
    const list = document.createElement('ul');
    for (const note of notesBySeatId[seatId]) {
      list.append(createElement('li', '', note));
    }
    section.append(list);
    elements.endedNotes.append(section);
  }
}

// ---------------------------------------------------------------------------
// 5. Seat cards and the seat editor
// ---------------------------------------------------------------------------

function cooldownSecondsLeft(seatId) {
  const endsAt = state.cooldownEndsAt[seatId];
  return endsAt ? Math.max(0, Math.ceil((endsAt - Date.now()) / 1000)) : 0;
}

function computeSeatState(seat) {
  if (!seat.available) return 'unavailable';
  if (!seat.enabled) return 'disabled';
  if (state.typingSeatIds.has(seat.id)) return 'speaking';
  if (cooldownSecondsLeft(seat.id) > 0) return 'cooldown';
  if (state.chosenSeatId === seat.id) return 'chosen';
  return 'idle';
}

function describeSeatState(seat, seatState) {
  switch (seatState) {
    case 'unavailable':
      return 'Not installed';
    case 'disabled':
      return 'Disabled';
    case 'speaking':
      return 'Speaking';
    case 'cooldown':
      return `Cooling down ${cooldownSecondsLeft(seat.id)} s`;
    case 'chosen':
      return 'Chosen';
    default:
      return state.lastBids[seat.id] || (state.room && state.room.mode === 'manual') ? 'Idle' : 'Waiting';
  }
}

function seatIdsMatchBuiltCards() {
  const builtIds = Object.keys(seatCardReferences);
  const currentIds = seatsOfRoom().map((seat) => seat.id);
  return builtIds.length === currentIds.length && currentIds.every((seatId) => builtIds.includes(seatId));
}

function renderSeats(forceEditorSync = false) {
  if (!state.room) return;
  if (!seatIdsMatchBuiltCards()) {
    buildSeatCards();
    forceEditorSync = true;
  }
  const ended = isRoomEnded();
  for (const seat of state.room.seats) {
    const references = seatCardReferences[seat.id];
    const seatState = computeSeatState(seat);
    const bid = state.lastBids[seat.id];
    const scoreFraction = bid ? bid.score / MAX_BID_SCORE : 0;

    references.card.dataset.state = seatState;
    references.card.style.setProperty('--score', String(scoreFraction));
    references.card.style.setProperty('--threshold', String(state.threshold / MAX_BID_SCORE));
    setSeatColor(references.card, seat.color);
    references.nameText.textContent = seat.name;
    references.programText.textContent = `${seat.program} · ${seat.model || 'default model'}`;
    references.meter.setAttribute('aria-valuenow', bid ? String(bid.score) : '0');
    references.scoreText.textContent = bid ? bid.score.toFixed(1) : '–';
    references.scoreText.title = bid ? `Jev scored ${bid.rawScore.toFixed(1)}; with eagerness ${seat.eagerness} the bid is ${bid.score.toFixed(1)}` : 'No bid yet';
    references.eagernessText.textContent = `eagerness ${seat.eagerness}`;
    references.eagernessText.title = describeEagerness(seat.eagerness);
    references.statusText.textContent = describeSeatState(seat, seatState);
    references.wakeButton.disabled = seatState === 'unavailable' || seatState === 'disabled' || !isConnected() || ended;
    references.editor.hidden = !state.expandedSeatIds.has(seat.id);
    references.editButton.setAttribute('aria-expanded', String(!references.editor.hidden));
    references.editButton.textContent = references.editor.hidden ? 'Edit' : 'Close';
    if (!references.editor.hidden) {
      syncSeatEditor(seat, references.fields, forceEditorSync);
    }

    const chip = seatChipReferences[seat.id];
    chip.element.dataset.state = seatState;
    chip.nameText.textContent = seat.name;
    setSeatColor(chip.element, seat.color);
    chip.fill.style.transform = `scaleX(${scoreFraction})`;
  }
}

function buildSeatCards() {
  seatCardReferences = {};
  seatChipReferences = {};
  elements.seatList.replaceChildren();
  elements.seatStrip.replaceChildren();

  for (const seat of seatsOfRoom()) {
    const card = createElement('div', 'seat-card');
    card.dataset.seat = seat.id;

    const nameText = createElement('span', 'seat-name');
    const programText = createElement('span', 'seat-program');
    const head = createElement('div', 'seat-head');
    head.append(nameText, programText);

    const meter = createElement('div', 'seat-meter');
    meter.setAttribute('role', 'meter');
    meter.setAttribute('aria-label', `${seat.name}'s last bid`);
    meter.setAttribute('aria-valuemin', '0');
    meter.setAttribute('aria-valuemax', String(MAX_BID_SCORE));
    meter.append(createElement('span', 'seat-meter-fill'), createElement('span', 'seat-meter-threshold'));

    const scoreText = createElement('span', 'seat-score', '–');
    const statusText = createElement('span', 'seat-status');
    const eagernessText = createElement('span', 'seat-eagerness small mute');
    const wakeButton = createButton('Wake', 'btn btn--ghost btn--small', () => sendFrame({ type: 'wake', seat: seat.id }));
    const editButton = createButton('Edit', 'btn btn--ghost btn--small', () => toggleSeatEditor(seat.id));
    editButton.setAttribute('aria-expanded', 'false');
    const foot = createElement('div', 'seat-foot');
    foot.append(scoreText, statusText, eagernessText, wakeButton, editButton);

    const { editor, fields } = buildSeatEditor(seat);
    card.append(head, meter, foot, editor);
    elements.seatList.append(card);

    const chipElement = createElement('li', 'seat-chip');
    const chipName = createElement('span', '', seat.name);
    const chipFill = createElement('span', 'seat-meter-fill');
    const chipMeter = createElement('span', 'seat-meter');
    chipMeter.style.width = '36px';
    chipMeter.style.marginTop = '0';
    chipMeter.append(chipFill);
    chipElement.append(chipName, chipMeter);
    chipElement.addEventListener('click', () => toggleDrawer());
    elements.seatStrip.append(chipElement);

    seatCardReferences[seat.id] = { card, nameText, programText, meter, scoreText, statusText, eagernessText, wakeButton, editButton, editor, fields };
    seatChipReferences[seat.id] = { element: chipElement, nameText: chipName, fill: chipFill };
  }
}

function toggleSeatEditor(seatId) {
  if (state.expandedSeatIds.has(seatId)) {
    state.expandedSeatIds.delete(seatId);
  } else {
    state.expandedSeatIds.add(seatId);
  }
  renderSeats(true);
}

/** `labelTarget` is the control the label points at when it differs from the displayed element. */
function labelledField(labelText, control, labelTarget = control) {
  const wrapper = createElement('div');
  const label = createElement('label', '', labelText);
  labelTarget.id = labelTarget.id || `field-${Math.random().toString(36).slice(2, 9)}`;
  label.htmlFor = labelTarget.id;
  wrapper.append(label, control);
  return wrapper;
}

function sendSeatPatch(seatId, patch) {
  sendFrame({ type: 'seat', seat: seatId, patch });
}

/** Build the editor for one seat. Every control sends its own `seat` frame when changed. */
function buildSeatEditor(seat) {
  const editor = createElement('div', 'seat-editor');
  const harness = findHarness(state.harnesses, seat.program);

  const nameInput = document.createElement('input');
  nameInput.type = 'text';
  nameInput.addEventListener('change', () => {
    const newName = nameInput.value.trim();
    if (newName) sendSeatPatch(seat.id, { name: newName });
  });

  const datalist = document.createElement('datalist');
  datalist.id = `models-${seat.id}`;
  fillDatalist(datalist, harness);
  const modelInput = document.createElement('input');
  modelInput.type = 'text';
  modelInput.setAttribute('list', datalist.id);
  modelInput.placeholder = harness && harness.default_model ? `default: ${harness.default_model}` : 'harness default';
  modelInput.addEventListener('change', () => sendSeatPatch(seat.id, { model: modelInput.value.trim() || null }));

  const thinkingSelect = document.createElement('select');
  fillThinkingSelect(thinkingSelect, harness, seat.thinking);
  thinkingSelect.addEventListener('change', () => sendSeatPatch(seat.id, { thinking: thinkingSelect.value }));

  const personaSelect = document.createElement('select');
  fillPersonaSelect(personaSelect, state.personas, seat.persona);
  personaSelect.addEventListener('change', () => {
    // A seat that loses its persona has no voice left, so the instruction card opens.
    if (!personaSelect.value) state.instructionOpenSeatIds.add(seat.id);
    sendSeatPatch(seat.id, { persona: personaSelect.value || null });
  });

  const instruction = createInstructionCard('', null, (text) => sendSeatPatch(seat.id, { instruction: text.trim() || null }));
  const instructionToggle = createButton('Add instruction', 'btn btn--ghost btn--small', () => {
    if (state.instructionOpenSeatIds.has(seat.id)) {
      state.instructionOpenSeatIds.delete(seat.id);
    } else {
      state.instructionOpenSeatIds.add(seat.id);
    }
    renderSeats(true);
  });

  const eagerness = createEagernessControl(
    seat.eagerness,
    null,
    (value) => sendSeatPatch(seat.id, { eagerness: value }),
  );

  const colorInput = document.createElement('input');
  colorInput.type = 'color';
  colorInput.addEventListener('change', () => sendSeatPatch(seat.id, { color: colorInput.value }));

  const enabledInput = document.createElement('input');
  enabledInput.type = 'checkbox';
  enabledInput.addEventListener('change', () => sendSeatPatch(seat.id, { enabled: enabledInput.checked }));
  const enabledLabel = createElement('label');
  enabledLabel.append(enabledInput, ' Enabled');

  const colorLabel = createElement('label');
  colorLabel.append('Colour ', colorInput);
  const toggles = createElement('div', 'row');
  toggles.append(colorLabel, enabledLabel);

  const removeButton = createButton('Remove seat', 'btn btn--ghost btn--small', () => removeSeat(seat.id));
  const programNote = createElement('p', 'small mute');

  editor.append(
    labelledField('Name', nameInput),
    labelledField('Model', modelInput),
    datalist,
    labelledField('Thinking', thinkingSelect),
    labelledField('Persona', personaSelect),
    labelledField('Eagerness', eagerness.element, eagerness.input),
    instructionToggle,
    instruction.card,
    toggles,
    programNote,
    removeButton,
  );
  editor.hidden = true;

  const fields = { eagerness, nameInput, modelInput, thinkingSelect, personaSelect, instruction, instructionToggle, colorInput, enabledInput, programNote };
  return { editor, fields };
}

/** Copy the room's truth into the editor, except into a control the owner is typing in. */
function syncSeatEditor(seat, fields, force) {
  const setIfIdle = (control, setter) => {
    if (force || document.activeElement !== control) setter();
  };
  setIfIdle(fields.nameInput, () => (fields.nameInput.value = seat.name));
  setIfIdle(fields.modelInput, () => (fields.modelInput.value = seat.model || ''));
  setIfIdle(fields.thinkingSelect, () => (fields.thinkingSelect.value = seat.thinking));
  setIfIdle(fields.personaSelect, () => (fields.personaSelect.value = seat.persona || ''));
  setIfIdle(fields.instruction.textarea, () => (fields.instruction.textarea.value = seat.instruction || ''));
  setIfIdle(fields.eagerness.input, () => fields.eagerness.setValue(seat.eagerness));
  setIfIdle(fields.colorInput, () => (fields.colorInput.value = safeColor(seat.color)));
  setIfIdle(fields.enabledInput, () => (fields.enabledInput.checked = seat.enabled));

  const mustShowInstruction = !seat.persona || Boolean(seat.instruction);
  const showInstruction = mustShowInstruction || state.instructionOpenSeatIds.has(seat.id);
  fields.instruction.card.hidden = !showInstruction;
  fields.instructionToggle.hidden = mustShowInstruction;
  fields.instructionToggle.textContent = showInstruction ? 'Hide instruction' : 'Add instruction';
  fields.programNote.textContent = `Harness: ${seat.program}${seat.harness_version ? ' ' + seat.harness_version : ''}. Edits apply on the next turn.`;
}

async function removeSeat(seatId) {
  const seat = seatById(seatId);
  if (!seat || !confirm(`Remove ${seat.name} from this room? Its messages stay.`)) return;
  try {
    const room = await fetchJson(`/api/rooms/${state.roomId}/seats/${seatId}`, { method: 'DELETE' });
    state.expandedSeatIds.delete(seatId);
    handleRoomFrame(room);
  } catch (error) {
    reportError('Could not remove the seat', error);
  }
}

// ----- add a seat -----

function buildAddSeatForm() {
  const form = elements.addSeatForm;
  form.replaceChildren();
  const firstFound = state.harnesses.find((harness) => harness.found);
  const colorDefault = paletteColor(seatsOfRoom().length);

  const nameInput = document.createElement('input');
  nameInput.type = 'text';
  nameInput.required = true;
  const harnessSelect = document.createElement('select');
  fillHarnessSelect(harnessSelect, state.harnesses, firstFound ? firstFound.program : 'claude');
  const datalist = document.createElement('datalist');
  datalist.id = 'add-seat-models';
  const modelInput = document.createElement('input');
  modelInput.type = 'text';
  modelInput.setAttribute('list', datalist.id);
  const thinkingSelect = document.createElement('select');
  const personaSelect = document.createElement('select');
  fillPersonaSelect(personaSelect, state.personas, '');
  const instruction = createInstructionCard('', null, null);
  const colorInput = document.createElement('input');
  colorInput.type = 'color';
  colorInput.value = colorDefault;
  const eagerness = createEagernessControl(EAGERNESS_DEFAULT);

  const refreshForHarness = () => {
    const harness = findHarness(state.harnesses, harnessSelect.value);
    fillDatalist(datalist, harness);
    fillThinkingSelect(thinkingSelect, harness, 'default');
    modelInput.placeholder = harness && harness.default_model ? `default: ${harness.default_model}` : 'harness default';
  };
  harnessSelect.addEventListener('change', refreshForHarness);
  refreshForHarness();

  const submitButton = createElement('button', 'btn btn--accent btn--small', 'Add');
  submitButton.type = 'submit';
  const cancelButton = createButton('Cancel', 'btn btn--ghost btn--small', () => {
    form.hidden = true;
  });
  const actions = createElement('div', 'side-actions');
  actions.append(submitButton, cancelButton);

  form.append(
    labelledField('Name', nameInput),
    labelledField('Harness', harnessSelect),
    labelledField('Model', modelInput),
    datalist,
    labelledField('Thinking', thinkingSelect),
    labelledField('Persona', personaSelect),
    labelledField('Custom instruction', instruction.card),
    labelledField('Colour', colorInput),
    labelledField('Eagerness', eagerness.element, eagerness.input),
    actions,
  );

  form.onsubmit = async (event) => {
    event.preventDefault();
    const seat = {
      name: nameInput.value.trim(),
      program: harnessSelect.value,
      thinking: thinkingSelect.value,
      color: colorInput.value,
      eagerness: Number(eagerness.input.value),
    };
    if (modelInput.value.trim()) seat.model = modelInput.value.trim();
    if (personaSelect.value) seat.persona = personaSelect.value;
    if (instruction.textarea.value.trim()) seat.instruction = instruction.textarea.value.trim();
    try {
      const room = await sendJson('POST', `/api/rooms/${state.roomId}/seats`, seat);
      form.hidden = true;
      handleRoomFrame(room);
    } catch (error) {
      reportError('Could not add the seat', error);
    }
  };
}

function toggleAddSeatForm() {
  if (elements.addSeatForm.hidden) {
    buildAddSeatForm();
    elements.addSeatForm.hidden = false;
    elements.addSeatForm.querySelector('input').focus();
  } else {
    elements.addSeatForm.hidden = true;
  }
}

/** Remove then re-add the class, with a forced reflow between, so the CSS animation restarts. */
function animateBidCards(seatIds) {
  for (const seatId of seatIds) {
    const references = seatCardReferences[seatId];
    if (!references) continue;
    references.card.classList.remove('is-bidding');
    void references.card.offsetWidth;
    references.card.classList.add('is-bidding');
    setTimeout(() => references.card.classList.remove('is-bidding'), BID_ANIMATION_MS);
  }
}

function ensureCooldownTicker() {
  if (cooldownIntervalId !== null) return;
  cooldownIntervalId = setInterval(() => {
    renderSeats();
    if (!seatsOfRoom().some((seat) => cooldownSecondsLeft(seat.id) > 0)) {
      clearInterval(cooldownIntervalId);
      cooldownIntervalId = null;
    }
  }, 1000);
}

// ---------------------------------------------------------------------------
// 6. Composer, @ menu, search, drawer, keyboard
// ---------------------------------------------------------------------------

function renderComposerState() {
  const ended = isRoomEnded();
  const connected = isConnected();
  elements.composerInput.disabled = ended;
  elements.sendButton.disabled = ended || !connected;
  elements.stopButton.disabled = ended || !connected || state.typingSeatIds.size === 0;
  elements.endButton.disabled = ended || !connected || state.endRequested || state.room === null;
  if (ended) {
    elements.composerHint.textContent = 'This discussion has ended.';
  } else if (!connected) {
    elements.composerHint.textContent = "Not connected. Messages can't be sent.";
  } else {
    elements.composerHint.textContent = defaultComposerHint;
  }
  elements.wakeAllButton.disabled = ended || !connected;
}

function autoGrowComposer() {
  const input = elements.composerInput;
  input.style.height = 'auto';
  input.style.height = Math.min(input.scrollHeight, window.innerHeight * COMPOSER_MAX_VIEWPORT_FRACTION) + 'px';
}

function submitComposer() {
  const text = elements.composerInput.value.trim();
  if (text === '' || isRoomEnded()) return;
  // No optimistic render: the server echoes a message frame to every tab.
  if (!sendFrame({ type: 'say', text })) return;
  elements.composerInput.value = '';
  autoGrowComposer();
  hideMentionMenu();
}

function requestStop() {
  if (state.typingSeatIds.size > 0) sendFrame({ type: 'stop' });
}

function requestEnd() {
  if (!confirm('End this discussion? Each seat with a persona will write memory notes.')) return;
  if (sendFrame({ type: 'end' })) {
    state.endRequested = true;
    renderComposerState();
  }
}

// ----- @ menu -----

function findMentionToken() {
  const input = elements.composerInput;
  const textBeforeCaret = input.value.slice(0, input.selectionStart);
  const match = /(^|\s)@([a-z0-9_*-]*)$/i.exec(textBeforeCaret);
  if (!match) return null;
  return { partial: match[2], tokenStart: match.index + match[1].length };
}

/** `@all`, then each seat, then `@prefix*` for every prefix two or more seats share. */
function buildMentionCandidates() {
  const seats = seatsOfRoom();
  const enabledCount = seats.filter((seat) => seat.enabled && seat.available).length;
  const candidates = [
    {
      key: 'all',
      insert: '@all',
      label: '@all',
      description: `every seat answers, in roster order · ${enabledCount} ${enabledCount === 1 ? 'turn' : 'turns'}`,
      color: null,
    },
  ];
  for (const seat of seats) {
    const seatState = describeSeatState(seat, computeSeatState(seat)).toLowerCase();
    candidates.push({
      key: seat.handle,
      insert: '@' + removeWhitespace(seat.name),
      label: '@' + removeWhitespace(seat.name),
      description: `${personaRoleOf(seat)} · ${seatState}`,
      color: seat.color,
    });
  }
  const seatsByPrefix = {};
  for (const seat of seats) {
    const displayPrefix = removeWhitespace(seat.name).replace(/\d+$/, '');
    if (!displayPrefix) continue;
    const prefixKey = displayPrefix.toLowerCase();
    seatsByPrefix[prefixKey] = seatsByPrefix[prefixKey] || { displayPrefix, members: [] };
    seatsByPrefix[prefixKey].members.push(seat);
  }
  for (const [prefixKey, group] of Object.entries(seatsByPrefix)) {
    if (group.members.length < 2) continue;
    candidates.push({
      key: `${prefixKey}*`,
      insert: `@${group.displayPrefix}*`,
      label: `@${group.displayPrefix}*`,
      description: `all ${group.members.length} ${group.displayPrefix} seats`,
      color: group.members[0].color,
    });
  }
  return candidates;
}

function updateMentionMenu() {
  const token = findMentionToken();
  if (!token || !state.room) {
    hideMentionMenu();
    return;
  }
  const lowerPartial = token.partial.toLowerCase();
  const items = buildMentionCandidates().filter((candidate) => candidate.key.startsWith(lowerPartial));
  if (items.length === 0) {
    hideMentionMenu();
    return;
  }
  const previous = state.mentionMenu.items[state.mentionMenu.activeIndex];
  const keptIndex = previous ? items.findIndex((item) => item.key === previous.key) : -1;
  state.mentionMenu = { open: true, items, activeIndex: keptIndex >= 0 ? keptIndex : 0, tokenStart: token.tokenStart };
  renderMentionMenu();
}

function hideMentionMenu() {
  if (!state.mentionMenu.open && elements.mentionMenu.hidden) return;
  state.mentionMenu = { open: false, items: [], activeIndex: 0, tokenStart: 0 };
  renderMentionMenu();
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
  menu.items.forEach((item, index) => {
    const option = createElement('li', 'mention-option');
    option.id = `mention-option-${index}`;
    option.setAttribute('role', 'option');
    option.setAttribute('aria-selected', String(index === menu.activeIndex));
    if (item.color) setSeatColor(option, item.color);
    if (item.color) {
      const swatch = createElement('span', 'dot');
      swatch.setAttribute('aria-hidden', 'true');
      option.append(swatch);
    }
    option.append(createElement('span', 'key', item.label), createElement('span', 'small mute', item.description));
    // mousedown, not click: click fires after the textarea has already lost focus.
    option.addEventListener('mousedown', (event) => {
      event.preventDefault();
      insertMention(item);
    });
    elements.mentionMenu.append(option);
  });
  elements.composerInput.setAttribute('aria-activedescendant', `mention-option-${menu.activeIndex}`);
}

function insertMention(item) {
  const input = elements.composerInput;
  const before = input.value.slice(0, state.mentionMenu.tokenStart);
  const after = input.value.slice(input.selectionStart);
  const insertion = `${item.insert} `;
  input.value = before + insertion + after;
  const caret = before.length + insertion.length;
  input.setSelectionRange(caret, caret);
  hideMentionMenu();
  autoGrowComposer();
  input.focus();
}

function handleComposerKeydown(event) {
  const menu = state.mentionMenu;
  if (menu.open) {
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      const step = event.key === 'ArrowDown' ? 1 : -1;
      menu.activeIndex = (menu.activeIndex + step + menu.items.length) % menu.items.length;
      renderMentionMenu();
      return;
    }
    if ((event.key === 'Enter' || event.key === 'Tab') && !event.isComposing) {
      event.preventDefault();
      insertMention(menu.items[menu.activeIndex]);
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

function appendHighlighted(parent, text, query) {
  const words = query.split(/\s+/).filter((word) => word.length > 0);
  if (words.length === 0) {
    parent.append(text);
    return;
  }
  const escaped = words.map((word) => word.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'));
  // With one capture group, matches land at odd indexes of the split result.
  text.split(new RegExp(`(${escaped.join('|')})`, 'gi')).forEach((piece, index) => {
    if (piece === '') return;
    parent.append(index % 2 === 1 ? createElement('mark', '', piece) : piece);
  });
}

function renderSearchResults(query, results) {
  elements.searchResults.replaceChildren();
  elements.searchResults.hidden = false;
  if (results.length === 0) {
    elements.searchResults.append(createElement('p', 'search-result mute', 'No messages match.'));
    return;
  }
  for (const result of results) {
    const message = result.message;
    const link = createElement('a', 'search-result');
    link.href = `/room/${message.room_id}#m-${message.id}`;
    const snippet = createElement('div', 'small');
    appendHighlighted(snippet, result.snippet, query);
    const author = message.role === 'owner' ? state.ownerName : message.author;
    link.append(
      createElement('b', '', result.room_title),
      snippet,
      createElement('div', 'small mute', `${author} · ${formatDay(message.created_at)}`),
    );
    if (message.room_id === state.roomId) {
      link.addEventListener('click', (event) => {
        event.preventDefault();
        hideSearchResults();
        elements.searchInput.value = '';
        scrollToMessage(message.id);
      });
    }
    elements.searchResults.append(link);
  }
}

async function runSearch(query) {
  searchRequestCounter += 1;
  const requestNumber = searchRequestCounter;
  try {
    const results = await fetchJson('/api/search?q=' + encodeURIComponent(query));
    if (requestNumber === searchRequestCounter) renderSearchResults(query, results);
  } catch (error) {
    if (requestNumber === searchRequestCounter) reportError('Search failed', error);
  }
}

// ----- drawer for narrow layouts -----

function isDrawerOpen() {
  return document.body.classList.contains('show-panel');
}

function setDrawer(open) {
  document.body.classList.toggle('show-panel', open);
  elements.panelToggle.setAttribute('aria-expanded', String(open));
  elements.drawerScrim.hidden = !open;
}

function toggleDrawer() {
  setDrawer(!isDrawerOpen());
}

// ----- keyboard -----

function handleDocumentKeydown(event) {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') {
    event.preventDefault();
    elements.searchInput.focus();
    elements.searchInput.select();
    return;
  }
  if (event.key !== 'Escape' || elements.endedDialog.open) return;
  if (!elements.searchResults.hidden) {
    elements.searchInput.value = '';
    hideSearchResults();
    return;
  }
  if (isDrawerOpen()) {
    setDrawer(false);
    return;
  }
  requestStop();
}

// ---------------------------------------------------------------------------
// 7. Startup
// ---------------------------------------------------------------------------

function bindEvents() {
  elements.messageScroller.addEventListener('scroll', () => {
    state.ownerScrolledUp = !isNearBottom();
    if (!state.ownerScrolledUp) hideJumpLatest();
  });
  elements.jumpLatest.addEventListener('click', () => {
    state.ownerScrolledUp = false;
    hideJumpLatest();
    scrollMessagesToBottom(!prefersReducedMotion());
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
  elements.copyThreadButton.addEventListener('click', copyThread);

  // The slider previews its sentence while dragging and sends one patch when released.
  elements.liveliness.addEventListener('input', () => {
    const value = Number(elements.liveliness.value);
    elements.livelinessValue.textContent = value;
    elements.livelinessSentence.textContent = describeLiveliness(value, countAutomaticTurns());
  });
  elements.liveliness.addEventListener('change', () => sendRoomPatch({ liveliness: Number(elements.liveliness.value) }));
  elements.budget.addEventListener('change', () => {
    const budget = Math.max(0, Number(elements.budget.value) || 0);
    sendRoomPatch({ budget_usd: budget });
  });
  elements.modeOptions.addEventListener('change', (event) => {
    if (event.target.name === 'mode') sendRoomPatch({ mode: event.target.value });
  });
  elements.pauseButton.addEventListener('click', togglePause);
  elements.wakeAllButton.addEventListener('click', () => sendFrame({ type: 'wake_all' }));
  elements.addSeatButton.addEventListener('click', toggleAddSeatForm);

  elements.searchForm.addEventListener('submit', (event) => {
    event.preventDefault();
    const query = elements.searchInput.value.trim();
    if (query) {
      clearTimeout(searchDebounceTimerId);
      runSearch(query);
    }
  });
  elements.searchInput.addEventListener('input', () => {
    clearTimeout(searchDebounceTimerId);
    const query = elements.searchInput.value.trim();
    if (!query) {
      hideSearchResults();
      return;
    }
    searchDebounceTimerId = setTimeout(() => runSearch(query), SEARCH_DEBOUNCE_MS);
  });

  elements.panelToggle.addEventListener('click', toggleDrawer);
  elements.drawerScrim.addEventListener('click', () => setDrawer(false));
  document.addEventListener('keydown', handleDocumentKeydown);
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible' && socketClient) socketClient.reconnectNow();
  });
}

async function loadLibraries() {
  try {
    const [harnesses, personas, settings] = await Promise.all([
      fetchJson('/api/harnesses'),
      fetchJson('/api/personas'),
      fetchJson('/api/settings'),
    ]);
    state.harnesses = harnesses;
    state.personas = personas;
    state.cooldownSeconds = settings.room_defaults.cooldown_s;
  } catch (error) {
    reportError('Could not load harnesses and personas', error);
  }
}

async function start() {
  bindEvents();
  renderConnection();
  const hashMatch = /^#m-(.+)$/.exec(location.hash);
  state.pendingHighlightId = hashMatch ? hashMatch[1] : null;
  await loadLibraries();
  socketClient = createSocketClient({
    url: `/ws/${encodeURIComponent(state.roomId)}`,
    onFrame: handleFrame,
    onStatus: handleStatus,
  });
  socketClient.connect();
}

start();
