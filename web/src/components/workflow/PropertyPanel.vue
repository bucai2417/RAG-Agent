<template>
  <div class="pp">
    <div v-if="!node" class="pp-empty">点击画布中的节点以编辑其配置。</div>
    <div v-else class="pp-body">
      <div class="pp-title" :style="{ color: meta(node.type).color }">
        {{ meta(node.type).icon }} {{ meta(node.type).label }}
      </div>

      <el-form label-position="top" size="small">
        <el-form-item label="节点 ID">
          <el-input v-model="nodeId" :disabled="node.type === 'start' || node.type === 'end'" @blur="commitId" />
          <div class="pp-hint">用于变量引用，如 {{ refExample }}</div>
        </el-form-item>

        <!-- start -->
        <template v-if="node.type === 'start'">
          <el-form-item label="输入参数">
            <div v-for="(inp, i) in inputs" :key="i" class="pp-row">
              <el-input v-model="inp.name" placeholder="名称" style="width: 92px" @input="touch" />
              <el-input v-model="inp.type" placeholder="类型" style="width: 72px" @input="touch" />
              <el-button size="small" type="danger" :icon="Delete" @click="removeInput(i)" />
            </div>
            <el-button size="small" :icon="Plus" @click="addInput">添加输入</el-button>
          </el-form-item>
        </template>

        <!-- llm -->
        <template v-else-if="node.type === 'llm'">
          <el-form-item label="模型">
            <el-select v-model="config.model_config_id" clearable placeholder="默认模型" style="width: 100%" @change="touch">
              <el-option v-for="m in chatModels" :key="m.id" :value="m.id" :label="m.display_name || m.model_name" />
            </el-select>
          </el-form-item>
          <el-form-item label="系统提示词">
            <el-input v-model="config.system" type="textarea" :rows="3" @input="touch" />
          </el-form-item>
          <el-form-item label="用户提示词">
            <el-input v-model="config.prompt" type="textarea" :rows="4" :placeholder="'资料：{{ret.output}}'" @input="touch" />
            <div class="pp-hint">{{ varHint }}</div>
          </el-form-item>
          <el-form-item label="温度">
            <el-input-number v-model="config.temperature" :min="0" :max="2" :step="0.1" style="width: 100%" @change="touch" />
          </el-form-item>
        </template>

        <!-- knowledge_retrieval -->
        <template v-else-if="node.type === 'knowledge_retrieval'">
          <el-form-item label="检索语句">
            <el-input v-model="config.query" type="textarea" :rows="2" placeholder="{{start.query}}" @input="touch" />
            <div class="pp-hint">{{ varHint }}</div>
          </el-form-item>
          <el-form-item label="知识库（留空=全部可访问）">
            <el-select v-model="config.kb_ids" multiple clearable style="width: 100%" @change="touch">
              <el-option v-for="k in kbs" :key="k.id" :value="k.id" :label="k.name" />
            </el-select>
          </el-form-item>
          <el-form-item label="返回条数 top_k">
            <el-input-number v-model="config.top_k" :min="1" :max="50" style="width: 100%" @change="touch" />
          </el-form-item>
        </template>

        <!-- condition -->
        <template v-else-if="node.type === 'condition'">
          <el-form-item label="条件表达式">
            <el-input v-model="config.expression" type="textarea" :rows="2" :placeholder="`{{llm.output}} contains '无法回答'`" @input="touch" />
            <div class="pp-hint">支持 == != &gt; &lt; &gt;= &lt;= contains in，可用 AND / OR 组合；两条出线分别连 true / false。</div>
          </el-form-item>
        </template>

        <!-- switch -->
        <template v-else-if="node.type === 'switch'">
          <el-form-item label="分支规则">
            <div v-for="(c, i) in cases" :key="i" class="pp-row">
              <el-input v-model="c.handle" placeholder="handle" style="width: 90px" @input="touch" />
              <el-input v-model="c.expr" placeholder="表达式" style="width: 150px" @input="touch" />
              <el-button size="small" type="danger" :icon="Delete" @click="removeCase(i)" />
            </div>
            <el-button size="small" :icon="Plus" @click="addCase">添加分支</el-button>
            <div class="pp-hint">命中即走对应出线（sourceHandle=handle）；都不中走 default 出线。</div>
          </el-form-item>
        </template>

        <!-- parallel / join -->
        <template v-else-if="node.type === 'parallel'">
          <div class="pp-note">把多条出线连接到不同分支链，每条分支链需线性走到同一个「汇聚」节点。各分支并发执行（独立会话），分支间不共享中间变量。</div>
        </template>
        <template v-else-if="node.type === 'join'">
          <div class="pp-note">汇聚并行分支。各分支末节点输出归并到本节点：<b>{{ joinRef }}</b> 为各分支输出数组。</div>
        </template>

        <!-- file -->
        <template v-else-if="node.type === 'file'">
          <el-form-item label="操作">
            <el-select v-model="config.op" style="width: 100%" @change="touch">
              <el-option value="read" label="读取文件（file_key / doc_id）" />
              <el-option value="generate" label="生成文件（docx/xlsx/pdf/pptx/md/csv/txt）" />
              <el-option value="convert" label="转换格式（xlsx→csv / docx→pdf 等）" />
            </el-select>
          </el-form-item>
          <el-form-item label="参数（JSON）">
            <el-input v-model="argsText" type="textarea" :rows="5" class="pp-mono" @input="onArgsInput" />
            <div class="pp-hint">{{ varHint }}</div>
          </el-form-item>
        </template>

        <!-- http -->
        <template v-else-if="node.type === 'http'">
          <el-form-item label="URL">
            <el-input v-model="config.url" placeholder="https://..." @input="touch" />
            <div class="pp-hint">{{ varHint }}</div>
          </el-form-item>
          <el-form-item label="方法">
            <el-select v-model="config.method" style="width: 100%" @change="touch">
              <el-option value="GET" label="GET" />
              <el-option value="POST" label="POST" />
            </el-select>
          </el-form-item>
          <el-form-item label="请求体模板">
            <el-input v-model="config.body_template" type="textarea" :rows="3" @input="touch" />
          </el-form-item>
          <el-form-item label="超时（秒）">
            <el-input-number v-model="config.timeout" :min="1" style="width: 100%" @change="touch" />
          </el-form-item>
        </template>

        <!-- variable -->
        <template v-else-if="node.type === 'variable'">
          <el-form-item label="变量赋值">
            <div v-for="(a, i) in assignments" :key="i" class="pp-row">
              <el-input v-model="a.name" placeholder="变量名" style="width: 100px" @input="touch" />
              <el-input v-model="a.value" placeholder="值，可 {{引用}}" style="width: 140px" @input="touch" />
              <el-button size="small" type="danger" :icon="Delete" @click="removeAssign(i)" />
            </div>
            <el-button size="small" :icon="Plus" @click="addAssign">添加变量</el-button>
            <div class="pp-hint">{{ varHint }}</div>
          </el-form-item>
        </template>

        <!-- code -->
        <template v-else-if="node.type === 'code'">
          <el-form-item label="Python 代码">
            <el-input v-model="config.code" type="textarea" :rows="10" class="pp-mono" @input="touch" />
            <div class="pp-hint">从 stdin 读 {inputs,args,scope}，向 stdout 打印 JSON。</div>
          </el-form-item>
          <el-form-item label="超时（秒）">
            <el-input-number v-model="config.timeout" :min="1" :max="60" style="width: 100%" @change="touch" />
          </el-form-item>
        </template>

        <!-- loop -->
        <template v-else-if="node.type === 'loop'">
          <el-form-item label="遍历数组">
            <el-input v-model="config.items" placeholder="{{start.list}}" @input="touch" />
            <div class="pp-hint">{{ varHint }}</div>
          </el-form-item>
          <el-form-item label="循环项变量名">
            <el-input v-model="config.item_var" @input="touch" />
          </el-form-item>
          <el-form-item label="最大迭代次数">
            <el-input-number v-model="config.max_iterations" :min="1" :max="200" style="width: 100%" @change="touch" />
          </el-form-item>
          <el-form-item label="循环体类型">
            <el-select :model-value="config.body?.op || 'template'" style="width: 100%" @change="(v: any) => setBodyOp(v)">
              <el-option value="template" label="模板拼接" />
              <el-option value="llm" label="大模型" />
              <el-option value="code" label="代码" />
            </el-select>
          </el-form-item>
          <el-form-item v-if="(config.body?.op || 'template') === 'template'" label="模板">
            <el-input v-model="config.body.template" type="textarea" :rows="3" placeholder="处理：{{item}}" @input="touch" />
          </el-form-item>
          <template v-if="config.body?.op === 'llm'">
            <el-form-item label="系统提示词">
              <el-input v-model="config.body.system" type="textarea" :rows="2" @input="touch" />
            </el-form-item>
            <el-form-item label="用户提示词">
              <el-input v-model="config.body.prompt" type="textarea" :rows="3" placeholder="处理 {{item}}" @input="touch" />
            </el-form-item>
          </template>
          <el-form-item v-if="config.body?.op === 'code'" label="Python 代码（item 注入 scope）">
            <el-input v-model="config.body.code" type="textarea" :rows="5" class="pp-mono" @input="touch" />
          </el-form-item>
        </template>

        <!-- approval -->
        <template v-else-if="node.type === 'approval'">
          <el-form-item label="审批标题">
            <el-input v-model="config.title" placeholder="需要人工审批" @input="touch" />
          </el-form-item>
          <el-form-item label="审批内容">
            <el-input v-model="config.content" type="textarea" :rows="4" @input="touch" />
            <div class="pp-hint">{{ varHint }}</div>
          </el-form-item>
        </template>

        <!-- agent -->
        <template v-else-if="node.type === 'agent'">
          <el-form-item label="目标智能体">
            <el-select v-model="config.agent_id" placeholder="选择要调用的智能体" style="width: 100%" @change="touch">
              <el-option v-for="a in agents" :key="a.id" :value="a.id" :label="a.name" />
            </el-select>
          </el-form-item>
          <el-form-item label="输入模板">
            <el-input v-model="config.input_template" type="textarea" :rows="3" placeholder="{{start.query}}" @input="touch" />
            <div class="pp-hint">{{ varHint }}</div>
          </el-form-item>
          <el-form-item label="最大调用深度（防循环）">
            <el-input-number v-model="config.max_depth" :min="1" :max="5" style="width: 100%" @change="touch" />
          </el-form-item>
        </template>

        <!-- notify -->
        <template v-else-if="node.type === 'notify'">
          <el-form-item label="推送方式">
            <el-select v-model="config.mode" style="width: 100%" @change="touch">
              <el-option value="notify_channel" label="通知渠道（站内/Webhook/企微/邮件…）" />
              <el-option value="external_channel" label="直接推外部渠道（QQ/微信/企微/飞书）" />
            </el-select>
          </el-form-item>
          <template v-if="(config.mode || 'notify_channel') === 'notify_channel'">
            <el-form-item label="通知渠道">
              <el-select v-model="config.channel_id" placeholder="选择通知渠道" style="width: 100%" @change="touch">
                <el-option v-for="c in notifyChannels" :key="c.id" :value="c.id" :label="`${c.name}（${c.kind}）`" />
              </el-select>
            </el-form-item>
            <el-form-item label="通知标题">
              <el-input v-model="config.title" placeholder="工作流消息" @input="touch" />
            </el-form-item>
          </template>
          <template v-else>
            <el-form-item label="外部渠道">
              <el-select v-model="config.channel_id" placeholder="选择已接入渠道" style="width: 100%" @change="touch">
                <el-option v-for="c in extChannels" :key="c.id" :value="c.id" :label="`${c.name}（${c.kind}）`" />
              </el-select>
            </el-form-item>
            <el-form-item label="接收对象类型">
              <el-select v-model="config.target_type" style="width: 100%" @change="touch">
                <el-option value="group" label="群" />
                <el-option value="user" label="个人" />
              </el-select>
            </el-form-item>
            <el-form-item label="接收对象 id">
              <el-input v-model="config.target" placeholder="群 id 或用户 id" @input="touch" />
            </el-form-item>
          </template>
          <el-form-item label="推送内容">
            <el-input v-model="config.content" type="textarea" :rows="4" placeholder="{{llm.output}}" @input="touch" />
            <div class="pp-hint">{{ varHint }}</div>
          </el-form-item>
        </template>

        <!-- end -->
        <template v-else-if="node.type === 'end'">
          <el-form-item label="输出">
            <el-input v-model="config.output" type="textarea" :rows="3" placeholder="{{llm.output}}" @input="touch" />
            <div class="pp-hint">{{ varHint }}</div>
          </el-form-item>
        </template>

        <!-- ==================== 数据处理 ==================== -->
        <template v-else-if="node.type === 'table'">
          <el-form-item label="数据源（行数组）">
            <el-input v-model="config.rows" type="textarea" :rows="2" placeholder="{{http_1.output}}" @input="touch" />
            <div class="pp-hint">{{ varHint }}；支持 JSON 数组或换行文本</div>
          </el-form-item>
          <el-form-item label="过滤条件（全部满足）">
            <div v-for="(f, i) in filters" :key="i" class="pp-row">
              <el-input v-model="f.field" placeholder="字段" style="width: 84px" @input="touch" />
              <el-select v-model="f.op" style="width: 92px" @change="touch">
                <el-option value="eq" label="等于" />
                <el-option value="ne" label="不等于" />
                <el-option value="contains" label="包含" />
                <el-option value="gt" label="大于" />
                <el-option value="lt" label="小于" />
                <el-option value="not_empty" label="非空" />
              </el-select>
              <el-input v-model="f.value" placeholder="值" style="width: 84px" @input="touch" />
              <el-button size="small" type="danger" :icon="Delete" @click="removeItem(filters, i)" />
            </div>
            <el-button size="small" :icon="Plus" @click="filters.push({ field: '', op: 'eq', value: '' }); touch()">添加条件</el-button>
          </el-form-item>
          <el-form-item label="排序">
            <div class="pp-row">
              <el-input v-model="config.sort.field" placeholder="字段" style="width: 130px" @input="touch" />
              <el-switch v-model="config.sort.desc" active-text="降序" @change="touch" />
            </div>
          </el-form-item>
          <el-form-item label="字段投影（留空=保留全部）">
            <div v-for="(f, i) in fieldMaps" :key="i" class="pp-row">
              <el-input v-model="f.name" placeholder="输出列名" style="width: 100px" @input="touch" />
              <el-input v-model="f.from" placeholder="源字段（可点路径）" style="width: 120px" @input="touch" />
              <el-button size="small" type="danger" :icon="Delete" @click="removeItem(fieldMaps, i)" />
            </div>
            <el-button size="small" :icon="Plus" @click="fieldMaps.push({ name: '', from: '' }); touch()">添加字段</el-button>
          </el-form-item>
          <el-form-item label="聚合汇总">
            <div v-for="(a, i) in aggregates" :key="i" class="pp-row">
              <el-input v-model="a.name" placeholder="结果名" style="width: 84px" @input="touch" />
              <el-input v-model="a.field" placeholder="字段" style="width: 84px" @input="touch" />
              <el-select v-model="a.op" style="width: 88px" @change="touch">
                <el-option value="sum" label="求和" />
                <el-option value="avg" label="平均" />
                <el-option value="count" label="计数" />
                <el-option value="min" label="最小" />
                <el-option value="max" label="最大" />
                <el-option value="distinct" label="去重数" />
                <el-option value="join" label="拼接" />
              </el-select>
              <el-button size="small" type="danger" :icon="Delete" @click="removeItem(aggregates, i)" />
            </div>
            <el-button size="small" :icon="Plus" @click="aggregates.push({ name: '', field: '', op: 'sum' }); touch()">添加汇总</el-button>
          </el-form-item>
          <el-form-item label="去重">
            <div class="pp-row">
              <el-switch v-model="config.distinct" @change="touch" />
              <el-input v-if="config.distinct" v-model="config.distinct_field" placeholder="按字段（留空=整行）" style="width: 150px" @input="touch" />
            </div>
          </el-form-item>
          <el-form-item label="最多保留行数（0=不限）">
            <el-input-number v-model="config.limit" :min="0" :max="5000" style="width: 100%" @change="touch" />
          </el-form-item>
          <el-form-item label="输出为 CSV">
            <el-switch v-model="config.as_csv" @change="touch" />
            <div class="pp-hint">关闭则 output 为 JSON。CSV 可直接喂给「文件处理」生成 Excel。</div>
          </el-form-item>
        </template>

        <template v-else-if="node.type === 'mapping'">
          <el-form-item label="字段映射">
            <div v-for="(m, i) in mappings" :key="i" class="pp-row">
              <el-input v-model="m.key" placeholder="目标字段" style="width: 110px" @input="touch" />
              <el-input v-model="m.value" placeholder="值，可 {{引用}}" style="width: 150px" @input="touch" />
              <el-button size="small" type="danger" :icon="Delete" @click="removeItem(mappings, i)" />
            </div>
            <el-button size="small" :icon="Plus" @click="mappings.push({ key: '', value: '' }); touch()">添加字段</el-button>
            <div class="pp-hint">{{ varHint }}</div>
          </el-form-item>
        </template>

        <template v-else-if="node.type === 'json_path'">
          <el-form-item label="源数据">
            <el-input v-model="config.source" type="textarea" :rows="2" placeholder="{{http_1.output}}" @input="touch" />
          </el-form-item>
          <el-form-item label="取值路径">
            <el-input v-model="config.path" placeholder="data.items.0.id" @input="touch" />
            <div class="pp-hint">用点号分隔；数组用数字下标。留空则返回整个对象。</div>
          </el-form-item>
        </template>

        <template v-else-if="node.type === 'transform'">
          <el-form-item label="输入值">
            <el-input v-model="config.value" type="textarea" :rows="2" placeholder="{{start.query}}" @input="touch" />
            <div class="pp-hint">{{ varHint }}</div>
          </el-form-item>
          <el-form-item label="转换方式">
            <el-select v-model="config.op" style="width: 100%" @change="touch">
              <el-option value="to_text" label="转文本" />
              <el-option value="split" label="按分隔符拆成数组" />
              <el-option value="join" label="数组合并成文本" />
              <el-option value="extract" label="正则提取" />
              <el-option value="to_number" label="提取数字（去千分位/货币符号）" />
            </el-select>
          </el-form-item>
          <el-form-item v-if="config.op === 'split' || config.op === 'join'" label="分隔符">
            <el-input v-model="config.separator" placeholder="\n 或 ," @input="touch" />
          </el-form-item>
          <template v-if="config.op === 'extract'">
            <el-form-item label="正则表达式">
              <el-input v-model="config.pattern" placeholder="订单号[:：]\s*(\w+)" class="pp-mono" @input="touch" />
            </el-form-item>
            <el-form-item label="捕获组（0=整体匹配）">
              <el-input-number v-model="config.group" :min="0" style="width: 100%" @change="touch" />
            </el-form-item>
          </template>
        </template>

        <template v-else-if="node.type === 'collection'">
          <el-form-item label="操作">
            <el-select v-model="config.op" style="width: 100%" @change="touch">
              <el-option value="merge" label="合并（保留重复）" />
              <el-option value="union" label="并集（去重）" />
              <el-option value="intersect" label="交集" />
              <el-option value="diff" label="差集（第一个减去其余）" />
            </el-select>
          </el-form-item>
          <el-form-item label="列表（每行一个引用）">
            <el-input v-model="listsText" type="textarea" :rows="3" class="pp-mono" />
            <div class="pp-hint">示例：{{ listExample }}</div>
          </el-form-item>
        </template>

        <template v-else-if="node.type === 'batch'">
          <el-form-item label="待分批列表">
            <el-input v-model="config.items" type="textarea" :rows="2" placeholder="{{http_1.items}}" @input="touch" />
            <div class="pp-hint">{{ varHint }}</div>
          </el-form-item>
          <el-form-item label="每批数量">
            <el-input-number v-model="config.size" :min="1" :max="500" style="width: 100%" @change="touch" />
          </el-form-item>
        </template>

        <template v-else-if="node.type === 'text_template'">
          <el-form-item label="段落">
            <div v-for="(s, i) in sections" :key="i" class="pp-section">
              <div class="pp-row">
                <el-input v-model="s.title" placeholder="小标题（可空）" style="width: 130px" @input="touch" />
                <el-button size="small" type="danger" :icon="Delete" @click="removeItem(sections, i)" />
              </div>
              <el-input v-model="s.content" type="textarea" :rows="3" placeholder="{{llm_1.output}}" @input="touch" />
            </div>
            <el-button size="small" :icon="Plus" @click="sections.push({ title: '', content: '' }); touch()">添加段落</el-button>
            <div class="pp-hint">{{ varHint }}</div>
          </el-form-item>
        </template>

        <!-- ==================== 办公 ==================== -->
        <template v-else-if="node.type === 'email'">
          <el-form-item label="发送账号">
            <el-select v-model="config.account_id" clearable placeholder="选择个人邮箱账号" style="width: 100%" @change="touch">
              <el-option v-for="a in mailAccounts" :key="a.id" :value="a.id" :label="a.email_addr || a.label" />
            </el-select>
            <div class="pp-hint">需先在「邮件」页配置个人邮箱账号（含 SMTP）。</div>
          </el-form-item>
          <el-form-item label="收件人（逗号分隔）">
            <el-input v-model="config.to" placeholder="a@b.com,c@d.com" @input="touch" />
            <div class="pp-hint">{{ varHint }}</div>
          </el-form-item>
          <el-form-item label="主题">
            <el-input v-model="config.subject" @input="touch" />
          </el-form-item>
          <el-form-item label="正文">
            <el-input v-model="config.body" type="textarea" :rows="5" placeholder="{{text_1.output}}" @input="touch" />
          </el-form-item>
        </template>

        <template v-else-if="node.type === 'summarize'">
          <el-form-item label="待处理文本">
            <el-input v-model="config.text" type="textarea" :rows="3" placeholder="{{http_1.output}}" @input="touch" />
            <div class="pp-hint">{{ varHint }}</div>
          </el-form-item>
          <el-form-item label="任务类型">
            <el-select v-model="config.mode" style="width: 100%" @change="touch">
              <el-option value="summary" label="要点摘要" />
              <el-option value="extract" label="结构化抽取（输出 JSON）" />
              <el-option value="classify" label="分类（输出类别名）" />
              <el-option value="sentiment" label="情感倾向" />
            </el-select>
          </el-form-item>
          <el-form-item label="模型">
            <el-select v-model="config.model_config_id" clearable placeholder="默认模型" style="width: 100%" @change="touch">
              <el-option v-for="m in chatModels" :key="m.id" :value="m.id" :label="m.display_name || m.model_name" />
            </el-select>
          </el-form-item>
        </template>

        <template v-else-if="node.type === 'router'">
          <el-form-item label="分类结果">
            <el-input v-model="config.value" placeholder="{{摘要_1.output}}" @input="touch" />
            <div class="pp-hint">通常接「摘要抽取」的 classify 输出。匹配方式为「包含」。{{ varHint }}</div>
          </el-form-item>
          <el-form-item label="路由规则">
            <div v-for="(c, i) in routerCases" :key="i" class="pp-router-case">
              <div class="pp-row">
                <el-input v-model="c.handle" placeholder="出线名" style="width: 100px" @input="touch" />
                <el-button size="small" type="danger" :icon="Delete" @click="removeItem(routerCases, i)" />
              </div>
              <el-input
                :model-value="(c.values || []).join(',')"
                placeholder="匹配值，逗号分隔：退货,退款"
                @input="(v: string) => { c.values = v.split(',').map((x) => x.trim()).filter(Boolean); touch() }"
              />
            </div>
            <el-button size="small" :icon="Plus" @click="routerCases.push({ handle: '', values: [] }); touch()">添加规则</el-button>
            <div class="pp-hint">都不命中走 default 出线。</div>
          </el-form-item>
        </template>

        <!-- ==================== 集成 ==================== -->
        <template v-else-if="node.type === 'http_form'">
          <el-form-item label="URL">
            <el-input v-model="config.url" placeholder="https://..." @input="touch" />
            <div class="pp-hint">{{ varHint }}</div>
          </el-form-item>
          <el-form-item label="方法">
            <el-select v-model="config.method" style="width: 100%" @change="touch">
              <el-option value="POST" label="POST" />
              <el-option value="PUT" label="PUT" />
            </el-select>
          </el-form-item>
          <el-form-item label="编码">
            <el-radio-group v-model="config.encoding" @change="touch">
              <el-radio-button value="form">表单 form-data</el-radio-button>
              <el-radio-button value="json">JSON</el-radio-button>
            </el-radio-group>
          </el-form-item>
          <el-form-item label="提交字段">
            <div v-for="(f, i) in formFields" :key="i" class="pp-row">
              <el-input v-model="f.name" placeholder="字段名" style="width: 110px" @input="touch" />
              <el-input v-model="f.value" placeholder="值，可 {{引用}}" style="width: 150px" @input="touch" />
              <el-button size="small" type="danger" :icon="Delete" @click="removeItem(formFields, i)" />
            </div>
            <el-button size="small" :icon="Plus" @click="formFields.push({ name: '', value: '' }); touch()">添加字段</el-button>
          </el-form-item>
        </template>

        <template v-else-if="node.type === 'webhook'">
          <el-form-item label="回调 URL">
            <el-input v-model="config.url" placeholder="https://erp.example.com/hook" @input="touch" />
            <div class="pp-hint">{{ varHint }}</div>
          </el-form-item>
          <el-form-item label="推送字段">
            <div v-for="(f, i) in webhookFields" :key="i" class="pp-row">
              <el-input v-model="f.name" placeholder="字段名" style="width: 110px" @input="touch" />
              <el-input v-model="f.value" placeholder="值，可 {{引用}}" style="width: 150px" @input="touch" />
              <el-button size="small" type="danger" :icon="Delete" @click="removeItem(webhookFields, i)" />
            </div>
            <el-button size="small" :icon="Plus" @click="webhookFields.push({ name: '', value: '' }); touch()">添加字段</el-button>
          </el-form-item>
        </template>

        <template v-else-if="node.type === 'delay'">
          <el-form-item label="等待秒数（上限 300）">
            <el-input-number v-model="config.seconds" :min="0" :max="300" :step="1" style="width: 100%" @change="touch" />
            <div class="pp-hint">长等待会占住执行器，超过 30 秒建议改用定时任务。</div>
          </el-form-item>
        </template>

        <el-form-item v-if="node.type !== 'start' && node.type !== 'end'" label="失败重试次数（0=不重试）">
          <el-input-number v-model="config.retry" :min="0" :max="5" style="width: 100%" @change="touch" />
        </el-form-item>

        <el-divider style="margin: 10px 0" />
        <div class="pp-actions">
          <el-button type="primary" size="small" @click="commit">应用</el-button>
          <el-button size="small" type="danger" :icon="Delete" :disabled="node.type === 'start' || node.type === 'end'" @click="emitDelete">
            删除节点
          </el-button>
        </div>
      </el-form>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { Delete, Plus } from '@element-plus/icons-vue'
