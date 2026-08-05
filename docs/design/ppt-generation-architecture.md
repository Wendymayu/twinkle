# jiuwenswarm PPT 生成架构深度分析

> 来源：jiuwenclaw `enterprise_dev` 分支，`jiuwenclaw/agentserver/skill_turbo/skill_codes/ppt/` 目录
> 日期：2026-08-03

## 1. 总体架构：SkillTurbo 流水线 + pptx-craft 外部 Skill

PPT 生成系统由 **两个独立组件** 协同工作：

1. **SkillTurbo 流水线**（Python）—— 编排层，决定"做什么"和"怎么做"
2. **pptx-craft Skill**（Node.js）—— 执行层，提供"能力"（CLI 工具、模板、风格定义）

```
┌─────────────────────────────────────────────────────────────────┐
│  SkillTurbo 流水线 (Python)                                     │
│  jiuwenclaw/agentserver/skill_turbo/skill_codes/ppt/            │
│                                                                 │
│  ┌─────────┐  ┌─────────┐  ┌─────────┐  ┌─────────┐          │
│  │ P0 Init │→ │ P1 Intent│→ │ P3 Doc  │→ │ P2 Req  │  ...     │
│  └─────────┘  └─────────┘  └─────────┘  └─────────┘          │
│       │                                              │          │
│       │  通过 inputs["pptx_root"] 定位 pptx-craft     │          │
│       │  通过 bash 工具调用 pptx-craft CLI             │          │
│       ▼                                              ▼          │
│  ┌──────────────────────────────────────────────────────┐      │
│  │  pptx-craft Skill (Node.js)                         │      │
│  │  office-claw-skills/pptx-craft/                     │      │
│  │                                                      │      │
│  │  packages/cli/     → CLI 工具 (convert, check, fix)  │      │
│  │  references/       → 风格定义 + 设计规范              │      │
│  │  image-insert/     → 图片处理脚本                     │      │
│  │  package.json      → npm 依赖 (playwright, jszip)    │      │
│  └──────────────────────────────────────────────────────┘      │
└─────────────────────────────────────────────────────────────────┘
```

### 1.1 pptx-craft 是什么？

**pptx-craft 是一个独立的 OfficeClaw Skill**，代码仓库位于 `office-claw-skills/pptx-craft/`。它是一个包含 SKILL.md + Node.js 代码的完整技能目录，通过 `JIUWENCLAW_SHARED_SKILLS_DIRS` 环境变量注册到系统中。

**关键事实**：pptx-craft **不是** Python 库，**不使用 python-pptx**。它是一个 Node.js 工具链，通过 Playwright 渲染 HTML → 截图/提取矢量 → JSZip 组装 PPTX。

### 1.2 pptx-craft 并未被当作 Skill 使用

这是理解整个架构的关键点：**pptx-craft 虽然以 Skill 目录结构存在，但 SkillTurbo 流水线并没有把它当 Skill 来用。**

在 jiuwenswarm 的架构里，一个"Skill"的标准用法是：

```
Agent Loop → SkillManager 发现 SKILL.md → 读取指令 → Agent 按 SKILL.md 的指示行动
```

但 SkillTurbo 流水线里，pptx-craft 的 **SKILL.md 从头到尾都没被读过**。它只是恰好被放在了 Skill 目录结构里（`office-claw-skills/pptx-craft/`），通过 `JIUWENCLAW_SHARED_SKILLS_DIRS` 注册到系统中，但 SkillTurbo 只用了它的：

1. **CLI 工具**（`packages/cli/dist/cli.js`）— 通过 bash 调用
2. **静态资源**（`references/styles/`、`references/designer.md`）— 通过 read_file 读取
3. **npm 依赖**（`package.json`）— 通过 npm install 安装

它本质上是一个 **Node.js 工具包**，只不过套了一个 Skill 的目录壳。SkillTurbo 把它当成了一个"外部工具箱"来用，而不是一个"让 Agent 按指令行事的 Skill"。

反过来说，**SkillTurbo 流水线自身的 Python 代码**（`skill_codes/ppt/` 下的 14 个 PlanNode）才是真正承担"Skill"角色的东西——它定义了整个 PPT 生成的流程、决策和内容生成逻辑。

```
传统 Skill 用法（pptx-craft 没走这条路）：
  Agent Loop → SkillManager → 读取 SKILL.md → Agent 自主行动

实际发生的事（SkillTurbo 的做法）：
  SkillTurbo Planner → 路由到 Python PlanNode 树 → PlanNode 调用 pptx-craft CLI → pptx-craft 作为工具箱被动执行
```

