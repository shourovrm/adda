/*
 * Settings page. Fields carry data-path="section.key" (or "harness.claude.model"); loading
 * writes the value at that path into the field and saving reads every field back into an
 * object of the same shape as GET /api/settings, which PUT /api/settings accepts (SPEC 6, 7).
 *
 * API keys are never returned, only whether each is set. A key is sent only when a new value
 * was typed, so saving never wipes a key.
 */

'use strict';

const KEY_FIELDS = [
  { name: 'openrouter', label: 'OpenRouter key', variable: 'OPENROUTER_API_KEY' },
  { name: 'typesafe', label: 'TypeSafe key', variable: 'TYPESAFE_API_KEY' },
  { name: 'deepseek', label: 'DeepSeek key', variable: 'DEEPSEEK_API_KEY' },
];

const state = { settings: null, harnesses: [], personas: [] };

const elements = {
  ownerName: document.getElementById('owner-name'),
  form: document.getElementById('settings-form'),
  harnessRows: document.getElementById('harness-rows'),
  personaRows: document.getElementById('persona-rows'),
  keyFields: document.getElementById('key-fields'),
  rescanButton: document.getElementById('rescan-button'),
  saveButton: document.getElementById('save-button'),
  saveStatus: document.getElementById('save-status'),
  defaultLiveliness: document.getElementById('default-liveliness-input'),
  defaultLivelinessValue: document.getElementById('default-liveliness-value'),
};

// ---------------------------------------------------------------------------
// Paths into the settings object
// ---------------------------------------------------------------------------

function readPath(object, path) {
  return path.split('.').reduce((value, key) => (value === undefined || value === null ? undefined : value[key]), object);
}

function writePath(object, path, value) {
  const keys = path.split('.');
  const last = keys.pop();
  let cursor = object;
  for (const key of keys) {
    cursor[key] = cursor[key] || {};
    cursor = cursor[key];
  }
  cursor[last] = value;
}

/** `env_overrides` is a list of dotted paths; an object keyed by path is accepted as well. */
function overriddenPaths() {
  const overrides = state.settings.env_overrides || [];
  return new Set(Array.isArray(overrides) ? overrides : Object.keys(overrides));
}

function fieldElements() {
  return [...elements.form.querySelectorAll('[data-path]')];
}

// ---------------------------------------------------------------------------
// Fill and collect
// ---------------------------------------------------------------------------

function fillField(field) {
  const value = readPath(state.settings, field.dataset.path);
  if (field.type === 'checkbox') {
    field.checked = Boolean(value);
  } else {
    field.value = value === undefined || value === null ? '' : value;
  }
}

function collectField(field) {
  if (field.type === 'checkbox') return field.checked;
  if (field.type === 'number' || field.type === 'range') return Number(field.value);
  return field.value;
}

function collectSettings() {
  const body = {};
  for (const field of fieldElements()) {
    // A disabled thinking select (harness without the flag) keeps its stored value untouched.
    if (field.disabled) continue;
    writePath(body, field.dataset.path, collectField(field));
  }
  const keys = {};
  for (const input of elements.keyFields.querySelectorAll('input[data-key]')) {
    if (input.value.trim() !== '') keys[input.dataset.key] = input.value.trim();
  }
  if (Object.keys(keys).length > 0) body.keys = keys;
  return body;
}

/** Mark every field a shell variable overrides; the saved value is shadowed until the variable is unset. */
function markEnvOverrides() {
  const overridden = overriddenPaths();
  for (const field of fieldElements()) {
    const label = field.id ? document.querySelector(`label[for="${field.id}"]`) : null;
    if (!overridden.has(field.dataset.path) || !label || label.querySelector('.tag--env')) continue;
    const badge = createElement('span', 'pill pill--mute', 'env');
    badge.title = 'A shell variable is set and overrides this value.';
    label.append(' ', badge);
  }
}

// ---------------------------------------------------------------------------
// Sections
// ---------------------------------------------------------------------------

function renderKeyFields() {
  elements.keyFields.replaceChildren();
  for (const keyField of KEY_FIELDS) {
    const isSet = Boolean((state.settings.keys || {})[keyField.name]);
    const wrapper = createElement('div', 'field');
    const label = createElement('label', '', keyField.label + ' ');
    label.append(createElement('span', isSet ? 'pill pill--good' : 'pill pill--bad', isSet ? 'set' : 'not set'));
    const input = document.createElement('input');
    input.type = 'password';
    input.autocomplete = 'off';
    input.id = `key-${keyField.name}`;
    input.dataset.key = keyField.name;
    input.placeholder = isSet ? 'set; paste a new key to replace it' : 'paste to set';
    label.htmlFor = input.id;
    wrapper.append(label, input);
    elements.keyFields.append(wrapper);
  }
}