import { agentApi, channelApi, kbApi, mailApi, notifyChannelApi, providerApi, type Agent, type ChannelItem, type KB, type ModelConfig, type NotifyChannelItem } from '@/api'
import { NODE_META, type NodeMeta } from './meta'

interface PanelNode { id: string; type: string; data: any }

const props = defineProps<{ node: PanelNode | null; allNodeIds: string[] }>()
const emit = defineEmits<{ change: [oldId: string, newId: string, data: any]; delete: [id: string] }>()

const config = reactive<any>({})
const nodeId = ref('')
const argsText = ref('')

const kbs = ref<KB[]>([])
const models = ref<ModelConfig[]>([])
const agents = ref<Agent[]>([])
const notifyChannels = ref<NotifyChannelItem[]>([])
const extChannels = ref<ChannelItem[]>([])
const mailAccounts = ref<{ id: number; label?: string; email_addr?: string }[]>([])

const chatModels = computed(() => models.value.filter((m) => m.purpose === 'chat'))
const refExample = computed(() => `{{${nodeId.value || 'nodeId'}.output}}`)
const joinRef = computed(() => '{{本节点id.branches}}')
const varHint = computed(
  () => `可用变量：${props.allNodeIds.filter((i) => i !== nodeId.value).map((i) => `{{${i}.output}}`).join('、') || '（无上游）'}`,
)

