<template>
  <div class="ai-chat-container">
    <van-nav-bar title="新闻 Agent" right-text="新对话" @click-right="newChat" fixed />

    <div class="chat-content">
      <section v-if="pendingActions.length" class="pending-actions" aria-label="待确认的新闻采集操作">
        <h3>待确认操作</h3>
        <article v-for="action in pendingActions" :key="action.id" class="pending-action">
          <div>
            <strong>{{ action.summary }}</strong>
            <p v-if="action.status === 'pending'">确认后才会执行，提案将在 {{ formatExpiry(action.expires_at) }} 过期。</p>
            <p v-else-if="action.status === 'executing'">操作正在执行…</p>
            <p v-else>{{ action.resultSummary || action.status }}</p>
          </div>
          <van-button
            v-if="action.status === 'pending'"
            size="small"
            type="primary"
            :loading="confirmingActionId === action.id"
            @click="confirmAction(action)"
          >确认执行</van-button>
        </article>
      </section>
      <div class="session-switcher">
        <span>会话</span>
        <select v-model="sessionId" @change="switchSession">
          <option value="">新对话</option>
          <option v-for="session in sessions" :key="session.id" :value="session.id">
            {{ session.title }}
          </option>
        </select>
      </div>
      <div class="messages-container" ref="messagesContainer">
        <div
          v-for="(message, index) in messages"
          :key="index"
          :class="['message', message.role === 'user' ? 'user-message' : 'ai-message']"
        >
          <div class="message-content">
            <div v-if="message.role === 'assistant' && message.content === ''" class="typing-indicator">
              <span></span><span></span><span></span>
            </div>
            <div v-else v-html="formatMessage(message.content)"></div>
            <div v-if="message.citations?.length" class="citations">
              <span>来源：</span>
              <span v-for="citation in message.citations" :key="citation.news_id" class="citation">
                [{{ citation.news_id }}] {{ citation.title }}
              </span>
            </div>
          </div>
        </div>
      </div>

      <div v-if="activeTool" class="tool-status">正在检索：{{ activeTool }}</div>

      <div class="input-container">
        <van-field
          v-model="userInput"
          rows="1"
          autosize
          type="textarea"
          placeholder="例如：总结最近的人工智能新闻"
          class="chat-input"
          @keypress.enter.prevent="sendMessage"
        />
        <van-button
          v-if="isLoading"
          type="warning"
          class="send-button"
          @click="cancelRequest"
        >
          停止生成
        </van-button>
        <van-button
          v-else
          type="primary"
          class="send-button"
          :disabled="!userInput.trim()"
          @click="sendMessage"
        >
          发送
        </van-button>
      </div>
    </div>

    <tab-bar />
  </div>
</template>

<script setup>
import { nextTick, onMounted, ref, watch } from 'vue';
import { showToast } from 'vant';
import { marked } from 'marked';
import DOMPurify from 'dompurify';
import TabBar from '../components/TabBar.vue';
import { apiConfig } from '../config/api';
import { authenticatedFetch } from '../utils/httpAuth';
import { useUserStore } from '../store/user';

const userStore = useUserStore();
const messages = ref([
  {
    role: 'assistant',
    content: '你好，我是新闻 Agent。你可以让我检索、总结和比较新闻。',
    citations: [],
  },
]);
const userInput = ref('');
const sessionId = ref(null);
const sessions = ref([]);
const messagesContainer = ref(null);
const isLoading = ref(false);
const activeTool = ref('');
const pendingActions = ref([]);
const confirmingActionId = ref('');
let activeController = null;
const welcomeMessage = {
  role: 'assistant',
  content: '你好，我是新闻 Agent。你可以让我检索、总结和比较新闻。',
  citations: [],
};

const formatMessage = (content) => {
  if (!content) return '';
  return DOMPurify.sanitize(marked.parse(content));
};

const scrollToBottom = () => {
  if (messagesContainer.value) {
    messagesContainer.value.scrollTop = messagesContainer.value.scrollHeight;
  }
};

const parseSseFrame = (frame) => {
  const lines = frame.split('\n');
  const event = lines.find((line) => line.startsWith('event:'))?.slice(6).trim() || 'message';
  const data = lines.find((line) => line.startsWith('data:'))?.slice(5).trim();
  if (!data) return null;
  try {
    return { event, payload: JSON.parse(data) };
  } catch {
    return null;
  }
};

