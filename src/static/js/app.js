const SESSION_MODE_AUTH = 'auth';
const SESSION_MODE_GUEST = 'guest';
const SESSION_MODE_AUTO = 'auto';
const SESSION_MODE_VALUES = new Set([
    SESSION_MODE_AUTH,
    SESSION_MODE_GUEST,
    SESSION_MODE_AUTO,
]);

const STORAGE_KEYS = {
    conversations: 'conversations',
    guestConversationStore: 'guestConversationStore',
    guestConversationBuckets: 'guestConversationBuckets',
    guestConversationMeta: 'guestConversationMeta',
    currentConversationId: 'currentConversationId',
    currentConversationTitle: 'currentConversationTitle',
    currentSectionId: 'currentSectionId',
    guestCurrentConversationId: 'guestCurrentConversationId',
    guestCurrentConversationTitle: 'guestCurrentConversationTitle',
    guestCurrentSectionId: 'guestCurrentSectionId',
    guestActiveSnapshotId: 'guestActiveSnapshotId',
    guestActivePoolKey: 'guestActivePoolKey',
    sessionMode: 'sessionMode',
    thinkMode: 'thinkMode',
    useAutoCot: 'useAutoCot',
    useDeepThink: 'useDeepThink',
    useSseStream: 'useSseStream',
};

const state = {
    currentConversationId: null,
    currentSectionId: null,
    currentConversationTitle: '新对话',
    currentConversationMessages: [],
    conversations: [],
    authConversations: [],
    guestConversationStore: [],
    guestConversationBuckets: {},
    guestConversationMeta: {},
    guestSnapshots: [],
    activeGuestSnapshotId: null,
    activeGuestPoolKey: null,
    isLoading: false,
    sessionMode: SESSION_MODE_GUEST,
    useSseStream: false,
};

const messageInput = document.getElementById('message-input');
const sendButton = document.getElementById('send-btn');
const chatContainer = document.getElementById('chat-container');
const newChatButton = document.getElementById('new-chat-btn');
const deleteChatButton = document.getElementById('delete-chat-btn');
const conversationList = document.getElementById('conversation-list');
const currentConversationTitle = document.getElementById('current-conversation-title');
const conversationUserCount = document.getElementById('conversation-user-count');
const conversationAssistantCount = document.getElementById('conversation-assistant-count');
const exportContextButton = document.getElementById('export-context-btn');
const useSseStreamCheckbox = document.getElementById('use-sse-stream');
const useAutoCotCheckbox = document.getElementById('use-auto-cot');
const useDeepThinkCheckbox = document.getElementById('use-deep-think');
const thinkModeSelect = document.getElementById('think-mode');
const sessionModeSelect = document.getElementById('session-mode-select');
const sessionModeHint = document.getElementById('session-mode-hint');
const conversationModeBadge = document.getElementById('conversation-mode-badge');
const openRuntimeButton = document.getElementById('open-runtime-btn');
const refreshRuntimeStatusButton = document.getElementById('refresh-runtime-status-btn');
const guestSessionPanel = document.getElementById('guest-session-panel');
const guestSessionSummary = document.getElementById('guest-session-summary');
const guestSessionList = document.getElementById('guest-session-list');
const refreshGuestSnapshotsButton = document.getElementById('refresh-guest-snapshots-btn');
const conversationListSummary = document.getElementById('conversation-list-summary');
const sidebarActions = document.querySelector('.sidebar-actions');
const runtimeBanner = document.getElementById('runtime-banner');
const runtimeBannerTitle = document.getElementById('runtime-banner-title');
const runtimeBannerText = document.getElementById('runtime-banner-text');
const runtimeRetryButton = document.getElementById('runtime-retry-btn');
const runtimeHideButton = document.getElementById('runtime-hide-btn');
let resetGuestButton = document.getElementById('reset-guest-btn');
let lastConversationListStructureKey = '';
let conversationListScrollMode = 'preserve';

function setConversationListScrollMode(mode = 'preserve') {
    conversationListScrollMode = mode === 'new' ? 'new' : 'preserve';
}

function normalizeConversationMessageRecord(message = {}) {
    if (!message || typeof message !== 'object') {
        return null;
    }

    const rawRole = String(message.role || '').trim().toLowerCase();
    let role = 'assistant';
    if (rawRole === 'user') {
        role = 'user';
    } else if (rawRole === 'system' || rawRole === 'error') {
        role = 'system';
    }

    const text = typeof message.text === 'string'
        ? message.text
        : String(message.text ?? message.content ?? '');
    const imagesSource = Array.isArray(message.images)
        ? message.images
        : (Array.isArray(message.img_urls) ? message.img_urls : []);
    const images = imagesSource
        .map((item) => String(item || '').trim())
        .filter(Boolean);
    const className = String(
        message.className || (role === 'system' ? 'error-message' : '')
    ).trim();

    return {
        role,
        text,
        images,
        className,
    };
}

function updateConversationMetrics() {
    const messages = Array.isArray(state.currentConversationMessages)
        ? state.currentConversationMessages
        : [];
    const userCount = messages.filter((item) => item && item.role === 'user').length;
    const assistantCount = messages.filter((item) => item && item.role === 'assistant').length;

    if (conversationUserCount) {
        conversationUserCount.textContent = `用户 ${userCount}`;
    }
    if (conversationAssistantCount) {
        conversationAssistantCount.textContent = `回复 ${assistantCount}`;
    }
}

function setCurrentConversationMessages(messages = []) {
    state.currentConversationMessages = (Array.isArray(messages) ? messages : [])
        .map((item) => normalizeConversationMessageRecord(item))
        .filter(Boolean);
    updateConversationMetrics();
}

function appendCurrentConversationMessage(message) {
    const normalized = normalizeConversationMessageRecord(message);
    if (!normalized) {
        return;
    }
    state.currentConversationMessages = [
        ...(Array.isArray(state.currentConversationMessages) ? state.currentConversationMessages : []),
        normalized,
    ];
    updateConversationMetrics();
}

function buildContextExportPayload() {
    const messages = Array.isArray(state.currentConversationMessages)
        ? state.currentConversationMessages.map((item) => ({
            role: item.role,
            text: item.text,
            images: Array.isArray(item.images) ? [...item.images] : [],
            class_name: item.className || '',
        }))
        : [];
    const userCount = messages.filter((item) => item.role === 'user').length;
    const assistantCount = messages.filter((item) => item.role === 'assistant').length;

    return {
        exported_at: new Date().toISOString(),
        session_mode: state.sessionMode,
        active_guest_snapshot_id: state.sessionMode === SESSION_MODE_GUEST
            ? (state.activeGuestSnapshotId || null)
            : null,
        conversation: {
            id: state.currentConversationId || null,
            section_id: state.currentSectionId || null,
            title: state.currentConversationTitle || '新对话',
            mode: getConversationMode(),
        },
        counts: {
            total_messages: messages.length,
            user_messages: userCount,
            assistant_messages: assistantCount,
        },
        messages,
    };
}

