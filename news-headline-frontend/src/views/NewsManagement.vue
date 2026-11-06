<template>
  <div class="management-page">
    <header class="page-header">
      <div>
        <p class="eyebrow">ADMIN WORKSPACE</p>
        <h1>新闻采集与内容管理</h1>
        <p class="subtitle">维护 RSS 来源，审核采集草稿并发布新闻。</p>
      </div>
      <button class="back-button" @click="router.push('/my')">返回个人中心</button>
    </header>

    <nav class="section-tabs">
      <button :class="{ active: section === 'feeds' }" @click="section = 'feeds'">采集源</button>
      <button :class="{ active: section === 'content' }" @click="section = 'content'">内容审核</button>
      <button :class="{ active: section === 'logs' }" @click="section = 'logs'; refreshReviewData()">自动审核记录</button>
    </nav>

    <section v-if="section === 'feeds'" class="panel">
      <div class="panel-heading">
        <div>
          <h2>RSS / Atom 采集源</h2>
          <p>新来源默认停用；测试并审核后再启用。定时采集与自动发布需要分别授权。</p>
        </div>
      </div>

      <form class="feed-form" @submit.prevent="createFeed">
        <label>来源类型
          <select v-model="feedForm.sourceType" required>
            <option value="official_original">官方原始来源</option>
            <option value="independent_media">独立媒体</option>
            <option value="syndicated">转载稿</option>
            <option value="other">其他 / 未确认</option>
          </select>
        </label>
        <label>稿源分组<input v-model.trim="feedForm.publisherGroup" maxlength="120" placeholder="例如：publisher-example" /></label>
        <label>来源名称<input v-model.trim="feedForm.name" required maxlength="100" placeholder="例如：科技新闻 RSS" /></label>
        <label class="url-field">RSS / Atom 地址<input v-model.trim="feedForm.url" type="url" required maxlength="1000" placeholder="https://example.com/feed.xml" /></label>
        <label>新闻分类
          <select v-model.number="feedForm.categoryId" required>
            <option v-for="category in categories" :key="category.id" :value="category.id">{{ category.name }}</option>
          </select>
        </label>
        <label class="checkbox-setting">
          <input v-model="feedForm.autoCollectEnabled" type="checkbox" /> 启用定时采集（来源启用后生效）
        </label>
        <label>采集间隔（分钟）
          <input v-model.number="feedForm.autoCollectIntervalMinutes" type="number" min="5" max="1440" required />
        </label>
        <div class="feed-form-actions">
          <button class="primary-button" :disabled="busy">{{ feedForm.id ? '保存修改' : '添加来源' }}</button>
          <button v-if="feedForm.id" type="button" @click="resetFeedForm">取消</button>
        </div>
      </form>

      <div v-if="feeds.length" class="feed-list">
        <article v-for="feed in feeds" :key="feed.id" class="feed-card">
          <div class="feed-details">
            <div class="feed-title-row">
              <h3>{{ feed.name }}</h3>
              <span class="pill" :class="feed.isActive ? 'pill-green' : 'pill-gray'">{{ feed.isActive ? '启用' : '停用' }}</span>
            </div>
            <a :href="feed.url" target="_blank" rel="noopener noreferrer">{{ feed.url }}</a>
            <p>来源谱系：{{ sourceTypeLabel(feed.sourceType) }} · 稿源分组：{{ feed.publisherGroup || '未登记' }}</p>
            <p>分类：{{ categoryName(feed.categoryId) }} · 最近采集：{{ formatDate(feed.lastFetchedAt) }}</p>
            <p>最近测试：{{ formatDate(feed.lastTestedAt) }}</p>
            <p v-if="feed.lastError" class="error-text">上次采集失败：{{ feed.lastError }}</p>
            <p v-if="feed.lastTestError" class="error-text">上次测试失败：{{ feed.lastTestError }}</p>
            <div class="automation-settings">
              <label class="checkbox-setting">
                <input
                  :checked="feed.autoCollectEnabled"
                  type="checkbox"
                  :disabled="busy || !feed.isActive"
                  @change="updateFeedAutomation(feed, 'autoCollectEnabled', $event.target.checked)"
                /> 定时采集
              </label>
              <label class="checkbox-setting">
                <input
                  :checked="feed.trustedForAutoPublish"
                  type="checkbox"
                  :disabled="busy || !feed.isActive"
                  @change="updateFeedAutomation(feed, 'trustedForAutoPublish', $event.target.checked)"
                /> 可信来源（允许申请自动发布）
              </label>
            </div>
          </div>
          <div class="feed-actions">
            <button @click="testFeed(feed)" :disabled="busy">测试 RSS</button>
            <button @click="collectFeed(feed)" :disabled="busy || !feed.isActive">立即采集</button>
            <button @click="editFeed(feed)" :disabled="busy">编辑</button>
            <button @click="toggleFeed(feed)" :disabled="busy">{{ feed.isActive ? '停用' : '启用' }}</button>
            <button class="danger-button" @click="deleteFeed(feed)" :disabled="busy">删除</button>
          </div>
        </article>
        <p v-if="!feeds.length" class="empty-state">还没有采集源。</p>
      </div>
      <p v-else class="empty-state">还没有采集源，添加 RSS 或 Atom 地址开始采集。</p>

      <section class="automation-policy">
        <div>
          <h3>自动发布分类白名单</h3>
          <p>只有新启用的分类、可信来源及全部审核门禁同时通过，文章才会自动发布。</p>
        </div>
        <label v-for="category in autoPublishCategories" :key="category.id" class="checkbox-setting">
          <input
            :checked="category.autoPublishEnabled"
            type="checkbox"
            :disabled="busy"
            @change="toggleAutoPublishCategory(category, $event.target.checked)"
          /> {{ category.name }}
        </label>
      </section>
    </section>

    <section v-else-if="section === 'content'" class="panel">
      <div class="panel-heading">
        <div>
          <h2>新闻内容</h2>
          <p>编辑已发布新闻会自动转为草稿，重新审核后再发布。</p>
        </div>
        <button class="primary-button" @click="createDraft">新建新闻草稿</button>
      </div>
      <form class="content-filters" @submit.prevent="loadContent(1)">
        <input v-model.trim="searchText" placeholder="搜索标题或摘要" maxlength="100" />
        <select v-model="statusFilter" @change="loadContent(1)">
          <option value="">全部状态</option>
          <option value="draft">待审核</option>
          <option value="published">已发布</option>
          <option value="archived">已归档</option>
        </select>
        <button class="primary-button" :disabled="busy">搜索</button>
      </form>

      <div v-if="contentItems.length" class="content-list">
        <article v-for="item in contentItems" :key="item.id" class="content-card">
          <div class="content-main">
            <div class="content-title-row">
              <h3>{{ item.title }}</h3>
              <span class="pill" :class="statusClass(item.status)">{{ statusLabel(item.status) }}</span>
            </div>
            <p>{{ item.description || item.content }}</p>
            <small>{{ item.author || '未署名' }} · {{ categoryName(item.categoryId) }} · {{ formatDate(item.publishTime) }}</small>
          </div>
          <div class="content-actions">
            <button @click="openEditor(item)">编辑</button>
            <button v-if="item.status !== 'published'" class="primary-button" @click="changeStatus(item, 'published')">发布</button>
            <button v-if="item.status === 'published'" @click="changeStatus(item, 'draft')">下架</button>
            <button v-if="item.status !== 'archived'" class="danger-button" @click="changeStatus(item, 'archived')">归档</button>
            <button v-else @click="changeStatus(item, 'draft')">恢复草稿</button>
          </div>
        </article>
      </div>
      <p v-else class="empty-state">没有符合条件的新闻。</p>

      <div class="pagination">
        <button :disabled="page <= 1 || busy" @click="loadContent(page - 1)">上一页</button>
        <span>第 {{ page }} 页 · 共 {{ total }} 条</span>
        <button :disabled="page * pageSize >= total || busy" @click="loadContent(page + 1)">下一页</button>
      </div>
    </section>

    <section v-else class="panel">
      <div class="panel-heading">
        <div>
          <h2>自动审核与发布记录</h2>
          <p>查看自动审核结论、风险拦截原因与发布时间；管理员可以撤回自动发布的新闻。</p>
        </div>
        <button :disabled="busy" @click="refreshReviewData">刷新记录</button>
      </div>
      <p class="mode-note">当前模式：{{ autoReviewEvaluation.mode === 'enforce' ? '执行发布' : '影子审核，仅记录候选，不发布稿件' }}</p>
      <div class="evaluation-grid">
        <article><small>误放率</small><strong>{{ formatPercent(autoReviewEvaluation.falseAllowRate) }}</strong><span>{{ autoReviewEvaluation.falseAllowCount || 0 }} / {{ autoReviewEvaluation.autoCandidateSamples || 0 }} 自动放行样本</span></article>
        <article><small>误拦率</small><strong>{{ formatPercent(autoReviewEvaluation.falseBlockRate) }}</strong><span>{{ autoReviewEvaluation.falseBlockCount || 0 }} / {{ autoReviewEvaluation.manualReviewSamples || 0 }} 人工复核样本</span></article>
        <article><small>证据引用准确率</small><strong>{{ formatPercent(autoReviewEvaluation.evidenceCitationAccuracy) }}</strong><span>{{ autoReviewEvaluation.evidenceCitationSamples || 0 }} 个已标注样本</span></article>
        <article><small>人工标注数</small><strong>{{ autoReviewEvaluation.feedbackCount || 0 }}</strong><span>管理员可更新自己的标注</span></article>
      </div>
      <div v-if="autoPublishLogs.length" class="content-list">
        <article v-for="log in autoPublishLogs" :key="log.id" class="content-card">
          <div class="content-main">
            <div class="content-title-row">
              <h3>{{ log.title }}</h3>
              <span v-if="log.decision !== 'shadow_pass'" class="pill" :class="log.withdrawnAt ? 'pill-gray' : (log.decision === 'auto_published' ? 'pill-green' : 'pill-amber')">
                {{ reviewDecisionLabel(log) }}
              </span>
              <span v-else class="pill pill-amber">影子候选 · 未发布</span>
            </div>
            <p>{{ log.sourceName }} · {{ log.categoryName }} · 最近审核：{{ formatDate(log.lastAttemptAt) }}</p>
            <p v-if="log.publishedAt">自动发布时间：{{ formatDate(log.publishedAt) }}</p>
            <a v-if="log.sourceUrl" :href="log.sourceUrl" target="_blank" rel="noopener noreferrer">查看来源原文</a>
            <small v-if="log.withdrawnAt">撤回时间：{{ formatDate(log.withdrawnAt) }} · {{ log.withdrawReason }}</small>
            <small v-if="log.reasons?.length">审核结论：{{ formatAutoReviewReasons(log.reasons) }}</small>
            <small v-if="log.reviewResult?.suggestions?.factual_check">
              事实核查：{{ log.reviewResult.suggestions.factual_check.status }}；引用新闻 ID：{{ log.reviewResult.suggestions.factual_check.evidence_news_ids.join('、') || '无' }}
            </small>
          </div>
          <div class="content-actions">
            <form v-if="log.decision !== 'review_error'" class="feedback-form" @submit.prevent="saveReviewFeedback(log)">
              <label>建议处理
                <select v-model="feedbackForms[log.id].label">
                  <option value="accepted">采纳建议</option>
                  <option value="rejected">驳回建议</option>
                  <option value="modified">修改后采纳</option>
                </select>
              </label>
              <label>稿件结论
                <select v-model="feedbackForms[log.id].safeToPublish">
                  <option value="true">可发布</option>
                  <option value="false">不应发布</option>
                </select>
              </label>
              <label>证据引用
                <select v-model="feedbackForms[log.id].evidenceAccurate">
                  <option value="true">准确</option>
                  <option value="false">不准确</option>
                </select>
              </label>
              <label class="feedback-reason">理由<textarea v-model.trim="feedbackForms[log.id].reason" required maxlength="1000" rows="2" placeholder="说明采纳、驳回或修改理由" /></label>
              <button class="primary-button" :disabled="busy">保存人工标注</button>
            </form>
            <button
              v-if="log.decision === 'auto_published' && !log.withdrawnAt && log.status === 'published'"
              class="danger-button"
              :disabled="busy || withdrawingLogId === log.id"
              @click="withdrawAutoPublished(log)"
            >{{ withdrawingLogId === log.id ? '正在撤回…' : '撤回发布' }}</button>
          </div>
        </article>
      </div>
      <p v-else class="empty-state">还没有自动审核记录。</p>
    </section>

    <div v-if="editing" class="modal-backdrop" @click.self="editing = null">
      <form class="editor-modal" @submit.prevent="saveContent">
        <div class="modal-heading">
          <div><p class="eyebrow">{{ editing.id ? `NEWS #${editing.id}` : 'NEW DRAFT' }}</p><h2>{{ editing.id ? '编辑新闻' : '新建新闻' }}</h2></div>
          <button type="button" class="close-button" @click="editing = null">×</button>
        </div>
        <label>标题<input v-model.trim="editing.title" required maxlength="255" /></label>
        <label>摘要<textarea v-model="editing.description" rows="2" maxlength="500" /></label>
        <label>正文<textarea v-model="editing.content" rows="10" required /></label>
        <div class="editor-grid">
          <label>作者<input v-model="editing.author" maxlength="50" /></label>
          <label>分类<select v-model.number="editing.categoryId" required><option v-for="category in categories" :key="category.id" :value="category.id">{{ category.name }}</option></select></label>
        </div>
        <label>封面图片 URL<input v-model="editing.image" maxlength="255" /></label>
        <div class="review-toolbar">
          <button type="button" :disabled="busy || reviewing" @click="runEditorialReview">
            {{ reviewing ? '正在审核…' : 'AI 内容审核建议' }}
          </button>
          <span>只生成建议，不会修改稿件或自动发布。</span>
        </div>
        <section v-if="reviewResult" class="review-panel">
          <div class="review-heading">
            <strong>{{ reviewResult.suggestions.recommendation === 'suggest_publish' ? '建议进入人工发布审核' : '需要人工复核' }}</strong>
            <span :class="reviewResult.ai_status === 'available' ? 'review-ok' : 'review-warning'">
              {{ reviewResult.ai_status === 'available' ? 'AI 分析完成' : 'AI 暂不可用，显示规则检查结果' }}
            </span>
          </div>
          <p v-if="reviewResult.suggestions.suggested_title"><b>建议标题：</b>{{ reviewResult.suggestions.suggested_title }}</p>
          <p v-if="reviewResult.suggestions.suggested_summary"><b>建议摘要：</b>{{ reviewResult.suggestions.suggested_summary }}</p>
          <p v-if="reviewResult.suggestions.suggested_category_name"><b>建议分类：</b>{{ reviewResult.suggestions.suggested_category_name }}</p>
          <p v-if="reviewResult.suggestions.suggested_tags?.length"><b>建议标签：</b>{{ reviewResult.suggestions.suggested_tags.join('、') }}</p>
          <p><b>事实引用：</b>{{ factualStatusLabel(reviewResult.suggestions.factual_check.status) }}
            <span v-if="reviewResult.suggestions.factual_check.evidence_news_ids.length">（新闻 ID：{{ reviewResult.suggestions.factual_check.evidence_news_ids.join('、') }}）</span>
          </p>
          <p v-if="reviewResult.suggestions.risk_flags?.length"><b>模型风险提示：</b>{{ reviewResult.suggestions.risk_flags.join('、') }}</p>
          <div v-if="reviewResult.findings.duplicates.length" class="review-finding">
            <b>可能重复：</b><span v-for="item in reviewResult.findings.duplicates" :key="item.news_id">#{{ item.news_id }} {{ item.title }}（{{ item.match }}） </span>
          </div>
          <div v-if="reviewResult.findings.suspicious_links.length" class="review-finding">
            <b>异常链接：</b><span v-for="item in reviewResult.findings.suspicious_links" :key="item.url">{{ item.url }}（{{ item.reason }}） </span>
          </div>
          <div v-if="reviewResult.findings.sensitive_or_ad_flags.length" class="review-finding">
            <b>规则提醒：</b>{{ reviewResult.findings.sensitive_or_ad_flags.join('、') }}
          </div>
          <div v-if="reviewResult.findings.evidence.length" class="review-evidence">
            <b>匹配到的已发布新闻：</b><span v-for="item in reviewResult.findings.evidence" :key="item.news_id">#{{ item.news_id }} {{ item.title }}；</span>
          </div>
          <small>建议仅供编辑参考。请人工核实事实和修改稿件，发布操作仍需管理员确认。</small>
          <div v-if="reviewResult.suggestions.factual_check.claims?.length" class="claim-list">
            <b>逐条事实主张（仅与站内已发布新闻比对）</b>
            <article v-for="(claim, index) in reviewResult.suggestions.factual_check.claims" :key="`${index}-${claim.claim}`" class="claim-card">
              <strong>{{ claimStatusLabel(claim.status) }} · {{ claim.importance === 'key' ? '关键主张' : '一般主张' }}</strong>
              <p>{{ claim.claim }}</p>
              <small>独立来源数：{{ claim.independent_source_count }}<span v-if="claim.requires_more_independent_sources">；关键主张需要至少两个不同稿源支持</span></small>
              <div v-for="citation in claim.evidence" :key="`${claim.claim}-${citation.news_id}`" class="claim-citation">
                <a v-if="citation.source_url" :href="citation.source_url" target="_blank" rel="noopener noreferrer">{{ citation.source_name }} · {{ citation.publish_time || '发布时间未知' }}</a>
                <span v-else>{{ citation.source_name }} · {{ citation.publish_time || '发布时间未知' }}</span>
                <small>{{ citation.quote }}</small>
              </div>
            </article>
          </div>
        </section>
        <div class="modal-actions">
          <button type="button" @click="editing = null">取消</button>
          <button class="primary-button" :disabled="busy">保存为草稿</button>
        </div>
      </form>
    </div>
  </div>