**对比**：

| | 传统 Skill | pptx-craft 在 SkillTurbo 中的角色 |
|---|---|---|
| **SKILL.md** | 核心入口，定义指令和流程 | 从未被读取 |
| **谁做决策** | Agent Loop 根据 SKILL.md 指示决策 | Python PlanNode 代码硬编码所有决策 |
| **谁做内容生成** | Agent Loop 调 LLM | Python PlanNode 调 LLM |
| **谁做执行** | Agent Loop 调工具 | pptx-craft CLI 作为工具被 bash 调用 |
| **角色** | Agent 的"大脑" | 工具箱（"双手"） |

**pptx-craft 的核心能力**：
| 能力 | 入口 | 说明 |
|------|------|------|
| HTML→PPTX 转换 | `cli.js convert` | 用 Playwright 渲染 HTML，提取矢量图形，组装 PPTX |
| 环境检测 | `cli.js check-env` | 检测 Node/npm/playwright 是否就绪 |
| 工作区管理 | `cli.js generate-timestamp-dir` / `ensure-output-dir` | 创建输出目录和 pages 子目录 |
| 页面质量检查 | `cli.js check` | 校验 HTML 页面是否符合规范 |
| 页面自动修复 | `cli.js fix` | 修复常见问题（字号、溢出、布局） |
| 研究质量校验 | `cli.js validate-research` | 校验研究内容的完整性和质量 |
| 演讲备注 | `cli.js notes extract-text` / `inject` | 从 PPTX 提取文本 / 注入备注 |
| 模板 DNA 快照 | `cli.js snapshot-template-dna` | 模板画布模式的 DNA 比对 |
| 产物终检 | `cli.js check-pptx-artifact` | 导出后的 PPTX 产物校验 |
| 风格定义 | `references/styles/{style_id}/style.md` | 预设风格的 Markdown 规范 |
| 设计规范 | `references/designer.md` | 页面内容预算、布局规范 |
| AI 图片计划 | `cli.js stage-ai-image` | AI 生图计划和执行 |

**npm 依赖**：
- `playwright` (1.57.0) — 无头浏览器渲染 HTML
- `jszip` (3.10.1) — PPTX 文件结构操作（PPTX 本质是 ZIP of XML）
- `commander` (13.0.0) — CLI 框架
- `express` (5.2.1) — 本地 HTTP 服务器
- `get-port` (7.2.0) — 端口分配

### 1.2 两者如何连接？

**连接机制**：SkillTurbo 通过 `inputs["pptx_root"]` 定位 pptx-craft 目录，通过 `bash` 工具调用其 CLI。

```
┌─ SkillTurbo (Python) ─────────────────────────────────────────┐
│                                                                 │
│  P0.1 PipelineInit:                                             │
│    inputs["pptx_root"] = _resolve_pptx_root(inputs)            │
│    # 解析路径: skill_root + skill_name → pptx-craft 目录        │
│    # 例: /opt/skills/pptx-craft/                                │
│                                                                 │
│    await _bash(node, "npm install", workdir=pptx_root)         │
│    await _bash(node, "npx playwright install chromium", ...)    │
│                                                                 │
│  P0.2 WorkspaceInit:                                            │
│    cli_path("generate-timestamp-dir", pptx_root)               │
│    # → "node /opt/skills/pptx-craft/packages/cli/dist/cli.js   │
│    #     generate-timestamp-dir"                                │
│    cli_path("ensure-output-dir", pptx_root)                    │
│                                                                 │
│  P7 StylePrepare:                                               │
│    # 读取预设风格文件                                            │
│    read_file(pptx_root/references/styles/business-classic/      │
│              style.md)                                          │
│                                                                 │
│  P9 PPTExport:                                                  │
│    cli_path("convert", pptx_root)                               │
│    # → "node cli.js convert {pages_dir} {pptx_path}"           │
│                                                                 │
│  P11 SpeakerNotes:                                              │
│    cli_path("notes", pptx_root) + "extract-text"               │
│    cli_path("notes", pptx_root) + "inject"                     │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

**路径解析链**：

```
JIUWENCLAW_SHARED_SKILLS_DIRS 环境变量
  → SkillTurboEnvironment._resolve_skill_root()
    → skill_root = "/opt/skills"  (包含多个 skill 的根目录)
      → pptx_root = skill_root + "/pptx-craft"  (具体 skill 目录)
        → cli.js = pptx_root + "/packages/cli/dist/cli.js"
        → styles = pptx_root + "/references/styles/"
        → designer = pptx_root + "/references/designer.md"