function sanitizeExportFileName(value) {
    const raw = String(value || '').trim() || 'context';
    const sanitized = raw.replace(/[<>:"/\\|?*\u0000-\u001F]/g, '_').replace(/\s+/g, '_');
    return sanitized.slice(0, 64) || 'context';
}

function downloadTextFile(filename, content, mimeType = 'application/json;charset=utf-8') {
    const blob = new Blob([content], { type: mimeType });
    const objectUrl = URL.createObjectURL(blob);
    const anchor = document.createElement('a');
    anchor.href = objectUrl;
    anchor.download = filename;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    URL.revokeObjectURL(objectUrl);
}

function exportCurrentConversationContext() {
    const payload = buildContextExportPayload();
    const conversationPart = sanitizeExportFileName(
        payload.conversation.id || payload.conversation.title || 'context'
    );
    const sessionPart = sanitizeExportFileName(payload.session_mode || 'session');
    const timestamp = new Date().toISOString().replace(/[:.]/g, '-');
    downloadTextFile(
        `doubao-context-${sessionPart}-${conversationPart}-${timestamp}.json`,
        JSON.stringify(payload, null, 2),
    );
}

function normalizeSessionMode(value) {
    const normalized = String(value || '').trim().toLowerCase();
    return SESSION_MODE_VALUES.has(normalized) ? normalized : SESSION_MODE_GUEST;
}

function isTemporaryConversationId(value) {
    const normalized = String(value || '').trim();
    return /^(local|load)_/i.test(normalized);
}

function normalizeStableConversationId(value) {
    const normalized = String(value || '').trim();
    if (!normalized || normalized === '0' || isTemporaryConversationId(normalized)) {
        return '';
    }
    return normalized;
}

function getStableCurrentConversationId() {
    return normalizeStableConversationId(state.currentConversationId);
}

function canBrowseHistory() {
    return true;
}

function getConversationMode() {
    return getStableCurrentConversationId() ? 'continue' : 'new';
}

function getCompletionEndpoint() {
    return getStableCurrentConversationId() ? '/api/chat/completions/continue' : '/api/chat/completions/new';
}

function getCompletionStreamEndpoint() {
    return `${getCompletionEndpoint()}/stream`;
}

function shouldUseSseStream() {
    return Boolean(state.useSseStream);
}

function getSessionModeHint(mode) {
    switch (mode) {
        case SESSION_MODE_AUTH:
            return '登录态优先使用已登录会话，支持历史恢复、续聊、删除和首发自动验证。';
        case SESSION_MODE_GUEST:
            return '游客态会保留当前活跃会话，并在没有可复用缓存时自动拉起验证窗口。';
        case SESSION_MODE_AUTO:
            return '自动模式会按当前可用会话工作；首发缺少 capture 时同样会自动进入可见验证流。';
        default:
            return '';
    }
}

function getModeBadgeLabel(mode) {
    switch (mode) {
        case SESSION_MODE_AUTH:
            return '登录态';
        case SESSION_MODE_AUTO:
            return '自动';
        default:
            return '游客态';
    }
}

function escapeHtml(text) {
    return String(text ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

function saveJson(key, value) {
    localStorage.setItem(key, JSON.stringify(value));
}

function loadJson(key, fallback) {
    try {
        const raw = localStorage.getItem(key);
        return raw ? JSON.parse(raw) : fallback;
    } catch (error) {
        console.warn(`读取 ${key} 失败`, error);
        return fallback;
    }
}

function normalizeConversationMetaEntry(entry) {
    if (!entry || typeof entry !== 'object') {
        return null;
    }
    const title = String(entry.title || '').trim();
    const sectionId = String(entry.sectionId || '').trim();
    if (!title && !sectionId) {
        return null;
    }
    return {
        title,
        sectionId,
    };
}

function normalizeGuestConversationMetaStore(value) {
    const source = value && typeof value === 'object' ? value : {};
    const normalized = {};
    Object.entries(source).forEach(([conversationId, entry]) => {
        const normalizedConversationId = String(conversationId || '').trim();
        if (!normalizedConversationId || normalizedConversationId === '0') {
            return;
        }
        const normalizedEntry = normalizeConversationMetaEntry(entry);
        if (!normalizedEntry) {
            return;
        }
        normalized[normalizedConversationId] = normalizedEntry;
    });
    return normalized;
}

function saveGuestConversationMeta() {
    saveJson(STORAGE_KEYS.guestConversationMeta, state.guestConversationMeta);
}

function buildGuestConversationMetaFromItems(items) {
    const nextMeta = {};
    (Array.isArray(items) ? items : []).forEach((item) => {
        const normalizedId = String(item?.id || '').trim();
        if (!normalizedId || normalizedId === '0') {
            return;
        }
        const normalizedEntry = normalizeConversationMetaEntry(item);
        if (!normalizedEntry) {
            return;
        }
        nextMeta[normalizedId] = normalizedEntry;
    });
    return nextMeta;
}

function mergeGuestConversationMeta(...sources) {
    const nextMeta = {};
    sources.forEach((source) => {
        const normalizedSource = normalizeGuestConversationMetaStore(source);
        Object.entries(normalizedSource).forEach(([conversationId, entry]) => {
            const currentEntry = nextMeta[conversationId] || { title: '', sectionId: '' };
            nextMeta[conversationId] = {
                title: entry.title || currentEntry.title || '',
                sectionId: entry.sectionId || currentEntry.sectionId || '',
            };
        });
    });
    return nextMeta;
}

function getGuestConversationMetaEntry(conversationId) {
    const normalizedConversationId = String(conversationId || '').trim();
    if (!normalizedConversationId) {
        return null;
    }
    return normalizeConversationMetaEntry(state.guestConversationMeta?.[normalizedConversationId]) || null;
}

function upsertGuestConversationMetaEntry(item) {
    const normalizedId = String(item?.id || '').trim();
    if (!normalizedId || normalizedId === '0') {
        return;
    }
    const normalizedEntry = normalizeConversationMetaEntry(item);
    if (!normalizedEntry) {
        return;
    }
    const currentEntry = getGuestConversationMetaEntry(normalizedId) || { title: '', sectionId: '' };
    state.guestConversationMeta[normalizedId] = {
        title: normalizedEntry.title || currentEntry.title || '',
        sectionId: normalizedEntry.sectionId || currentEntry.sectionId || '',
    };
}

function saveSessionMode() {
    localStorage.setItem(STORAGE_KEYS.sessionMode, state.sessionMode);
}

function getCurrentConversationStorageKeys(mode = state.sessionMode) {
    if (mode === SESSION_MODE_GUEST) {
        return {
            conversationId: STORAGE_KEYS.guestCurrentConversationId,
            sectionId: STORAGE_KEYS.guestCurrentSectionId,
            title: STORAGE_KEYS.guestCurrentConversationTitle,
        };
    }
    return {
        conversationId: STORAGE_KEYS.currentConversationId,
        sectionId: STORAGE_KEYS.currentSectionId,
        title: STORAGE_KEYS.currentConversationTitle,
    };
}

function getModeConversationStore(mode = state.sessionMode) {
    if (mode === SESSION_MODE_GUEST) {
        return getActiveGuestConversationStore();
    }
    return state.authConversations;
}

function setModeConversationStore(mode, nextItems) {
    if (mode === SESSION_MODE_GUEST) {
        setActiveGuestConversationStore(nextItems);
    } else {
        state.authConversations = Array.isArray(nextItems) ? nextItems : [];
    }
}

function saveConversations(mode = state.sessionMode) {
    if (mode === SESSION_MODE_GUEST) {
        saveJson(STORAGE_KEYS.guestConversationStore, state.guestConversationStore);
        saveJson(STORAGE_KEYS.guestConversationBuckets, state.guestConversationBuckets);
        saveGuestConversationMeta();
    } else {
        saveJson(STORAGE_KEYS.conversations, state.authConversations);
    }
}

function saveActiveGuestSnapshotId() {
    if (state.activeGuestSnapshotId) {
        localStorage.setItem(STORAGE_KEYS.guestActiveSnapshotId, state.activeGuestSnapshotId);
    } else {
        localStorage.removeItem(STORAGE_KEYS.guestActiveSnapshotId);
    }
    if (state.activeGuestPoolKey) {
        localStorage.setItem(STORAGE_KEYS.guestActivePoolKey, state.activeGuestPoolKey);
    } else {
        localStorage.removeItem(STORAGE_KEYS.guestActivePoolKey);
    }
}

function saveCurrentConversationState(mode = state.sessionMode) {
    const keys = getCurrentConversationStorageKeys(mode);
    const conversationId = getStableCurrentConversationId();
    if (conversationId) {
        localStorage.setItem(keys.conversationId, conversationId);
    } else {
        localStorage.removeItem(keys.conversationId);
    }

    if (conversationId && state.currentSectionId) {
        localStorage.setItem(keys.sectionId, state.currentSectionId);
    } else {
        localStorage.removeItem(keys.sectionId);
    }

    localStorage.setItem(keys.title, state.currentConversationTitle || '新对话');
}

function loadStoredConversationState(mode = state.sessionMode) {
    const keys = getCurrentConversationStorageKeys(mode);
    const conversationId = normalizeStableConversationId(localStorage.getItem(keys.conversationId));
    return {
        conversationId,
        sectionId: conversationId ? localStorage.getItem(keys.sectionId) : null,
        title: localStorage.getItem(keys.title) || '新对话',
    };
}

function saveThinkMode() {
    if (thinkModeSelect) {
        localStorage.setItem(STORAGE_KEYS.thinkMode, thinkModeSelect.value);
    }
}

function saveThinkingToggles() {
    if (useAutoCotCheckbox) {
        localStorage.setItem(STORAGE_KEYS.useAutoCot, String(useAutoCotCheckbox.checked));
    }
    if (useDeepThinkCheckbox) {
        localStorage.setItem(STORAGE_KEYS.useDeepThink, String(useDeepThinkCheckbox.checked));
    }
}

function saveSseStreamPreference() {
    if (useSseStreamCheckbox) {
        state.useSseStream = Boolean(useSseStreamCheckbox.checked);
        localStorage.setItem(STORAGE_KEYS.useSseStream, String(state.useSseStream));
    }
}

function hydrateSettings() {
    state.authConversations = loadJson(STORAGE_KEYS.conversations, []);
    state.guestConversationStore = loadJson(STORAGE_KEYS.guestConversationStore, []);
    state.guestConversationBuckets = loadJson(STORAGE_KEYS.guestConversationBuckets, {});
    state.guestConversationMeta = normalizeGuestConversationMetaStore(
        loadJson(STORAGE_KEYS.guestConversationMeta, {})
    );
    if (
        (!state.guestConversationBuckets || Object.keys(state.guestConversationBuckets).length === 0) &&
        Array.isArray(state.guestConversationStore) &&
        state.guestConversationStore.length > 0
    ) {
        state.guestConversationBuckets = {
            'guest:default': [...state.guestConversationStore],
        };
    }
    state.guestConversationMeta = mergeGuestConversationMeta(
        state.guestConversationMeta,
        buildGuestConversationMetaFromItems(state.guestConversationStore),
        ...Object.values(state.guestConversationBuckets || {}).map((items) => buildGuestConversationMetaFromItems(items))
    );
    state.activeGuestSnapshotId = localStorage.getItem(STORAGE_KEYS.guestActiveSnapshotId);
    state.activeGuestPoolKey = localStorage.getItem(STORAGE_KEYS.guestActivePoolKey);
    state.sessionMode = normalizeSessionMode(localStorage.getItem(STORAGE_KEYS.sessionMode));

    const savedUseAutoCot = localStorage.getItem(STORAGE_KEYS.useAutoCot);
    const savedUseDeepThink = localStorage.getItem(STORAGE_KEYS.useDeepThink);
    const savedThinkMode = localStorage.getItem(STORAGE_KEYS.thinkMode);
    const savedUseSseStream = localStorage.getItem(STORAGE_KEYS.useSseStream);

    if (useAutoCotCheckbox && savedUseAutoCot !== null) {
        useAutoCotCheckbox.checked = savedUseAutoCot === 'true';
    }
    if (useDeepThinkCheckbox && savedUseDeepThink !== null) {
        useDeepThinkCheckbox.checked = savedUseDeepThink === 'true';
    }
    if (thinkModeSelect && savedThinkMode !== null) {
        thinkModeSelect.value = savedThinkMode;
    }
    if (useSseStreamCheckbox) {
        useSseStreamCheckbox.checked = savedUseSseStream === 'true';
        state.useSseStream = Boolean(useSseStreamCheckbox.checked);
    }

    saveGuestConversationMeta();
    syncVisibleConversationList();
}

function getResolvedActiveGuestSnapshotId() {
    const activeSnapshot = getActiveGuestSnapshot();
    return String(activeSnapshot?.snapshot_id || state.activeGuestSnapshotId || '').trim() || 'guest:default';
}

function getGuestSnapshotPoolKey(snapshot) {
    const metadata = snapshot && typeof snapshot.metadata === 'object' ? snapshot.metadata : {};
    return String(snapshot?.pool_key || metadata.pool_key || '').trim();
}

function resolveGuestSnapshotSelection({ snapshotId = '', poolKey = '' } = {}) {
    const snapshots = Array.isArray(state.guestSnapshots) ? state.guestSnapshots : [];
    const normalizedSnapshotId = String(snapshotId || '').trim();
    const normalizedPoolKey = String(poolKey || '').trim();
    if (normalizedSnapshotId) {
        const exactSnapshot = snapshots.find((item) => String(item?.snapshot_id || '').trim() === normalizedSnapshotId);
        if (exactSnapshot) {
            return exactSnapshot;
        }
    }
    if (normalizedPoolKey) {
        const poolSnapshot = snapshots.find((item) => getGuestSnapshotPoolKey(item) === normalizedPoolKey);
        if (poolSnapshot) {
            return poolSnapshot;
        }
    }
    return null;
}

function isConversationInGuestSnapshot(snapshot, conversationId) {
    const normalizedConversationId = normalizeStableConversationId(conversationId);
    if (!normalizedConversationId || !snapshot || typeof snapshot !== 'object') {
        return false;
    }
    const roomId = normalizeStableConversationId(snapshot.room_id);
    if (roomId && roomId === normalizedConversationId) {
        return true;
    }
    const snapshotConversationIds = dedupeConversationIds(snapshot.conversation_ids || []);
    return snapshotConversationIds.includes(normalizedConversationId);
}

function hasGuestSnapshotSessionSeed(snapshot) {
    const session = snapshot && typeof snapshot.session === 'object' ? snapshot.session : {};
    return Boolean(
        String(session.cookie || '').trim() &&
        String(session.device_id || '').trim()
    );
}

function getGuestSnapshotLaunchContext() {
    if (state.sessionMode !== SESSION_MODE_GUEST) {
        return {
            snapshot: null,
            conversationId: '',
            sectionId: null,
            hasConversationData: false,
            hasSelectedSnapshot: false,
            hasSessionSeed: false,
        };
    }

    const activeSnapshot = getActiveGuestSnapshot();
    const activeSnapshotId = String(activeSnapshot?.snapshot_id || '').trim();
    const activeBucket = activeSnapshotId
        ? getGuestConversationBucket(activeSnapshotId)
        : getActiveGuestConversationStore();
    const currentConversationId = getStableCurrentConversationId();
    const canUseCurrentConversation = Boolean(
        currentConversationId &&
        (!activeSnapshot || isConversationInGuestSnapshot(activeSnapshot, currentConversationId))
    );
    if (canUseCurrentConversation) {
        const currentConversation = activeBucket.find((item) => item.id === currentConversationId)
            || buildGuestConversationItem(currentConversationId);
        return {
            snapshot: activeSnapshot,
            conversationId: currentConversationId,
            sectionId: currentConversation?.sectionId || state.currentSectionId || null,
            hasConversationData: true,
            hasSelectedSnapshot: Boolean(activeSnapshot),
            hasSessionSeed: hasGuestSnapshotSessionSeed(activeSnapshot),
        };
    }

    const snapshotConversationIds = dedupeConversationIds(activeSnapshot?.conversation_ids || []);
    const snapshotRoomId = normalizeStableConversationId(activeSnapshot?.room_id);
    const snapshotConversationId = String(
        (snapshotRoomId && snapshotRoomId !== '0' && snapshotRoomId) ||
        snapshotConversationIds[0] ||
        ''
    ).trim();
    const existingConversation = snapshotConversationId
        ? activeBucket.find((item) => item.id === snapshotConversationId) || buildGuestConversationItem(snapshotConversationId)
        : null;
    return {
        snapshot: activeSnapshot,
        conversationId: snapshotConversationId,
        sectionId: existingConversation?.sectionId || null,
        hasConversationData: Boolean(snapshotConversationId && snapshotConversationId !== '0'),
        hasSelectedSnapshot: Boolean(activeSnapshot),
        hasSessionSeed: hasGuestSnapshotSessionSeed(activeSnapshot),
    };
}

function buildGuestVerificationRuntimeOverrides(overrides = {}) {
    if (state.sessionMode !== SESSION_MODE_GUEST) {
        return {
            seed_existing_cookies: true,
            reuse_existing_window: true,
            wait_for_ready: false,
            ...overrides,
        };
    }

    const launchContext = getGuestSnapshotLaunchContext();
    const shouldContinue = Boolean(launchContext.hasConversationData && launchContext.conversationId);
    return {
        conversation_mode: shouldContinue ? 'continue' : 'new',
        conversation_id: shouldContinue ? launchContext.conversationId : null,
        section_id: shouldContinue ? (launchContext.sectionId || null) : null,
        open_conversation_id: shouldContinue ? launchContext.conversationId : null,
        seed_existing_cookies: Boolean(launchContext.hasSelectedSnapshot && launchContext.hasSessionSeed),
        reuse_existing_window: false,
        wait_for_ready: false,
        ...overrides,
    };
}

function getGuestConversationBucket(snapshotId) {
    const normalizedSnapshotId = String(snapshotId || '').trim();
    if (!normalizedSnapshotId) {
        return [];
    }
    const bucket = state.guestConversationBuckets && typeof state.guestConversationBuckets === 'object'
        ? state.guestConversationBuckets[normalizedSnapshotId]
        : null;
    return Array.isArray(bucket) ? sanitizeConversationItems(bucket) : [];
}

function getActiveGuestConversationStore() {
    const snapshotId = getResolvedActiveGuestSnapshotId();
    const bucket = getGuestConversationBucket(snapshotId);
    if (bucket.length > 0) {
        return bucket;
    }
    if (String(state.activeGuestSnapshotId || '').trim() || String(state.activeGuestPoolKey || '').trim()) {
        return [];
    }
    return sanitizeConversationItems(state.guestConversationStore);
}

function setActiveGuestConversationStore(nextItems) {
    const snapshotId = getResolvedActiveGuestSnapshotId();
    const normalizedItems = sanitizeConversationItems(nextItems);
    normalizedItems.forEach((item) => upsertGuestConversationMetaEntry(item));
    if (!state.guestConversationBuckets || typeof state.guestConversationBuckets !== 'object') {
        state.guestConversationBuckets = {};
    }
    state.guestConversationBuckets[snapshotId] = [...normalizedItems];
    const activeSnapshot = Array.isArray(state.guestSnapshots)
        ? state.guestSnapshots.find((snapshot) => snapshot?.snapshot_id === snapshotId)
        : null;
    if (activeSnapshot) {
        activeSnapshot.conversation_ids = normalizedItems.map((item) => item.id);
        activeSnapshot.conversation_count = activeSnapshot.conversation_ids.length;
    }
    state.guestConversationStore = [...normalizedItems];
}

function findGuestConversationAcrossBuckets(conversationId) {
    const normalizedConversationId = String(conversationId || '').trim();
    if (!normalizedConversationId) {
        return null;
    }
    const activeItems = getActiveGuestConversationStore();
    const activeMatch = activeItems.find((item) => item.id === normalizedConversationId);
    if (activeMatch) {
        return activeMatch;
    }
    const bucketValues = Object.values(state.guestConversationBuckets || {});
    for (const bucket of bucketValues) {
        if (!Array.isArray(bucket)) {
            continue;
        }
        const match = sanitizeConversationItems(bucket).find((item) => item.id === normalizedConversationId);
        if (match) {
            return match;
        }
    }
    return sanitizeConversationItems(state.guestConversationStore).find(
        (item) => item.id === normalizedConversationId
    ) || (
        (() => {
            const meta = getGuestConversationMetaEntry(normalizedConversationId);
            if (!meta) {
                return null;
            }
            return {
                id: normalizedConversationId,
                sectionId: meta.sectionId || '',
                title: meta.title || `缓存会话 ${normalizedConversationId.slice(-8)}`,
            };
        })()
    );
}

function findGuestConversationLocation(conversationId) {
    const normalizedConversationId = String(conversationId || '').trim();
    if (!normalizedConversationId) {
        return null;
    }

    for (const snapshot of Array.isArray(state.guestSnapshots) ? state.guestSnapshots : []) {
        const snapshotId = String(snapshot?.snapshot_id || '').trim();
        if (!snapshotId) {
            continue;
        }
        const snapshotConversationIds = dedupeConversationIds(snapshot?.conversation_ids || []);
        if (!snapshotConversationIds.includes(normalizedConversationId)) {
            continue;
        }
        const bucket = getGuestConversationBucket(snapshotId);
        const match = bucket.find((item) => item.id === normalizedConversationId) || findGuestConversationAcrossBuckets(normalizedConversationId);
        if (match) {
            return {
                snapshotId,
                item: match,
            };
        }
    }

    const activeSnapshotId = getResolvedActiveGuestSnapshotId();
    const activeItems = getGuestConversationBucket(activeSnapshotId);
    const activeMatch = activeItems.find((item) => item.id === normalizedConversationId);
    if (activeMatch) {
        return {
            snapshotId: activeSnapshotId,
            item: activeMatch,
        };
    }
    return null;
}

function findGuestConversationLocations(conversationId) {
    const normalizedConversationId = String(conversationId || '').trim();
    if (!normalizedConversationId) {
        return [];
    }
    const locations = [];
    for (const snapshot of Array.isArray(state.guestSnapshots) ? state.guestSnapshots : []) {
        const snapshotId = String(snapshot?.snapshot_id || '').trim();
        if (!snapshotId) {
            continue;
        }
        const snapshotConversationIds = dedupeConversationIds(snapshot?.conversation_ids || []);
        if (!snapshotConversationIds.includes(normalizedConversationId)) {
            continue;
        }
        const bucket = getGuestConversationBucket(snapshotId);
        const item = bucket.find((entry) => entry.id === normalizedConversationId)
            || findGuestConversationAcrossBuckets(normalizedConversationId)
            || buildGuestConversationItem(normalizedConversationId);
        if (!item) {
            continue;
        }
        locations.push({
            snapshotId,
            item,
            active: Boolean(snapshot?.active),
        });
    }
    return locations;
}

function ensureActiveGuestSnapshotForConversation(conversationId, { persist = true } = {}) {
    if (state.sessionMode !== SESSION_MODE_GUEST) {
        return false;
    }
    const normalizedConversationId = String(conversationId || '').trim();
    if (!normalizedConversationId) {
        return false;
    }
    const currentSnapshotId = String(state.activeGuestSnapshotId || '').trim();
    const currentSnapshot = currentSnapshotId
        ? (Array.isArray(state.guestSnapshots) ? state.guestSnapshots.find((item) => item.snapshot_id === currentSnapshotId) : null)
        : null;
    const currentSnapshotConversationIds = dedupeConversationIds(currentSnapshot?.conversation_ids || []);
    if (currentSnapshotConversationIds.includes(normalizedConversationId)) {
        return false;
    }

    const locations = findGuestConversationLocations(normalizedConversationId);
    if (!locations.length) {
        return false;
    }

    const preferredLocation = locations.find((item) => item.snapshotId === currentSnapshotId)
        || locations.find((item) => item.active)
        || (locations.length === 1 ? locations[0] : null);
    if (!preferredLocation || !preferredLocation.snapshotId || preferredLocation.snapshotId === state.activeGuestSnapshotId) {
        return false;
    }

    state.activeGuestSnapshotId = preferredLocation.snapshotId;
    state.activeGuestPoolKey = getGuestSnapshotPoolKey(
        Array.isArray(state.guestSnapshots)
            ? state.guestSnapshots.find((item) => item?.snapshot_id === preferredLocation.snapshotId)
            : null
    ) || null;
    state.guestConversationStore = [...getGuestConversationBucket(preferredLocation.snapshotId)];
    if (persist) {
        saveActiveGuestSnapshotId();
    }
    return true;
}

function dedupeConversationIds(ids) {
    return Array.from(new Set(
        (Array.isArray(ids) ? ids : [])
            .map((value) => normalizeStableConversationId(value))
            .filter(Boolean)
    ));
}

function buildGuestConversationItem(conversationId, ...fallbackItems) {
    const normalizedConversationId = normalizeStableConversationId(conversationId);
    if (!normalizedConversationId) {
        return null;
    }
    const fallback = sanitizeConversationItems(fallbackItems.filter(Boolean))[0] || null;
    const meta = getGuestConversationMetaEntry(normalizedConversationId);
    return {
        id: normalizedConversationId,
        sectionId: String(fallback?.sectionId || meta?.sectionId || '').trim(),
        title: String(
            fallback?.title ||
            meta?.title ||
            `缓存会话 ${normalizedConversationId.slice(-8)}`
        ).trim(),
    };
}

function syncGuestConversationBucketsFromSnapshots() {
    const nextBuckets = {};
    const snapshots = Array.isArray(state.guestSnapshots) ? state.guestSnapshots : [];

    snapshots.forEach((snapshot) => {
        const snapshotId = String(snapshot?.snapshot_id || '').trim();
        if (!snapshotId) {
            return;
        }
        const snapshotConversationIds = dedupeConversationIds(snapshot?.conversation_ids || []);
        const existingBucket = getGuestConversationBucket(snapshotId);
        const nextItems = snapshotConversationIds
            .map((conversationId) => {
                const existing = existingBucket.find((item) => item.id === conversationId);
                const item = buildGuestConversationItem(conversationId, existing);
                if (item) {
                    upsertGuestConversationMetaEntry(item);
                }
                return item;
            })
            .filter((item) => isRenderableConversationItem(item));
        nextBuckets[snapshotId] = nextItems;
    });

    if (!snapshots.length && Array.isArray(state.guestConversationStore) && state.guestConversationStore.length > 0) {
        nextBuckets['guest:default'] = sanitizeConversationItems(state.guestConversationStore);
        nextBuckets['guest:default'].forEach((item) => upsertGuestConversationMetaEntry(item));
    }

    state.guestConversationBuckets = nextBuckets;
    const activeSnapshotId = getResolvedActiveGuestSnapshotId();
    state.guestConversationStore = [...sanitizeConversationItems(nextBuckets[activeSnapshotId] || [])];
}

function normalizeGuestSnapshot(snapshot) {
    const roomId = normalizeStableConversationId(snapshot?.room_id);
    const conversationIds = dedupeConversationIds([
        roomId,
        ...(Array.isArray(snapshot?.conversation_ids) ? snapshot.conversation_ids : []),
    ]);
    const backupAtMs = Number(snapshot?.backup_at_ms || 0);
    const poolKey = getGuestSnapshotPoolKey(snapshot);
    return {
        ...snapshot,
        snapshot_id: String(snapshot?.snapshot_id || '').trim(),
        source: String(snapshot?.source || '').trim() || 'backup',
        pool_key: poolKey,
        room_id: roomId,
        conversation_ids: conversationIds,
        conversation_count: conversationIds.length,
        backup_at_ms: Number.isFinite(backupAtMs) && backupAtMs > 0 ? backupAtMs : null,
        reason: String(snapshot?.reason || '').trim() || null,
    };
}

function isDisplayableGuestSnapshot(snapshot, { selectedSnapshotId = '', activeSnapshotId = '' } = {}) {
    const normalizedSelectedSnapshotId = String(selectedSnapshotId || '').trim();
    const normalizedActiveSnapshotId = String(activeSnapshotId || '').trim();
    const roomId = normalizeStableConversationId(snapshot?.room_id);
    const conversationIds = dedupeConversationIds(snapshot?.conversation_ids || []);
    const hasConversations = conversationIds.length > 0;
    const hasRoom = roomId && roomId !== '0';
    if (snapshot?.snapshot_id === normalizedSelectedSnapshotId || snapshot?.snapshot_id === normalizedActiveSnapshotId) {
        return true;
    }
    if (snapshot?.active || snapshot?.source === 'current') {
        return true;
    }
    return hasConversations || hasRoom;
}

function buildGuestSnapshotDisplayKey(snapshot) {
    const sourceKey = snapshot?.source === 'current' ? 'current' : 'backup';
    const roomKey = String(snapshot?.room_id || '').trim() || '0';
    const conversationKey = dedupeConversationIds(snapshot?.conversation_ids || []).join(',');
    const reasonKey = sourceKey === 'backup'
        ? String(snapshot?.reason || '').trim().toLowerCase()
        : '';
    return [sourceKey, roomKey, conversationKey, reasonKey].join('|');
}

function shouldReplaceGuestSnapshotDisplay(existingSnapshot, nextSnapshot, { selectedSnapshotId = '', activeSnapshotId = '' } = {}) {
    const normalizedSelectedSnapshotId = String(selectedSnapshotId || '').trim();
    const normalizedActiveSnapshotId = String(activeSnapshotId || '').trim();
    if (existingSnapshot?.snapshot_id === normalizedSelectedSnapshotId || existingSnapshot?.snapshot_id === normalizedActiveSnapshotId) {
        return false;
    }
    if (nextSnapshot?.snapshot_id === normalizedSelectedSnapshotId || nextSnapshot?.snapshot_id === normalizedActiveSnapshotId) {
        return true;
    }
    if (existingSnapshot?.active && !nextSnapshot?.active) {
        return false;
    }
    if (nextSnapshot?.active && !existingSnapshot?.active) {
        return true;
    }
    const existingBackupAtMs = Number(existingSnapshot?.backup_at_ms || 0);
    const nextBackupAtMs = Number(nextSnapshot?.backup_at_ms || 0);
    return nextBackupAtMs > existingBackupAtMs;
}

function normalizeGuestSnapshotsForDisplay(snapshots, { selectedSnapshotId = '', activeSnapshotId = '' } = {}) {
    const seenSnapshotIds = new Set();
    return (Array.isArray(snapshots) ? snapshots : [])
        .map((snapshot) => normalizeGuestSnapshot(snapshot))
        .filter((snapshot) => snapshot.snapshot_id)
        .filter((snapshot) => isDisplayableGuestSnapshot(snapshot, { selectedSnapshotId, activeSnapshotId }))
        .filter((snapshot) => {
            if (seenSnapshotIds.has(snapshot.snapshot_id)) {
                return false;
            }
            seenSnapshotIds.add(snapshot.snapshot_id);
            return true;
        });
}

function isRenderableConversationItem(item) {
    if (!item || typeof item !== 'object') {
        return false;
    }
    const id = String(item.id || '').trim();
    const title = String(item.title || '').trim();
    const sectionId = String(item.sectionId || '').trim();
    if (!normalizeStableConversationId(id)) {
        return false;
    }
    if (!title) {
        return false;
    }
    if (!sectionId && /^游客会话\s+[A-Za-z0-9_-]+$/u.test(title)) {
        return false;
    }
    return true;
}

function sanitizeConversationItems(items) {
    const seen = new Set();
    return (Array.isArray(items) ? items : [])
        .map((item) => ({
            id: String((item && item.id) || '').trim(),
            sectionId: String((item && item.sectionId) || '').trim(),
            title: String((item && item.title) || '').trim(),
        }))
        .filter((item) => {
            if (!isRenderableConversationItem(item) || seen.has(item.id)) {
                return false;
            }
            seen.add(item.id);
            return true;
        });
}

function getActiveGuestSnapshot() {
    const snapshots = Array.isArray(state.guestSnapshots) ? state.guestSnapshots : [];
    const selected = resolveGuestSnapshotSelection({
        snapshotId: state.activeGuestSnapshotId,
        poolKey: state.activeGuestPoolKey,
    });
    if (selected) {
        return selected;
    }
    return snapshots.find((item) => item.active) || snapshots[0] || null;
}

function getPreferredGuestSnapshotContext() {
    if (state.sessionMode !== SESSION_MODE_GUEST) {
        return {
            conversationId: '',
            sectionId: null,
        };
    }

    const launchContext = getGuestSnapshotLaunchContext();
    if (launchContext.hasConversationData && launchContext.conversationId) {
        return {
            conversationId: launchContext.conversationId,
            sectionId: launchContext.sectionId || null,
        };
    }
    return {
        conversationId: '',
        sectionId: null,
    };
}

function buildGuestVisibleConversations() {
    const activeSnapshot = getActiveGuestSnapshot();
    if (!activeSnapshot) {
        return sanitizeConversationItems(getActiveGuestConversationStore());
    }
    const activeSnapshotId = String(activeSnapshot?.snapshot_id || getResolvedActiveGuestSnapshotId()).trim();
    const activeItems = getGuestConversationBucket(activeSnapshotId);
    const snapshotIds = dedupeConversationIds(activeSnapshot?.conversation_ids || []);
    if (snapshotIds.length === 0) {
        return [];
    }

    return sanitizeConversationItems(snapshotIds
        .map((conversationId) => {
            const existing = activeItems.find((item) => item.id === conversationId) || null;
            return buildGuestConversationItem(conversationId, existing);
        })
        .filter((item) => isRenderableConversationItem(item)));
}

function syncVisibleConversationList() {
    state.conversations = state.sessionMode === SESSION_MODE_GUEST
        ? buildGuestVisibleConversations()
        : [...state.authConversations];
}

function buildConversationListStructureKey() {
    const browseHistory = canBrowseHistory() ? '1' : '0';
    const guestSnapshotKey = state.sessionMode === SESSION_MODE_GUEST
        ? String(getGuestSnapshotPoolKey(getActiveGuestSnapshot()) || state.activeGuestSnapshotId || '').trim()
        : '';
    const itemsKey = (Array.isArray(state.conversations) ? state.conversations : [])
        .map((item) => [
            String((item && item.id) || '').trim(),
            String((item && item.sectionId) || '').trim(),
            String((item && item.title) || '').trim(),
        ].join('\u001f'))
        .join('\u001e');
    return [state.sessionMode, guestSnapshotKey, browseHistory, itemsKey].join('\u001d');
}

function updateConversationListActiveState({ scrollActive = false } = {}) {
    if (!conversationList) {
        return;
    }
    const activeId = String(state.currentConversationId || '').trim();
    let hasActiveItem = false;
    conversationList.querySelectorAll('.conversation-item').forEach((item) => {
        const isActive = Boolean(activeId) && item.dataset.conversationId === activeId;
        item.classList.toggle('active', isActive);
        if (isActive) {
            hasActiveItem = true;
        }
    });
    if (scrollActive && hasActiveItem) {
        scrollConversationListToActive('auto');
    }
}

function findConversationEntry(conversationId, mode = state.sessionMode) {
    const normalizedConversationId = String(conversationId || '').trim();
    if (!normalizedConversationId) {
        return null;
    }
    if (mode === SESSION_MODE_GUEST) {
        return findGuestConversationAcrossBuckets(normalizedConversationId);
    }
    return getModeConversationStore(mode).find((item) => item.id === normalizedConversationId) || null;
}

function buildWelcomeMarkup() {
    const modeText = state.sessionMode === SESSION_MODE_AUTH
        ? '当前为登录态，可以恢复历史并继续已有会话。'
        : state.sessionMode === SESSION_MODE_AUTO
            ? '当前为自动模式，系统会按可用会话和 capture 状态自动选择链路。'
            : '当前为游客态，系统会保留当前活跃会话，并在首发时自动处理验证与 capture。';

    return `
        <div class="welcome-card">
            <p class="eyebrow">准备就绪</p>
            <h2>${escapeHtml(state.currentConversationTitle || '新对话')}</h2>
            <p>${modeText}</p>
        </div>
    `;
}

function resetChatToWelcome() {
    setCurrentConversationMessages([]);
    chatContainer.innerHTML = buildWelcomeMarkup();
    scrollChatToBottom('auto');
}

function scrollChatToBottom(behavior = 'smooth') {
    requestAnimationFrame(() => {
        chatContainer.scrollTo({
            top: chatContainer.scrollHeight,
            behavior,
        });
    });
}

function isElementFullyVisible(container, element) {
    if (!container || !element) {
        return false;
    }
    const containerTop = container.scrollTop;
    const containerBottom = containerTop + container.clientHeight;
    const elementTop = element.offsetTop;
    const elementBottom = elementTop + element.offsetHeight;
    return elementTop >= containerTop && elementBottom <= containerBottom;
}

function scrollConversationListToActive(behavior = 'smooth') {
    requestAnimationFrame(() => {
        const activeItem = conversationList.querySelector('.conversation-item.active');
        if (!activeItem) {
            return;
        }
        if (isElementFullyVisible(conversationList, activeItem)) {
            return;
        }
        activeItem.scrollIntoView({
            block: 'nearest',
            inline: 'nearest',
            behavior,
        });
    });
}

function updateDeleteButtonState() {
    deleteChatButton.disabled = !canBrowseHistory() || !state.currentConversationId;
}

function renderGuestSnapshotSummary() {
    if (!guestSessionSummary) {
        return;
    }
    if (state.sessionMode !== SESSION_MODE_GUEST) {
        guestSessionSummary.textContent = '当前为非游客态，缓存池面板已停用。';
        return;
    }
    const snapshots = Array.isArray(state.guestSnapshots) ? state.guestSnapshots : [];
    const activeSnapshot = getActiveGuestSnapshot();
    if (!snapshots.length) {
        guestSessionSummary.textContent = '这里显示游客缓存池卡片，每张卡只代表一个缓存池，不代表单条会话。';
        return;
    }
    const poolLabel = String(activeSnapshot?.label || '未选中缓存池').trim();
    guestSessionSummary.textContent = `当前共有 ${snapshots.length} 个缓存池；当前选中：${poolLabel}。这里不直接展示会话内容。`;
}

function renderConversationListSummary() {
    if (!conversationListSummary) {
        return;
    }
    if (state.sessionMode !== SESSION_MODE_GUEST) {
        conversationListSummary.textContent = '这里显示当前模式下的可恢复会话列表。';
        return;
    }
    const activeSnapshot = getActiveGuestSnapshot();
    const snapshotLabel = String(activeSnapshot?.label || '未选中缓存池').trim();
    const conversationCount = Array.isArray(state.conversations) ? state.conversations.length : 0;
    conversationListSummary.textContent = `会话列表由“${snapshotLabel}”分发，当前显示 ${conversationCount} 个会话。`;
}

function updateSessionModeUI() {
    sessionModeSelect.value = state.sessionMode;
    sessionModeHint.textContent = getSessionModeHint(state.sessionMode);
    conversationModeBadge.textContent = getModeBadgeLabel(state.sessionMode);
    if (resetGuestButton) {
        resetGuestButton.disabled = state.sessionMode !== SESSION_MODE_GUEST;
    }
    syncVisibleConversationList();
    renderGuestSnapshotList();
    renderConversationList();
    updateDeleteButtonState();
}

function renderRuntimeBanner(payload, { hidden = false } = {}) {
    if (hidden || !payload) {
        runtimeBanner.classList.add('hidden');
        runtimeBanner.classList.remove('ready', 'waiting');
        return;
    }

    runtimeBanner.classList.remove('hidden');
    runtimeBanner.classList.toggle('ready', Boolean(payload.ready));
    runtimeBanner.classList.toggle('waiting', !payload.ready);
    runtimeBannerTitle.textContent = payload.ready ? '运行时已准备' : '等待运行时准备';
    runtimeBannerText.textContent = payload.message || '等待验证窗口与 capture 进入可用状态。';
}

function normalizeRuntimePayload(value) {
    if (!value || typeof value !== 'object') {
        return null;
    }
    return {
        ready: Boolean(value.ready),
        capture_ready: Boolean(value.capture_ready),
        manual_ready: Boolean(value.manual_ready),
        awaiting_user_action: Boolean(value.awaiting_user_action),
        manual_window_opened: Boolean(value.manual_window_opened),
        message: value.message || '运行时状态已更新。',
        open_url: value.open_url || null,
    };
}

function setMessageBodyContent(body, content, images = []) {
    if (!body) {
        return;
    }
    body.innerHTML = escapeHtml(content).replace(/\n/g, '<br>');

    if (Array.isArray(images) && images.length > 0) {
        const imageGroup = document.createElement('div');
        imageGroup.className = 'message-images';
        images.forEach((imageUrl) => {
            const img = document.createElement('img');
            img.className = 'message-image';
            img.src = imageUrl;
            img.alt = '图片';
            img.addEventListener('click', () => window.open(imageUrl, '_blank'));
            imageGroup.appendChild(img);
        });
        body.appendChild(imageGroup);
    }
}

function createMessageElement(content, isUser, images = [], className = '') {
    const messageDiv = document.createElement('div');
    messageDiv.className = `message ${isUser ? 'user-message' : 'bot-message'} ${className}`.trim();

    const avatar = document.createElement('div');
    avatar.className = 'message-avatar';
    avatar.textContent = className === 'error-message' ? '!' : isUser ? 'U' : 'AI';

    const body = document.createElement('div');
    body.className = 'message-content';
    setMessageBodyContent(body, content, images);

    messageDiv.appendChild(avatar);
    messageDiv.appendChild(body);
    return messageDiv;
}

function appendMessageElementToChat(content, isUser, images = [], className = '') {
    if (chatContainer.querySelector('.welcome-card')) {
        chatContainer.innerHTML = '';
    }
    chatContainer.appendChild(createMessageElement(content, isUser, images, className));
    scrollChatToBottom('auto');
}

function addMessageToChat(content, isUser, images = [], className = '', options = {}) {
    appendMessageElementToChat(content, isUser, images, className);
    if (options.track === false) {
        return;
    }
    appendCurrentConversationMessage({
        role: options.role || (className === 'error-message' ? 'system' : (isUser ? 'user' : 'assistant')),
        text: content,
        images,
        className,
    });
}

function addStreamingMessage(content = '') {
    if (chatContainer.querySelector('.welcome-card')) {
        chatContainer.innerHTML = '';
    }
    const messageId = `stream-${Date.now()}-${Math.random().toString(16).slice(2, 8)}`;
    const messageElement = createMessageElement(content, false, [], 'streaming-message');
    messageElement.id = messageId;
    chatContainer.appendChild(messageElement);
    scrollChatToBottom('auto');
    return messageId;
}

function updateStreamingMessage(messageId, content, images = []) {
    const messageElement = document.getElementById(messageId);
    if (!messageElement) {
        return;
    }
    const body = messageElement.querySelector('.message-content');
    setMessageBodyContent(body, content, images);
    scrollChatToBottom('auto');
}

function finalizeStreamingMessage(messageId, content, images = []) {
    const messageElement = document.getElementById(messageId);
    if (!messageElement) {
        return;
    }
    messageElement.classList.remove('streaming-message');
    updateStreamingMessage(messageId, content, images);
    if (!String(content || '').trim() && (!Array.isArray(images) || images.length === 0)) {
        return;
    }
    appendCurrentConversationMessage({
        role: 'assistant',
        text: content,
        images,
        className: '',
    });
}

function removeStreamingMessage(messageId) {
    const messageElement = document.getElementById(messageId);
    if (messageElement) {
        messageElement.remove();
    }
}

function addLoadingMessage(text = '正在准备发送...') {
    const loadingId = `loading-${Date.now()}`;
    const loadingDiv = document.createElement('div');
    loadingDiv.id = loadingId;
    loadingDiv.className = 'message bot-message';
    loadingDiv.innerHTML = `
        <div class="message-avatar">AI</div>
        <div class="message-content">
            <span class="loading-dot"></span>
            <span class="loading-text">${escapeHtml(text)}</span>
        </div>
    `;
    chatContainer.appendChild(loadingDiv);
    scrollChatToBottom('auto');
    return loadingId;
}

function updateLoadingMessage(id, text) {
    const loadingMessage = document.getElementById(id);
    if (!loadingMessage) {
        return;
    }
    const label = loadingMessage.querySelector('.loading-text');
    if (label) {
        label.textContent = text;
    }
    scrollChatToBottom('auto');
}

function removeLoadingMessage(id) {
    const loadingMessage = document.getElementById(id);
    if (loadingMessage) {
        loadingMessage.remove();
    }
    scrollChatToBottom('auto');
}

function showError(message) {
    addMessageToChat(`错误：${message}`, false, [], 'error-message', { role: 'system' });
    console.error(message);
}

async function parseJsonResponse(response) {
    const contentType = response.headers.get('content-type') || '';
    if (contentType.includes('application/json')) {
        return response.json();
    }
    const text = await response.text();
    return { detail: text };
}

function formatErrorDetail(detail, status) {
    if (typeof detail === 'string' && detail.trim()) {
        return detail;
    }
    if (detail && typeof detail === 'object') {
        if (typeof detail.message === 'string' && detail.message.trim()) {
            return detail.message;
        }
        if (typeof detail.detail === 'string' && detail.detail.trim()) {
            return detail.detail;
        }
        return JSON.stringify(detail);
    }
    return `请求失败 (${status})`;
}

function buildRequestError(status, detail, fallbackMessage = '') {
    const message = fallbackMessage || formatErrorDetail(detail, status);
    const error = new Error(message || `请求失败 (${status || 500})`);
    error.status = Number(status || 500);
    error.detail = detail;
    return error;
}

async function requestJson(url, options = {}) {
    const response = await fetch(url, options);
    const payload = await parseJsonResponse(response).catch(() => ({}));
    if (!response.ok) {
        throw buildRequestError(response.status, payload.detail ?? payload);
    }
    return payload;
}

async function fetchConversationInfo(conversationId) {
    return requestJson(`/api/chat/conversation/info?conversation_id=${encodeURIComponent(conversationId)}`);
}

async function fetchConversationMessages(conversationId, anchorIndex) {
    const params = new URLSearchParams({
        conversation_id: conversationId,
        limit: '20',
    });
    if (anchorIndex !== undefined && anchorIndex !== null && anchorIndex !== '') {
        params.set('anchor_index', String(anchorIndex));
    }
    return requestJson(`/api/chat/conversation/messages?${params.toString()}`);
}

function renderConversationMessages(messages) {
    chatContainer.innerHTML = '';
    if (!messages || messages.length === 0) {
        resetChatToWelcome();
        return;
    }
    const normalizedMessages = messages
        .map((message) => normalizeConversationMessageRecord({
            role: message.role,
            text: message.text,
            images: message.images || message.img_urls || [],
            className: message.role === 'system' ? 'error-message' : '',
        }))
        .filter(Boolean);
    setCurrentConversationMessages(normalizedMessages);
    normalizedMessages.forEach((message) => {
        appendMessageElementToChat(
            message.text,
            message.role === 'user',
            message.images,
            message.className,
        );
    });
    scrollChatToBottom('auto');
}

function renderGuestSnapshotList() {
    if (!guestSessionPanel || !guestSessionList) {
        return;
    }

    const shouldShow = state.sessionMode === SESSION_MODE_GUEST;
    guestSessionPanel.classList.toggle('hidden', !shouldShow);
    if (!shouldShow) {
        guestSessionList.innerHTML = '';
        return;
    }

    guestSessionList.innerHTML = '';
    const snapshots = Array.isArray(state.guestSnapshots) ? state.guestSnapshots : [];
    if (snapshots.length === 0) {
        renderGuestSnapshotSummary();
        const empty = document.createElement('div');
        empty.className = 'guest-session-empty';
        empty.textContent = '当前还没有可切换的游客缓存，首次验证成功后会自动写入。';
        guestSessionList.appendChild(empty);
        return;
    }

    snapshots.forEach((snapshot) => {
        const snapshotConversationCount = dedupeConversationIds(snapshot.conversation_ids || []).length
            || Number(snapshot.conversation_count || 0);
        const button = document.createElement('button');
        button.type = 'button';
        const isActiveSnapshot = String(snapshot.snapshot_id || '').trim()
            && String(getActiveGuestSnapshot()?.snapshot_id || '').trim() === String(snapshot.snapshot_id || '').trim();
        button.className = `guest-snapshot-item ${isActiveSnapshot ? 'active' : ''}`;
        const sourceLabel = snapshot.source === 'backup' ? '备份' : '当前';
        const roomLabel = snapshot.room_id ? String(snapshot.room_id).slice(-8) : 'new';
        const reasonLabel = snapshot.reason ? ` · ${snapshot.reason}` : '';
        button.innerHTML = `
            <span class="guest-snapshot-title">
                ${escapeHtml(snapshot.label || `游客缓存 ${roomLabel}`)}
                <span class="guest-snapshot-badge">${escapeHtml(sourceLabel)}</span>
            </span>
            <span class="guest-snapshot-meta">
                ${escapeHtml(`${snapshot.conversation_count || 0} 个会话 · room ${roomLabel}${reasonLabel}`)}
            </span>
        `;
        const guestSnapshotMeta = button.querySelector('.guest-snapshot-meta');
        if (guestSnapshotMeta) {
            guestSnapshotMeta.textContent = `${snapshotConversationCount} 个会话 · room ${roomLabel}${reasonLabel}`;
        }
        button.addEventListener('click', async () => {
            if (snapshot.snapshot_id === state.activeGuestSnapshotId) {
                return;
            }
            await restoreGuestSnapshot(snapshot.snapshot_id);
        });
        guestSessionList.appendChild(button);
    });
    renderGuestSnapshotSummary();
}

function renderConversationList() {
    syncVisibleConversationList();
    const shouldScrollToActive = conversationListScrollMode === 'new';
    const previousScrollTop = shouldScrollToActive ? 0 : conversationList.scrollTop;
    const structureKey = buildConversationListStructureKey();
    if (structureKey === lastConversationListStructureKey && conversationList.childElementCount > 0) {
        renderConversationListSummary();
        updateConversationListActiveState({ scrollActive: shouldScrollToActive });
        conversationListScrollMode = 'preserve';
        return;
    }

    conversationList.innerHTML = '';
    if (!state.conversations.length) {
        renderConversationListSummary();
        const empty = document.createElement('div');
        empty.className = 'conversation-list-empty';
        empty.textContent = state.sessionMode === SESSION_MODE_GUEST
            ? '当前游客缓存还没有可见会话，完成一次发送后会自动出现。'
            : '当前没有可恢复会话。';
        conversationList.appendChild(empty);
        lastConversationListStructureKey = structureKey;
        if (!shouldScrollToActive) {
            requestAnimationFrame(() => {
                conversationList.scrollTop = previousScrollTop;
            });
        }
        conversationListScrollMode = 'preserve';
        return;
    }

    const fragment = document.createDocumentFragment();
    state.conversations.forEach((conversation) => {
        const item = document.createElement('button');
        item.type = 'button';
        item.className = 'conversation-item';
        item.dataset.conversationId = String(conversation.id || '').trim();
        if (!canBrowseHistory()) {
            item.classList.add('disabled');
        }
        const metaId = String(conversation.id || '').trim();
        item.innerHTML = `
            <span class="conversation-item-title">${escapeHtml(conversation.title || '继续对话')}</span>
            <span class="conversation-item-meta">${escapeHtml(metaId ? `ID ${metaId.slice(-8)}` : '未绑定')}</span>
        `;
        item.addEventListener('click', () => {
            if (!canBrowseHistory()) {
                showError('当前模式不支持切换历史会话，但会保留当前活跃会话继续发送。');
                return;
            }
            selectConversation(conversation.id, conversation.sectionId, conversation.title);
        });
        fragment.appendChild(item);
    });
    conversationList.appendChild(fragment);
    lastConversationListStructureKey = structureKey;
    renderConversationListSummary();
    updateConversationListActiveState({ scrollActive: shouldScrollToActive });
    if (!shouldScrollToActive) {
        requestAnimationFrame(() => {
            conversationList.scrollTop = previousScrollTop;
        });
    }
    conversationListScrollMode = 'preserve';
}

function syncGuestSnapshotState(data) {
    if (!data || typeof data !== 'object') {
        return;
    }
    const snapshots = Array.isArray(data.snapshots) ? data.snapshots : [];
    const previouslySelectedSnapshotId = String(state.activeGuestSnapshotId || '').trim();
    const previouslySelectedPoolKey = String(state.activeGuestPoolKey || '').trim()
        || getGuestSnapshotPoolKey(
            Array.isArray(state.guestSnapshots)
                ? state.guestSnapshots.find((snapshot) => String(snapshot?.snapshot_id || '').trim() === previouslySelectedSnapshotId)
                : null
        );
    state.guestSnapshots = normalizeGuestSnapshotsForDisplay(snapshots, {
        selectedSnapshotId: previouslySelectedSnapshotId,
        activeSnapshotId: data.active_snapshot_id,
    });
    const activeSnapshot = resolveGuestSnapshotSelection({
        snapshotId: previouslySelectedSnapshotId,
        poolKey: previouslySelectedPoolKey,
    })
        || state.guestSnapshots.find((snapshot) => snapshot.active)
        || state.guestSnapshots.find((snapshot) => snapshot.snapshot_id === data.active_snapshot_id)
        || state.guestSnapshots[0]
        || null;
    state.activeGuestSnapshotId = activeSnapshot ? activeSnapshot.snapshot_id : null;
    state.activeGuestPoolKey = getGuestSnapshotPoolKey(activeSnapshot) || null;
    syncGuestConversationBucketsFromSnapshots();
    saveConversations(SESSION_MODE_GUEST);
    saveActiveGuestSnapshotId();
    syncVisibleConversationList();
    renderGuestSnapshotList();
    renderConversationList();
}

async function refreshGuestSnapshots({ silent = false } = {}) {
    if (state.sessionMode !== SESSION_MODE_GUEST) {
        state.guestSnapshots = [];
        state.activeGuestSnapshotId = null;
        state.activeGuestPoolKey = null;
        saveActiveGuestSnapshotId();
        renderGuestSnapshotList();
        return null;
    }

    try {
        const data = await requestGuestSnapshots();
        syncGuestSnapshotState(data);
        return data;
    } catch (error) {
        if (!silent) {
            showError(`刷新游客缓存列表失败：${error.message}`);
        }
        return null;
    }
}

async function restoreGuestSnapshot(snapshotId) {
    if (!snapshotId || state.sessionMode !== SESSION_MODE_GUEST) {
        return;
    }

    if (refreshGuestSnapshotsButton) {
        refreshGuestSnapshotsButton.disabled = true;
    }
    renderRuntimeBanner({
        ready: false,
        message: '正在切换游客缓存并准备验证窗口...',
    });
    try {
        const selectedSnapshot = resolveGuestSnapshotSelection({
            snapshotId,
            poolKey: getGuestSnapshotPoolKey(
                Array.isArray(state.guestSnapshots)
                    ? state.guestSnapshots.find((snapshot) => String(snapshot?.snapshot_id || '').trim() === String(snapshotId || '').trim())
                    : null
            ),
        }) || (Array.isArray(state.guestSnapshots)
            ? state.guestSnapshots.find((snapshot) => String(snapshot?.snapshot_id || '').trim() === String(snapshotId || '').trim())
            : null);
        const data = await requestGuestSnapshotRestore(snapshotId);
        state.activeGuestSnapshotId = String(selectedSnapshot?.snapshot_id || snapshotId || '').trim() || null;
        state.activeGuestPoolKey = getGuestSnapshotPoolKey(selectedSnapshot) || null;
        saveActiveGuestSnapshotId();
        renderRuntimeBanner(normalizeRuntimePayload(data));
        await refreshGuestSnapshots({ silent: true });
        const refreshedActiveSnapshot = getActiveGuestSnapshot();
        const restoredConversationId = normalizeStableConversationId(
            data.restored_conversation_id ||
            (data.session_params && data.session_params.room_id) ||
            (Array.isArray(refreshedActiveSnapshot?.conversation_ids) ? refreshedActiveSnapshot.conversation_ids[0] : '') ||
            (getActiveGuestConversationStore()[0] && getActiveGuestConversationStore()[0].id) ||
            resolveConversationIdFromPayload(data) ||
            ''
        );
        if (restoredConversationId) {
            const existing = findConversationEntry(restoredConversationId, SESSION_MODE_GUEST);
            const restoredTitle = String(
                data.restored_conversation_name ||
                existing?.title ||
                state.currentConversationTitle ||
                buildTitleFromPrompt('游客缓存')
            ).trim();
            const restoredSectionId = data.restored_section_id || existing?.sectionId || null;
            setCurrentConversation(
                restoredConversationId,
                restoredSectionId,
                restoredTitle,
            );
            upsertConversation(restoredConversationId, restoredSectionId, restoredTitle);
            if (Array.isArray(data.history_messages) && data.history_messages.length > 0) {
                renderConversationMessages(data.history_messages);
            } else {
                await selectConversation(restoredConversationId, restoredSectionId, restoredTitle);
            }
        } else {
            createNewChat();
        }
    } catch (error) {
        showError(error.message);
    } finally {
        if (refreshGuestSnapshotsButton) {
            refreshGuestSnapshotsButton.disabled = false;
        }
    }
}

function upsertConversation(conversationId, sectionId, title, { moveToFront = false } = {}) {
    const normalizedConversationId = normalizeStableConversationId(conversationId);
    if (!normalizedConversationId) {
        return;
    }
    const store = [...getModeConversationStore()];
    const existingIndex = store.findIndex((item) => item.id === normalizedConversationId);
    const nextConversation = {
        id: normalizedConversationId,
        sectionId: sectionId || '',
        title: title || '继续对话',
    };
    if (existingIndex === -1) {
        if (moveToFront) {
            store.unshift(nextConversation);
            setConversationListScrollMode('new');
        } else {
            store.push(nextConversation);
        }
    } else {
        if (moveToFront) {
            store.splice(existingIndex, 1);
            store.unshift(nextConversation);
            setConversationListScrollMode('new');
        } else {
            store.splice(existingIndex, 1, nextConversation);
        }
    }
    setModeConversationStore(state.sessionMode, store);
    saveConversations(state.sessionMode);
    syncVisibleConversationList();
    renderConversationList();
}

function setCurrentConversation(conversationId, sectionId, title) {
    const normalizedConversationId = normalizeStableConversationId(conversationId);
    state.currentConversationId = normalizedConversationId || null;
    state.currentSectionId = normalizedConversationId ? (sectionId || null) : null;
    state.currentConversationTitle = title || '新对话';
    currentConversationTitle.textContent = state.currentConversationTitle;
    saveCurrentConversationState(state.sessionMode);
    syncVisibleConversationList();
    updateDeleteButtonState();
    renderConversationList();
}

async function selectConversation(id, sectionId, title) {
    setCurrentConversation(id, sectionId, title);
    if (!id) {
        resetChatToWelcome();
        return;
    }

    setCurrentConversationMessages([]);
    chatContainer.innerHTML = `
        <div class="welcome-card">
            <p class="eyebrow">正在恢复</p>
            <h2>${escapeHtml(title)}</h2>
            <p>正在加载该会话的历史消息...</p>
        </div>
    `;

    try {
        const info = await fetchConversationInfo(id);
        const resolvedSectionId = info.section_id || sectionId || null;
        const resolvedTitle = info.name || title || '继续对话';
        setCurrentConversation(id, resolvedSectionId, resolvedTitle);
        upsertConversation(id, resolvedSectionId, resolvedTitle, { moveToFront: false });

        const messageData = await fetchConversationMessages(id, info.latest_index);
        if (messageData.section_id) {
            state.currentSectionId = messageData.section_id;
            saveCurrentConversationState();
            upsertConversation(id, messageData.section_id, resolvedTitle, { moveToFront: false });
        }
        renderConversationMessages(messageData.messages);
    } catch (error) {
        resetChatToWelcome();
        showError(`恢复会话失败：${error.message}`);
    }
}

function createNewChat() {
    setCurrentConversation(null, null, '新对话');
    resetChatToWelcome();
}

async function deleteCurrentChat() {
    if (!state.currentConversationId || !canBrowseHistory()) {
        return;
    }

    try {
        const response = await requestJson(
            `/api/chat/delete?conversation_id=${encodeURIComponent(state.currentConversationId)}`,
            { method: 'POST' },
        );
        if (!response.ok) {
            throw new Error(response.msg || '删除会话失败');
        }
        const store = getModeConversationStore().filter((item) => item.id !== state.currentConversationId);
        setModeConversationStore(state.sessionMode, store);
        if (state.sessionMode === SESSION_MODE_GUEST) {
            state.guestSnapshots = state.guestSnapshots.map((snapshot) => ({
                ...snapshot,
                conversation_ids: dedupeConversationIds(
                    (snapshot.conversation_ids || []).filter((item) => item !== state.currentConversationId)
                ),
                conversation_count: Math.max(
                    0,
                    Number(snapshot.conversation_count || 0) - ((snapshot.conversation_ids || []).includes(state.currentConversationId) ? 1 : 0)
                ),
            }));
        }
        saveConversations(state.sessionMode);
        syncVisibleConversationList();
        createNewChat();
    } catch (error) {
        showError(`删除会话失败：${error.message}`);
    }
}

function buildConversationContext() {
    const guestSnapshotContext = getPreferredGuestSnapshotContext();
    const conversationId = getStableCurrentConversationId() || null;
    const sectionId = conversationId ? (state.currentSectionId || null) : null;
    return {
        conversation_mode: getConversationMode(),
        conversation_id: conversationId,
        section_id: sectionId,
        open_conversation_id: conversationId || guestSnapshotContext.conversationId || null,
    };
}

function buildRuntimePayload(overrides = {}) {
    return {
        guest: state.sessionMode === SESSION_MODE_GUEST,
        session_mode: state.sessionMode,
        seed_existing_cookies: true,
        reuse_existing_window: true,
        wait_for_ready: false,
        ...buildConversationContext(),
        ...overrides,
    };
}

async function requestRuntimeBootstrap(overrides = {}) {
    return requestJson('/api/chat/runtime/bootstrap', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json; charset=utf-8',
            'Accept': 'text/event-stream',
            'Cache-Control': 'no-cache',
        },
        body: JSON.stringify(buildRuntimePayload(overrides)),
    });
}

async function requestRuntimeStatus() {
    const params = new URLSearchParams({
        guest: String(state.sessionMode === SESSION_MODE_GUEST),
        session_mode: state.sessionMode,
    });
    const guestSnapshotContext = getPreferredGuestSnapshotContext();
    const targetConversationId = String(
        state.sessionMode === SESSION_MODE_GUEST
            ? guestSnapshotContext.conversationId
            : (getStableCurrentConversationId() || guestSnapshotContext.conversationId || '')
    ).trim();
    if (targetConversationId) {
        params.set('conversation_id', targetConversationId);
    }
    return requestJson(`/api/chat/runtime/status?${params.toString()}`);
}

async function requestManualVerificationStart(overrides = {}) {
    const payload = buildRuntimePayload(buildGuestVerificationRuntimeOverrides());
    return requestJson('/api/chat/runtime/manual-verify/start', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json; charset=utf-8',
        },
        body: JSON.stringify({
            ...payload,
            ...overrides,
        }),
    });
}