function createHarnessRow(harness) {
  const program = harness.program;
  const row = createElement('tr');

  // Name, where it was found and how it logs in share one cell so the model field gets the width.
  const nameCell = createElement('td');
  nameCell.append(createElement('b', '', program), createElement('br'));
  if (harness.found) {
    nameCell.append(createElement('span', 'small mute', `${harness.version || 'unknown version'} · ${harness.path}`));
  } else {
    nameCell.append(createElement('span', 'pill pill--bad', 'not on PATH'));
  }
  nameCell.append(createElement('br'), createElement('span', 'small', harness.login_hint));

  const modelInput = document.createElement('input');
  modelInput.type = 'text';
  modelInput.dataset.path = `harness.${program}.model`;
  modelInput.setAttribute('aria-label', `${program} default model`);
  modelInput.disabled = !harness.found;
  const datalist = document.createElement('datalist');
  datalist.id = `settings-models-${program}`;
  fillDatalist(datalist, harness);
  modelInput.setAttribute('list', datalist.id);

  const thinkingSelect = document.createElement('select');
  thinkingSelect.dataset.path = `harness.${program}.thinking`;
  thinkingSelect.setAttribute('aria-label', `${program} default thinking`);
  fillThinkingSelect(thinkingSelect, harness, readPath(state.settings, thinkingSelect.dataset.path));

  const enabledInput = document.createElement('input');
  enabledInput.type = 'checkbox';
  enabledInput.dataset.path = `harness.${program}.enabled`;
  enabledInput.setAttribute('aria-label', `${program} enabled`);

  row.append(
    nameCell,
    wrapCell('Default model', modelInput, datalist),
    wrapCell('Thinking', thinkingSelect),
    wrapCell('On', enabledInput),
  );
  return row;
}

/** `columnName` is shown as a caption above the control when the table is stacked on a narrow screen. */
function wrapCell(columnName, ...children) {
  const cell = createElement('td');
  cell.dataset.column = columnName;
  cell.append(...children);
  return cell;
}

function renderHarnessRows() {
  elements.harnessRows.replaceChildren(...state.harnesses.map(createHarnessRow));
  for (const field of elements.harnessRows.querySelectorAll('[data-path]')) {
    // fillThinkingSelect has already set the select; the others take their value from settings.
    if (field.tagName !== 'SELECT') fillField(field);
  }
}

function renderPersonaRows() {
  elements.personaRows.replaceChildren();
  const personasDir = state.settings.folders.personas_dir;
  for (const persona of state.personas) {
    const row = createElement('tr');
    const dot = createElement('span', 'persona-dot');
    setSeatColor(dot, persona.color);
    const nameCell = createElement('td');
    nameCell.append(dot, ' ', createElement('b', '', persona.name), ` · ${persona.role}`);
    const notes = persona.note_count === 1 ? '1 memory note' : `${persona.note_count} memory notes`;
    const detail = createElement('td', 'small');
    detail.append(notes, createElement('br'), createElement('code', '', `${personasDir}/${persona.slug}`));
    row.append(nameCell, detail);
    elements.personaRows.append(row);
  }
}

function renderAll() {
  elements.ownerName.textContent = state.settings.owner.name;
  renderKeyFields();
  renderHarnessRows();
  renderPersonaRows();
  for (const field of fieldElements()) {
    if (!elements.harnessRows.contains(field)) fillField(field);
  }
  elements.defaultLivelinessValue.textContent = elements.defaultLiveliness.value;
  markEnvOverrides();
}

// ---------------------------------------------------------------------------
// Actions
// ---------------------------------------------------------------------------

async function rescanHarnesses() {
  try {
    state.harnesses = await fetchJson('/api/harnesses');
    renderHarnessRows();
    showToast('Harnesses scanned again.');
  } catch (error) {
    reportError('Could not re-scan', error);
  }
}

async function saveSettings(event) {
  event.preventDefault();
  elements.saveButton.disabled = true;
  elements.saveStatus.textContent = 'Saving';
  try {
    state.settings = await sendJson('PUT', '/api/settings', collectSettings());
    state.harnesses = await fetchJson('/api/harnesses');
    renderAll();
    elements.saveStatus.textContent = 'Saved';
    showToast('Settings saved.');
  } catch (error) {
    elements.saveStatus.textContent = 'Not saved';
    reportError('Could not save settings', error);
  } finally {
    elements.saveButton.disabled = false;
  }
}

async function start() {
  elements.form.addEventListener('submit', saveSettings);
  elements.rescanButton.addEventListener('click', rescanHarnesses);
  elements.defaultLiveliness.addEventListener('input', () => {
    elements.defaultLivelinessValue.textContent = elements.defaultLiveliness.value;
  });
  try {
    const [settings, harnesses, personas] = await Promise.all([
      fetchJson('/api/settings'),
      fetchJson('/api/harnesses'),
      fetchJson('/api/personas'),
    ]);
    Object.assign(state, { settings, harnesses, personas });
  } catch (error) {
    reportError('Could not load settings', error);
    return;
  }
  renderAll();
}

start();