</template>

<script setup>
import { onMounted, reactive, ref } from 'vue'
import axios from 'axios'
import { showToast } from 'vant'
import { useRouter } from 'vue-router'
import { apiConfig } from '../config/api'
import { useUserStore } from '../store/user'

const router = useRouter()
const userStore = useUserStore()
const section = ref('feeds')
const busy = ref(false)
const categories = ref([])
const feeds = ref([])
const contentItems = ref([])
const searchText = ref('')
const statusFilter = ref('draft')
const page = ref(1)
const pageSize = 20
const total = ref(0)
const editing = ref(null)
const reviewing = ref(false)
const reviewResult = ref(null)
const autoPublishCategories = ref([])
const autoPublishLogs = ref([])
const autoReviewEvaluation = ref({})
const feedbackForms = reactive({})
const withdrawingLogId = ref(null)
const feedForm = reactive({
  id: null,
  name: '',
  url: '',
  sourceType: 'other',
  publisherGroup: '',
  categoryId: null,
  autoCollectEnabled: false,
  autoCollectIntervalMinutes: 60,
})

const callApi = async (method, path, data, params) => {
  const response = await axios({
    method,
    url: `${apiConfig.baseURL}/api/admin${path}`,
    data,
    params,
    headers: { Authorization: `Bearer ${userStore.getToken}` }
  })
  if (!response.data || response.data.code !== 200) {
    throw new Error(response.data?.message || '请求失败')
  }
  return response.data.data
}

