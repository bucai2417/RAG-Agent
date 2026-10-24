# 企业级 RAG 知识库智能体平台(源码非正式版)

**当前版本：v1.0.0**

可对接企业内部系统、也供员工直接使用，具备多权限体系、智能体与自动化能力的企业级 RAG 平台。

推荐另外一个项目，Agent基础框架，基于 LangChain 1.x + LangGraph 1.x等，可作为底座：https://github.com/241793/Agent-Framework

后端 FastAPI + SQLAlchemy 2.0 async，前端 Vue 3 + TypeScript + Vite + Element Plus，**默认 SQLite 零依赖起步**，可平滑切换 PostgreSQL(+pgvector)/MySQL。
<div>
<img width="1900" height="864" alt="b3f131b9-495f-474d-ba72-85c43c11da73" src="https://github.com/user-attachments/assets/42638f2f-c107-42c1-a08f-3d5a75590350" />
  <img width="1910" height="873" alt="image" src="https://github.com/user-attachments/assets/46a7f163-5355-4839-aa17-c6dd6e4ab2b1" />

</div>
---

## 功能全景

### 一、知识库与问答

- **三类知识库**（同级，建库时选类型）：
  - **向量知识库**：上传 PDF/Office/图片等文档，自动解析 → 分块 → 向量化 → 检索
  - **图文知识库**：直接录入「一段文字 + 配套图片/附件」，问题命中后**自动把配套文件发给提问者**（无需向量模型，走关键词检索）
  - **外部知识库**：对接 Dify / RAGFlow / FastGPT / 通用 HTTP / MCP / SQL 库 / 飞书表格 / Google Sheets，检索时实时联邦调用并融合排序
- **入库流水线**：多格式解析、父子分块/定长/递归分块、可配向量模型；失败可重灌、支持分块级编辑（改/删/拆分）
- **混合检索**：向量召回 + BM25 关键词 + RRF 融合（三路权重可调）+ 可选重排；**MMR 去冗余**提升结果多样性；**多轮追问改写**把「它/这个」等指代句结合历史压成独立 query；带**检索调试台**观察命中与分数
- **流式问答 + 引用溯源**：答案标注引用编号，可点开原文；区分「知识库资料」与「模型自身知识」防幻觉
- **内容巡检与未命中归因**：知识库健康检查（空/失败/无分块/未标签/陈旧文档）+ 高频未命中 query 聚合，便于补内容
- **问答质量评估**：建评测集（问题 + 标准答案 + 期望文档）→ 批量跑 RAG → **AI 裁判打分**（忠实度/相关性）+ 命中率 → 导出 CSV

### 二、智能体（Agent）平台

- **ReAct 工具循环**：LLM 自主多轮调用工具直到得出答案，运行过程可视化（可折叠的调用卡片）
- **工具生态**：15 个常驻内置工具（知识检索 / HTTP / 文件读写 / 表格分析 / 办公文档生成 / 待办 / 会话管理 …）+ 96 个管理运维工具
  - 问答页按需装配工具（`find_tools` 工具路由），省 token、更快
- **技能（Skill）**：能力包（提示词+工具+知识库）与 HTTP 工具两种形态；支持**技能集合**——把多技能仓库整体导入，AI 按需加载子技能（progressive disclosure）
  - **多运行时脚本**：技能脚本支持 Python / Node / Shell（按扩展名或 `skill.yaml` 清单识别），Python 脚本可用第三方库；依赖一键安装（pip / npm ci）、产物自动回传、环境变量白名单隔离与资源限额
- **技能市场**：`find_skill` 从 GitHub 实时搜索并一键安装技能包
- **行为模式（AgentMode）**：同一智能体多套行为，运行时切换
- **工作流编排（DAG）**：15 类节点（start / llm / 检索 / 条件 / **多路分支 switch** / HTTP / 文件 / 变量 / 代码 / 循环 / **人工审批** / 协作智能体 / 消息推送 / **并行网关 parallel** / 汇聚 join / end）+ 变量引用 + 可视化画布（自绘）+ 发布/版本回滚