function meta(type: string): NodeMeta {
  return NODE_META[type] || { label: type || '节点', color: '#999', icon: '?' }
}

function setConfig(src: any) {
  for (const k of Object.keys(config)) delete config[k]
  Object.assign(config, JSON.parse(JSON.stringify(src || {})))
  // switch / start / variable 等数组字段兜底
  if (props.node?.type === 'switch') config.cases = config.cases || []
  if (props.node?.type === 'start') config.inputs = config.inputs || []
  if (props.node?.type === 'variable') config.assignments = config.assignments || []
  argsText.value = typeof config.args === 'string' ? config.args : JSON.stringify(config.args || {}, null, 2)
  // 扩展节点的数组字段兜底：避免模板里直接 push 到 undefined
  ensureArray('filters', { field: '', op: 'eq', value: '' })
  ensureArray('fields', { name: '', from: '' })       // table / http_form / webhook 共用
  ensureArray('aggregates', { name: '', field: '', op: 'sum' })
  ensureArray('sections', { title: '', content: '' })
  ensureArray('mappings', { key: '', value: '' })
  ensureArray('cases', { handle: '', values: [] })
  ensureArray('lists', '')
  if (props.node?.type === 'table' && !config.sort) config.sort = { field: '', desc: false }
}

function ensureArray(key: string, seed: any) {
  if (Array.isArray(config[key])) return
  config[key] = seed === '' ? [] : [JSON.parse(JSON.stringify(seed))]
}

