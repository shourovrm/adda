/*
 * Rooms page: the list of rooms and the new-room card (SPEC section 8).
 *
 * The seat table keeps its draft in `state.draft.seats`. Typing into a text field updates the
 * draft without re-rendering (a re-render would drop focus); choices that change what other
 * fields offer (harness, persona) re-render the table.
 */

'use strict';

const MAX_PREVIEWED_ROOMS = 30;
const FOLDER_SUGGESTION_DEBOUNCE_MS = 300;

const state = {
  rooms: [],
  harnesses: [],
  personas: [],
  settings: null,
  draft: null,
  nextRowKey: 1,
  folderPicker: { path: '', parent: null, folders: [], selectedPath: null },
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
  browseButton: document.getElementById('browse-folder-button'),
  folderDialog: document.getElementById('folder-dialog'),
  folderPath: document.getElementById('folder-dialog-path'),
  folderUpButton: document.getElementById('folder-up-button'),
  folderList: document.getElementById('folder-list'),
  folderOpenButton: document.getElementById('folder-open-button'),
  folderNewButton: document.getElementById('folder-new-button'),
  folderNewForm: document.getElementById('folder-new-form'),
  folderNewName: document.getElementById('folder-new-name'),
  folderChooseButton: document.getElementById('folder-choose-button'),
  folderCancelButton: document.getElementById('folder-cancel-button'),
  budget: document.getElementById('room-budget-input'),
  mode: document.getElementById('room-mode-select'),
  maxConcurrent: document.getElementById('room-max-concurrent-select'),
  liveliness: document.getElementById('room-liveliness-input'),
  livelinessValue: document.getElementById('room-liveliness-value'),
  livelinessSentence: document.getElementById('room-liveliness-sentence'),
  access: document.getElementById('room-access-select'),
  accessDescription: document.getElementById('room-access-description'),
  accessWarning: document.getElementById('room-access-warning'),
  seatTable: document.getElementById('seat-table'),
  addSeatButton: document.getElementById('add-seat-button'),
  lastSeatsButton: document.getElementById('last-seats-button'),
  summary: document.getElementById('room-summary'),
  openButton: document.getElementById('open-room-button'),
};

// ---------------------------------------------------------------------------
// Room list
// ---------------------------------------------------------------------------

function createStatusPill(room) {
  if (room.ended_at) return createElement('span', 'pill pill--mute', 'Ended');
  if (room.liveliness === 0) return createElement('span', 'pill pill--mute', 'Paused');
  return createElement('span', 'pill pill--good', 'Live');
}

function describeRoomSpend(room) {
  const budgetText = room.budget_usd > 0 ? ` of ${formatMoney(room.budget_usd)}` : '';
  return `${formatMoney(room.spent_usd)}${budgetText}`;
}

/** Empty for talk, the default, so only rooms with tools carry the extra words. */
function describeRoomAccessSuffix(room) {
  return room.access && room.access !== 'talk' ? ` · access ${room.access}` : '';
}

/**
 * Room.threads (SPEC v0.5 F2) carries every thread's message count, so the meta line needs no
 * extra request.
 */
function describeRoomMeta(room) {
  const threads = Array.isArray(room.threads) ? room.threads : [];
  const messageCount = threads.reduce((total, thread) => total + (thread.message_count || 0), 0);
  const threadWord = threads.length === 1 ? 'thread' : 'threads';
  const messageWord = messageCount === 1 ? 'message' : 'messages';
  return `${threads.length} ${threadWord} · ${messageCount} ${messageWord} · ${describeRoomSpend(room)}${describeRoomAccessSuffix(room)}`;
}

function createRoomCard(room) {
  const isLive = !room.ended_at && room.liveliness !== 0;
  const card = createElement('article', isLive ? 'room-card room-card--live' : 'room-card');

  const head = createElement('div', 'room-card-head');
  const title = createElement('h2');
  const link = createElement('a', '', room.title);
  link.href = `/room/${room.id}`;
  title.append(link);
  head.append(title, createStatusPill(room));

  const seatLine = createElement('div', 'room-seats');
  for (const seat of room.seats) {
    const seatName = createElement('span', 'room-seat');
    setSeatColor(seatName, seat.color);
    seatName.append(createSwatch(seat.name), seat.name);
    seatLine.append(seatName);
  }

  card.append(head, seatLine, createElement('div', 'room-meta', describeRoomMeta(room)));
  return card;
}