### 三、外部渠道与客服

- **IM 渠道接入**：QQ 机器人、微信（iLink 协议，扫码登录）、企业微信、飞书——收发消息、群聊、图片/文件
- **客服工单**：渠道转人工自动建单；分类/标签/优先级/SLA 超时告警/满意度评价；客服工作台查看双方消息（含附件）、直接回复回发渠道、发附件、客户备注
- **话术库**：预存快捷回复，客服一键引用；工单批量操作（关单/改派/改优先级/打标签）
- **渠道身份绑定**：把外部渠道身份绑定到内部账号，绑定后**继承该账号真实权限**（管理员=全权，外部客户=只读）
- **客服网关 API**：API Key 认证的对外接口，外部系统可建单/查单/追加/评价
- **多语言客服**：`/lang` 切换回答语言，或按渠道默认语言

### 四、办公与自动化

- **智能录单**：定义模板 → AI 从通话记录/聊天/邮件抽结构化台账 → 导出 Excel/CSV
- **办公文档生成**：会议纪要 / 周报日报 / 待办清单 / 公文通知（docx/pdf/xlsx/md）
  - 专业排版：DOCX 封面/页眉页脚/页码/目录、XLSX 多工作表/列宽自适应/数字格式/图表（柱状/折线/饼）、PDF 封面/页眉/页码/跨页表头
  - PPTX 自绘设计版式（封面/章节/要点/两栏/引用/表格/结尾 + 主题色）
- **待办日程**：待办/日程/提醒，支持到期推送、重复规则（cron）、提前提醒；AI 可直接代建/代查
- **审批中心**：集中处理工作流中的人工审批节点，支持批准/拒绝并填写意见
- **对话导出/分享**、文档批量合并导出
- **邮件入库**：配置 IMAP，邮件正文与附件自动入库知识库（可直接选用个人邮箱，免重复配置）
- **个人邮件**：用户级邮箱收发（IMAP 收 + SMTP 发），收件箱/已发送/已删除、可选自动入库；**AI 可代管**（读信/写信/回复，发信需确认）
- **定时任务**：cron / 间隔 / 一次性 / **事件触发**；失败重试、任务级通知；支持**依赖链**（A 成功后自动触发 B）；**执行产物随通知附带**（如生成的报表作为邮件附件）
- **事件订阅 Webhook**：外部系统订阅平台事件（工单/文档/工作流…），带 HMAC 签名回调
- **通知渠道**：站内 / Webhook / 企业微信 / 钉钉 / 邮件 / 外部渠道（把已接入的 QQ/微信/企微/飞书兼作推送出口）

### 五、平台能力

- **MCP 客户端**：接入 http / sse / stdio 三类 MCP 服务器，工具自动注册为 AI 工具
- **模型管理**：统一 Provider 抽象（OpenAI 兼容 / Anthropi Claud / Ollama / 本地 bge / 离线 local_hash），每个 Provider 可配 chat / embedding / rerank，含连通性测试
- **RBAC + 文档级 ACL**：用户 / 角色 / 部门 / 用户组四级授权；知识库可见性（公开/内部/私有）+ 文档级例外授权 + 部门向下继承
- **审计与日志**：写操作审计、系统运行日志、内容安全检测
- **用量统计**：按模型/用户/天聚合 token 消耗；**工具调用统计**（次数/成功率/耗时）
- **API 密钥**：OpenAI 兼容接口，供外部系统调用平台
- **单点登录（SSO）**：支持 **通用 OIDC** / 企业微信 / 钉钉 / 飞书（OIDC + PKCE）。
  在「单点登录」页配置并启用后，登录页会自动出现对应按钮；首次登录可自动建号（JIT）
  并按配置分配默认角色。未配置时登录页不显示该区域，不影响账号密码登录