const showError = (error) => showToast(error.response?.data?.detail || error.message || '操作失败')

const loadFeeds = async () => {
  feeds.value = await callApi('get', '/feeds')
}

const loadCategories = async () => {
  const response = await axios.get(`${apiConfig.baseURL}/api/news/categories`)
  categories.value = response.data?.data || []
  if (!feedForm.categoryId && categories.value.length) feedForm.categoryId = categories.value[0].id
}

const loadAutoPublishCategories = async () => {
  autoPublishCategories.value = await callApi('get', '/auto-publish/categories')
}

const loadAutoPublishLogs = async () => {
  autoPublishLogs.value = await callApi('get', '/auto-publish/logs', undefined, { limit: 100 })
  autoPublishLogs.value.forEach((log) => {
    feedbackForms[log.id] = {
      label: log.feedback?.label || 'modified',
      safeToPublish: String(log.feedback?.safeToPublish ?? true),
      evidenceAccurate: String(log.feedback?.evidenceAccurate ?? false),
      reason: log.feedback?.reason || '',
    }
  })
}

const loadAutoReviewEvaluation = async () => {
  autoReviewEvaluation.value = await callApi('get', '/auto-publish/evaluation')
}

const refreshReviewData = async () => {
  await Promise.all([loadAutoPublishLogs(), loadAutoReviewEvaluation()])
}

