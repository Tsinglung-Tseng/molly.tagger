-- Molly Tagger Database Schema
-- 支持 LLM 实体提取和实体属性管理

-- 笔记表：存储 Obsidian 笔记元信息
CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path TEXT NOT NULL UNIQUE,
    title TEXT,
    last_processed TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    file_hash TEXT NOT NULL,
    file_mtime REAL,  -- 文件修改时间戳
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_notes_file_path ON notes(file_path);
CREATE INDEX IF NOT EXISTS idx_notes_file_hash ON notes(file_hash);

-- 实体表：存储唯一命名实体
CREATE TABLE IF NOT EXISTS entities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    label TEXT NOT NULL,  -- PERSON, ORG, GPE, DATE, MONEY, PRODUCT, EVENT, LAW 等
    
    -- LLM 原始输出（保留未清洗的版本）
    raw_text TEXT,
    
    -- LLM 验证字段
    is_deleted BOOLEAN DEFAULT 0,  -- 软删除
    llm_validated INTEGER DEFAULT NULL,  -- 0=拒绝, 1=接受, NULL=未验证
    validation_reason TEXT,  -- LLM 验证理由
    
    -- 扩展属性
    properties TEXT,       -- JSON 格式存储额外属性
    verified BOOLEAN DEFAULT 0,  -- 是否经过人工验证
    
    -- 时间戳
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    
    UNIQUE(text, label)    -- 同一文本+标签组合唯一
);

CREATE INDEX IF NOT EXISTS idx_entities_text ON entities(text);
CREATE INDEX IF NOT EXISTS idx_entities_label ON entities(label);

-- 笔记-实体关联表：多对多关系
CREATE TABLE IF NOT EXISTS note_entities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    note_id INTEGER NOT NULL,
    entity_id INTEGER NOT NULL,
    
    -- 统计信息
    count INTEGER DEFAULT 1,           -- 在该笔记中出现次数
    positions TEXT,                    -- JSON 数组存储位置 [{"start": 10, "end": 15, "context": "..."}]
    confidence REAL DEFAULT 1.0,       -- 识别置信度 (0-1)
    
    -- 时间戳
    first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    
    FOREIGN KEY (note_id) REFERENCES notes(id) ON DELETE CASCADE,
    FOREIGN KEY (entity_id) REFERENCES entities(id) ON DELETE CASCADE,
    UNIQUE(note_id, entity_id)
);

CREATE INDEX IF NOT EXISTS idx_note_entities_note_id ON note_entities(note_id);
CREATE INDEX IF NOT EXISTS idx_note_entities_entity_id ON note_entities(entity_id);
CREATE INDEX IF NOT EXISTS idx_note_entities_count ON note_entities(count);

-- 创建视图：方便查询实体及其相关笔记
CREATE VIEW IF NOT EXISTS entity_notes_view AS
SELECT 
    e.id AS entity_id,
    e.text AS entity_text,
    e.label AS entity_label,
    n.id AS note_id,
    n.file_path,
    n.title,
    ne.count,
    ne.confidence,
    ne.positions
FROM entities e
JOIN note_entities ne ON e.id = ne.entity_id
JOIN notes n ON ne.note_id = n.id;

-- 创建视图：统计实体频率
CREATE VIEW IF NOT EXISTS entity_frequency_view AS
SELECT 
    e.id,
    e.text,
    e.label,
    COUNT(DISTINCT ne.note_id) AS note_count,
    SUM(ne.count) AS total_occurrences
FROM entities e
LEFT JOIN note_entities ne ON e.id = ne.entity_id
GROUP BY e.id
ORDER BY total_occurrences DESC;
