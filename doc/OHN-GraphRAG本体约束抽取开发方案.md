# OHN-GraphRAG 本体约束实体与关系抽取开发方案

## 1. 开发目标

本方案将 OHN-GraphRAG 的文档层级导航设计与本体约束抽取机制结合，构建一套适用于法律、财税和制度文档的知识图谱抽取模块。

目标不是让大模型自由生成实体和关系，而是：

```text
本体规定可抽取的类型、属性和关系
LLM 只负责从 Chunk 中填充具体值
程序负责格式、类型、关系和证据校验
Neo4j 保存层级结构、实体、事实和原文来源
```

## 2. 理论依据与设计原则

论文中的核心思想是将领域本体表示为：

```text
(subject, attribute, value)
```

属性值可以是另一个实体，也可以是从文档中提取的开放值。为避免不同上下文中的值互相覆盖，抽取结果应保存为带来源、时间和适用范围的事实块（FactBlock）。

本项目采用以下原则：

1. 本体先于抽取结果定义。
2. 实体类型使用白名单和层级体系。
3. 关系使用白名单和源/目标类型约束。
4. 每个实体、属性和关系必须绑定来源 Content。
5. 无法从原文验证的事实不进入正式图谱。
6. 文档层级关系、导航关系和业务关系分开建模。
7. 同一实体在不同时间或不同文件中的属性通过事实块隔离。

## 3. 总体架构

```text
chunks.jsonl
    ↓
文档层级解析器
    ↓
Document / Section / Content
    ↓
Ontology Controller 加载本体
    ↓
LLM 本体映射抽取
    ↓
JSON Schema 校验
    ↓
实体归一化与消歧
    ↓
关系类型和证据校验
    ↓
FactBlock 生成
    ↓
Neo4j 写入
    ↓
层级导航和 GraphRAG 检索
```

建议模块：

```text
ontology/
  loader.py              本体加载
  models.py              Pydantic 数据模型
  validator.py           类型、关系和证据校验
  normalizer.py          实体归一化
  prompt_builder.py      本体抽取 Prompt
  fact_blocks.py         事实块生成
ingestion/
  hierarchy_builder.py   Document/Section/Content 建图
  extractor.py           LLM 抽取协调
  neo4j_writer.py        图谱写入
```

## 4. 本体模型

### 4.1 实体类型层级

第一版法律本体建议：

```text
Entity
├── Organization
│   ├── GovernmentAgency
│   ├── Court
│   └── Enterprise
├── LegalDocument
│   ├── Constitution
│   ├── Law
│   ├── Regulation
│   ├── JudicialInterpretation
│   └── AdministrativeNotice
├── LegalConcept
│   ├── Right
│   ├── Obligation
│   ├── Liability
│   ├── Procedure
│   ├── LegalAct
│   └── Remedy
├── Person
├── Case
├── Party
├── Article
├── Condition
├── Exception
├── Penalty
├── Amount
├── Date
├── Region
├── Evidence
└── Material
```

LLM 不得创建本体之外的新类型。无法判断时使用 `UnknownEntity`，进入待审核队列。

### 4.2 实体属性

每类实体定义：

```text
属性名
属性类型
是否必填
是否多值
值域（开放值或另一个实体类型）
是否需要来源
```

示例：

```yaml
LegalDocument:
  attributes:
    title:
      type: string
      required: true
    document_number:
      type: string
    issuing_authority:
      range: Organization
    publication_date:
      range: Date
    effective_date:
      range: Date
    validity_status:
      type: enum
      values: [effective, abolished, draft, unknown]

Organization:
  attributes:
    canonical_name:
      type: string
      required: true
    organization_type:
      type: enum
      values: [government, court, enterprise, association, institution, unknown]
    jurisdiction:
      range: Region

Article:
  attributes:
    article_number:
      type: string
      required: true
    text:
      type: string
      required: true
    document:
      range: LegalDocument
      required: true

Amount:
  attributes:
    value:
      type: number
    unit:
      type: enum
      values: [元, 万元, 百分比, 天, 月, 年]
    original_text:
      type: string
```

