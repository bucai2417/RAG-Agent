<template>
  <PageContainer :title="kb?.name || '知识库'" :subtitle="kb?.description || undefined">
    <template #extra>
      <el-button :icon="ArrowLeft" @click="router.push('/kb')">返回列表</el-button>
      <el-button type="primary" :icon="Edit" :disabled="!kb" @click="openEditKb">编辑知识库</el-button>
    </template>

    <!-- 概览 -->
    <div v-if="kb" class="app-card panel" style="margin-bottom: 16px">
      <div class="kv-row">
        <span class="kb-tag" v-if="kb.source_type === 'entry'">图文知识库</span>
        <span class="kb-tag purple" v-if="isExternal">外部知识库</span>
        <span class="kb-tag orange" v-if="isKeyword && !isEntry">纯关键词</span>
      </div>
      <el-descriptions v-if="isExternal" :column="4" size="small" border>
        <el-descriptions-item label="来源">外部知识库</el-descriptions-item>
        <el-descriptions-item label="连接器">{{ kb.connector_kind || '-' }}</el-descriptions-item>
        <el-descriptions-item label="可见性">{{ visLabel(kb.visibility) }}</el-descriptions-item>
        <el-descriptions-item label="检索方式">实时联邦检索</el-descriptions-item>
      </el-descriptions>
      <el-descriptions v-else :column="6" size="small" border>
        <el-descriptions-item label="可见性">{{ visLabel(kb.visibility) }}</el-descriptions-item>
        <el-descriptions-item label="索引方式">
          <el-tag size="small" :type="isKeyword ? 'warning' : 'primary'">{{ isKeyword ? '纯关键词' : '向量 + 关键词' }}</el-tag>
        </el-descriptions-item>
        <el-descriptions-item v-if="!isKeyword" label="向量模型">
          <el-select
            v-if="canManage"
            size="small"
            style="min-width: 160px"
            :model-value="kb.embedding_model_id ?? 0"
            @change="(v: number) => setKbEmbedding(v === 0 ? null : v)"
          >
            <el-option :value="0" label="租户默认" />
            <el-option v-for="m in embModels" :key="m.id" :value="m.id" :label="m.display_name" />
          </el-select>
          <span v-else>{{ embModelLabel }}</span>
        </el-descriptions-item>
        <el-descriptions-item label="分块策略">{{ (kb as any).chunk_strategy?.type || 'parent_child' }}</el-descriptions-item>
        <el-descriptions-item label="文档数">{{ kb.doc_count }}</el-descriptions-item>
        <el-descriptions-item label="分块数">{{ kb.chunk_count }}</el-descriptions-item>
      </el-descriptions>
    </div>

    <el-tabs v-model="activeTab" @tab-change="onTabChange">
      <!-- Tab：连接信息（外部库） -->
      <el-tab-pane v-if="isExternal" label="连接信息" name="connector">
        <div class="app-card panel">
          <div class="panel-head">
            <span class="panel-title">外部知识库连接</span>
            <el-button size="small" :icon="Refresh" @click="loadConnector">刷新</el-button>
          </div>
          <el-descriptions :column="2" size="small" border style="margin-bottom: 16px">
            <el-descriptions-item label="连接器类型">{{ connector?.connector_kind || kb?.connector_kind || '-' }}</el-descriptions-item>
            <el-descriptions-item label="API Key">{{ connector?.config?.api_key_set ? '已配置' : '未配置' }}</el-descriptions-item>
            <el-descriptions-item v-for="(v, k) in connectorConfig" :key="k" :label="k">
              <span class="mono-xs">{{ typeof v === 'object' ? JSON.stringify(v) : String(v) }}</span>
            </el-descriptions-item>
          </el-descriptions>

          <div class="toolbar" style="margin-bottom: 12px">
            <el-input v-model="connQuery" placeholder="测试查询词" :prefix-icon="Search" style="width: 240px" />
            <el-button type="primary" :loading="connTestLoading" @click="testConnector">测试检索</el-button>
          </div>
          <div v-if="connTestResult" class="test-list">
            <el-empty v-if="!connTestResult.items.length" description="连接成功但无返回，请检查响应映射配置" :image-size="60" />
            <div v-for="(it, i) in connTestResult.items" :key="i" class="test-item">
              <div class="test-head">
                <el-tag size="small" type="success">{{ it.title || '（无标题）' }}</el-tag>
                <el-tag v-if="it.score > 0" size="small">score {{ it.score }}</el-tag>
                <a v-if="it.source_uri" :href="it.source_uri" target="_blank" rel="noreferrer">原文</a>
              </div>
              <p class="test-body">{{ it.content }}</p>
            </div>
          </div>
          <el-alert
            type="info"
            :closable="false"
            show-icon
            style="margin-top: 12px"
            title="该知识库在检索时实时调用外部系统，结果与本地库一起排序融合；修改连接配置请在「编辑知识库」中进行。"
          />

          <el-divider content-position="left">导入同步</el-divider>
          <el-alert
            type="warning"
            :closable="false"
            show-icon
            style="margin-bottom: 12px"
            title="实时检索依赖远端可用与响应速度；「导入同步」把远端内容一次性拉取并落本地索引，之后检索走本地、更快更稳，且可与本地内容一起重排。重复同步按来源去重，不会产生重复文档。"
          />
          <el-descriptions v-if="syncSt" :column="3" size="small" style="margin-bottom: 12px">
            <el-descriptions-item label="已同步文档">{{ syncSt.synced_doc_count }}</el-descriptions-item>
            <el-descriptions-item label="上次同步">{{ syncSt.last_at ? fmtTime(syncSt.last_at) : '从未' }}</el-descriptions-item>
            <el-descriptions-item label="上次状态">
              <el-tag v-if="syncSt.last_status === 'success'" size="small" type="success">成功（新增 {{ syncSt.last_count ?? 0 }}）</el-tag>
              <el-tag v-else-if="syncSt.last_status === 'failed'" size="small" type="danger">失败</el-tag>
              <el-tag v-else size="small">未同步</el-tag>
            </el-descriptions-item>
            <el-descriptions-item v-if="syncSt.last_error" label="错误" :span="3">
              <span class="err-text">{{ syncSt.last_error }}</span>
            </el-descriptions-item>
          </el-descriptions>
          <div class="toolbar">
            <el-button type="primary" :icon="Refresh" :loading="syncing" :disabled="!canManage" @click="doSync">立即同步</el-button>
            <el-button :icon="Setting" :disabled="!canManage" @click="openSyncConfig">同步配置</el-button>
          </div>
        </div>
      </el-tab-pane>

      <!-- Tab：文档管理 / 图文条目 -->
      <el-tab-pane v-if="!isExternal" :label="docsTabLabel" name="docs">
        <div class="app-card panel">
          <div class="panel-head">
            <div class="toolbar wrap">
              <template v-if="!isEntry">
                <el-select v-model="folderFilter" size="small" style="width: 160px">
                  <el-option value="all" label="全部文档" />
                  <el-option value="root" label="未分类" />
                  <el-option v-for="f in folders" :key="f.id" :value="f.id" :label="`📁 ${f.name}`" />
                </el-select>
                <el-button size="small" :icon="FolderAdd" :disabled="!canWrite" @click="addFolder">新建文件夹</el-button>
                <template v-if="typeof folderFilter === 'number' && canWrite">
                  <el-button size="small" :icon="Edit" @click="renameCurrentFolder">重命名</el-button>
                  <el-button size="small" :icon="ArrowUp" title="上移" @click="moveFolderOrder(folderFilter as number, -1)" />
                  <el-button size="small" :icon="ArrowDown" title="下移" @click="moveFolderOrder(folderFilter as number, 1)" />
                  <el-popconfirm title="删除该文件夹？文档会移回未分类" @confirm="removeFolder(folderFilter as number)">
                    <template #reference><el-button size="small" type="danger" :icon="Delete">删除文件夹</el-button></template>
                  </el-popconfirm>
                </template>
              </template>

              <template v-if="selectedDocIds.length && canWrite">
                <el-popconfirm :title="`删除选中的 ${selectedDocIds.length} 个文档？`" @confirm="batchDelete">
                  <template #reference>
                    <el-button size="small" type="danger">批量删除（{{ selectedDocIds.length }}）</el-button>
                  </template>
                </el-popconfirm>
                <el-select
                  :model-value="undefined"
                  size="small"
                  placeholder="批量移到"
                  style="width: 130px"
                  @change="onBatchMove"
                >
                  <el-option :value="0" label="未分类" />
                  <el-option v-for="f in folders" :key="f.id" :value="f.id" :label="f.name" />
                </el-select>
                <el-select
                  :model-value="undefined"
                  size="small"
                  placeholder="批量可见性"
                  style="width: 130px"
                  @change="onBatchVisibility"
                >
                  <el-option value="inherit" label="继承知识库" />
                  <el-option value="public" label="公开" />
                  <el-option value="restricted" label="受限" />
                </el-select>
                <el-popconfirm :title="`重新处理选中的 ${selectedDocIds.length} 个文档？`" @confirm="batchReprocess">
                  <template #reference><el-button size="small" :icon="Refresh">批量重灌</el-button></template>
                </el-popconfirm>
              </template>

              <el-dropdown v-if="selectedDocIds.length" trigger="click" @command="mergeExport">
                <el-button size="small" :icon="Download">合并导出（{{ selectedDocIds.length }}）</el-button>
                <template #dropdown>
                  <el-dropdown-menu>
                    <el-dropdown-item command="docx">合并导出 Word</el-dropdown-item>
                    <el-dropdown-item command="pdf">合并导出 PDF</el-dropdown-item>
                    <el-dropdown-item command="md">合并导出 Markdown</el-dropdown-item>
                    <el-dropdown-item command="txt">合并导出 TXT</el-dropdown-item>
                  </el-dropdown-menu>
                </template>
              </el-dropdown>
            </div>

            <div v-if="canWrite" class="toolbar">
              <el-button v-if="isEntry" type="primary" :icon="Plus" @click="openEntry()">添加图文条目</el-button>
              <template v-else>
                <el-upload :before-upload="beforeUpload" :show-file-list="false" multiple>
                  <el-button type="primary" :icon="Upload">上传文档</el-button>
                </el-upload>
                <el-upload :before-upload="beforeUpload" :show-file-list="false" multiple :directory="true" :accept="''">
                  <el-button :icon="FolderOpened">上传文件夹</el-button>
                </el-upload>
              </template>
            </div>
          </div>

          <!-- 拖拽上传区 -->
          <div v-if="canWrite && !isEntry" class="drop-zone" @dragover.prevent @drop.prevent="onDrop">
            <el-icon class="dz-ico"><UploadFilled /></el-icon>
            <div class="dz-text">拖拽文件到这里上传，或点上方「上传文档 / 上传文件夹」</div>
          </div>

          <el-alert
            v-if="isKeyword && !isEntry"
            type="info"
            :closable="false"
            show-icon
            style="margin-bottom: 12px"
            title="本库为「纯关键词」索引，不需要向量模型：文档以原文分块存入，检索靠 BM25 关键词匹配。适合术语/编号/短条目；如需更强语义检索，可在「编辑知识库」改为「向量+关键词」。"
          />

          <el-table
            v-loading="loading"
            :data="filteredDocs"
            row-key="id"
            @selection-change="onSelChange"
          >
            <el-table-column type="selection" width="44" />
            <el-table-column label="标题" min-width="200" show-overflow-tooltip>
              <template #default="{ row }">
                <span class="row-title">
                  <el-tag v-if="row.kind === 'entry'" size="small" type="success">图文条目</el-tag>
                  {{ row.title }}
                </span>
              </template>
            </el-table-column>
            <el-table-column label="类型" width="80">
              <template #default="{ row }">
                <el-tag v-if="row.kind === 'entry'" size="small" type="success">条目</el-tag>
                <el-tag v-else size="small" type="info">{{ row.file_ext || '-' }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="状态" width="180">
              <template #default="{ row }">
                <div class="status-cell">
                  <el-tag size="small" :type="statusTag(row.status)">{{ statusLabelOf(row.status) }}</el-tag>
                  <el-progress
                    v-if="!['ready', 'failed'].includes(row.status)"
                    :percentage="row.progress || 0"
                    :stroke-width="6"
                    :show-text="false"
                  />
                  <el-link
                    v-if="row.error_msg || row.error_detail"
                    type="primary"
                    :underline="false"
                    style="font-size: 11px"
                    @click="errDoc = row"
                  >查看详情</el-link>
                </div>
              </template>
            </el-table-column>
            <el-table-column label="标签" width="170">
              <template #default="{ row }">
                <div class="tag-cell" @click="openTags(row)">
                  <template v-if="(row.tags || []).length">
                    <el-tag v-for="t in row.tags" :key="t" size="small" type="primary" class="tag-sm">{{ t }}</el-tag>
                  </template>
                  <span v-else class="muted-xs"><el-icon><PriceTag /></el-icon> 添加</span>
                </div>
              </template>
            </el-table-column>
            <template v-if="!isEntry">
              <el-table-column label="可见性" width="130">
                <template #default="{ row }">
                  <el-select
                    size="small"
                    style="width: 116px"
                    :model-value="row.visibility || 'inherit'"
                    @change="(v: string) => setVisibility(row, v)"
                  >
                    <el-option value="inherit" label="继承知识库" />
                    <el-option value="public" label="公开" />
                    <el-option value="restricted" label="受限" />
                  </el-select>
                </template>
              </el-table-column>
              <el-table-column label="分块" width="70" prop="chunk_count" />
              <el-table-column label="文件夹" width="140">
                <template #default="{ row }">
                  <el-select
                    size="small"
                    style="width: 124px"
                    placeholder="未分类"
                    :model-value="row.folder_id || 0"
                    @change="(v: number) => moveDoc(row.id, v === 0 ? null : v)"
                  >
                    <el-option :value="0" label="未分类" />
                    <el-option v-for="f in folders" :key="f.id" :value="f.id" :label="f.name" />
                  </el-select>
                </template>
              </el-table-column>
            </template>
            <el-table-column label="大小" width="90">
              <template #default="{ row }">{{ humanSize(row.file_size || 0) }}</template>
            </el-table-column>
            <el-table-column label="创建时间" width="160">
              <template #default="{ row }"><span class="muted-xs">{{ fmtTime(row.created_at) }}</span></template>
            </el-table-column>
            <el-table-column label="操作" width="100" align="center" fixed="right">
              <template #default="{ row }">
                <div class="ops">
                  <el-tooltip :content="isEntry ? '编辑条目' : '在线预览'" placement="top">
                    <el-button
                      size="small"
                      text
                      :icon="isEntry ? Edit : View"
                      @click="isEntry ? openEntry(row) : openPreview(row)"
                    />
                  </el-tooltip>
                  <el-dropdown trigger="click" @command="(c: string) => onRowCmd(c, row)">
                    <el-button size="small" text :icon="MoreFilled" />
                    <template #dropdown>
                      <el-dropdown-menu>
                        <template v-if="isEntry">
                          <el-dropdown-item v-if="canWrite" command="del" :icon="Delete">删除条目</el-dropdown-item>
                          <el-dropdown-item v-else disabled>无更多操作</el-dropdown-item>
                        </template>
                        <template v-else>
                          <el-dropdown-item command="chunks" :icon="Files">分块管理</el-dropdown-item>
                          <el-dropdown-item command="versions" :icon="Clock">版本历史</el-dropdown-item>
                          <template v-if="canWrite">
                            <el-dropdown-item command="acl" :icon="Lock" divided>文档权限</el-dropdown-item>
                            <el-dropdown-item command="reprocess" :icon="Refresh">重新处理</el-dropdown-item>
                            <el-dropdown-item command="del" :icon="Delete" divided>删除文档</el-dropdown-item>
                          </template>
                        </template>
                      </el-dropdown-menu>
                    </template>
                  </el-dropdown>
                </div>
              </template>
            </el-table-column>
          </el-table>
          <el-empty v-if="!loading && !filteredDocs.length" description="暂无文档" :image-size="70" />
        </div>
      </el-tab-pane>

      <!-- Tab：统计 -->
      <el-tab-pane v-if="!isExternal" label="统计" name="stats">
        <div class="app-card panel">
          <EmptyState v-if="!stats" description="暂无统计数据" icon="DataAnalysis" />
          <template v-else>
            <el-descriptions :column="5" size="small" style="margin-bottom: 16px">
              <el-descriptions-item label="文档数">{{ stats.doc_count }}</el-descriptions-item>
              <el-descriptions-item label="分块数">{{ stats.chunk_count }}</el-descriptions-item>
              <el-descriptions-item label="已向量化">{{ stats.embedded_chunks }}</el-descriptions-item>
              <el-descriptions-item label="向量覆盖率">{{ Math.round((stats.embedded_ratio || 0) * 100) }}%</el-descriptions-item>
              <el-descriptions-item label="存储占用">{{ humanSize(stats.total_size) }}</el-descriptions-item>
            </el-descriptions>
            <div class="chart-row">
              <div class="chart-box">
                <div class="panel-title">状态分布</div>
                <VChart v-if="Object.keys(stats.by_status || {}).length" :option="statusPieOption" height="280px" />
                <el-empty v-else description="无数据" :image-size="60" />
              </div>
              <div class="chart-box">
                <div class="panel-title">文件类型分布</div>
                <VChart v-if="Object.keys(stats.by_ext || {}).length" :option="extPieOption" height="280px" />
                <el-empty v-else description="无数据" :image-size="60" />
              </div>
            </div>
          </template>
        </div>
      </el-tab-pane>

      <!-- Tab：内容巡检 -->
      <el-tab-pane label="内容巡检" name="audit">
        <div class="stack">
          <div class="app-card panel">
            <div class="panel-head">
              <span class="panel-title">内容巡检</span>
              <el-button size="small" :icon="Refresh" :loading="auditLoading" @click="loadAudit">扫描</el-button>
            </div>
            <EmptyState v-if="!audit" description="点击「扫描」检查内容质量" icon="Search" />
            <template v-else>
              <div class="toolbar wrap" style="margin-bottom: 12px">
                <el-tag type="danger" size="small">失败 {{ audit.counts?.failed || 0 }}</el-tag>
                <el-tag type="warning" size="small">空文档 {{ audit.counts?.empty || 0 }}</el-tag>
                <el-tag size="small" class="tag-gold">无分块 {{ audit.counts?.no_chunk || 0 }}</el-tag>
                <el-tag type="info" size="small">未标签 {{ audit.counts?.untagged || 0 }}</el-tag>
                <el-tag type="primary" size="small">陈旧 {{ audit.counts?.stale || 0 }}</el-tag>
              </div>
              <div v-for="g in auditGroups" :key="g.label" style="margin-bottom: 10px">
                <template v-if="g.list && g.list.length">
                  <div class="audit-label">{{ g.label }}（{{ g.list.length }}）</div>
                  <div class="audit-tags">
                    <el-tag v-for="d in g.list.slice(0, 20)" :key="d.id" size="small" :type="g.type" style="margin-bottom: 4px">
                      {{ d.title }}（#{{ d.id }}）
                    </el-tag>
                  </div>
                </template>
              </div>
              <el-alert v-if="auditClean" type="success" :closable="false" show-icon title="未发现问题，内容质量良好" />
            </template>
          </div>

          <div class="app-card panel">
            <div class="panel-head">
              <span class="panel-title">未命中查询（近 30 天）</span>
              <el-button size="small" :icon="Refresh" :loading="missLoading" @click="loadMisses">加载</el-button>
            </div>
            <p class="muted-xs" style="margin: 0 0 10px">这些提问在知识库中检索不到内容——按频次排序，是最值得补的内容缺口。</p>
            <EmptyState v-if="!misses" description="点击「加载」查看未命中查询" icon="Search" />
            <el-alert v-else-if="!misses.length" type="success" :closable="false" show-icon title="近 30 天没有未命中记录" />
            <el-table v-else :data="misses" size="small" :pagination="false">
              <el-table-column prop="query" label="查询" />
              <el-table-column label="次数" width="90">
                <template #default="{ row }">
                  <el-tag size="small" :type="row.count >= 5 ? 'danger' : row.count >= 2 ? 'warning' : 'info'">{{ row.count }}</el-tag>
                </template>
              </el-table-column>
            </el-table>
          </div>
        </div>
      </el-tab-pane>

      <!-- Tab：成员管理 -->
      <el-tab-pane v-if="canManage" label="成员管理" name="members">
        <div class="app-card panel">
          <div class="panel-head">
            <span class="panel-title">成员</span>
            <el-button type="primary" :icon="Plus" @click="openMember">添加成员</el-button>
          </div>
          <el-table :data="members" row-key="id">
            <el-table-column label="授权对象">
              <template #default="{ row }">
                <div class="mem-cell">
                  <el-tag size="small" :type="principalTag(row.principal_type)">{{ principalLabel(row.principal_type) }}</el-tag>
                  <span>{{ row.display_name }}</span>
                  <el-tag v-if="row.user_id && row.user_id === auth.user?.id" size="small" type="info">你</el-tag>
                </div>
              </template>
            </el-table-column>
            <el-table-column label="权限" width="140">
              <template #default="{ row }">
                <el-tag size="small" :type="permTag(row.perm_level)">{{ permLabel(row.perm_level) }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="90">
              <template #default="{ row }">
                <el-popconfirm title="移除该成员？" @confirm="removeMember(row)">
                  <template #reference><el-button size="small" type="danger" :icon="Delete" /></template>
                </el-popconfirm>
              </template>
            </el-table-column>
          </el-table>
          <p class="muted-xs" style="margin: 12px 0 0">
            说明：知识库可见性为「私有」时仅创建者与下列成员可访问；「公开/内部」则本租户全员可见。可按用户 / 部门 / 角色 / 用户组授权。
          </p>
        </div>
      </el-tab-pane>
    </el-tabs>

    <!-- 文档预览 -->
    <el-drawer v-model="previewOpen" :title="preview ? `预览：${preview.title}` : '文档预览'" size="720px" destroy-on-close>
      <div v-if="preview" v-loading="previewLoading">
        <img v-if="previewIsImg" :src="withToken(docApi.rawUrl(preview.id))" :alt="preview.title" style="max-width: 100%" />
        <iframe
          v-else-if="previewIsPdf"
          :src="withToken(docApi.rawUrl(preview.id))"
          :title="preview.title"
          style="width: 100%; height: 72vh; border: 0"
        />
        <iframe
          v-else-if="previewIsHtml"
          :src="withToken(docApi.rawUrl(preview.id))"
          :title="preview.title"
          sandbox=""
          style="width: 100%; height: 72vh; border: 1px solid var(--border)"
        />
        <template v-else>
          <p v-if="previewFallback" class="muted-xs" style="margin: 0 0 8px">
            该类型（.{{ preview.file_ext || '未知' }}）暂不支持内联预览，可下载后查看；以下为解析出的文本内容：
          </p>
          <a :href="withToken(docApi.rawUrl(preview.id))" target="_blank" rel="noreferrer">
            <el-button size="small" :icon="Download" style="margin-bottom: 10px">下载原文件</el-button>
          </a>
          <MarkdownBody v-if="previewIsMarkdown" :content="previewText || '(无内容)'" />
          <pre v-else class="presplit">{{ previewText || '(无内容)' }}</pre>
        </template>
      </div>
    </el-drawer>

    <!-- 分块管理 -->
    <el-drawer v-model="chunksOpen" :title="chunks ? `分块：${chunks.doc.title}（共 ${chunks.total}）` : '分块管理'" size="720px">
      <el-empty v-if="!chunks || !chunks.items.length" description="暂无分块" :image-size="70" />
      <div v-else class="chunk-list">
        <div v-for="c in chunks.items" :key="c.id" class="chunk-item">
          <div class="chunk-head">
            <el-tag size="small" type="primary">#{{ c.ordinal }}</el-tag>
            <el-tag size="small" type="info">{{ c.chunk_type === 'parent' ? '父块' : '子块' }}</el-tag>
            <el-tag v-if="c.page" size="small">第{{ c.page }}页</el-tag>
            <el-tag size="small">{{ c.token_count }} 字</el-tag>
            <el-tag size="small" :type="c.has_embedding ? 'success' : 'warning'">{{ c.has_embedding ? '已向量化' : '无向量' }}</el-tag>
            <el-tag size="small" :type="visScopeTag(c.vis_scope)">{{ visScopeLabel(c.vis_scope) }}</el-tag>
            <template v-if="canWrite">
              <el-button size="small" text type="primary" :icon="Edit" @click="chunkEdit = { id: c.id, text: c.content }">编辑</el-button>
              <el-button size="small" text type="primary" :icon="Scissor" @click="startSplit(c.id, c.content)">拆分</el-button>
              <el-popconfirm title="删除该分块？" @confirm="delChunk(c.id)">
                <template #reference><el-button size="small" text type="danger">删除</el-button></template>
              </el-popconfirm>
            </template>
          </div>
          <pre class="chunk-body">{{ c.content }}</pre>
        </div>
      </div>
    </el-drawer>

    <!-- 版本历史 -->
    <el-drawer v-model="versionsOpen" :title="verDoc ? `版本历史：${verDoc.title}` : '版本历史'" size="720px">
      <el-alert
        type="info"
        :closable="false"
        show-icon
        style="margin-bottom: 12px"
        title="文档每次「重新处理 / 覆盖上传」都会归档上一版。回滚会把分块与正文还原到所选版本（回滚前会先归档当前版本，操作可逆）。"
      />
      <el-table v-loading="verLoading" :data="verList" row-key="id" size="small">
        <el-table-column label="版本" width="100">
          <template #default="{ row }">
            <el-tag size="small" :type="row.current ? 'success' : 'info'">v{{ row.version }}</el-tag>
            <el-tag v-if="row.current" size="small" type="primary" style="margin-left: 4px">当前</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="chunk_count" label="分块数" width="80" />
        <el-table-column prop="char_count" label="字符数" width="90" />
        <el-table-column label="归档原因" width="100">
          <template #default="{ row }"><el-tag size="small" type="info">{{ reasonLabel(row.reason) }}</el-tag></template>
        </el-table-column>
        <el-table-column label="归档时间" width="180">
          <template #default="{ row }">{{ row.created_at ? fmtTime(row.created_at) : '-' }}</template>
        </el-table-column>
        <el-table-column label="操作" width="190">
          <template #default="{ row }">
            <el-button size="small" :icon="View" @click="viewVersion(row.version)">查看</el-button>
            <el-popconfirm v-if="!row.current && canWrite" :title="`回滚到 v${row.version}？当前版本会先被归档`" @confirm="rollbackVersion(row.version)">
              <template #reference><el-button size="small" :icon="Clock">回滚</el-button></template>
            </el-popconfirm>
            <span v-else-if="row.current" class="muted-xs">—</span>
          </template>
        </el-table-column>
      </el-table>
    </el-drawer>

    <!-- 版本内容查看 -->
    <el-drawer v-model="verViewOpen" :title="verView ? `版本内容：v${verView.version}` : '版本内容'" size="720px">
      <template v-if="verView">
        <el-descriptions size="small" :column="3" style="margin-bottom: 12px">
          <el-descriptions-item label="版本">v{{ verView.version }}</el-descriptions-item>
          <el-descriptions-item label="分块数">{{ verView.chunk_count }}</el-descriptions-item>
          <el-descriptions-item label="归档时间">{{ verView.created_at ? fmtTime(verView.created_at) : '-' }}</el-descriptions-item>
        </el-descriptions>
        <p class="muted-xs" style="margin: 0 0 6px">归档正文：</p>
        <pre class="presplit">{{ verView.content || '(该版本未保存正文，可查看分块快照)' }}</pre>
      </template>
    </el-drawer>

    <!-- 文档级权限 ACL -->
    <el-drawer v-model="aclOpen" :title="aclDoc ? `文档权限：${aclDoc.title}` : '文档权限'" size="560px">
      <p class="muted-xs" style="margin: 0 0 10px">
        说明：添加授权会将该文档设为「受限」，只有命中 allow 且未命中 deny 的主体可检索到。
      </p>
      <div class="toolbar wrap" style="margin-bottom: 12px">
        <el-select v-model="aclType" style="width: 120px" @change="aclPid = undefined">
          <el-option value="user" label="用户" />
          <el-option value="department" label="部门" />
          <el-option value="role" label="角色" />
          <el-option value="group" label="用户组" />
        </el-select>
        <el-select v-model="aclPid" filterable placeholder="选择授权对象" style="width: 200px">
          <el-option v-for="o in objectOptions" :key="o.value" :value="o.value" :label="o.label" />
        </el-select>
        <el-select v-model="aclEffect" style="width: 100px">
          <el-option value="allow" label="允许" />
          <el-option value="deny" label="拒绝" />
        </el-select>
        <el-button type="primary" @click="addAcl">添加</el-button>
      </div>
      <el-empty v-if="!aclList.length" description="暂无授权（文档默认继承知识库可见性）" :image-size="60" />
      <div v-else class="acl-list">
        <div v-for="a in aclList" :key="a.id" class="acl-item">
          <div class="acl-main">
            <el-tag size="small" :type="a.effect === 'allow' ? 'success' : 'danger'">{{ a.effect === 'allow' ? '允许' : '拒绝' }}</el-tag>
            <span>{{ a.principal_name || `主体 #${a.principal_id}` }}</span>
          </div>
          <el-popconfirm title="移除该授权？" @confirm="delAcl(a.id)">
            <template #reference><el-button size="small" type="danger" :icon="Delete" /></template>
          </el-popconfirm>
        </div>
      </div>
    </el-drawer>

    <!-- 编辑知识库 -->
    <el-dialog v-model="kbDialog" title="编辑知识库" width="520px">
      <el-form label-position="top">
        <el-form-item label="名称" required>
          <el-input v-model="kbForm.name" placeholder="如：产品文档中心" />
        </el-form-item>
        <el-form-item label="描述">
          <el-input v-model="kbForm.description" type="textarea" :rows="3" placeholder="简要说明该知识库的用途" />
        </el-form-item>
        <el-form-item label="可见性">
          <el-radio-group v-model="kbForm.visibility">
            <el-radio-button value="internal">内部</el-radio-button>
            <el-radio-button value="public">公开</el-radio-button>
            <el-radio-button value="private">私有</el-radio-button>
          </el-radio-group>
        </el-form-item>
        <el-form-item v-if="!isExternal" label="索引方式">
          <el-radio-group v-model="kbForm.index_mode">
            <el-radio-button value="vector">向量 + 关键词</el-radio-button>
            <el-radio-button value="keyword">纯关键词</el-radio-button>
          </el-radio-group>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="kbDialog = false">取消</el-button>
        <el-button type="primary" :loading="kbSaving" @click="saveKb">保存</el-button>
      </template>
    </el-dialog>

    <!-- 导入同步配置 -->
    <el-dialog v-model="syncCfgOpen" title="导入同步配置" width="520px">
      <el-form label-position="top">
        <el-form-item label="启用定时同步">
          <el-switch v-model="syncForm.enabled" />
          <span class="muted-xs" style="margin-left: 8px">开启后可由定时任务按计划触发；也可随时手动「立即同步」</span>
        </el-form-item>
        <el-form-item label="单次同步上限">
          <el-input v-model.number="syncForm.limit" type="number" placeholder="500" />
        </el-form-item>
        <el-form-item label="种子查询（每行一条）">
          <el-input v-model="syncForm.seed_queries" type="textarea" :rows="4" placeholder="例如：&#10;产品手册&#10;常见问题" />
          <span class="muted-xs">连接器无「批量列举」能力时，用这些查询词去远端检索并导入；具备批量列举能力的连接器会忽略此配置</span>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="syncCfgOpen = false">取消</el-button>
        <el-button type="primary" @click="saveSyncConfig">保存</el-button>
      </template>
    </el-dialog>

    <!-- 分块编辑 -->
    <el-dialog v-model="chunkEditOpen" title="编辑分块（保存后自动重算向量）" width="720px" destroy-on-close>
      <p class="muted-xs" style="margin: 0 0 8px">修改分块内容会重新生成向量并刷新关键词索引，检索结果随之更新。</p>
      <el-input v-model="chunkEditText" type="textarea" :rows="14" class="mono-area" />
      <template #footer>
        <el-button @click="chunkEdit = null">取消</el-button>
        <el-button type="primary" @click="saveChunk">保存</el-button>
      </template>
    </el-dialog>

    <!-- 拆分分块 -->
    <el-dialog v-model="splitOpen" title="拆分分块" width="720px" destroy-on-close>
      <template v-if="splitCtx">
        <p class="muted-xs" style="margin: 0 0 6px">
          拖动滑块选择切分位置（共 {{ splitCtx.content.length }} 字）。滑块左侧为前一块，右侧为后一块。
        </p>
        <el-slider v-model="splitCtx.offset" :min="1" :max="Math.max(1, splitCtx.content.length - 1)" />
        <span class="muted-xs">切分点：第 <b>{{ splitCtx.offset }}</b> 字</span>
        <div class="split-preview">
          <span>{{ splitCtx.content.slice(0, splitCtx.offset) }}</span><span class="split-caret">|</span><span>{{ splitCtx.content.slice(splitCtx.offset) }}</span>
        </div>
      </template>
      <template #footer>
        <el-button @click="splitCtx = null">取消</el-button>
        <el-button type="primary" @click="doSplitChunk">确认拆分</el-button>
      </template>
    </el-dialog>

    <!-- 标签编辑 -->
    <el-dialog v-model="tagOpen" :title="tagDoc ? `编辑标签：${tagDoc.title}` : '编辑标签'">
      <p class="muted-xs" style="margin: 0 0 10px">多个标签用回车分隔，最多 20 个。</p>
      <el-select v-model="tagModel" multiple filterable allow-create default-first-option placeholder="输入标签后回车" style="width: 100%">
        <el-option v-for="t in tagModel" :key="t" :value="t" :label="t" />
      </el-select>
      <template #footer>
        <el-button @click="tagDoc = null">取消</el-button>
        <el-button type="primary" @click="saveTags">保存</el-button>
      </template>
    </el-dialog>

    <!-- 添加/编辑图文条目 -->
    <el-dialog v-model="entryOpen" :title="entryEdit ? '编辑图文条目' : '添加图文条目'" width="640px" destroy-on-close>
      <el-alert
        type="info"
        :closable="false"
        show-icon
        style="margin-bottom: 12px"
        title="录入一段文字并配图片/附件；当有人问到与这段文字相关的问题、被检索命中时，配套图片/附件会自动发送给提问者（渠道用户）。"
      />
      <el-form label-position="top">
        <el-form-item label="标题" required>
          <el-input v-model="entryTitle" placeholder="如：如何重置密码" />
        </el-form-item>
        <el-form-item label="正文（用于检索匹配）" required>
          <el-input v-model="entryContent" type="textarea" :rows="6" placeholder="用自然语言描述这个条目要说明的内容，客户问相关问题时能匹配上。" />
        </el-form-item>
        <el-form-item label="配套图片/附件（命中后自动发送）">
          <el-upload :before-upload="onEntryFile" :show-file-list="false" multiple>
            <el-button :icon="Upload">选择图片/附件</el-button>
          </el-upload>
          <div v-if="entryEdit && (entryEdit.attachments || []).length" class="entry-attach">
            <el-tag
              v-for="(a, i) in entryEdit.attachments"
              :key="`old-${i}`"
              size="small"
              closable
              type="primary"
              @close="removeOldAttachment(i)"
            >已有：{{ a.name }}</el-tag>
          </div>
          <div v-if="entryFiles.length" class="entry-attach">
            <el-tag
              v-for="(f, i) in entryFiles"
              :key="i"
              size="small"
              closable
              type="success"
              @close="entryFiles.splice(i, 1)"
            >新增：{{ f.name }}</el-tag>
          </div>
        </el-form-item>
      </el-form>
      <p v-if="entryEdit" class="muted-xs" style="margin: 0">保存正文改动后，新增附件会自动追加到该条目。删除已有附件即时生效。</p>
      <template #footer>
        <el-button @click="entryOpen = false">取消</el-button>
        <el-button type="primary" :loading="entryBusy" @click="saveEntry">保存</el-button>
      </template>
    </el-dialog>

    <!-- 添加成员 -->
    <el-dialog v-model="memberOpen" title="添加成员" width="520px" destroy-on-close>
      <el-form label-position="top">
        <el-form-item label="授权对象类型">
          <el-radio-group v-model="memberForm.principal_type" @change="memberForm.principal_id = undefined">
            <el-radio-button value="user">用户</el-radio-button>
            <el-radio-button value="department">部门</el-radio-button>
            <el-radio-button value="role">角色</el-radio-button>
            <el-radio-button value="group">用户组</el-radio-button>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="授权对象" required>
          <el-select v-model="memberForm.principal_id" filterable placeholder="请选择对象" style="width: 100%">
            <el-option v-for="o in memberObjectOptions" :key="o.value" :value="o.value" :label="o.label" />
          </el-select>
        </el-form-item>
        <el-form-item label="权限">
          <el-select v-model="memberForm.perm_level" style="width: 100%">
            <el-option value="viewer" label="查看" />
            <el-option value="editor" label="编辑（上传/删除文档）" />
            <el-option value="manager" label="管理（含成员管理）" />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="memberOpen = false">取消</el-button>
        <el-button type="primary" @click="addMember">保存</el-button>
      </template>
    </el-dialog>

    <!-- 文档失败详情 -->
    <el-dialog
      :model-value="!!errDoc"
      title="处理失败详情"
      width="560px"
      @update:model-value="(v: boolean) => { if (!v) errDoc = null }"
    >
      <template v-if="errDoc">
        <p class="err-text" style="margin: 0 0 10px">{{ errDoc.error_msg || '未知错误' }}</p>
        <pre v-if="errDoc.error_detail" class="presplit">{{ JSON.stringify(errDoc.error_detail, null, 2) }}</pre>
      </template>
      <template #footer>
        <el-button @click="errDoc = null">关闭</el-button>
        <el-button v-if="canWrite" type="primary" @click="reprocessErrDoc">重新处理</el-button>
      </template>
    </el-dialog>
  </PageContainer>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  ArrowDown, ArrowLeft, ArrowUp, Clock, Delete, Download, Edit, Files,
  FolderAdd, FolderOpened, Lock, MoreFilled, Plus, PriceTag, Refresh, Scissor, Search, Setting, Upload, UploadFilled, View,
} from '@element-plus/icons-vue'
import PageContainer from '@/components/PageContainer.vue'
import EmptyState from '@/components/EmptyState.vue'
import MarkdownBody from '@/components/MarkdownBody.vue'
import VChart from '@/components/VChart.vue'
import {
  docApi, kbApi, providerApi, rbacApi, withToken,
  type ConnectorInfo, type Doc, type KB, type KBMember, type KBStats,
} from '@/api'
import { errMsg } from '@/api/http'
import { useAuthStore } from '@/stores/auth'

const route = useRoute()
const router = useRouter()
const kbId = Number(route.params.id)
const auth = useAuthStore()

// ---------- 状态 ----------
const kb = ref<KB | null>(null)
const loading = ref(false)
const activeTab = ref('docs')
const embModels = ref<{ id: number; display_name: string; embedding_dim?: number | null }[]>([])

const docs = ref<Doc[]>([])
const folders = ref<{ id: number; name: string; parent_id: number | null }[]>([])
const folderFilter = ref<number | 'all' | 'root'>('all')
const selectedDocIds = ref<number[]>([])

const stats = ref<KBStats | null>(null)
const audit = ref<any>(null)
const auditLoading = ref(false)
const misses = ref<{ query: string; count: number }[] | null>(null)
const missLoading = ref(false)

const members = ref<KBMember[]>([])
const users = ref<any[]>([])
const roles = ref<any[]>([])
const groups = ref<any[]>([])
const depts = ref<any[]>([])
const memberOpen = ref(false)
const memberForm = reactive<{ principal_type: string; principal_id: number | undefined; perm_level: string }>({
  principal_type: 'user', principal_id: undefined, perm_level: 'viewer',
})

const connector = ref<ConnectorInfo | null>(null)
const connQuery = ref('测试')
const connTestLoading = ref(false)
const connTestResult = ref<{ count: number; items: any[] } | null>(null)
const syncSt = ref<{
  enabled: boolean; last_at: number | null; last_status: string | null; last_count: number | null
  last_error: string | null; seed_queries: string[]; limit: number; synced_doc_count: number
} | null>(null)
const syncing = ref(false)
const syncCfgOpen = ref(false)
const syncForm = reactive<{ enabled: boolean; limit: number; seed_queries: string }>({ enabled: false, limit: 500, seed_queries: '' })

// 预览
const preview = ref<Doc | null>(null)
const previewText = ref('')
const previewLoading = ref(false)

// 分块
const chunks = ref<{ doc: Doc; total: number; items: any[] } | null>(null)
const chunkEdit = ref<{ id: number; text: string } | null>(null)
const chunkEditText = ref('')
const splitCtx = ref<{ chunkId: number; content: string; offset: number } | null>(null)

// 版本
const verDoc = ref<Doc | null>(null)
const verList = ref<any[]>([])
const verView = ref<{ version: number; content: string | null; chunk_count: number; created_at: string | null } | null>(null)
const verLoading = ref(false)

// 权限 ACL
const aclDoc = ref<Doc | null>(null)
const aclList = ref<{ id: number; principal_id: number; effect: string; principal_name?: string | null }[]>([])
const aclType = ref('user')
const aclPid = ref<number | undefined>()
const aclEffect = ref('allow')

// 标签 / 错误
const tagDoc = ref<Doc | null>(null)
const tagModel = ref<string[]>([])
const errDoc = ref<Doc | null>(null)

// 图文条目
const entryOpen = ref(false)
const entryEdit = ref<Doc | null>(null)
const entryTitle = ref('')
const entryContent = ref('')
const entryFiles = ref<File[]>([])
const entryBusy = ref(false)

// 编辑知识库
const kbDialog = ref(false)
const kbSaving = ref(false)
const kbForm = reactive<{ name: string; description: string; visibility: string; index_mode: string }>({
  name: '', description: '', visibility: 'internal', index_mode: 'vector',
})

let pollTimer: ReturnType<typeof setInterval> | null = null
const IMG_EXT = ['png', 'jpg', 'jpeg', 'gif', 'webp']
const TEXT_EXT = ['txt', 'text', 'md', 'markdown', 'csv', 'log', 'html', 'htm']

// ---------- 计算属性 ----------
const isExternal = computed(() => (kb.value?.source_type || 'local') === 'external')
const isEntry = computed(() => (kb.value?.source_type || 'local') === 'entry')
const isKeyword = computed(() => (kb.value?.index_mode || 'vector') === 'keyword')
const canWrite = computed(() => !!kb.value && ['owner', 'manager', 'editor'].includes(kb.value.my_perm || ''))
const canManage = computed(() => !!kb.value && ['owner', 'manager'].includes(kb.value.my_perm || ''))
const docsTabLabel = computed(() => (isEntry.value ? '图文条目' : '文档管理'))
const embModelLabel = computed(() => {
  const id = kb.value?.embedding_model_id
  if (!id) return '默认'
  return embModels.value.find((m) => m.id === id)?.display_name || `#${id}`
})
const connectorConfig = computed<Record<string, any>>(() => {
  const cfg = connector.value?.config || {}
  return Object.fromEntries(Object.entries(cfg).filter(([k]) => k !== 'api_key' && k !== 'api_key_set'))
})
const filteredDocs = computed(() =>
  docs.value.filter((d) => {
    if (folderFilter.value === 'all') return true
    if (folderFilter.value === 'root') return !d.folder_id
    return d.folder_id === folderFilter.value
  }),
)
const deptOptions = computed(() => {
  const out: { value: number; label: string }[] = []
  const walk = (nodes: any[], prefix: string) => {
    for (const n of nodes || []) {
      out.push({ value: n.id, label: `${prefix}${n.name}` })
      if (n.children?.length) walk(n.children, `${prefix}— `)
    }
  }
  walk(depts.value, '')
  return out
})
const objectOptions = computed(() => {
  if (aclType.value === 'user') return users.value.map((u) => ({ value: u.id, label: u.display_name || u.username }))
  if (aclType.value === 'role') return roles.value.map((r) => ({ value: r.id, label: r.name }))
  if (aclType.value === 'group') return groups.value.map((g) => ({ value: g.id, label: g.name }))
  return deptOptions.value
})
const memberObjectOptions = computed(() => {
  const t = memberForm.principal_type
  if (t === 'user') return users.value.map((u) => ({ value: u.id, label: `${u.display_name || u.username}（${u.username}）` }))
  if (t === 'department') return deptOptions.value
  if (t === 'role') return roles.value.map((r) => ({ value: r.id, label: r.name }))
  return groups.value.map((g) => ({ value: g.id, label: g.name }))
})
const statusPieOption = computed(() => pieOption(stats.value?.by_status || {}, STATUS_LABEL as Record<string, string>))
const extPieOption = computed(() => pieOption(stats.value?.by_ext || {}))
const auditGroups = computed(() => {
  const a = audit.value
  if (!a) return []
  return [
    { label: '失败（需修复）', list: a.failed, type: 'danger' },
    { label: '空文档（无正文）', list: a.empty, type: 'warning' },
    { label: '已就绪但无分块', list: a.no_chunk, type: 'warning' },
    { label: '陈旧（超 180 天未更新）', list: a.stale, type: 'primary' },
  ]
})
const auditClean = computed(() => {
  const counts = audit.value?.counts || {}
  return !Object.values(counts).some((n: any) => n > 0)
})
const previewOpen = computed({ get: () => !!preview.value, set: (v) => { if (!v) preview.value = null } })
const previewExt = computed(() => (preview.value?.file_ext || '').toLowerCase())
const previewIsImg = computed(() => IMG_EXT.includes(previewExt.value))
const previewIsPdf = computed(() => previewExt.value === 'pdf')
const previewIsHtml = computed(() => previewExt.value === 'html' || previewExt.value === 'htm')
const previewIsMarkdown = computed(() => preview.value?.kind === 'entry' || ['md', 'markdown'].includes(previewExt.value))
const previewFallback = computed(() => !previewIsMarkdown.value && !['txt', 'text', 'csv', 'log'].includes(previewExt.value))
const chunksOpen = computed({ get: () => !!chunks.value, set: (v) => { if (!v) chunks.value = null } })
const chunkEditOpen = computed({
  get: () => !!chunkEdit.value,
  set: (v) => { if (!v) chunkEdit.value = null },
})
const splitOpen = computed({ get: () => !!splitCtx.value, set: (v) => { if (!v) splitCtx.value = null } })
const versionsOpen = computed({ get: () => !!verDoc.value, set: (v) => { if (!v) verDoc.value = null } })
const verViewOpen = computed({ get: () => !!verView.value, set: (v) => { if (!v) verView.value = null } })
const aclOpen = computed({ get: () => !!aclDoc.value, set: (v) => { if (!v) aclDoc.value = null } })
const tagOpen = computed({ get: () => !!tagDoc.value, set: (v) => { if (!v) tagDoc.value = null } })

// ---------- 常量映射 ----------
const STATUS_LABEL: Record<string, string> = {
  pending: '等待', parsing: '解析中', chunking: '切分中', embedding: '向量化', ready: '就绪', failed: '失败', disabled: '停用',
}
const STATUS_TAG: Record<string, any> = { pending: 'info', parsing: 'primary', chunking: 'primary', embedding: 'primary', ready: 'success', failed: 'danger', disabled: 'warning' }
const VIS_SCOPE: Record<number, string> = { 0: 'inherit', 1: 'public', 2: 'restricted' }

const statusLabelOf = (s: string) => STATUS_LABEL[s] || s
const statusTag = (s: string) => STATUS_TAG[s] || 'info'
const visLabel = (v: string) => ({ public: '公开', internal: '内部', private: '私有' }[v] || v)
const visScopeLabel = (s: number) => ({ inherit: '继承', public: '公开', restricted: '受限' }[VIS_SCOPE[s] || 'inherit'] || '继承')
const visScopeTag = (s: number) => ({ 1: 'primary', 2: 'danger' }[s] || 'info')
const principalTag = (t: string) => ({ user: 'primary', department: 'success', role: 'warning', group: 'info' }[t] || 'info')
const principalLabel = (t: string) => ({ user: '用户', department: '部门', role: '角色', group: '用户组' }[t] || t)
const permTag = (l: string) => (l === 'manager' ? 'danger' : l === 'editor' ? 'primary' : 'info')
const permLabel = (l: string) => (l === 'manager' ? '管理' : l === 'editor' ? '编辑' : '查看')
const reasonLabel = (r: string) => ({ reprocess: '重新处理', reupload: '覆盖上传' }[r] || '手动')

function humanSize(n: number): string {
  if (!n) return '0 B'
  const u = ['B', 'KB', 'MB', 'GB']
  let i = 0
  let v = n
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++ }
  return `${v.toFixed(i === 0 ? 0 : 1)} ${u[i]}`
}
function fmtTime(v: number | string | null | undefined): string {
  if (!v) return '-'
  return new Date(v).toLocaleString('zh-CN')
}
function pieOption(data: Record<string, number>, nameMap?: Record<string, string>) {
  return {
    tooltip: { trigger: 'item' },
    legend: { orient: 'vertical', left: 'left', type: 'scroll' },
    series: [{
      type: 'pie', radius: ['40%', '68%'], center: ['62%', '50%'],
      data: Object.entries(data).map(([k, v]) => ({ name: nameMap?.[k] || k, value: v })),
    }],
  }
}

