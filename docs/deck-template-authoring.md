# Deck 模版创作指南

> 面向 `digital-human-engine` 的 ppt-master 引擎。Deck 模版 = **身份 + 版式结构 + SVG 页面原型**，导出时生成真实的 `p:sldMaster` / `p:sldLayout` 继承。
> 本文是操作层面的「怎么做」；schema 权威在 skill 内 `templates/README.md`、`create-template.md` / `create-deck.md`，以及 `templates/decks/中国电信` 这个可对照的成品。

## 1. 四种模版如何区分

| 种类 | 拥有 | 目录 | 示例 |
|---|---|---|---|
| **Brand** | 仅身份：颜色/字体/logo/语气/图标风格 | `templates/brands/` | 中国电信、华为 |
| **Style** | 沟通方法/视觉方向 | `templates/styles/` | consulting-decision |
| **Layout** | 品牌中立结构（Master/Layout/槽位） | `templates/layouts/` | presentation_core |
| **Deck** | 一个复用场景 = 集成身份 + 结构 + 页面原型 | `templates/decks/` | 中国电信、中汽研 |

要「带页面版式的完整模版」就选 **Deck**（`kind: deck`）。

## 2. 目录结构

```
templates/decks/<deck_id>/
├── templates/
│   ├── design_spec.md      # 规格（frontmatter + 章节）
│   ├── 01_cover.svg        # 每页一个 SVG 原型，声明 Master + Layout
│   ├── 02_toc.svg
│   ├── 03_chapter.svg
│   ├── 04_content.svg
│   └── 05_ending.svg
└── images/
    ├── logo.png
    ├── header_brand.png
    └── footer_ribbon.png
```

- `templates/` 必填；`images/` 可选（SVG 里用 `href="../images/<name>"` 引用）。
- `exports/` 是评审证据（可选，多 Master 时必填），Git 忽略，应用时永不消费。
- 每个 SVG 根节点声明 Master + Layout；多个 Master 家族用不同 `data-pptx-master` 值区分。

## 3. `design_spec.md`

### frontmatter（必填）

```markdown
---
deck_id: <slug>
kind: deck
category: brand            # brand | general | scenario | government | special
summary: <一句话：复用场景 + 预期结果>
keywords: [标签1, 标签2, 标签3]
primary_color: "#C00000"
canvas_format: ppt169
canvas_width: 1280
canvas_height: 720
canvas_viewbox: "0 0 1280 720"
replication_mode: fidelity   # standard | fidelity | mirror
native_structure_mode: structured
page_count: 5
---
```

### 章节（必备 + 按需）

```markdown
# <Deck 名> — Design Specification

## I. Template Overview          # 复用场景、受众、交付假设、叙事角色（描述性，不规定选页）
## II. Color Scheme             # 每个颜色 #RRGGBB + 用途
## III. Typography              # 字体栈 + 用途（可省略以用共享默认）
## IV. Signature Design Elements # 页眉/卡片/飘带等视觉签名
## V. Page Roster               # 每页原型：文件→Master→Layout key→picker 名→槽位
## VI. Assets                   # images/ 里每个图的用途（有 assets 才写）
## VII. Placeholder Overrides   # 占位覆盖（有才写）
```

可对照 `templates/decks/中国电信/templates/design_spec.md`。

## 4. SVG 原型

每个 SVG 根节点声明 Master + Layout：

```xml
<svg xmlns="http://www.w3.org/2000/svg"
  width="1280" height="720" viewBox="0 0 1280 720"
  data-pptx-master="china_telecom_brand_master" data-pptx-master-name="China Telecom Brand"
  data-pptx-layout="cover" data-pptx-layout-name="Cover">
```

元素分层：

| 属性 | 含义 |
|---|---|
| `data-pptx-layer="master" data-pptx-editable="false"` | 跨页共享母版元素（背景/页眉/飘带） |
| `data-pptx-layer="layout" data-pptx-editable="false"` | 本页版式元素（标题条/卡片） |
| 内容占位 slot | 可编辑的内容承载区 |

要点：
- 每个 SVG 是一页**完整预览**，声明一个根 Master + 一个 Layout。
- 多个 Master 家族用不同 `data-pptx-master` 值区分（中国电信有 `Brand` / `Content` 两个 Master）。
- 颜色/字体/图片必须与 `design_spec.md` 的身份段一致。
- 图片引用 `../images/<name>`。

## 5. 校验 + 注册

```bash
# 校验（模板模式）
python3 skills/ppt-master/scripts/svg_quality_checker.py \
  "skills/ppt-master/templates/decks/<deck_id>/templates" --template-mode --canonical-authoring

# 注册进 decks_index.json
python3 skills/ppt-master/scripts/register_template.py <deck_id> --kind deck
# 先 dry-run 看会写入什么
python3 skills/ppt-master/scripts/register_template.py <deck_id> --kind deck --dry-run
```

注册后在 `decks_index.json` 生成 `{summary, canvas_format, page_count, primary_color}` 条目。

## 6. 使用

- 引擎接口：`template: "skills/ppt-master/templates/decks/<deck_id>"`
- 前台下拉：在 `digital-human-web/src/views/digital/ppt-master.vue` 的 `templateOptions` 加一条。

## 7. 三种生成方式

1. **手动**：照 §3–§4 写 `design_spec.md` + 手写每页 SVG 原型 + 放 images（可控但繁琐）。
2. **从参考 PPTX 导入**：`pptx_template_import.py <source.pptx>` 转成 import workspace（`standard` / `fidelity` / `mirror` 三种策略），再按 create-deck 工作流精修。
3. **LLM 跑 Create Template → Create Deck 工作流**：skill 有完整流程（`workflows/create-template/create-deck.md`）；**当前 `/generate_ppt_master` 引擎只做 quick-generate 生成 deck，未接建模版入口**。

> 注意：Brand / Style / Layout / Deck 是四种独立 kind，不是继承层级。Deck 是「身份 + 结构」一起复用；只有身份用 Brand，只有结构用 Layout。别混用 schema。