async function requestGuestSessionReset() {
    return requestJson('/api/chat/runtime/guest/reset', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json; charset=utf-8',
        },
        body: JSON.stringify({
            guest: true,
            session_mode: SESSION_MODE_GUEST,
            open_verification_window: false,
            wait_for_ready: false,
        }),
    });
}

async function requestGuestSnapshots(limitBackups = 20) {
    const params = new URLSearchParams({
        limit_backups: String(limitBackups),
    });
    return requestJson(`/api/chat/runtime/guest/snapshots?${params.toString()}`);
}

async function requestGuestSnapshotRestore(snapshotId) {
    return requestJson('/api/chat/runtime/guest/restore', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json; charset=utf-8',
        },
        body: JSON.stringify({
            guest: true,
            session_mode: SESSION_MODE_GUEST,
            snapshot_id: snapshotId,
            open_verification_window: false,
            wait_for_ready: false,
        }),
    });
}

function hasManualRuntimeWindow(payload) {
    const browser = payload && typeof payload === 'object' && payload.browser && typeof payload.browser === 'object'
        ? payload.browser
        : {};
    return Boolean(
        browser.manual_browser_started &&
        browser.manual_context_ready &&
        browser.manual_page_ready
    );
}

function shouldAvoidAutomaticRuntimeBootstrap() {
    if (state.sessionMode !== SESSION_MODE_GUEST) {
        return false;
    }
    const activeSnapshotId = String(state.activeGuestSnapshotId || '').trim();
    const currentConversationId = String(state.currentConversationId || '').trim();
    const visibleConversationCount = Array.isArray(state.conversations) ? state.conversations.length : 0;
    const snapshotCount = Array.isArray(state.guestSnapshots) ? state.guestSnapshots.length : 0;
    return Boolean(activeSnapshotId || currentConversationId || visibleConversationCount > 0 || snapshotCount > 0);
}