// ---------- 加载 ----------
async function loadEmbModels() {
  try {
    const cfgs = await providerApi.configs()
    embModels.value = cfgs
      .filter((c) => c.purpose === 'embedding')
      .map((c) => ({ id: c.id, display_name: c.display_name, embedding_dim: c.embedding_dim }))
  } catch { /* 无权或未配置 */ }
}
async function loadConnector() {
  try { connector.value = await kbApi.getConnector(kbId) } catch { /* 忽略 */ }
}
async function loadSyncStatus() {
  try { syncSt.value = await kbApi.syncStatus(kbId) } catch { /* 忽略 */ }
}
async function loadDocs() {
  try {
    const list = await docApi.list(kbId)
    docs.value = list
    const pending = list.some((d) => !['ready', 'failed'].includes(d.status))
    if (pending && !pollTimer) pollTimer = setInterval(loadDocs, 1500)
    else if (!pending && pollTimer) { clearInterval(pollTimer); pollTimer = null }
  } catch (e) { ElMessage.error(errMsg(e)) }
}
async function loadFolders() {
  try { folders.value = await docApi.listFolders(kbId) } catch (e) { ElMessage.error(errMsg(e)) }
}
async function loadStats() {
  try { stats.value = await kbApi.stats(kbId) } catch { /* 无权或暂无 */ }
}
async function loadMembers() {
  try { members.value = await kbApi.members(kbId) } catch (e) { ElMessage.error(errMsg(e)) }
}
async function loadAudit() {
  auditLoading.value = true
  try { audit.value = await kbApi.audit(kbId) } catch (e) { ElMessage.error(errMsg(e)) } finally { auditLoading.value = false }
}
async function loadMisses() {
  missLoading.value = true
  try { misses.value = await kbApi.missedQueries(30) } catch (e) { ElMessage.error(errMsg(e)) } finally { missLoading.value = false }
}
async function loadRbac() {
  try {
    const r = await rbacApi.users(1, 100)
    users.value = r.items
  } catch { /* 忽略 */ }
  rbacApi.roles().then((r) => (roles.value = r)).catch(() => {})
  rbacApi.groups().then((r) => (groups.value = r)).catch(() => {})
  rbacApi.deptTree().then((r) => (depts.value = r)).catch(() => {})
}

