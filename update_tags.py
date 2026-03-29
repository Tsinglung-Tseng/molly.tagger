#!/usr/bin/env python3
"""
Update Obsidian tags based on extracted entities.
"""

import sqlite3
import re
from pathlib import Path
from typing import List, Dict, Set
import frontmatter

DB_PATH = Path("entities.db")

def connect_db():
    if not DB_PATH.exists():
        raise FileNotFoundError(f"Database not found: {DB_PATH}")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def sanitize_tag(text: str) -> str:
    # Replace spaces and special chars with underscore
    # Remove any characters that are not allowed in Obsidian tags (e.g. #, comma, space)
    # Obsidian tags: alphanumeric, underscore, hyphen, forward slash (for nesting)
    # Here we keep it simple: replace non-alphanumeric (except chinese) with _
    
    # Simple strategy: replace spaces with _, strip explicit invalid chars
    clean = text.strip()
    clean = re.sub(r'[\s,]+', '_', clean)
    clean = re.sub(r'[#]', '', clean)
    return clean

def get_entities_by_file(conn) -> Dict[str, Set[str]]:
    query = """
    SELECT 
        n.file_path,
        e.label,
        e.text
    FROM entities e
    JOIN note_entities ne ON e.id = ne.entity_id
    JOIN notes n ON ne.note_id = n.id
    WHERE e.is_deleted = 0
    """
    cursor = conn.cursor()
    cursor.execute(query)
    
    result = {}
    for row in cursor.fetchall():
        path = row['file_path']
        label = row['label']
        text = row['text']
        
        tag = f"{label}_{sanitize_tag(text)}"
        
        if path not in result:
            result[path] = set()
        result[path].add(tag)
        
    return result

def update_files(file_tags_map: Dict[str, Set[str]]):
    count = 0
    skipped = 0
    updated = 0
    
    for file_path_str, tags_set in file_tags_map.items():
        path = Path(file_path_str)
        if not path.exists():
            print(f"File not found: {path}")
            continue
            
        try:
            post = frontmatter.load(path)
            
            # Check exclusion criteria
            note_type = post.get('type')
            if note_type in ['work-session', 'task-view']:
                print(f"Skipping {path.name} (type: {note_type})")
                skipped += 1
                continue
                
            # Update tags
            new_tags = sorted(list(tags_set))
            
            # Use 'tags' field. Some users use 'tag' (singular), we'll standardize on 'tags'
            # Remove 'tag' if present to avoid duplication/confusion?
            # User said: "previous tags delete, write new ones"
            
            if 'tag' in post.metadata:
                del post.metadata['tag']
                
            post.metadata['tags'] = new_tags
            
            # Write back
            with open(path, 'wb') as f:
                frontmatter.dump(post, f)
                
            updated += 1
            print(f"Updated {path.name}: {len(new_tags)} tags")
            
        except Exception as e:
            print(f"Error processing {path.name}: {e}")
            
    print(f"\nSummary: Updated {updated}, Skipped {skipped}")

def main():
    try:
        conn = connect_db()
        print("Connected to database.")
        
        file_tags = get_entities_by_file(conn)
        print(f"Found entities in {len(file_tags)} files.")
        
        update_files(file_tags)
        
    except Exception as e:
        print(f"Fatal error: {e}")
    finally:
        if 'conn' in locals():
            conn.close()

if __name__ == "__main__":
    main()