function renderRoomList() {
  elements.roomList.replaceChildren();
  if (state.rooms.length === 0) {
    elements.roomList.append(createElement('p', 'mute', 'No rooms yet. Start one with New room.'));
    return;
  }
  for (const room of state.rooms.slice(0, MAX_PREVIEWED_ROOMS)) {
    elements.roomList.append(createRoomCard(room));
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
    eagerness: EAGERNESS_DEFAULT,
    access: null,
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
    maxConcurrent: defaults.max_concurrent ?? 2,
    access: defaults.access || 'talk',
    seats: [],
  };
  state.draft = draft;
  draft.seats.push(createDraftRow({ instructionOpen: true }));
}

/** Local date as YYYYMMDD, the prefix of a default room folder (SPEC v0.3 D4). */
function todayStamp() {
  const now = new Date();
  const month = String(now.getMonth() + 1).padStart(2, '0');
  const day = String(now.getDate()).padStart(2, '0');
  return `${now.getFullYear()}${month}${day}`;
}

/** Used while the server's suggestion is pending and when the call fails. */
function suggestFolderLocally() {
  const slug = slugify(state.draft.title);
  const base = state.settings.folders.rooms_dir.replace(/\/+$/, '');
  return slug ? `${base}/${todayStamp()}-${slug}` : base;
}

let folderSuggestionTimerId = null;
let folderSuggestionRequestCounter = 0;

/**
 * Ask the server for a folder name that does not exist yet (it appends -2, -3). Only the newest
 * request may write the field, and never over a folder the owner typed or picked.
 */
async function refreshFolderSuggestion() {
  const requestNumber = ++folderSuggestionRequestCounter;
  const title = state.draft.title;
  let suggestion;
  try {
    const answer = await fetchJson(`/api/rooms/folder-suggestion?title=${encodeURIComponent(title)}`);
    suggestion = answer.folder || answer.path;
  } catch (error) {
    console.warn('Folder suggestion failed; using the local one', error);
  }
  if (requestNumber !== folderSuggestionRequestCounter || state.draft.folderEdited) return;
  elements.folder.value = suggestion || suggestFolderLocally();
  renderFolderDependentText();
}

function scheduleFolderSuggestion() {
  elements.folder.value = suggestFolderLocally();
  renderFolderDependentText();
  clearTimeout(folderSuggestionTimerId);
  folderSuggestionTimerId = setTimeout(refreshFolderSuggestion, FOLDER_SUGGESTION_DEBOUNCE_MS);
}

function renderDraftFields() {
  const draft = state.draft;
  elements.title.value = draft.title;
  elements.folder.value = draft.folder;
  if (!draft.folderEdited) {
    scheduleFolderSuggestion();
  }
  elements.budget.value = draft.budget;
  elements.mode.value = draft.mode;
  elements.liveliness.value = draft.liveliness;
  elements.maxConcurrent.value = String(draft.maxConcurrent);
  renderDraftLiveliness();
  fillAccessSelect(elements.access, draft.access);
  renderDraftAccess();
  renderSeatTable();
}

function renderDraftLiveliness() {
  elements.livelinessValue.textContent = state.draft.liveliness;
  elements.livelinessSentence.textContent = describeLiveliness(state.draft.liveliness, 0);
}

function renderDraftAccess() {
  const access = state.draft.access;
  elements.accessDescription.textContent = ACCESS_DESCRIPTIONS[access];
  elements.accessWarning.hidden = access !== 'work';
  elements.accessWarning.textContent = describeAccessWarning(elements.folder.value.trim());
}

/** The access warning and the summary line both name the folder, so they follow it. */
function renderFolderDependentText() {
  renderDraftAccess();
  renderSummary();
}

function describeSummaryAccess() {
  const folder = elements.folder.value.trim() || 'the rooms folder';
  const overrides = state.draft.seats.filter((row) => row.access).map((row) => `${row.name} ${row.access}`);
  const overrideText = overrides.length > 0 ? ` (${overrides.join(', ')})` : '';
  return `Access: ${ACCESS_LABELS[state.draft.access]} in ${folder}${overrideText}`;
}

function summaryHasWork() {
  return state.draft.access === 'work' || state.draft.seats.some((row) => row.access === 'work');
}

function renderSummary() {
  const seatCount = state.draft.seats.length;
  const harnessCount = new Set(state.draft.seats.map((row) => row.program)).size;
  const seatWord = seatCount === 1 ? 'seat' : 'seats';
  const harnessWord = harnessCount === 1 ? 'harness' : 'harnesses';
  elements.summary.textContent = `${seatCount} ${seatWord} · ${harnessCount} ${harnessWord} · ${describeSummaryAccess()}`;
  if (summaryHasWork()) {
    const warning = createElement('div', 'access-warning', describeAccessWarning(elements.folder.value.trim()));
    elements.summary.append(warning);
  }
  elements.openButton.disabled = seatCount === 0;
}

// ---------------------------------------------------------------------------
// Seat table
// ---------------------------------------------------------------------------

