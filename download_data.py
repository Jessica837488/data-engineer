# -*- coding: utf-8 -*-
"""
数据集下载脚本（模块 0：环境准备）

职责：
1. 通过 kagglehub 下载两份公开数据集（匿名下载，无需 Kaggle API Key）：
   - Online Retail II（UCI）：mashlyn/online-retail-ii-uci
   - Brazilian E-Commerce（Olist）：olistbr/brazilian-ecommerce
2. 将 CSV 统一整理到仓库根目录的 data/ 下（Olist 的 9 张表放入 data/olist/）。
3. 幂等：若目标文件已存在且非空，跳过下载（支持手动下载 fallback）。

手动下载 fallback（网络受限时）：
  1) 从 Kaggle 页面下载数据集 zip 并解压；
  2) 将 Online Retail II 的 CSV 放到 data/ 下（任意文件名，脚本会自动识别重命名）；
  3) 将 Olist 的所有 CSV 放到 data/olist/ 下。
脚本每次运行都会先检测本地文件，存在则跳过对应下载步骤。
"""

import logging
import shutil
import sys
from pathlib import Path

# 踩坑记录：Windows 默认控制台编码为 GBK，输出 "✓" 等字符会抛 UnicodeEncodeError，
# 这里统一将 stdout 重配置为 UTF-8（Python 3.7+），保证跨平台日志正常
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

try:
    import kagglehub
except ImportError:
    print("缺少依赖 kagglehub，请先执行: pip install -r requirements.txt")
    sys.exit(1)

# 日志配置：同时输出到控制台和 data/download.log
LOG_PATH = Path(__file__).resolve().parent / "data" / "download.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
    ],
)
logger = logging.getLogger("download_data")

# 仓库根目录与数据目录
ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
OLIST_DIR = DATA_DIR / "olist"

# Kaggle 数据集标识
ONLINE_RETAIL_HANDLE = "mashlyn/online-retail-ii-uci"
OLIST_HANDLE = "olistbr/brazilian-ecommerce"

# 目标文件名（README 要求统一小写 ii）
ONLINE_RETAIL_TARGET = DATA_DIR / "online_retail_ii.csv"


def ensure_dirs() -> None:
    """创建数据目录结构（data/.gitkeep 已在仓库中，目录不存在则补建）。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    OLIST_DIR.mkdir(parents=True, exist_ok=True)


def download_online_retail() -> None:
    """下载 Online Retail II 并统一重命名为 data/online_retail_ii.csv。"""
    if ONLINE_RETAIL_TARGET.exists() and ONLINE_RETAIL_TARGET.stat().st_size > 0:
        logger.info("[1/2] online_retail_ii.csv 已存在，跳过下载")
        return

    logger.info("[1/2] 开始下载 Online Retail II（首次约 300MB，请耐心等待）...")
    try:
        # kagglehub 对公开数据集支持匿名下载，返回缓存目录路径
        cache_dir = Path(kagglehub.dataset_download(ONLINE_RETAIL_HANDLE))
    except Exception as exc:  # noqa: BLE001 网络/认证异常统一捕获，保证模块可独立失败
        logger.error("Online Retail II 下载失败: %s", exc)
        logger.error(
            "请手动下载: https://www.kaggle.com/datasets/mashlyn/online-retail-ii-uci ，"
            "解压后将 CSV 放入 data/ 目录（任意文件名均可，重跑本脚本会自动识别）"
        )
        raise

    # 缓存目录中查找唯一的主 CSV（数据集内可能嵌套子目录）
    csv_files = list(cache_dir.rglob("*.csv"))
    if not csv_files:
        logger.error("下载缓存中未找到 CSV 文件: %s", cache_dir)
        raise FileNotFoundError(f"no csv under {cache_dir}")

    # 取最大的 CSV 作为主数据文件（避免误选附带的小文件）
    source = max(csv_files, key=lambda p: p.stat().st_size)
    shutil.copy2(source, ONLINE_RETAIL_TARGET)
    logger.info(
        "[1/2] 完成: %s -> %s（%.1f MB）",
        source.name, ONLINE_RETAIL_TARGET.name,
        ONLINE_RETAIL_TARGET.stat().st_size / 1024 / 1024,
    )


def download_olist() -> None:
    """下载 Olist 数据集，将全部 CSV 复制到 data/olist/ 下。"""
    # 已有全部 9 张核心表则跳过
    existing = sorted(p.name for p in OLIST_DIR.glob("*.csv"))
    if len(existing) >= 9:
        logger.info("[2/2] olist/ 目录已有 %d 个 CSV，跳过下载", len(existing))
        return

    logger.info("[2/2] 开始下载 Brazilian E-Commerce by Olist（首次约 200MB）...")
    try:
        cache_dir = Path(kagglehub.dataset_download(OLIST_HANDLE))
    except Exception as exc:  # noqa: BLE001
        logger.error("Olist 下载失败: %s", exc)
        logger.error(
            "请手动下载: https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce ，"
            "解压后将所有 olist_*_dataset.csv 放入 data/olist/ 目录"
        )
        raise

    copied = 0
    for csv_file in cache_dir.rglob("*.csv"):
        # olist 原始压缩包里可能混有营销/地理等附加表，全部保留以备后用
        dest = OLIST_DIR / csv_file.name
        if not dest.exists() or dest.stat().st_size == 0:
            shutil.copy2(csv_file, dest)
            copied += 1
    logger.info("[2/2] 完成: 新复制 %d 个 CSV 到 data/olist/", copied)


def main() -> int:
    """入口：依次下载两份数据集，任一失败即以非零码退出（但不会阻塞本地已有的另一份）。"""
    ensure_dirs()
    rc = 0
    for step in (download_online_retail, download_olist):
        try:
            step()
        except Exception as exc:  # noqa: BLE001
            logger.error("步骤 %s 失败: %s", step.__name__, exc)
            rc = 1
    if rc == 0:
        logger.info("全部数据集就绪 ✓")
    return rc


if __name__ == "__main__":
    sys.exit(main())