```

**安全校验**：pptx-craft 目录有 SHA256 校验和（`skill_checksum`），在 `SkillTurboEnvironment._scan_skills_dir` 中验证，防止 skill 被篡改。

### 1.3 为什么不用 python-pptx？

| 方案 | 优势 | 劣势 |
|------|------|------|
| **python-pptx** | Python 原生，无需外部依赖 | 布局能力有限，难以实现复杂视觉设计；图表需要手动构建 XML |
| **HTML→PPTX** (pptx-craft) | CSS 自由布局，ECharts 图表，Tailwind 样式，所见即所得 | 需要 Node.js + Playwright，依赖链长；HTML→PPTX 转换有边界限制 |

**pptx-craft 选择 HTML→PPTX 的核心原因**：
1. **CSS 布局能力**：Flexbox + Tailwind 可以实现复杂的卡片、图表、数据可视化布局
2. **ECharts 图表**：直接在 HTML 中嵌入交互式图表，转为 PPTX 后保留矢量图形
3. **设计一致性**：HTML+CSS 可以精确控制 1280×720px 画布，确保每个元素位置精确
4. **迭代效率**：HTML 可以在浏览器中预览调试，比 python-pptx 的 XML 操作直观得多

**但 HTML→PPTX 转换带来了限制**：
- 不支持 CSS Grid（html-to-pptx 转换器限制）
- 不支持 `overflow-hidden`（PPTX 导出不尊重 CSS overflow）
- ECharts 必须用 SVG renderer（Canvas 会导致位图化）
- 图表颜色不能用渐变（会导致位图化）
- padding 缩放 0.85、border-width 缩放 0.65（转换器自动缩放）

### 1.4 核心设计理念

| 理念 | 实现 |
|------|------|
| **纯代码编排** | 不依赖 LLM 做流程决策，每个节点用 Python 代码显式编排 |
| **共享上下文** | 所有节点通过 `inputs: dict[str, Any]` 读写共享状态，节点间无需直接通信 |
| **LLM 仅用于内容生成** | LLM 只负责文本/代码生成，不决定流程走向 |
| **条件执行** | P3（文档解析）、P3.5（模板上下文）、P6.2（搜索）、P11（演讲备注）均为条件执行 |
| **重试与兜底** | P4 内容策划最多 2 次，P6.1 搜索最多 2 轮重搜，用户超时走 LLM 兜底 |
| **best-effort** | P11 演讲备注、P6.5 图片准备等失败不阻塞主流程 |

## 2. PlanNode 基类

```python
class PlanNode(ABC):
    def __init__(self, plan_name, instruction, sub_plans=None, depth=0): ...
    
    # 子类必须实现
    async def _execute(self, inputs: dict) -> dict: ...
    
    # 可选流式实现
    async def _execute_stream(self, inputs: dict) -> AsyncIterator[dict]: ...
    
    # 外部能力访问（通过回调注入）
    def has_tool(self, name: str) -> bool: ...
    async def call_tool(self, name, **kwargs) -> Any: ...
    async def stream_llm_collect(self, prompt, system_prompt=None) -> str: ...
    async def execute_subplan(self, subplan, inputs) -> Any: ...