async function waitForRuntimeReady({ timeoutMs = 300000, intervalMs = 1500 } = {}) {
    const deadline = Date.now() + timeoutMs;
    let lastPayload = null;

    while (Date.now() < deadline) {
        const statusPayload = await requestRuntimeStatus();
        renderRuntimeBanner(buildRuntimeStatusPayload(statusPayload));
        if (statusPayload.ready) {
            return statusPayload;
        }
        lastPayload = statusPayload;

        if (!hasManualRuntimeWindow(statusPayload) && !shouldAvoidAutomaticRuntimeBootstrap()) {
            const bootstrapPayload = await requestRuntimeBootstrap();
            renderRuntimeBanner(normalizeRuntimePayload(bootstrapPayload));
            if (bootstrapPayload.ready) {
                return bootstrapPayload;
            }
            lastPayload = bootstrapPayload;
        }
        await new Promise((resolve) => setTimeout(resolve, intervalMs));
    }

    throw new Error(
        (lastPayload && lastPayload.message) ||
        '等待验证窗口进入可聊天状态超时，请完成验证后重试。'
    );
}

async function ensureRuntimeReadyForSend() {
    const statusPayload = await requestRuntimeStatus();
    renderRuntimeBanner(buildRuntimeStatusPayload(statusPayload));
    if (statusPayload.ready) {
        return statusPayload;
    }
    if (shouldAvoidAutomaticRuntimeBootstrap()) {
        return statusPayload;
    }
    const bootstrap = await requestRuntimeBootstrap();
    renderRuntimeBanner(normalizeRuntimePayload(bootstrap));
    if (bootstrap.ready) {
        return bootstrap;
    }
    return waitForRuntimeReady();
}