const createFeed = async () => {
  busy.value = true
  try {
    const data = {
      name: feedForm.name,
      url: feedForm.url,
      sourceType: feedForm.sourceType,
      publisherGroup: feedForm.publisherGroup || null,
      categoryId: feedForm.categoryId,
      autoCollectEnabled: feedForm.autoCollectEnabled,
      autoCollectIntervalMinutes: feedForm.autoCollectIntervalMinutes,
    }
    if (feedForm.id) await callApi('patch', `/feeds/${feedForm.id}`, data)
    else await callApi('post', '/feeds', data)
    resetFeedForm()
    await loadFeeds()
    showToast('采集源已保存')
  } catch (error) { showError(error) } finally { busy.value = false }
}

const editFeed = (feed) => {
  feedForm.id = feed.id
  feedForm.name = feed.name
  feedForm.url = feed.url
  feedForm.sourceType = feed.sourceType || 'other'
  feedForm.publisherGroup = feed.publisherGroup || ''
  feedForm.categoryId = feed.categoryId
  feedForm.autoCollectEnabled = feed.autoCollectEnabled
  feedForm.autoCollectIntervalMinutes = feed.autoCollectIntervalMinutes
  window.scrollTo({ top: 0, behavior: 'smooth' })
}

const resetFeedForm = () => {
  feedForm.id = null
  feedForm.name = ''
  feedForm.url = ''
  feedForm.sourceType = 'other'
  feedForm.publisherGroup = ''
  feedForm.categoryId = categories.value[0]?.id || null
  feedForm.autoCollectEnabled = false
  feedForm.autoCollectIntervalMinutes = 60
}