```

**关键约束**：
- 节点禁止直接 `import os/subprocess`，必须通过 `call_tool` 访问外部能力
- 节点禁止覆盖 `run()`，框架统一处理异常和 fallback
- `plan_name` 在同一 skill 内唯一

## 3. 各阶段详解

### 3.1 P0 — PipelineInit（流水线初始化）

**子节点**：P0.1（环境依赖）+ P0.2（工作区初始化）

**P0.1 环境依赖检测**：
- 解析 `pptx_root`（外部 skill 目录，优先级：显式指定 > skill_root+skill_name 拼接）
- 检测 Node.js/npm/playwright 是否就绪
- `npm install`（必须成功）+ `npx playwright install chromium`（失败不阻塞）
- 依赖：`commander`, `express`, `get-port`, `playwright`, `jszip`

**P0.2 工作区初始化**：
- 解析 `output_dir`（用户指定 / 自动生成时间戳目录）
- `cli.js ensure-output-dir` 创建 `pages` 子目录
- 产出：`pptx_root`, `output_dir`, `pages_dir`, `session_dir`

### 3.2 P1 — IntentClassify（意图分类）

**三种场景**：
1. **有附件** → LLM 仅从 query 提取额外文件路径，`has_documents=True`
2. **无附件有路径** → LLM 从 query 提取路径，`has_documents=True`
3. **无附件无路径** → LLM 从 query 预提取槽位信息（topic/page_count/audience/style_id 等），`has_documents=False`

**额外检测**：
- `need_speaker_notes`：检测用户是否要求演讲备注（关键词：演讲备注/讲稿/speaker notes）
- `edit_existing_ppt`：检测用户是否要编辑已有 PPT
- 图片路径分流：图片不进 `doc_paths`（不触发 P3），单独存 `image_paths`

### 3.3 P3 — DocumentParse（文档解析）

**条件执行**：`has_documents=False` 时跳过

**流程**：
1. 按 `doc_paths` 逐个读取附件：文本用 `read_file`，图片用 `image_ocr`/`visual_question_answering`，大 PDF 分批读取
2. 合并所有内容 → 写入 `{output_dir}/doc_raw.md`
3. 最多 2 次重试
4. 若用户未指定 topic → LLM 推断主题

### 3.4 P2 — RequirementCollect（需求收集）

**子节点**：P2.1（槽位提取）+ P2.2（批量询问）+ P2.3（风格询问）+ P2.4（派生参数）

**槽位**：`topic`, `page_count`, `audience`, `presentation_purpose`, `style_id`

**P2.1 槽位提取**：
- LLM 从用户消息 + 文档摘要提取槽位
- `topic` 缺失时：LLM 生成 4 个主题候选 → `ask_user_question` 让用户选择
- 用户超时（120s）→ LLM 兜底从候选中挑选

**P2.2 批量询问**：
- 缺失 `page_count`/`audience`/`presentation_purpose` → `ask_user_question` 一次性询问
- 超时 → LLM 兜底推断默认值

**P2.3 风格询问**：
- 5 种预设风格：商务经典 / 科技极简 / 典雅叙事 / 工业科技 / 自由发挥
- 超时 → LLM 兜底选 `business-classic`

**P2.4 派生参数**：
- `search_mode`：auto / no_search / force_search
- `source_type`：topic / outline / description
- `research_depth`：L1 / L2 / L3
- `need_imagegen`：是否需要 AI 生图

**关键设计**：
- `page_count` 是**内容页数**（不含封面/结束页），总页数 = page_count + 2
- 用户说"生成N页"→ N 表示总页数 → page_count = max(N - 2, 1)
- "华为风格"统一映射为 `business-classic`，不得填 `custom`

### 3.5 P3.5 — TemplateContext（模板叙事上下文）

**条件执行**：仅 `style_mode == template_canvas` 时运行

读取模板包 `template-spec.json` 的 `narrative_framework` 字段，作为软约束传给 P4。

### 3.6 P4 — ContentPlan（内容策划）

**子节点**：P4.1（素材评估）+ P4.2（快速调研）+ P4.3（大纲生成）+ P4.4（校验）

**P4.1 素材充裕度评估**：

| search_mode | material_richness | 是否搜索 |
|-------------|-------------------|---------|
| no_search | — | 否 |
| force_search | — | 是 |
| auto | rich | 否 |
| auto | thin/empty | 是 |

**P4.2 快速调研**：
- LLM 生成 3-8 条搜索查询（含实体名、中英双语、加年份）
- 并行 `web_search`
- 相关性闸门：规则预检（实体名是否在结果中）+ LLM 判定
- 最多 2 轮重搜（empty→扩搜，irrelevant→收窄）
- 信息不足自检标记：`[INSUFFICIENT_INFO]`

**P4.3 大纲生成**：
- 按 `source_type` 选择不同策略：
  - `topic`：基于搜索结果 + 素材生成
  - `outline`：保留用户原文，仅做结构化重组
  - `description`：从描述中提取页面结构
- 产出 `outline.md`，格式：`# 大纲：{topic}` + `## 页面规划` + `### P{N}:` 页面块

**P4.4 校验**：
- 检查 `# 大纲：`、`## 页面规划`、`### P{N}:` 页面块
- 内容页数（✅）必须等于 `page_count`
- 首页类型必须为 cover，末页必须为 ending
- 搜索模式下需有 `## 已搜索来源` 章节
- 校验失败 → P4 整体重试（最多 2 次）

### 3.7 P5 — OutlineReview（大纲审阅）

- 读取 `outline.md` 全文
- `ask_user_question` 提供预览（可编辑）
- 用户确认 → 继续；用户编辑 → 写回；用户 NL 修改 → LLM 修订
- 工具不可用或引导模式未开启 → 直接跳过

