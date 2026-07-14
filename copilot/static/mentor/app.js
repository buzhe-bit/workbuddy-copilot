// 导师观察台前端逻辑
// 导师干预雷达 + 原三栏：学员列表 / 对话列表 / 时间线
//
// 架构：内存 state 单一数据源
//   - 所有 render 只读 state；事件只改 state 再 render（不从 DOM 反读）
//   - 学员可控文本一律 textContent / createElement，禁止 innerHTML 拼接（防 XSS）
//
// 数据来源：
//   GET  /api/mentor/students                       学员列表
//   GET  /api/mentor/students/{id}/sessions         某学员的对话列表
//   GET  /api/mentor/sessions/{id}/timeline         某对话的时间线
//   POST /api/mentor/message {student_id,text,client_request_id} 幂等发消息
//   POST /api/mentor/messages/status               断线/响应丢失后补查送达状态
//   WS   /ws/mentor                                 实时事件推送

'use strict';

// ─────────────────────────────────────────────────────────────
// 单一数据源
// ─────────────────────────────────────────────────────────────
const state = {
  attention: newAttentionState(),
  systemStatus: null,
  // 响应式界面状态独立于业务数据；移动端一次只展示一个工作区。
  ui: { mobileView: 'attention' },
  students: [],          // [{student_id, display_name, last_severity, session_count, analysis_count, alert_count, ...}]
  sessions: [],          // [{session_id, session_title, last_severity, analysis_count, alert_count, ...}]
  timeline: [],          // 归一化条目（见 normalize* 函数）
  currentStudentId: null,
  currentSessionId: null,
  lastSeenMessageId: null,
  // 对话列表分组折叠态（空间/任务），跨重渲染保留（事件只改此处再 render）
  groupCollapsed: { space: false, task: false },
  // 会话级完整对话原文（懒加载，一处入口共享；切会话即重置）
  transcript: newTranscriptState(),
  // 导师触发上传的唯一状态源；REST/WS 都先归一化到这里再渲染。
  uploadRequest: newUploadRequestState(),
  // AI 回复摘要卡「显示详情」热加载缓存，按 reply_ref 存：
  //   { open, loading, content:null|string, failed } —— 展开态与已加载内容都在此，
  //   不从 DOM 反读、跨重渲染保留；加载过一次即缓存，隐藏后再展开不重新请求。
  replies: {},
  // 出站导师消息独立于当前 timeline，切学员后仍能匹配送达回执。
  outboundMessages: [],
  pendingDeliveryReceipts: [],
};

// 取（惰性创建）某 reply_ref 的回复展开态
function replyState(replyRef) {
  const key = String(replyRef);
  if (!state.replies[key]) {
    state.replies[key] = { open: false, loading: false, content: null, failed: false };
  }
  return state.replies[key];
}

// 换会话/换学员 → 清空上一会话的 AI 回复展开缓存
function resetReplies() {
  state.replies = {};
}

// transcript 状态机：content 命中即成功；missing=404；failed=其他错误；loading=请求中
function newTranscriptState() {
  return { open: false, loading: false, content: null, missing: false, failed: false };
}

function resetTranscript() {
  state.transcript = newTranscriptState();
}

function newUploadRequestState() {
  return {
    requestId: null,
    studentId: null,
    transferStatus: null,
    analysisStatus: null,
    transferError: '',
    analysisError: '',
    result: null,
    updatedAt: 0,
  };
}

function newAttentionState() {
  return {
    items: [],
    filters: { status: 'active', priority: '', category: '', studentId: '' },
    loading: false,
    error: '',
    loadGeneration: 0,
    revision: 0,
    itemRevisions: {},
  };
}

const attentionListEl = document.getElementById('attention-list');
const attentionFeedbackEl = document.getElementById('attention-feedback');
const attentionRefreshBtn = document.getElementById('attention-refresh');
const attentionStatusFilter = document.getElementById('attention-status-filter');
const attentionPriorityFilter = document.getElementById('attention-priority-filter');
const attentionCategoryFilter = document.getElementById('attention-category-filter');
const attentionStudentFilter = document.getElementById('attention-student-filter');
const studentListEl = document.getElementById('student-list');
const sessionListEl = document.getElementById('session-list');
const timelineEl = document.getElementById('timeline');
const wsStatusEl = document.getElementById('ws-status');
const systemStatusEl = document.getElementById('system-status');
const composeForm = document.getElementById('compose');
const composeInput = document.getElementById('compose-input');
const composeSend = document.getElementById('compose-send');
const transcriptEntryEl = document.getElementById('transcript-entry');
const transcriptBodyEl = document.getElementById('transcript-body');
const syncBtn = document.getElementById('sync-student');
const syncFeedbackEl = document.getElementById('sync-feedback');
const retryAnalysisBtn = document.getElementById('retry-analysis');
const mobileTablist = document.querySelector('[role="tablist"][aria-label="导师工作区"]');
const mobileTabs = Array.from(document.querySelectorAll('[role="tab"][data-view]'));
const mobilePanels = {
  attention: document.querySelector('[data-mobile-panel="attention"]') ||
    document.querySelector('.panel-attention'),
  students: document.querySelector('[data-mobile-panel="students"]') ||
    document.querySelector('.panel-students'),
  conversation: document.querySelector('[data-mobile-panel="conversation"]') ||
    document.querySelector('.conversation-workspace') ||
    document.querySelector('.panel-timeline'),
};
const mobileMedia = window.matchMedia('(max-width: 599px)');
let responsiveWasMobile = mobileMedia.matches;
let lastMobileFocusView = state.ui.mobileView;
let lastMobileFocusWasTab = false;
let hadWorkspaceFocus = false;

let outboundSeq = 0; // 出站消息本地唯一 id 生成器
let uploadPollController = null;
let uploadPollTimer = null;
let uploadTrackingGeneration = 0;
let uploadAttemptGeneration = 0;
let studentSelectionGeneration = 0;
let studentLoadGeneration = 0;
let systemStatusLoadGeneration = 0;
let studentAggregateRefreshTimer = null;
let mentorReauthPromise = null;
const MENTOR_TOKEN_STORAGE_KEY = 'workbuddy_copilot_mentor_token';
const MENTOR_ID_STORAGE_KEY = 'workbuddy_copilot_mentor_id';
let fallbackMentorId = '';
const MAX_OUTBOUND_MESSAGES = 300;
const MAX_MESSAGE_STATUS_BATCH = 300;

// ─────────────────────────────────────────────────────────────
// 工具
// ─────────────────────────────────────────────────────────────
function severityClass(severity) {
  if (severity === 'error') return 'status-red';
  if (severity === 'warn') return 'status-yellow';
  return 'status-green';
}

function formatTime(ts) {
  if (!ts) return '';
  // 后端时间戳为秒；容错：若像毫秒则按毫秒处理
  const ms = ts > 1e12 ? ts : ts * 1000;
  const d = new Date(ms);
  if (isNaN(d.getTime())) return '';
  return d.toLocaleString('zh-CN', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

function currentStudentName() {
  const s = state.students.find((x) => x.student_id === state.currentStudentId);
  return (s && (s.display_name || s.student_id)) || state.currentStudentId || '学员';
}

function storedMentorToken() {
  return sessionStorage.getItem(MENTOR_TOKEN_STORAGE_KEY) || '';
}

function askMentorToken() {
  let token = window.prompt('请输入导师访问 token') || '';
  if (!token) {
    return '';
  }
  token = token.trim();
  if (token) sessionStorage.setItem(MENTOR_TOKEN_STORAGE_KEY, token);
  return token;
}

function clearMentorToken() {
  sessionStorage.removeItem(MENTOR_TOKEN_STORAGE_KEY);
}

function currentMentorId() {
  try {
    const stored = localStorage.getItem(MENTOR_ID_STORAGE_KEY);
    if (stored) return stored;
    const suffix = window.crypto && typeof window.crypto.randomUUID === 'function'
      ? window.crypto.randomUUID()
      : Date.now().toString(36) + '-' + Math.random().toString(36).slice(2);
    const created = 'mentor-console-' + suffix;
    localStorage.setItem(MENTOR_ID_STORAGE_KEY, created);
    return created;
  } catch (err) {
    if (!fallbackMentorId) {
      fallbackMentorId = 'mentor-console-' + Date.now().toString(36) + '-' +
        Math.random().toString(36).slice(2);
    }
    return fallbackMentorId;
  }
}

function newClientRequestId() {
  const suffix = window.crypto && typeof window.crypto.randomUUID === 'function'
    ? window.crypto.randomUUID()
    : Date.now().toString(36) + '-' + (++outboundSeq) + '-' +
      Math.random().toString(36).slice(2);
  return 'mentor-message-' + suffix;
}

function reauthenticateMentor(failedToken) {
  const available = storedMentorToken();
  if (available && available !== failedToken) return Promise.resolve(available);
  if (!mentorReauthPromise) {
    mentorReauthPromise = Promise.resolve().then(() => {
      const current = storedMentorToken();
      if (current && current !== failedToken) return current;
      clearMentorToken();
      wsStatusEl.textContent = '认证失败';
      return askMentorToken();
    }).finally(() => {
      mentorReauthPromise = null;
    });
  }
  return mentorReauthPromise;
}

async function authFetch(url, options) {
  const opts = Object.assign({}, options || {});
  const headers = new Headers(opts.headers || {});
  const token = storedMentorToken();
  if (token) {
    headers.set('Authorization', 'Bearer ' + token);
    headers.set('X-Copilot-Token', token);
  }
  opts.headers = headers;
  const resp = await fetch(url, opts);
  if (resp.status === 401) {
    const retryToken = await reauthenticateMentor(token);
    if (retryToken) {
      const retryOpts = Object.assign({}, options || {});
      const retryHeaders = new Headers(retryOpts.headers || {});
      retryHeaders.set('Authorization', 'Bearer ' + retryToken);
      retryHeaders.set('X-Copilot-Token', retryToken);
      retryOpts.headers = retryHeaders;
      return fetch(url, retryOpts);
    }
  }
  return resp;
}

function mentorWsUrl() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const url = new URL(proto + '//' + location.host + '/ws/mentor');
  const token = storedMentorToken();
  if (token) url.searchParams.set('token', token);
  return url.toString();
}

// 创建带 class 的元素并（可选）安全地设置文本
function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text; // textContent → XSS 安全
  return node;
}