function onTabChange(name: string | number) {
  if (name === 'audit') {
    if (!audit.value && !auditLoading.value) loadAudit()
    if (!misses.value && !missLoading.value) loadMisses()
  }
}

// ---------- 知识库编辑 ----------
function openEditKb() {
  if (!kb.value) return
  Object.assign(kbForm, {
    name: kb.value.name, description: kb.value.description || '',
    visibility: kb.value.visibility, index_mode: kb.value.index_mode || 'vector',
  })
  kbDialog.value = true
}
async function saveKb() {
  if (!kbForm.name.trim()) { ElMessage.warning('请输入名称'); return }
  kbSaving.value = true
  try {
    kb.value = await kbApi.update(kbId, {
      name: kbForm.name.trim(), description: kbForm.description,
      visibility: kbForm.visibility as KB['visibility'],
      index_mode: kbForm.index_mode as KB['index_mode'],
      // 乐观锁：带上本次加载时的版本，别人在这期间改过就返回 409
      row_version: kb.value?.row_version,
    })
    ElMessage.success('已保存')
    kbDialog.value = false
  } catch (e) { ElMessage.error(errMsg(e)) } finally { kbSaving.value = false }
}
async function setKbEmbedding(modelId: number | null) {
  // 换模型前先确认：向量维度不同的模型混用，旧向量与新查询向量无法比对，
  // 检索会返回「向量维度不一致」。库里已有分块时必须让用户知道代价。
  if (modelId && (kb.value?.chunk_count ?? 0) > 0) {
    const target = embModels.value.find((m) => m.id === modelId)
    const newDim = target?.embedding_dim || 0
    const oldDim = (kb.value as any)?.embedding_dim || 0
    const dimNote = newDim && oldDim && newDim !== oldDim
      ? `新模型为 ${newDim} 维，本库现有向量为 ${oldDim} 维，两者无法比对。\n`
      : ''
    try {
      await ElMessageBox.confirm(
        `${dimNote}切换后需要「重新处理」全部文档（共 ${kb.value?.chunk_count} 个分块）才会用新模型重新向量化，`
        + '在此之前该库只能靠关键词检索。现在切换？',
        '切换向量模型',
        { type: 'warning', confirmButtonText: '切换', cancelButtonText: '取消' },
      )
    } catch { return }
  }
  try {
    kb.value = await kbApi.update(kbId, { embedding_model_id: modelId, row_version: kb.value?.row_version })
    ElMessage.success(modelId ? '已切换向量模型，旧文档需「重新处理」才会用新模型重嵌入' : '已恢复默认向量模型')
  } catch (e) { ElMessage.error(errMsg(e)) }
}

