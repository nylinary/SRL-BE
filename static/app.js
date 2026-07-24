const SEEN_KEY = 'qt:seen';
const THEME_KEY = 'qt:theme';
const FILTERS_KEY = 'qt:filters';

const el = (id) => document.getElementById(id);

const dom = {
  card: el('card'),
  crumbs: el('crumbs'),
  question: el('question'),
  answer: el('answer'),
  answerBody: el('answer-body'),
  noAnswer: el('no-answer'),
  skeleton: el('skeleton'),
  message: el('message'),
  messageTitle: el('message-title'),
  messageText: el('message-text'),
  messageRetry: el('message-retry'),
  theme: el('theme-select'),
  subtheme: el('subtheme-select'),
  answeredOnly: el('answered-only'),
  next: el('next-btn'),
  refresh: el('refresh-btn'),
  themeToggle: el('theme-toggle'),
  progress: el('progress'),
  staleBadge: el('stale-badge'),
  resetSeen: el('reset-seen'),
};

let index = null;          // theme tree from /api/index
let seen = loadSeen();     // ids already shown, per filter key
let poolSize = 0;
let busy = false;

/* ------------------------------------------------------------------ state */

function loadSeen() {
  try {
    return JSON.parse(localStorage.getItem(SEEN_KEY)) || {};
  } catch {
    return {};
  }
}

function saveSeen() {
  localStorage.setItem(SEEN_KEY, JSON.stringify(seen));
}

function filters() {
  return {
    theme: dom.theme.value || null,
    subtheme: dom.subtheme.value || null,
    answered_only: dom.answeredOnly.checked,
  };
}

function filterKey() {
  const f = filters();
  return [f.theme || '*', f.subtheme || '*', f.answered_only ? 'a' : 'all'].join('|');
}

function seenIds() {
  return seen[filterKey()] || [];
}

function markSeen(id) {
  const key = filterKey();
  const ids = seen[key] || [];
  if (!ids.includes(id)) ids.push(id);
  seen[key] = ids;
  saveSeen();
}

function saveFilters() {
  localStorage.setItem(FILTERS_KEY, JSON.stringify(filters()));
}

/* -------------------------------------------------------------------- ui */

function show(view) {
  dom.skeleton.hidden = view !== 'loading';
  dom.card.hidden = view !== 'question';
  dom.message.hidden = view !== 'message';
}

function fail(title, text) {
  dom.messageTitle.textContent = title;
  dom.messageText.textContent = text;
  show('message');
}

function updateProgress() {
  if (!poolSize) {
    dom.progress.textContent = '—';
    return;
  }
  dom.progress.textContent = `${seenIds().length} / ${poolSize} пройдено`;
}

function renderCrumbs(question) {
  const parts = [question.theme, question.subtheme, question.section].filter(Boolean);
  dom.crumbs.innerHTML = '';
  parts.forEach((part, i) => {
    if (i > 0) {
      const sep = document.createElement('span');
      sep.className = 'crumbs__sep';
      sep.textContent = '›';
      dom.crumbs.append(sep);
    }
    const crumb = document.createElement('span');
    crumb.className = i === 0 ? 'crumb crumb--theme' : 'crumb';
    crumb.textContent = part;
    dom.crumbs.append(crumb);
  });
}

function highlight(root) {
  if (!window.hljs) return;
  root.querySelectorAll('pre code').forEach((block) => window.hljs.highlightElement(block));
}

function renderQuestion(payload) {
  const q = payload.question;
  poolSize = payload.pool_size;

  renderCrumbs(q);
  dom.question.innerHTML = q.question_html;

  dom.answer.open = false;                 // always collapsed by default
  dom.answer.hidden = !q.has_answer;
  dom.noAnswer.hidden = q.has_answer;
  dom.answerBody.innerHTML = q.has_answer ? q.answer_html : '';
  if (q.has_answer) highlight(dom.answerBody);

  markSeen(q.id);
  updateProgress();
  show('question');
  dom.card.classList.remove('card--enter');
  void dom.card.offsetWidth;               // restart the entrance animation
  dom.card.classList.add('card--enter');
}

/* ---------------------------------------------------------------- loading */