function makeKeyboardActivatable(node, activate) {
  node.setAttribute('role', 'button');
  node.tabIndex = 0;
  node.addEventListener('click', activate);
  node.addEventListener('keydown', (event) => {
    if (event.key !== 'Enter' && event.key !== ' ') return;
    event.preventDefault();
    activate();
  });
  return node;
}

// ─────────────────────────────────────────────────────────────
// 三档响应式导航（只管理可见工作区，不复制任何业务数据或 DOM）
// ─────────────────────────────────────────────────────────────
function isMobileLayout() {
  return mobileMedia.matches;
}

function mobileViewForNode(node) {
  if (!(node instanceof Element)) return null;
  const tab = node.closest('[role="tab"][data-view]');
  if (tab && tab.dataset.view) return tab.dataset.view;
  for (const [view, panel] of Object.entries(mobilePanels)) {
    if (panel && panel.contains(node)) return view;
  }
  return null;
}

function elementIsRendered(node) {
  if (!(node instanceof HTMLElement)) return false;
  return node.getClientRects().length > 0 && getComputedStyle(node).visibility !== 'hidden';
}

function focusWorkspaceEntry(view) {
  const panel = mobilePanels[view];
  if (!panel) return;
  const selected = Array.from(panel.querySelectorAll(
    '.student-item[aria-pressed="true"], .session-item[aria-pressed="true"]'
  )).find((candidate) => elementIsRendered(candidate));
  if (selected) {
    selected.focus();
    return;
  }
  const candidates = Array.from(panel.querySelectorAll(
    'button:not([disabled]), input:not([disabled]), select:not([disabled]), ' +
    'summary, [role="button"][tabindex="0"]'
  ));
  const firstControl = candidates.find((candidate) => elementIsRendered(candidate));
  if (firstControl) {
    firstControl.focus();
    return;
  }
  // conversation wrapper 在宽屏是 display:contents，不能作为可靠焦点兜底；
  // 标题始终对应一个真实可见面板，也能向键盘/读屏用户说明当前位置。
  const heading = Array.from(panel.querySelectorAll('h2')).find(
    (candidate) => elementIsRendered(candidate)
  );
  const fallback = heading || (elementIsRendered(panel) ? panel : null);
  if (!fallback) return;
  fallback.tabIndex = -1;
  fallback.focus();
}

function syncResponsiveMode() {
  const mobile = isMobileLayout();
  const enteringMobile = mobile && !responsiveWasMobile;
  const activeBefore = document.activeElement;
  const focusedView = mobileViewForNode(activeBefore);
  const leavingMobileTab = !mobile && responsiveWasMobile &&
    (
      lastMobileFocusWasTab ||
      (activeBefore instanceof Element && !!activeBefore.closest('[role="tab"][data-view]'))
    );

  // 桌面/平板进入移动断点时，以当前键盘焦点所在工作区为准，避免把它隐藏。
  const enteringFocusView = focusedView || (hadWorkspaceFocus ? lastMobileFocusView : null);
  if (enteringMobile && enteringFocusView) {
    state.ui.mobileView = enteringFocusView;
  }
  const activeView = state.ui.mobileView;
  document.body.dataset.mobileView = activeView;
  mobileTabs.forEach((tab) => {
    const selected = tab.dataset.view === activeView;
    tab.setAttribute('aria-selected', selected ? 'true' : 'false');
    tab.tabIndex = selected ? 0 : -1;
  });
  Object.entries(mobilePanels).forEach(([view, panel]) => {
    if (!panel) return;
    if (mobile) {
      panel.setAttribute('role', 'tabpanel');
      panel.setAttribute('aria-labelledby', view + '-tab');
      panel.setAttribute('aria-hidden', view === activeView ? 'false' : 'true');
    } else {
      // 大/中屏没有页签关系，避免可见区域被一个隐藏 tab 错误命名。
      panel.removeAttribute('role');
      panel.removeAttribute('aria-hidden');
      if (view === 'attention') panel.setAttribute('aria-labelledby', 'attention-heading');
      else if (view === 'students') panel.setAttribute('aria-labelledby', 'students-heading');
      else panel.removeAttribute('aria-labelledby');
    }
  });
  responsiveWasMobile = mobile;

  if (leavingMobileTab) {
    const restoreView = focusedView || lastMobileFocusView || activeView;
    focusWorkspaceEntry(restoreView);
    // Chromium 可能在 media change 回调后才完成 display:none 导航的 blur；
    // 下一帧只在焦点仍不可见时补一次，避免覆盖用户已主动移动的焦点。
    window.requestAnimationFrame(() => {
      if (!elementIsRendered(document.activeElement)) focusWorkspaceEntry(restoreView);
    });
    lastMobileFocusWasTab = false;
  } else if (enteringMobile && enteringFocusView && hadWorkspaceFocus) {
    window.requestAnimationFrame(() => {
      if (!elementIsRendered(document.activeElement)) focusWorkspaceEntry(enteringFocusView);
    });
  } else if (
    mobile &&
    activeBefore instanceof HTMLElement &&
    activeBefore !== document.body &&
    !elementIsRendered(activeBefore)
  ) {
    const activeTab = mobileTabs.find((tab) => tab.dataset.view === activeView);
    if (activeTab) activeTab.focus();
  }
}

function setMobileView(view, { focusTab = false } = {}) {
  if (!Object.prototype.hasOwnProperty.call(mobilePanels, view)) return;
  const tab = mobileTabs.find((candidate) => candidate.dataset.view === view);
  if (focusTab && isMobileLayout() && tab) tab.focus();
  state.ui.mobileView = view;
  syncResponsiveMode();
}

function moveMobileTabFocus(event) {
  if (!isMobileLayout() || !mobileTabs.length) return;
  const currentIndex = mobileTabs.indexOf(event.currentTarget);
  if (currentIndex < 0) return;
  let nextIndex = currentIndex;
  if (event.key === 'ArrowRight') nextIndex = (currentIndex + 1) % mobileTabs.length;
  else if (event.key === 'ArrowLeft') {
    nextIndex = (currentIndex - 1 + mobileTabs.length) % mobileTabs.length;
  } else if (event.key === 'Home') nextIndex = 0;
  else if (event.key === 'End') nextIndex = mobileTabs.length - 1;
  else return;
  event.preventDefault();
  setMobileView(mobileTabs[nextIndex].dataset.view, { focusTab: true });
}

mobileTabs.forEach((tab) => {
  tab.addEventListener('click', () => setMobileView(tab.dataset.view));
  tab.addEventListener('keydown', moveMobileTabFocus);
});
document.addEventListener('focusin', (event) => {
  const target = event.target;
  const view = mobileViewForNode(target);
  if (view) {
    lastMobileFocusView = view;
    hadWorkspaceFocus = true;
  }
  if (isMobileLayout()) {
    lastMobileFocusWasTab = target instanceof Element &&
      !!target.closest('[role="tab"][data-view]');
  }
});
if (typeof mobileMedia.addEventListener === 'function') {
  mobileMedia.addEventListener('change', syncResponsiveMode);
} else if (typeof mobileMedia.addListener === 'function') {
  mobileMedia.addListener(syncResponsiveMode);
}

// 导师干预雷达：REST 是权威补拉，WS/PATCH 按 id + updated_at 增量合并。
function attentionIsActive(item) {
  return item && (item.status === 'open' || item.status === 'in_progress');
}

function attentionUpdatedAt(item) {
  const value = Number(item && item.updated_at);
  return Number.isFinite(value) ? value : 0;
}

function attentionStatusRank(status) {
  if (status === 'resolved' || status === 'dismissed') return 2;
  if (status === 'in_progress') return 1;
  return 0;
}

function upsertAttentionItem(incoming) {
  if (!incoming || incoming.id == null) return { changed: false, previous: null };
  const itemId = String(incoming.id);
  const index = state.attention.items.findIndex((item) => String(item.id) === itemId);
  const previous = index >= 0 ? state.attention.items[index] : null;
  if (previous) {
    const incomingUpdatedAt = attentionUpdatedAt(incoming);
    const previousUpdatedAt = attentionUpdatedAt(previous);
    if (incomingUpdatedAt < previousUpdatedAt) {
      return { changed: false, previous: previous };
    }
    if (
      incomingUpdatedAt === previousUpdatedAt &&
      attentionStatusRank(incoming.status) < attentionStatusRank(previous.status)
    ) {
      return { changed: false, previous: previous };
    }
  }
  const next = Object.assign({}, previous || {}, incoming);
  if (previous && JSON.stringify(next) === JSON.stringify(previous)) {
    return { changed: false, previous: previous };
  }
  if (index >= 0) state.attention.items[index] = next;
  else state.attention.items.push(next);
  return { changed: true, previous: previous };
}

function recordAttentionMutation(itemId) {
  state.attention.revision += 1;
  state.attention.itemRevisions[String(itemId)] = state.attention.revision;
}

function attentionPriorityRank(priority) {
  if (priority === 'high') return 2;
  if (priority === 'medium') return 1;
  return 0;
}

function compareAttentionItems(a, b) {
  const priorityDelta = attentionPriorityRank(b.priority) - attentionPriorityRank(a.priority);
  if (priorityDelta) return priorityDelta;
  const createdDelta = (Number(a.created_at) || 0) - (Number(b.created_at) || 0);
  if (createdDelta) return createdDelta;
  return (Number(a.id) || 0) - (Number(b.id) || 0);
}

function attentionStudentName(studentId) {
  const student = state.students.find((item) => item.student_id === studentId);
  return (student && (student.display_name || student.student_id)) || studentId || '未知学员';
}

function attentionStatusText(status) {
  const labels = {
    open: '未处理',
    in_progress: '处理中',
    resolved: '已解决',
    dismissed: '已忽略',
  };
  return labels[status] || '未知状态';
}

