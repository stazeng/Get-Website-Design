---
name: get-web-design
description: This skill should be used when the user wants to extract a complete DESIGN.md style guide (measured token frontmatter + eight standard sections + actionable design rules + evidence appendix) from any live website URL. It captures 3 viewport screenshots, samples DOM and computed CSS via chrome-devtools MCP, asks a user-configured multimodal LLM to analyze the visual style from screenshots+DOM, then merges everything into a single DESIGN.md file. Trigger phrases include "extract design from a url", "生成 DESIGN.md", "分析这个网站的设计风格", "get web design".
---

# get-web-design

将任意线上网站的设计风格提取为一份结构化 `DESIGN.md`。本 skill 是 design-extractor Chrome 扩展的命令行 / Claude Code 移植版。

## 何时使用

调用本 skill 当且仅当用户希望：

- 从一个 URL 生成一份可用于 AI 编程提示词的 `DESIGN.md`
- 提取某站点的设计 token（颜色 / 字体 / 间距 / 圆角 / 阴影 / 动效 token）
- 让 AI 描述某站点的视觉风格、组件规范、整体氛围
- 识别网页里真正有记忆点的标志性元素，并用纯文字输出视觉规则（不含代码）

典型触发语句：
- "生成 https://stripe.com 的 DESIGN.md"
- "分析 linear.app 的设计风格并保存"
- "extract design from <url>"

## 硬性前置条件

调用本 skill **前**必须确认：

1. **chrome-devtools MCP 已连接** —— 通过 `/mcp` 验证是否有 `chrome-devtools` 服务，没有则参考 `references/setup.md` 安装。
2. **多模态 LLM 三项配置已就绪**（环境变量或 CLI 参数）：
   - `WEB_DESIGN_API_KEY`
   - `WEB_DESIGN_BASE_URL` （OpenAI 兼容根路径，必须含 `/v1` 或对应路径前缀）
   - `WEB_DESIGN_MODEL` （**必须支持 vision**，纯文本模型会失败）

   若任一项缺失，**必须先向用户询问并由用户自行填写**，不要替用户编造任何 key/url/model。
   详见 `references/setup.md`。

## 总体工作流（5 步）

```
URL → [chrome-devtools] 3 截图 + collected.json → [Python] CSS 压缩 + LLM 调用 + 拼装 →
       output/<hostname>/{shot1,shot2,shot3}.jpg
       output/<hostname>/design.md
```

每一步细节见 `references/workflow.md`。chrome-devtools MCP 的精确调用配方见 `references/chrome_devtools_recipes.md`。

### Step 1 — 创建输出目录

所有截图和最终 `design.md` 都直接落到 **当前工作目录** 下的 `output/<hostname>/`。

```bash
mkdir -p output/<hostname>
```

`<hostname>` 即 URL 的 host（如 `https://platform.moonshot.cn/...` → `platform.moonshot.cn`）。
`collected.json` 仍可放到 `/tmp/` 等临时位置（它是中间产物，不必随结果发布）。

### Step 2 — 用 chrome-devtools 采集

按 `references/chrome_devtools_recipes.md` 顺序：

1. `mcp__chrome-devtools__new_page({ url })`，必要时 `wait_for` 等首屏渲染。
2. 依次滚到 0% / 35% / 70%，每次滚动后 ≥600ms 再 `take_screenshot`，**直接存到** `output/<hostname>/shot1.jpg`、`shot2.jpg`、`shot3.jpg`。
3. 读取 `assets/collect_design_data.js` 全部内容，包成 `() => { …全部代码… }` 传给 `evaluate_script`，把返回值序列化写入 `collected.json`（建议放 `/tmp/get-web-design/<run-id>/collected.json`）。

> 该 JS 文件最后一行是 `return collectDesignData({ includeCss: true });`，
> 所以包装层只需要把整个文件内容塞进 `() => { ... }` 里就能得到结构化对象。

### Step 3 — 调用编排脚本

```bash
python3 <skill_dir>/scripts/generate_design_md.py \
  --collected /tmp/get-web-design/<run-id>/collected.json \
  --screenshots output/<hostname>/shot1.jpg output/<hostname>/shot2.jpg output/<hostname>/shot3.jpg \
  --hostname "<hostname>"
```

默认会输出到 `output/<hostname>/design.md`，并将传入的 3 张截图归位到同一目录（已经在该目录的会跳过复制）。
如需自定义可用 `--output-dir <dir>` 整体改目录，或 `--output <path>` 仅改 markdown 路径。
语言默认英文（`--language en`）；`--language zh` 输出中文正文，八个 H2 标题仍保持规范的英文名称。

