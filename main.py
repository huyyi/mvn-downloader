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
import threading
import time

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

        # 工作状态追踪
        self.active_parsers = 0
        self.active_downloaders = 0
        self.state_lock = threading.Lock()
        self.should_stop = threading.Event()
        self.parse_has_work = threading.Event()
        self.download_has_work = threading.Event()

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

    def _is_all_idle(self):
        """检查是否所有worker都空闲且队列都为空"""
        with self.state_lock:
            parse_empty = self.parse_queue.empty()
            download_empty = self.download_queue.empty()
            no_active = self.active_parsers == 0 and self.active_downloaders == 0
            return parse_empty and download_empty and no_active

    def parse_worker(self):
        """分析worker：持续从parse_queue获取URL进行分析"""
        while not self.should_stop.is_set():
            try:
                item = self.parse_queue.get(timeout=0.1)
                with self.state_lock:
                    self.active_parsers += 1

                try:
                    self.logger.debug("Parse worker handling: %s", item)
                    self.parse_url(item)
                finally:
                    with self.state_lock:
                        self.active_parsers -= 1
                    self.parse_queue.task_done()

            except Empty:
                # 队列空时检查是否应该结束
                if self._is_all_idle():
                    self.logger.debug("Parse worker detected all idle, stopping")
                    self.should_stop.set()
                    break
                # 否则短暂休眠等待新任务
                time.sleep(0.1)
            except Exception as e:
                self.logger.error("Parse worker error: %s", e, exc_info=True)
                with self.state_lock:
                    self.active_parsers -= 1


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


    def download_worker(self):
        """下载worker：持续从download_queue获取文件进行下载"""
        while not self.should_stop.is_set():
            try:
                item = self.download_queue.get(timeout=0.1)
                with self.state_lock:
                    self.active_downloaders += 1

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
                    target.parent.mkdir(parents=True, exist_ok=True)
                    # 如果文件是pom文件，分析其依赖，将对应依赖加入分析队列
                    if item.endswith(".pom"):
                        try:
                            self.logger.info("Downloading: %s -> %s", item, download_url)
                            resp = requests.get(download_url, stream=True, timeout=20)
                            resp.raise_for_status()
                        except requests.RequestException:
                            self.logger.warning("Download mirror %s failed, fallback to source: %s", base, item)
                            download_url = urllib.parse.urljoin(self.source_site, item)
                            resp = requests.get(download_url, stream=True, timeout=20)
                            resp.raise_for_status()
                        with target.open("w", encoding="utf-8") as f:
                            f.write(resp.text)
                        pom_soup = BeautifulSoup(resp.text, "xml")
                        for dep in pom_soup.find_all("dependency"):
                            groupId = dep.find("groupId")
                            if groupId:
                                groupId = groupId.text.replace('.', '/')
                            artifactId = dep.find("artifactId")
                            if artifactId:
                                artifactId = artifactId.text
                            version = dep.find("version")
                            if version:
                                version = version.text
                            if not (groupId and artifactId and version):
                                continue
                            if version.startswith("${") or version.startswith("<"):
                                # 跳过变量或复杂版本
                                continue
                            dep_path = f"{groupId}/{artifactId}/{version}/"
                            self.logger.debug("Queue dependency: %s", dep_path)
                            self.parse_queue.put(dep_path)
                    else:
                        self.logger.info("Downloading: %s -> %s", item, download_url)
                        # 流式下载文件
                        try:
                            resp = requests.get(download_url, stream=True, timeout=20)
                            resp.raise_for_status()
                        except requests.RequestException:
                            self.logger.warning("Download mirror %s failed, fallback to source: %s", base, item)
                            download_url = urllib.parse.urljoin(self.source_site, item)
                            resp = requests.get(download_url, stream=True, timeout=20)
                            resp.raise_for_status()
                        with target.open("wb") as f:
                            for chunk in resp.iter_content(chunk_size=8192):
                                if chunk:
                                    f.write(chunk)
                    self.logger.info("Saved: %s", target)

                finally:
                    with self.state_lock:
                        self.active_downloaders -= 1
                    self.download_queue.task_done()

            except Empty:
                # 队列空时检查是否应该结束
                if self._is_all_idle():
                    self.logger.debug("Download worker detected all idle, stopping")
                    self.should_stop.set()
                    break
                # 否则短暂休眠等待新任务
                time.sleep(0.1)
            except Exception as e:
                self.logger.error("Download worker error: %s", e, exc_info=True)
                with self.state_lock:
                    self.active_downloaders -= 1

    def run(self, parse_threads=4, download_threads=8):
        """启动双线程池并发运行分析和下载任务"""
        self.logger.info("Starting with %d parse threads and %d download threads", parse_threads, download_threads)

        with ThreadPoolExecutor(max_workers=parse_threads) as parse_executor, \
             ThreadPoolExecutor(max_workers=download_threads) as download_executor:

            # 启动所有worker
            parse_futures = [parse_executor.submit(self.parse_worker) for _ in range(parse_threads)]
            download_futures = [download_executor.submit(self.download_worker) for _ in range(download_threads)]

            # 等待所有worker完成
            for f in parse_futures + download_futures:
                try:
                    f.result()
                except Exception as e:
                    self.logger.error("Worker failed: %s", e, exc_info=True)

        self.logger.info("All workers completed. Parse queue: %d, Download queue: %d",
                        self.parse_queue.qsize(), self.download_queue.qsize())


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
                dl.parse_queue.put(start_url)
        else:
            dl.parse_queue.put(start_url)

        # 使用双线程池并发运行
        parse_threads = args.threads
        download_threads = args.threads
        dl.run(parse_threads=parse_threads, download_threads=download_threads)

    except KeyboardInterrupt:
        logging.warning("Interrupted by user. Saving queues and exiting...")
        dl.should_stop.set()
        time.sleep(0.5)  # 给worker一些时间完成当前任务
        dl.save_queues()

if __name__ == "__main__":
    main()