function attentionMatchesFilters(item) {
  const filters = state.attention.filters;
  if (filters.status === 'active' && !attentionIsActive(item)) return false;
  if (filters.status && filters.status !== 'active' && item.status !== filters.status) return false;
  if (filters.priority && item.priority !== filters.priority) return false;
  if (filters.category && item.category !== filters.category) return false;
  if (filters.studentId && item.student_id !== filters.studentId) return false;
  return true;
}

function renderAttentionStudentFilter() {
  if (!attentionStudentFilter) return;
  const selected = state.attention.filters.studentId;
  attentionStudentFilter.innerHTML = '';
  const all = el('option', null, '全部');
  all.value = '';
  attentionStudentFilter.appendChild(all);
  state.students.forEach((student) => {
    const option = el(
      'option',
      null,
      student.display_name || student.student_id || '(未命名学员)'
    );
    option.value = student.student_id;
    attentionStudentFilter.appendChild(option);
  });
  if (selected && !state.students.some((student) => student.student_id === selected)) {
    const option = el('option', null, selected);
    option.value = selected;
    attentionStudentFilter.appendChild(option);
  }
  attentionStudentFilter.value = selected;
}

function makeAttentionAction(label, action, item, primary) {
  const button = el('button', primary ? 'primary' : '', label);
  button.type = 'button';
  button.dataset.action = action;
  button.disabled = !!item._updating;
  if (action === 'view') {
    button.addEventListener('click', () => focusAttentionContext(item));
  } else if (action === 'prefill') {
    button.addEventListener('click', () => prefillAttentionSuggestion(item));
  } else {
    button.addEventListener('click', () => updateAttentionStatus(item.id, action));
  }
  return button;
}

function buildAttentionCard(item) {
  const priority = item.priority === 'high' ? 'high' : 'medium';
  const category = item.category === 'system' ? 'system' : 'learning';
  const card = el('li', 'attention-card priority-' + priority);
  card.dataset.attentionId = String(item.id);
  if (item._updating) card.classList.add('updating');

  const top = el('div', 'attention-card-top');
  top.appendChild(el('span', 'attention-student-name', attentionStudentName(item.student_id)));
  top.appendChild(el('span', 'attention-priority ' + priority, priority === 'high' ? '高' : '中'));
  card.appendChild(top);

  const meta = el('div', 'attention-card-meta');
  meta.appendChild(el(
    'span', 'attention-category ' + category,
    category === 'system' ? '系统异常' : '学习关注'
  ));
  meta.appendChild(el('span', 'attention-status', attentionStatusText(item.status)));
  const confidence = Math.round(Math.max(0, Math.min(1, Number(item.confidence) || 0)) * 100);
  meta.appendChild(el('span', 'attention-confidence', '置信度 ' + confidence + '%'));
  meta.appendChild(el('span', 'attention-created-at', formatTime(item.created_at)));
  card.appendChild(meta);

  card.appendChild(el('div', 'attention-reason', item.reason || '需要导师关注'));
  const evidence = Array.isArray(item.evidence) ? item.evidence.slice(0, 3) : [];
  if (evidence.length) {
    const list = el('ul', 'attention-evidence');
    evidence.forEach((value) => list.appendChild(el('li', null, value)));
    card.appendChild(list);
  }
  if (item.suggested_action) {
    card.appendChild(el('div', 'attention-action', '建议：' + item.suggested_action));
  }
  if (item._error) card.appendChild(el('div', 'attention-error', item._error));

  const actions = el('div', 'attention-card-actions');
  actions.appendChild(makeAttentionAction('查看对话', 'view', item, false));
  actions.appendChild(makeAttentionAction('填入消息框', 'prefill', item, true));
  if (item.status === 'open') {
    actions.appendChild(makeAttentionAction('处理中', 'in_progress', item, false));
  }
  if (attentionIsActive(item)) {
    actions.appendChild(makeAttentionAction('已解决', 'resolved', item, false));
    actions.appendChild(makeAttentionAction('忽略', 'dismissed', item, false));
  }
  card.appendChild(actions);
  return card;
}

function renderAttention() {
  if (!attentionListEl) return;
  attentionListEl.innerHTML = '';
  attentionListEl.setAttribute('aria-busy', state.attention.loading ? 'true' : 'false');
  if (attentionRefreshBtn) attentionRefreshBtn.disabled = state.attention.loading;
  if (attentionFeedbackEl) attentionFeedbackEl.textContent = '';
  if (state.attention.loading) {
    attentionListEl.appendChild(el('li', 'attention-loading', '正在加载关注队列…'));
  }
  if (state.attention.error) {
    attentionListEl.appendChild(el('li', 'attention-error', '关注队列加载失败'));
  }
  const visible = state.attention.items
    .filter(attentionMatchesFilters)
    .slice()
    .sort(compareAttentionItems);
  visible.forEach((item) => attentionListEl.appendChild(buildAttentionCard(item)));
  if (!state.attention.loading && !state.attention.error && !visible.length) {
    attentionListEl.appendChild(el('li', 'attention-empty', '暂无需要关注的学员'));
  }
}

function attentionStatusesForFilter(statusFilter) {
  const statuses = ['open', 'in_progress'];
  if (statusFilter === 'resolved' || statusFilter === 'dismissed') {
    statuses.push(statusFilter);
  } else if (!statusFilter) {
    statuses.push('resolved', 'dismissed');
  }
  return statuses;
}

function attentionMatchesLoadScope(item, statuses, filters) {
  if (!statuses.includes(item.status)) return false;
  if (filters.priority && item.priority !== filters.priority) return false;
  if (filters.category && item.category !== filters.category) return false;
  if (filters.studentId && item.student_id !== filters.studentId) return false;
  return true;
}

async function loadAttention() {
  const generation = ++state.attention.loadGeneration;
  const startingRevision = state.attention.revision;
  const filters = Object.assign({}, state.attention.filters);
  const requestedStatuses = attentionStatusesForFilter(filters.status);
  state.attention.loading = true;
  state.attention.error = '';
  renderAttention();
  try {
    const responses = await Promise.all(requestedStatuses.map((status) => {
      const params = new URLSearchParams({ status: status, limit: '200' });
      if (filters.priority) {
        params.set('priority', filters.priority);
      }
      if (filters.category) {
        params.set('category', filters.category);
      }
      if (filters.studentId) {
        params.set('student_id', filters.studentId);
      }
      return authFetch('/api/mentor/attention?' + params.toString());
    }));
    const failed = responses.find((resp) => !resp.ok);
    if (failed) throw new Error('attention_http_' + failed.status);
    const payloads = await Promise.all(responses.map((resp) => resp.json()));
    if (generation !== state.attention.loadGeneration) return;
    state.attention.items = state.attention.items.filter((item) => {
      if (!attentionMatchesLoadScope(item, requestedStatuses, filters)) return true;
      return (state.attention.itemRevisions[String(item.id)] || 0) > startingRevision;
    });
    payloads.forEach((data) => {
      const incoming = Array.isArray(data.items) ? data.items : [];
      incoming.forEach((item) => upsertAttentionItem(item));
    });
  } catch (err) {
    if (generation !== state.attention.loadGeneration) return;
    console.error('加载关注队列失败', err);
    state.attention.error = 'load_failed';
  } finally {
    if (generation === state.attention.loadGeneration) {
      state.attention.loading = false;
      renderAttention();
    }
  }
}

async function refreshAttentionData() {
  await Promise.all([loadStudents(), loadAttention(), loadSystemStatus()]);
}

function scheduleStudentAggregateRefresh() {
  if (studentAggregateRefreshTimer != null) {
    clearTimeout(studentAggregateRefreshTimer);
  }
  studentAggregateRefreshTimer = setTimeout(() => {
    studentAggregateRefreshTimer = null;
    loadStudents();
  }, 50);
}

async function focusAttentionContext(item) {
  if (!item || !item.student_id) return false;
  if (attentionFeedbackEl) attentionFeedbackEl.textContent = '';
  if (state.currentStudentId !== item.student_id) {
    const selected = await selectStudent(item.student_id);
    if (!selected || state.currentStudentId !== item.student_id) return false;
  }
  if (item.session_id && state.currentSessionId !== item.session_id) {
    const sessionExists = state.sessions.some((session) => session.session_id === item.session_id);
    if (!sessionExists) {
      if (attentionFeedbackEl) {
        attentionFeedbackEl.textContent = '来源对话当前不可用，已定位到学员';
      }
      setMobileView('students', { focusTab: true });
      return true;
    }
    await selectSession(item.session_id);
    if (state.currentStudentId !== item.student_id || state.currentSessionId !== item.session_id) {
      return false;
    }
  }
  if (item.session_id) {
    setMobileView('conversation', { focusTab: true });
  } else {
    setMobileView('students', { focusTab: true });
  }
  return true;
}

async function prefillAttentionSuggestion(item) {
  const focused = await focusAttentionContext(item);
  if (!focused || !item.suggested_action) return;
  setMobileView('conversation', { focusTab: true });
  composeInput.value = item.suggested_action;
  composeInput.focus();
}

async function updateAttentionStatus(itemId, status) {
  const item = state.attention.items.find((candidate) => String(candidate.id) === String(itemId));
  if (!item || item._updating) return;
  item._updating = true;
  item._error = '';
  renderAttention();
  let conflict = false;
  try {
    const resp = await authFetch('/api/mentor/attention/' + encodeURIComponent(itemId), {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ status: status, mentor_id: currentMentorId(), note: '' }),
    });
    if (!resp.ok) {
      conflict = resp.status === 409;
      throw new Error('attention_patch_http_' + resp.status);
    }
    const incoming = await resp.json();
    incoming._updating = false;
    incoming._error = '';
    const result = upsertAttentionItem(incoming);
    if (result.changed) {
      recordAttentionMutation(incoming.id);
      scheduleStudentAggregateRefresh();
    }
  } catch (err) {
    console.error('更新关注项失败', err);
    if (conflict) await refreshAttentionData();
    const current = state.attention.items.find(
      (candidate) => String(candidate.id) === String(itemId)
    ) || item;
    current._updating = false;
    current._error = '更新失败，请重试';
  } finally {
    const current = state.attention.items.find((candidate) => String(candidate.id) === String(itemId));
    if (current) current._updating = false;
    renderAttention();
  }
}