// ---------- 外部库同步 ----------
async function doSync() {
  syncing.value = true
  try {
    const r = await kbApi.sync(kbId, syncSt.value?.limit || 500)
    ElMessage.success(`同步完成：拉取 ${r.fetched} 条，新增 ${r.created} 篇，跳过重复 ${r.skipped} 篇`)
    loadSyncStatus()
  } catch (e) { ElMessage.error(errMsg(e)) } finally { syncing.value = false }
}
function openSyncConfig() {
  syncForm.enabled = syncSt.value?.enabled ?? false
  syncForm.limit = syncSt.value?.limit ?? 500
  syncForm.seed_queries = (syncSt.value?.seed_queries || []).join('\n')
  syncCfgOpen.value = true
}
async function saveSyncConfig() {
  try {
    await kbApi.syncConfig(kbId, {
      enabled: syncForm.enabled,
      limit: Number(syncForm.limit) || 500,
      seed_queries: syncForm.seed_queries.split('\n').map((s) => s.trim()).filter(Boolean),
    })
    ElMessage.success('已保存同步配置')
    syncCfgOpen.value = false
    loadSyncStatus()
  } catch (e) { ElMessage.error(errMsg(e)) }
}
async function testConnector() {
  connTestLoading.value = true
  connTestResult.value = null
  try {
    const r = await kbApi.testConnector(kbId, connQuery.value, 3)
    connTestResult.value = { count: r.count, items: r.items }
    ElMessage.success(`连接成功，返回 ${r.count} 条`)
  } catch (e) { ElMessage.error(errMsg(e)) } finally { connTestLoading.value = false }
}

