#!/usr/bin/env python3
"""
Molly Tagger Watcher Service

监控 Obsidian vault 目录，当有 .md 文件创建或修改时，
自动运行「LLM 提取 → tag 回写」流程。

由 Molly 框架拉起并注入环境变量。
"""

import os
import sys
import time
import signal
import logging
import sqlite3
import argparse
import queue
import threading
from pathlib import Path
from threading import Timer


# ---------------------------------------------------------------------------
# Parent-death detection: exit when Molly (parent) dies
# ---------------------------------------------------------------------------

def _watch_parent():
    """Background thread: exit when parent process dies (PPID becomes 1/launchd)."""
    parent_pid = os.getppid()
    while True:
        time.sleep(2)
        if os.getppid() != parent_pid:
            logging.getLogger('tagger-watcher').info(
                f"Parent process {parent_pid} died (ppid now {os.getppid()}), exiting."
            )
            os.kill(os.getpid(), signal.SIGTERM)
            break

threading.Thread(target=_watch_parent, daemon=True).start()

# --- 路径配置 ---
BASE_DIR = Path(__file__).parent.resolve()
os.chdir(BASE_DIR)          # config.yaml / entities.db 均以此目录为基准
sys.path.insert(0, str(BASE_DIR))

_vault = os.environ.get('MOLLY_VAULT_PATH')
if not _vault:
    sys.exit("Error: MOLLY_VAULT_PATH environment variable is not set.")
VAULT_PATH = Path(_vault)
DB_PATH    = BASE_DIR / 'entities.db'
LOG_PATH   = BASE_DIR / 'watcher.log'

DEBOUNCE_SECONDS = float(os.environ.get('MOLLY_DEBOUNCE_SEC', '3.0'))     # 等待最后一次变更后 N 秒再处理，防止编辑器批量写入


# --- 日志 ---
def setup_logging(verbose: bool = False):
    level = logging.DEBUG if verbose else logging.INFO
    fmt   = '%(asctime)s [%(levelname)s] %(message)s'
    handlers = [
        logging.FileHandler(LOG_PATH, encoding='utf-8'),
        logging.StreamHandler(sys.stdout),
    ]
    logging.basicConfig(level=level, format=fmt, handlers=handlers)

log = logging.getLogger('molly-tagger')


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

class TaggerPipeline:
    """封装单文件 LLM 标注 + tag 回写流程。

    并发控制策略（Last-Write-Wins）：
    - 使用单一 worker 线程，确保 SQLite 连接不跨线程
    - 每个文件维护一个版本号；每次有新变更时递增
    - worker 处理前检查版本，若已被更新版本取代则直接跳过
    - 防抖窗口（3s）处理编辑器批量写入；版本号处理处理期间到达的新变更
    """

    def __init__(self):
        self._queue: queue.Queue = queue.Queue()
        self._versions: dict[str, int] = {}          # path -> latest version
        self._ver_lock = threading.Lock()             # protect _versions
        self._worker = threading.Thread(target=self._worker_loop, name='tagger-worker', daemon=True)
        self._worker.start()

    def _worker_loop(self):
        """在 worker 线程中初始化 LLM tagger 和数据库，然后循环处理任务"""
        from llm_tag import LLMTagger

        self._tagger = LLMTagger(db_path=str(DB_PATH))
        self._tagger.connect()

        log.info(f"LLM tagger pipeline ready. Model: {self._tagger.model}")

        while True:
            item = self._queue.get()
            if item is None:   # 停止信号
                break
            file_path, version = item
            self._do_process(file_path, version)
            self._queue.task_done()

    def process_file(self, file_path: Path):
        """递增版本号并将任务加入队列（线程安全）"""
        key = str(file_path)
        with self._ver_lock:
            v = self._versions.get(key, 0) + 1
            self._versions[key] = v
        self._queue.put((file_path, v))

    def _file_hash(self, file_path: Path) -> str:
        import hashlib
        md5 = hashlib.md5()
        with open(file_path, 'rb') as f:
            md5.update(f.read())
        return md5.hexdigest()

    def _do_process(self, file_path: Path, version: int):
        """在 worker 线程中执行：LLM 提取实体 → 回写 tags"""
        # Last-Write-Wins：若已有更新版本在队列中，跳过本次
        key = str(file_path)
        with self._ver_lock:
            latest = self._versions.get(key, 0)
        if version < latest:
            log.debug(f"  - superseded (v{version} < v{latest}), skip: {file_path.name}")
            return

        try:
            # 文件已被删除（创建后立即删除等场景），跳过处理
            if not file_path.exists():
                log.debug(f"  - file gone, skip: {file_path.name}")
                return

            # 预检 hash：内容未变则跳过，避免 tag 回写触发二次处理
            current_hash = self._file_hash(file_path)
            cursor = self._tagger.conn.cursor()
            cursor.execute("SELECT file_hash FROM notes WHERE file_path = ?", (str(file_path),))
            row = cursor.fetchone()
            if row and row['file_hash'] == current_hash:
                log.debug(f"  - unchanged, skipped: {file_path.name}")
                return

            print(f"MOLLY_STATUS: processing {file_path.name}", flush=True)
            extracted, saved = self._tagger.tag_file(file_path)

            if extracted == 0 and saved == 0:
                # 内容过短或 API 失败：不更新 hash，下次修改文件时重试
                log.warning(f"  ✗ no entities extracted: {file_path.name}")
                print("MOLLY_STATUS: idle", flush=True)
                return

            # 实体入库成功 → 回写 frontmatter tags
            self._update_tags(file_path)

            # 写回后立刻更新 DB hash，防止 watchdog 检测到文件变更再次触发
            new_hash = self._file_hash(file_path)
            self._tagger.conn.execute(
                "UPDATE notes SET file_hash = ? WHERE file_path = ?",
                (new_hash, str(file_path))
            )
            self._tagger.conn.commit()

            log.info(f"  ✓ done: {file_path.name} (extracted={extracted}, saved={saved})")
            print("MOLLY_STATUS: idle", flush=True)

        except FileNotFoundError:
            log.debug(f"  - file gone during processing, skip: {file_path.name}")
        except Exception as e:
            log.error(f"  ✗ error [{file_path.name}]: {e}", exc_info=True)
            print(f"MOLLY_STATUS: error {e}", flush=True)

    def _update_tags(self, file_path: Path):
        """在 worker 线程中更新 tags，使用 worker 自己的 DB 连接"""
        from update_tags import get_entities_by_file, update_files

        conn = self._tagger.conn
        try:
            all_tags = get_entities_by_file(conn)
            tags_for_file = all_tags.get(str(file_path), set())
            if tags_for_file:
                update_files({str(file_path): tags_for_file})
            else:
                log.debug(f"  - no entities to tag: {file_path.name}")
        except Exception as e:
            log.error(f"  ✗ tag update error [{file_path.name}]: {e}", exc_info=True)

    def close(self):
        self._queue.put(None)     # 停止信号（worker 检查 item is None）
        self._worker.join(timeout=10)
        if hasattr(self, '_tagger'):
            self._tagger.close()