function acceptAttentionEvent(payload) {
  const incoming = payload && payload.item;
  if (!incoming) return;
  const result = upsertAttentionItem(incoming);
  if (!result.changed) return;
  recordAttentionMutation(incoming.id);
  // students 聚合没有与 WS 共享版本水位，不做本地 +/- 猜测；
  // 事件持久化后 debounce 回读权威聚合，generation 丢弃旧响应。
  scheduleStudentAggregateRefresh();
  renderAttention();
}

if (attentionRefreshBtn) attentionRefreshBtn.addEventListener('click', refreshAttentionData);
if (attentionStatusFilter) attentionStatusFilter.addEventListener('change', () => {
  state.attention.filters.status = attentionStatusFilter.value;
  renderAttention();
  loadAttention();
});
if (attentionPriorityFilter) attentionPriorityFilter.addEventListener('change', () => {
  state.attention.filters.priority = attentionPriorityFilter.value;
  renderAttention();
  loadAttention();
});
if (attentionCategoryFilter) attentionCategoryFilter.addEventListener('change', () => {
  state.attention.filters.category = attentionCategoryFilter.value;
  renderAttention();
  loadAttention();
});
if (attentionStudentFilter) attentionStudentFilter.addEventListener('change', () => {
  state.attention.filters.studentId = attentionStudentFilter.value;
  renderAttention();
  loadAttention();
});

// ─────────────────────────────────────────────────────────────
// 学员列表
// ─────────────────────────────────────────────────────────────
async function loadStudents() {
  const generation = ++studentLoadGeneration;
  try {
    const resp = await authFetch('/api/mentor/students');
    if (!resp.ok) throw new Error('students_http_' + resp.status);
    const data = await resp.json();
    if (generation !== studentLoadGeneration) return;
    state.students = data.items || [];
    renderStudents();
    renderAttentionStudentFilter();
    renderAttention();
  } catch (err) {
    if (generation === studentLoadGeneration) {
      console.error('加载学员列表失败', err);
    }
  }
}

function windowsRolloutLabel(status) {
  const normalized = String(status || '').toLowerCase();
  if (normalized.includes('rollout_ready')) return 'Windows 可试点';
  if (normalized.includes('implementation_candidate')) return 'Windows 候选版';
  return 'Windows 待实机';
}

function renderSystemStatus() {
  if (!systemStatusEl || !state.systemStatus) return;
  const pending = Math.max(0, Number(state.systemStatus.pending_analyses) || 0);
  const failed = Math.max(0, Number(state.systemStatus.failed_analyses) || 0);
  const windows = windowsRolloutLabel(state.systemStatus.windows_rollout_status);
  const parts = [];
  if (pending) parts.push('待分析 ' + pending);
  if (failed) parts.push('失败 ' + failed);
  if (!parts.length) parts.push('系统正常');
  parts.push(windows);
  systemStatusEl.textContent = parts.join(' · ');
  systemStatusEl.dataset.level = pending || failed ? 'warning' : 'normal';
}

async function loadSystemStatus() {
  if (!systemStatusEl) return;
  const generation = ++systemStatusLoadGeneration;
  try {
    const resp = await authFetch('/api/mentor/system-status');
    if (!resp.ok) throw new Error('system_status_http_' + resp.status);
    const payload = await resp.json();
    if (generation !== systemStatusLoadGeneration) return;
    state.systemStatus = payload;
    renderSystemStatus();
  } catch (err) {
    if (generation !== systemStatusLoadGeneration) return;
    systemStatusEl.textContent = '系统状态不可用';
    systemStatusEl.dataset.level = 'error';
    console.error('加载系统状态失败', err);
  }
}

function renderStudents() {
  const activeBefore = document.activeElement;
  const focusedStudent = activeBefore instanceof Element
    ? activeBefore.closest('.student-item[data-student-id]')
    : null;
  const focusedStudentId = focusedStudent && studentListEl.contains(focusedStudent)
    ? focusedStudent.dataset.studentId
    : null;
  studentListEl.innerHTML = ''; // 清空骨架（非用户值），安全
  const students = state.students
    .map((student, index) => ({ student: student, index: index }))
    .sort((left, right) => {
      const a = left.student;
      const b = right.student;
      const priorityDelta = attentionPriorityRank(b.highest_attention_priority) -
        attentionPriorityRank(a.highest_attention_priority);
      if (priorityDelta) return priorityDelta;
      const countDelta = (Number(b.open_attention_count) || 0) -
        (Number(a.open_attention_count) || 0);
      if (countDelta) return countDelta;
      const activityDelta = (Number(b.last_attention_at || b.last_ts) || 0) -
        (Number(a.last_attention_at || a.last_ts) || 0);
      if (activityDelta) return activityDelta;
      return left.index - right.index;
    })
    .map((entry) => entry.student);
  students.forEach((s) => {
    const li = el('li', 'student-item');
    li.dataset.studentId = s.student_id;
    const selected = s.student_id === state.currentStudentId;
    if (selected) li.classList.add('selected');
    li.setAttribute('aria-pressed', selected ? 'true' : 'false');

    li.appendChild(el('span', 'status-dot ' + severityClass(s.last_severity)));

    const info = el('div', 'student-info');
    info.appendChild(el('div', 'name', s.display_name || s.student_id || '(未命名学员)'));
    const sessionCount = s.session_count || 0;
    const analysisCount = s.analysis_count || 0;
    info.appendChild(el('div', 'meta', sessionCount + ' 对话 · ' + analysisCount + ' 分析'));
    li.appendChild(info);
    const attentionCount = Number(s.open_attention_count) || 0;
    if (attentionCount > 0) {
      li.appendChild(el('span', 'attention-count', String(attentionCount)));
    }

    makeKeyboardActivatable(li, () => selectStudent(s.student_id));
    studentListEl.appendChild(li);
  });
  if (focusedStudentId) {
    const replacement = Array.from(studentListEl.querySelectorAll('.student-item')).find(
      (item) => item.dataset.studentId === focusedStudentId
    );
    if (replacement && elementIsRendered(replacement)) replacement.focus();
  }
}

// ─────────────────────────────────────────────────────────────
// 选中学员 → 加载对话列表
// ─────────────────────────────────────────────────────────────
async function selectStudent(studentId) {
  const generation = ++studentSelectionGeneration;
  uploadAttemptGeneration += 1;
  cancelUploadTracking();
  state.currentStudentId = studentId;
  state.currentSessionId = null;
  state.sessions = [];
  state.timeline = [];
  resetTranscript();  // 换学员 → 清空上一会话的原文缓存/展开态
  resetReplies();     // 换学员 → 清空 AI 回复展开缓存
  state.uploadRequest = newUploadRequestState();
  clearSyncFeedback(); // 换学员 → 清掉上一个学员的同步反馈
  renderStudents();   // 刷新选中态（读 state）
  renderSessions();
  renderTimeline();
  updateComposeEnabled();
  updateSyncEnabled(); // 选中学员后同步按钮可用
  try {
    const resp = await authFetch('/api/mentor/students/' + encodeURIComponent(studentId) + '/sessions');
    if (!resp.ok) throw new Error('sessions_http_' + resp.status);
    const data = await resp.json();
    if (generation !== studentSelectionGeneration || state.currentStudentId !== studentId) {
      return false;
    }
    state.sessions = data.items || [];
    renderSessions();
    return true;
  } catch (err) {
    if (generation === studentSelectionGeneration) {
      console.error('加载对话列表失败', err);
    }
    return false;
  }
}

function sessionTitleOf(s) {
  // 后端当前返回 title；契约字段名 session_title —— 两者都兼容
  // 标题为空（含纯空白）时显示"未命名对话"，绝不回退到原始 session_id
  // （原始 id 如 "44cc4b2e-1d3e…" / "hook-test-3" 对导师无意义、观感差）
  const raw = (s.session_title || s.title || '').trim();
  return raw || '未命名对话';
}

// 会话最后活动时间（倒序排列用）；主字段 last_activity_at，容错回退到其它时间字段
function sessionActivityTs(s) {
  const v = s.last_activity_at != null ? s.last_activity_at
    : s.last_activity != null ? s.last_activity
    : s.updated_at != null ? s.updated_at
    : s.created_at != null ? s.created_at
    : 0;
  return Number(v) || 0;
}

// ── 会话分组：空间（按 space_name 二级分组） / 任务（含 group_type="" 的其它会话） ──
//   参照 WorkBuddy 侧边栏：空间区在上、任务区在下（大组顺序固定，在 renderSessions 保证）。
//   group_type="space" → 归空间，再按 space_name 聚合；其余（"task" / "" / 缺失）→ 归任务。
//   排序（纯前端，不依赖后端顺序，和 WorkBuddy 一致：最新在上）：
//     - 任务组内：按 last_activity_at 倒序
//     - 空间子组内：按 last_activity_at 倒序
//     - 空间各子组之间：按"各子组最新会话时间"倒序
function groupSessions(sessions) {
  const spaceMap = new Map(); // space_name -> [session]（先聚合，后排序）
  const tasks = [];
  let spaceCount = 0;
  sessions.forEach((s) => {
    if ((s.group_type || '') === 'space') {
      const name = (s.space_name || '').trim() || '未命名空间';
      if (!spaceMap.has(name)) spaceMap.set(name, []);
      spaceMap.get(name).push(s);
      spaceCount += 1;
    } else {
      tasks.push(s); // "task" 或 group_type="" 一律归任务
    }
  });

  const byActivityDesc = (a, b) => sessionActivityTs(b) - sessionActivityTs(a);

  // 子组内排序 + 子组间按各自最新会话时间倒序
  const spaceSubgroups = Array.from(spaceMap.entries()).map(([name, items]) => {
    const sorted = items.slice().sort(byActivityDesc);
    const latest = sorted.length ? sessionActivityTs(sorted[0]) : 0;
    return { name, items: sorted, latest };
  });
  spaceSubgroups.sort((a, b) => b.latest - a.latest);

  const sortedTasks = tasks.slice().sort(byActivityDesc);

  return { spaceSubgroups, spaceCount, tasks: sortedTasks };
}