属性值分为两类：

```text
实体值：LegalDocument.issuing_authority → Organization
开放值：Amount.value、Date.original_text、Condition.text
```

### 4.3 业务关系

关系定义必须包括：

```text
关系名称
源实体类型
目标实体类型
关系描述
允许属性
是否需要时间
是否必须有证据
```

第一版关系白名单：

```yaml
ISSUED_BY:
  source: [LegalDocument]
  target: [Organization]
  evidence_required: true

AMENDS:
  source: [LegalDocument]
  target: [LegalDocument]
  evidence_required: true

REPEALS:
  source: [LegalDocument]
  target: [LegalDocument]
  evidence_required: true

CONTAINS_ARTICLE:
  source: [LegalDocument]
  target: [Article]
  evidence_required: true

DEFINES:
  source: [LegalDocument]
  target: [LegalConcept]
  evidence_required: true

APPLIES_TO:
  source: [LegalConcept]
  target: [Person, Organization, Party]
  evidence_required: true

REQUIRES:
  source: [LegalConcept]
  target: [Condition]
  evidence_required: true

EXCEPTS:
  source: [LegalConcept]
  target: [Exception]
  evidence_required: true

HAS_PENALTY:
  source: [LegalConcept]
  target: [Penalty]
  evidence_required: true

HAS_AMOUNT:
  source: [LegalConcept]
  target: [Amount]
  evidence_required: true

EFFECTIVE_ON:
  source: [LegalDocument]
  target: [Date]
  evidence_required: true

APPLIES_IN:
  source: [LegalDocument]
  target: [Region]
  evidence_required: true

CITES:
  source: [LegalDocument, Article, Content]
  target: [LegalDocument, Article, Content]
  evidence_required: true
```

## 5. 图谱数据模型

### 5.1 节点

```text
(:Document)
(:Section)
(:Content)
(:Entity)
(:FactBlock)
(:Amount)
(:Date)
```

实体类型可以同时作为标签，例如：

```text
(:Entity:Organization)
(:Entity:LegalDocument)
(:Entity:LegalConcept)
```

### 5.2 层级和导航关系

```text
Document -[:CONTAINS]-> Section
Section -[:CONTAINS]-> Section
Section -[:CONTAINS]-> Content
Content -[:MENTIONS]-> Entity
Content -[:NEXT]-> Content
Content -[:PREVIOUS]-> Content
Content -[:CITES]-> Content/Document
Content -[:SIMILAR_TO]-> Content
Entity -[:CO_OCCURS]-> Entity
```

### 5.3 事实块关系

复杂法律事实建议使用 FactBlock：

```text
Content -[:SUPPORTS]-> FactBlock
FactBlock -[:ABOUT]-> Entity
FactBlock -[:APPLIES_TO]-> Entity
FactBlock -[:REQUIRES]-> Entity
FactBlock -[:HAS_AMOUNT]-> Amount
FactBlock -[:EFFECTIVE_ON]-> Date
```

FactBlock 属性：

```text
fact_block_id
source_chunk_id
source_document_id
evidence_text
valid_at
invalid_at
jurisdiction
confidence
status
```

关系也应保留 `source_chunk_id`、`evidence_text`、`confidence`、`valid_at` 和 `invalid_at`。

## 6. LLM 抽取协议

LLM Prompt 必须注入当前版本本体，包括：

```text
允许的实体类型及说明
实体属性及值域
允许的关系及源/目标类型
输出 JSON Schema
证据和保守抽取规则
```

Prompt 约束：