watch(
  () => props.node,
  (n) => {
    if (!n) return
    nodeId.value = n.id
    setConfig(n.data)
  },
  { immediate: true },
)

// ---- 数组字段编辑器 ----
const inputs = computed<any[]>(() => config.inputs || (config.inputs = []))
function addInput() { config.inputs.push({ name: '', type: 'string', required: false }); touch() }
function removeInput(i: number) { config.inputs.splice(i, 1); touch() }

const cases = computed<any[]>(() => config.cases || (config.cases = []))
function addCase() { config.cases.push({ handle: `case${(config.cases || []).length + 1}`, expr: '' }); touch() }
function removeCase(i: number) { config.cases.splice(i, 1); touch() }

const assignments = computed<any[]>(() => config.assignments || (config.assignments = []))
function addAssign() { config.assignments.push({ name: '', value: '' }); touch() }
function removeAssign(i: number) { config.assignments.splice(i, 1); touch() }

// ---- 扩展节点：通用数组字段与文本桥接 ----
function removeItem(arr: any[], i: number) { arr.splice(i, 1); touch() }

const filters = computed<any[]>(() => config.filters || (config.filters = []))
const aggregates = computed<any[]>(() => config.aggregates || (config.aggregates = []))
const sections = computed<any[]>(() => config.sections || (config.sections = []))
const mappings = computed<any[]>(() => config.mappings || (config.mappings = []))
const routerCases = computed<any[]>(() => config.cases || (config.cases = []))
// 提示文案里要显示 {{x}} 字面量，模板插值会解析它，故在 script 里拼好
const listExample = '{{a_1.items}} 与 {{b_1.items}} 各占一行'