# ---------------------------------------------------------------------------
# File system event handler
# ---------------------------------------------------------------------------

class MarkdownHandler:
    """文件系统事件处理器，含防抖逻辑"""

    def __init__(self, pipeline: TaggerPipeline):
        self.pipeline = pipeline
        self._timers: dict[str, Timer] = {}

    # --- public ---

    def on_change(self, path: str):
        p = Path(path)
        if not self._should_handle(p):
            return
        log.debug(f"[change] {p.name}")
        self._debounce(path)

    # --- private ---

    def _should_handle(self, p: Path) -> bool:
        """只处理 vault 根目录下的 .md 文件（不含子目录）"""
        if p.suffix != '.md':
            return False
        # 文件必须直接位于 vault 根目录
        return p.parent == VAULT_PATH

    def _debounce(self, path: str):
        """取消旧定时器，重新计时"""
        if path in self._timers:
            self._timers[path].cancel()
        t = Timer(DEBOUNCE_SECONDS, self._run, args=[path])
        t.daemon = True
        t.start()
        self._timers[path] = t

    def _run(self, path: str):
        self._timers.pop(path, None)
        self.pipeline.process_file(Path(path))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Molly Tagger 文件监控服务")
    parser.add_argument('--verbose', '-v', action='store_true', help='显示 DEBUG 日志')
    args = parser.parse_args()

    setup_logging(args.verbose)

    try:
        from watchdog.observers import Observer
        from watchdog.events import FileSystemEventHandler, FileSystemEvent
    except ImportError:
        print("缺少依赖，请运行: uv add watchdog  (或 pip install watchdog)")
        sys.exit(1)

    log.info("=" * 60)
    log.info("[Molly Tagger] Watcher Service")
    log.info(f"  Vault    : {VAULT_PATH}")
    log.info(f"  DB       : {DB_PATH}")
    log.info(f"  Log      : {LOG_PATH}")
    log.info(f"  Debounce : {DEBOUNCE_SECONDS}s")
    log.info("=" * 60)

    pipeline       = TaggerPipeline()
    handler_logic  = MarkdownHandler(pipeline)

    class WatchdogBridge(FileSystemEventHandler):
        def on_created(self, event: FileSystemEvent):
            if not event.is_directory:
                handler_logic.on_change(event.src_path)

        def on_modified(self, event: FileSystemEvent):
            if not event.is_directory:
                handler_logic.on_change(event.src_path)

    observer = Observer()
    observer.schedule(WatchdogBridge(), str(VAULT_PATH), recursive=False)
    observer.start()

    log.info(f"Watching for changes... (Ctrl+C to stop)")
    print("MOLLY_READY", flush=True)

    try:
        while observer.is_alive():
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        log.info("Shutting down...")
        observer.stop()
        observer.join()
        pipeline.close()
        log.info("Watcher stopped.")


if __name__ == '__main__':
    main()
