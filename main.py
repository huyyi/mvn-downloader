from queue import Queue
import random
import urllib
from bs4 import BeautifulSoup
import requests
import argparse
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from queue import Empty
import logging
import json

class Downloader:
    def __init__(self, start_url=None, output_dir="downloads"):
        self.logger = logging.getLogger("mvn-downloader")
        self.output_dir = Path(output_dir)
        self.source_site = "https://repo1.maven.org/maven2/"
        self.index_mirror = [
            "https://maven.proxy.ustclug.org/maven2/"
        ]
        self.download_mirror = [
            "https://maven.proxy.ustclug.org/maven2/",
            # aliyun
            "https://maven.aliyun.com/repository/public",
            # tencent
            "https://mirrors.cloud.tencent.com/nexus/repository/maven-public/",
            # huaweicloud
            "https://mirrors.huaweicloud.com/repository/maven/"
        ]
        # 下载队列线程安全
        self.download_queue = Queue()
        self.parse_queue = Queue()
        if start_url:
            self.parse_queue.put(start_url)
        self.logger.debug("Initialized Downloader | index_mirror=%d download_mirror=%d", len(self.index_mirror), len(self.download_mirror))

    def save_queues(self, file_path=None):
        """保存当前 parse_queue 与 download_queue 到 JSON 文件。"""
        if file_path is None:
            file_path = self.output_dir / ".mvn-downloader" / "pending.json"
        fp = Path(file_path)
        fp.parent.mkdir(parents=True, exist_ok=True)

        # 线程安全地快照队列内容
        with self.parse_queue.mutex:
            parse_list = list(self.parse_queue.queue)
        with self.download_queue.mutex:
            download_list = list(self.download_queue.queue)

        data = {
            "parse_queue": parse_list,
            "download_queue": download_list,
        }
        try:
            with fp.open("w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            self.logger.info(
                "Saved queues snapshot: %s (parse=%d, download=%d)",
                fp,
                len(parse_list),
                len(download_list),
            )
        except Exception as e:
            self.logger.error("Failed to save queues snapshot: %s", e)

    def load_queues(self, file_path=None):
        """从 JSON 文件加载到 parse_queue 与 download_queue。"""
        if file_path is None:
            file_path = self.output_dir / ".mvn-downloader" / "pending.json"
        fp = Path(file_path)
        if not fp.exists():
            self.logger.warning("Pending file not found: %s", fp)
            return 0, 0

        try:
            with fp.open("r", encoding="utf-8") as f:
                data = json.load(f)
            parse_list = data.get("parse_queue", [])
            download_list = data.get("download_queue", [])
            for it in parse_list:
                self.parse_queue.put(it)
            for it in download_list:
                self.download_queue.put(it)
            self.logger.info(
                "Loaded queues snapshot: %s (parse=%d, download=%d)",
                fp,
                len(parse_list),
                len(download_list),
            )
            return len(parse_list), len(download_list)
        except Exception as e:
            self.logger.error("Failed to load queues snapshot: %s", e)
            return 0, 0

    # 多线程处理parse队列
    def parse(self):
        def worker():
            while True:
                try:
                    item = self.parse_queue.get(timeout=1)
                except Empty:
                    break
                try:
                    # 解析目录，可能会继续向 parse_queue 与 download_queue 填充
                    self.logger.debug("Parse worker handling: %s", item)
                    self.parse_url(item)
                finally:
                    try:
                        self.parse_queue.task_done()
                    except Exception:
                        pass

        max_workers = 8
        self.logger.info("Starting parse with %d threads", max_workers)
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(worker) for _ in range(max_workers)]
            for f in futures:
                f.result()
        self.logger.info("Parse completed")


    def parse_url(self, url):
        # 该路径是一个目录
        if url.endswith("/"):
            base_url = random.choice(self.index_mirror) if self.index_mirror else self.source_site
            absolute_url = urllib.parse.urljoin(base_url, url)
            # 获取内容，若请求失败使用源站
            try:
                self.logger.debug("Index request: base=%s url=%s", base_url, absolute_url)
                response = requests.get(absolute_url, timeout=10)
                response.raise_for_status()
            except requests.RequestException:
                self.logger.warning("Index mirror failed, fallback to source: %s", url)
                absolute_url = urllib.parse.urljoin(self.source_site, url)
                response = requests.get(absolute_url, timeout=10)
            # bs4 解析 HTML
            soup = BeautifulSoup(response.text, "html.parser")
            for link in soup.find_all("a"):
                href = link.get("href")
                if href and href not in ("../", "./"):
                    if any(href.endswith(suffix) for suffix in (".asc", ".sha1", ".md5")):
                        self.logger.debug("Skip signature/checksum file: %s", urllib.parse.urljoin(url, href))
                        continue
                    elif href.endswith("/"):
                        self.logger.debug("Queue dir: %s", urllib.parse.urljoin(url, href))
                        self.parse_queue.put(urllib.parse.urljoin(url, href))
                    else:
                        self.logger.debug("Queue file: %s", urllib.parse.urljoin(url, href))
                        self.download_queue.put(urllib.parse.urljoin(url, href))


    def download(self):
        # 多线程下载文件：从下载队列取相对/绝对路径，拼接镜像并保存到本地对应位置
        def worker():
            while True:
                try:
                    item = self.download_queue.get(timeout=1)
                except Empty:
                    break
                try:
                    # 原始队列项可能是绝对 URL 或相对路径
                    # 目标保存路径：output_dir + 原始相对/绝对路径位置
                    target = self.output_dir / item
                    if target.exists():
                        self.logger.info("Skip exists: %s", target)
                        continue

                    # 构造下载链接：优先镜像，失败回源站
                    base = random.choice(self.download_mirror) if self.download_mirror else self.source_site
                    download_url = urllib.parse.urljoin(base, item)
                    try:
                        self.logger.info("Downloading: %s -> %s", item, download_url)
                        resp = requests.get(download_url, stream=True, timeout=20)
                        resp.raise_for_status()
                    except requests.RequestException:
                        self.logger.warning("Mirror failed, fallback to source: %s", item)
                        download_url = urllib.parse.urljoin(self.source_site, item)
                        resp = requests.get(download_url, stream=True, timeout=20)
                        resp.raise_for_status()

                    target.parent.mkdir(parents=True, exist_ok=True)
                    # 如果文件是pom文件，分析其依赖，将对应依赖加入分析队列
                    if item.endswith(".pom"):
                        pom_soup = BeautifulSoup(resp.text, "xml")
                        for dep in pom_soup.find_all("dependency"):
                            groupId = dep.find("groupId").text.replace('.', '/')
                            artifactId = dep.find("artifactId").text
                            version = dep.find("version").text
                            dep_path = f"{groupId}/{artifactId}/{version}/"
                            self.logger.debug("Queue dependency: %s", dep_path)
                            self.parse_url(dep_path)
                    else:
                        with target.open("wb") as f:
                            for chunk in resp.iter_content(chunk_size=64 * 1024):
                                if chunk:
                                    f.write(chunk)
                    self.logger.info("Saved: %s", target)
                finally:
                    try:
                        self.download_queue.task_done()
                    except Exception:
                        # 如果队列不支持 task_done（防御性处理）
                        pass

        # 默认并发线程数
        max_workers = 8
        self.logger.info("Starting download with %d threads", max_workers)
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(worker) for _ in range(max_workers)]
            for f in futures:
                # 等待线程完成
                f.result()
        self.logger.info("Download completed")