### 3.8 P6 — DeepResearch（深度研究）

**子节点**：P6.0（全局预处理）+ P6.1（per-page 并发闭环）

**P6.0 全局预处理**：
- 解析 outline 中需要研究的页面（✅ 页）
- 提取已搜索 URL
- 素材覆盖度评估（covered/partial/uncovered）
- 计算每页最低字数（L1=1200/L2=2000/L3=3500，均分到页）

**P6.1 per-page 并发闭环**（N 页 `asyncio.gather` 并发）：
1. **搜索**：按覆盖度生成查询 → 并行 web_search → 来源评分筛选（A+/A/A-/B+/B/C）→ 缺口补搜
2. **抓取校验**：批量 `fetch_webpage` → 幽灵来源识别 → 数据充分性校验 → 定向回溯
3. **撰写**：LLM 撰写单页研究报告 → 按页校验（7 项）→ 失败重写 1 次
4. **最终门禁**：`cli validate-research` 全量校验

**来源可信度评分**：A+（权威机构）> A（企业官方）> A-（学术论文）> B+（权威媒体）> B（行业媒体）> C（自媒体，丢弃）

**无数据降级**：`no_data_fallback=True` 时跳过搜索/抓取/校验，直接生成骨架

### 3.9 P7 — StylePrepare（风格准备）

**两种模式**：
1. **预设风格**：从 `pptx_root/references/styles/{style_id}/style.md` 读取
2. **自定义风格**：LLM 生成风格规范 Markdown（含 YAML frontmatter、配色方案、字体、排版规范、CSS 主题变量、图表约束、设计禁忌）
3. **模板画布模式**：跳过风格文件生成，透传 `pack_dir`

**产出**：`{output_dir}/style-{style_id}.md`

### 3.10 P6.5 — ImagePrepare（图片准备）

**图片来源级联**：local（用户本地图）→ ai（text_to_image）

**流程**：
1. **Step 0**：LLM 分析逐页图片需求（封面需背景图，数据页不需图等）
2. **Step A**：本地图片处理 → VQA/OCR 描述 → 实体提取 → 语义匹配
3. **AI 源**：`cli stage-ai-image` 生成 AI 图片计划 → `text_to_image` 逐张生成
4. **Step D**：`stepD-finalize.js` 汇总为 `image_map.json`

**产出**：`{output_dir}/image_map.json`

### 3.11 P8 — PPTPageGen（HTML 幻灯片生成）

这是最复杂的节点，包含 PrepareNode + PageWorkerNode + 多个后置校验。

**核心流程**：
1. **P8.0 预处理**：读取 outline/research/style → 构建逐页 prompt
2. **P8.1 逐页生成**：LLM 生成完整 HTML 页面（每页一个独立 HTML 文件）
3. **P8.2 后置校验**：密度检查（17 项）+ 自动修复（ECharts SVG renderer、CSS Grid 禁令、可见页码等）
4. **P8.3 QA**：`cli check` + `cli fix` 自动修复

**HTML 生成规范**：
- 页面尺寸：1280×720px（安全区 1220×660px）
- 标准骨架：`header` + `main`（flex 双栏）+ `footer`
- 禁止 CSS Grid（html-to-pptx 不支持）
- 禁止 `rounded-*`（border-radius:0）
- ECharts 必须用 SVG renderer
- 图表颜色必须来自风格文件
- CDN 引用：`cdn.digitalhumanai.top`（禁止公共 CDN）
- Tailwind CSS + FontAwesome + ECharts

**页面类型布局**：
| 类型 | 布局 |
|------|------|
| cover | 居中，标题+副标题+日期 |
| ending | 居中，感谢+联系方式 |
| data | header 数字卡片 + main(3:2) 左论点右图表 |
| trend | header 数字卡片 + main 折线图+对比 |
| comparison | main 双栏对比 + 对比表格 |
| case | header 数字卡片 + main(2:3) 论点+图表 |
| technology | header 数字卡片 + main 图表+论点 |

**密度检查 17 项**：
1. 数据可视化 ≥1 ECharts 或 ≥3 数据卡片
2. 核心要点 6-10 个列表项/卡片
3. 装饰图标 ≥3 个 FontAwesome
4. 留白质量 < 30%
5-17. 数据来源、文字段落、视觉层级、布局正确性、完整显示、SVG 检查、grid-cols、字号一致性、图表颜色、标签防重叠、溢出风险、装饰边界

### 3.12 P9 — PPTExport（PPTX 导出）

**普通分支**：`cli.js convert` 将 pages_dir 下 HTML 转为 PPTX