// 单个会话项：状态点 + 标题 + meta。
//   灰显判据："既无内容也无分析"才加 .unanalyzed 灰显（有内容就立刻点亮，不等 LLM 诊断）：
//   - 有分析(analysis_count>0)：三色 last_severity 点 + meta "X 分析 · Y 告警"。
//   - 有内容无分析(message_count>0, analysis_count=0)：不灰显 + 中性灰点 + meta "N 条对话 · 待诊断"。
//     （内容已落库、LLM 诊断后台异步补，故正常显示而非灰显。）
//   - 全空(analysis_count=0 且 message_count=0)：灰显 + 中性灰点 + meta "未分析"。
//   三种形态均可点击。
function buildSessionItem(s) {
  const item = el('div', 'session-item');
  item.dataset.sessionId = s.session_id;
  const analysisCount = s.analysis_count || 0;
  const messageCount = s.message_count || 0;
  const analyzed = analysisCount > 0;
  const hasContent = messageCount > 0;
  // 既无内容也无分析才灰显（判据从"analysis_count==0"升级为"内容与分析皆无"）
  if (!analyzed && !hasContent) item.classList.add('unanalyzed');
  const selected = s.session_id === state.currentSessionId;
  if (selected) item.classList.add('selected');
  item.setAttribute('aria-pressed', selected ? 'true' : 'false');

  // 状态点：有分析用 last_severity 三色；否则（有内容待诊断 / 全空）用中性灰点
  const dotCls = analyzed ? severityClass(s.last_severity) : 'status-none';
  item.appendChild(el('span', 'status-dot ' + dotCls));

  const info = el('div', 'session-info');
  info.appendChild(el('div', 'name', sessionTitleOf(s)));
  const alertCount = s.alert_count || 0;
  let meta;
  if (analyzed) {
    meta = analysisCount + ' 分析' + (alertCount ? ' · ' + alertCount + ' 告警' : '');
  } else if (hasContent) {
    meta = messageCount + ' 条对话 · 待诊断';
  } else {
    meta = '未分析';
  }
  info.appendChild(el('div', 'meta', meta));
  item.appendChild(info);

  makeKeyboardActivatable(item, () => selectSession(s.session_id));
  return item;
}

// 可折叠分组标题：点标题只改 state.groupCollapsed 再整体重渲染（不从 DOM 反读）
function buildGroupHeader(groupKey, title, count) {
  const collapsed = !!state.groupCollapsed[groupKey];
  const header = el('div', 'group-header');
  header.dataset.group = groupKey;
  header.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
  header.appendChild(el('span', 'group-caret', collapsed ? '▸' : '▾'));
  header.appendChild(el('span', 'group-title', title));
  header.appendChild(el('span', 'group-count', String(count)));
  makeKeyboardActivatable(header, () => {
    state.groupCollapsed[groupKey] = !state.groupCollapsed[groupKey];
    renderSessions();
  });
  return header;
}

function renderSessions() {
  const activeBefore = document.activeElement;
  const focusedSession = activeBefore instanceof Element
    ? activeBefore.closest('.session-item[data-session-id]')
    : null;
  const focusedGroup = activeBefore instanceof Element
    ? activeBefore.closest('.group-header[data-group]')
    : null;
  const focusedSessionId = focusedSession && sessionListEl.contains(focusedSession)
    ? focusedSession.dataset.sessionId
    : null;
  const focusedGroupKey = focusedGroup && sessionListEl.contains(focusedGroup)
    ? focusedGroup.dataset.group
    : null;
  sessionListEl.innerHTML = '';
  const { spaceSubgroups, spaceCount, tasks } = groupSessions(state.sessions);

  // 空间分组（有空间会话才渲染），内部再按 space_name 二级分组（子组已按最新时间倒序）
  if (spaceCount > 0) {
    const group = el('li', 'session-group');
    group.dataset.group = 'space';
    if (state.groupCollapsed.space) group.classList.add('collapsed');
    group.appendChild(buildGroupHeader('space', '空间', spaceCount));
    const body = el('div', 'group-body');
    spaceSubgroups.forEach(({ name, items }) => {
      const sub = el('div', 'space-subgroup');
      sub.appendChild(el('div', 'subgroup-title', name));
      items.forEach((s) => sub.appendChild(buildSessionItem(s)));
      body.appendChild(sub);
    });
    group.appendChild(body);
    sessionListEl.appendChild(group);
  }

  // 任务分组
  if (tasks.length > 0) {
    const group = el('li', 'session-group');
    group.dataset.group = 'task';
    if (state.groupCollapsed.task) group.classList.add('collapsed');
    group.appendChild(buildGroupHeader('task', '任务', tasks.length));
    const body = el('div', 'group-body');
    tasks.forEach((s) => body.appendChild(buildSessionItem(s)));
    group.appendChild(body);
    sessionListEl.appendChild(group);
  }

  let replacement = null;
  if (focusedSessionId) {
    replacement = Array.from(sessionListEl.querySelectorAll('.session-item')).find(
      (item) => item.dataset.sessionId === focusedSessionId
    );
  } else if (focusedGroupKey) {
    replacement = Array.from(sessionListEl.querySelectorAll('.group-header')).find(
      (item) => item.dataset.group === focusedGroupKey
    );
  }
  if (replacement && elementIsRendered(replacement)) replacement.focus();
}

// ─────────────────────────────────────────────────────────────
// 选中对话 → 加载时间线（从 state.sessions 取，不从 DOM 反读）
// ─────────────────────────────────────────────────────────────
async function selectSession(sessionId) {
  state.currentSessionId = sessionId;
  setMobileView('conversation', { focusTab: true });
  resetTranscript(); // 换会话 → 原文入口回到未加载/收起态（原文是会话级）
  resetReplies();    // 换会话 → AI 回复展开缓存回到未加载/收起态
  renderSessions(); // 只更新选中态，state.sessions 不变（修 B3 伪状态 bug）
  await fetchTimeline(sessionId, { replace: true });
}

async function fetchTimeline(sessionId, { replace } = {}) {
  try {
    const resp = await authFetch('/api/mentor/sessions/' + encodeURIComponent(sessionId) + '/timeline');
    const data = await resp.json();
    const items = (data.items || []).map(normalizeRestEntry);
    if (state.currentSessionId !== sessionId) return; // 期间已切走
    if (replace) {
      // timeline 接口不返回导师出站消息；按学员恢复本页面发送记录。
      const pendingOutbound = state.outboundMessages.filter(
        (entry) => entry.student_id === state.currentStudentId
      );
      state.timeline = items.concat(pendingOutbound);
    } else {
      state.timeline = items;
    }
    renderTimeline();
  } catch (err) {
    console.error('加载时间线失败', err);
  }
}

// ─────────────────────────────────────────────────────────────
// 时间线归一化：REST / WS → 统一内部结构
//   { key, type, content, created_at, severity, understanding,
//     is_technical, topic, suggestion, full_reply, prompt_id, reply_ref, has_full_reply,
//     mentor_name, message_id, server_id, delivered, _optimistic }
// ─────────────────────────────────────────────────────────────
function normalizeRestEntry(r) {
  return {
    key: 'rest-' + (r.type || '') + '-' + (r.created_at || 0) + '-' + Math.random().toString(36).slice(2, 7),
    type: r.type || 'unknown',
    content: r.content || '',
    created_at: r.created_at || 0,
    severity: r.severity || '',
    understanding: r.understanding || '',   // 后端 timeline 暂未透出，兼容将来字段
    is_technical: !!r.is_technical,
    topic: r.topic || '',
    suggestion: r.suggestion || '',
    full_reply: r.full_reply || r.full || '', // 优先完整回复字段
    // ai_summary 每条对应一次学员提问：reply_ref 用于热加载完整回复，has_full_reply 标识是否有原文
    prompt_id: r.prompt_id != null ? r.prompt_id : null,
    reply_ref: r.reply_ref || (r.prompt_id != null ? ('prompt:' + r.prompt_id) : null),
    has_full_reply: !!r.has_full_reply,
    delivered: true,
  };
}

function wsPayloadToTimeline(payload) {
  const ts = payload.timestamp || Date.now() / 1000;
  if (payload.type === 'prompt') {
    return {
      key: 'ws-prompt-' + (payload.prompt_id || ts),
      type: 'prompt',
      content: payload.prompt || '',
      created_at: ts,
    };
  }
  if (payload.type === 'ai_summary') {
    return {
      key: 'ws-ai-' + (payload.prompt_id || ts),
      type: 'ai_summary',
      content: payload.summary || payload.content || '',
      full_reply: payload.full_reply || payload.full || '',
      prompt_id: payload.prompt_id != null ? payload.prompt_id : null,
      reply_ref: payload.reply_ref || (payload.prompt_id != null ? ('prompt:' + payload.prompt_id) : null),
      has_full_reply: !!payload.has_full_reply,
      created_at: ts,
    };
  }
  if (payload.type === 'analysis') {
    const r = payload.result || {};
    return {
      key: 'ws-an-' + (payload.report_id || ts),
      type: 'analysis',
      content: r.diagnosis || '',
      suggestion: r.suggestion || '',
      severity: r.severity || '',
      understanding: r.understanding || '', // WS 分析结果带 understanding
      is_technical: !!r.is_technical,
      topic: r.topic || '',
      created_at: ts,
    };
  }
  if (payload.type === 'mentor_message') {
    // 导师台一般不收此事件（出站是自己发的），兼容处理
    return {
      key: 'ws-me-' + (payload.message_id || payload.id || ts),
      type: 'mentor_message',
      content: payload.text || '',
      mentor_name: payload.mentor_name || '导师',
      message_id: payload.message_id || null,
      server_id: payload.id != null ? payload.id : null,
      created_at: ts,
      delivered: !!payload.delivered,
    };
  }
  return null;
}