def main():
    argparser = argparse.ArgumentParser(description="Maven simple downloader.")
    argparser.add_argument("url", type=str, help="starting URL (directory) e.g. HTTPClient/")
    argparser.add_argument("--output", "-o", type=str, default="downloads", help="output directory (default: downloads)")
    argparser.add_argument("--threads", "-t", type=int, default=8, help="concurrent download threads")
    argparser.add_argument("--verbose", "-v", action="store_true", help="enable verbose logging")
    argparser.add_argument("--resume", action="store_true", default=True, help="resume from pending.json snapshot")
    args = argparser.parse_args()

    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=log_level, format="[%(levelname)s] %(message)s")

    dl = Downloader(output_dir=args.output)
    # 解析起始目录，填充下载队列
    start_url = args.url if args.url.endswith('/') else args.url + '/'
    try:
        if args.resume:
            loaded_parse, loaded_download = dl.load_queues()
            if loaded_parse == 0 and loaded_download == 0:
                dl.parse_url(start_url)
        else:
            dl.parse_url(start_url)
        # 多线程消费 parse_queue，递归解析子目录
        dl.parse()
        # 执行下载
        dl.download()
    except KeyboardInterrupt:
        logging.warning("Interrupted by user. Saving queues and exiting...")
        dl.save_queues()

if __name__ == "__main__":
    main()