**模板画布分支**（template_canvas）：
1. `cli check` → 复核 template-filler 输出
2. `cli snapshot-template-dna` → DNA 快照
3. `cli fix --profile template-safe` → 模板安全修复
4. `cli check-post-fix-template-pages` → post-fix 安全闸
5. `cli convert` → 导出 PPTX
6. `cli check-pptx-artifact` → 产物硬闸

**验证**：PPTX 文件存在 + 大小 > 10KB

### 3.13 P11 — SpeakerNotes（演讲备注）

**条件执行**：仅 `need_speaker_notes=True` 时执行，best-effort 不阻塞交付

**流程**：
1. 取语调规则（优先 tone-style skill，降级为内置默认）
2. `cli notes extract-text` 抽取每页可见纯文本
3. 按页并发 LLM 生成备注分片（50-200字）
4. 分片校验 + 缺失重跑 1 次
5. `cli notes inject` 写回 .pptx

### 3.14 P10 — Delivery（交付）

1. 验证 PPTX 产物
2. 验证 HTML 页面完整性
3. `send_file_to_user` 发送 PPTX（不可用时 fallback 到 artifact tag）
4. 生成 `__artifact__` 供 SkillTurboExecutor 提取产物摘要

## 4. 关键设计模式

### 4.1 共享上下文（inputs dict）

所有节点通过同一个 `inputs` 字典共享数据。每个节点从 `inputs` 读取上游产出，将自己的产出写回 `inputs`。这是整个流水线的核心数据流机制。

```
P0 → inputs[pptx_root, output_dir, pages_dir]
P1 → inputs[has_documents, doc_paths, image_paths, need_speaker_notes]
P3 → inputs[doc_raw_path, doc_parse_ok, topic]
P2 → inputs[topic, page_count, audience, style_id, search_mode, source_type, research_depth]
P4 → inputs[outline_path, material_richness, search_results]
P6 → inputs[research_paths]
P7 → inputs[style_file_path]
P6.5 → inputs[image_map_path]
P8 → pages_dir 下 HTML 文件
P9 → inputs[pptx_path, pptx_filename]
P10 → inputs[delivery_status, __artifact__]
```

### 4.2 用户交互（ask_user_question）

PPT 生成中有 3 处用户交互：
1. **P2.1 主题选择**：4 个候选主题
2. **P2.2 批量询问**：页数/受众/目的
3. **P2.3 风格选择**：5 种预设风格
4. **P5 大纲审阅**：确认/编辑/修改

**超时兜底**：用户 120s 未作答 → LLM 从候选中挑选最合适的默认值

### 4.3 LLM 调用模式

所有 LLM 调用通过 `stream_llm_collect(prompt, system_prompt=)` 完成：
- **JSON 输出**：所有 LLM 调用都要求返回 JSON，通过 `PptCommon.parse_json_payload()` 解析（支持 markdown fence + 正文 JSON 对象）
- **Markdown 输出**：大纲生成和 HTML 页面生成返回 Markdown/HTML
- **并发 LLM**：`stream_llm_collect(concurrent=True)` 用于评分等可并发场景

### 4.5 谁决定使用 SkillTurbo？何时选择？

**决策链路**：LLM（DeepAgent）自主选择 → SkillTurboPlanner 二次确认 → 降级兜底

```
用户消息："帮我生成一个关于AI的PPT"
    │
    ▼
DeepAgent (ReAct Agent Loop)
    │
    │  LLM 看到 skill_acceleration_exec 工具的 description：
    │  "技能加速模块。当用户意图涉及技能类任务（如生成 PPT、文档转换等结构化产出）时，
    │   可优先尝试调用此工具以获得更快的生成流程。"
    │
    ▼  LLM 自主决策：调用 skill_acceleration_exec(query="生成一个关于AI的PPT")
    │
    ▼
SkillTurbo.run_stream()
    │
    ├─ Step 1: SkillTurboPlanner.plan() — LLM 路由
    │   │
    │   │  再调一次 LLM，让 LLM 从已注册 skills 中选最匹配的：
    │   │  system_prompt 包含 PPT 准入/排除规则（5 条准入 + 7 条排除）
    │   │  输出: {"skill_name": "pptx-craft", "confidence": 0.9, "reason": "..."}
    │   │  confidence < 0.6 → 路由失败，返回 None
    │   │
    │   ▼
    │  plan_code = "from jiuwenclaw.agentserver.skill_turbo.skill_codes.ppt.ppt_gen_root import root"
    │
    ├─ Step 2: SkillTurboExecutor.execute_plan_stream() — 执行
    │   │
    │   │  加载 PPTGenRootNode，注入回调，执行 14 阶段流水线
    │   │
    │   ▼
    │  成功 → 返回结果 + 停止提示
    │
    └─ 失败 → SkillTurboNotHandled 异常 → 降级回 DeepAgent 标准流程
```

