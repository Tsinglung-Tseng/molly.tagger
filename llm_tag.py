#!/usr/bin/env python3
"""
LLM-based entity tagger.

使用 LLM 直接提取笔记实体，置信度由 LLM 自评估，存入 note_entities.confidence 字段。
支持任意 OpenAI 兼容 API（SiliconFlow、OpenAI、Ollama 等）。

用法:
    python llm_tag.py file <note.md>          # 标注单文件
    python llm_tag.py all <vault_dir>          # 批量标注
    python llm_tag.py review-items             # 输出低置信度实体 JSON（供 skill 使用）
"""

import os
import sqlite3
import requests
import json
import argparse
import time
import yaml
import hashlib
import re
from pathlib import Path
from typing import List, Dict, Optional, Tuple
import frontmatter as fm


DB_PATH = Path(__file__).parent / "entities.db"

# LightRAG-style delimiters
TUPLE_DELIMITER = "<|#|>"
COMPLETION_DELIMITER = "<|COMPLETE|>"

ENTITY_TYPES_STR = "PERSON,ORG,LOC,GPE,WORK_OF_ART,PRODUCT,EVENT,METHOD,NORP,LAW,TITLE"

EXTRACTION_SYSTEM_PROMPT = f"""---Role---
You are a Named Entity Recognition specialist. Extract named entities from the input text.

---Entity Types---
Use ONLY these entity types:
- PERSON: specific person names (马斯克, 李彦宏, Musk)
- ORG: organizations/companies (百度, OpenAI, 清华大学)
- LOC: locations (北京, 硅谷, Silicon Valley)
- GPE: countries/cities (中国, 美国, Shanghai)
- WORK_OF_ART: works (《三体》, 黑神话悟空)
- PRODUCT: products/software/tools/hardware/AI models (iPhone, Claude, vLLM, PyTorch, RTX4090, Qwen3.5-9B, CUDA)
- EVENT: specific events (世界杯2022)
- METHOD: theories/methods/concepts (深度学习, RAG, 强化学习)
- NORP: ethnic/religious/political groups
- LAW: laws/regulations (民法典)
- TITLE: specific titles (CEO, 首席科学家 — only when referring to a specific person)

Do NOT extract: generic job titles, common nouns, overly broad concepts (AI, 技术, 数据), single-character entities.

---Output Format---
For each entity output ONE line:
entity{TUPLE_DELIMITER}<entity_name>{TUPLE_DELIMITER}<entity_type>{TUPLE_DELIMITER}<brief description>

After all entities, output: {COMPLETION_DELIMITER}

Output ONLY entity lines and the completion delimiter. No JSON, no explanations.

---Example---
Input: 使用 vLLM 在 RTX4090 上部署 Qwen3.5-9B，需要 CUDA 12.1 和 PyTorch
Output:
entity{TUPLE_DELIMITER}vLLM{TUPLE_DELIMITER}PRODUCT{TUPLE_DELIMITER}开源LLM推理框架
entity{TUPLE_DELIMITER}RTX4090{TUPLE_DELIMITER}PRODUCT{TUPLE_DELIMITER}NVIDIA高端显卡
entity{TUPLE_DELIMITER}Qwen3.5-9B{TUPLE_DELIMITER}PRODUCT{TUPLE_DELIMITER}阿里千问系列语言模型
entity{TUPLE_DELIMITER}CUDA 12.1{TUPLE_DELIMITER}PRODUCT{TUPLE_DELIMITER}NVIDIA并行计算平台
entity{TUPLE_DELIMITER}PyTorch{TUPLE_DELIMITER}PRODUCT{TUPLE_DELIMITER}深度学习框架
{COMPLETION_DELIMITER}
"""


def load_config() -> dict:
    cfg_path = Path(__file__).parent / "config.yaml"
    if cfg_path.exists():
        with open(cfg_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}


def clean_text(text: str) -> str:
    """移除 Markdown 语法，保留纯文本供 LLM 分析"""
    # 代码块：保留第一行（往往是命令/工具名），去掉其余内容
    def keep_first_line(m):
        lines = m.group(0).split('\n')
        # lines[0] = ```lang, lines[-1] = ``` — 取中间第一行
        inner = [l.strip() for l in lines[1:-1] if l.strip()]
        return inner[0] if inner else ''
    text = re.sub(r'```[\s\S]*?```', keep_first_line, text)
    text = re.sub(r'`[^`\n]+`', '', text)
    text = re.sub(r'\[\[([^\]|]+)(?:\|[^\]]+)?\]\]', r'\1', text)
    text = re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', text)
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'^#+\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


