/*
 * Room page (SPEC section 8 and the v0.5 delta). A room is a project: it owns the seats, the
 * folder and the budget; the discussion happens in one of its threads, and the page shows one
 * thread at a time over `WS /ws/threads/{thread_id}`. Regions, top to bottom:
 *   1. Constants and state
 *   2. Small lookups (seats, colours, labels)
 *   3. Frame handlers, one per server frame type (SPEC section 6)
 *   4. Render functions, one per region of the page
 *   5. Seat cards and the seat editor
 *   6. Composer, @ menu, search, drawer, keyboard
 *   7. Threads: list, switching, new, rename
 *   8. Memory drawer and Open folder
 *   9. Startup
 *
 * Seats are identified by id everywhere; names are only shown. Message text reaches the DOM
 * through AddaMarkdown (which never uses innerHTML) or textContent.
 */

'use strict';

// ---------------------------------------------------------------------------
// 1. Constants and state
// ---------------------------------------------------------------------------

const MAX_BID_SCORE = 10;
const DEFAULT_MAX_AUTO_TURNS = 40; // used only if /api/settings cannot be read
const DEFAULT_RESUME_LIVELINESS = 6;
const NEAR_BOTTOM_PIXELS = 80;
const HIGHLIGHT_DURATION_MS = 2500;
const BID_ANIMATION_MS = 900;
const SEARCH_DEBOUNCE_MS = 250;
const COMPOSER_MAX_VIEWPORT_FRACTION = 0.4;
const MODE_LABELS = { bidding: 'Automatic', roundrobin: 'Take turns', manual: 'Only when mentioned' };
const WOKEN_BY_LABELS = {
  mention: 'woken by mention',
  all: 'woken by @all',
  wake: 'woken by Wake',
  roundrobin: 'in turn',
  afterthought: 'afterthought',
  more: 'continued',
};
const DEFAULT_MAX_CONCURRENT = 2;
const QUIET_COUNTDOWN_TICK_MS = 1000;
const THREAD_LAST_LINE_LENGTH = 90;
const THIS_ROOM_TAB = 'this-room';
const CONNECTION_PILL_MODIFIERS = { open: 'pill--good', closed: 'pill--bad', connecting: 'pill--mute', reconnecting: 'pill--mute' };

/** `/room/{room_id}` or `/room/{room_id}/thread/{thread_id}`; anything else yields empty ids. */
function parseRoomPath(pathname) {
  const match = /^\/room\/([^/]+)(?:\/thread\/([^/]+))?\/?$/.exec(pathname);
  if (!match) return { roomId: '', threadId: null };
  return { roomId: decodeURIComponent(match[1]), threadId: match[2] ? decodeURIComponent(match[2]) : null };
}

