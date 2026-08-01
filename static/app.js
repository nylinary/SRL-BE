const THEME_KEY = 'qt:theme';
const FILTERS_KEY = 'qt:filters';

const el = (id) => document.getElementById(id);

const dom = {
  tabTrain: el('tab-train'),
  tabStats: el('tab-stats'),
  tabSettings: el('tab-settings'),
  viewTrain: el('view-train'),
  viewStats: el('view-stats'),
  viewSettings: el('view-settings'),
  trainFilters: el('train-filters'),
  setRetention: el('set-retention'),
  setSource: el('set-source'),
  optimFill: el('optim-fill'),
  optimCount: el('optim-count'),
  optimHint: el('optim-hint'),
  optimizeBtn: el('optimize-btn'),
  optimStatus: el('optim-status'),
  optimWarn: el('optim-warn'),
  card: el('card'),
  crumbs: el('crumbs'),
  question: el('question'),
  answer: el('answer'),
  answerBody: el('answer-body'),
  noAnswer: el('no-answer'),
  grade: el('grade'),
  gradeMeta: el('grade-meta'),
  skeleton: el('skeleton'),
  message: el('message'),
  messageTitle: el('message-title'),
  messageText: el('message-text'),
  messageRetry: el('message-retry'),
  theme: el('theme-select'),
  subtheme: el('subtheme-select'),
  answeredOnly: el('answered-only'),
  spacedMode: el('spaced-mode'),
  skip: el('skip-btn'),
  refresh: el('refresh-btn'),
  themeToggle: el('theme-toggle'),
  progress: el('progress'),
  dueBadge: el('due-badge'),
  staleBadge: el('stale-badge'),
  statsSearch: el('stats-search'),
  statsTheme: el('stats-theme'),
  statsDueOnly: el('stats-due-only'),
  statsCount: el('stats-count'),
  statsBody: el('stats-body'),
  statsEmpty: el('stats-empty'),
  statsTable: el('stats-table'),
};

let index = null;          // theme tree from /api/index
let shown = 0;             // cards shown this session (in-memory counter only)
let current = null;        // the question on screen
let poolSize = 0;
let busy = false;
let optimizing = false;    // an optimizer run is in flight (survives a status refresh)

const stats = { rows: [], now: 0, sortKey: 'next_due', sortDir: 1 };

/* ------------------------------------------------------------------ state */

function filters() {
  return {
    theme: dom.theme.value || null,
    subtheme: dom.subtheme.value || null,
    answered_only: dom.answeredOnly.checked,
    mode: dom.spacedMode.checked ? 'spaced' : 'random',
  };
}
const saveFilters = () => localStorage.setItem(FILTERS_KEY, JSON.stringify(filters()));

/* ---------------------------------------------------------------- formatting */

const dateFmt = new Intl.DateTimeFormat('ru-RU', { day: 'numeric', month: 'short', year: 'numeric' });

/** Human "time until": seconds -> "<1 мин", "10 мин", "3 ч", "2 дн", "4 мес", "1.2 г". */
function humanInterval(seconds) {
  const m = seconds / 60, h = seconds / 3600, d = seconds / 86400;
  if (seconds < 45) return '<1 мин';
  if (m < 60) return `${Math.round(m)} мин`;
  if (h < 24) return `${Math.round(h)} ч`;
  if (d < 31) return `${Math.round(d)} дн`;
  if (d < 365) return `${Math.round(d / 30)} мес`;
  return `${(d / 365).toFixed(1)} г`;
}

function dueLabel(nextDue, now) {
  if (!nextDue) return { text: '—', overdue: false };
  if (nextDue <= now) return { text: 'к повтору', overdue: true };
  const days = Math.round((nextDue - now) / 86400);
  if (days <= 0) return { text: 'сегодня', overdue: false };
  if (days === 1) return { text: 'завтра', overdue: false };
  return { text: dateFmt.format(new Date(nextDue * 1000)), overdue: false };
}

/* -------------------------------------------------------------------- views */

function showTab(name) {
  dom.tabTrain.classList.toggle('is-active', name === 'train');
  dom.tabStats.classList.toggle('is-active', name === 'stats');
  dom.tabSettings.classList.toggle('is-active', name === 'settings');
  dom.viewTrain.hidden = name !== 'train';
  dom.viewStats.hidden = name !== 'stats';
  dom.viewSettings.hidden = name !== 'settings';
  dom.trainFilters.hidden = name !== 'train';
  if (name === 'stats') loadStats();
  if (name === 'settings') loadOptimizerStatus();
}

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
  dom.progress.textContent = poolSize ? `${shown} за сессию · ${poolSize} в наборе` : '—';
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

