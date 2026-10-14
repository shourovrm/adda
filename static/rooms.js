/*
 * Rooms page: the list of rooms and the new-room card (SPEC section 8).
 *
 * The seat table keeps its draft in `state.draft.seats`. Typing into a text field updates the
 * draft without re-rendering (a re-render would drop focus); choices that change what other
 * fields offer (harness, persona) re-render the table.
 */

'use strict';

const MAX_PREVIEWED_ROOMS = 30;
const MODE_LABELS = { bidding: 'Bidding', roundrobin: 'Round-robin', manual: 'Manual' };

const state = {
  rooms: [],
  harnesses: [],
  personas: [],
  settings: null,
  draft: null,
  nextRowKey: 1,
};

const elements = {
  ownerName: document.getElementById('owner-name'),
  roomList: document.getElementById('room-list'),
  harnessList: document.getElementById('harness-list'),
  newRoomButton: document.getElementById('new-room-button'),
  form: document.getElementById('new-room-form'),
  cancelButton: document.getElementById('cancel-room-button'),
  title: document.getElementById('room-title-input'),
  folder: document.getElementById('room-folder-input'),
  budget: document.getElementById('room-budget-input'),
  mode: document.getElementById('room-mode-select'),
  liveliness: document.getElementById('room-liveliness-input'),
  livelinessValue: document.getElementById('room-liveliness-value'),
  livelinessSentence: document.getElementById('room-liveliness-sentence'),
  seatTable: document.getElementById('seat-table'),
  addSeatButton: document.getElementById('add-seat-button'),
  lastSeatsButton: document.getElementById('last-seats-button'),
  summary: document.getElementById('room-summary'),
  openButton: document.getElementById('open-room-button'),
};

// ---------------------------------------------------------------------------
// Room list
// ---------------------------------------------------------------------------

function describeRoomTag(room) {
  if (room.ended_at) {
    return createElement('span', 'tag', `Ended ${formatDay(room.ended_at)}`);
  }
  if (room.liveliness === 0) {
    return createElement('span', 'tag', 'Paused');
  }
  return createElement('span', 'tag tag--live', 'Live');
}

function describeRoomSpend(room) {
  const budgetText = room.budget_usd > 0 ? ` of ${formatMoney(room.budget_usd)}` : '';
  return `${formatMoney(room.spent_usd)}${budgetText}`;
}

function createRoomCard(room) {
  const card = createElement('article', room.ended_at || room.liveliness === 0 ? 'card room-card' : 'card room-card room-card--live');

  const head = createElement('div', 'row row--between');
  const left = createElement('div');
  left.append(describeRoomTag(room), ' ');
  const details = room.ended_at
    ? ''
    : `started ${formatClock(room.created_at)} · ${MODE_LABELS[room.mode] || room.mode} · liveliness ${room.liveliness}`;
  left.append(createElement('span', 'small mute', details));
  const right = createElement('div', 'small mute row-fixed', describeRoomSpend(room));
  right.dataset.role = 'counts';
  head.append(left, right);

  const title = createElement('h2');
  title.style.marginTop = '6px';
  const link = createElement('a', '', room.title);
  link.href = `/room/${room.id}`;
  title.append(link);

  const seatLine = createElement('div', 'seat-line');
  for (const seat of room.seats) {
    const dot = createElement('span', 'dot');
    setSeatColor(dot, seat.color);
    seatLine.append(dot, ' ', seat.name, ' ');
  }
  seatLine.append(createElement('span', 'mute', room.folder || 'no folder'));

  const lastLine = createElement('p', 'last-line mute', '');
  lastLine.dataset.role = 'last-line';

  card.append(head, title, seatLine, lastLine);
  return card;
}

/**
 * GET /api/rooms returns rooms without messages, so each card asks for its own thread once to
 * show the message count and the last line. Failures are logged and leave the card without a preview.
 */