function buildRuntimeStatusPayload(data) {
    const browser = (data && data.browser) || {};
    const sessionParams = (data && data.session_params) || {};
    const guestSnapshotContext = getPreferredGuestSnapshotContext();
    const manualActive = Boolean(
        browser.manual_browser_started &&
        browser.manual_context_ready &&
        browser.manual_page_ready
    );
    const manualReady = Boolean(browser.manual_chat_ready);
    const hiddenReady = Boolean(
        browser.browser_started &&
        browser.context_ready &&
        browser.page_ready
    );
    const manualUrl = String(browser.manual_current_url || '').trim();
    const roomId = String(
        sessionParams.room_id ||
        state.currentConversationId ||
        guestSnapshotContext.conversationId ||
        ''
    ).trim();
    const statusBits = [
        `会话: ${roomId || '无'}`,
        `可见窗口: ${manualActive ? (manualReady ? '可聊天' : '已打开') : '未打开'}`,
        `隐藏运行时: ${hiddenReady ? '已就绪' : '未就绪'}`,
        `manual capture: ${Number(browser.manual_capture_count || 0)}`,
        `hidden capture: ${Number(browser.hidden_capture_count || 0)}`,
    ];
    if (manualUrl) {
        statusBits.push(`当前页: ${manualUrl}`);
    }
    return {
        ready: manualReady,
        capture_ready: false,
        manual_ready: manualReady,
        awaiting_user_action: manualActive && !manualReady,
        manual_window_opened: false,
        message: statusBits.join(' | '),
        open_url: manualUrl || null,
    };
}