```text
1. 不得创造新的实体类型。
2. 不得创造新的关系类型。
3. 只有原文明确表达的事实才能抽取。
4. 每个实体必须提供 source_text。
5. 每条关系必须提供 source_chunk_id 和 evidence_text。
6. 属性值保留原文表达，同时提供规范化值（如可确定）。
7. 无法判断的类型使用 UnknownEntity。
8. 同一上下文中的事实放入同一个 fact_block。
9. 只返回 JSON，不返回解释和 Markdown。
```

建议输出：

```json
{
  "fact_blocks": [
    {
      "fact_block_id": "fb_001",
      "entities": [
        {
          "id": "e1",
          "name": "甲公司",
          "canonical_name": "甲公司",
          "type": "Organization",
          "attributes": {},
          "source_text": "甲公司"
        }
      ],
      "relations": [
        {
          "type": "ISSUED_BY",
          "source": "e2",
          "target": "e1",
          "evidence_text": "由甲公司发布",
          "source_chunk_id": "chunk_001"
        }
      ],
      "source": {
        "chunk_id": "chunk_001",
        "document_id": "doc_001"
      }
    }
  ]
}
```

## 7. 抽取后校验

LLM 输出禁止直接写入 Neo4j，必须经过：

### 7.1 JSON 校验

检查 JSON 格式、字段完整性、字段类型和数组结构。

### 7.2 类型校验

检查实体类型是否在白名单、父类型是否匹配、属性是否属于该类型、属性值是否符合值域。

### 7.3 关系校验

检查关系类型是否在白名单，源实体和目标实体类型是否满足定义，方向是否正确。

### 7.4 证据校验

检查实体 `source_text` 是否出现在 `page_content`，关系 `evidence_text` 是否能在原文定位，`source_chunk_id` 是否为当前 Chunk。

校验失败的结果：

```text
可修复：标准化后重试
不确定：进入 pending_review
明显错误：丢弃并记录日志
```

## 8. 实体归一化

建议按以下顺序：

```text
精确名称匹配
→ 别名匹配
→ 同文档上下文匹配
→ 向量相似度
→ LLM 消歧
→ 无法确定则保留为不同实体
```

实体属性：

```text
entity_id
canonical_name
entity_type
aliases
description
normalization_status
confidence
```

例如：

```text
国家税务总局
税务总局
国家税务总局机关
```

可以归一化为：

```text
canonical_name = 国家税务总局
entity_type = GovernmentAgency
aliases = [税务总局, 国家税务总局机关]
```

## 9. 与 chunks.jsonl 的结合

使用以下字段恢复层级：

```text
document_id
document_title
chunk_id
node_id
heading_path
heading_path_parts
parent_path
parent_node_id
chunk_index
previous_leaf_id
next_leaf_id
```

建图顺序：

```text
1. 创建 Document
2. 创建 Section 层级
3. 创建 Content
4. 建立 CONTAINS
5. 建立 NEXT/PREVIOUS
6. 将 Content 发送给本体抽取器
7. 创建或合并 Entity
8. 建立 MENTIONS
9. 建立 FactBlock 和业务关系
10. 建立 CITES、SIMILAR_TO、CO_OCCURS
11. 运行质量校验
```

## 10. Graphiti 改造方案

Graphiti 继续负责：

```text
episode 写入
LLM 调用
Embedding
实体初步抽取
时间关系处理
混合检索
```

新增 Ontology Controller 负责：

```text
加载本体
构建本体 Prompt
校验实体类型和关系类型
实体归一化
证据绑定
FactBlock 生成
```

推荐不要直接替换 Graphiti 的默认数据模型，而是在 `add_episode` 前后增加本体处理层：

```text
add_episode 请求
→ Ontology Controller 抽取和校验
→ Graphiti 保存 Content/episode
→ Neo4j 写入本体实体、FactBlock 和关系
```

## 11. 配置文件规划

建议目录：

```text
graphiti/
└── ontology/
    ├── legal_ontology.yaml
    ├── entity_types.yaml
    ├── relation_types.yaml
    ├── extraction_prompt.md
    ├── output_schema.json
    └── validation_rules.yaml
```