function renderPreview(preview) {
  ['again', 'hard', 'good', 'easy'].forEach((key) => {
    const span = dom.grade.querySelector(`[data-eta="${key}"]`);
    if (span) span.textContent = preview && preview[key] != null ? humanInterval(preview[key]) : '—';
  });
}

function renderCardMeta(progress, now) {
  if (!progress || !progress.count) {
    dom.gradeMeta.textContent = 'Новый вопрос — ещё не оценивался.';
    return;
  }
  const due = dueLabel(progress.next_due, now);
  const retention = Math.round((progress.retrievability ?? 0) * 100);
  dom.gradeMeta.textContent =
    `Повторов: ${progress.count} · средняя: ${progress.avg_score} · ` +
    `последняя: ${progress.last_score} · удержание: ${retention}% · следующий: ${due.text}`;
}

function renderQuestion(payload) {
  current = payload.question;
  poolSize = payload.pool_size;

  renderCrumbs(current);
  dom.question.innerHTML = current.question_html;

  dom.answer.open = false;                 // collapsed by default
  dom.answer.hidden = !current.has_answer;
  dom.noAnswer.hidden = current.has_answer;
  dom.answerBody.innerHTML = current.has_answer ? current.answer_html : '';
  if (current.has_answer) highlight(dom.answerBody);

  renderPreview(current.preview);
  renderCardMeta(current.progress, Math.floor(Date.now() / 1000));

  dom.dueBadge.hidden = (payload.due_count + payload.new_count) === 0;
  dom.dueBadge.textContent = `${payload.due_count} к повтору · ${payload.new_count} новых`;

  shown += 1;
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

  fillThemeSelect(dom.theme, 'Все темы', true);
  fillThemeSelect(dom.statsTheme, 'Все темы', false);
  syncSubthemes();
  dom.staleBadge.hidden = !index.status.stale;
}

function fillThemeSelect(select, allLabel, withCounts) {
  const saved = select.value;
  select.innerHTML = `<option value="">${allLabel}</option>`;
  index.themes
    .slice()
    .sort((a, b) => a.name.localeCompare(b.name, 'ru'))
    .forEach((theme) => {
      const option = document.createElement('option');
      option.value = theme.name;
      option.textContent = withCounts ? `${theme.name} (${theme.answered})` : theme.name;
      select.append(option);
    });
  select.value = saved;
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
  dom.skip.disabled = true;
  show('loading');

  try {
    const res = await fetch('/api/questions/random', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...filters(), exclude: current ? [current.id] : [] }),
    });
    const payload = await res.json();

    if (!res.ok) {
      fail(res.status === 503 ? 'Нет связи с источником' : 'Ничего не найдено',
        payload.detail || `HTTP ${res.status}`);
      return;
    }
    renderQuestion(payload);
  } catch (error) {
    fail('Ошибка загрузки', error.message);
  } finally {
    busy = false;
    dom.skip.disabled = false;
  }
}

async function grade(rating) {
  if (busy || !current || dom.card.hidden) return;
  const id = current.id;
  busy = true;

  const btn = dom.grade.querySelector(`[data-rating="${rating}"]`);
  btn?.classList.add('is-active');

  try {
    const res = await fetch('/api/reviews', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question_id: id, rating }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
  } catch (error) {
    btn?.classList.remove('is-active');
    busy = false;
    fail('Не удалось сохранить оценку', error.message);
    return;
  }

  busy = false;
  await nextQuestion();
  btn?.classList.remove('is-active');
}

/* ---------------------------------------------------------------- stats tab */

async function loadStats() {
  try {
    const res = await fetch('/api/stats');
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    stats.rows = data.rows;
    stats.now = data.now;
  } catch {
    stats.rows = [];
  }
  renderStats();
}