// table/http_form/webhook 的字段列表语义不同，但结构一致（name/from 或 name/value）
const fieldMaps = computed<any[]>(() => config.fields || (config.fields = []))
const formFields = computed<any[]>(() => config.fields || (config.fields = []))
const webhookFields = computed<any[]>(() => config.fields || (config.fields = []))

// collection.lists 用文本编辑（每行一个 {{引用}}），与数组互转
const listsText = computed({
  get: () => (config.lists || []).join('\n'),
  set: (v: string) => { config.lists = v.split('\n').map((x) => x.trim()).filter(Boolean); touch() },
})

function setBodyOp(op: string) {
  config.body = { ...(config.body || {}), op }
  touch()
}

function onArgsInput() {
  try {
    config.args = JSON.parse(argsText.value)
  } catch {
    config.args = argsText.value
  }
  touch()
}

let dirty = false
function touch() { dirty = true }

function commitId() {
  const id = nodeId.value.trim()
  if (!id) { ElMessage.warning('节点 ID 不能为空'); nodeId.value = props.node?.id || ''; return }
  if (props.node && id !== props.node.id && props.allNodeIds.includes(id)) {
    ElMessage.error(`节点 ID "${id}" 已存在`)
    nodeId.value = props.node.id
  }
}

function commit() {
  if (!props.node) return
  commitId()
  if (!nodeId.value.trim()) return
  if (nodeId.value !== props.node.id && props.allNodeIds.includes(nodeId.value)) return
  emit('change', props.node.id, nodeId.value.trim(), JSON.parse(JSON.stringify(config)))
  dirty = false
  ElMessage.success('已应用')
}