ALLOWED_LABELS = {
    'PERSON', 'ORG', 'LOC', 'GPE', 'NORP',
    'WORK_OF_ART', 'EVENT', 'FAC', 'LAW', 'LANGUAGE',
    'TITLE', 'PRODUCT', 'METHOD'
}


def _build_system_prompt(custom_types: list) -> str:
    """在内置 prompt 的实体类型列表末尾插入自定义类型"""
    if not custom_types:
        return EXTRACTION_SYSTEM_PROMPT
    custom_lines = "\n".join(
        "- {name}: {desc}{ex}".format(
            name=ct["name"].upper(),
            desc=ct.get("description", ""),
            ex=f" (e.g. {ct['examples']})" if ct.get("examples") else "",
        )
        for ct in custom_types
        if ct.get("name")
    )
    insert_marker = "Do NOT extract:"
    return EXTRACTION_SYSTEM_PROMPT.replace(
        insert_marker,
        custom_lines + "\n" + insert_marker,
    )


def _build_allowed_labels(custom_types: list) -> set:
    """内置允许标签 + 用户自定义类型名"""
    labels = set(ALLOWED_LABELS)
    for ct in custom_types:
        if ct.get("name"):
            labels.add(ct["name"].upper())
    return labels


# 模型偶尔返回非标准标签时的兜底映射
LABEL_REMAP = {
    'FRAMEWORK': 'PRODUCT',
    'LIBRARY':   'PRODUCT',
    'SOFTWARE':  'PRODUCT',
    'HARDWARE':  'PRODUCT',
    'MODEL':     'PRODUCT',
    'TOOL':      'PRODUCT',
    'DATASET':   'PRODUCT',
    'ALGORITHM': 'METHOD',
    'CONCEPT':   'METHOD',
    'COUNTRY':   'GPE',
    'CITY':      'GPE',
    'LOCATION':  'LOC',
    'COMPANY':   'ORG',
    'INSTITUTE': 'ORG',
}


def _parse_lightrag_response(raw: str) -> List[Dict]:
    """解析 LightRAG 分隔符格式的实体提取结果"""
    # 去掉 thinking 块和 completion 标记
    raw = re.sub(r'<think>[\s\S]*?</think>', '', raw)
    raw = raw.replace(COMPLETION_DELIMITER, '').strip()

    entities = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or not line.startswith('entity'):
            continue
        fields = line.split(TUPLE_DELIMITER)
        if len(fields) < 3:
            continue
        text = fields[1].strip().strip('"\'')
        label = fields[2].strip().upper()
        if text and label:
            entities.append({"text": text, "label": label, "confidence": 0.85})
    return entities


def call_llm(
    content: str,
    api_url: str,
    api_key: str,
    model: str,
    temperature: float = 0.1,
    max_tokens: int = 1000,
    system_prompt: str = None,
) -> Optional[List[Dict]]:
    """调用 LLM 提取实体，返回 [{text, label, confidence}] 或 None（失败）"""
    if system_prompt is None:
        system_prompt = EXTRACTION_SYSTEM_PROMPT
    if len(content) > 3000:
        content = content[:3000] + "…"

    user_prompt = f"""Extract entities from the text below.

<Input Text>
```
{content}
```

<Output>
"""

    try:
        resp = requests.post(
            api_url,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": temperature,
                "max_tokens": max_tokens,
            },
            timeout=90,
        )
        resp.raise_for_status()
        raw = resp.json()["choices"][0]["message"]["content"].strip()
        return _parse_lightrag_response(raw)

    except Exception as e:
        print(f"  API 错误: {e}")
        return None