function renderSeatTable() {
  elements.seatTable.replaceChildren();
  for (const row of state.draft.seats) {
    appendSeatRow(row);
  }
  renderSummary();
}

/** A visible label tied to its control; ids only need to be unique among the rows on the page. */
function createSeatField(row, fieldName, labelText, control) {
  const field = createElement('div', 'field');
  control.id = `seat-${row.key}-${fieldName}`;
  const label = createElement('label', '', labelText);
  label.htmlFor = control.id;
  field.append(label, control);
  return field;
}

function appendSeatRow(row) {
  const harness = findHarness(state.harnesses, row.program);

  const colorInput = document.createElement('input');
  colorInput.type = 'color';
  colorInput.value = safeColor(row.color);
  colorInput.addEventListener('input', () => {
    row.color = colorInput.value;
    row.colorEdited = true;
  });

  const nameInput = document.createElement('input');
  nameInput.type = 'text';
  nameInput.value = row.name;
  nameInput.addEventListener('input', () => {
    row.name = nameInput.value;
    row.nameEdited = true;
  });

  const harnessSelect = document.createElement('select');
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
  const modelInput = document.createElement('input');
  modelInput.type = 'text';
  modelInput.setAttribute('list', datalist.id);
  modelInput.placeholder = harness && harness.default_model ? `default: ${harness.default_model}` : 'harness default';
  modelInput.value = row.model;
  modelInput.addEventListener('input', () => {
    row.model = modelInput.value;
  });

  const thinkingSelect = document.createElement('select');
  fillThinkingSelect(thinkingSelect, harness, row.thinking);
  thinkingSelect.addEventListener('change', () => {
    row.thinking = thinkingSelect.value;
  });

  const personaSelect = document.createElement('select');
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

  const accessSelect = document.createElement('select');
  fillAccessSelect(accessSelect, row.access, 'Room level');
  accessSelect.addEventListener('change', () => {
    row.access = readAccessSelect(accessSelect);
    renderSummary();
  });

  const eagerness = createEagernessControl(row.eagerness, (value) => {
    row.eagerness = value;
  });

  const fields = createElement('div', 'seat-fields');
  fields.append(
    createSeatField(row, 'name', 'Name', nameInput),
    createSeatField(row, 'color', 'Colour', colorInput),
    createSeatField(row, 'harness', 'Harness', harnessSelect),
    createSeatField(row, 'model', 'Model', modelInput),
    createSeatField(row, 'thinking', 'Thinking', thinkingSelect),
    createSeatField(row, 'persona', 'Persona', personaSelect),
    createSeatField(row, 'access', 'Access', accessSelect),
    createSeatField(row, 'eagerness', 'Eagerness', eagerness.input),
  );
  // The eagerness sentence belongs to the slider, so it moves into the slider's field.
  fields.lastElementChild.append(eagerness.element.querySelector('.eagerness-sentence'));
  fields.append(datalist);

  const actions = createElement('div', 'seat-actions');
  actions.append(
    createButton(row.instructionOpen ? 'Hide instruction' : 'Add instruction', 'btn', () => {
      row.instructionOpen = !row.instructionOpen;
      renderSeatTable();
    }),
    createButton('Duplicate seat', 'btn', () => duplicateRow(row)),
    createButton('Remove seat', 'btn', () => {
      state.draft.seats = state.draft.seats.filter((candidate) => candidate !== row);
      renderSeatTable();
    }),
  );

  const card = createElement('div', 'seat-card');
  card.append(fields, actions);
  if (row.instructionOpen) {
    const instruction = createInstructionCard(row.instruction, (text) => {
      row.instruction = text;
    });
    card.append(instruction.card);
  }
  elements.seatTable.append(card);
}

/** Scout -> Scout2 -> Scout3: the lowest number from 2 up that no other row already uses. */
function nextFreeName(name) {
  const stem = name.replace(/\d+$/, '') || name;
  const takenNames = new Set(state.draft.seats.map((row) => row.name));
  let number = 2;
  while (takenNames.has(`${stem}${number}`)) number += 1;
  return `${stem}${number}`;
}