- **系统设置**：Web 端热改配置（含启动期项 + 重启按钮）
- **桌面版**：Electron 打包（`desktop/`）

---

## 技术栈

| 层 | 选型 |
|---|---|
| 后端 | FastAPI + SQLAlchemy 2.0 async + Uvicorn |
| 数据库 | **默认 SQLite**（零依赖），可切换 **PostgreSQL(+pgvector)** / **MySQL** |
| 向量检索 | pgvector（PG）/ numpy 应用层（SQLite/MySQL），自动切换 |
| 任务队列 | 进程内 asyncio 队列（零依赖），可换 Celery |
| 前端 | Vue 3 + TypeScript + Vite 6 + Element Plus + Pinia（工作流画布自绘 / ECharts 图表） |
| 模型 | 统一 Provider 抽象层：OpenAI 兼容 / Anthropi Claud / Ollama / 本地 bge / 离线 |
| 桌面 | Electron 33 + electron-builder |

---

## 快速开始

> **统一入口**：后端监听 **6677** 端口，并同时托管前端构建产物。浏览器只需访问 `http://127.0.0.1:6677` 一个地址。

### 一键启动（推荐）

```bash
start.bat
```

自动完成：初始化数据库 → 构建前端 → 启动服务。然后打开 http://127.0.0.1:6677 ，登录 **admin / admin123**。

### 账号与注册

- **默认管理员**：`admin / admin123`（首次登录后请及时改密码）
- **自助注册默认关闭**：公网部署时避免被随意注册、灌满审核队列。
  需要开放时到「**系统设置 → 安全**」打开「开放自助注册」，登录页随即出现「立即注册」入口
- 注册后默认**需管理员审核**（可在同一处关闭），管理员在「用户管理」里通过后即可登录
- 相关开关（均在「系统设置 → 安全」，改完立即生效）：
  `开放自助注册` / `注册后需管理员审核` / `登录限流(次/分钟/IP)` /
  `连续失败锁定阈值` / `锁定时长(分钟)`

### 手动启动

```bash
# 1. 后端准备
cd backend
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -r requirements.txt   # Windows
cp .env.example .env
./.venv/Scripts/python.exe scripts/seed.py

# 2. 构建前端（产物进 web/dist，由后端托管）
cd ../web
npm install
npm run build

# 3. 启动统一服务
cd ../backend
./.venv/Scripts/python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 6677
```

访问 http://127.0.0.1:6677 （前端），http://127.0.0.1:6677/docs （API 文档）。

### 前端开发模式（可选）

```bash
cd web && npm run dev
```

Vite 跑在 5173 并把 `/api` 代理到 6677。改完后 `npm run build` 让后端托管最新产物。

---

## 构建与部署

### Docker（服务器 / 内网部署）

```bash
# 首次启动：直接用 GHCR 发布的镜像（不本地构建）
docker compose up -d

# 应用 + PostgreSQL(pgvector)
docker compose --profile pg up -d      # 并把 INSTALL_PG 设为 true 后重建镜像

# 本地改代码时自己构建
docker compose --profile build up -d --build

# 指定版本
RAG_IMAGE=ghcr.io/241793/rag-agent:1.0.1 docker compose up -d
```

**目录结构**：`./rag-web` 单目录挂载到容器 `/app`，包含

```
rag-web/
├── backend/          # 源码（在线更新会覆盖）
│   └── data/         # ★ 数据：SQLite 库 + 上传文件 + 技能包（升级不丢）
├── web/dist/         # 前端产物（在线更新会覆盖）
└── updates/          # 更新暂存与备份
```

首次启动时若 `./rag-web` 为空，容器会**自动从镜像内置的种子副本初始化**（`docker/entrypoint.sh`），无需手工准备。

> **从旧版本升级注意**：v1.0.0 的数据在 `./data`，v1.0.1 起在 `./rag-web/backend/data`。
> 升级前请迁移：`mkdir -p rag-web/backend && mv data rag-web/backend/data`