const state = {
  roomId: parseRoomPath(location.pathname).roomId,
  threadId: null,
  room: null,
  thread: null, // ThreadSummary of the thread on screen
  threads: [], // ThreadSummary list of the room, newest first
  renamingThreadId: null,
  memoryTab: null, // seat id or THIS_ROOM_TAB
  editingNoteFile: null, // "slug/file" of the note being edited
  memoryView: { kind: 'none', seat: null }, // the notes last loaded into the drawer
  messages: [],
  ownerName: 'Owner',
  router: null,
  harnesses: [],
  personas: [],
  maxAutoTurns: DEFAULT_MAX_AUTO_TURNS,
  typingSeatIds: new Set(),
  lastBids: {}, // seat id -> {score, confidence}
  threshold: 5,
  chosenSeatIds: new Set(),
  lastBidsKind: 'message',
  cooldownEndsAt: {}, // seat id -> epoch ms
  quietReason: null,
  quietFrame: null, // the last quiet frame, with quietReceivedAt for the live countdown
  quietReceivedAt: 0,
  quietTimer: null,
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
  threadTitle: document.getElementById('thread-title'),
  meta: document.getElementById('room-meta'),
  folder: document.getElementById('room-folder'),
  openFolderButton: document.getElementById('open-folder-button'),
  roomState: document.getElementById('room-state'),
  threadsToggle: document.getElementById('threads-toggle'),
  threadNewForm: document.getElementById('thread-new-form'),
  threadNewTitle: document.getElementById('thread-new-title'),
  threadNewButton: document.getElementById('thread-new-button'),
  threadList: document.getElementById('thread-list'),
  memoryButton: document.getElementById('memory-button'),
  memoryDialog: document.getElementById('memory-dialog'),
  memoryCloseButton: document.getElementById('memory-close-button'),
  memoryTabs: document.getElementById('memory-tabs'),
  memoryPanel: document.getElementById('memory-panel'),
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
  maxConcurrent: document.getElementById('max-concurrent-select'),
  access: document.getElementById('access-select'),
  accessDescription: document.getElementById('access-description'),
  accessWarning: document.getElementById('access-warning'),
  liveliness: document.getElementById('liveliness-input'),
  livelinessValue: document.getElementById('liveliness-value'),
  livelinessSentence: document.getElementById('liveliness-sentence'),
  budget: document.getElementById('budget-input'),
  cooldown: document.getElementById('cooldown-input'),
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

/** An archived room takes no edits to its seats or settings. */
function isRoomArchived() {
  return Boolean(state.room && state.room.ended_at);
}

/** Nothing more can be said in an ended thread or in any thread of an archived room. */
function isThreadClosed() {
  return isRoomArchived() || Boolean(state.thread && state.thread.ended_at);
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
  // The connection pill already reads Offline or Reconnecting, so the owner gets no toast.
  console.warn('Not connected; frame not sent', frame);
  return false;
}

// ---------------------------------------------------------------------------
// 3. Frame handlers
// ---------------------------------------------------------------------------

function handleFrame(frame) {
  const keepsQuietNotice = ['quiet', 'typing', 'bids', 'room', 'passed', 'thread'];
  if (!keepsQuietNotice.includes(frame.type)) {
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
      handleQuietFrame(frame);
      break;
    case 'ended':
      handleEndedFrame(frame);
      break;
    case 'thread':
      handleThreadFrame(frame.thread);
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

function handleQuietFrame(frame) {
  state.quietReason = frame.reason;
  state.quietFrame = frame;
  state.quietReceivedAt = Date.now();
  ensureQuietTimer();
  renderStatusLine();
}

/** Re-render once a second while a countdown is showing; stop when nothing is counting. */
function ensureQuietTimer() {
  const counting = state.quietReason && state.quietFrame && typeof state.quietFrame.next_tick_in_s === 'number';
  if (counting && !state.quietTimer) {
    state.quietTimer = setInterval(renderStatusLine, QUIET_COUNTDOWN_TICK_MS);
  } else if (!counting && state.quietTimer) {
    clearInterval(state.quietTimer);
    state.quietTimer = null;
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
  if (Array.isArray(frame.room.threads)) {
    state.threads = frame.room.threads;
  }
  if (frame.thread) {
    state.thread = frame.thread;
    upsertThreadSummary(frame.thread);
  }
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
  const cooldownSeconds = state.room ? state.room.cooldown_s : 0;
  if (message.role === 'seat' && message.seat_id && cooldownSeconds > 0) {
    state.cooldownEndsAt[message.seat_id] = Date.now() + cooldownSeconds * 1000;
    ensureCooldownTicker();
  }
  // An owner message clears every seat's cooldown on the server.
  if (message.role === 'owner') state.cooldownEndsAt = {};
  state.chosenSeatIds.clear();
  state.quietReason = null;
  state.messages.push(message);
  appendMessageElement(message);
  renderRoomMeta();
  renderThreadList();
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
    state.chosenSeatIds.delete(frame.seat);
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
  state.chosenSeatIds = new Set(Array.isArray(frame.chosen) ? frame.chosen : [frame.chosen].filter(Boolean));
  state.lastBidsKind = frame.kind || 'message';
  state.threshold = frame.threshold;
  state.router = frame.router;
  renderSeats();
  renderRouterName();
  animateBidCards(Object.keys(bidsBySeat));
}

function handleRoomFrame(room) {
  const namesChanged = renameMessageAuthors(room);
  state.room = room;
  if (Array.isArray(room.threads)) {
    state.threads = room.threads;
    const current = state.threads.find((thread) => thread.id === state.threadId);
    if (current) state.thread = current;
  }
  renderRoomMeta();
  renderThreadList();
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
  // The `ended` frame belongs to the thread on screen; the room itself stays open.
  if (state.thread && !state.thread.ended_at) {
    state.thread = { ...state.thread, ended_at: new Date().toISOString() };
    upsertThreadSummary(state.thread);
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
  renderThreadList();
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

function describeThreadState() {
  if (isRoomArchived()) return 'Archived';
  if (state.thread && state.thread.ended_at) return 'Thread ended';
  return describeRoomState(state.room);
}

/** Heading: room title, then the thread title. Meta: mode, counts, spend, state. The folder lives in the panel settings. */
function renderRoomMeta() {
  const room = state.room;
  if (!room) return;
  const threadTitle = state.thread ? state.thread.title : '';
  elements.title.textContent = room.title;
  // The first thread is named after the room; repeating that title in the heading says nothing.
  const showsThreadTitle = threadTitle && threadTitle !== room.title;
  elements.threadTitle.textContent = showsThreadTitle ? threadTitle : '';
  elements.threadTitle.hidden = !showsThreadTitle;
  document.title = showsThreadTitle ? `${threadTitle} · ${room.title} · Adda` : `${room.title} · Adda`;
  const persisted = state.messages.filter((message) => !message.isLocal);
  const budgetText = room.budget_usd > 0 ? ` of ${formatMoney(room.budget_usd)}` : ' (no budget limit)';
  elements.meta.textContent = MODE_LABELS[room.mode] || room.mode;
  elements.folder.textContent = room.folder || 'No folder set';
  elements.folder.title = room.folder || '';
  elements.openFolderButton.hidden = !room.folder;
  const after = [
    `${persisted.length} ${persisted.length === 1 ? 'message' : 'messages'} in this thread`,
    `${formatMoney(room.spent_usd)}${budgetText}`,
    describeThreadState(),
  ];
  elements.roomState.textContent = ` · ${after.join(' · ')}`;
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
  elements.connection.className = `pill connection ${CONNECTION_PILL_MODIFIERS[connectionState] || 'pill--mute'}`;
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

/** 3100 -> "3.1k", 420 -> "420". */
function formatTokenCount(count) {
  return count < 1000 ? String(count) : `${(count / 1000).toFixed(1)}k`;
}

/** "3.1k cached · 0.4k new · 0.2k out"; empty when the tool reported no tokens. */
function describeTokens(meta) {
  const parts = [];
  const { input_tokens: input, cached_input_tokens: cached, output_tokens: output } = meta;
  if (typeof input === 'number') {
    const cachedCount = typeof cached === 'number' ? cached : 0;
    if (typeof cached === 'number') parts.push(`${formatTokenCount(cachedCount)} cached`);
    parts.push(`${formatTokenCount(Math.max(0, input - cachedCount))} new`);
  }
  if (typeof output === 'number') parts.push(`${formatTokenCount(output)} out`);
  return parts.join(' · ');
}

function describeMessageMeta(message) {
  const parts = [formatClock(message.created_at)];
  const meta = message.meta || {};
  if (typeof message.cost_usd === 'number') {
    // A subscription seat is not charged per reply; its cost is what the same reply would cost on the API.
    const costText = formatMoney(message.cost_usd);
    parts.push(meta.billing === 'subscription' ? `${costText} at API prices` : costText);
  }
  const tokensText = describeTokens(meta);
  if (tokensText) parts.push(tokensText);
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
  // Focusable so a tap on a touch screen reveals the tools through :focus-within.
  article.tabIndex = 0;
  const color = isOwner ? null : messageColor(message);
  if (color) setSeatColor(article, color);

  const authorName = isOwner ? state.ownerName : message.author;
  const head = createElement('div', 'msg-head');
  head.append(createElement('span', 'msg-who', authorName));
  const seat = isOwner ? null : seatById(message.seat_id) || seatsOfRoom().find((candidate) => candidate.name === message.author);
  if (seat) head.append(createElement('span', 'msg-harness', seat.program));
  head.append(createElement('span', 'msg-meta', describeMessageMeta(message)));

  const tools = createElement('div', 'msg-tools');
  tools.append(createButton('Copy', 'btn', (event) => copyMessage(message, event.currentTarget)));
  tools.append(createButton('Quote', 'btn', () => quoteMessage(message, authorName)));

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
  showToast(copied ? 'Thread copied.' : 'Copy failed.', !copied);
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

function describeQuietNotice() {
  const frame = state.quietFrame;
  if (state.quietReason === 'budget') {
    const room = state.room;
    return `Budget reached (${formatMoney(room.spent_usd)} of ${formatMoney(room.budget_usd)}). Raise it to continue.`;
  }
  if (state.quietReason === 'safety') {
    return `Paused after ${state.maxAutoTurns} replies without you. Say something to continue.`;
  }
  if (!frame || typeof frame.next_tick_in_s !== 'number') return describeQuiet({ reason: state.quietReason });
  const elapsedSeconds = (Date.now() - state.quietReceivedAt) / 1000;
  return describeQuiet({ ...frame, next_tick_in_s: Math.max(0, frame.next_tick_in_s - elapsedSeconds) });
}

function renderStatusLine() {
  elements.statusLine.replaceChildren();
  const typingSeats = [...state.typingSeatIds].map((seatId) => seatById(seatId)).filter(Boolean);
  if (typingSeats.length > 0) {
    elements.statusLine.append(buildTypingIndicator(typingSeats));
  }
  if (state.quietReason && state.typingSeatIds.size === 0 && state.room) {
    elements.statusLine.append(createElement('span', 'quiet-notice', describeQuietNotice()));
  }
  ensureQuietTimer();
}

/** "Ravi is thinking", "Ravi and Tuli are thinking", "Ravi, Tuli and Pi are thinking". */
function buildTypingIndicator(seats) {
  const typing = createElement('span', 'typing');
  seats.forEach((seat, index) => {
    if (index > 0) typing.append(index === seats.length - 1 ? ' and ' : ', ');
    const seatPart = createElement('span', 'typing-seat');
    setSeatColor(seatPart, seat.color);
    const dots = createElement('span', 'typing-dots');
    dots.setAttribute('aria-hidden', 'true');
    dots.append(document.createElement('i'), document.createElement('i'), document.createElement('i'));
    seatPart.append(dots, createElement('span', 'typing-name', seat.name));
    typing.append(seatPart);
  });
  typing.append(seats.length === 1 ? ' is thinking' : ' are thinking');
  return typing;
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
  if (document.activeElement !== elements.maxConcurrent) {
    elements.maxConcurrent.value = String(room.max_concurrent ?? DEFAULT_MAX_CONCURRENT);
  }
  elements.maxConcurrent.disabled = isRoomArchived();
  renderRoomAccess(room);
  elements.livelinessValue.textContent = room.liveliness;
  elements.livelinessSentence.textContent = describeLiveliness(room.liveliness);
  if (document.activeElement !== elements.budget) elements.budget.value = room.budget_usd;
  if (document.activeElement !== elements.cooldown) elements.cooldown.value = room.cooldown_s;
  elements.spendText.textContent = `${formatMoney(room.spent_usd)} used`;
  for (const radio of elements.modeOptions.querySelectorAll('input[name="mode"]')) {
    radio.checked = radio.value === room.mode;
    radio.disabled = isRoomArchived();
  }
  elements.pauseButton.textContent = room.liveliness === 0 ? 'Resume' : 'Pause';
  elements.pauseButton.disabled = isRoomArchived();
  elements.wakeAllButton.disabled = isThreadClosed() || !isConnected();
  elements.addSeatButton.disabled = isRoomArchived();
}

function renderRoomAccess(room) {
  const access = room.access || 'talk';
  if (document.activeElement !== elements.access) elements.access.value = access;
  elements.access.disabled = isRoomArchived();
  elements.accessDescription.textContent = ACCESS_DESCRIPTIONS[access];
  elements.accessWarning.hidden = access !== 'work';
  elements.accessWarning.textContent = describeAccessWarning(room.folder);
}

function renderRouterName() {
  // Only the fallback is worth a line: it explains why turn-taking feels different.
  const isSimple = state.router === 'heuristic';
  elements.routerName.textContent = isSimple ? 'Simple turn-taking (no network)' : '';
  elements.routerName.hidden = !isSimple;
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

function clearCooldowns() {
  state.cooldownEndsAt = {};
  renderSeats();
}

function cooldownSecondsLeft(seatId) {
  const endsAt = state.cooldownEndsAt[seatId];
  return endsAt ? Math.max(0, Math.ceil((endsAt - Date.now()) / 1000)) : 0;
}

function limitedUntilDate(seat) {
  if (!seat.limited_until) return null;
  const until = new Date(seat.limited_until);
  return until.getTime() > Date.now() ? until : null;
}

function computeSeatState(seat) {
  if (!seat.available) return 'unavailable';
  if (!seat.enabled) return 'disabled';
  if (limitedUntilDate(seat)) return 'limited';
  if (state.typingSeatIds.has(seat.id)) return 'speaking';
  if (cooldownSecondsLeft(seat.id) > 0) return 'cooldown';
  if (state.chosenSeatIds.has(seat.id)) return 'chosen';
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
    case 'limited':
      return `Limit reached · back ${formatClock(limitedUntilDate(seat).toISOString())}`;
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
  const ended = isThreadClosed();
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
    const roundLabel = state.lastBidsKind === 'afterthought' ? 'Afterthought. ' : '';
    references.scoreText.title = bid
      ? `${roundLabel}Urge to speak ${bid.score.toFixed(1)}, ${bid.rawScore.toFixed(1)} before eagerness ${seat.eagerness}`
      : 'Quiet';
    references.eagernessText.textContent = `eagerness ${seat.eagerness}`;
    references.eagernessText.title = describeEagerness(seat.eagerness);
    references.statusText.textContent = describeSeatState(seat, seatState);
    renderAccessMarker(references.accessMarker, seat);
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

/** A seat that only talks needs no marker; look and work show up so the owner sees who has tools. */
function renderAccessMarker(marker, seat) {
  const level = seat.effective_access || 'talk';
  marker.hidden = level === 'talk';
  marker.textContent = level;
  marker.className = `access-marker access-marker--${level}`;
  marker.title = `${ACCESS_LABELS[level]}${seat.access ? ' (set on this seat)' : ' (room level)'}`;
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
    meter.setAttribute('aria-label', `${seat.name}'s urge to speak`);
    meter.setAttribute('aria-valuemin', '0');
    meter.setAttribute('aria-valuemax', String(MAX_BID_SCORE));
    meter.append(createElement('span', 'seat-meter-fill'), createElement('span', 'seat-meter-threshold'));

    const scoreText = createElement('span', 'seat-score', '–');
    const statusText = createElement('span', 'seat-status');
    const eagernessText = createElement('span', 'seat-eagerness small mute');
    const accessMarker = createElement('span', 'access-marker');
    const wakeButton = createButton('Wake', 'btn', () => {
      clearCooldowns();
      sendFrame({ type: 'wake', seat: seat.id });
    });
    const editButton = createButton('Edit', 'btn', () => toggleSeatEditor(seat.id));
    editButton.setAttribute('aria-expanded', 'false');
    const foot = createElement('div', 'seat-foot');
    foot.append(scoreText, statusText, eagernessText, accessMarker, wakeButton, editButton);

    const { editor, fields } = buildSeatEditor(seat);
    card.append(head, meter, foot, editor);
    elements.seatList.append(card);

    const chipElement = createElement('li', 'seat-chip');
    const chipName = createElement('span', 'seat-chip-name', seat.name);
    const chipFill = createElement('span', 'seat-meter-fill');
    const chipMeter = createElement('span', 'seat-meter');
    chipMeter.append(chipFill);
    chipElement.append(chipName, chipMeter);
    chipElement.addEventListener('click', () => toggleDrawer());
    elements.seatStrip.append(chipElement);

    seatCardReferences[seat.id] = { card, nameText, programText, meter, scoreText, statusText, eagernessText, accessMarker, wakeButton, editButton, editor, fields };
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
  const wrapper = createElement('div', 'field');
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

  const modelField = createModelField(harness, (model) => sendSeatPatch(seat.id, { model: model || null }));

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
  const instructionToggle = createButton('Add instruction', 'btn', () => {
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

  const accessSelect = document.createElement('select');
  fillAccessSelect(accessSelect, seat.access, 'Room level');
  accessSelect.addEventListener('change', () => sendSeatPatch(seat.id, { access: readAccessSelect(accessSelect) }));

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
  const toggles = createElement('div', 'seat-editor-toggles');
  toggles.append(colorLabel, enabledLabel);

  const removeButton = createButton('Remove seat', 'btn btn--warning', () => removeSeat(seat.id));
  const programNote = createElement('p', 'hint');

  editor.append(
    labelledField('Name', nameInput),
    labelledField('Model', modelField.element, modelField.input),
    labelledField('Thinking', thinkingSelect),
    labelledField('Persona', personaSelect),
    labelledField('Eagerness', eagerness.element, eagerness.input),
    labelledField('Access', accessSelect),
    instructionToggle,
    instruction.card,
    toggles,
    programNote,
    removeButton,
  );
  editor.hidden = true;

  const fields = { eagerness, accessSelect, nameInput, modelField, thinkingSelect, personaSelect, instruction, instructionToggle, colorInput, enabledInput, programNote };
  return { editor, fields };
}

/** Copy the room's truth into the editor, except into a control the owner is typing in. */
function syncSeatEditor(seat, fields, force) {
  const setIfIdle = (control, setter) => {
    if (force || document.activeElement !== control) setter();
  };
  setIfIdle(fields.nameInput, () => (fields.nameInput.value = seat.name));
  if (force || !fields.modelField.element.contains(document.activeElement)) fields.modelField.setValue(seat.model);
  setIfIdle(fields.thinkingSelect, () => (fields.thinkingSelect.value = seat.thinking));
  setIfIdle(fields.personaSelect, () => (fields.personaSelect.value = seat.persona || ''));
  setIfIdle(fields.instruction.textarea, () => (fields.instruction.textarea.value = seat.instruction || ''));
  setIfIdle(fields.eagerness.input, () => fields.eagerness.setValue(seat.eagerness));
  setIfIdle(fields.accessSelect, () => (fields.accessSelect.value = seat.access || ''));
  setIfIdle(fields.colorInput, () => (fields.colorInput.value = safeColor(seat.color)));
  setIfIdle(fields.enabledInput, () => (fields.enabledInput.checked = seat.enabled));

  const mustShowInstruction = !seat.persona || Boolean(seat.instruction);
  const showInstruction = mustShowInstruction || state.instructionOpenSeatIds.has(seat.id);
  fields.instruction.card.hidden = !showInstruction;
  fields.instructionToggle.hidden = mustShowInstruction;
  fields.instructionToggle.textContent = showInstruction ? 'Hide instruction' : 'Add instruction';
  fields.programNote.textContent = `Agent: ${seat.program}${seat.harness_version ? ' ' + seat.harness_version : ''}. Edits apply from the next reply.`;
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
  const modelField = createModelField(null, () => {});
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
    modelField.setHarness(harness);
    modelField.setValue('');
    fillThinkingSelect(thinkingSelect, harness, 'default');
  };
  harnessSelect.addEventListener('change', refreshForHarness);
  refreshForHarness();

  const submitButton = createElement('button', 'btn', 'Add');
  submitButton.type = 'submit';
  const cancelButton = createButton('Cancel', 'btn', () => {
    form.hidden = true;
  });
  const actions = createElement('div', 'side-actions');
  actions.append(submitButton, cancelButton);

  form.append(
    labelledField('Name', nameInput),
    labelledField('Agent', harnessSelect),
    labelledField('Model', modelField.element, modelField.input),
    labelledField('Thinking', thinkingSelect),
    labelledField('Persona', personaSelect),
    labelledField('Instruction', instruction.card),
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
    if (modelField.value()) seat.model = modelField.value();
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
  const ended = isThreadClosed();
  const connected = isConnected();
  elements.composerInput.disabled = ended;
  elements.sendButton.disabled = ended || !connected;
  const seatIsComposing = !ended && connected && state.typingSeatIds.size > 0;
  elements.stopButton.hidden = !seatIsComposing;
  elements.stopButton.disabled = !seatIsComposing;
  elements.endButton.disabled = ended || !connected || state.endRequested || state.room === null;
  if (isRoomArchived()) {
    elements.composerHint.textContent = 'This room is archived.';
  } else if (ended) {
    elements.composerHint.textContent = 'This thread has ended.';
  } else {
    elements.composerHint.textContent = defaultComposerHint;
  }
  elements.wakeAllButton.disabled = ended || !connected;
}

function autoGrowComposer() {
  const input = elements.composerInput;
  input.style.height = 'auto';
  // scrollHeight excludes the border, but the box is border-box, so add it back or a scrollbar appears.
  const borderHeight = input.offsetHeight - input.clientHeight;
  const wantedHeight = input.scrollHeight + borderHeight;
  const maximumHeight = window.innerHeight * COMPOSER_MAX_VIEWPORT_FRACTION;
  input.style.height = Math.min(wantedHeight, maximumHeight) + 'px';
  // Scroll only once the text outgrows the cap; while it fits, a scrollbar would only eat width.
  input.style.overflowY = wantedHeight > maximumHeight ? 'auto' : 'hidden';
}

function submitComposer() {
  const text = elements.composerInput.value.trim();
  if (text === '' || isThreadClosed()) return;
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
  if (!confirm('End this thread? Each seat with a persona that spoke will write memory notes.')) return;
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
      description: `every seat replies, in order · ${enabledCount} ${enabledCount === 1 ? 'reply' : 'replies'}`,
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
    option.append(createElement('span', 'mention-key', item.label), createElement('span', 'hint', item.description));
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
    const threadId = result.thread_id || message.thread_id || null;
    const link = createElement('a', 'search-result');
    link.href = threadId ? `${threadPath(message.room_id, threadId)}#m-${message.id}` : `/room/${message.room_id}#m-${message.id}`;
    const snippet = createElement('div', 'small');
    appendHighlighted(snippet, result.snippet, query);
    const author = message.role === 'owner' ? state.ownerName : message.author;
    const where = result.thread_title ? `${result.room_title} · ${result.thread_title}` : result.room_title;
    link.append(
      createElement('b', '', where),
      snippet,
      createElement('div', 'small mute', `${author} · ${formatDay(message.created_at)}`),
    );
    if (message.room_id === state.roomId && threadId) {
      // Same room: stay on the page; another thread is a socket switch, not a page load.
      link.addEventListener('click', (event) => {
        event.preventDefault();
        hideSearchResults();
        elements.searchInput.value = '';
        if (threadId === state.threadId) {
          scrollToMessage(message.id);
        } else {
          state.pendingHighlightId = String(message.id);
          openThread(threadId, { pushHistory: true });
        }
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

function isThreadDrawerOpen() {
  return document.body.classList.contains('show-threads');
}

/** The scrim stays while either narrow-screen drawer is open. */
function syncDrawerScrim() {
  elements.drawerScrim.hidden = !isDrawerOpen() && !isThreadDrawerOpen();
}

function setDrawer(open) {
  document.body.classList.toggle('show-panel', open);
  elements.panelToggle.setAttribute('aria-expanded', String(open));
  if (open) setThreadDrawer(false);
  syncDrawerScrim();
}

function toggleDrawer() {
  setDrawer(!isDrawerOpen());
}

function setThreadDrawer(open) {
  document.body.classList.toggle('show-threads', open);
  elements.threadsToggle.setAttribute('aria-expanded', String(open));
  if (open) setDrawer(false);
  syncDrawerScrim();
}

function closeDrawers() {
  setDrawer(false);
  setThreadDrawer(false);
}

// ----- keyboard -----

function handleDocumentKeydown(event) {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') {
    event.preventDefault();
    elements.searchInput.focus();
    elements.searchInput.select();
    return;
  }
  // An open dialog closes itself on Esc; that Esc must not also stop a speaker.
  if (event.key !== 'Escape' || elements.endedDialog.open || elements.memoryDialog.open) return;
  if (!elements.searchResults.hidden) {
    elements.searchInput.value = '';
    hideSearchResults();
    return;
  }
  if (state.renamingThreadId !== null) return; // the rename field handles its own Esc
  if (isDrawerOpen() || isThreadDrawerOpen()) {
    closeDrawers();
    return;
  }
  requestStop();
}

// ---------------------------------------------------------------------------
// 7. Threads: list, switching, new, rename
// ---------------------------------------------------------------------------

let memoryRequestCounter = 0;

function threadPath(roomId, threadId) {
  return `/room/${encodeURIComponent(roomId)}/thread/${encodeURIComponent(threadId)}`;
}

/** Replace a summary by id, or add it; the list stays newest first like Room.threads. */
function upsertThreadSummary(summary) {
  const index = state.threads.findIndex((thread) => thread.id === summary.id);
  if (index >= 0) {
    state.threads[index] = { ...state.threads[index], ...summary };
  } else {
    state.threads.push(summary);
  }
  state.threads.sort((first, second) => second.created_at.localeCompare(first.created_at));
}

function handleThreadFrame(summary) {
  upsertThreadSummary(summary);
  if (summary.id === state.threadId) {
    state.thread = state.threads.find((thread) => thread.id === summary.id);
    renderRoomMeta();
    renderPanel();
    renderSeats();
    renderComposerState();
  }
  renderThreadList();
}

function flattenLine(text) {
  const flat = String(text).replace(/\s+/g, ' ').replace(/[#*`>|]/g, '').trim();
  return flat.length > THREAD_LAST_LINE_LENGTH ? flat.slice(0, THREAD_LAST_LINE_LENGTH) + '…' : flat;
}

/**
 * Count and last line of a thread. The thread on screen is counted from the loaded messages,
 * because the owner's own messages arrive without a fresh summary from the server.
 */
function describeThreadActivity(thread) {
  const persisted = thread.id === state.threadId ? state.messages.filter((message) => !message.isLocal) : [];
  if (persisted.length === 0) {
    return { count: thread.message_count || 0, author: thread.last_author, line: thread.last_line };
  }
  const lastSpoken = [...persisted].reverse().find((message) => message.role !== 'system');
  return {
    count: persisted.length,
    author: lastSpoken ? lastSpoken.author : null,
    line: lastSpoken ? lastSpoken.text : null,
  };
}

function createThreadItem(thread) {
  const item = createElement('li');
  const link = createElement('a', 'thread-item');
  link.href = threadPath(state.roomId, thread.id);
  link.dataset.thread = thread.id;
  link.title = 'Open this thread. Double-click or press F2 to rename it.';
  if (thread.id === state.threadId) link.setAttribute('aria-current', 'page');

  const head = createElement('span', 'thread-item-head');
  head.append(createElement('span', 'thread-item-title', thread.title));
  if (thread.ended_at) head.append(createElement('span', 'pill pill--mute', 'ended'));

  const activity = describeThreadActivity(thread);
  const line = createElement('span', 'thread-item-line');
  if (activity.line) {
    const authorName = activity.author === 'owner' || !activity.author ? state.ownerName : activity.author;
    const author = createElement('b', '', authorName);
    const seat = seatsOfRoom().find((candidate) => candidate.name === activity.author);
    if (seat) {
      setSeatColor(author, seat.color);
      author.classList.add('seat-coloured');
    }
    line.append(author, ' ', flattenLine(activity.line));
  } else {
    line.append('No messages yet.');
  }

  const countWord = activity.count === 1 ? 'message' : 'messages';
  const meta = createElement('span', 'thread-item-meta', `${activity.count} ${countWord} · ${formatDay(thread.created_at)}`);
  link.append(head, line, meta);
  item.append(link);
  return item;
}

function renderThreadList() {
  // A rebuild would destroy the rename field the owner is typing in; it re-renders when done.
  if (state.renamingThreadId !== null) return;
  elements.threadList.replaceChildren();
  for (const thread of state.threads) {
    elements.threadList.append(createThreadItem(thread));
  }
  elements.threadNewTitle.disabled = isRoomArchived();
  elements.threadNewButton.disabled = isRoomArchived();
  elements.threadNewTitle.placeholder = isRoomArchived() ? 'This room is archived' : 'New thread title';
}

/** Forget everything that belongs to the previous thread; the room and the libraries stay. */
function resetThreadState() {
  state.messages = [];
  state.typingSeatIds.clear();
  state.lastBids = {};
  state.chosenSeatIds.clear();
  state.lastBidsKind = 'message';
  state.cooldownEndsAt = {};
  state.quietReason = null;
  state.quietFrame = null;
  state.endRequested = false;
  state.ownerScrolledUp = false;
  hideJumpLatest();
}

/** Show one thread: drop the old socket, clear its state, connect to the new one. */
function openThread(threadId, { pushHistory }) {
  if (pushHistory && threadId !== state.threadId) {
    history.pushState({ threadId }, '', threadPath(state.roomId, threadId));
  }
  if (socketClient) socketClient.close();
  state.threadId = threadId;
  state.thread = state.threads.find((thread) => thread.id === threadId) || null;
  resetThreadState();
  renderEverything();
  connectThreadSocket(threadId);
  setThreadDrawer(false);
}

/** Frames and status from a socket that was already replaced are ignored. */
function connectThreadSocket(threadId) {
  const client = createSocketClient({
    url: `/ws/threads/${encodeURIComponent(threadId)}`,
    onFrame: (frame) => {
      if (client === socketClient) handleFrame(frame);
    },
    onStatus: (status) => {
      if (client === socketClient) handleStatus(status);
    },
  });
  socketClient = client;
  client.connect();
}

async function createThread(event) {
  event.preventDefault();
  const title = elements.threadNewTitle.value.trim();
  if (!title) {
    elements.threadNewTitle.focus();
    return;
  }
  elements.threadNewButton.disabled = true;
  try {
    const summary = await sendJson('POST', `/api/rooms/${encodeURIComponent(state.roomId)}/threads`, { title });
    elements.threadNewTitle.value = '';
    upsertThreadSummary(summary);
    openThread(summary.id, { pushHistory: true });
    elements.composerInput.focus();
  } catch (error) {
    reportError('Could not start the thread', error);
  } finally {
    elements.threadNewButton.disabled = isRoomArchived();
  }
}

/** Swap the thread's list entry for a title field; Enter or leaving the field saves, Esc cancels. */
function startRenamingThread(threadId) {
  const thread = state.threads.find((candidate) => candidate.id === threadId);
  const link = elements.threadList.querySelector(`[data-thread="${CSS.escape(threadId)}"]`);
  if (!thread || !link) return;
  state.renamingThreadId = threadId;

  const input = createElement('input', 'thread-rename');
  input.type = 'text';
  input.value = thread.title;
  input.maxLength = 200;
  input.setAttribute('aria-label', 'Thread title');
  let finished = false;
  const finish = (save) => {
    if (finished) return;
    finished = true;
    finishRenamingThread(thread, save ? input.value.trim() : null);
  };
  input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      event.preventDefault();
      finish(true);
    } else if (event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation();
      finish(false);
    }
  });
  input.addEventListener('blur', () => finish(true));
  link.parentElement.replaceChildren(input);
  input.focus();
  input.select();
}

async function finishRenamingThread(thread, newTitle) {
  try {
    if (newTitle && newTitle !== thread.title) {
      const summary = await sendJson('PATCH', `/api/threads/${encodeURIComponent(thread.id)}`, { title: newTitle });
      upsertThreadSummary(summary);
      if (summary.id === state.threadId) {
        state.thread = state.threads.find((candidate) => candidate.id === summary.id);
      }
    }
  } catch (error) {
    reportError('Could not rename the thread', error);
  } finally {
    state.renamingThreadId = null;
    renderThreadList();
    renderRoomMeta();
  }
}

function threadIdFromEvent(event) {
  const link = event.target.closest('.thread-item');
  return link ? link.dataset.thread : null;
}

function bindThreadEvents() {
  elements.threadNewForm.addEventListener('submit', createThread);
  // Delegated, because the list is rebuilt between the two clicks of a double-click.
  elements.threadList.addEventListener('click', (event) => {
    const threadId = threadIdFromEvent(event);
    if (!threadId) return;
    // Let modified clicks open a new tab the browser's way.
    if (event.ctrlKey || event.metaKey || event.shiftKey || event.button !== 0) return;
    event.preventDefault();
    if (threadId !== state.threadId) openThread(threadId, { pushHistory: true });
  });
  elements.threadList.addEventListener('dblclick', (event) => {
    const threadId = threadIdFromEvent(event);
    if (!threadId) return;
    event.preventDefault();
    startRenamingThread(threadId);
  });
  elements.threadList.addEventListener('keydown', (event) => {
    const threadId = threadIdFromEvent(event);
    if (threadId && event.key === 'F2') {
      event.preventDefault();
      startRenamingThread(threadId);
    }
  });
  window.addEventListener('popstate', () => {
    const { roomId, threadId } = parseRoomPath(location.pathname);
    if (roomId !== state.roomId) {
      location.reload();
      return;
    }
    if (threadId && threadId !== state.threadId) openThread(threadId, { pushHistory: false });
  });
}

// ---------------------------------------------------------------------------
// 8. Memory drawer (SPEC v0.5 F3) and Open folder (F4)
// ---------------------------------------------------------------------------

function memoryTabId(tabKey) {
  return `memory-tab-${tabKey}`;
}

function sortNotesNewestFirst(notes) {
  // Notes written before v0.5 have no date; they go last, in file order.
  return [...notes].sort((first, second) => {
    const byDate = (second.written || '').localeCompare(first.written || '');
    return byDate !== 0 ? byDate : first.file.localeCompare(second.file);
  });
}

function personaBySlug(slug) {
  return state.personas.find((persona) => persona.slug === slug) || null;
}

function openMemoryDrawer() {
  if (!state.room) return;
  const tabStillExists = state.memoryTab === THIS_ROOM_TAB || seatById(state.memoryTab);
  if (!tabStillExists) {
    const firstSeat = seatsOfRoom()[0];
    state.memoryTab = firstSeat ? firstSeat.id : THIS_ROOM_TAB;
  }
  elements.memoryDialog.showModal();
  selectMemoryTab(state.memoryTab, false);
}

function renderMemoryTabs() {
  elements.memoryTabs.replaceChildren();
  const tabs = seatsOfRoom().map((seat) => ({ key: seat.id, label: seat.name, color: seat.color }));
  tabs.push({ key: THIS_ROOM_TAB, label: 'Room', color: null });
  for (const tab of tabs) {
    const selected = tab.key === state.memoryTab;
    const button = createButton('', 'btn memory-tab', () => selectMemoryTab(tab.key, true));
    button.id = memoryTabId(tab.key);
    button.dataset.tab = tab.key;
    button.setAttribute('role', 'tab');
    button.setAttribute('aria-selected', String(selected));
    button.setAttribute('aria-controls', 'memory-panel');
    // Roving tabindex: Tab reaches the selected tab, the arrow keys move between tabs.
    button.tabIndex = selected ? 0 : -1;
    if (tab.color) {
      setSeatColor(button, tab.color);
      const swatch = createElement('span', 'dot');
      swatch.setAttribute('aria-hidden', 'true');
      button.append(swatch, ' ');
    }
    button.append(tab.label);
    elements.memoryTabs.append(button);
  }
}

function selectMemoryTab(tabKey, moveFocus) {
  state.memoryTab = tabKey;
  state.editingNoteFile = null;
  renderMemoryTabs();
  elements.memoryPanel.setAttribute('aria-labelledby', memoryTabId(tabKey));
  if (moveFocus) document.getElementById(memoryTabId(tabKey)).focus();
  loadMemoryTab();
}

function handleMemoryTabKeydown(event) {
  if (event.key !== 'ArrowRight' && event.key !== 'ArrowLeft') return;
  const keys = [...elements.memoryTabs.querySelectorAll('[role="tab"]')].map((tab) => tab.dataset.tab);
  const step = event.key === 'ArrowRight' ? 1 : -1;
  const nextIndex = (keys.indexOf(state.memoryTab) + step + keys.length) % keys.length;
  event.preventDefault();
  selectMemoryTab(keys[nextIndex], true);
}

/** Fetch the selected tab's notes and keep them, so edit mode can toggle without a refetch. */
async function loadMemoryTab() {
  memoryRequestCounter += 1;
  const requestNumber = memoryRequestCounter;
  const tabKey = state.memoryTab;
  const seat = tabKey === THIS_ROOM_TAB ? null : seatById(tabKey);
  if (seat && !seat.persona) {
    state.memoryView = { kind: 'none', seat };
    renderMemoryPanel();
    return;
  }
  elements.memoryPanel.replaceChildren(createElement('p', 'hint', 'Loading notes…'));
  try {
    if (seat) {
      const notes = await fetchJson(`/api/personas/${encodeURIComponent(seat.persona)}/memory`);
      if (requestNumber !== memoryRequestCounter) return;
      state.memoryView = { kind: 'persona', seat, slug: seat.persona, notes: sortNotesNewestFirst(notes) };
    } else {
      const grouped = await fetchJson(`/api/rooms/${encodeURIComponent(state.roomId)}/memory`);
      if (requestNumber !== memoryRequestCounter) return;
      state.memoryView = { kind: 'room', grouped };
    }
    renderMemoryPanel();
  } catch (error) {
    if (requestNumber !== memoryRequestCounter) return;
    elements.memoryPanel.replaceChildren(createElement('p', 'bad', 'Could not load the notes.'));
    reportError('Could not load memory', error);
  }
}

function renderMemoryPanel() {
  const view = state.memoryView;
  const panel = elements.memoryPanel;
  panel.replaceChildren();
  if (view.kind === 'none') {
    panel.append(createElement('p', 'memory-empty', `${view.seat.name} has no persona.`));
    return;
  }
  if (view.kind === 'persona') {
    const persona = personaBySlug(view.slug);
    const countWord = view.notes.length === 1 ? 'note' : 'notes';
    const personaName = persona ? `${persona.name} · ${persona.role}` : view.slug;
    panel.append(createElement('p', 'hint', `${view.seat.name} as ${personaName} · ${view.notes.length} ${countWord}`));
    if (view.notes.length === 0) panel.append(createElement('p', 'memory-empty', 'No notes yet.'));
    for (const note of view.notes) panel.append(createNoteElement(view.slug, note));
    return;
  }
  renderRoomMemoryGroups(panel, view.grouped);
}

/** One group per persona; the heading names the seats of this room that speak as it. */
function renderRoomMemoryGroups(panel, grouped) {
  const slugs = Object.keys(grouped).filter((slug) => grouped[slug].length > 0);
  if (slugs.length === 0) {
    panel.append(createElement('p', 'memory-empty', 'No notes from this room yet.'));
    return;
  }
  for (const slug of slugs) {
    const seats = seatsOfRoom().filter((seat) => seat.persona === slug);
    const persona = personaBySlug(slug);
    const section = createElement('section', 'memory-group');
    if (seats[0]) setSeatColor(section, seats[0].color);
    const seatNames = seats.length > 0 ? seats.map((seat) => seat.name).join(', ') : 'no seat now';
    section.append(createElement('h3', '', `${seatNames} · ${persona ? persona.role : slug}`));
    for (const note of sortNotesNewestFirst(grouped[slug])) section.append(createNoteElement(slug, note));
    panel.append(section);
  }
}

function noteKey(slug, note) {
  return `${slug}/${note.file}`;
}

/** "Adda v0.5 · Crash recovery", linked to the thread it came from, or a plain line for old notes. */
function createNoteSource(note) {
  const source = createElement('span', 'memory-note-source');
  if (!note.thread_id || !note.room_id) {
    source.append('No source recorded');
    return source;
  }
  const link = createElement('a', '', `${note.room_title || 'a room'} · ${note.thread_title || 'a thread'}`);
  link.href = threadPath(note.room_id, note.thread_id);
  if (note.room_id === state.roomId) {
    link.addEventListener('click', (event) => {
      event.preventDefault();
      elements.memoryDialog.close();
      if (note.thread_id !== state.threadId) openThread(note.thread_id, { pushHistory: true });
    });
  }
  source.append('From ', link);
  return source;
}

function createNoteElement(slug, note) {
  const article = createElement('article', 'memory-note');
  if (state.editingNoteFile === noteKey(slug, note)) {
    article.append(createNoteEditor(slug, note));
    return article;
  }
  article.append(createElement('h4', 'memory-note-title', note.title));
  article.append(createElement('p', 'memory-note-body', note.body));
  const meta = createElement('div', 'memory-note-meta hint');
  meta.append(createNoteSource(note));
  if (note.written) meta.append(` · ${formatDay(note.written)} ${formatClock(note.written)}`);
  const actions = createElement('div', 'memory-note-actions');
  actions.append(
    createButton('Edit', 'btn', () => {
      state.editingNoteFile = noteKey(slug, note);
      renderMemoryPanel();
    }),
    createButton('Delete', 'btn btn--warning', () => deleteNote(slug, note)),
  );
  article.append(meta, actions);
  return article;
}

function createNoteEditor(slug, note) {
  const form = createElement('form', 'memory-note-editor');
  const titleInput = document.createElement('input');
  titleInput.type = 'text';
  titleInput.value = note.title;
  titleInput.required = true;
  titleInput.maxLength = 200;
  const bodyInput = document.createElement('textarea');
  bodyInput.rows = 4;
  bodyInput.value = note.body;
  const saveButton = createElement('button', 'btn', 'Save');
  saveButton.type = 'submit';
  const cancelButton = createButton('Cancel', 'btn', () => {
    state.editingNoteFile = null;
    renderMemoryPanel();
  });
  const actions = createElement('div', 'side-actions');
  actions.append(saveButton, cancelButton);
  form.append(labelledField('Title', titleInput), labelledField('Note', bodyInput), actions);
  form.addEventListener('submit', (event) => {
    event.preventDefault();
    saveNote(slug, note, titleInput.value.trim(), bodyInput.value.trim(), saveButton);
  });
  // Esc inside the editor cancels the edit instead of closing the whole drawer.
  form.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') {
      event.preventDefault();
      cancelButton.click();
    }
  });
  queueMicrotask(() => titleInput.focus());
  return form;
}

function noteEndpoint(slug, note) {
  return `/api/personas/${encodeURIComponent(slug)}/memory/${encodeURIComponent(note.file)}`;
}

async function saveNote(slug, note, title, body, saveButton) {
  if (!title) {
    showToast('A note needs a title.', true);
    return;
  }
  saveButton.disabled = true;
  try {
    const saved = await sendJson('PUT', noteEndpoint(slug, note), { title, body });
    // The PUT answers with the note; keep the local copy if a server answers with less.
    Object.assign(note, { title, body }, saved && saved.file ? saved : {});
    state.editingNoteFile = null;
    renderMemoryPanel();
    showToast('Note saved.');
  } catch (error) {
    saveButton.disabled = false;
    reportError('Could not save the note', error);
  }
}

async function deleteNote(slug, note) {
  if (!confirm(`Delete the note “${note.title}”?`)) return;
  try {
    await fetchJson(noteEndpoint(slug, note), { method: 'DELETE' });
    removeNoteFromView(slug, note);
    renderMemoryPanel();
    showToast('Note deleted.');
  } catch (error) {
    reportError('Could not delete the note', error);
  }
}

function removeNoteFromView(slug, note) {
  const view = state.memoryView;
  if (view.kind === 'persona') {
    view.notes = view.notes.filter((candidate) => candidate.file !== note.file);
  } else if (view.kind === 'room' && view.grouped[slug]) {
    view.grouped[slug] = view.grouped[slug].filter((candidate) => candidate.file !== note.file);
  }
}

function bindMemoryEvents() {
  elements.memoryButton.addEventListener('click', openMemoryDrawer);
  elements.memoryCloseButton.addEventListener('click', () => elements.memoryDialog.close());
  elements.memoryTabs.addEventListener('keydown', handleMemoryTabKeydown);
  // A click on the backdrop lands on the dialog element itself, outside its content box.
  elements.memoryDialog.addEventListener('click', (event) => {
    if (event.target === elements.memoryDialog) elements.memoryDialog.close();
  });
}

// ---------------------------------------------------------------------------
// 9. Startup
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
    elements.livelinessSentence.textContent = describeLiveliness(value);
  });
  elements.maxConcurrent.addEventListener('change', () => sendRoomPatch({ max_concurrent: Number(elements.maxConcurrent.value) }));
  fillAccessSelect(elements.access, 'talk');
  elements.access.addEventListener('change', () => sendRoomPatch({ access: elements.access.value }));
  elements.liveliness.addEventListener('change', () => sendRoomPatch({ liveliness: Number(elements.liveliness.value) }));
  elements.cooldown.addEventListener('change', () => {
    const cooldown = Math.max(0, Number(elements.cooldown.value) || 0);
    sendRoomPatch({ cooldown_s: cooldown });
  });
  elements.budget.addEventListener('change', () => {
    const budget = Math.max(0, Number(elements.budget.value) || 0);
    sendRoomPatch({ budget_usd: budget });
  });
  elements.modeOptions.addEventListener('change', (event) => {
    if (event.target.name === 'mode') sendRoomPatch({ mode: event.target.value });
  });
  elements.pauseButton.addEventListener('click', togglePause);
  elements.wakeAllButton.addEventListener('click', () => {
    clearCooldowns();
    sendFrame({ type: 'wake_all' });
  });
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
  elements.threadsToggle.addEventListener('click', () => setThreadDrawer(!isThreadDrawerOpen()));
  elements.drawerScrim.addEventListener('click', closeDrawers);
  document.addEventListener('keydown', handleDocumentKeydown);
  bindThreadEvents();
  bindMemoryEvents();
  elements.openFolderButton.addEventListener('click', () => openRoomFolder(state.roomId));
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
    state.maxAutoTurns = settings.room_defaults.max_auto_turns;
  } catch (error) {
    reportError('Could not load agents and personas', error);
  }
}

async function start() {
  bindEvents();
  renderConnection();
  const hashMatch = /^#m-(.+)$/.exec(location.hash);
  state.pendingHighlightId = hashMatch ? hashMatch[1] : null;
  await loadLibraries();
  const threadId = await chooseStartingThread();
  if (threadId === null) {
    showNoThreads();
    return;
  }
  // The address names the thread, so a reload or a shared link opens the same one.
  history.replaceState({ threadId }, '', threadPath(state.roomId, threadId) + location.hash);
  openThread(threadId, { pushHistory: false });
}

/**
 * The thread in the address if it exists in this room, else the newest open thread, else the
 * newest thread. Returns null when the room has no thread at all or cannot be read.
 */
async function chooseStartingThread() {
  const requestedThreadId = parseRoomPath(location.pathname).threadId;
  try {
    const { room } = await fetchJson(`/api/rooms/${encodeURIComponent(state.roomId)}`);
    state.room = room;
    state.threads = Array.isArray(room.threads) ? room.threads : [];
  } catch (error) {
    reportError('Could not load the room', error);
    return null;
  }
  if (requestedThreadId && state.threads.some((thread) => thread.id === requestedThreadId)) {
    return requestedThreadId;
  }
  if (requestedThreadId) {
    showToast('Opened the newest thread.');
  }
  const newestOpen = state.threads.find((thread) => !thread.ended_at);
  const fallback = newestOpen || state.threads[0];
  return fallback ? fallback.id : null;
}

function showNoThreads() {
  renderRoomMeta();
  renderThreadList();
  renderPanel();
  renderSeats(true);
  elements.messageList.replaceChildren(createElement('li', 'msg msg--system', 'No thread yet.'));
}

start();