function duplicateRow(row) {
  const copy = createDraftRow({
    ...row,
    key: state.nextRowKey++,
    name: nextFreeName(row.name),
    nameEdited: true,
  });
  const position = state.draft.seats.indexOf(row);
  state.draft.seats.splice(position + 1, 0, copy);
  renderSeatTable();
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
        eagerness: seat.eagerness ?? EAGERNESS_DEFAULT,
        access: seat.access || null,
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
    eagerness: row.eagerness,
    access: row.access,
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
    max_concurrent: draft.maxConcurrent,
    access: draft.access,
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
// Folder picker (SPEC v0.3 D3)
// ---------------------------------------------------------------------------

/** Returns true when the listing loaded. `quiet` skips the toast for a path that may not exist yet. */
async function loadFolder(path, quiet = false) {
  try {
    const listing = await fetchJson(`/api/folders?path=${encodeURIComponent(path)}`);
    state.folderPicker = { ...listing, selectedPath: null };
    renderFolderPicker();
    return true;
  } catch (error) {
    if (!quiet) reportError('Could not list that folder', error);
    return false;
  }
}

function selectFolderEntry(path) {
  state.folderPicker.selectedPath = path;
  renderFolderPicker();
}

function renderFolderPicker() {
  const picker = state.folderPicker;
  elements.folderPath.textContent = picker.path;
  elements.folderUpButton.disabled = picker.parent === null;
  elements.folderOpenButton.disabled = picker.selectedPath === null;
  elements.folderList.replaceChildren();
  if (picker.folders.length === 0) {
    elements.folderList.append(createElement('li', 'small mute folder-empty', 'No folders here.'));
  }
  for (const folder of picker.folders) {
    const item = createElement('li');
    const button = createButton(folder.name, 'folder-entry', () => selectFolderEntry(folder.path));
    button.setAttribute('aria-pressed', String(folder.path === picker.selectedPath));
    button.addEventListener('dblclick', () => loadFolder(folder.path));
    button.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        event.preventDefault();
        loadFolder(folder.path);
      }
    });
    item.append(button);
    elements.folderList.append(item);
  }
}

async function openFolderPicker() {
  elements.folderNewForm.hidden = true;
  elements.folderDialog.showModal();
  // The field usually holds a folder that does not exist yet, so start at its parent, which
  // normally does; an empty path makes the server list the rooms folder.
  const fieldFolder = elements.folder.value.trim();
  const parentOfField = fieldFolder.includes('/') ? fieldFolder.slice(0, fieldFolder.lastIndexOf('/')) : '';
  const openedFieldFolder = await loadFolder(parentOfField, true);
  if (!openedFieldFolder) await loadFolder('');
}

async function createFolderHere(event) {
  event.preventDefault();
  const name = elements.folderNewName.value.trim();
  if (!name) return;
  try {
    const created = await sendJson('POST', '/api/folders', { path: state.folderPicker.path, name });
    elements.folderNewForm.hidden = true;
    elements.folderNewName.value = '';
    await loadFolder(created.path);
  } catch (error) {
    reportError('Could not create the folder', error);
  }
}

function chooseCurrentFolder() {
  state.draft.folderEdited = true;
  state.draft.folder = state.folderPicker.path;
  elements.folder.value = state.folderPicker.path;
  renderFolderDependentText();
  elements.folderDialog.close();
  elements.folder.focus();
}

function bindFolderPicker() {
  elements.browseButton.addEventListener('click', openFolderPicker);
  elements.folderUpButton.addEventListener('click', () => loadFolder(state.folderPicker.parent));
  elements.folderOpenButton.addEventListener('click', () => loadFolder(state.folderPicker.selectedPath));
  elements.folderNewButton.addEventListener('click', () => {
    elements.folderNewForm.hidden = false;
    elements.folderNewName.focus();
  });
  elements.folderNewForm.addEventListener('submit', createFolderHere);
  elements.folderChooseButton.addEventListener('click', chooseCurrentFolder);
  elements.folderCancelButton.addEventListener('click', () => elements.folderDialog.close());
}

// ---------------------------------------------------------------------------
// Startup
// ---------------------------------------------------------------------------

function openNewRoomForm() {
  createDraft();
  elements.form.hidden = false;
  // While the form is open its submit button is the page's one primary action.
  elements.newRoomButton.hidden = true;
  renderDraftFields();
  elements.title.focus();
}

function bindEvents() {
  elements.newRoomButton.addEventListener('click', openNewRoomForm);
  elements.cancelButton.addEventListener('click', () => {
    elements.form.hidden = true;
    elements.newRoomButton.hidden = false;
  });
  elements.form.addEventListener('submit', openRoom);
  bindFolderPicker();

  elements.title.addEventListener('input', () => {
    state.draft.title = elements.title.value;
    if (!state.draft.folderEdited) scheduleFolderSuggestion();
  });
  elements.folder.addEventListener('input', () => {
    state.draft.folderEdited = true;
    state.draft.folder = elements.folder.value;
    renderFolderDependentText();
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
  elements.maxConcurrent.addEventListener('change', () => {
    state.draft.maxConcurrent = Number(elements.maxConcurrent.value);
  });
  elements.access.addEventListener('change', () => {
    state.draft.access = elements.access.value;
    renderDraftAccess();
    renderSummary();
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