// ---------- 文档上传 / 条目 ----------
async function beforeUpload(file: any) {
  try {
    const r = await docApi.upload(kbId, file)
    const existed = docs.value.some((d) => d.id === r.id)
    ElMessage.success(existed ? `${file.name} 内容与已有文档重复，已复用` : `${file.name} 已上传，正在处理`)
    loadDocs()
  } catch (e) {
    ElMessageBox.alert(
      `${errMsg(e)}\n\n可按以下方式处理：\n· 若提示「未配置向量模型」：去「模型管理」配一个 embedding 模型；或本库改用纯关键词索引（编辑知识库 → 索引方式）。\n· 若提示解析失败：确认文件格式受支持（PDF / Word / Excel / PPT / Markdown / 文本 / 图片）。\n· 其它情况：点文档列表的「查看详情」看结构化报错与建议。`,
      '上传失败',
      { confirmButtonText: '知道了' },
    ).catch(() => {})
  }
  return false
}

/** 拖拽上传：把落下的文件逐个走 beforeUpload（与点选上传同一路径）。 */
async function onDrop(e: DragEvent) {
  const files = Array.from(e.dataTransfer?.files || [])
  if (!files.length) return
  for (const f of files) {
    if ((f as any).type === '' && (f as any).size === 0) continue // 目录占位，跳过
    await beforeUpload(f)
  }
}
function onEntryFile(file: any) {
  entryFiles.value.push(file as File)
  return false
}
function openEntry(d?: Doc) {
  entryEdit.value = d || null
  entryTitle.value = d?.title || ''
  entryContent.value = d?.content || ''
  entryFiles.value = []
  entryOpen.value = true
}
async function saveEntry() {
  if (!entryTitle.value.trim() || !entryContent.value.trim()) { ElMessage.warning('标题和正文都要填'); return }
  entryBusy.value = true
  try {
    if (entryEdit.value) {
      await docApi.updateEntry(entryEdit.value.id, { title: entryTitle.value.trim(), content: entryContent.value.trim() })
      if (entryFiles.value.length) await docApi.addEntryAttachments(entryEdit.value.id, entryFiles.value)
    } else {
      await docApi.createEntry(kbId, entryTitle.value.trim(), entryContent.value.trim(), entryFiles.value)
    }
    ElMessage.success('已保存，正在入库')
    entryOpen.value = false
    loadDocs()
  } catch (e) { ElMessage.error(errMsg(e)) } finally { entryBusy.value = false }
}
async function removeOldAttachment(idx: number) {
  if (!entryEdit.value) return
  try {
    const updated = await docApi.removeEntryAttachment(entryEdit.value.id, idx)
    entryEdit.value = { ...entryEdit.value, attachments: updated.attachments }
    ElMessage.success('已删除附件')
  } catch (e) { ElMessage.error(errMsg(e)) }
}