本体文件必须带版本：

```yaml
ontology_id: legal_ontology
version: "1.0.0"
domain: legal
```

写入实体和关系时保存：

```text
ontology_id
ontology_version
```

## 12. 增量更新和版本策略

当前方法以建库和检索为核心，动态更新属于后续增强功能。实现增量更新时：

```text
新 Chunk
→ 按同一版本本体抽取
→ 与已有实体归一化
→ 新增或修正 FactBlock
→ 保留 source_chunk_id
→ 根据 valid_at/invalid_at 保存时间差异
```

本体版本变化时不要直接覆盖旧结果，建议：

```text
保留 ontology_version=1 的结果
使用新版本重新抽取受影响 Chunk
通过版本字段对比结果
```

## 13. 质量指标

至少统计：

```text
实体类型合法率
关系类型合法率
源/目标类型匹配率
实体证据命中率
关系证据命中率
FactBlock 完整率
实体归一化准确率
孤立节点数
无来源关系数
UnknownEntity 比例
```

正式入库门槛建议：

```text
关系类型合法率 = 100%
源/目标类型匹配率 = 100%
关系来源 Chunk 覆盖率 = 100%
实体来源 Chunk 覆盖率 ≥ 99%
```

## 14. 实施阶段

### 阶段一：本体与模型

完成实体类型、属性、关系白名单和 JSON Schema。

### 阶段二：层级建图

根据 `chunks.jsonl` 创建 Document、Section、Content 及层级、相邻关系。

### 阶段三：单 Chunk 抽取

使用 `qwen-plus` + `json_object` + `enable_thinking=false` 验证完整抽取结果。

### 阶段四：校验与归一化

完成类型校验、证据校验和实体消歧。

### 阶段五：FactBlock 写入

将通过校验的事实写入 Neo4j。

### 阶段六：小批量评估

先处理少量 Chunk，统计错误类型和本体覆盖率，再扩大数据量。

### 阶段七：检索接入

实现从实体、事实、层级和导航关系回到原文的证据检索。

## 15. 最终结论

本方案将本体作为 LLM 抽取的结构模板和校验标准，将 OHN-GraphRAG 的层级结构作为证据导航骨架：

```text
层级解决“知识在哪里”
本体解决“知识是什么”
事实块解决“哪些属性和关系在同一上下文中成立”
Content 解决“能否回到原文证明”
```

最终推荐的统一图谱为：

```text
Document → Section → Content → Entity
                              ↓
                          FactBlock
                              ↓
                       业务关系和属性
```

## 16. 节点属性与构造职责（与 OHN-GraphRAG 方法报告对齐）

本节明确哪些字段由输入元数据确定，哪些内容需要 LLM 抽取，避免把结构信息交给模型猜测。

### 16.1 Document 节点

Document 表示一个文件，按 `document_id` 幂等合并。以下属性全部来自 JSONL `metadata`，由规则写入，不由 LLM 生成：

```text
document_id、document_title、source_file、source_path、file_type、file_version
document_number、publication_date、content_hash、file_role、parent_document_id
attachment_chain_document_ids、attachment_document_ids、attachment_bindings
attachment_number、attachment_label、attachment_content_type、attachment_presentation
is_ocr、ocr_engine、visual_type、ontology_id、ontology_version
```

文件之间的 `ATTACHMENT_OF` 由 `parent_document_id` 规则构造。`AMENDS`、`REPEALS`、`CITES` 等法律语义关系必须从正文抽取并经过证据校验，不能仅根据文件名、日期或版本号推断。

### 16.2 Section（目录）节点

Section 表示文件原有目录层级（章、节、小节或条款目录），由规则根据 JSONL 元数据恢复：

```text
node_id                 目录节点标识，优先使用 parent_node_id 或目录节点 ID
title                   当前目录标题
heading_path            完整目录路径
heading_path_parts      路径数组
parent_path             父目录路径
parent_node_id          父节点标识
level                   heading_path_parts 的长度
document_id             所属文件
chunk_summary           目录摘要（若输入提供）
```