const consumeStream = async (response, assistantMessage) => {
  const reader = response.body?.getReader();
  if (!reader) throw new Error('服务器没有返回流式数据');

  const decoder = new TextDecoder();
  let buffer = '';
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const frames = buffer.split('\n\n');
    buffer = frames.pop() || '';
    for (const frame of frames) {
      const parsed = parseSseFrame(frame);
      if (!parsed) continue;
      const { event, payload } = parsed;
      if (event === 'session') sessionId.value = payload.session_id;
      if (event === 'tool_started') activeTool.value = payload.tool || '新闻';
      if (event === 'tool_finished') activeTool.value = '';
      if (event === 'token') {
        assistantMessage.content += payload.content || '';
        await nextTick();
        scrollToBottom();
      }
      if (event === 'citations') assistantMessage.citations = payload.items || [];
      if (event === 'action_proposed' && payload.id) {
        // 提案事件只更新确认卡片；实际操作必须由管理员主动点击确认。
        if (!pendingActions.value.some((item) => item.id === payload.id)) {
          pendingActions.value.unshift({ ...payload, status: 'pending' });
        }
        await refreshPendingActions();
      }
      if (event === 'error') throw new Error(payload.message || 'Agent 执行失败');
    }
  }
};

const refreshPendingActions = async () => {
  if (!userStore.token) return;
  try {
    // 普通用户没有此权限，因此列表加载失败时不影响普通新闻问答。
    const response = await authenticatedFetch(apiConfig.baseURL + '/api/agent/actions/pending', {
      headers: { Authorization: 'Bearer ' + userStore.token },
    });
    if (!response.ok) return;
    const payload = await response.json();
    const finished = pendingActions.value.filter((item) => item.status !== 'pending');
    pendingActions.value = [...(payload.data || []), ...finished].slice(0, 20);
  } catch {
    // The list is admin-only; regular users do not need this panel.
  }
};

const actionResultText = (result) => {
  if (result?.error) return `执行失败：${result.error}`;
  if (Number.isInteger(result?.entry_count)) return `订阅测试成功，发现 ${result.entry_count} 条新闻。`;
  if (Number.isInteger(result?.source_id)) return `采集源已添加（ID ${result.source_id}）。`;
  if (Number.isInteger(result?.created)) return `采集完成：新增 ${result.created} 条，跳过 ${result.skipped} 条，失败 ${result.failed} 个来源。`;
  return '操作已完成。';
};

const confirmAction = async (action) => {
  // 前端避免重复点击；服务端仍会通过原子状态更新防止并发重复执行。
  if (!userStore.token || action.status !== 'pending' || confirmingActionId.value) return;
  confirmingActionId.value = action.id;
  action.status = 'executing';
  try {
    const response = await authenticatedFetch(
      apiConfig.baseURL + '/api/agent/actions/' + encodeURIComponent(action.id) + '/confirm',
      {
        method: 'POST',
        headers: { Authorization: 'Bearer ' + userStore.token },
      },
    );
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.detail || '确认操作失败');
    action.status = payload.data?.status || 'completed';
    action.resultSummary = actionResultText(payload.data?.result);
    showToast(action.resultSummary);
    await refreshPendingActions();
  } catch (error) {
    action.status = 'failed';
    action.resultSummary = error.message || '确认操作失败';
    showToast(action.resultSummary);
  } finally {
    confirmingActionId.value = '';
  }
};

const formatExpiry = (value) => value ? new Date(value).toLocaleTimeString() : '稍后';

const refreshSessions = async () => {
  if (!userStore.token) return;
  const response = await authenticatedFetch(apiConfig.baseURL + '/api/agent/sessions?limit=50', {
    headers: { Authorization: 'Bearer ' + userStore.token },
  });
  if (!response.ok) return;
  const payload = await response.json();
  sessions.value = payload.data || [];
};

const switchSession = async () => {
  if (!sessionId.value) {
    newChat();
    return;
  }
  const response = await authenticatedFetch(
    apiConfig.baseURL + '/api/agent/sessions/' + sessionId.value + '/messages',
    { headers: { Authorization: 'Bearer ' + userStore.token } },
  );
  if (!response.ok) {
    showToast('无法读取该会话');
    return;
  }
  const payload = await response.json();
  messages.value = payload.data?.messages?.length
    ? payload.data.messages.map((message) => ({
        role: message.role,
        content: message.content,
        citations: message.citations || [],
      }))
    : [{ ...welcomeMessage }];
  await nextTick();
  scrollToBottom();
};