**关键决策点**：

1. **第一次决策（LLM 自主）**：DeepAgent 的 LLM 在工具选择阶段看到 `skill_acceleration_exec` 的描述，自主决定是否调用。这是**软引导**——LLM 可能选择不调用，直接走标准 ReAct 流程。

2. **第二次决策（Planner LLM 路由）**：即使 LLM 调用了 `skill_acceleration_exec`，SkillTurboPlanner 内部还会再调一次 LLM 做路由，包含详细的准入/排除规则。这确保了即使 LLM 误调，也不会进入错误流程。

3. **降级兜底**：如果 Planner 路由失败（confidence < 0.6 或无匹配 skill），或执行过程中出错，抛出 `SkillTurboNotHandled`，由 DeepAgent 回退到标准 ReAct 流程（通过 `skill_tool` 走 pptx-craft 标准流程）。

**为什么不用 ReAct Agent 生成 PPT？**

| | ReAct Agent | SkillTurbo |
|---|---|---|
| **流程** | LLM 自由决策每一步 | 预定义 14 阶段流水线 |
| **可靠性** | LLM 可能跳步、遗漏、重复 | 每个阶段必须执行，结构化校验 |
| **效率** | 多轮 LLM 对话，每轮需决策 | 一次 LLM 调用 = 一个阶段，无决策开销 |
| **质量** | 依赖 LLM 自我纠错 | 17 项密度检查 + CLI check/fix + 重试 |
| **用户交互** | 不确定何时需要交互 | 精确的 3 个交互点 + 超时兜底 |
| **成本** | 高（多轮 LLM 对话） | 低（LLM 只用于内容生成） |

核心原因：**PPT 生成是高度结构化的任务**，流程可完全预定义（需求收集→大纲→研究→生成→导出），不需要 LLM 做流程决策。ReAct Agent 更适合开放式任务（如"帮我调研一下市场"），而不是结构化生产任务（如"生成一个 10 页 PPT"）。

**SkillTurbo 的降级机制**：

```
SkillTurbo 失败 → SkillTurboNotHandled
    │
    ▼
skill_acceleration_exec 返回 {"success": False, "error": "..."}
    │
    ▼
DeepAgent LLM 看到 error，决定改用 skill_tool 走 pptx-craft 标准流程
（即让 Agent Loop 读 SKILL.md，按 SKILL.md 指示自主行动）
```

**注意**：降级后的 `skill_tool` 走的才是 pptx-craft 的**传统 Skill 用法**——Agent 读取 SKILL.md，按指示行动。但这个路径的 PPT 生成质量和效率都不如 SkillTurbo 流水线。

PPT 生成依赖 **pptx-craft**（一个独立的 Node.js Skill，代码仓库位于 `office-claw-skills/pptx-craft/`），通过 `bash` 工具调用。SkillTurbo 流水线通过 `inputs["pptx_root"]` 定位 pptx-craft 目录，所有 CLI 调用都通过 `cli_path(subcommand, pptx_root)` 构建，最终执行 `node {pptx_root}/packages/cli/dist/cli.js {subcommand}`。

| CLI 命令 | 调用节点 | 用途 |
|----------|---------|------|
| `check-env` | P0.1 | 环境检测 |
| `generate-timestamp-dir` | P0.2 | 生成时间戳输出目录 |
| `ensure-output-dir` | P0.2 | 创建 pages 子目录 |
| `validate-research` | P6.1 | 研究质量全量门禁 |
| `convert` | P9 | HTML → PPTX 转换（核心能力，使用 Playwright + JSZip） |
| `check` | P8/P9 | 页面质量检查 |
| `fix` | P8/P9 | 自动修复（支持 `--profile template-safe` 等模式） |
| `notes extract-text` | P11 | 抽取 PPTX 每页可见文本 |
| `notes inject` | P11 | 注入演讲备注到 PPTX |
| `stage-ai-image` | P6.5 | AI 图片生成计划 |
| `snapshot-template-dna` | P9 | 模板 DNA 快照（模板画布模式） |
| `check-post-fix-template-pages` | P9 | 模板修复后安全闸 |
| `check-pptx-artifact` | P9 | PPTX 产物硬闸 |