// ---------- 文件夹 ----------
async function addFolder() {
  try {
    const { value } = await ElMessageBox.prompt('请输入文件夹名称', '新建文件夹', {
      inputPlaceholder: '文件夹名称', confirmButtonText: '创建', cancelButtonText: '取消',
      inputValidator: (v) => (v && v.trim() ? true : '请输入名称'),
    })
    await docApi.createFolder(kbId, value.trim())
    ElMessage.success('已创建')
    loadFolders()
  } catch { /* 取消 */ }
}
async function renameCurrentFolder() {
  if (typeof folderFilter.value !== 'number') return
  const f = folders.value.find((x) => x.id === folderFilter.value)
  if (!f) return
  try {
    const { value } = await ElMessageBox.prompt('请输入新的文件夹名称', '重命名文件夹', {
      inputValue: f.name, confirmButtonText: '保存', cancelButtonText: '取消',
      inputValidator: (v) => (v && v.trim() ? true : '请输入名称'),
    })
    const name = value.trim()
    if (name === f.name) return
    await docApi.updateFolder(f.id, { name })
    ElMessage.success('已重命名')
    loadFolders()
  } catch { /* 取消 */ }
}
async function moveFolderOrder(folderId: number, dir: -1 | 1) {
  const ids = folders.value.map((f) => f.id)
  const i = ids.indexOf(folderId)
  const j = i + dir
  if (i < 0 || j < 0 || j >= ids.length) return
  ;[ids[i], ids[j]] = [ids[j], ids[i]]
  try { await docApi.reorderFolders(kbId, ids); loadFolders() } catch (e) { ElMessage.error(errMsg(e)) }
}
async function removeFolder(folderId: number) {
  try {
    await docApi.removeFolder(folderId)
    ElMessage.success('已删除文件夹')
    folderFilter.value = 'all'
    loadFolders()
    loadDocs()
  } catch (e) { ElMessage.error(errMsg(e)) }
}
async function moveDoc(docId: number, folderId: number | null) {
  try { await docApi.moveDoc(docId, folderId); ElMessage.success('已移动'); loadDocs() } catch (e) { ElMessage.error(errMsg(e)) }
}