const newChat = () => {
  activeController?.abort();
  sessionId.value = null;
  messages.value = [{ ...welcomeMessage }];
  userInput.value = '';
  activeTool.value = '';
};

const cancelRequest = () => activeController?.abort();

const sendMessage = async () => {
  const content = userInput.value.trim();
  if (!content || isLoading.value) return;
  if (!userStore.token) {
    showToast('请先登录');
    return;
  }

  messages.value.push({ role: 'user', content, citations: [] });
  userInput.value = '';
  const assistantMessage = { role: 'assistant', content: '', citations: [] };
  messages.value.push(assistantMessage);
  isLoading.value = true;
  activeTool.value = '';
  activeController = new AbortController();
  await nextTick();
  scrollToBottom();

  try {
    const response = await authenticatedFetch(apiConfig.baseURL + '/api/agent/chat', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: 'Bearer ' + userStore.token,
        Accept: 'text/event-stream',
      },
      signal: activeController.signal,
      body: JSON.stringify({
        message: content,
        session_id: sessionId.value,
      }),
    });
    if (!response.ok) {
      const detail = await response.json().catch(() => ({}));
      throw new Error(detail.detail || '请求失败（' + response.status + '）');
    }
    await consumeStream(response, assistantMessage);
    await refreshSessions();
    if (!assistantMessage.content) assistantMessage.content = '没有生成可展示的回答。';
  } catch (error) {
    if (error.name === 'AbortError') {
      assistantMessage.content = assistantMessage.content
        ? `${assistantMessage.content}\n\n生成已停止。`
        : '生成已停止。';
    } else {
      assistantMessage.content = error.message || 'Agent 请求失败，请稍后重试。';
    }
  } finally {
    activeController = null;
    isLoading.value = false;
    activeTool.value = '';
    await nextTick();
    scrollToBottom();
  }
};

watch(messages, () => nextTick(scrollToBottom), { deep: true });
onMounted(async () => {
  await Promise.all([refreshSessions(), refreshPendingActions()]);
  if (sessions.value.length) {
    sessionId.value = sessions.value[0].id;
    await switchSession();
  } else {
    scrollToBottom();
  }
});
</script>

<style scoped>
.ai-chat-container {
  display: flex;
  flex-direction: column;
  height: 100vh;
  padding-top: 46px;
  padding-bottom: 50px;
  box-sizing: border-box;
}

.chat-content { flex: 1; display: flex; flex-direction: column; overflow: hidden; }
.pending-actions { max-height: 180px; overflow: auto; padding: 8px 12px; background: #fffaeb; border-bottom: 1px solid #fedf89; }
.pending-actions h3 { margin: 0 0 6px; color: #93370d; font-size: 13px; }
.pending-action { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 8px 0; border-top: 1px solid #f5d08a; }
.pending-action strong { font-size: 13px; line-height: 1.4; }
.pending-action p { margin: 4px 0 0; color: #7a2e0e; font-size: 12px; }
.session-switcher { display: flex; align-items: center; gap: 8px; padding: 6px 12px; color: #666; font-size: 12px; }
.session-switcher select { flex: 1; min-width: 0; padding: 5px; border: 1px solid #ddd; border-radius: 5px; background: white; }
.messages-container { flex: 1; overflow-y: auto; padding: 10px; }
.message { max-width: 85%; margin-bottom: 10px; }
.user-message { margin-left: auto; }
.ai-message { margin-right: auto; }
.message-content { padding: 10px; border-radius: 10px; word-break: break-word; }
.user-message .message-content { background: #007aff; color: white; }
.ai-message .message-content { background: #f2f2f2; color: #333; }
.input-container { display: flex; padding: 10px; border-top: 1px solid #eee; background: #fff; }
.chat-input { flex: 1; margin-right: 10px; }
.send-button { align-self: flex-end; }
.tool-status { padding: 4px 12px; color: #666; font-size: 12px; }
.citations { margin-top: 8px; color: #666; font-size: 12px; }
.citation { display: block; margin-top: 3px; }
.typing-indicator { display: flex; padding: 5px; }
.typing-indicator span { width: 8px; height: 8px; margin: 0 2px; background: #999; border-radius: 50%; }
:deep(pre) { overflow-x: auto; padding: 10px; background: #f0f0f0; border-radius: 4px; }
:deep(code) { padding: 2px 4px; background: #f0f0f0; border-radius: 4px; }
</style>