**pptx-craft 还提供非 CLI 资源**：
- `references/styles/{style_id}/style.md` — 预设风格定义（P7 读取）
- `references/designer.md` — 设计规范（P8 读取，提取页面预算、布局规范等章节）
- `image-insert/scripts/stepD-finalize.js` — 图片汇总脚本（P6.5 调用）

### 4.5 文件产物

| 文件 | 产出节点 | 用途 |
|------|---------|------|
| `doc_raw.md` | P3 | 合并后的文档原文 |
| `outline.md` | P4.3 | PPT 大纲 |
| `research-P{N}.md` | P6.1 | 每页研究报告 |
| `style-{style_id}.md` | P7 | 风格规范 |
| `image_map.json` | P6.5 | 图片映射 |
| `page-{N}.pptx.html` | P8 | 每页 HTML |
| `{topic}.pptx` | P9 | 最终 PPTX |

## 5. 对 Twinkle 的启示

### 5.1 可借鉴的架构

1. **PlanNode 递归执行树**：Twinkle 的 `skill_turbo` 可复用类似模式，每个 skill 是一棵 PlanNode 树，节点间通过共享上下文通信
2. **条件执行 + best-effort**：非核心节点失败不阻塞主流程，提升鲁棒性
3. **用户交互超时兜底**：LLM 兜底替代用户超时，保证流水线不会卡住
4. **HTML 中间格式**：先生成 HTML 再转 PPTX，比直接生成 PPTX 更灵活（可预览、可编辑、可调试）
5. **来源可信度评分**：搜索结果的 A+/A/A-/B+/B/C 评分体系，值得在 research 相关场景借鉴

### 5.2 关键差异

| 维度 | jiuwenswarm | Twinkle |
|------|-------------|---------|
| 流水线编排 | PlanNode 递归树（纯代码） | 可参考引入 PlanNode |
| 外部依赖 | pptx-craft Skill（Node.js CLI + Playwright + JSZip） | 需自行评估是否引入 pptx-craft 或自研 |
| PPTX 生成方式 | **HTML→PPTX**（不用 python-pptx） | 需决策：HTML→PPTX vs python-pptx |
| LLM 调用 | `stream_llm_collect`（回调注入） | 可通过 AgentLoop 的 model call |
| 用户交互 | `ask_user_question` 工具 | 需通过 HITL 机制实现 |
| 文件访问 | `call_tool("read_file"/"write_file")` | 可直接 Python IO |
| 搜索 | `web_search` + `fetch_webpage` 工具 | 需集成搜索工具 |
| Skill 隔离 | pptx-craft 作为外部 Skill，通过 JIUWENCLAW_SHARED_SKILLS_DIRS 注册 | 可参考 Skill 注册机制 |

### 5.3 实现建议

如果要为 Twinkle 实现类似的 PPT 生成能力，需要先做一个**架构决策**：

**决策 1：PPTX 生成方式**
- **方案 A**：直接依赖 pptx-craft（引入 Node.js + Playwright 依赖链）
  - 优势：功能完整，已有成熟方案
  - 劣势：Twinkle 需要引入 Node.js 运行时，依赖链长
- **方案 B**：自研 HTML→PPTX 转换器（参考 pptx-craft 的 Playwright + JSZip 方案）
  - 优势：可控制依赖，可按需裁剪
  - 劣势：开发量大，需要处理大量 HTML→PPTX 边界情况
- **方案 C**：使用 python-pptx 直接生成
  - 优势：纯 Python，无外部依赖
  - 劣势：布局能力有限，图表需要手动构建，视觉设计不如 HTML 灵活

**推荐**：如果追求视觉效果和功能完整性，方案 A 最快；如果追求轻量和可控，方案 C 最简单，但需要接受视觉设计的局限。

**分阶段建议**：

1. **Phase 1**：引入 PlanNode 基类 + 最小流水线（P0→P1→P2→P4→P8→P9→P10），只支持无文档、无搜索的简单场景
2. **Phase 2**：加入 P3 文档解析 + P6 搜索 + P7 风格，支持完整的 topic 模式
3. **Phase 3**：加入 P5 大纲审阅 + P6.5 图片 + P11 演讲备注，完整功能
4. **Phase 4**：引入 pptx-craft CLI 或自研 HTML→PPTX 转换器

---

*本文档基于 jiuwenclaw `enterprise_dev` 分支代码分析，文件路径以 `jiuwenclaw/agentserver/skill_turbo/skill_codes/ppt/` 为根目录。*
