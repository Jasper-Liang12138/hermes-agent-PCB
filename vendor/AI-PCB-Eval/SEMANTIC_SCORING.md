# Semantic Scoring Design

本文档专门说明 `s1` 的计算逻辑，包括：

- KiCad 代码语义相似度如何计算
- 普通文本如何参与评分
- 两部分如何融合成最终语义分

对应代码位置：

- [semantic.py](/H:/Program/MindSpeed-LLM/eval/semantic.py)
- [kicad_utils.py](/H:/Program/MindSpeed-LLM/eval/kicad_utils.py)
- [types.py](/H:/Program/MindSpeed-LLM/eval/types.py)

---

## 1. 整体流程

语义评分入口是 [semantic.py](/H:/Program/MindSpeed-LLM/eval/semantic.py) 中的 `KiCadSemanticScorer.score()`。

核心流程如下：

1. 从 `prediction_raw` 中抽取候选 KiCad 代码
2. 判断回复里是否包含 KiCad 风格代码
3. 对预测代码和 `label` 做 KiCad 归一化
4. 计算 KiCad 分支的 5 个子指标
5. 计算普通文本分支的轻量相似度
6. 将 KiCad 分支和文本分支融合成最终 `s1`

对应主代码：

```python
prediction_code, has_kicad_code, extracted_blocks = extract_kicad_or_text(sample.prediction_raw)
...
kicad_score = max(0.0, min(1.0, score))
prediction_text = extract_plain_text(sample.prediction_raw)
label_text = extract_plain_text(sample.label)
text_score = self._text_similarity(prediction_text, label_text)
semantic_score = self._combine_semantic_scores(kicad_score, text_score, label_text)
```

位置：

- [semantic.py](/H:/Program/MindSpeed-LLM/eval/semantic.py)

---

## 2. KiCad 代码抽取

### 2.1 Markdown code block 提取

首先从模型回复中提取 fenced code block：

```python
CODE_FENCE_RE = re.compile(r"```(?:[a-zA-Z0-9_+-]+)?\s*\n(.*?)```", re.DOTALL)
```

```python
def extract_code_blocks(text: str) -> List[str]:
    return [match.strip() for match in CODE_FENCE_RE.findall(text or "") if match.strip()]
```

位置：

- [kicad_utils.py](/H:/Program/MindSpeed-LLM/eval/kicad_utils.py)

### 2.2 KiCad 风格识别

抽取出代码块后，用一组 KiCad S-expression 关键提示词判断是否像 KiCad：

```python
KICAD_S_EXPR_HINTS = (
    "(kicad_pcb",
    "(segment",
    "(via",
    "(arc",
    "(module",
    "(footprint",
    "(net ",
    "(gr_line",
    "(gr_arc",
    "(gr_text",
    "(zone",
)
```

```python
def looks_like_kicad(text: str) -> bool:
    if not text:
        return False
    lowered = text.lower()
    return any(hint in lowered for hint in KICAD_S_EXPR_HINTS)
```

位置：

- [kicad_utils.py](/H:/Program/MindSpeed-LLM/eval/kicad_utils.py)

### 2.3 选取最终用于 KiCad 比较的内容

优先选择“像 KiCad 的 code block”；如果没有，再退化到普通 code block；如果还没有，就直接用原始文本：

```python
def extract_kicad_or_text(text: str) -> Tuple[str, bool, List[str]]:
    blocks = extract_code_blocks(text)
    kicad_blocks = [block for block in blocks if looks_like_kicad(block)]
    if kicad_blocks:
        return "\n\n".join(kicad_blocks), True, blocks
    if blocks:
        joined = "\n\n".join(blocks)
        return joined, looks_like_kicad(joined), blocks
    stripped = (text or "").strip()
    return stripped, looks_like_kicad(stripped), []
```

这样可以兼容：

- 只输出代码
- 解释文字 + 代码块
- 没有 fenced code block、直接输出 KiCad

---

## 3. KiCad 特殊处理

普通字符串比较不适合 KiCad，因为 KiCad 是结构化 S-expression。当前实现做了 4 类专门处理。

### 3.1 归一化空白、换行、注释、数字

归一化函数：

```python
def normalize_kicad(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r";.*?$", "", text, flags=re.MULTILINE)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    ...
    compact = NUMBER_RE.sub(_normalize_number, compact)
    compact = re.sub(r"\s*\(\s*", " (", compact)
    compact = re.sub(r"\s*\)\s*", ") ", compact)
    compact = re.sub(r"\s+", " ", compact).strip()
    return compact
```