function emitDelete() {
  if (props.node) emit('delete', props.node.id)
}

onMounted(() => {
  kbApi.list().then((v) => (kbs.value = v)).catch(() => {})
  providerApi.configs().then((v) => (models.value = v)).catch(() => {})
  agentApi.list().then((v) => (agents.value = v.filter((a) => a.type === 'agent'))).catch(() => {})
  notifyChannelApi.list().then((v) => (notifyChannels.value = v)).catch(() => {})
  channelApi.list().then((v) => (extChannels.value = v)).catch(() => {})
  mailApi.listAccounts().then((v) => (mailAccounts.value = v)).catch(() => {})
})
</script>

<style scoped>
.pp { font-size: 13px; }
.pp-empty { color: var(--text-muted); font-size: 13px; padding: 16px; }
.pp-title { font-weight: 600; margin-bottom: 12px; }
.pp-hint { font-size: 11px; color: var(--text-muted); line-height: 1.5; margin-top: 4px; }
.pp-note { font-size: 12px; color: var(--text-secondary); line-height: 1.6; }
.pp-row { display: flex; gap: 6px; margin-bottom: 6px; align-items: center; }
.pp-mono :deep(textarea) { font-family: 'SFMono-Regular', Consolas, monospace; font-size: 12px; }
.pp-actions { display: flex; gap: 8px; }
.pp-body :deep(.el-form-item) { margin-bottom: 12px; }
</style>
