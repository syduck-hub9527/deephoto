"""嵌入向量批量重建(embedding-bge-m3-migration §5.5):基于已存 searchable_text 重算向量。

切换嵌入模型后无需重新解析文档(D7):

    python -m deephoto.indexing.reembed [--dry-run] [--prune] [--sleep SECONDS] [--force]

- 版本守卫:只处理"没有当前版本向量"的索引项,中断后重跑自然续跑(§5.5.5);
- 每批一次提交;批次失败记录 id、跳过、继续,最后汇总(§5.5.4);
- 有任何批次失败时不得 --prune(§5.5.6);探测失败或有入库任务进行中拒绝运行(§5.5.1/2)。
"""

from __future__ import annotations

import argparse
import logging
import sys
import time

import numpy as np

from .. import repo
from ..config import load_settings
from ..db import connect, init_db
from ..llm import build_embeddings
from .probe import probe_embeddings

logger = logging.getLogger("deephoto.reembed")

# 正在进行的入库任务(以 ingest 状态机为准):重建与其并发可能互相覆盖索引项
IN_FLIGHT_STATUSES = ("queued", "parsing", "describing", "indexing")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m deephoto.indexing.reembed",
        description="基于已存文本批量重建嵌入向量(版本守卫,可续跑)")
    parser.add_argument("--dry-run", action="store_true", help="只打印待重建数量,不写库")
    parser.add_argument("--prune", action="store_true",
                        help="全部成功后删除旧版本向量(有任何失败时拒绝执行)")
    parser.add_argument("--sleep", type=float, default=0.0, metavar="SECONDS",
                        help="批次之间等待秒数(规避 TPM 429,默认 0)")
    parser.add_argument("--force", action="store_true", help="有正在进行的入库任务时也强制执行")
    parser.add_argument("--batch", type=int, default=0, metavar="N",
                        help="每批条数(默认取 DEEPHOTO_EMBEDDING_BATCH_SIZE)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    settings = load_settings()
    embeddings = build_embeddings(settings)
    if embeddings is None:
        logger.error("未配置 DEEPHOTO_EMBEDDING_BASE_URL/EMBEDDING_MODEL,无法重建")
        return 2
    probe = probe_embeddings(embeddings, settings)
    if not probe.ok:
        logger.error("嵌入探测失败:%s", probe.error)          # §5.5.1:探测失败退出非 0
        return 2
    version = probe.version
    batch_size = args.batch or settings.embedding_batch_size

    init_db(settings.db_path)
    # connect() 返回 thread-local 缓存连接,生命周期由 db 模块管理;这里不能 close
    conn = connect(settings.db_path)
    inflight = repo.count_documents_by_status(conn, IN_FLIGHT_STATUSES)
    if inflight and not args.force:
        logger.error("有 %d 份文档正在入库(%s);为避免互相覆盖,拒绝重建(--force 可覆盖)",
                     inflight, "/".join(IN_FLIGHT_STATUSES))
        return 2

    total = repo.count_items_missing_vector(conn, version)
    if args.dry_run:
        print(f"待重建 {total} 项(目标版本 {version})")
        return 0
    logger.info("开始重建:共 %d 项,版本 %s,批大小 %d", total, version, batch_size)
    started = time.monotonic()
    done = failed = 0
    after = ""
    while True:
        batch = repo.items_missing_vector(conn, version, batch_size, after)
        if not batch:
            break
        after = batch[-1]["id"]
        try:
            vectors = embeddings.embed_documents([item["searchable_text"] for item in batch])
        except Exception as exc:
            # 批次失败:记录该批 id、跳过、继续;失败项仍是 missing,重跑自然再处理(§5.5.4/5)
            failed += len(batch)
            logger.warning("批次失败(%d 项),已跳过:ids=%s,原因=%s",
                           len(batch), [item["id"] for item in batch], exc)
            continue
        with conn:                                            # 每批一次提交(§5.5.4)
            for item, vector in zip(batch, vectors):
                repo.put_embedding(conn, item["id"],
                                   np.asarray(vector, dtype=np.float32).tobytes())
                repo.set_item_version(conn, item["id"], version, "ready")
        done += len(batch)
        logger.info("进度 %d/%d(失败 %d)", done + failed, total, failed)
        if args.sleep > 0:
            time.sleep(args.sleep)                            # §5.5.7:批次间节流规避 429

    elapsed = time.monotonic() - started
    logger.info("重建结束:成功 %d,失败 %d,耗时 %.1fs", done, failed, elapsed)
    if failed:
        logger.error("有 %d 项失败;修复原因后重跑本命令即可续跑,未执行任何清理", failed)
        return 1
    if args.prune:
        removed = repo.prune_old_vectors(conn, version)       # §5.5.6:仅全部成功才清理
        conn.commit()
        logger.info("已清理旧版本向量 %d 条", removed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