// ─────────────────────────────────────────────────────────────
// 理解程度徽章：优先 understanding，缺失时从 severity 兜底
//   understanding: high|medium|low|stuck（后端 llm 值域）
//   severity: error|warn|info
// ─────────────────────────────────────────────────────────────
function understandingBadge(entry) {
  let u = (entry.understanding || '').toLowerCase();
  if (!u || u === 'unknown') {
    if (entry.severity === 'error') u = 'stuck';
    else if (entry.severity === 'warn') u = 'low';
    else u = 'mid';
  }
  if (u === 'medium') u = 'mid';
  const map = {
    stuck: { text: '卡点', cls: 'und-stuck' },
    low: { text: '薄弱', cls: 'und-low' },
    mid: { text: '一般', cls: 'und-mid' },
    high: { text: '良好', cls: 'und-high' },
  };
  return map[u] || map.mid;
}

// ─────────────────────────────────────────────────────────────
// 渲染时间线（全量重建，只读 state）
// ─────────────────────────────────────────────────────────────
function renderTimeline() {
  timelineEl.innerHTML = '';
  if (!state.timeline.length) {
    const hint = state.currentSessionId ? '此对话暂无分析记录' : '请选择一个对话';
    timelineEl.appendChild(el('div', 'timeline-empty', hint));
    renderTranscriptEntry(); // 空时间线 → 隐藏原文入口
    return;
  }
  state.timeline.forEach((entry) => timelineEl.appendChild(buildTimelineRow(entry)));
  timelineEl.scrollTop = timelineEl.scrollHeight;
  renderTranscriptEntry(); // 非空 → 显示单一原文入口
}

// 按 reply_ref 热加载该次提问的完整 AI 回复原文；结果缓存到 state.replies[replyRef]。
//   已加载(content!=null)或加载中直接返回，不重复请求 —— 隐藏后再展开即命中缓存。
//   加载中/失败态由 renderTimeline 依 state 回显（"加载中…"/"加载失败"）。
async function loadReply(replyRef) {
  const rs = replyState(replyRef);
  if (rs.content != null || rs.loading) return; // 命中缓存 / 正在请求 → 不再发请求
  rs.failed = false;
  rs.loading = true;
  renderTimeline(); // 回显"加载中…"
  try {
    const resp = await authFetch('/api/mentor/replies/' + encodeURIComponent(replyRef) + '/text');
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    const data = await resp.json();
    rs.content = data.reply || data.text || '';
  } catch (err) {
    console.error('加载 AI 完整回复失败', err);
    rs.failed = true;
  } finally {
    rs.loading = false;
  }
  renderTimeline();
}

function buildTimelineRow(entry) {
  const row = el('div', 'tl-row');
  const card = el('div', 'tl-card');
  const top = el('div', 'tl-top');

  if (entry.type === 'prompt') {
    row.appendChild(badge('badge-q', '问'));
    card.classList.add('card-q');
    top.appendChild(el('span', null, '学员提问 · ' + formatTime(entry.created_at)));
    card.appendChild(top);
    card.appendChild(el('div', 'tl-txt', entry.content));

  } else if (entry.type === 'ai_summary') {
    row.appendChild(badge('badge-ai', 'AI'));
    card.classList.add('card-ai');
    top.appendChild(el('span', null, 'AI 回复摘要 · ' + formatTime(entry.created_at)));
    card.appendChild(top);
    // 每条 ai_summary = 该次学员提问的 AI 回复摘要（50-300 字，平均约 100 字）——本就是摘要，正常显示不截断。
    // content 为空 → 显示"（未生成摘要）"占位。
    const summary = (entry.content || '').trim();
    card.appendChild(el('div', 'tl-txt ai-summary', summary || '（未生成摘要）'));

    // "显示详情"热加载：仅当有 reply_ref 且后端标识有原文时提供。
    const replyRef = entry.reply_ref || (entry.prompt_id != null ? ('prompt:' + entry.prompt_id) : null);
    if (replyRef != null && entry.has_full_reply) {
      const rs = replyState(replyRef);
      // "显示详情/隐藏" 按钮放在摘要正文下方
      const toggle = el('button', 'ai-detail-toggle', rs.open ? '隐藏 ▴' : '显示详情 ▾');
      toggle.type = 'button';
      toggle.dataset.replyRef = String(replyRef);
      toggle.addEventListener('click', () => {
        const s = replyState(replyRef);
        s.open = !s.open;
        if (s.open) loadReply(replyRef); // 首次展开热加载；已缓存则 loadReply 内部直接返回不再请求
        renderTimeline();
      });
      card.appendChild(toggle);

      // 展开态：在摘要卡下方动态渲染同款面板显示完整回复原文（textContent 防 XSS）
      if (rs.open) {
        const panel = el('div', 'ai-detail');
        if (rs.content != null) {
          panel.textContent = rs.content || '（无完整回复）';
        } else if (rs.failed) {
          panel.classList.add('pending');
          panel.textContent = '加载失败';
        } else {
          panel.classList.add('pending');
          panel.textContent = '加载中…';
        }
        card.appendChild(panel);
      }
    }

  } else if (entry.type === 'analysis') {
    row.classList.add('row-an'); // 诊断整体右缩进，视觉上区别于问/AI
    row.appendChild(badge('badge-an', '诊'));
    card.classList.add('card-an');
    top.appendChild(el('span', null, '学习诊断 · ' + formatTime(entry.created_at)));
    const ub = understandingBadge(entry);
    top.appendChild(el('span', 'und ' + ub.cls, ub.text));
    if (entry.is_technical) top.appendChild(el('span', 'techtag', '技术'));
    card.appendChild(top);
    card.appendChild(el('div', 'tl-txt', entry.content || '(无诊断)'));
    if (entry.suggestion) {
      card.appendChild(el('div', 'tl-sug', '💡 建议：' + entry.suggestion));
    }

  } else if (entry.type === 'mentor_message') {
    row.classList.add('out');
    row.appendChild(badge('badge-me', '师'));
    card.classList.add('card-me');
    const who = '导师提示 → ' + currentStudentName()
      + ' · ' + (entry.mentor_name || '导师') + ' ' + formatTime(entry.created_at);
    top.appendChild(el('span', null, who));
    top.appendChild(deliveredPill(entry));
    card.appendChild(top);
    card.appendChild(el('div', 'tl-txt', entry.content));

  } else {
    card.appendChild(top);
    card.appendChild(el('div', 'tl-txt', entry.content || ''));
  }

  row.appendChild(card);
  return row;
}

function badge(cls, text) {
  return el('div', 'tl-badge ' + cls, text);
}

function deliveredPill(entry) {
  let pill;
  if (entry.delivered) pill = el('span', 'pill', '✓ 已展示');
  else if (entry._failed) pill = el('span', 'pill failed', '发送失败');
  else pill = el('span', 'pill sending', '发送中…');
  pill.setAttribute('role', 'status');
  pill.setAttribute('aria-live', 'polite');
  return pill;
}

// ─────────────────────────────────────────────────────────────
// 会话级「查看完整对话原文」单一入口（时间线顶部）
//   - 时间线非空才显示；空则隐藏
//   - 首次展开时按需 lazy fetch GET /api/mentor/sessions/{id}/transcript
//   - 原文经 textContent 渲染（学员可控文本 → 防 XSS，绝不 innerHTML）
//   - 404 → 友好提示（历史会话无原文）；其他错误 → 加载失败
// ─────────────────────────────────────────────────────────────
function renderTranscriptEntry() {
  if (!transcriptEntryEl) return;
  const hasTimeline = state.timeline.length > 0;
  if (!hasTimeline) {
    transcriptEntryEl.hidden = true;
    if (transcriptEntryEl.open) transcriptEntryEl.open = false; // 收起，避免残留展开
    return;
  }
  transcriptEntryEl.hidden = false;
  // 同步 details 展开态到 state（会话切换后 state.open=false → DOM 应收起）
  if (transcriptEntryEl.open !== state.transcript.open) {
    transcriptEntryEl.open = state.transcript.open;
  }
  renderTranscriptBody();
}

// 依据 state.transcript 把当前状态回显进展开区（纯 textContent）
function renderTranscriptBody() {
  const t = state.transcript;
  const body = transcriptBodyEl;
  if (!body) return;
  if (t.content != null) {
    body.classList.remove('pending');
    body.textContent = t.content || '(此会话暂无对话原文)';
  } else if (t.missing) {
    // HTTP 404：历史会话本就没有完整原文，友好提示而非"加载失败"
    body.classList.add('pending');
    body.textContent = '（此会话暂无完整原文，可能是历史会话）';
  } else if (t.failed) {
    // 网络异常 / 其他非 2xx：真正的加载失败
    body.classList.add('pending');
    body.textContent = '加载失败';
  } else if (t.loading) {
    body.classList.add('pending');
    body.textContent = '加载中…';
  } else {
    body.classList.add('pending');
    body.textContent = '';
  }
}

// 首次展开时按需拉取当前会话完整 transcript；结果缓存到 state.transcript
async function loadTranscript() {
  const t = state.transcript;
  // 已加载 / 无原文(404) / 已失败 / 加载中：直接回显，不重复请求
  if (t.content != null || t.missing || t.failed || t.loading) {
    renderTranscriptBody();
    return;
  }
  const sessionId = state.currentSessionId;
  if (!sessionId) {
    t.failed = true;
    renderTranscriptBody();
    return;
  }
  t.loading = true;
  renderTranscriptBody(); // 显示「加载中…」
  try {
    const resp = await authFetch('/api/mentor/sessions/' + encodeURIComponent(sessionId) + '/transcript');
    if (state.transcript !== t) return; // 期间已切会话，丢弃本次结果
    if (resp.status === 404) {
      // 历史会话无 raw_transcripts：后端返回 404，是"没有"而非"出错"
      t.missing = true;
    } else if (!resp.ok) {
      throw new Error('HTTP ' + resp.status);
    } else {
      const data = await resp.json();
      if (state.transcript !== t) return;
      t.content = data.content || '';
    }
  } catch (err) {
    console.error('加载完整对话原文失败', err);
    t.failed = true;
  } finally {
    t.loading = false;
  }
  if (state.transcript === t) renderTranscriptBody();
}