构造优先级为 `parent_node_id > parent_path > heading_path_parts`。目录标题、路径、层级和归属是确定性结构，禁止由 LLM 改写或重新组织。若需要目录摘要，可使用已有摘要字段；没有摘要时不要求模型臆造。

### 16.3 Content（内容）节点

Content 是原文证据的最小单元，每条 JSONL 记录对应一个 Content。以下字段由规则从 `page_content` 和 `metadata` 原样保存：

```text
chunk_id、node_id、node_type、page_content、title、heading_path
heading_path_parts、parent_path、parent_node_id、document_id
chunk_index、document_chunk_count、chunk_summary、page_number
previous_leaf_id、next_leaf_id、content_hash
source_file、source_path、file_version、document_number、publication_date
```

`Content -[:NEXT/PREVIOUS]-> Content` 使用 `previous_leaf_id`、`next_leaf_id`；缺少指针时才可按同一文件的 `chunk_index` 补建。Content 的原文、顺序和来源不得由 LLM 改写。Embedding、摘要或关键词属于派生字段，可由程序或模型生成，但不能替代 `page_content`。

### 16.4 Entity 节点

Entity 是从 Content 原文中识别的专业对象。LLM 在注入本体白名单后提出候选实体，程序校验后写入：

```text
entity_id              稳定标识
name                   原文名称
canonical_name         规范名称
entity_type            本体类型
aliases                别名
description            原文可支持的描述
normalization_status   normalized / pending_review / unresolved
confidence             抽取或归一化置信度
source_text            原文证据片段
source_chunk_id        来源 Content
ontology_id
ontology_version
```

LLM 负责识别实体边界、类型候选、规范名和别名；程序负责白名单校验、`source_text` 命中校验、归一化和跨 Chunk 合并。无法判断类型时使用 `UnknownEntity` 并进入审核队列，不得创建新类型。

### 16.5 属性和关系的分工

实体的结构/来源属性（ID、文件、路径、Chunk 顺序、哈希、OCR 信息）由规则写入；实体语义属性（规范名、别名、描述、开放值）由 LLM 从原文抽取后写入 FactBlock 或带来源属性。业务关系由 LLM 提出候选，程序检查关系白名单、源/目标类型、证据文本和来源 Chunk 后才入库。

统一职责链如下：

```text
规则解析 metadata → Document / Section / Content 和层级导航关系
LLM 阅读 page_content → Entity、候选业务关系、事实属性
程序校验 → JSON、类型、关系方向、证据命中、来源完整性
Neo4j 写入 → 通过校验的节点、FactBlock、业务关系
```

该职责划分适用于当前建库和 RAG 检索；检索阶段始终沿 Entity/业务关系定位，再回读 Content 原文作为证据。

## 17. chunks.jsonl 35 个元数据字段职责表

上游 `chunks.jsonl` 的 `metadata` 是下游检索的确定性导航信息，必须逐字段保留；不得因字段暂时为空而删除字段。字段值原则上原样保存，只有 `level`、`document_id` 等索引辅助字段允许按规则派生。LLM 不得修改以下元数据。