脚本内部完成：
- `normalize_css_evidence` —— 用 `scripts/css_evidence.py` 把 280 行 computed-style 压成高频 token；
- `format_css_evidence_markdown` —— 渲染成 `## Engineering CSS Evidence` 段（英文）；
- `extract_design_tokens` —— 从原始 CSS 行保守提取规范 token，保留透明度和同一元素的排版组合；
- `build_messages` —— DOM JSON + 实测 designTokens/tokenEvidence + 3 张截图 base64 + 中英文 system prompt；
- `call_llm` —— OpenAI 兼容 `/chat/completions` 非流式调用；
- `assemble_design_md` —— 按固定顺序拼装：

```
frontmatter (name / version: alpha / measured colors, typography, rounded, spacing, components)
+ 使用说明         (assets/design_thinking.md，证据优先级和适用边界)
+ 八个标准章节      (AI 解释设计决策；校验章节顺序、重复与空内容)
+ 实施检查         (assets/core_principles.md，复用与验证规则)
+ Token evidence + Evidence Appendix (样本来源与压缩证据)
```

### Step 4 — 校验输出

应当生成以下 4 个文件：

```
output/<hostname>/design.md
output/<hostname>/shot1.jpg
output/<hostname>/shot2.jpg
output/<hostname>/shot3.jpg
```

打开 `output/<hostname>/design.md`，确认：
- 顶部是有效 YAML frontmatter，`version: alpha`；实测 token 使用规范分组，未知值省略；
- 八个 H2 依次为 `Overview`、`Colors`、`Typography`、`Layout`、`Elevation & Depth`、`Shapes`、`Components`、`Do's and Don'ts`；
- AI 正文无 HTML/CSS 代码块，标志性元素最多两个且有证据，未测状态/主题/视口不能当成事实；
- 定量值与 frontmatter 一致；`Token evidence` 记录采样位置，`Evidence Appendix` 只作启发式诊断，不覆盖 token；
- 可用 Google 官方 CLI 验证：`npx @google/design.md lint <output-path>/design.md`。格式通过不等于 Stitch 导入或生成效果已验证。

若 `Engineering CSS Evidence` 大量为 "Not enough evidence"，回到 Step 2 检查 evaluate_script 返回值是否完整。

### Step 5 — 关闭页面（可选）

```
mcp__chrome-devtools__close_page({ pageIdx })
```

## 文件清单

| 路径 | 用途 |
|------|------|
| `assets/collect_design_data.js` | 注入到目标页面的 DOM+CSS 采集脚本（不依赖任何外部库） |
| `assets/system_prompt_zh.txt` / `system_prompt_en.txt` | 多模态 LLM 的 system prompt |
| `assets/design_thinking.md` / `core_principles.md` | 使用边界与实施检查，不能引入覆盖原站风格的通用禁令 |
| `scripts/design_document.py` | 实测 token、标准 YAML 与八节正文校验（Python 3.9，无第三方依赖） |
| `scripts/css_evidence.py` | CSS computed-style → 设计 token 压缩 + Markdown 渲染（Python 库） |
| `scripts/generate_design_md.py` | 主入口 CLI；负责调 LLM 与最终拼装 |
| `references/setup.md` | 安装 chrome-devtools MCP + 配置 LLM 凭据 |
| `references/workflow.md` | 完整数据流详解（与原 design-extractor 对齐） |
| `references/chrome_devtools_recipes.md` | chrome-devtools MCP 的精确调用顺序与故障兜底 |

## 重要约束

- **数值由程序提供，AI 解释用途。** prompt 包含保守实测 designTokens 与 tokenEvidence，不注入完整 CSS；AI 不得从截图估算精确值。语义角色仍需结合样本确认，采样选择器不是官方组件 API。
- **输出聚焦设计细节，禁止代码块。** 分析重点是配色、字体、圆角、间距、阴影、质感、动效；AI 输出中不允许出现 HTML/CSS 代码块或结构草图，避免下游把 DESIGN.md 当页面骨架照抄。
- **标志性元素必须来自真实证据且最多 2 个。** `domSnapshot.distinctiveCandidates` 只用于确认元素真实存在；证据不足时直接省略该节，不要为不存在的页面元素编造描述。
- **遵循 Google 格式与 Vercel 使用方法。** frontmatter → 使用说明 → 八节正文 → 实施检查 → 来源与证据附录。已观察事实、推断和实施建议分开，按读者任务复用设计而不是复制页面结构；格式不合格时停止成功导出。
- **3 张截图是上限。** 由 Chrome 速率限制决定；不要尝试加到 5 张以上。
- **模型必须支持 vision。** 用纯文本模型会得到无视觉 grounding 的低质量结果。
- **API key/url/model 由用户提供。** 不要为用户填默认值；缺失时用 `AskUserQuestion` 询问，然后让用户用环境变量或 `--api-key` 传入。