// details 展开/收起：展开时懒加载（闭包引用静态 DOM，只绑定一次）
if (transcriptEntryEl) {
  transcriptEntryEl.addEventListener('toggle', () => {
    state.transcript.open = transcriptEntryEl.open;
    if (transcriptEntryEl.open) loadTranscript();
  });
}

// ─────────────────────────────────────────────────────────────
// 同步状态：POST 返回真实 request_id；WS 加速更新，REST 轮询补漏。
// 所有响应都经过 request_id + generation 校验，旧学员的慢响应不会覆盖新状态。
// ─────────────────────────────────────────────────────────────
function normalizeUploadRequest(snapshot, previous) {
  const prior = previous || newUploadRequestState();
  const legacy = snapshot.status || '';
  const transfer = snapshot.transfer_status || (
    legacy === 'done' ? 'stored' : (legacy || prior.transferStatus)
  );
  return {
    requestId: snapshot.request_id != null ? String(snapshot.request_id) : prior.requestId,
    studentId: snapshot.student_id != null ? String(snapshot.student_id) : prior.studentId,
    transferStatus: transfer || null,
    analysisStatus: snapshot.analysis_status || prior.analysisStatus || 'not_requested',
    transferError: snapshot.transfer_error || '',
    analysisError: snapshot.analysis_error || '',
    result: snapshot.result !== undefined ? snapshot.result : prior.result,
    updatedAt: snapshot.updated_at != null ? Number(snapshot.updated_at) || 0 : prior.updatedAt,
  };
}

function reduceUploadRequest(snapshot) {
  if (!snapshot || snapshot.request_id == null) return false;
  const requestId = String(snapshot.request_id);
  if (state.uploadRequest.requestId && requestId !== state.uploadRequest.requestId) return false;
  const incomingUpdatedAt = snapshot.updated_at != null ? Number(snapshot.updated_at) || 0 : 0;
  if (state.uploadRequest.requestId === requestId &&
      incomingUpdatedAt < state.uploadRequest.updatedAt) return false;
  const studentId = snapshot.student_id != null ? String(snapshot.student_id) : state.uploadRequest.studentId;
  if (studentId && studentId !== state.currentStudentId) return false;
  state.uploadRequest = normalizeUploadRequest(snapshot, state.uploadRequest);
  renderUploadRequest();
  return true;
}

function uploadRequestIsTerminal(request) {
  if (!request.requestId) return true;
  if (request.transferStatus === 'failed') return true;
  if (request.transferStatus !== 'stored') return false;
  return ['not_requested', 'done', 'failed'].includes(request.analysisStatus);
}

function renderUploadRequest() {
  const request = state.uploadRequest;
  if (!request.requestId) {
    clearSyncFeedback();
    updateSyncEnabled();
    return;
  }

  let text = '';
  let isError = false;
  let canRetryAnalysis = false;
  if (request.transferStatus === 'pending') {
    text = '已请求同步，等待学员端接收…';
  } else if (request.transferStatus === 'running') {
    text = '正在上传对话…';
  } else if (request.transferStatus === 'failed') {
    text = '同步失败' + (request.transferError ? '：' + request.transferError : '，请重试。');
    isError = true;
  } else if (request.transferStatus === 'stored') {
    if (request.analysisStatus === 'pending') {
      text = '内容已保存，诊断等待中…';
    } else if (request.analysisStatus === 'running') {
      text = '内容已保存，正在诊断…';
    } else if (request.analysisStatus === 'done') {
      text = '内容已保存，诊断完成。';
    } else if (request.analysisStatus === 'failed') {
      text = '内容已保存，诊断失败' + (request.analysisError ? '：' + request.analysisError : '。');
      isError = true;
      canRetryAnalysis = true;
    } else {
      text = '内容已保存，诊断未请求。';
    }
  } else {
    text = '同步状态未知，请重试。';
    isError = true;
  }
  showSyncFeedback(text, isError);
  if (retryAnalysisBtn) {
    retryAnalysisBtn.hidden = !canRetryAnalysis;
    retryAnalysisBtn.disabled = false;
  }
  updateSyncEnabled();
}

function updateSyncEnabled() {
  if (!syncBtn) return;
  const request = state.uploadRequest;
  const transferActive = request.studentId === state.currentStudentId &&
    ['pending', 'running'].includes(request.transferStatus);
  syncBtn.disabled = !state.currentStudentId || transferActive;
}

function showSyncFeedback(text, isError) {
  if (!syncFeedbackEl) return;
  syncFeedbackEl.hidden = false;
  syncFeedbackEl.textContent = text;
  syncFeedbackEl.classList.toggle('error', !!isError);
}

function clearSyncFeedback() {
  if (!syncFeedbackEl) return;
  syncFeedbackEl.hidden = true;
  syncFeedbackEl.textContent = '';
  syncFeedbackEl.classList.remove('error');
  if (retryAnalysisBtn) retryAnalysisBtn.hidden = true;
}

function cancelUploadTracking() {
  uploadTrackingGeneration += 1;
  if (uploadPollTimer) {
    clearTimeout(uploadPollTimer);
    uploadPollTimer = null;
  }
  if (uploadPollController) {
    uploadPollController.abort();
    uploadPollController = null;
  }
}

function scheduleUploadPoll(generation, delay) {
  if (generation !== uploadTrackingGeneration || uploadRequestIsTerminal(state.uploadRequest)) return;
  uploadPollTimer = setTimeout(() => pollUploadRequest(generation), delay);
}

async function pollUploadRequest(generation) {
  const requestId = state.uploadRequest.requestId;
  const controller = uploadPollController;
  if (!requestId || !controller || generation !== uploadTrackingGeneration) return;
  try {
    const resp = await authFetch(
      '/api/mentor/upload-requests/' + encodeURIComponent(requestId),
      { signal: controller.signal }
    );
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    const snapshot = await resp.json();
    if (controller.signal.aborted || generation !== uploadTrackingGeneration ||
        state.uploadRequest.requestId !== requestId) return;
    reduceUploadRequest(snapshot);
  } catch (err) {
    if (controller.signal.aborted || err.name === 'AbortError') return;
    console.error('读取同步状态失败', err);
  }
  scheduleUploadPoll(generation, 750);
}

function beginUploadTracking(snapshot) {
  cancelUploadTracking();
  state.uploadRequest = normalizeUploadRequest(snapshot, newUploadRequestState());
  renderUploadRequest();
  if (uploadRequestIsTerminal(state.uploadRequest)) return;
  uploadPollController = new AbortController();
  const generation = uploadTrackingGeneration;
  scheduleUploadPoll(generation, 500);
}

function acceptUploadStatusEvent(snapshot) {
  if (!snapshot || snapshot.type !== 'upload_request_status') return false;
  if (!state.uploadRequest.requestId || String(snapshot.request_id) !== state.uploadRequest.requestId) return false;
  if (snapshot.student_id != null && String(snapshot.student_id) !== state.currentStudentId) return false;
  if (!reduceUploadRequest(snapshot)) return false;
  // 取消可能携带更旧数据库快照的在途轮询，再以 WS 快照为起点补拉。
  cancelUploadTracking();
  if (!uploadRequestIsTerminal(state.uploadRequest)) {
    uploadPollController = new AbortController();
    const generation = uploadTrackingGeneration;
    scheduleUploadPoll(generation, 500);
  }
  return true;
}

async function requestStudentUpload() {
  const studentId = state.currentStudentId;
  if (!studentId) return;
  const attemptGeneration = ++uploadAttemptGeneration;
  cancelUploadTracking();
  state.uploadRequest = newUploadRequestState();
  syncBtn.disabled = true;
  showSyncFeedback('正在创建同步请求…', false);
  try {
    const resp = await authFetch(
      '/api/mentor/students/' + encodeURIComponent(studentId) + '/request-upload',
      { method: 'POST' }
    );
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    const snapshot = await resp.json();
    if (state.currentStudentId !== studentId ||
        attemptGeneration !== uploadAttemptGeneration) return;
    beginUploadTracking(snapshot);
  } catch (err) {
    if (state.currentStudentId !== studentId ||
        attemptGeneration !== uploadAttemptGeneration) return;
    console.error('请求同步全部对话失败', err);
    showSyncFeedback('同步请求失败，请重试。', true);
  } finally {
    updateSyncEnabled();
  }
}

async function retryUploadAnalysis() {
  const requestId = state.uploadRequest.requestId;
  const studentId = state.currentStudentId;
  if (!requestId || state.uploadRequest.analysisStatus !== 'failed') return;
  retryAnalysisBtn.disabled = true;
  try {
    const resp = await authFetch(
      '/api/mentor/upload-requests/' + encodeURIComponent(requestId) + '/retry-analysis',
      { method: 'POST' }
    );
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    const snapshot = await resp.json();
    if (state.currentStudentId !== studentId || state.uploadRequest.requestId !== requestId) return;
    beginUploadTracking(snapshot);
  } catch (err) {
    if (state.currentStudentId !== studentId || state.uploadRequest.requestId !== requestId) return;
    console.error('仅重试诊断失败', err);
    showSyncFeedback('诊断重试请求失败，请稍后再试。', true);
    retryAnalysisBtn.disabled = false;
  }
}

if (syncBtn) syncBtn.addEventListener('click', requestStudentUpload);
if (retryAnalysisBtn) retryAnalysisBtn.addEventListener('click', retryUploadAnalysis);

// ─────────────────────────────────────────────────────────────
// 导师发消息 + 学员端已展示
// ─────────────────────────────────────────────────────────────
function updateComposeEnabled() {
  const enabled = !!state.currentStudentId;
  composeInput.disabled = !enabled;
  composeSend.disabled = !enabled;
  composeInput.placeholder = enabled
    ? ('给 ' + currentStudentName() + ' 发一条提示…（不改 AI，仅提示学员）')
    : '选中学员后可发送提示…（不改 AI，仅提示学员）';
}