const updateFeedAutomation = async (feed, field, enabled) => {
  busy.value = true
  try {
    await callApi('patch', `/feeds/${feed.id}`, { [field]: enabled })
    await loadFeeds()
  } catch (error) { showError(error) } finally { busy.value = false }
}

const testFeed = async (feed) => {
  busy.value = true
  try {
    const result = await callApi('post', `/feeds/${feed.id}/test`)
    showToast(`RSS 测试成功：发现 ${result.entryCount} 条新闻`)
    await loadFeeds()
  } catch (error) { showError(error); await loadFeeds() } finally { busy.value = false }
}

const toggleAutoPublishCategory = async (category, enabled) => {
  busy.value = true
  try {
    await callApi('put', `/auto-publish/categories/${category.id}`, { enabled })
    await loadAutoPublishCategories()
  } catch (error) { showError(error) } finally { busy.value = false }
}

const withdrawAutoPublished = async (log) => {
  if (!window.confirm(`确认撤回自动发布的新闻《${log.title}》？`)) return
  withdrawingLogId.value = log.id
  busy.value = true
  try {
    await callApi('post', `/auto-publish/logs/${log.id}/withdraw`, { reason: '管理员在审计记录中撤回' })
    await Promise.all([loadAutoPublishLogs(), loadContent(1)])
    showToast('新闻已撤回并归档')
  } catch (error) { showError(error) } finally {
    withdrawingLogId.value = null
    busy.value = false
  }
}

const saveReviewFeedback = async (log) => {
  const form = feedbackForms[log.id]
  if (!form?.reason?.trim()) return showToast('请填写人工标注理由')
  busy.value = true
  try {
    await callApi('post', `/auto-publish/logs/${log.id}/feedback`, {
      label: form.label,
      safeToPublish: form.safeToPublish === 'true',
      evidenceAccurate: form.evidenceAccurate === 'true',
      reason: form.reason.trim(),
    })
    await refreshReviewData()
    showToast('人工标注已保存')
  } catch (error) { showError(error) } finally { busy.value = false }
}