数字归一化逻辑：

```python
def _normalize_number(match: re.Match[str]) -> str:
    value = float(match.group(0))
    if math.isclose(value, round(value), abs_tol=1e-9):
        return str(int(round(value)))
    return f"{value:.6f}".rstrip("0").rstrip(".")
```

这可以消除很多与语义无关的差异，比如：

- `0.200000` 和 `0.2`
- 多余空格
- 不同换行格式

位置：

- [kicad_utils.py](/H:/Program/MindSpeed-LLM/eval/kicad_utils.py)

### 3.2 KiCad token 提取

不是做普通中文/英文分词，而是做 S-expression 风格 token 切分：

```python
KICAD_TOKEN_RE = re.compile(r"\(|\)|\"[^\"]*\"|[^\s()]+")

def tokenize_kicad(text: str) -> List[str]:
    return KICAD_TOKEN_RE.findall(normalize_kicad(text).lower())
```

例如：

```lisp
(segment (start 1 1) (end 2 2) (width 0.2) (layer Top) (net 1))
```

会保留：

- `segment`
- `start`
- `end`
- `width`
- `layer`
- `net`
- 以及括号结构

位置：

- [kicad_utils.py](/H:/Program/MindSpeed-LLM/eval/kicad_utils.py)

### 3.3 数值抽取

KiCad 中数值本身就是重要语义，例如：

- 坐标 `start/end/at`
- 线宽 `width`
- 钻孔 `drill`
- 过孔尺寸 `size`

对应函数：

```python
def extract_numeric_values(text: str) -> List[float]:
    values = []
    for item in NUMBER_RE.findall(text or ""):
        try:
            values.append(float(item))
        except ValueError:
            continue
    return values
```

位置：

- [kicad_utils.py](/H:/Program/MindSpeed-LLM/eval/kicad_utils.py)

### 3.4 结构特征抽取

除了 token 和数值，还会统计对象级结构特征：

```python
def extract_structural_features(text: str) -> Dict[str, int]:
    normalized = normalize_kicad(text).lower()
    features = {
        "segment": normalized.count("(segment"),
        "via": normalized.count("(via"),
        "arc": normalized.count("(arc"),
        "zone": normalized.count("(zone"),
        "net": normalized.count("(net "),
        "layer": normalized.count("(layer "),
        "start": normalized.count("(start "),
        "end": normalized.count("(end "),
        "width": normalized.count("(width "),
    }
    features["paren_balance_abs"] = abs(normalized.count("(") - normalized.count(")"))
    return features
```

位置：

- [kicad_utils.py](/H:/Program/MindSpeed-LLM/eval/kicad_utils.py)

---

## 4. KiCad 分支如何打分

KiCad 分支由 5 个子指标组成。

### 4.1 整体字符串相似度

```python
"sequence_ratio": SequenceMatcher(None, normalized_pred, normalized_label).ratio()
```

作用：

- 反映整体形式是否接近
- 但不作为唯一依据

### 4.2 token 集合相似度

```python
def jaccard_similarity(tokens_a, tokens_b) -> float:
    set_a, set_b = set(tokens_a), set(tokens_b)
    ...
    return len(set_a & set_b) / len(set_a | set_b)
```

作用：

- 比较两边是否用了类似的 KiCad 结构词汇

### 4.3 token 频次重合度

```python
def weighted_counter_similarity(tokens_a, tokens_b) -> float:
    counter_a = Counter(tokens_a)
    counter_b = Counter(tokens_b)
    ...
    return overlap / total if total else 1.0
```

作用：

- 比较相同结构元素出现的次数是否接近

### 4.4 数值相似度

```python
def number_similarity(values_a, values_b, tol: float = 1e-4) -> float:
    ...
    return (2 * matched) / (len(values_a) + len(values_b))
```

作用：

- 比较坐标、线宽、尺寸等数值是否接近

### 4.5 结构特征相似度

```python
def feature_similarity(features_a, features_b) -> float:
    ...
    return max(0.0, 1.0 - (sum(penalties) / len(penalties)))
```

作用：

- 比较 `segment/via/net/layer/start/end/width` 等统计特征是否接近

### 4.6 KiCad 分支融合

最终 KiCad 分支分数：