| 元数据字段 | 节点/关系归属 | 构造与赋值规则 |
|---|---|---|
| `document_id` | Document 主键；Content 外键 | 上游原样赋值；按此字段 `MERGE` 文档 |
| `document_title` | Document.title；Content.document_title | 原样复制 |
| `source_file` | Document、Content | 原样复制 |
| `source_path` | Document、Content | 原样复制 |
| `file_type` | Document、Content | 原样复制 |
| `file_version` | Document、Content | 原样复制；`unknown` 也保留 |
| `document_number` | Document、Content | 原样复制 |
| `publication_date` | Document、Content | 原样复制；不自行推断日期 |
| `content_hash` | Document、Content | 原样复制；Document 按文件保存，Content 按 Chunk 保存 |
| `file_role` | Document、Content | 原样复制（如 `content_document`、`attachment`） |
| `parent_document_id` | Document；ATTACHMENT_OF 目标 | 非空时规则创建 `attachment-[:ATTACHMENT_OF]->parent` |
| `attachment_chain_document_ids` | Document | 原样保存列表 |
| `attachment_document_ids` | Document | 原样保存列表 |
| `attachment_bindings` | Document | 原样保存列表；不由模型重建 |
| `attachment_number` | Document；ATTACHMENT_OF 属性 | 原样保存；关系属性同步该值 |
| `attachment_label` | Document | 原样保存 |
| `attachment_content_type` | Document | 原样保存 |
| `attachment_presentation` | Document | 原样保存 |
| `is_ocr` | Document、Content | 原样保存布尔值 |
| `ocr_engine` | Document、Content | 原样保存 |
| `visual_type` | Document、Content | 原样保存 |
| `chunk_id` | Content 主键；导航关系端点 | 原样赋值；按此字段 `MERGE` Content |
| `node_id` | Content.node_id | 原样保存；通常与 `chunk_id` 相同，不自动覆盖 |
| `node_type` | Content.node_type | 原样保存；当前通常为 `content` |
| `chunk_index` | Content；NEXT/PREVIOUS 属性 | 原样保存；仅用于同文件缺失导航指针时排序补建 |
| `document_chunk_count` | Content | 原样保存 |
| `title` | Content.title；Section.title 候选 | 原样保存；Section 标题优先使用目录节点字段 |
| `heading_path` | Content、Section | 原样保存完整路径 |
| `heading_path_parts` | Content、Section | 原样保存列表；Section.level 派生为列表长度 |
| `parent_path` | Content、Section | 原样保存列表 |
| `parent_node_id` | Content；Section 主键候选 | 非空时作为 Content 所属 Section；为空时按路径规则回退 |
| `chunk_summary` | Content；Section 可选摘要 | 原样保存空字符串或摘要；不要求 LLM 补写 |
| `page_number` | Content | 原样保存，包括 `null` |
| `previous_leaf_id` | Content；PREVIOUS/NEXT 端点 | 原样保存；非空时规则创建相邻关系 |
| `next_leaf_id` | Content；NEXT/PREVIOUS 端点 | 原样保存；非空时规则创建相邻关系 |

### 17.1 字段赋值的统一伪代码

```python
meta = record['metadata']
content = {
    **meta,
    'text': record['page_content'],
    'document_id': meta['document_id'],
}
document = {k: meta[k] for k in DOCUMENT_METADATA_FIELDS}
section = {
    'title': meta['heading_path_parts'][-1],
    'heading_path': meta['heading_path'],
    'heading_path_parts': meta['heading_path_parts'],
    'parent_path': meta['parent_path'],
    'parent_node_id': meta['parent_node_id'],
    'level': len(meta['heading_path_parts']),
    'document_id': meta['document_id'],
}
```

其中 `DOCUMENT_METADATA_FIELDS` 是表中标记为 Document 的字段集合。`document`、`section`、`content` 均由规则层构造后写入 Neo4j；LLM 只能读取 `content.text`，输出 Entity、FactBlock 和候选业务关系，不能覆盖这些字段。

### 17.2 元数据作为检索路径

检索器应保留并使用这些字段进行过滤和导航：

```text
document_id / parent_document_id → 文件与附件回溯
heading_path / parent_node_id    → 章节定位
chunk_index / previous_leaf_id / next_leaf_id → 前后文扩展
publication_date / file_version  → 版本筛选
file_role / attachment_*         → 主文件与附件范围筛选
source_file / source_path        → 证据出处展示
page_number / content_hash       → 原文定位与去重
```

因此，元数据不是普通附加信息，而是 OHN-GraphRAG 的确定性导航索引；任何元数据缺失、改写或错挂节点都会直接影响后续 RAG 的证据回溯。