const toggleFeed = async (feed) => {
  busy.value = true
  try {
    await callApi('patch', `/feeds/${feed.id}`, { isActive: !feed.isActive })
    await loadFeeds()
  } catch (error) { showError(error) } finally { busy.value = false }
}

const collectFeed = async (feed) => {
  busy.value = true
  try {
    const result = await callApi('post', `/feeds/${feed.id}/collect`)
    showToast(`采集完成：新增 ${result.created} 条，重复 ${result.skipped} 条`)
    await Promise.all([loadFeeds(), loadContent(1)])
  } catch (error) { showError(error) } finally { busy.value = false }
}

const deleteFeed = async (feed) => {
  if (!window.confirm(`删除采集源“${feed.name}”？已采集的新闻会保留。`)) return
  busy.value = true
  try {
    await callApi('delete', `/feeds/${feed.id}`)
    await loadFeeds()
    showToast('采集源已删除')
  } catch (error) { showError(error) } finally { busy.value = false }
}

const loadContent = async (targetPage = page.value) => {
  page.value = targetPage
  busy.value = true
  try {
    const params = { page: targetPage, pageSize }
    if (statusFilter.value) params.status = statusFilter.value
    if (searchText.value) params.q = searchText.value
    const result = await callApi('get', '/content', undefined, params)
    contentItems.value = result.items
    total.value = result.total
  } catch (error) { showError(error) } finally { busy.value = false }
}

const openEditor = (item) => { editing.value = { ...item }; reviewResult.value = null }

const createDraft = () => {
  reviewResult.value = null
  editing.value = {
    id: null,
    title: '',
    description: '',
    content: '',
    image: '',
    author: '',
    categoryId: categories.value[0]?.id || null
  }
}

const runEditorialReview = async () => {
  if (!editing.value) return
  reviewing.value = true
  try {
    // 仅请求辅助建议并展示结果；保存和发布仍由编辑者、管理员分别操作。
    reviewResult.value = await callApi('post', '/content/review-assist', {
      newsId: editing.value.id || undefined,
      title: editing.value.title,
      description: editing.value.description,
      content: editing.value.content,
      categoryId: editing.value.categoryId,
      sourceUrl: editing.value.sourceUrl,
    })
  } catch (error) { showError(error) } finally { reviewing.value = false }
}

const saveContent = async () => {
  if (!editing.value) return
  busy.value = true
  try {
    const item = editing.value
    const content = {
      title: item.title,
      description: item.description,
      content: item.content,
      image: item.image,
      author: item.author,
      categoryId: item.categoryId
    }
    if (item.id) await callApi('patch', `/content/${item.id}`, content)
    else await callApi('post', '/content', content)
    editing.value = null
    statusFilter.value = 'draft'
    await loadContent(1)
    showToast('内容已保存到待审核草稿')
  } catch (error) { showError(error) } finally { busy.value = false }
}

const changeStatus = async (item, status) => {
  const action = status === 'published' ? '发布' : status === 'archived' ? '归档' : '下架'
  if (!window.confirm(`确定${action}《${item.title}》？`)) return
  busy.value = true
  try {
    await callApi('patch', `/content/${item.id}/status`, { status })
    await loadContent()
    showToast(`新闻已${action}`)
  } catch (error) { showError(error) } finally { busy.value = false }
}

const categoryName = (id) => categories.value.find((item) => item.id === id)?.name || `分类 ${id}`
const formatDate = (value) => value ? new Date(value).toLocaleString() : '暂无'
// 将自动审核的固定风险码转换为管理员可读的中文说明。
const formatAutoReviewReasons = (reasons = []) => {
  const labels = {
    article_not_draft: '稿件状态已变化',
    source_inactive: 'RSS 来源已停用',
    automatic_collection_disabled: '来源未启用定时采集',
    source_not_trusted: '来源未获自动发布信任',
    article_predates_source_approval: '稿件早于来源授权时间',
    category_not_allowlisted: '分类未加入自动发布白名单',
    article_too_short: '稿件内容过短',
    missing_source_url: '缺少来源链接',
    ai_review_unavailable: 'AI 审核暂不可用',
    model_recommends_manual_review: 'AI 建议人工复核',
    category_suggestion_mismatch: '模型建议分类与稿件分类不一致',
    insufficient_published_news_evidence: '缺少已发布新闻库的事实依据',
    claim_evidence_incomplete: '逐条事实证据不完整',
    key_claim_lacks_independent_sources: '关键主张缺少独立来源支持',
    source_provenance_unverified: '来源谱系未核验',
    publisher_group_missing: '缺少稿源分组',
    high_impact_casualty: '涉及伤亡高风险内容',
    public_safety: '涉及公共安全内容',
    major_policy: '涉及重大政策内容',
    financial_content: '涉及金融内容',
    evidence_conflict: '站内证据存在矛盾',
    shadow_mode_no_publication: '影子审核模式只记录、不发布',
    sensitive_content_keyword: '命中敏感内容规则',
    possible_advertising: '疑似广告内容',
    suspicious_links: '来源中存在异常链接',
    model_flagged_risk: 'AI 标记了内容风险',
    possible_duplicate: '疑似重复稿件',
    suspicious_link: '检测到异常链接',
    sensitive_or_advertising_content: '疑似敏感或广告内容',
    review_execution_error: '审核执行异常，等待重试或人工处理',
  }
  return reasons.map((reason) => labels[reason] || reason).join('、')
}
const statusLabel = (status) => ({ draft: '待审核', published: '已发布', archived: '已归档' }[status] || status)
const statusClass = (status) => ({ draft: 'pill-amber', published: 'pill-green', archived: 'pill-gray' }[status] || 'pill-gray')