```python
score = (
    0.20 * metrics["sequence_ratio"]
    + 0.25 * metrics["token_jaccard"]
    + 0.25 * metrics["token_overlap"]
    + 0.15 * metrics["number_score"]
    + 0.15 * metrics["feature_score"]
)
kicad_score = max(0.0, min(1.0, score))
```

位置：

- [semantic.py](/H:/Program/MindSpeed-LLM/eval/semantic.py)

---

## 5. 普通文本如何参与评分

用户可能会输出：

- “缺失了几条线，现在我来帮你补全”
- “下面是我补全的 via”
- “我先添加一条连接线，再打一颗过孔”

这些内容虽然不是 KiCad 代码，但也携带一定语义。

因此当前实现单独计算一个轻量文本分支。

### 5.1 去掉 code block

```python
def strip_code_blocks(text: str) -> str:
    return CODE_FENCE_RE.sub(" ", text or "")
```

```python
def extract_plain_text(text: str) -> str:
    stripped = strip_code_blocks(text)
    lines = [line.strip() for line in stripped.splitlines()]
    return "\n".join(line for line in lines if line).strip()
```

作用：

- 去掉 fenced code block
- 只保留普通解释性文本

位置：

- [kicad_utils.py](/H:/Program/MindSpeed-LLM/eval/kicad_utils.py)

### 5.2 轻量文本相似度

普通文本分支不做复杂结构解析，只做：

- 字符串相似度
- token 集合相似度
- token 频次重合度

代码：

```python
def _text_similarity(self, prediction_text: str, label_text: str) -> float:
    if not label_text.strip():
        return 1.0
    pred = " ".join(prediction_text.split()).lower()
    label = " ".join(label_text.split()).lower()
    ...
    sequence_score = SequenceMatcher(None, pred, label).ratio()
    token_jaccard = jaccard_similarity(pred_tokens, label_tokens)
    token_overlap = weighted_counter_similarity(pred_tokens, label_tokens)
    return max(0.0, min(1.0, 0.5 * sequence_score + 0.25 * token_jaccard + 0.25 * token_overlap))
```

位置：

- [semantic.py](/H:/Program/MindSpeed-LLM/eval/semantic.py)

---

## 6. 最终语义分如何融合

最终 `s1` 不是只看 KiCad，也不是只看文本，而是两者融合：

```python
def _combine_semantic_scores(self, kicad_score: float, text_score: float, label_text: str) -> float:
    if not label_text.strip():
        return kicad_score
    total_weight = self.config.semantic_kicad_weight + self.config.semantic_text_weight
    if total_weight <= 0:
        return kicad_score
    return (
        self.config.semantic_kicad_weight * kicad_score
        + self.config.semantic_text_weight * text_score
    ) / total_weight
```

默认权重定义在 [types.py](/H:/Program/MindSpeed-LLM/eval/types.py)：

```python
semantic_kicad_weight: float = 0.85
semantic_text_weight: float = 0.15
```

这意味着：

- KiCad 代码仍然是主体
- 普通解释文本只占较小权重

如果 `label` 中没有普通文本，则最终分数直接退化为 `kicad_score`，不会因为模型多写解释而被无意义拉低。

---

## 7. 输出中能看到哪些细节

`SemanticScore.detail` 当前会保留：

- `metrics`
- `kicad_score`
- `text_score`
- `semantic_kicad_weight`
- `semantic_text_weight`
- `prediction_features`
- `label_features`
- `prediction_text`
- `label_text`

这意味着你后续分析时可以单独看：

- KiCad 主体是不是对
- 普通文本解释是不是像
- 最终融合后为什么得到这个分数

---

## 8. 当前优点与局限

### 优点

- 比纯字符串相似度更适合 KiCad
- 对格式噪声更稳
- 能把解释性文本也纳入语义评分
- 完全离线，不依赖 embedding 模型

### 局限

- 还不是 AST 级语义比较
- 数值比较目前仍是全局数值集合匹配，不区分字段语义
- 普通文本分支是轻量规则，不做深层语义理解
- 对复杂拓扑等价关系的判断还不够强

---

## 9. 推荐的后续增强方向

如果后续要继续增强 `s1`，最值得做的是：

1. KiCad 对象级比较
2. 字段绑定的数值比较
3. net / pad / layer 拓扑级比较
4. 普通文本分支改成 embedding 相似度