// ---------- 批量 ----------
function onSelChange(rows: any[]) {
  selectedDocIds.value = rows.map((r) => r.id)
}
async function batchDelete() {
  if (!selectedDocIds.value.length) return
  try {
    const r = await docApi.batchDelete(selectedDocIds.value)
    ElMessage.success(r.message || '已删除')
    selectedDocIds.value = []
    loadDocs(); loadStats()
  } catch (e) { ElMessage.error(errMsg(e)) }
}
async function onBatchMove(v: number) {
  if (!selectedDocIds.value.length) return
  try {
    const r = await docApi.batchMove(selectedDocIds.value, v === 0 ? null : v)
    ElMessage.success(r.message || '已移动')
    selectedDocIds.value = []
    loadDocs()
  } catch (e) { ElMessage.error(errMsg(e)) }
}
function onBatchVisibility(v: string) {
  if (!selectedDocIds.value.length) return
  const label = ({ inherit: '继承知识库', public: '公开', restricted: '受限' } as Record<string, string>)[v] || v
  ElMessageBox.confirm(
    `将 ${selectedDocIds.value.length} 个文档可见性改为「${label}」？改「公开」会让本租户全员可检索到这些文档；「受限」则仅命中下方授权的主体可检索。`,
    '确认修改',
    { type: 'warning', confirmButtonText: '确认修改', cancelButtonText: '取消' },
  ).then(async () => {
    try {
      const r = await docApi.batchVisibility(selectedDocIds.value, v)
      ElMessage.success(r.message || '已更新')
      selectedDocIds.value = []
      loadDocs()
    } catch (e) { ElMessage.error(errMsg(e)) }
  }).catch(() => {})
}
async function batchReprocess() {
  if (!selectedDocIds.value.length) return
  try {
    const r = await docApi.batchReprocess(selectedDocIds.value)
    ElMessage.success(r.message || '已提交')
    selectedDocIds.value = []
    loadDocs()
  } catch (e) { ElMessage.error(errMsg(e)) }
}
async function mergeExport(fmt: string) {
  if (!selectedDocIds.value.length) return
  try {
    const token = localStorage.getItem('access_token') || ''
    const resp = await fetch(docApi.mergeExportUrl(fmt as any), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
      body: JSON.stringify({ ids: selectedDocIds.value }),
    })
    if (!resp.ok) throw new Error('导出失败')
    const blob = await resp.blob()
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `合并导出-${selectedDocIds.value.length}篇.${fmt}`
    a.click()
    URL.revokeObjectURL(url)
  } catch (e) { ElMessage.error(errMsg(e)) }
}

// ---------- 单文档操作 ----------
async function setVisibility(d: Doc, v: string) {
  // 回传 row_version：若列表加载后别人改过这篇文档，这里会拿到 409 而不是静默覆盖
  try { await docApi.setVisibility(d.id, v, d.row_version); ElMessage.success('已更新可见性'); loadDocs() } catch (e) { ElMessage.error(errMsg(e)) }
}
async function reprocessDoc(d: Doc) {
  try { await docApi.reprocess(d.id); ElMessage.info('已重新提交处理'); loadDocs() } catch (e) { ElMessage.error(errMsg(e)) }
}
async function reprocessErrDoc() {
  if (!errDoc.value) return
  const d = errDoc.value
  errDoc.value = null
  await reprocessDoc(d)
}
function onRowCmd(cmd: string, row: Doc) {
  if (cmd === 'chunks') openChunks(row)
  else if (cmd === 'versions') openVersions(row)
  else if (cmd === 'acl') openAcl(row)
  else if (cmd === 'reprocess') reprocessDoc(row)
  else if (cmd === 'del') {
    ElMessageBox.confirm(`「${row.title}」删除后不可恢复。`, isEntry.value ? '删除该条目？' : '删除该文档？', {
      type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消',
    }).then(async () => {
      await docApi.remove(row.id)
      ElMessage.success('已删除')
      loadDocs()
    }).catch(() => {})
  }
}

// ---------- 预览 ----------
async function openPreview(d: Doc) {
  preview.value = d
  previewText.value = ''
  const ext = (d.file_ext || '').toLowerCase()
  if (IMG_EXT.includes(ext) || ext === 'pdf' || ext === 'html' || ext === 'htm') return
  previewLoading.value = true
  try {
    const r = await docApi.content(d.id)
    previewText.value = r.content
  } catch (e) { ElMessage.error(errMsg(e)) } finally { previewLoading.value = false }
}

