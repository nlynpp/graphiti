# OHN-GraphRAG 实体去重与消歧归一化方案

## 1. 目标与原则

本方案解决两个相反问题：同一实体被重复构建，以及不同实体因名称相似被错误合并。

```text
同名不必然同一实体
异名可能指向同一实体
实体合并必须保守，且必须可追溯、可回滚
```

实体去重与消歧属于抽取后的程序控制层。LLM 负责提出实体提及和候选类型，程序负责候选召回、别名匹配、上下文判定、唯一键生成和最终入库。

## 2. 节点与提及分离

全局实体节点不使用 Chunk 级 ID。每次文本出现通过 `MENTIONS` 关系连接，并在关系上保存本次提及证据。

```text
(:Content)-[:MENTIONS {
  mention_text,
  source_text,
  source_chunk_id,
  source_document_id,
  resolution,
  resolution_confidence
}]->(:Entity)
```

Entity 节点保存稳定属性：

```text
entity_key
canonical_name
entity_type
aliases
description
normalization_status
confidence
ontology_id
ontology_version
```

同一实体出现在多个 Chunk 时，只保留一个 Entity，建立多条带来源的 `MENTIONS` 关系。

## 3. 全局唯一键

推荐使用：

```text
entity_key = entity_type + ":" + canonical_id
```

示例：

```text
region:china
enterprise:apple_inc
material:apple_fruit
legal_concept:private_economy
```

禁止仅用名称作为唯一键，因为“苹果公司/苹果水果”或“申请（程序）/申请（业务事项）”可能同名不同义。

没有可靠规范 ID 时，可暂时以规范名生成候选键，但必须保留 `pending_review` 状态，不能进行高风险全局合并。

## 4. 去重流程

```text
LLM 提取实体提及
→ 名称和标点清洗
→ 类型标准化
→ 同一 Chunk 内去重
→ 强别名匹配
→ 同类型同规范名精确匹配
→ 同文档上下文匹配
→ 候选实体评分
→ 合并、保持独立或进入待审核
→ 写入 Entity 和 MENTIONS
```

### 4.1 名称清洗

规则包括：去除首尾空格、统一全角半角、压缩连续空白、统一括号和标点、去除不具有区分作用的引号。清洗只生成匹配键，不覆盖原文 `mention_text`。

### 4.2 同 Chunk 去重

同一 Chunk 内按以下键去重：

```text
(normalized_entity_type, normalized_canonical_name)
```

重复提及合并为一个候选实体，所有原文片段保留在 `MENTIONS` 或提及证据列表中。

### 4.3 跨 Chunk 去重

按以下顺序匹配：

1. 外部 ID或人工确认的规范 ID。
2. `(实体类型, 规范名称)` 精确匹配。
3. 强别名词典匹配。
4. 同一文档内的上下文匹配。
5. 向量相似度和属性相似度候选召回。
6. LLM 消歧。

无法确定时保留不同实体，不强行合并。

## 5. 消歧决策

### 5.1 确定合并

满足以下任一条件且无冲突时合并：

```text
命中人工维护的强别名
外部规范 ID 相同
实体类型相同、规范名相同、上下文无冲突
```

### 5.2 保持独立

出现以下情况不得合并：

```text
实体类型不同
描述或属性互相冲突
关系集合指向明显不同
上下文明确属于不同领域对象
```

例如：

```text
Apple Inc. / Enterprise
苹果 / Material
```

### 5.3 待审核

以下情况进入 `pending_review`：

```text
候选实体评分接近
只有简称或代词，缺少上下文
别名跨领域使用
LLM 类型判断不稳定
```

待审核记录必须保存候选实体、相似度、上下文、来源 Chunk 和决策原因。

## 6. 别名词典

别名必须分级管理：

```yaml
aliases:
  - canonical_name: 中华人民共和国
    canonical_id: china
    entity_type: Region
    aliases: [中国]
    alias_level: strong
    confidence: 1.0

  - canonical_name: Apple Inc.
    canonical_id: apple_inc
    entity_type: Enterprise
    aliases: [Apple, 苹果公司]
    alias_level: strong
    confidence: 1.0

  - canonical_name: Apple Inc.
    canonical_id: apple_inc
    entity_type: Enterprise
    aliases: [苹果]
    alias_level: conditional
    conditions: [出现 iPhone、Mac、公司、发布、产品等企业上下文]
    confidence: 0.8
```

“中华人民共和国—中国”可作为强别名；“苹果—Apple Inc.”只能作为条件别名，必须结合上下文，不能全局无条件合并。

## 7. 上下文特征

消歧至少使用以下特征：

```text
实体类型
当前句和相邻句
所属 Chunk、Section、Document
附近实体
已建立的业务关系
时间和地区
描述、属性和外部 ID
```

例如：

```text
Apple + iPhone + 发布 → Apple Inc.
苹果 + 水果 + 食用 → 苹果水果
```

“我国”只有在当前文档语境已确定指向中华人民共和国时，才可作为条件别名处理。

## 8. 候选评分建议

可采用可解释的加权评分：

```text
外部 ID 相同                         +1.00
强别名命中                           +0.95
类型和规范名精确匹配                 +0.90
同文档上下文一致                     +0.20
关系和属性一致                       +0.20
向量相似度                           ×0.20
类型冲突                             -1.00
属性或关系明显冲突                   -0.60
```

建议阈值：

```text
score ≥ 0.90       自动合并
0.70 ≤ score < .90 进入待审核或要求 LLM 消歧
score < 0.70       保持独立
```

阈值必须通过人工标注样本评估后调整，不能把向量相似度单独作为合并依据。

## 9. Neo4j 写入约束

Entity 使用全局键幂等写入：

```cypher
MERGE (e:Entity {entity_key: $entity_key})
SET e.canonical_name = $canonical_name,
    e.entity_type = $entity_type,
    e.aliases = $aliases,
    e.normalization_status = $status,
    e.ontology_id = $ontology_id,
    e.ontology_version = $ontology_version
```

提及关系保存本次证据：

```cypher
MATCH (c:Content {chunk_id: $chunk_id})
MATCH (e:Entity {entity_key: $entity_key})
MERGE (c)-[m:MENTIONS {mention_key: $mention_key}]->(e)
SET m.mention_text = $mention_text,
    m.source_text = $source_text,
    m.source_chunk_id = $chunk_id,
    m.source_document_id = $document_id,
    m.resolution = $resolution,
    m.resolution_confidence = $confidence
```

禁止使用 `llm:{chunk_id}:{id}` 作为正式全局 Entity ID；该形式只能作为抽取阶段的临时候选 ID。

## 10. 合并、回滚与审计

每次自动合并必须记录：

```text
旧实体键、新实体键、触发规则、候选分数、来源 Chunk、时间、操作者或模型版本
```

发现误合并时，应依据审计记录拆分 Entity，并恢复对应 `MENTIONS` 关系；不得直接删除来源证据。

## 11. 当前项目实施顺序

```text
1. 新增名称清洗和 EntityNormalizer
2. 统一规则实体与 LLM 实体入口
3. 同 Chunk 去重
4. 全局 entity_key 合并
5. 将 source_chunk_id 等来源迁移到 MENTIONS
6. 建立强别名词典和条件别名词典
7. 增加 pending_review 队列
8. 重新构建图谱并统计重复率、误合并率和待审核率
```

最终目标是：一个全局实体节点可以被多个 Content 引用；不同语义实体保持独立；每次归一化都有可解释依据和原文证据。