> **中文字体**：镜像内已装 `fonts-wqy-zenhei`。AI 生成 PDF 走 reportlab 嵌入字体，若基础镜像缺中文字体，PDF 里的中文会**静默变成乱码**（`I`/`II` 之类的占位符）。reportlab 只接受 **TrueType** 轮廓字体，因此**不能用 `fonts-noto-cjk`**（它是 CFF/PostScript 轮廓，会被直接拒绝）。自托管字体可用 `RAG_PDF_FONT` / `RAG_PDF_FONT_BOLD` 环境变量指定。

### 桌面版（终端用户 / Windows）

```bash
# 1. 先构建前端（PyInstaller 依赖 web/dist，缺了会打出无界面的 exe）
cd web && npm install && npm run build

# 2. 打包后端（onedir）
cd ../backend && pyinstaller build_backend.spec --noconfirm

# 3. 打包 Electron 桌面版
cd ../desktop && npm install && npm run dist
# 产物：desktop/release/win-unpacked/RAG知识库.exe
# 单文件便携版：npm run dist:portable
```

桌面版数据写在 `%APPDATA%/RAG知识库`，不污染安装目录。

### CI / 自动发布（GitHub Actions）

| 工作流 | 触发 | 做什么 |
|---|---|---|
| `.github/workflows/ci.yml` | push main / PR | 后端 `pytest` + 前端 `vue-tsc` 类型检查与构建 |
| `.github/workflows/release.yml` | 推 `v*` tag / 手动 | 校验版本号 → 推 Docker 镜像到 GHCR → 产出 3 个 zip → 发 GitHub Release |

**Release 产物**：

| 产物 | 用途 |
|---|---|
| `*-win-x64.zip` | 桌面版（Windows，解压双击 `RAG知识库.exe`） |
| `*-docker.zip` | **Docker 在线更新包**（后端源码 + 已构建前端，约 2 MB） |
| `*-src.zip` | 纯源码 |

**Release 正文**由 CI 从 `CHANGELOG.md` 提取该版本段落**原样写入**，
所以发版前必须在 `CHANGELOG.md` 顶部补齐对应版本段落，否则 CI 会**拒绝发版**。

发版流程：

```bash
# 1. 改版本号（4 处需一致）
#    backend/app/core/config.py 的 app_version
#    backend/pyproject.toml / web/package.json / desktop/package.json
# 2. 在 CHANGELOG.md 顶部写该版本段落
# 3. 提交并打 tag
git tag -a v1.0.2 -m "v1.0.2"
git push origin v1.0.2
```

> 首次推送到 GHCR 的包默认是 **private**，需在 GitHub Packages 页面手动改为 public；再次推送沿用该可见性。

### 应用内更新

侧边栏底部显示当前版本，点击打开「关于 / 检查更新」，可查看新版本与**更新内容**（来源见下）。

| 部署形态 | 能否应用内更新 | 方式 |
|---|---|---|
| **桌面版** | ✅ | 下载 → sha256 校验 → 换盘 → 自动重启 |
| **Docker** | ✅ | 下载源码包覆盖 `/app` → `pip install` → 退出进程 → compose 自动拉起 |
| **源码运行** | ✅ | 下载源码包覆盖 `backend/` 与 `web/dist/` → `pip install` → `os.execv` 原地重启 |

> 源码运行更新时**只替换代码与前端产物**，`backend/data/`（数据库、上传文件）、`.env`、`.venv` **一律不动**；失败自动从备份回滚。

#### 更新内容从哪来

```
CHANGELOG.md（仓库根，你维护）
      ↓ CI 在发版时提取该版本段落
GitHub Release 正文
      ↓ 后端读取
应用内「检查更新」弹窗 + 顶部更新日志滚动条
```

所以**只需维护 `CHANGELOG.md`**，三处展示自动一致。