// ---------- 分块 ----------
async function openChunks(d: Doc) {
  try {
    const r = await docApi.chunks(d.id)
    chunks.value = { doc: d, total: r.total, items: r.items }
  } catch (e) { ElMessage.error(errMsg(e)) }
}
async function reloadChunks() {
  if (!chunks.value) return
  try {
    const r = await docApi.chunks(chunks.value.doc.id)
    chunks.value = { doc: chunks.value.doc, total: r.total, items: r.items }
    loadDocs()
  } catch (e) { ElMessage.error(errMsg(e)) }
}
watch(chunkEdit, (c) => { chunkEditText.value = c?.text || '' })
async function saveChunk() {
  if (!chunks.value || !chunkEdit.value) return
  try {
    await docApi.updateChunk(chunks.value.doc.id, chunkEdit.value.id, chunkEditText.value)
    ElMessage.success('已保存并重算向量')
    chunkEdit.value = null
    reloadChunks()
  } catch (e) { ElMessage.error(errMsg(e)) }
}
async function delChunk(chunkId: number) {
  if (!chunks.value) return
  try { await docApi.deleteChunk(chunks.value.doc.id, chunkId); ElMessage.success('已删除'); reloadChunks() }
  catch (e) { ElMessage.error(errMsg(e)) }
}
function startSplit(chunkId: number, content: string) {
  splitCtx.value = { chunkId, content, offset: Math.floor(content.length / 2) }
}
async function doSplitChunk() {
  if (!chunks.value || !splitCtx.value) return
  const n = splitCtx.value.offset
  if (n <= 0 || n >= splitCtx.value.content.length) { ElMessage.warning('切分点需在文本中间'); return }
  try {
    await docApi.splitChunk(chunks.value.doc.id, splitCtx.value.chunkId, n)
    ElMessage.success('已拆分')
    splitCtx.value = null
    reloadChunks()
  } catch (e) { ElMessage.error(errMsg(e)) }
}

// ---------- 版本 ----------
async function openVersions(d: Doc) {
  verDoc.value = d
  verList.value = []
  verLoading.value = true
  try { verList.value = await docApi.listVersions(d.id) } catch (e) { ElMessage.error(errMsg(e)) } finally { verLoading.value = false }
}
async function viewVersion(v: number) {
  if (!verDoc.value) return
  try {
    const d = await docApi.getVersion(verDoc.value.id, v)
    verView.value = { version: d.version, content: d.content, chunk_count: d.chunk_count, created_at: d.created_at }
  } catch (e) { ElMessage.error(errMsg(e)) }
}
async function rollbackVersion(v: number) {
  if (!verDoc.value) return
  try {
    await docApi.rollbackVersion(verDoc.value.id, v)
    ElMessage.success(`已回滚到 v${v}`)
    verDoc.value = null
    loadDocs(); loadStats()
  } catch (e) { ElMessage.error(errMsg(e)) }
}

// ---------- ACL ----------
async function openAcl(d: Doc) {
  aclDoc.value = d
  aclType.value = 'user'
  aclPid.value = undefined
  aclEffect.value = 'allow'
  try { const r = await docApi.acl(d.id); aclList.value = r.items || [] } catch (e) { ElMessage.error(errMsg(e)) }
}
async function addAcl() {
  if (!aclDoc.value || aclPid.value == null) { ElMessage.warning('请选择对象'); return }
  try {
    await docApi.addAcl(aclDoc.value.id, { principal_type: aclType.value, principal_id: aclPid.value, effect: aclEffect.value })
    ElMessage.success('已添加授权')
    const r = await docApi.acl(aclDoc.value.id)
    aclList.value = r.items || []
    loadDocs()
  } catch (e) { ElMessage.error(errMsg(e)) }
}
async function delAcl(aclId: number) {
  if (!aclDoc.value) return
  try {
    await docApi.removeAcl(aclDoc.value.id, aclId)
    ElMessage.success('已移除')
    const r = await docApi.acl(aclDoc.value.id)
    aclList.value = r.items || []
  } catch (e) { ElMessage.error(errMsg(e)) }
}

// ---------- 标签 ----------
function openTags(d: Doc) {
  tagDoc.value = d
  tagModel.value = [...(d.tags || [])]
}
async function saveTags() {
  if (!tagDoc.value) return
  try {
    await docApi.setTags(tagDoc.value.id, tagModel.value.slice(0, 20), tagDoc.value.row_version)
    ElMessage.success('已保存标签')
    tagDoc.value = null
    loadDocs()
  } catch (e) { ElMessage.error(errMsg(e)) }
}

// ---------- 成员 ----------
function openMember() {
  memberForm.principal_type = 'user'
  memberForm.principal_id = undefined
  memberForm.perm_level = 'viewer'
  memberOpen.value = true
}
async function addMember() {
  if (memberForm.principal_id == null) { ElMessage.warning('请选择对象'); return }
  try {
    await kbApi.addMember(kbId, memberForm.principal_type, memberForm.principal_id, memberForm.perm_level)
    ElMessage.success('已保存成员')
    memberOpen.value = false
    loadMembers()
  } catch (e) { ElMessage.error(errMsg(e)) }
}
async function removeMember(m: KBMember) {
  try {
    await kbApi.removeMember(kbId, m.id)
    ElMessage.success('已移除')
    loadMembers()
  } catch (e) { ElMessage.error(errMsg(e)) }
}

// ---------- 生命周期 ----------
async function loadAll() {
  loading.value = true
  try {
    const found = await kbApi.get(kbId).catch(() => null)
    kb.value = found
    if (found?.source_type === 'external') {
      activeTab.value = 'connector'
      loadConnector(); loadSyncStatus()
    } else {
      activeTab.value = 'docs'
      loadDocs(); loadFolders(); loadStats(); loadEmbModels()
    }
    loadMembers()
    loadRbac()
  } finally { loading.value = false }
}

onMounted(loadAll)
onBeforeUnmount(() => { if (pollTimer) { clearInterval(pollTimer); pollTimer = null } })
</script>

<style scoped>
.panel { padding: 18px 20px; }
.stack { display: flex; flex-direction: column; gap: 16px; }
.panel-head {
  display: flex; align-items: center; justify-content: space-between;
  gap: 12px; margin-bottom: 14px; flex-wrap: wrap;
}
.panel-title { font-size: 15px; font-weight: 600; color: var(--text-title); }
.toolbar { display: flex; align-items: center; gap: 8px; }
.toolbar.wrap { flex-wrap: wrap; }
.drop-zone {
  margin: 10px 0 14px; padding: 22px; border: 1.5px dashed var(--border-strong, #cbd5e1);
  border-radius: 10px; text-align: center; color: var(--text-muted);
  background: var(--bg-subtle, #f8fafc); transition: border-color .15s, background .15s;
}
.drop-zone:hover { border-color: var(--color-primary, #2563eb); background: var(--color-primary-subtle, #eff6ff); }
.dz-ico { font-size: 28px; color: var(--color-primary, #2563eb); margin-bottom: 6px; }
.dz-text { font-size: 13px; }
.kv-row { display: flex; gap: 8px; margin-bottom: 12px; }
.kb-tag {
  font-size: 12px; padding: 2px 8px; border-radius: 999px;
  background: var(--color-primary-subtle); color: var(--brand-700);
}
.kb-tag.purple { background: #f3e8ff; color: #7c3aed; }
.kb-tag.orange { background: #ffedd5; color: #c2410c; }
.row-title { display: inline-flex; align-items: center; gap: 6px; }
.status-cell { display: flex; flex-direction: column; gap: 2px; width: 150px; }
.tag-cell { display: flex; flex-wrap: wrap; gap: 2px; cursor: pointer; align-items: center; }
.tag-sm { margin-right: 0; }
.muted-xs { color: var(--text-muted); font-size: 12px; display: inline-flex; align-items: center; gap: 2px; }
.mono-xs { font-family: ui-monospace, Consolas, monospace; font-size: 12px; }
.err-text { color: #dc2626; font-size: 13px; white-space: pre-wrap; }
.ops { display: inline-flex; align-items: center; gap: 2px; }
.mem-cell { display: flex; align-items: center; gap: 8px; }
.test-list { border: 1px solid var(--border); border-radius: 8px; overflow: hidden; }
.test-item { padding: 8px 12px; border-bottom: 1px solid var(--border-subtle); }
.test-item:last-child { border-bottom: none; }
.test-head { display: flex; align-items: center; gap: 6px; }
.test-body { margin: 4px 0 0; font-size: 12px; color: var(--text-secondary); }
.chart-row { display: flex; gap: 16px; flex-wrap: wrap; }
.chart-box {
  flex: 1 1 360px; border: 1px solid var(--border-subtle); border-radius: 8px; padding: 12px;
}
.audit-label { font-weight: 600; color: var(--text-title); font-size: 13px; margin-bottom: 6px; }
.audit-tags { display: flex; flex-wrap: wrap; gap: 4px; }
.tag-gold { background: #fef9c3; color: #a16207; border-color: #fde68a; }
.chunk-list { display: flex; flex-direction: column; gap: 12px; }
.chunk-item { border: 1px solid var(--border); border-radius: 8px; padding: 10px 12px; }
.chunk-head { display: flex; flex-wrap: wrap; gap: 4px; align-items: center; margin-bottom: 6px; }
.chunk-body, .presplit {
  margin: 0; white-space: pre-wrap; word-break: break-word; font-size: 13px;
  font-family: ui-monospace, Consolas, monospace; color: var(--text-body);
}
.presplit {
  background: var(--bg-subtle); padding: 12px; border-radius: 6px;
  max-height: 66vh; overflow: auto;
}
.split-preview {
  margin-top: 8px; background: var(--bg-subtle); border-radius: 6px; padding: 8px;
  max-height: 320px; overflow: auto; white-space: pre-wrap;
  font-family: ui-monospace, Consolas, monospace; font-size: 13px;
}
.split-caret { background: var(--color-primary); color: #fff; padding: 0 1px; }
.acl-list { display: flex; flex-direction: column; gap: 8px; }
.acl-item {
  display: flex; align-items: center; justify-content: space-between; gap: 8px;
  border: 1px solid var(--border); border-radius: 8px; padding: 8px 12px;
}
.acl-main { display: flex; align-items: center; gap: 8px; font-size: 13px; }
.entry-attach { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; width: 100%; }
.mono-area :deep(textarea) { font-family: ui-monospace, Consolas, monospace; font-size: 13px; }
</style>