function retainOutboundMessage(entry) {
  state.outboundMessages.push(entry);
  if (state.outboundMessages.length <= MAX_OUTBOUND_MESSAGES) return;
  let removable = state.outboundMessages.findIndex(
    (message) => message.delivered || message._failed
  );
  if (removable < 0) removable = 0;
  const removed = state.outboundMessages.splice(removable, 1)[0];
  state.timeline = state.timeline.filter((message) => message !== removed);
}

function applyRecoveredMessageStatus(item) {
  if (!item || !item.client_request_id) return false;
  const entry = state.outboundMessages.find(
    (message) => message.client_request_id === item.client_request_id
  );
  if (!entry || (item.student_id && item.student_id !== entry.student_id)) return false;

  let changed = false;
  if (item.message_id && entry.message_id !== item.message_id) {
    entry.message_id = item.message_id;
    changed = true;
  }
  if (item.id != null && entry.server_id !== item.id) {
    entry.server_id = item.id;
    changed = true;
  }
  // 状态查询命中即证明 POST 已持久化；不再把“响应丢失”误报为发送失败。
  if (entry._failed) {
    entry._failed = false;
    changed = true;
  }
  // 送达状态只单调前进，避免较旧查询覆盖较新回执。
  if (item.delivered && !entry.delivered) {
    entry.delivered = true;
    changed = true;
  }
  state.pendingDeliveryReceipts = state.pendingDeliveryReceipts.filter((receipt) => {
    if (!deliveryMatchesEntry(entry, receipt)) return true;
    if (!entry.delivered) {
      entry.delivered = true;
      changed = true;
    }
    return false;
  });
  return changed;
}

async function reconcileOutboundMessageStatuses(requestedClientRequestIds) {
  const retained = new Map(
    state.outboundMessages
      .filter((message) => message.client_request_id && !message.delivered)
      .map((message) => [message.client_request_id, message])
  );
  const sourceIds = Array.isArray(requestedClientRequestIds)
    ? requestedClientRequestIds
    : Array.from(retained.keys()).slice(-MAX_MESSAGE_STATUS_BATCH);
  const clientRequestIds = Array.from(new Set(sourceIds))
    .filter((clientRequestId) => retained.has(clientRequestId))
    .slice(0, MAX_MESSAGE_STATUS_BATCH);
  const found = new Set();
  if (!clientRequestIds.length) return found;

  try {
    const resp = await authFetch('/api/mentor/messages/status', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ client_request_ids: clientRequestIds }),
    });
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    const data = await resp.json();
    let changed = false;
    (data.items || []).forEach((item) => {
      const entry = retained.get(item && item.client_request_id);
      if (!entry || (item.student_id && item.student_id !== entry.student_id)) return;
      found.add(item.client_request_id);
      if (applyRecoveredMessageStatus(item)) changed = true;
    });
    if (changed) renderTimeline();
  } catch (err) {
    console.error('恢复导师消息状态失败', err);
  }
  return found;
}

function waitForMessageRecovery(delayMs) {
  return new Promise((resolve) => setTimeout(resolve, delayMs));
}

async function recoverFailedOutboundMessage(entry) {
  const startedAt = Date.now();
  // SQLite 默认写锁等待可达 5s；最后一次放在 6s，
  // 仍是有界查询，且始终使用原 client_request_id，不重发消息。
  const retryOffsets = [0, 250, 1000, 3000, 6000];
  for (const retryOffset of retryOffsets) {
    const remainingDelay = startedAt + retryOffset - Date.now();
    if (remainingDelay > 0) await waitForMessageRecovery(remainingDelay);
    if (!state.outboundMessages.includes(entry) || entry.delivered) return;
    const found = await reconcileOutboundMessageStatuses([entry.client_request_id]);
    if (found.has(entry.client_request_id)) return;
  }
}

async function sendMentorMessage(text) {
  const studentId = state.currentStudentId;
  if (!studentId || !text.trim()) return;
  const localId = 'out-' + (++outboundSeq);
  const clientRequestId = newClientRequestId();

  // 乐观插入出站条（state 驱动），初始「发送中」
  const entry = {
    key: localId,
    localId: localId,
    type: 'mentor_message',
    content: text.trim(),
    created_at: Date.now() / 1000,
    mentor_name: '我',
    message_id: null,
    server_id: null,
    delivered: false,
    _optimistic: true,
    student_id: studentId,
    client_request_id: clientRequestId,
  };
  retainOutboundMessage(entry);
  state.timeline.push(entry);
  renderTimeline();

  try {
    const resp = await authFetch('/api/mentor/message', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        student_id: studentId,
        text: entry.content,
        client_request_id: clientRequestId,
      }),
    });
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    const data = await resp.json();
    entry.message_id = data.message_id || null;
    entry.server_id = data.id != null ? data.id : null;
    // 服务端只会在 StudentAgent 的 REST receipt 已持久化后返回 true；
    // 普通 WebSocket 写入成功仍保持“发送中”，等待 message_delivered 事件。
    if (data.delivered) entry.delivered = true;
    state.pendingDeliveryReceipts = state.pendingDeliveryReceipts.filter((receipt) => {
      if (!deliveryMatchesEntry(entry, receipt)) return true;
      entry.delivered = true;
      entry._failed = false;
      return false;
    });
    state.lastSeenMessageId = entry.message_id || state.lastSeenMessageId;
  } catch (err) {
    console.error('发送提示失败', err);
    entry._failed = true;
    // 请求可能已入库、仅响应丢失；用幂等键查状态，绝不自动重发。
    recoverFailedOutboundMessage(entry);
  }
  renderTimeline();
}

// WS message_delivered → 把对应出站条标记学员端已展示
function deliveryMatchesEntry(entry, payload) {
  const byMsgId = payload.message_id && entry.message_id === payload.message_id;
  const byServerId = payload.id != null && entry.server_id === payload.id;
  const byClientRequestId = payload.client_request_id &&
    entry.client_request_id === payload.client_request_id;
  return !!(byMsgId || byServerId || byClientRequestId);
}

function markDelivered(payload) {
  let changed = false;
  let matched = false;
  const candidates = state.outboundMessages.concat(
    state.timeline.filter((entry) => !state.outboundMessages.includes(entry))
  );
  candidates.forEach((e) => {
    if (e.type !== 'mentor_message' || !deliveryMatchesEntry(e, payload)) return;
    matched = true;
    if (!e.message_id && payload.message_id) e.message_id = payload.message_id;
    if (e.server_id == null && payload.id != null) e.server_id = payload.id;
    if (!e.delivered) {
      e.delivered = true;
      e._failed = false;
      changed = true;
    }
  });
  if (!matched) {
    const duplicate = state.pendingDeliveryReceipts.some((receipt) =>
      (payload.message_id && receipt.message_id === payload.message_id) ||
      (payload.id != null && receipt.id === payload.id) ||
      (payload.client_request_id &&
        receipt.client_request_id === payload.client_request_id)
    );
    if (!duplicate) {
      state.pendingDeliveryReceipts.push(payload);
      if (state.pendingDeliveryReceipts.length > 300) state.pendingDeliveryReceipts.shift();
    }
  }
  if (changed) renderTimeline();
}

composeForm.addEventListener('submit', (evt) => {
  evt.preventDefault();
  const text = composeInput.value;
  if (!text.trim() || !state.currentStudentId) return;
  composeInput.value = '';
  sendMentorMessage(text);
});

// ─────────────────────────────────────────────────────────────
// WebSocket
// ─────────────────────────────────────────────────────────────
function connectMentorWS() {
  const url = mentorWsUrl();
  let ws;
  try {
    ws = new WebSocket(url);
  } catch (err) {
    console.error('WebSocket 创建失败', err);
    wsStatusEl.textContent = '连接失败';
    return;
  }

  ws.onopen = () => {
    wsStatusEl.textContent = '已连接';
    wsStatusEl.classList.add('connected');
    // 断线重连后重新拉取当前会话时间线，补齐断连期间缺口
    if (state.currentSessionId) {
      fetchTimeline(state.currentSessionId, { replace: true });
    }
    // 首次建连也必须补拉：关闭初始 REST 快照与 WS open 之间的事件空窗。
    refreshAttentionData();
    // 首连/重连都用 client_request_id 补查，恢复断线期间错过的送达回执。
    reconcileOutboundMessageStatuses();
  };

  ws.onmessage = (evt) => {
    let payload;
    try {
      payload = JSON.parse(evt.data);
    } catch (e) {
      return;
    }

    if (payload.type === 'attention_updated') {
      acceptAttentionEvent(payload);
      return;
    }

    // 已展示回执：不限会话，按 client/message/server id 匹配出站条
    if (payload.type === 'message_delivered') {
      markDelivered(payload);
      return;
    }

    if (payload.type === 'upload_request_status') {
      acceptUploadStatusEvent(payload);
      return;
    }

    // 正向事件：仅当前会话才插入
    if (state.currentSessionId && payload.session_id === state.currentSessionId) {
      const entry = wsPayloadToTimeline(payload);
      if (entry) {
        state.timeline.push(entry);
        renderTimeline();
      }
    }
  };

  ws.onclose = () => {
    wsStatusEl.textContent = '已断开';
    wsStatusEl.classList.remove('connected');
    setTimeout(connectMentorWS, 3000);
  };

  ws.onerror = () => {
    wsStatusEl.textContent = '连接错误';
  };
}

// ─────────────────────────────────────────────────────────────
// 初始化
// ─────────────────────────────────────────────────────────────
window.addEventListener('DOMContentLoaded', () => {
  syncResponsiveMode();
  renderAttention();     // 初始关注队列骨架
  renderTimeline();      // 初始空态提示
  updateComposeEnabled();
  updateSyncEnabled();   // 初始未选中学员 → 同步按钮禁用
  // 先完成一次导师鉴权，再拉关注队列，避免 public 模式并发 401 弹两次 token 输入。
  loadStudents().then(() => Promise.all([loadAttention(), loadSystemStatus()]));
  connectMentorWS();
});