class LLMTagger:
    def __init__(self, db_path: str = None, dry_run: bool = False):
        cfg = load_config()
        # 优先读取 llm_tagger 配置，回退到 llm 配置
        llm_cfg = cfg.get("llm_tagger", cfg.get("llm", {}))

        # Env vars injected by Molly DaemonWorker take precedence over config.yaml
        _env_api_url = os.environ.get("MOLLY_LLM_API_URL", "")
        # Molly injects base URL (e.g. https://api.siliconflow.cn/v1); append /chat/completions
        if _env_api_url and not _env_api_url.endswith("/chat/completions"):
            _env_api_url = _env_api_url.rstrip("/") + "/chat/completions"
        self.api_url = _env_api_url or llm_cfg.get("api_url", "https://api.siliconflow.cn/v1/chat/completions")
        self.model = os.environ.get("MOLLY_LLM_MODEL") or llm_cfg.get("model", "")
        self.api_key = os.environ.get("MOLLY_LLM_API_KEY") or llm_cfg.get("api_key", "")
        self.temperature = llm_cfg.get("temperature", 0.1)
        self.max_tokens = llm_cfg.get("max_tokens", 1000)

        self.db_path = Path(db_path) if db_path else DB_PATH
        self.dry_run = dry_run
        self.conn = None

        custom_types = cfg.get("custom_entity_types") or []
        self.system_prompt = _build_system_prompt(custom_types)
        self.allowed_labels = _build_allowed_labels(custom_types)

    def connect(self):
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row

    def close(self):
        if self.conn:
            self.conn.close()

    # ── DB 辅助方法 ──────────────────────────────────────────────

    def _get_or_create_note(self, file_path: str, title: str, file_hash: str, mtime: float) -> int:
        cursor = self.conn.cursor()
        cursor.execute("""
            INSERT INTO notes (file_path, title, file_hash, file_mtime, last_processed)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(file_path) DO UPDATE SET
                title        = excluded.title,
                file_hash    = excluded.file_hash,
                file_mtime   = excluded.file_mtime,
                last_processed = CURRENT_TIMESTAMP,
                updated_at   = CURRENT_TIMESTAMP
        """, (file_path, title, file_hash, mtime))
        self.conn.commit()
        cursor.execute("SELECT id FROM notes WHERE file_path = ?", (file_path,))
        return cursor.fetchone()["id"]

    def _upsert_entity(self, text: str, label: str) -> int:
        cursor = self.conn.cursor()
        cursor.execute("""
            INSERT INTO entities (text, label, raw_text, llm_validated, validation_reason)
            VALUES (?, ?, ?, 1, 'llm_tag: LLM direct extraction')
            ON CONFLICT(text, label) DO UPDATE SET
                updated_at = CURRENT_TIMESTAMP
        """, (text, label, text))
        self.conn.commit()
        cursor.execute("SELECT id FROM entities WHERE text = ? AND label = ?", (text, label))
        return cursor.fetchone()["id"]

    def _upsert_note_entity(self, note_id: int, entity_id: int, confidence: float):
        cursor = self.conn.cursor()
        cursor.execute("""
            INSERT INTO note_entities (note_id, entity_id, count, confidence)
            VALUES (?, ?, 1, ?)
            ON CONFLICT(note_id, entity_id) DO UPDATE SET
                confidence = MAX(confidence, excluded.confidence),
                count      = count + 1,
                last_seen  = CURRENT_TIMESTAMP
        """, (note_id, entity_id, confidence))
        self.conn.commit()

    # ── 核心方法 ─────────────────────────────────────────────────

    def tag_file(self, path: Path) -> Tuple[int, int]:
        """对单个文件提取并存储实体。返回 (LLM提取数, 有效保存数)"""
        try:
            post = fm.load(path)
        except Exception as e:
            print(f"  无法读取 {path.name}: {e}")
            return 0, 0

        title = post.get("title") or path.stem
        content = clean_text(post.content)

        if len(content.strip()) < 20:
            print(f"  跳过 {path.name}: 内容过短")
            return 0, 0

        file_hash = hashlib.md5(content.encode()).hexdigest()
        mtime = path.stat().st_mtime

        entities = call_llm(
            content, self.api_url, self.api_key,
            self.model, self.temperature, self.max_tokens,
            system_prompt=self.system_prompt,
        )

        if entities is None:
            print(f"  {path.name}: API 调用失败")
            return 0, 0

        # 兜底映射：将非标准标签替换为允许标签
        for e in entities:
            if isinstance(e, dict) and e.get("label") in LABEL_REMAP:
                e["label"] = LABEL_REMAP[e["label"]]

        valid = [
            e for e in entities
            if isinstance(e, dict)
            and e.get("text") and len(e["text"].strip()) >= 2
            and e.get("label") in self.allowed_labels
        ]

        print(f"  {path.name}: LLM提取 {len(entities)}, 有效 {len(valid)}")

        if self.dry_run:
            for e in valid:
                print(f"    [{e['label']}] {e['text']}  conf={e.get('confidence', '?'):.2f}")
            return len(entities), len(valid)

        note_id = self._get_or_create_note(str(path), title, file_hash, mtime)
        saved = 0
        for e in valid:
            try:
                eid = self._upsert_entity(e["text"].strip(), e["label"])
                conf = float(e.get("confidence", 0.8))
                self._upsert_note_entity(note_id, eid, conf)
                saved += 1
            except Exception as err:
                print(f"    保存失败 [{e['text']}]: {err}")

        return len(entities), saved

    def tag_all(self, vault_path: str, limit: int = None, force: bool = False):
        """批量处理 vault 中的所有笔记"""
        vault = Path(vault_path)
        md_files = sorted(vault.glob("**/*.md"))

        # 排除隐藏目录（.obsidian, .ner 等）
        md_files = [
            f for f in md_files
            if not any(part.startswith(".") for part in f.relative_to(vault).parts)
        ]

        if limit:
            md_files = md_files[:limit]

        print(f"共 {len(md_files)} 个笔记文件，使用模型: {self.model}")

        total_extracted = total_saved = 0
        for i, path in enumerate(md_files, 1):
            print(f"[{i}/{len(md_files)}]", end=" ")
            ext, saved = self.tag_file(path)
            total_extracted += ext
            total_saved += saved
            time.sleep(0.3)

        print(f"\n完成: LLM提取 {total_extracted}, 写入DB {total_saved}")