#### 桌面版更新流程

Windows 不允许覆盖正在运行的 exe，所以换盘由应用退出后的外部脚本执行：

```
点「立即更新」→ 流式下载 zip → 校验 sha256 → 解压到 …\updates\staging\vX.Y.Z\
  → 点「重启并应用」→ 写 …\updates\apply.requested
  → Electron 拉起 apply_update.bat → 退出（释放 exe 文件锁）
  → 脚本等进程退出 → 备份 → robocopy 覆盖 → 启动新版
  → 失败则还原 .bak 并启动旧版（不会变砖）
```

用户数据在 `%APPDATA%\RAG知识库\data`，与安装目录分离，**更新不影响数据**。

#### Docker 更新流程

```
点「立即更新」→ 下载 *-docker.zip（backend 源码 + 已构建的 web/dist）
  → 校验 sha256 → 解压到 /app/updates/staging/
  → 点「重启并应用」→ 写 /app/updates/apply.requested
  → 容器内看门狗：备份 → 覆盖 backend/ 与 web/dist → pip install → os._exit(0)
  → docker compose 的 restart: unless-stopped 拉起新版本
  → 任一步失败自动从备份回滚
```

数据在 `./rag-web/backend/data`，**更新与重建容器都不会丢失**。

> 也可在宿主机用镜像方式升级：`docker compose pull && docker compose up -d`
> （两种方式等价；应用内更新适合不能随意连外网拉镜像的场景。）

#### 源码运行更新流程

```
点「立即更新」→ 下载 *-docker.zip（backend 源码 + 已构建的 web/dist）
  → 校验 sha256 → 解压到 backend/updates/staging/
  → 点「重启并应用」→ 备份 backend/ 与 web/dist → 覆盖 → pip install
  → os.execv 原地重启（约 10 秒）
  → 任一步失败自动从备份回滚
```

**不会被覆盖的内容**：`backend/data/`（数据库与上传文件）、`.env`、`.venv`。
即使更新包内误带了 `data/`，也会被跳过 —— 由测试固化。

> 前提：进程需有项目目录写权限；用 `start.bat` 或直接 `uvicorn` 启动均可。
> `start.bat` 本身无需改动 —— 它只是启动脚本，更新由运行中的服务完成。

相关设置：`update_check_enabled` / `update_repo`（系统设置 → 高级）。

> **注意**：v1.0.0 是引入本功能前的版本，从 v1.0.0 升级到 v1.0.1 仍需手动替换；
> **自 v1.0.1 起**应用内更新才生效。
> 无代码签名证书时，新版 exe 首次启动会有 SmartScreen「未知发布者」提示，属预期。

### 更新日志（滚动条）

顶部 header 下方有一条**横向滚动**的更新日志，**所有已登录用户**可见（点击展开多版本历史）。

内容来源（本地优先，GitHub 兜底）：

| 顺序 | 来源 | 说明 |
|---|---|---|
| 1 | `CHANGELOG.md`（仓库根） | 打进桌面版包内、COPY 进 Docker 镜像 → **内网/离线也能显示** |
| 2 | GitHub Release 正文 | 本地读不到时回退（正文由 CI 从 `CHANGELOG.md` 同名段落提取，不含安装命令） |

**发版时你要做的**：在 `CHANGELOG.md` 顶部按 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)
格式加一段。CI 会校验 tag 与 `app_version` 一致**且** `CHANGELOG.md` 含该版本段落，缺失则发版失败。

滚动条按版本记忆关闭状态（`localStorage: changelog_dismissed_<版本>`），下个版本自动重新出现。

---

## 配置模型（关键）

首次打开后，进入「**模型管理**」添加 Provider 与模型配置。支持以下类型：

