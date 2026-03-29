# molly.tagger

Obsidian 笔记自动打标签服务。监控 Vault 目录，当 `.md` 文件创建或修改时，调用 LLM 提取命名实体并回写到 frontmatter `tags`。

## 工作原理

```
文件变更 → watcher.py (watchdog)
               |
               v
         llm_tag.py — 调用 LLM 提取实体 → entities.db
               |
               v
        update_tags.py — 回写 frontmatter tags
```

支持的实体类型：`PERSON` / `ORG` / `LOC` / `GPE` / `WORK_OF_ART` / `PRODUCT` / `EVENT` / `METHOD` / `NORP` / `LAW`

支持通过 `config.yaml` 新增自定义实体类型（见下方配置说明）。

## 环境变量

由 Molly 框架注入，watcher 自身不提供配置模板。

| 变量 | 必填 | 说明 |
|------|------|------|
| `MOLLY_VAULT_PATH` | ✅ | Obsidian Vault 根目录绝对路径 |
| `MOLLY_LLM_API_KEY` | ✅ | LLM API Key |
| `MOLLY_LLM_API_URL` | 可选 | 覆盖 `config.yaml` 中的 `api_url` |
| `MOLLY_LLM_MODEL` | 可选 | 覆盖 `config.yaml` 中的 `model` |
| `MOLLY_DEBOUNCE_SEC` | 可选 | 文件变更防抖秒数，默认 `3.0` |

## 安装

```bash
uv sync
```

## 配置

编辑 `config.yaml`，设置 LLM 模型和 API 地址（可被环境变量覆盖）。支持任意 OpenAI 兼容 API：

```yaml
llm_tagger:
  api_url: https://api.openai.com/v1/chat/completions  # 或 SiliconFlow、Ollama 等
  model: gpt-4o-mini
  temperature: 0.1
  confidence_threshold: 0.7
```

### 自定义实体类型

在 `config.yaml` 中添加 `custom_entity_types` 即可扩展内置类型：

```yaml
custom_entity_types:
  - name: TECH_STACK      # 标签名（大写）
    description: "技术栈/框架组合"
    examples: "MERN, JAMStack, LAMP"
  - name: FOOD
    description: "食物/菜肴/餐饮品牌"
    examples: "北京烤鸭, 麻婆豆腐"
```

- `name`：必填，最终写入 frontmatter 的标签前缀（建议大写）
- `description`：发给 LLM 的类型说明
- `examples`：可选，逗号分隔的示例，帮助 LLM 理解范围

自定义类型会自动追加到 LLM prompt 并加入过滤白名单，无需修改代码。

## 运行

```bash
python watcher.py
python watcher.py --verbose   # DEBUG 日志
```

## 手动打标签

```bash
# 单文件
python llm_tag.py file /path/to/note.md

# 整个 Vault
python llm_tag.py all /path/to/vault

# 查看低置信度实体（供审查）
python llm_tag.py review-items
```