function buildCompletionRequest(message) {
    const conversationId = getStableCurrentConversationId();
    const requestBody = {
        prompt: message,
        attachments: [],
        use_auto_cot: Boolean(useAutoCotCheckbox?.checked),
        use_deep_think: Boolean(useDeepThinkCheckbox?.checked),
        guest: state.sessionMode === SESSION_MODE_GUEST,
        session_mode: state.sessionMode,
        conversation_mode: getConversationMode(),
    };

    if (thinkModeSelect && thinkModeSelect.value !== '') {
        requestBody.think_mode = Number(thinkModeSelect.value);
    }
    if (conversationId) {
        requestBody.conversation_id = conversationId;
    }
    if (conversationId && state.currentSectionId) {
        requestBody.section_id = state.currentSectionId;
    }
    return requestBody;
}

async function sendCompletionRequest(requestBody) {
    return requestJson(getCompletionEndpoint(), {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json; charset=utf-8',
        },
        body: JSON.stringify(requestBody),
    });
}

function parseSseEventBlock(block) {
    const normalizedBlock = String(block || '').replace(/\r/g, '');
    if (!normalizedBlock.trim()) {
        return null;
    }
    const lines = normalizedBlock.split('\n');
    let eventName = 'message';
    const dataLines = [];
    for (const line of lines) {
        if (line.startsWith('event:')) {
            eventName = line.slice(6).trim() || 'message';
            continue;
        }
        if (line.startsWith('data:')) {
            dataLines.push(line.slice(5).trimStart());
        }
    }
    const rawData = dataLines.join('\n');
    if (!rawData && eventName !== 'message') {
        return { event: eventName, data: null };
    }
    let parsedData = rawData;
    if (rawData) {
        try {
            parsedData = JSON.parse(rawData);
        } catch (error) {
            parsedData = rawData;
        }
    }
    return {
        event: eventName,
        data: parsedData,
    };
}