## 18. Content 与 FactBlock 开发明细

### 18.1 职责边界

```text
Content：原文和元数据的确定性证据单元
FactBlock：从原文抽取出的结构化事实单元
```

Content 解决“原文在哪里、原文是什么”；FactBlock 解决“原文表达了什么事实及其限定条件”。二者不能互相替代。

### 18.2 Content 节点

每条 JSONL 记录必须创建或合并一个 Content。`text/page_content` 原样保存，35 个上游元数据字段完整保存。Content 由规则构造，LLM 不得改写正文、来源、路径和顺序。

核心属性包括：

```text
chunk_id、node_id、text/page_content、document_id、title
heading_path、heading_path_parts、parent_path、parent_node_id
chunk_index、document_chunk_count、previous_leaf_id、next_leaf_id
source_file、source_path、page_number、content_hash
以及 chunks.jsonl 中其余元数据字段
```

### 18.3 FactBlock 创建条件

以下情况创建 FactBlock：

```text
一个 Chunk 中存在两个以上实体及明确语义关系
关系包含基础、条件、例外、时间、地区、比例或金额限定
同一上下文包含多个相互关联的属性事实
需要隔离不同 Chunk、文件或时间中的属性值
```

没有复杂限定的简单事实可以直接保存为 Entity-Entity 业务关系，但仍须保留 `source_chunk_id` 和 `evidence_text`。

### 18.4 FactBlock 属性

```text
fact_block_id
source_chunk_id
source_document_id
evidence_text
valid_at、invalid_at
jurisdiction
confidence
status
ontology_id、ontology_version
```

`evidence_text` 必须能在 `Content.text` 中定位；`status` 使用 `accepted`、`pending_review` 或 `rejected`。

### 18.5 FactBlock 图结构

对“国家在社会主义公有制基础上实行计划经济”：

```text
Content -[:SUPPORTS]-> FactBlock
FactBlock -[:ABOUT]-> 国家
FactBlock -[:TARGETS]-> 计划经济
FactBlock -[:HAS_BASIS]-> 社会主义公有制
```

同时可建立规范关系：

```text
国家 -[:IMPLEMENTS {
  predicate_surface: "在社会主义公有制基础上实行",
  predicate_lemma: "实行",
  basis_text: "社会主义公有制",
  evidence_text: "国家在社会主义公有制基础上实行计划经济",
  source_chunk_id: "...",
  confidence: 0.95
}]-> 计划经济
```

`IMPLEMENTS` 是规范关系，`predicate_surface`、`basis_text` 和 `evidence_text` 用于保留原文细粒度语义。

### 18.6 抽取和写入流程

```text
规则创建 Content
→ LLM 抽取实体、规范关系和限定项
→ JSON、类型、关系端点和证据校验
→ 简单事实写入 Entity-Entity 关系
→ 复杂事实创建 FactBlock
→ Content-[:SUPPORTS]->FactBlock
→ 写入来源、时间、范围、置信度和本体版本
```

### 18.7 RAG 分工

```text
查询
→ 检索 Entity、业务关系或 FactBlock
→ 通过 SUPPORTS 找到 Content
→ 通过 CONTAINS 回溯 Section 和 Document
→ 通过 NEXT/PREVIOUS 补充上下文
→ 返回 Content.text 作为最终证据
```

FactBlock 用于精准筛选和组合事实，Content 用于最终原文证明。生成器不得跳过 Content 原文。

### 18.8 质量约束

```text
每个 FactBlock 至少有一个 SUPPORTS Content
evidence_text 必须命中 Content.text
FactBlock 必须连接相关实体
业务关系和 FactBlock 必须有 source_chunk_id
无法验证的事实进入 pending_review，不进入正式检索
```

统一模型：

```text
Document → Section → Content → Entity
                              ↓
                          FactBlock
                              ↓
                       复杂业务事实
```