# ── CLI ──────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="LLM-based note tagger",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
子命令:
  file <path>          标注单个 .md 文件
  all <vault>          批量标注整个 vault
  review-items         输出低置信度实体 JSON（供 tag-review skill 调用）
        """
    )
    sub = parser.add_subparsers(dest="cmd")

    # file 子命令
    p_file = sub.add_parser("file", help="Tag a single note")
    p_file.add_argument("path", help=".md 文件路径")
    p_file.add_argument("--dry-run", action="store_true", help="仅显示，不写入 DB")

    # all 子命令
    p_all = sub.add_parser("all", help="Tag all notes in vault")
    p_all.add_argument("vault", help="Vault 根目录")
    p_all.add_argument("--limit", type=int, help="最多处理文件数")
    p_all.add_argument("--dry-run", action="store_true")

    # review-items 子命令（供 tag-review skill 使用）
    p_rev = sub.add_parser("review-items", help="Output low-confidence items as JSON")
    p_rev.add_argument("--threshold", type=float, default=0.7,
                       help="置信度阈值（低于此值的实体待审核）")
    p_rev.add_argument("--limit", type=int, default=50,
                       help="返回的最大实体数")
    p_rev.add_argument("--db", default=str(DB_PATH), help="数据库路径")

    args = parser.parse_args()

    if args.cmd == "file":
        tagger = LLMTagger(dry_run=args.dry_run)
        tagger.connect()
        try:
            tagger.tag_file(Path(args.path))
        finally:
            tagger.close()

    elif args.cmd == "all":
        tagger = LLMTagger(dry_run=args.dry_run)
        tagger.connect()
        try:
            tagger.tag_all(args.vault, limit=args.limit)
        finally:
            tagger.close()

    elif args.cmd == "review-items":
        conn = sqlite3.connect(args.db)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("""
            SELECT
                n.file_path,
                n.title,
                e.id   AS entity_id,
                e.text AS entity_text,
                e.label,
                ne.confidence,
                ne.count
            FROM note_entities ne
            JOIN entities e ON ne.entity_id = e.id
            JOIN notes n    ON ne.note_id   = n.id
            WHERE ne.confidence < ?
              AND e.is_deleted   = 0
              AND e.verified     = 0
            ORDER BY ne.confidence ASC, n.file_path
            LIMIT ?
        """, (args.threshold, args.limit))
        rows = [dict(r) for r in cursor.fetchall()]
        conn.close()
        print(json.dumps(rows, ensure_ascii=False, indent=2))

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
