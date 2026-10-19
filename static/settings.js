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
  // Sits beside the Base URL in the Agents section instead of the turn-taking keys.
  { name: 'anthropic_auth_token', label: 'Claude provider key', variable: 'ANTHROPIC_AUTH_TOKEN', containerId: 'claude-key-field' },
];

// Two 24-unit strokes: an open eye, and the same eye crossed out while the key is shown.
const EYE_ICON =
  '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
  + '<path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7S1 12 1 12z"/><circle cx="12" cy="12" r="3"/></svg>';
const EYE_OFF_ICON =
  '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
  + '<path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7S1 12 1 12z"/><circle cx="12" cy="12" r="3"/><line x1="3" y1="3" x2="21" y2="21"/></svg>';

const state = { settings: null, harnesses: [], personas: [], dirty: false };

const elements = {
  ownerName: document.getElementById('owner-name'),
  form: document.getElementById('settings-form'),
  harnessRows: document.getElementById('harness-rows'),
  personaRows: document.getElementById('persona-rows'),
  keyFields: document.getElementById('key-fields'),
  claudeKeyField: document.getElementById('claude-key-field'),
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
  for (const input of elements.form.querySelectorAll('input[data-key]')) {
    if (input.value.trim() !== '') keys[input.dataset.key] = input.value.trim();
  }
  if (Object.keys(keys).length > 0) body.keys = keys;
  return body;
}

/** Mark every field a shell variable overrides; the saved value is shadowed until the variable is unset. */
function markEnvOverrides() {
  const overridden = overriddenPaths();
  for (const field of fieldElements()) {
    const wrapper = field.closest('.field');
    if (!overridden.has(field.dataset.path) || !wrapper || wrapper.querySelector('.env-hint')) continue;
    wrapper.append(createElement('div', 'hint env-hint', 'Set by your shell'));
  }
}

// ---------------------------------------------------------------------------
// Sections
// ---------------------------------------------------------------------------

function renderKeyFields() {
  elements.keyFields.replaceChildren();
  elements.claudeKeyField.replaceChildren();
  for (const keyField of KEY_FIELDS) {
    const isSet = Boolean((state.settings.keys || {})[keyField.name]);
    // A key with its own container (already a .field in the page) fills it; the rest get a new field.
    const ownContainer = keyField.containerId ? document.getElementById(keyField.containerId) : null;
    const wrapper = ownContainer || createElement('div', 'field');
    const label = createElement('label', '', keyField.label + ' ');
    label.append(createElement('span', isSet ? 'pill pill--good' : 'pill pill--bad', isSet ? 'Set' : 'Not set'));
    const input = document.createElement('input');
    input.type = 'password';
    input.autocomplete = 'off';
    input.id = `key-${keyField.name}`;
    input.dataset.key = keyField.name;
    input.placeholder = isSet ? 'Paste a new key to replace it' : 'Paste a key';
    label.htmlFor = input.id;
    // The eye reveals only what is typed now; saved keys never come back from the server.
    const toggle = createElement('button', 'field-eye');
    toggle.type = 'button';
    toggle.innerHTML = EYE_ICON;
    toggle.setAttribute('aria-label', 'Show key');
    toggle.setAttribute('aria-pressed', 'false');
    toggle.setAttribute('aria-controls', input.id);
    toggle.addEventListener('click', () => {
      const revealing = input.type === 'password';
      input.type = revealing ? 'text' : 'password';
      toggle.innerHTML = revealing ? EYE_OFF_ICON : EYE_ICON;
      toggle.setAttribute('aria-label', revealing ? 'Hide key' : 'Show key');
      toggle.setAttribute('aria-pressed', String(revealing));
    });
    const row = createElement('div', 'key-row');
    row.append(input, toggle);
    wrapper.append(label, row);
    if (!ownContainer) elements.keyFields.append(wrapper);
  }
}

function createHarnessRow(harness) {
  const program = harness.program;
  const row = createElement('tr');

  // Name, where it was found and how to set it up share one cell so the model field gets the width.
  const nameCell = createElement('td');
  nameCell.append(createElement('b', '', program), createElement('br'));
  if (harness.found) {
    nameCell.append(createElement('span', 'small mute', `${harness.version || 'unknown version'} · ${harness.path}`));
  } else {
    // An installed agent needs no instruction; Adda cannot tell whether it is signed in.
    nameCell.append(createElement('span', 'pill pill--bad', 'Not installed'));
    nameCell.append(createElement('br'), createElement('span', 'small', plainLoginHint(harness.login_hint)));
  }

  // The picker is two controls, so a hidden input carries the data-path for fill and collect.
  const modelValue = document.createElement('input');
  modelValue.type = 'hidden';
  modelValue.dataset.path = `harness.${program}.model`;
  modelValue.disabled = !harness.found;
  const modelField = createModelField(harness, (model) => {
    modelValue.value = model;
  });
  modelField.element.classList.add('input--30');
  modelField.select.setAttribute('aria-label', `${program} default model`);
  modelField.input.setAttribute('aria-label', `${program} default model name`);
  modelField.setDisabled(!harness.found);
  modelField.setValue(readPath(state.settings, modelValue.dataset.path));

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
    wrapCell('Model', modelField.element, modelValue),
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
    showToast('Agents checked.');
  } catch (error) {
    reportError('Could not check the agents', error);
  }
}

/** Marks the section list entry for the panel the reader is in: the last one that has reached
 *  the top third of the window, or the last panel of all once the page is scrolled to its end,
 *  since a short last panel never reaches the top on its own. */
function markCurrentSection() {
  const links = [...document.querySelectorAll('.section-nav a')];
  const panels = links.map((link) => document.querySelector(link.getAttribute('href'))).filter(Boolean);
  if (panels.length === 0) return;

  function update() {
    const atPageEnd = window.scrollY + window.innerHeight >= document.documentElement.scrollHeight - 2;
    const threshold = window.innerHeight / 3;
    let current = panels[0];
    for (const panel of panels) {
      if (panel.getBoundingClientRect().top <= threshold) current = panel;
    }
    if (atPageEnd) current = panels[panels.length - 1];
    for (const link of links) {
      if (link.getAttribute('href') === `#${current.id}`) link.setAttribute('aria-current', 'true');
      else link.removeAttribute('aria-current');
    }
  }

  let scheduled = false;
  window.addEventListener('scroll', () => {
    if (scheduled) return;
    scheduled = true;
    requestAnimationFrame(() => {
      scheduled = false;
      update();
    });
  });
  update();
}

async function saveSettings(event) {
  event.preventDefault();
  elements.saveButton.disabled = true;
  elements.saveStatus.textContent = 'Saving';
  try {
    state.settings = await sendJson('PUT', '/api/settings', collectSettings());
    state.harnesses = await fetchJson('/api/harnesses');
    renderAll();
    state.dirty = false;
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
  // Save sits in the page head, away from the fields, so the status says when there is work to save.
  elements.form.addEventListener('input', () => {
    state.dirty = true;
    elements.saveStatus.textContent = 'Unsaved changes';
  });
  // Leaving with unsaved edits (close, reload, another page) asks first; the browser shows the dialog.
  window.addEventListener('beforeunload', (event) => {
    if (!state.dirty) return;
    event.preventDefault();
    event.returnValue = '';
  });
  markCurrentSection();
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