async function sendCompletionRequestStream(requestBody, loadingMessageId) {
    const response = await fetch(getCompletionStreamEndpoint(), {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json; charset=utf-8',
        },
        body: JSON.stringify(requestBody),
    });

    if (!response.ok) {
        const payload = await parseJsonResponse(response).catch(() => ({}));
        throw buildRequestError(response.status, payload.detail ?? payload);
    }

    const contentType = String(response.headers.get('content-type') || '').toLowerCase();
    if (contentType && !contentType.includes('text/event-stream')) {
        const fallbackPayload = await parseJsonResponse(response).catch(() => null);
        if (fallbackPayload && typeof fallbackPayload === 'object') {
            return {
                data: fallbackPayload,
                rendered: false,
            };
        }
        throw new Error(`流式接口返回了非 SSE 内容: ${contentType}`);
    }

    if (!response.body) {
        throw new Error('当前环境不支持 SSE 流式响应。');
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder('utf-8');
    let buffer = '';
    let streamingMessageId = null;
    let accumulatedText = '';
    let accumulatedImages = [];
    let finalPayload = null;
    let streamError = null;
    let rendered = false;

    const ensureStreamingMessage = () => {
        if (!streamingMessageId) {
            removeLoadingMessage(loadingMessageId);
            streamingMessageId = addStreamingMessage('');
        }
        return streamingMessageId;
    };

    const applyStreamEvent = (eventName, data) => {
        const payload = data && typeof data === 'object' ? data : {};
        if (eventName === 'status') {
            if (payload.message) {
                updateLoadingMessage(loadingMessageId, payload.message);
            }
            return;
        }
        if (eventName === 'delta') {
            const nextText = typeof payload.text === 'string'
                ? payload.text
                : `${accumulatedText}${String(payload.delta || '')}`;
            accumulatedText = nextText;
            ensureStreamingMessage();
            updateStreamingMessage(streamingMessageId, accumulatedText, accumulatedImages);
            rendered = true;
            return;
        }
        if (eventName === 'images') {
            accumulatedImages = Array.isArray(payload.img_urls) ? payload.img_urls : accumulatedImages;
            ensureStreamingMessage();
            updateStreamingMessage(streamingMessageId, accumulatedText, accumulatedImages);
            rendered = true;
            return;
        }
        if (eventName === 'done') {
            finalPayload = payload;
            accumulatedText = typeof payload.text === 'string' ? payload.text : accumulatedText;
            accumulatedImages = Array.isArray(payload.img_urls) ? payload.img_urls : accumulatedImages;
            if (accumulatedText || accumulatedImages.length > 0) {
                ensureStreamingMessage();
                finalizeStreamingMessage(streamingMessageId, accumulatedText, accumulatedImages);
                rendered = true;
            } else if (streamingMessageId) {
                removeStreamingMessage(streamingMessageId);
                streamingMessageId = null;
            }
            return;
        }
        if (eventName === 'error') {
            streamError = buildRequestError(
                payload.status || payload.status_code || 500,
                payload.detail ?? payload,
                payload.message || ''
            );
        }
    };

    while (true) {
        const { value, done } = await reader.read();
        buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
        const normalizedBuffer = buffer.replace(/\r\n/g, '\n');
        const segments = normalizedBuffer.split('\n\n');
        buffer = segments.pop() || '';

        for (const segment of segments) {
            const parsedEvent = parseSseEventBlock(segment);
            if (!parsedEvent) {
                continue;
            }
            applyStreamEvent(parsedEvent.event, parsedEvent.data);
            if (streamError) {
                break;
            }
        }

        if (streamError || done) {
            break;
        }
    }

    if (!streamError && buffer.trim()) {
        const tailEvent = parseSseEventBlock(buffer);
        if (tailEvent) {
            applyStreamEvent(tailEvent.event, tailEvent.data);
        }
    }

    if (streamError) {
        throw streamError;
    }
    if (!finalPayload || typeof finalPayload !== 'object') {
        throw new Error('流式响应未返回完成事件。');
    }

    return {
        data: finalPayload,
        rendered,
    };
}

function isManualVerificationError(error) {
    return Boolean(
        error &&
        error.status === 409 &&
        error.detail &&
        typeof error.detail === 'object' &&
        error.detail.code === 'manual_verification_required'
    );
}

function isGuestSessionRotatedError(error) {
    return Boolean(
        error &&
        error.status === 409 &&
        error.detail &&
        typeof error.detail === 'object' &&
        error.detail.code === 'guest_session_rotated'
    );
}

function isUpstreamVerificationError(error) {
    return Boolean(
        error &&
        error.status === 429 &&
        error.detail &&
        typeof error.detail === 'object' &&
        error.detail.code === 'upstream_verification_required'
    );
}

function buildTitleFromPrompt(prompt) {
    const trimmed = String(prompt || '').trim();
    if (!trimmed) {
        return '继续对话';
    }
    return trimmed.length > 22 ? `${trimmed.slice(0, 22)}...` : trimmed;
}