async function fillRoomPreview(room, card) {
  try {
    const { messages } = await fetchJson(`/api/rooms/${room.id}`);
    const countText = `${messages.length} messages · ${describeRoomSpend(room)}`;
    card.querySelector('[data-role="counts"]').textContent = countText;
    const lastSpoken = [...messages].reverse().find((message) => message.role !== 'system');
    if (lastSpoken) {
      const author = lastSpoken.role === 'owner' ? state.settings.owner.name : lastSpoken.author;
      const lineElement = card.querySelector('[data-role="last-line"]');
      const authorElement = createElement('b', '', author);
      if (lastSpoken.role === 'seat') {
        const seat = room.seats.find((candidate) => candidate.id === lastSpoken.seat_id);
        if (seat) setSeatColor(authorElement, seat.color);
        authorElement.style.color = 'var(--seat-text)';
      }
      const flat = lastSpoken.text.replace(/\s+/g, ' ').replace(/[#*`>|]/g, '').trim();
      lineElement.replaceChildren(authorElement, ' ', flat.length > 140 ? flat.slice(0, 140) + '…' : flat);
    }
  } catch (error) {
    console.error('Could not preview room', room.id, error);
  }
}

function renderRoomList() {
  elements.roomList.replaceChildren();
  if (state.rooms.length === 0) {
    elements.roomList.append(createElement('p', 'mute', 'No rooms yet. Start one with New room.'));
    return;
  }
  for (const room of state.rooms.slice(0, MAX_PREVIEWED_ROOMS)) {
    const card = createRoomCard(room);
    elements.roomList.append(card);
    fillRoomPreview(room, card);
  }
  elements.roomList.append(
    createElement('p', 'small mute', `Showing ${Math.min(state.rooms.length, MAX_PREVIEWED_ROOMS)} of ${state.rooms.length}`),
  );
}

// ---------------------------------------------------------------------------
// New-room draft
// ---------------------------------------------------------------------------

function firstFoundHarness() {
  return state.harnesses.find((harness) => harness.found) || state.harnesses[0];
}

function createDraftRow(overrides = {}) {
  const harness = firstFoundHarness();
  const row = {
    key: state.nextRowKey++,
    name: '',
    program: harness ? harness.program : 'claude',
    model: '',
    thinking: 'default',
    persona: '',
    instruction: '',
    instructionOpen: false,
    copies: 1,
    color: paletteColor(state.draft ? state.draft.seats.length : 0),
    nameEdited: false,
    colorEdited: false,
    ...overrides,
  };
  if (!row.name) {
    row.name = defaultSeatName(row);
  }
  return row;
}

function defaultSeatName(row) {
  const persona = state.personas.find((candidate) => candidate.slug === row.persona);
  if (persona) return persona.name;
  return row.program.charAt(0).toUpperCase() + row.program.slice(1);
}

function createDraft() {
  const defaults = state.settings.room_defaults;
  const draft = {
    title: '',
    folder: '',
    folderEdited: false,
    budget: defaults.budget_usd,
    mode: defaults.mode,
    liveliness: defaults.liveliness,
    seats: [],
  };
  state.draft = draft;
  draft.seats.push(createDraftRow({ instructionOpen: true }));
}

function suggestFolder() {
  const slug = slugify(state.draft.title);
  const base = state.settings.folders.rooms_dir.replace(/\/+$/, '');
  return slug ? `${base}/${slug}` : base;
}

function renderDraftFields() {
  const draft = state.draft;
  elements.title.value = draft.title;
  elements.folder.value = draft.folderEdited ? draft.folder : suggestFolder();
  elements.budget.value = draft.budget;
  elements.mode.value = draft.mode;
  elements.liveliness.value = draft.liveliness;
  renderDraftLiveliness();
  renderSeatTable();
}

function renderDraftLiveliness() {
  elements.livelinessValue.textContent = state.draft.liveliness;
  elements.livelinessSentence.textContent = describeLiveliness(state.draft.liveliness, 0);
}

function renderSummary() {
  const seatCount = state.draft.seats.reduce((sum, row) => sum + (Number(row.copies) || 1), 0);
  const harnessCount = new Set(state.draft.seats.map((row) => row.program)).size;
  const seatWord = seatCount === 1 ? 'seat' : 'seats';
  const harnessWord = harnessCount === 1 ? 'harness' : 'harnesses';
  elements.summary.textContent = `${seatCount} ${seatWord} · ${harnessCount} ${harnessWord}`;
  elements.openButton.disabled = seatCount === 0;
}

// ---------------------------------------------------------------------------
// Seat table
// ---------------------------------------------------------------------------

const SEAT_TABLE_COLUMNS = ['', 'Name', 'Harness', 'Model', 'Thinking', 'Persona', 'Copies', ''];

function renderSeatTable() {
  elements.seatTable.replaceChildren();

  const head = createElement('div', 'seat-row seat-row--head');
  for (const column of SEAT_TABLE_COLUMNS) {
    head.append(createElement('span', '', column));
  }
  elements.seatTable.append(head);

  for (const row of state.draft.seats) {
    appendSeatRow(row);
  }
  renderSummary();
}

function labelled(element, label) {
  element.setAttribute('aria-label', label);
  return element;
}

function appendSeatRow(row) {
  const harness = findHarness(state.harnesses, row.program);
  const container = elements.seatTable;

  const rowElement = createElement('div', 'seat-row');

  const colorInput = labelled(document.createElement('input'), `Colour for ${row.name}`);
  colorInput.type = 'color';
  colorInput.value = safeColor(row.color);
  colorInput.addEventListener('input', () => {
    row.color = colorInput.value;
    row.colorEdited = true;
  });

  const nameInput = labelled(document.createElement('input'), 'Seat name');
  nameInput.type = 'text';
  nameInput.value = row.name;
  nameInput.addEventListener('input', () => {
    row.name = nameInput.value;
    row.nameEdited = true;
  });

  const harnessSelect = labelled(document.createElement('select'), 'Harness');
  fillHarnessSelect(harnessSelect, state.harnesses, row.program);
  harnessSelect.addEventListener('change', () => {
    row.program = harnessSelect.value;
    row.model = '';
    row.thinking = 'default';
    if (!row.nameEdited) row.name = defaultSeatName(row);
    renderSeatTable();
  });

  const datalist = document.createElement('datalist');
  datalist.id = `models-${row.key}`;
  fillDatalist(datalist, harness);
  const modelInput = labelled(document.createElement('input'), 'Model');
  modelInput.type = 'text';
  modelInput.setAttribute('list', datalist.id);
  modelInput.placeholder = harness && harness.default_model ? `default: ${harness.default_model}` : 'harness default';
  modelInput.value = row.model;
  modelInput.addEventListener('input', () => {
    row.model = modelInput.value;
  });

  const thinkingSelect = labelled(document.createElement('select'), 'Thinking');
  fillThinkingSelect(thinkingSelect, harness, row.thinking);
  thinkingSelect.addEventListener('change', () => {
    row.thinking = thinkingSelect.value;
  });

  const personaSelect = labelled(document.createElement('select'), 'Persona');
  fillPersonaSelect(personaSelect, state.personas, row.persona);
  personaSelect.addEventListener('change', () => {
    row.persona = personaSelect.value;
    const persona = state.personas.find((candidate) => candidate.slug === row.persona);
    if (!row.nameEdited) row.name = defaultSeatName(row);
    if (persona && !row.colorEdited) row.color = persona.color;
    // A seat without a persona has no voice, so the instruction card opens to ask for one.
    if (!row.persona) row.instructionOpen = true;
    renderSeatTable();
  });

  const copiesInput = labelled(document.createElement('input'), 'Copies');
  copiesInput.type = 'number';
  copiesInput.min = '1';
  copiesInput.max = '8';
  copiesInput.value = row.copies;
  copiesInput.addEventListener('input', () => {
    row.copies = Math.max(1, Math.min(8, parseInt(copiesInput.value, 10) || 1));
    renderSummary();
  });

  const removeButton = createButton('×', 'btn btn--ghost btn--small', () => {
    state.draft.seats = state.draft.seats.filter((candidate) => candidate !== row);
    renderSeatTable();
  });
  removeButton.setAttribute('aria-label', `Remove ${row.name}`);

  rowElement.append(colorInput, nameInput, harnessSelect, modelInput, thinkingSelect, personaSelect, copiesInput, removeButton, datalist);
  container.append(rowElement);

  // The instruction control and card sit under the row, aligned with the name column.
  const toggleLabel = row.instructionOpen ? 'Hide instruction' : 'Add instruction';
  const toggle = createButton(toggleLabel, 'btn btn--ghost btn--small', () => {
    row.instructionOpen = !row.instructionOpen;
    renderSeatTable();
  });
  const actions = createElement('div', 'seat-row seat-row--sub');
  const actionCell = createElement('div', 'seat-row-actions');
  actionCell.append(toggle);
  if (row.copies > 1) {
    actionCell.append(createElement('span', 'small mute', copiesDescription(row)));
  }
  actions.append(createElement('span'), actionCell);
  container.append(actions);

  if (row.instructionOpen) {
    const { card } = createInstructionCard(row.instruction, (text) => {
      row.instruction = text;
    });
    const cardRow = createElement('div', 'seat-row seat-row--sub');
    cardRow.append(createElement('span'), card);
    container.append(cardRow);
  }
}

function copiesDescription(row) {
  const names = [];
  for (let index = 1; index <= row.copies; index++) names.push(`${row.name}${index}`);
  return `Joins as ${names.join(', ')}, each with its own session.`;
}

async function fillFromLastRoom() {
  try {
    const lastSeats = await fetchJson('/api/rooms/last-seats');
    if (lastSeats.length === 0) {
      showToast('There is no earlier room to copy seats from.');
      return;
    }
    state.draft.seats = lastSeats.map((seat) =>
      createDraftRow({
        name: seat.name,
        nameEdited: true,
        program: seat.program,
        model: seat.model || '',
        thinking: seat.thinking || 'default',
        persona: seat.persona || '',
        instruction: seat.instruction || '',
        instructionOpen: Boolean(seat.instruction) || !seat.persona,
        color: seat.color || paletteColor(state.draft.seats.length),
        colorEdited: true,
      }),
    );
    renderSeatTable();
  } catch (error) {
    reportError('Could not load the last room', error);
  }
}

// ---------------------------------------------------------------------------
// Submit
// ---------------------------------------------------------------------------

function buildNewSeat(row) {
  const seat = {
    name: row.name.trim(),
    program: row.program,
    thinking: row.thinking,
    color: row.color,
    copies: row.copies,
  };
  if (row.model.trim()) seat.model = row.model.trim();
  if (row.persona) seat.persona = row.persona;
  if (row.instruction.trim()) seat.instruction = row.instruction.trim();
  return seat;
}

async function openRoom(event) {
  event.preventDefault();
  const draft = state.draft;
  if (draft.seats.some((row) => !row.name.trim())) {
    showToast('Every seat needs a name.', true);
    return;
  }
  const body = {
    title: draft.title.trim(),
    mode: draft.mode,
    liveliness: draft.liveliness,
    budget_usd: Number(draft.budget) || 0,
    seats: draft.seats.map(buildNewSeat),
  };
  const folder = elements.folder.value.trim();
  if (folder) body.folder = folder;

  elements.openButton.disabled = true;
  try {
    const room = await sendJson('POST', '/api/rooms', body);
    location.assign(`/room/${room.id}`);
  } catch (error) {
    elements.openButton.disabled = false;
    reportError('Could not open the room', error);
  }
}

// ---------------------------------------------------------------------------
// Startup
// ---------------------------------------------------------------------------

function openNewRoomForm() {
  createDraft();
  elements.form.hidden = false;
  renderDraftFields();
  elements.title.focus();
}

function bindEvents() {
  elements.newRoomButton.addEventListener('click', openNewRoomForm);
  elements.cancelButton.addEventListener('click', () => {
    elements.form.hidden = true;
  });
  elements.form.addEventListener('submit', openRoom);

  elements.title.addEventListener('input', () => {
    state.draft.title = elements.title.value;
    if (!state.draft.folderEdited) elements.folder.value = suggestFolder();
  });
  elements.folder.addEventListener('input', () => {
    state.draft.folderEdited = true;
    state.draft.folder = elements.folder.value;
  });
  elements.budget.addEventListener('input', () => {
    state.draft.budget = elements.budget.value;
  });
  elements.mode.addEventListener('change', () => {
    state.draft.mode = elements.mode.value;
  });
  elements.liveliness.addEventListener('input', () => {
    state.draft.liveliness = Number(elements.liveliness.value);
    renderDraftLiveliness();
  });
  elements.addSeatButton.addEventListener('click', () => {
    state.draft.seats.push(createDraftRow({ instructionOpen: true }));
    renderSeatTable();
  });
  elements.lastSeatsButton.addEventListener('click', fillFromLastRoom);
}

async function start() {
  bindEvents();
  try {
    const [rooms, harnesses, personas, settings] = await Promise.all([
      fetchJson('/api/rooms'),
      fetchJson('/api/harnesses'),
      fetchJson('/api/personas'),
      fetchJson('/api/settings'),
    ]);
    Object.assign(state, { rooms, harnesses, personas, settings });
  } catch (error) {
    reportError('Could not load the rooms page', error);
    return;
  }
  elements.ownerName.textContent = state.settings.owner.name;
  renderHarnessList(elements.harnessList, state.harnesses);
  renderRoomList();
}

start();