function renderStats() {
  const search = dom.statsSearch.value.trim().toLowerCase();
  const theme = dom.statsTheme.value;
  const dueOnly = dom.statsDueOnly.checked;

  const rows = stats.rows.filter((r) => {
    if (theme && r.theme !== theme) return false;
    if (dueOnly && !(r.next_due && r.next_due <= stats.now)) return false;
    if (search && !r.question_text.toLowerCase().includes(search)) return false;
    return true;
  });

  const { sortKey, sortDir } = stats;
  rows.sort((a, b) => {
    if (sortKey === 'question_text') return a[sortKey].localeCompare(b[sortKey], 'ru') * sortDir;
    return ((a[sortKey] ?? 0) - (b[sortKey] ?? 0)) * sortDir;
  });

  dom.statsBody.innerHTML = '';
  rows.forEach((r) => dom.statsBody.append(statsRow(r)));

  dom.statsEmpty.hidden = stats.rows.length > 0;
  dom.statsTable.hidden = stats.rows.length === 0;
  dom.statsCount.textContent = stats.rows.length ? `${rows.length} из ${stats.rows.length}` : '';

  dom.statsTable.querySelectorAll('th.sortable').forEach((th) => {
    const active = th.dataset.key === sortKey;
    th.classList.toggle('is-sorted', active);
    th.dataset.dir = active ? (sortDir > 0 ? 'asc' : 'desc') : '';
  });
}

function statsRow(r) {
  const tr = document.createElement('tr');
  if (r.orphaned) tr.className = 'is-orphaned';

  const q = document.createElement('td');
  q.className = 'stats-q';
  const path = [r.theme, r.subtheme, r.section].filter(Boolean).join(' › ');
  q.innerHTML = `<span class="stats-q__text"></span>` +
    `<span class="stats-q__path">${path}${r.orphaned ? ' · удалён из источника' : ''}</span>`;
  q.querySelector('.stats-q__text').textContent = r.question_text || r.question_id;

  const avg = document.createElement('td');
  avg.className = 'is-num';
  avg.append(scorePill(r.avg_score));

  const last = document.createElement('td');
  last.className = 'is-num';
  last.append(scorePill(r.last_score));

  const count = document.createElement('td');
  count.className = 'is-num';
  count.textContent = r.count;

  const ret = document.createElement('td');
  ret.className = 'is-num';
  ret.textContent = r.retrievability != null ? `${Math.round(r.retrievability * 100)}%` : '—';

  const due = document.createElement('td');
  due.className = 'is-num';
  const label = dueLabel(r.next_due, stats.now);
  due.innerHTML = `<span class="due ${label.overdue ? 'due--now' : ''}">${label.text}</span>`;

  tr.append(q, avg, last, count, ret, due);
  return tr;
}

function scorePill(value) {
  const span = document.createElement('span');
  span.className = `score-pill score-pill--${Math.round(value) || 0}`;
  span.textContent = value ? value : '—';
  return span;
}

/* -------------------------------------------------------------- settings tab */

const dateTimeFmt = new Intl.DateTimeFormat('ru-RU', { day: 'numeric', month: 'short', year: 'numeric' });

function sourceLabel(current) {
  if (current.source === 'trained') {
    const when = current.trained_at ? ' · ' + dateTimeFmt.format(new Date(current.trained_at * 1000)) : '';
    return `Обучено на ${current.trained_on_reviews} повторах${when}`;
  }
  if (current.source === 'custom') return 'Заданы вручную (FSRS_PARAMETERS)';
  return 'По умолчанию (популяционные)';
}

async function loadOptimizerStatus() {
  if (optimizing) return;               // don't disturb an in-flight run's status/message
  dom.optimStatus.textContent = '';
  dom.optimStatus.className = 'optim__status';
  try {
    const res = await fetch('/api/optimizer/status');
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    renderOptimizer(await res.json());
  } catch (error) {
    dom.optimHint.textContent = 'Не удалось загрузить статус: ' + error.message;
  }
}

function renderOptimizer(s) {
  dom.setRetention.textContent = `${Math.round(s.retention * 100)}%`;
  dom.setSource.textContent = sourceLabel(s.current);

  const pct = Math.min(100, Math.round((s.effective_reviews / s.required) * 100));
  dom.optimFill.style.width = `${pct}%`;
  dom.optimFill.classList.toggle('optim__fill--ready', s.ready);
  dom.optimCount.textContent = `${s.effective_reviews} / ${s.required} повторов для обучения`;

  if (!s.optimizer_available) {
    dom.optimWarn.hidden = false;
    dom.optimWarn.textContent =
      'Оптимизатор не установлен на сервере. Добавьте его: uv sync --extra optimizer';
    dom.optimHint.textContent = '';
  } else {
    dom.optimWarn.hidden = true;
    dom.optimHint.textContent = s.ready
      ? `Достаточно данных · всего повторов: ${s.review_count}`
      : `Ещё ${s.remaining} повторений в разные дни (всего сделано: ${s.review_count})`;
  }
  dom.optimizeBtn.disabled = optimizing || !(s.ready && s.optimizer_available);
}