async function loadIndex() {
  const res = await fetch('/api/index');
  if (!res.ok) throw new Error((await res.json()).detail || `HTTP ${res.status}`);
  index = await res.json();

  const saved = dom.theme.value;
  dom.theme.innerHTML = '<option value="">Все темы</option>';
  index.themes
    .slice()
    .sort((a, b) => a.name.localeCompare(b.name, 'ru'))
    .forEach((theme) => {
      const option = document.createElement('option');
      option.value = theme.name;
      option.textContent = `${theme.name} (${theme.answered})`;
      dom.theme.append(option);
    });
  dom.theme.value = saved;
  syncSubthemes();

  dom.staleBadge.hidden = !index.status.stale;
}

function syncSubthemes() {
  const theme = index?.themes.find((t) => t.name === dom.theme.value);
  const saved = dom.subtheme.value;

  dom.subtheme.innerHTML = '<option value="">Все подтемы</option>';
  dom.subtheme.disabled = !theme || theme.subthemes.length === 0;

  (theme?.subthemes || [])
    .slice()
    .sort((a, b) => a.name.localeCompare(b.name, 'ru'))
    .forEach((sub) => {
      const option = document.createElement('option');
      option.value = sub.name;
      option.textContent = `${sub.name} (${sub.answered})`;
      dom.subtheme.append(option);
    });

  if ([...dom.subtheme.options].some((o) => o.value === saved)) dom.subtheme.value = saved;
}

async function nextQuestion() {
  if (busy) return;
  busy = true;
  dom.next.disabled = true;
  show('loading');

  try {
    const res = await fetch('/api/questions/random', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...filters(), seen: seenIds() }),
    });
    const payload = await res.json();

    if (!res.ok) {
      fail(res.status === 503 ? 'Нет связи с источником' : 'Ничего не найдено',
        payload.detail || `HTTP ${res.status}`);
      return;
    }
    if (payload.cycle_completed) {
      seen[filterKey()] = [];               // full circle — start a new round
    }
    renderQuestion(payload);
  } catch (error) {
    fail('Ошибка загрузки', error.message);
  } finally {
    busy = false;
    dom.next.disabled = false;
  }
}

/* --------------------------------------------------------------- controls */

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  localStorage.setItem(THEME_KEY, theme);
}

function restorePreferences() {
  applyTheme(localStorage.getItem(THEME_KEY) || 'dark');
  try {
    const saved = JSON.parse(localStorage.getItem(FILTERS_KEY));
    if (saved) {
      dom.theme.value = saved.theme || '';
      dom.subtheme.value = saved.subtheme || '';
      dom.answeredOnly.checked = saved.answered_only !== false;
    }
  } catch { /* first run */ }
}

function bind() {
  dom.next.addEventListener('click', nextQuestion);
  dom.messageRetry.addEventListener('click', nextQuestion);

  dom.theme.addEventListener('change', () => {
    dom.subtheme.value = '';
    syncSubthemes();
    saveFilters();
    nextQuestion();
  });

  dom.subtheme.addEventListener('change', () => {
    saveFilters();
    nextQuestion();
  });

  dom.answeredOnly.addEventListener('change', () => {
    saveFilters();
    nextQuestion();
  });

  dom.themeToggle.addEventListener('click', () => {
    applyTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark');
  });

  dom.refresh.addEventListener('click', async () => {
    dom.refresh.classList.add('spinning');
    try {
      await fetch('/api/refresh', { method: 'POST' });
      await loadIndex();
      await nextQuestion();
    } catch (error) {
      fail('Не удалось обновить', error.message);
    } finally {
      dom.refresh.classList.remove('spinning');
    }
  });

  dom.resetSeen.addEventListener('click', () => {
    seen[filterKey()] = [];
    saveSeen();
    updateProgress();
  });

  document.addEventListener('keydown', (event) => {
    if (event.target.matches('input, select, textarea')) return;

    if (event.key === 'n' || event.key === 'N' || event.key === 'ArrowRight') {
      event.preventDefault();
      nextQuestion();
    } else if (event.key === ' ' || event.key === 'Enter') {
      if (dom.card.hidden || dom.answer.hidden) return;
      event.preventDefault();
      dom.answer.open = !dom.answer.open;
    }
  });
}

(async function start() {
  restorePreferences();
  bind();
  show('loading');
  try {
    await loadIndex();
  } catch (error) {
    fail('Нет связи с источником', error.message);
    return;
  }
  await nextQuestion();
})();