| 类型 | 说明 | base_url 示例 |
|---|---|---|
| `openai` | OpenAI 兼容（含中转站、vLLM） | `https://api.xxx.com/v1` |
| `anthropi` | Anthropi Claud | `https://api.anthropi.com` |
| `ollama` | 本地 Ollama | `http://localhost:11434/v1` |
| `local_bge` | 本地 bge（需 `pip install FlagEmbedding`） | 模型路径 |
| `local_hash` | 离线自测（无需网络） | `local` |

每个 Provider 下可配置多种用途：**chat / embedding / rerank**。
embedding 维度需与知识库一致（bge-m3=1024，text-embedding-3-small=1536）。

> 也可在 `.env` 中预置默认 Provider，`seed.py` 会自动写入。

---

## 切换数据库（数据可迁移）

编辑 `backend/.env` 的 `DATABASE_URL`：

```ini
# SQLite（默认）
DATABASE_URL=sqlite+aiosqlite:///./data/rag.db
# PostgreSQL
DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/rag
# MySQL
DATABASE_URL=mysql+asyncmy://root:root@localhost:3306/rag
```

切换后安装对应驱动并重建表：

```bash
pip install asyncpg pgvector    # PostgreSQL
pip install asyncmy             # MySQL
```

向量后端 `VECTOR_BACKEND=auto` 会自动选择：PostgreSQL 用 pgvector 原生检索，其余用 numpy 应用层余弦。

---

## 权限体系

- **租户隔离**：所有数据带 `tenant_id`。
- **权限过滤唯一入口**：`backend/app/services/permission.py` —— 所有检索的权限条件都由此生成，避免越权召回（企业 RAG 最高危点）。
- **知识库可见性**：`public`（全租户）/ `internal`（按成员）/ `private`（仅显式成员）。
- **文档级 ACL**：知识库之上的例外授权（allow/deny），支持部门向下继承。
- **principal 统一编码**：`PUBLIC=0`、`USER=1e12+id`、`DEPT=2e12+id`、`ROLE=3e12+id`、`GROUP=4e12+id`。用户 / 角色 / 部门 / 用户组四级授权均已启用。
- **渠道身份绑定**：外部渠道身份可绑定到内部账号，绑定后继承真实权限。

---

## 目录结构

```
backend/
  app/
    core/          # 配置/数据库/安全/日志/错误
    models/        # SQLAlchemy ORM（63 张表）
    schemas/       # Pydantic DTO
    api/v1/        # 36 个路由模块（kb/document/chat/agents/channels/service/…）
    services/      # permission(权限唯一入口)/retrieval/chat/eval/service_ticket/skill_runtime/…
    agents/        # 智能体运行时：react 循环 / tools / workflow 引擎
    providers/     # 模型抽象层：base/registry/drivers/*
    retrieval/     # vector_store/bm25/fusion
    ingest/        # storage/parsers/chunkers(含父子分块)
    channels/      # IM 渠道驱动（qqbot/wxclaw/wework/feishu）
    connectors/    # 外部知识库连接器
    notifiers/     # 通知出口
    tasks/         # 进程内队列 + 入库/调度/评测/SLA 等后台任务
  scripts/         # seed.py(初始化) / 自测脚本
  data/            # SQLite 库 + 上传文件（gitignore）
web/               # 前端（42 个页面）
desktop/           # Electron 桌面版
```

---

## 常见问题

**Q: 中文关键词检索效果一般？**
默认用应用层按字分词。PostgreSQL 可启用 `zhparser` 扩展增强中文 BM25；未启用时向量单路检索仍可用。

**Q: 换 embedding 模型后检索异常？**
向量空间不兼容，需重建该知识库的向量（切换模型后重新上传文档）。

**Q: 对话无输出？**
检查「模型管理」是否配置了 chat 模型，或先用 `local_hash` 离线驱动验证链路。

**Q: 图文知识库报「向量模型」错误？**
图文知识库**不需要**向量模型，走关键词检索；若提示向量相关错误，确认库类型为「图文知识库」而非「向量知识库」。

---

## License

[MIT](LICENSE)