async function runOptimizer() {
  if (optimizing) return;
  optimizing = true;
  dom.optimizeBtn.disabled = true;
  dom.optimStatus.textContent = 'Обучение… это может занять до минуты';
  dom.optimStatus.className = 'optim__status optim__status--busy';
  try {
    const res = await fetch('/api/optimizer/run', { method: 'POST' });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    optimizing = false;
    renderOptimizer(data);   // reflects the fresh (trained) status on the button
    dom.optimStatus.textContent =
      `Готово — веса обновлены и применены (${data.weights.length} параметров), без перезапуска`;
    dom.optimStatus.className = 'optim__status optim__status--ok';
  } catch (error) {
    optimizing = false;
    dom.optimStatus.textContent = 'Ошибка: ' + error.message;
    dom.optimStatus.className = 'optim__status optim__status--err';
    dom.optimizeBtn.disabled = false;   // allow a retry
  }
}

/* --------------------------------------------------------------- controls */

const HLJS_BASE = 'https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles';

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  localStorage.setItem(THEME_KEY, theme);
  const hljsTheme = document.getElementById('hljs-theme');
  if (hljsTheme) hljsTheme.href = `${HLJS_BASE}/github${theme === 'dark' ? '-dark' : ''}.min.css`;
}

function restorePreferences() {
  applyTheme(localStorage.getItem(THEME_KEY) || 'dark');
  try {
    const saved = JSON.parse(localStorage.getItem(FILTERS_KEY));
    if (saved) {
      dom.theme.value = saved.theme || '';
      dom.subtheme.value = saved.subtheme || '';
      dom.answeredOnly.checked = saved.answered_only !== false;
      dom.spacedMode.checked = saved.mode !== 'random';
    }
  } catch { /* first run */ }
}

function bind() {
  dom.tabTrain.addEventListener('click', () => showTab('train'));
  dom.tabStats.addEventListener('click', () => showTab('stats'));
  dom.tabSettings.addEventListener('click', () => showTab('settings'));
  dom.optimizeBtn.addEventListener('click', runOptimizer);

  dom.skip.addEventListener('click', nextQuestion);
  dom.messageRetry.addEventListener('click', nextQuestion);

  dom.grade.addEventListener('click', (event) => {
    const btn = event.target.closest('[data-rating]');
    if (btn) grade(Number(btn.dataset.rating));
  });

  const refilter = () => { saveFilters(); nextQuestion(); };
  dom.theme.addEventListener('change', () => { dom.subtheme.value = ''; syncSubthemes(); refilter(); });
  dom.subtheme.addEventListener('change', refilter);
  dom.answeredOnly.addEventListener('change', refilter);
  dom.spacedMode.addEventListener('change', refilter);

  dom.themeToggle.addEventListener('click', () => {
    applyTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark');
  });

  dom.refresh.addEventListener('click', async () => {
    dom.refresh.classList.add('spinning');
    try {
      await fetch('/api/refresh', { method: 'POST' });
      await loadIndex();
      if (!dom.viewTrain.hidden) await nextQuestion();
      else if (!dom.viewStats.hidden) await loadStats();
      else await loadOptimizerStatus();
    } catch (error) {
      fail('Не удалось обновить', error.message);
    } finally {
      dom.refresh.classList.remove('spinning');
    }
  });

  dom.statsSearch.addEventListener('input', renderStats);
  dom.statsTheme.addEventListener('change', renderStats);
  dom.statsDueOnly.addEventListener('change', renderStats);
  dom.statsTable.querySelectorAll('th.sortable').forEach((th) => {
    th.addEventListener('click', () => {
      const key = th.dataset.key;
      if (stats.sortKey === key) stats.sortDir *= -1;
      else { stats.sortKey = key; stats.sortDir = key === 'next_due' ? 1 : -1; }
      renderStats();
    });
  });

  document.addEventListener('keydown', (event) => {
    if (event.target.matches('input, select, textarea')) return;
    if (dom.viewTrain.hidden) return;                      // shortcuts are training-only

    if (event.key >= '1' && event.key <= '4') {
      event.preventDefault();
      grade(Number(event.key));
    } else if (event.key === 'n' || event.key === 'N' || event.key === 'ArrowRight') {
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

  const initial = location.hash.slice(1);   // deep-link: /#stats or /#settings
  if (initial === 'stats' || initial === 'settings') showTab(initial);
})();
