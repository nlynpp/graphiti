# OHN-GraphRAG 知识图谱落地设计

## 1. 总体模型

统一图谱由四类节点组成：`Document`（文件）、`Section`（目录/章节）、`Content`（Chunk 原文）、`Entity`（本体实体）。核心路径为：

```text
Document → Section → Content → Entity
```

实体和业务关系用于定位知识，最终回答必须回溯到 Content 原文。

## 2. 输入字段映射

目标文件：`G:/实习/律所RAG/OHN-GraphRAG_chunks_test/chunks.jsonl`

| 字段 | 用途 |
|---|---|
| `document_id` | Document ID |
| `document_title` | 文档名称 |
| `source_file` / `source_path` | 来源信息 |
| `file_version` / `publication_date` / `document_number` | 版本和时间 |
| `chunk_id` / `node_id` | Content ID |
| `page_content` | Content 原文 |
| `title` | 当前章节/条款标题 |
| `heading_path` | 完整目录路径 |
| `heading_path_parts` | 层级标题数组 |
| `parent_path` | 父目录路径 |
| `parent_node_id` | 父目录 ID |
| `chunk_index` | 文档顺序 |
| `previous_leaf_id` / `next_leaf_id` | 前后 Chunk |
| `chunk_summary` / `content_hash` | 摘要和去重 |

## 3. 节点抽象

### Document

按 `document_id` 去重，保存标题、来源、版本、发布日期、文件编号和内容哈希。

### Section

根据 `heading_path_parts` 创建目录节点。`level` 为标题数组长度。父节点优先级为：

```text
parent_node_id > parent_path > heading_path_parts
```

### Content

每条 JSONL 记录对应一个 Content，保存 `chunk_id`、`page_content`、标题、路径、顺序、来源和元数据。

### Entity

实体按法律本体归类，而不是只保存关键词。推荐类型：

```text
Organization、LegalDocument、Law、Regulation、Article、LegalConcept、
Person、Court、Case、Party、Condition、Exception、Penalty、Amount、
Date、Region、Evidence、Procedure
```

实体保存标准名、类型、别名、描述、置信度和来源 Chunk。

## 4. 三类关系

### 4.1 层级结构关系

只表示文档位置和归属，不表达法律事实：

```text
Document -[:CONTAINS]-> Section
Section -[:CONTAINS]-> Section
Section -[:CONTAINS]-> Content
Content -[:MENTIONS]-> Entity
```

`MENTIONS` 只表示实体在 Chunk 中出现，不能解释为业务关系。

### 4.2 导航增强关系

帮助 Agent 沿图谱继续找证据：

```text
Content -[:NEXT]-> Content
Content -[:PREVIOUS]-> Content
Content -[:CITES]-> Content/Document
Content -[:SIMILAR_TO {similarity}]-> Content
Entity -[:CO_OCCURS {source_chunk_id}]-> Entity
```

`NEXT/PREVIOUS` 使用 `previous_leaf_id`、`next_leaf_id` 或 `chunk_index` 建立；`CITES` 来自条款号、文件号和脚注；`SIMILAR_TO` 来自向量相似度；`CO_OCCURS` 只能说明可能相关。

### 4.3 本体业务关系

业务关系必须满足：两端类型符合本体、原文明确表达、能够回溯来源 Chunk、通过规则或置信度校验。

推荐关系：

```text
LegalDocument -[:ISSUED_BY]-> Organization
LegalDocument -[:AMENDS|REPEALS]-> LegalDocument
LegalDocument -[:CONTAINS_ARTICLE]-> Article
LegalDocument -[:DEFINES]-> LegalConcept
LegalConcept -[:APPLIES_TO]-> Party/Organization
LegalConcept -[:REQUIRES]-> Condition
LegalConcept -[:EXCEPTS]-> Exception
LegalConcept -[:HAS_PENALTY]-> Penalty
LegalConcept -[:HAS_AMOUNT]-> Amount
LegalDocument -[:EFFECTIVE_ON]-> Date
LegalDocument -[:APPLIES_IN]-> Region
```

业务关系必须保存 `source_chunk_id`、`source_document_id`、`evidence_text`、`confidence`、`valid_at` 和 `invalid_at`。

## 5. 层级构建规则

1. 先创建 Document，再创建 Section，最后创建 Content。
2. 有 `heading_path` 时必须恢复 `Document → Section → Content`，不能只把路径当普通字符串。
3. 优先使用 `parent_node_id`，避免同名章节混淆。
4. Content 与 Entity 必须通过 `MENTIONS` 连接，保证实体可追溯到原文、章节和文件。
5. 层级关系 `CONTAINS`、导航关系 `NEXT/CITES/SIMILAR_TO` 和业务关系必须分开。
6. 每条业务关系必须绑定原文 Chunk；无法验证的关系不入图。

## 6. 推荐建图顺序

```text
读取并校验 JSONL
→ 合并 Document
→ 合并 Section
→ 创建 Content
→ 建立 CONTAINS
→ 建立 NEXT/PREVIOUS
→ 按本体抽取 Entity
→ 建立 MENTIONS
→ 抽取并校验业务关系
→ 建立 CITES、SIMILAR_TO、CO_OCCURS
→ 创建索引并输出统计
```

## 7. 检索导航

```text
初始 Content
→ MENTIONS 找实体
→ 业务关系找适用对象、条件、例外和数值
→ CONTAINS 回到章节和文件
→ NEXT/PREVIOUS 读取上下文
→ CITES 跳转被引用条款
→ SIMILAR_TO 补充相似表达
→ 回读 Content 原文作为证据
```

## 8. Neo4j 校验查询

```cypher
MATCH (n) RETURN labels(n) AS labels, count(n) AS count;
MATCH (c:Content)-[:MENTIONS]->(e:Entity) RETURN c, e LIMIT 50;
MATCH (a)-[r]->(b) WHERE r.source_chunk_id IS NOT NULL RETURN a, r, b LIMIT 50;
MATCH (d:Document)-[:CONTAINS*1..3]->(x) RETURN d, x LIMIT 100;
```

至少验证：每个 Content 能追溯到 Document；每个 Entity 至少有一条 `MENTIONS`；每条业务关系有 `source_chunk_id`；相邻关系与 `chunk_index` 一致；不存在孤立节点和违反本体的关系。

## 9. 与 Graphiti 的落地分工

Graphiti 负责 Content/episode 写入、实体抽取、业务关系抽取、Embedding 和混合检索。文档解析器或额外导入程序负责显式创建 Document、Section、`CONTAINS`、`NEXT/PREVIOUS` 和基于元数据的层级导航。两部分最终写入同一个 Neo4j 图谱。