const formatPercent = (value) => value === null || value === undefined ? '样本不足' : `${(value * 100).toFixed(1)}%`
const sourceTypeLabel = (type) => ({ official_original: '官方原始来源', independent_media: '独立媒体', syndicated: '转载稿', other: '其他 / 未确认' }[type] || '其他 / 未确认')
const claimStatusLabel = (status) => ({ supported: '有站内证据支持', contradicted: '与站内证据矛盾', insufficient: '证据不足' }[status] || '证据不足')
const factualStatusLabel = (status) => ({ supported: '站内证据支持', contradicted: '与站内证据矛盾', unsupported: '未找到站内证据', uncertain: '站内证据不足' }[status] || '站内证据不足')
const reviewDecisionLabel = (log) => {
  if (log.withdrawnAt) return '已撤回'
  if (log.decision === 'shadow_pass') return '影子候选 · 未发布'
  if (log.decision === 'auto_published') return log.status === 'published' ? '已自动发布' : '当前未发布'
  if (log.decision === 'review_error') return '审核异常，待人工处理'
  return '待人工处理'
}
const reviewDecisionClass = (log) => log.withdrawnAt ? 'pill-gray' : (log.decision === 'auto_published' ? 'pill-green' : 'pill-amber')

onMounted(async () => {
  busy.value = true
  try {
    await Promise.all([loadCategories(), loadFeeds(), loadContent(1)])
    await Promise.all([loadAutoPublishCategories(), refreshReviewData()])
  } catch (error) { showError(error) } finally { busy.value = false }
})
</script>