function extractConversationIdFromChatUrl(url) {
    const raw = String(url || '').trim();
    if (!raw) {
        return '';
    }
    const match = raw.match(/\/chat\/([^/?#]+)/i);
    if (!match || !match[1]) {
        return '';
    }
    const conversationId = decodeURIComponent(match[1]).trim();
    if (!conversationId || conversationId === 'chat' || conversationId === '0') {
        return '';
    }
    return normalizeStableConversationId(conversationId);
}

function resolveConversationIdFromPayload(detail, { allowUrlFallback = true } = {}) {
    if (!detail || typeof detail !== 'object') {
        return '';
    }
    const explicitConversationId = normalizeStableConversationId(detail.conversation_id);
    if (explicitConversationId) {
        return explicitConversationId;
    }
    const sessionRoomId = normalizeStableConversationId(detail.session_params?.room_id);
    if (sessionRoomId) {
        return sessionRoomId;
    }
    if (!allowUrlFallback) {
        return '';
    }
    return String(
        extractConversationIdFromChatUrl(detail.manual_current_url) ||
        extractConversationIdFromChatUrl(detail.open_url) ||
        ''
    ).trim();
}

function syncConversationFromDetail(detail, prompt, { allowUrlFallback = true } = {}) {
    if (!detail || typeof detail !== 'object') {
        return false;
    }

    const conversationId = resolveConversationIdFromPayload(detail, { allowUrlFallback });
    if (!conversationId) {
        return false;
    }

    const sectionId = String(detail.section_id || '').trim() || null;
    const nextTitle = state.currentConversationId
        ? (state.currentConversationTitle || buildTitleFromPrompt(prompt))
        : buildTitleFromPrompt(prompt);

    setCurrentConversation(conversationId, sectionId, nextTitle);
    upsertConversation(conversationId, sectionId, nextTitle);
    return true;
}

function syncConversationFromRuntimePayload(
    payload,
    { allowAdoptWhenNew = false, allowSwitchConversation = false } = {},
) {
    if (!payload || typeof payload !== 'object') {
        return false;
    }

    const browser = payload.browser && typeof payload.browser === 'object' ? payload.browser : {};
    const conversationId = normalizeStableConversationId(
        extractConversationIdFromChatUrl(browser.manual_current_url) ||
        payload.session_params?.room_id
    ) || '';
    if (!conversationId) {
        return false;
    }
    const currentConversationId = String(state.currentConversationId || '').trim();
    if (!currentConversationId && !allowAdoptWhenNew) {
        return false;
    }
    if (currentConversationId && currentConversationId !== conversationId && !allowSwitchConversation) {
        return false;
    }

    const existingConversation = findConversationEntry(conversationId);
    const nextTitle = existingConversation?.title || state.currentConversationTitle || '继续对话';
    const nextSectionId = (
        (state.currentConversationId === conversationId && state.currentSectionId) ||
        existingConversation?.sectionId ||
        null
    );

    setCurrentConversation(conversationId, nextSectionId, nextTitle);
    upsertConversation(conversationId, nextSectionId, nextTitle);
    return true;
}

function hasVerificationWindowHint(detail) {
    if (!detail || typeof detail !== 'object') {
        return false;
    }
    return Boolean(
        detail.manual_window_opened ||
        detail.open_url ||
        detail.manual_current_url
    );
}

async function ensureVisibleVerificationWindowForError(detail) {
    const normalizedDetail = detail && typeof detail === 'object' ? detail : {};
    return normalizedDetail;
}

function shouldAutoContinueAfterCreate(data) {
    return Boolean(
        data &&
        typeof data === 'object' &&
        data.follow_up_required &&
        data.follow_up_conversation_mode === 'continue' &&
        normalizeStableConversationId(data.conversation_id)
    );
}

async function handleCompletionSuccess(
    prompt,
    data,
    { wasNewConversation = false, skipAssistantRender = false } = {},
) {
    const responseText = String(data.text || '');
    const responseImages = Array.isArray(data.img_urls) ? data.img_urls : [];
    if (!skipAssistantRender && (responseText || responseImages.length > 0)) {
        addMessageToChat(responseText, false, responseImages);
    }

    let resolvedConversationId = normalizeStableConversationId(data.conversation_id) || getStableCurrentConversationId() || null;
    let resolvedSectionId = resolvedConversationId ? (data.section_id || state.currentSectionId || null) : null;
    if (!resolvedConversationId && wasNewConversation) {
        try {
            const runtimePayload = await requestRuntimeStatus();
            syncConversationFromRuntimePayload(runtimePayload, {
                allowAdoptWhenNew: true,
                allowSwitchConversation: true,
            });
            resolvedConversationId = getStableCurrentConversationId() || null;
            resolvedSectionId = resolvedConversationId ? (state.currentSectionId || resolvedSectionId || null) : null;
        } catch (error) {
            console.warn('鏂颁細璇濆悗缁悓姝ヨ繍琛屾椂浼氳瘽澶辫触', error);
        }
    }
    const resolvedTitle = state.currentConversationId
        ? (state.currentConversationTitle || buildTitleFromPrompt(prompt))
        : buildTitleFromPrompt(prompt);

    setCurrentConversation(resolvedConversationId, resolvedSectionId, resolvedTitle);

    if (resolvedConversationId && canBrowseHistory()) {
        upsertConversation(resolvedConversationId, resolvedSectionId, resolvedTitle, {
            moveToFront: Boolean(wasNewConversation),
        });
        try {
            const info = await fetchConversationInfo(resolvedConversationId);
            const syncedTitle = info.name || resolvedTitle;
            const syncedSectionId = info.section_id || resolvedSectionId;
            setCurrentConversation(resolvedConversationId, syncedSectionId, syncedTitle);
            upsertConversation(resolvedConversationId, syncedSectionId, syncedTitle);
        } catch (error) {
            console.warn('同步会话信息失败', error);
        }
    }

    if (state.sessionMode === SESSION_MODE_GUEST) {
        await refreshGuestSnapshots({ silent: true });
    }
}

async function sendCompletionRequestWithRecovery(message, loadingMessageId) {
    try {
        return await sendCompletionRequest(buildCompletionRequest(message));
    } catch (error) {
        if (isUpstreamVerificationError(error)) {
            syncConversationFromDetail(error.detail, message, {
                allowUrlFallback: Boolean(state.currentConversationId),
            });
            renderRuntimeBanner(normalizeRuntimePayload(error.detail));
            updateLoadingMessage(loadingMessageId, '检测到上游滑块验证，正在准备可见验证窗口...');
            const detail = error.detail && typeof error.detail === 'object' ? error.detail : {};
            if (!detail.manual_window_opened || !detail.open_url) {
                try {
                    const startPayload = await ensureVisibleVerificationWindowForError(detail);
                    renderRuntimeBanner(normalizeRuntimePayload(startPayload));
                } catch (startError) {
                    console.warn('自动拉起验证窗口失败', startError);
                }
            }
            updateLoadingMessage(loadingMessageId, '请在可见窗口完成验证后重试。');
            const verifyError = new Error('检测到上游滑块验证，请在可见窗口完成验证后重试。');
            verifyError.status = error.status;
            verifyError.detail = error.detail;
            throw verifyError;
        }
        if (!isManualVerificationError(error) && !isGuestSessionRotatedError(error)) {
            throw error;
        }
        syncConversationFromDetail(error.detail, message, {
            allowUrlFallback: Boolean(state.currentConversationId),
        });
        renderRuntimeBanner(normalizeRuntimePayload(error.detail));
        updateLoadingMessage(loadingMessageId, '正在等待验证窗口完成准备...');
        const readyPayload = await waitForRuntimeReady();
        syncConversationFromRuntimePayload(readyPayload);
        updateLoadingMessage(loadingMessageId, '验证已完成，正在重新发送...');
        return sendCompletionRequest(buildCompletionRequest(message));
    }
}

async function sendCompletionRequestWithSnapshotAwareRecovery(message, loadingMessageId) {
    try {
        return await sendCompletionRequest(buildCompletionRequest(message));
    } catch (error) {
        if (isUpstreamVerificationError(error)) {
            syncConversationFromDetail(error.detail, message, {
                allowUrlFallback: Boolean(state.currentConversationId),
            });
            renderRuntimeBanner(normalizeRuntimePayload(error.detail));
            const detail = error.detail && typeof error.detail === 'object' ? error.detail : {};
            if (!hasVerificationWindowHint(detail)) {
                updateLoadingMessage(loadingMessageId, '请点击打开验证窗口，继续使用当前游客缓存进行验证。');
                const verifyError = new Error('当前游客缓存需要验证，请点击打开验证窗口，不会自动拉起新的游客状态。');
                verifyError.status = error.status;
                verifyError.detail = error.detail;
                throw verifyError;
            }
            updateLoadingMessage(loadingMessageId, '请在当前可见窗口完成验证后重试。');
            const readyPayload = await waitForRuntimeReady();
            syncConversationFromRuntimePayload(readyPayload);
            updateLoadingMessage(loadingMessageId, '验证已完成，正在重新发送...');
            return sendCompletionRequest(buildCompletionRequest(message));
        }
        if (!isManualVerificationError(error) && !isGuestSessionRotatedError(error)) {
            throw error;
        }
        syncConversationFromDetail(error.detail, message, {
            allowUrlFallback: Boolean(state.currentConversationId),
        });
        renderRuntimeBanner(normalizeRuntimePayload(error.detail));
        const detail = error.detail && typeof error.detail === 'object' ? error.detail : {};
        if (!hasVerificationWindowHint(detail)) {
            updateLoadingMessage(loadingMessageId, '请点击打开验证窗口，继续使用当前游客缓存进行验证。');
            const verifyError = new Error('当前游客缓存需要可见验证，请先点击打开验证窗口。');
            verifyError.status = error.status;
            verifyError.detail = error.detail;
            throw verifyError;
        }
        updateLoadingMessage(loadingMessageId, '正在等待当前验证窗口完成准备...');
        const readyPayload = await waitForRuntimeReady();
        syncConversationFromRuntimePayload(readyPayload);
        updateLoadingMessage(loadingMessageId, '验证已完成，正在重新发送...');
        return sendCompletionRequest(buildCompletionRequest(message));
    }
}

async function sendCompletionRequestStreamWithSnapshotAwareRecovery(message, loadingMessageId) {
    try {
        return await sendCompletionRequestStream(buildCompletionRequest(message), loadingMessageId);
    } catch (error) {
        if (isUpstreamVerificationError(error)) {
            syncConversationFromDetail(error.detail, message, {
                allowUrlFallback: Boolean(state.currentConversationId),
            });
            renderRuntimeBanner(normalizeRuntimePayload(error.detail));
            const detail = error.detail && typeof error.detail === 'object' ? error.detail : {};
            if (!hasVerificationWindowHint(detail)) {
                updateLoadingMessage(loadingMessageId, '请点击打开验证窗口，继续使用当前游客缓存进行验证。');
                const verifyError = new Error('当前游客缓存需要验证，请点击打开验证窗口，不会自动拉起新的游客状态。');
                verifyError.status = error.status;
                verifyError.detail = error.detail;
                throw verifyError;
            }
            updateLoadingMessage(loadingMessageId, '请在当前可见窗口完成验证后重试。');
            const readyPayload = await waitForRuntimeReady();
            syncConversationFromRuntimePayload(readyPayload);
            updateLoadingMessage(loadingMessageId, '验证已完成，正在重新发送...');
            return sendCompletionRequestStream(buildCompletionRequest(message), loadingMessageId);
        }
        if (!isManualVerificationError(error) && !isGuestSessionRotatedError(error)) {
            throw error;
        }
        syncConversationFromDetail(error.detail, message, {
            allowUrlFallback: Boolean(state.currentConversationId),
        });
        renderRuntimeBanner(normalizeRuntimePayload(error.detail));
        const detail = error.detail && typeof error.detail === 'object' ? error.detail : {};
        if (!hasVerificationWindowHint(detail)) {
            updateLoadingMessage(loadingMessageId, '请点击打开验证窗口，继续使用当前游客缓存进行验证。');
            const verifyError = new Error('当前游客缓存需要可见验证，请先点击打开验证窗口。');
            verifyError.status = error.status;
            verifyError.detail = error.detail;
            throw verifyError;
        }
        updateLoadingMessage(loadingMessageId, '正在等待当前验证窗口完成准备...');
        const readyPayload = await waitForRuntimeReady();
        syncConversationFromRuntimePayload(readyPayload);
        updateLoadingMessage(loadingMessageId, '验证已完成，正在重新发送...');
        return sendCompletionRequestStream(buildCompletionRequest(message), loadingMessageId);
    }
}

async function sendCompletionRequestWithSelectedMode(message, loadingMessageId) {
    if (shouldUseSseStream()) {
        return sendCompletionRequestStreamWithSnapshotAwareRecovery(message, loadingMessageId);
    }
    return {
        data: await sendCompletionRequestWithSnapshotAwareRecovery(message, loadingMessageId),
        rendered: false,
    };
}

async function startVisibleRuntimeFlow() {
    openRuntimeButton.disabled = true;
    runtimeRetryButton.disabled = true;
    try {
        renderRuntimeBanner({
            ready: false,
            message: '正在打开可见验证窗口...',
        });
        const startPayload = await requestManualVerificationStart({
            wait_for_ready: false,
        });
        syncConversationFromRuntimePayload(startPayload);
        renderRuntimeBanner(normalizeRuntimePayload(startPayload));
        if (state.sessionMode === SESSION_MODE_GUEST) {
            await refreshGuestSnapshots({ silent: true });
        }
        if (!startPayload.ready) {
            const readyPayload = await waitForRuntimeReady();
            syncConversationFromRuntimePayload(readyPayload);
            if (state.sessionMode === SESSION_MODE_GUEST) {
                await refreshGuestSnapshots({ silent: true });
            }
        }
    } catch (error) {
        showError(error.message);
    } finally {
        openRuntimeButton.disabled = false;
        runtimeRetryButton.disabled = false;
    }
}

async function refreshRuntimeStatus() {
    if (!refreshRuntimeStatusButton) {
        return;
    }
    refreshRuntimeStatusButton.disabled = true;
    try {
        renderRuntimeBanner({
            ready: false,
            message: '正在刷新运行时状态...',
        });
        const payload = await requestRuntimeStatus();
        syncConversationFromRuntimePayload(payload);
        renderRuntimeBanner(buildRuntimeStatusPayload(payload));
        if (state.sessionMode === SESSION_MODE_GUEST) {
            await refreshGuestSnapshots({ silent: true });
        }
    } catch (error) {
        showError(error.message);
    } finally {
        refreshRuntimeStatusButton.disabled = false;
    }
}

async function resetGuestSession() {
    if (state.sessionMode !== SESSION_MODE_GUEST) {
        showError('Only guest mode can rotate to a fresh guest session.');
        return;
    }

    if (resetGuestButton) {
        resetGuestButton.disabled = true;
    }
    try {
        renderRuntimeBanner({
            ready: false,
            message: 'Backing up the current guest session and opening a fresh verification window...',
        });
        const data = await requestGuestSessionReset();
        createNewChat();
        renderRuntimeBanner({
            ready: Boolean(data.ready),
            message: data.message || 'Guest session has been rotated.',
        });
        await refreshGuestSnapshots({ silent: true });
        syncConversationFromRuntimePayload(data);
    } catch (error) {
        showError(error.message);
    } finally {
        if (resetGuestButton) {
            resetGuestButton.disabled = false;
        }
    }
}

async function sendMessage() {
    const message = messageInput.value.trim();
    if (!message || state.isLoading) {
        return;
    }
    const wasNewConversation = getConversationMode() === 'new';

    state.isLoading = true;
    messageInput.disabled = true;
    sendButton.disabled = true;

    addMessageToChat(message, true);
    messageInput.value = '';
    messageInput.style.height = 'auto';

    const loadingMessageId = addLoadingMessage('正在检查运行时状态...');

    try {
        await ensureRuntimeReadyForSend();
        updateLoadingMessage(
            loadingMessageId,
            shouldUseSseStream() ? '正在建立 SSE 连接并等待返回...' : '正在发送并等待返回...'
        );

        let completionResult = await sendCompletionRequestWithSelectedMode(message, loadingMessageId);
        let data = completionResult.data;
        let skipAssistantRender = Boolean(completionResult.rendered);
        let autoContinued = false;
        if (shouldAutoContinueAfterCreate(data)) {
            syncConversationFromDetail(data, message, { allowUrlFallback: false });
            autoContinued = true;
            updateLoadingMessage(loadingMessageId, '新会话已创建，正在自动续聊...');
            completionResult = await sendCompletionRequestWithSelectedMode(message, loadingMessageId);
            data = completionResult.data;
            skipAssistantRender = Boolean(skipAssistantRender || completionResult.rendered);
        }
        if (shouldAutoContinueAfterCreate(data)) {
            syncConversationFromDetail(data, message, { allowUrlFallback: false });
            throw new Error(
                autoContinued
                    ? '新会话已创建，但自动续聊后仍未拿到正文，请重试。'
                    : '新会话已创建，正在等待前端续聊处理，请重试。'
            );
        }

        removeLoadingMessage(loadingMessageId);
        renderRuntimeBanner({
            ready: true,
            message: '本次发送已完成，运行时 capture 也已经同步到当前缓存。',
        });
        await handleCompletionSuccess(message, data, {
            wasNewConversation,
            skipAssistantRender,
        });
    } catch (error) {
        removeLoadingMessage(loadingMessageId);
        showError(error.message || '发送失败');
    } finally {
        state.isLoading = false;
        messageInput.disabled = false;
        sendButton.disabled = false;
        messageInput.focus();
        updateDeleteButtonState();
    }
}

function restoreInitialConversation() {
    syncVisibleConversationList();
    const savedState = loadStoredConversationState(state.sessionMode);
    const savedConversationId = savedState.conversationId;
    const savedSectionId = savedState.sectionId;
    const savedTitle = savedState.title || '新对话';

    if (!savedConversationId) {
        createNewChat();
        return;
    }

    const cachedConversation = state.conversations.find((item) => item.id === savedConversationId);
    if (cachedConversation) {
        selectConversation(
            cachedConversation.id,
            cachedConversation.sectionId || savedSectionId,
            cachedConversation.title || savedTitle,
        );
        return;
    }

    if (state.sessionMode === SESSION_MODE_GUEST) {
        const activeSnapshotConversation = state.conversations[0] || null;
        if (activeSnapshotConversation) {
            selectConversation(
                activeSnapshotConversation.id,
                activeSnapshotConversation.sectionId || savedSectionId,
                activeSnapshotConversation.title || savedTitle,
            );
            return;
        }
        createNewChat();
        return;
    }

    setCurrentConversation(savedConversationId, savedSectionId, savedTitle);
    resetChatToWelcome();
}

async function applySessionModeChange(mode) {
    state.sessionMode = normalizeSessionMode(mode);
    saveSessionMode();
    if (state.sessionMode === SESSION_MODE_GUEST) {
        await refreshGuestSnapshots({ silent: true });
    }
    updateSessionModeUI();
    restoreInitialConversation();
}

function ensureResetGuestButton() {
    if (resetGuestButton || !sidebarActions) {
        return;
    }
    const button = document.createElement('button');
    button.id = 'reset-guest-btn';
    button.type = 'button';
    button.className = 'secondary-btn';
    button.innerHTML = '<i class="bi bi-arrow-repeat"></i> Refresh guest';
    sidebarActions.appendChild(button);
    resetGuestButton = button;
}

sendButton.addEventListener('click', sendMessage);
messageInput.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey) {
        event.preventDefault();
        sendMessage();
    }
});
messageInput.addEventListener('input', () => {
    messageInput.style.height = 'auto';
    messageInput.style.height = `${messageInput.scrollHeight}px`;
});

newChatButton.addEventListener('click', createNewChat);
deleteChatButton.addEventListener('click', deleteCurrentChat);
openRuntimeButton.addEventListener('click', startVisibleRuntimeFlow);
if (refreshRuntimeStatusButton) {
    refreshRuntimeStatusButton.addEventListener('click', refreshRuntimeStatus);
}
ensureResetGuestButton();
if (resetGuestButton) {
    resetGuestButton.addEventListener('click', resetGuestSession);
}
if (refreshGuestSnapshotsButton) {
    refreshGuestSnapshotsButton.addEventListener('click', () => {
        refreshGuestSnapshots().catch((error) => showError(error.message));
    });
}
runtimeRetryButton.addEventListener('click', startVisibleRuntimeFlow);
runtimeHideButton.addEventListener('click', () => renderRuntimeBanner(null, { hidden: true }));

sessionModeSelect.addEventListener('change', () => {
    applySessionModeChange(sessionModeSelect.value).catch((error) => {
        showError(error.message);
    });
});

if (thinkModeSelect) {
    thinkModeSelect.addEventListener('change', saveThinkMode);
}
if (useAutoCotCheckbox) {
    useAutoCotCheckbox.addEventListener('change', saveThinkingToggles);
}
if (useDeepThinkCheckbox) {
    useDeepThinkCheckbox.addEventListener('change', saveThinkingToggles);
}
if (useSseStreamCheckbox) {
    useSseStreamCheckbox.addEventListener('change', saveSseStreamPreference);
}
if (exportContextButton) {
    exportContextButton.addEventListener('click', exportCurrentConversationContext);
}

document.addEventListener('DOMContentLoaded', async () => {
    hydrateSettings();
    updateConversationMetrics();
    if (state.sessionMode === SESSION_MODE_GUEST) {
        await refreshGuestSnapshots({ silent: true });
    }
    updateSessionModeUI();
    restoreInitialConversation();
});