<style scoped>
.management-page { min-height: 100vh; padding: 32px max(20px, calc((100vw - 1180px) / 2)); color: #182230; background: #f4f6fa; }
.page-header, .panel-heading, .feed-title-row, .content-title-row, .modal-heading, .modal-actions, .pagination { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
.page-header { margin-bottom: 24px; }
.eyebrow { margin: 0 0 6px; color: #4275d6; font-size: 11px; font-weight: 800; letter-spacing: .12em; }
h1, h2, h3, p { margin-top: 0; }
h1 { margin-bottom: 6px; font-size: 28px; }
h2 { margin-bottom: 6px; font-size: 20px; }
h3 { margin-bottom: 6px; font-size: 16px; }
.subtitle, .panel-heading p { margin-bottom: 0; color: #667085; }
button, input, textarea, select { font: inherit; }
button { min-height: 38px; padding: 8px 13px; color: #344054; background: #fff; border: 1px solid #d0d5dd; border-radius: 8px; cursor: pointer; }
button:disabled { opacity: .55; cursor: wait; }
.primary-button { color: #fff; background: #2563eb; border-color: #2563eb; }
.danger-button { color: #b42318; }
.back-button { white-space: nowrap; }
.section-tabs { display: flex; gap: 8px; margin-bottom: 16px; }
.section-tabs button { border: 0; background: transparent; }
.section-tabs button.active { color: #1d4ed8; background: #e8efff; font-weight: 700; }
.panel { padding: 24px; background: #fff; border: 1px solid #e4e7ec; border-radius: 14px; box-shadow: 0 4px 16px #1018280a; }
.panel-heading { margin-bottom: 20px; }
.feed-form, .content-filters { display: flex; align-items: end; gap: 12px; margin-bottom: 20px; }
.feed-form-actions { display: flex; gap: 8px; }
label { display: flex; flex: 1; flex-direction: column; gap: 6px; color: #475467; font-size: 13px; font-weight: 600; }
input, textarea, select { width: 100%; padding: 10px 11px; color: #182230; background: #fff; border: 1px solid #d0d5dd; border-radius: 8px; outline: none; }
input:focus, textarea:focus, select:focus { border-color: #528bff; box-shadow: 0 0 0 3px #2e90fa1a; }
.url-field { flex: 2; }
.feed-list, .content-list { display: grid; gap: 12px; }
.feed-card, .content-card { display: flex; align-items: center; justify-content: space-between; gap: 20px; padding: 16px; border: 1px solid #eaecf0; border-radius: 10px; }
.feed-details, .content-main { min-width: 0; flex: 1; }
.feed-details a { display: block; overflow: hidden; color: #2563eb; font-size: 13px; text-overflow: ellipsis; white-space: nowrap; }
.feed-details p, .content-main p { margin: 8px 0 0; color: #667085; font-size: 13px; }
.automation-settings, .automation-policy { display: flex; flex-wrap: wrap; gap: 12px; margin-top: 12px; }
.automation-policy { align-items: center; justify-content: space-between; padding: 18px; background: #f8faff; border: 1px solid #d6e4ff; border-radius: 10px; }
.automation-policy h3 { margin: 0; }
.automation-policy p { margin: 5px 0 0; color: #667085; font-size: 13px; }
.checkbox-setting { display: inline-flex; align-items: center; gap: 7px; color: #344054; font-size: 13px; }
.checkbox-setting input { width: auto; }
.content-main p { display: -webkit-box; overflow: hidden; -webkit-box-orient: vertical; -webkit-line-clamp: 2; }
.content-main small { display: block; margin-top: 9px; color: #98a2b3; }
.feed-actions, .content-actions { display: flex; flex-wrap: wrap; justify-content: end; gap: 8px; }
.pill { display: inline-flex; flex-shrink: 0; padding: 4px 9px; border-radius: 999px; font-size: 12px; }
.pill-green { color: #067647; background: #ecfdf3; }
.pill-amber { color: #b54708; background: #fffaeb; }
.pill-gray { color: #475467; background: #f2f4f7; }
.error-text { color: #b42318 !important; }
.empty-state { padding: 30px 10px; color: #98a2b3; text-align: center; }
.pagination { justify-content: center; margin-top: 20px; color: #667085; }
.pagination span { min-width: 140px; text-align: center; }
.modal-backdrop { position: fixed; z-index: 2000; inset: 0; display: grid; overflow: auto; padding: 24px; background: #10182880; place-items: center; }
.editor-modal { width: min(720px, 100%); max-height: calc(100vh - 48px); overflow: auto; padding: 24px; background: #fff; border-radius: 14px; box-shadow: 0 20px 50px #10182840; }
.modal-heading { margin-bottom: 18px; }
.modal-heading h2 { margin-bottom: 0; }
.close-button { width: 38px; padding: 0; font-size: 26px; }
.editor-modal > label { margin-bottom: 14px; }
.editor-grid { display: flex; gap: 12px; margin-bottom: 14px; }
.modal-actions { justify-content: end; margin-top: 18px; }
.review-toolbar { display: flex; align-items: center; gap: 12px; margin-top: 16px; }
.review-toolbar span, .review-panel small { color: #667085; font-size: 12px; }
.review-panel { display: grid; gap: 8px; margin-top: 14px; padding: 16px; background: #f8faff; border: 1px solid #d6e4ff; border-radius: 10px; }
.review-panel p { margin: 0; color: #344054; line-height: 1.5; }
.review-heading { display: flex; justify-content: space-between; gap: 12px; }
.review-ok { color: #067647; }
.review-warning, .review-finding { color: #b54708; }
.review-finding, .review-evidence { font-size: 13px; line-height: 1.5; overflow-wrap: anywhere; }
.review-evidence { color: #475467; }
.claim-list { display: grid; gap: 10px; margin-top: 8px; }
.claim-card { display: grid; gap: 6px; padding: 12px; background: #fff; border: 1px solid #eaecf0; border-radius: 8px; }
.claim-card p, .claim-card small { margin: 0; }
.claim-card > strong { color: #344054; }
.claim-citation { display: grid; gap: 3px; padding-top: 5px; border-top: 1px solid #eaecf0; }
.claim-citation a { color: #2563eb; overflow-wrap: anywhere; }
.claim-citation small { color: #667085; }
.mode-note { margin: 0 0 12px; color: #475467; font-weight: 700; }
.evaluation-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; margin-bottom: 18px; }
.evaluation-grid article { display: grid; gap: 5px; padding: 14px; background: #f8faff; border: 1px solid #d6e4ff; border-radius: 10px; }
.evaluation-grid small, .evaluation-grid span { color: #667085; font-size: 12px; }
.evaluation-grid strong { color: #182230; font-size: 23px; }
.feedback-form { display: grid; grid-template-columns: repeat(3, minmax(110px, 1fr)); gap: 8px; width: min(520px, 100%); }
.feedback-form label { font-size: 11px; }
.feedback-form select, .feedback-form textarea { padding: 7px; font-size: 12px; }
.feedback-reason { grid-column: 1 / -1; }
@media (max-width: 760px) {
  .management-page { padding: 20px 14px 40px; }
  .page-header { align-items: start; }
  h1 { font-size: 22px; }
  .back-button { padding: 7px 9px; font-size: 12px; }
  .panel { padding: 16px; }
  .feed-form, .content-filters, .feed-card, .content-card { align-items: stretch; flex-direction: column; }
  .feed-actions, .content-actions { justify-content: start; }
  .feed-actions button, .content-actions button { flex: 1; }
  .editor-grid { flex-direction: column; }
  .evaluation-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .feedback-form { grid-template-columns: 1fr; }
}
</style>
