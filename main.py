import os
import re
import argparse
import random
import requests
import json
import signal
import sys
from collections import defaultdict
from bs4 import BeautifulSoup
from urllib.parse import urljoin, unquote
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from queue import Queue
from threading import Lock
import xml.etree.ElementTree as ET
from tqdm import tqdm


class MavenDownloader:
    def __init__(self, base_url="https://repo1.maven.org/maven2/", output_dir="./downloads", max_workers=10, mirrors=None, verbose=False, exclude_patterns=None):
        self.base_url = base_url
        self.output_dir = Path(output_dir)
        self.max_workers = max_workers
        self.verbose = verbose
        self.downloaded_files = set()
        self.lock = Lock()
        self.download_queue = Queue()
        self.pending_files = []  # 待下载文件列表
        self.interrupted = False  # 中断标志
        self.new_dependencies = Queue()  # 新发现的依赖 groupId 队列
        self.exclude_patterns = exclude_patterns if exclude_patterns else []  # 排除模式列表
        
        # 状态文件路径
        self.state_dir = self.output_dir / ".mvn-downloader"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.downloaded_log = self.state_dir / "downloaded.txt"
        self.pending_log = self.state_dir / "pending.json"
        
        # 加载已下载文件记录
        self._load_downloaded_files()
        
        # 配置镜像源列表（优先使用镜像，失败时回退到源站）
        if mirrors is None:
            self.mirrors = [
                # 阿里云镜像（中国大陆速度快）
                "https://maven.aliyun.com/repository/public/",
                # 中科大镜像
                "https://maven.proxy.ustclug.org/maven2/",
                # 华为云镜像
                "https://repo.huaweicloud.com/repository/maven/",
                # 腾讯云镜像
                "https://mirrors.cloud.tencent.com/nexus/repository/maven-public/",
            ]
        else:
            self.mirrors = mirrors if isinstance(mirrors, list) else []
        
        # 模拟 Maven 客户端的 headers
        self.headers = {
            'User-Agent': 'Apache-Maven/3.9.6 (Java 17.0.9; Linux 5.15.0-91-generic)',
            'Accept': '*/*',
            'Accept-Encoding': 'gzip, deflate',
            'Accept-Language': 'en-US,en;q=0.9',
            'Cache-Control': 'no-cache',
            'Pragma': 'no-cache',
        }
    
    def _load_downloaded_files(self):
        """加载已下载文件记录"""
        if self.downloaded_log.exists():
            try:
                with open(self.downloaded_log, 'r', encoding='utf-8') as f:
                    for line in f:
                        file_path = line.strip()
                        if file_path:
                            self.downloaded_files.add(file_path)
                if self.downloaded_files:
                    print(f"✓ 加载已下载记录: {len(self.downloaded_files)} 个文件")
            except Exception as e:
                print(f"⚠ 加载下载记录失败: {e}")
    
    def _save_downloaded_file(self, file_path):
        """记录已下载的文件"""
        try:
            with open(self.downloaded_log, 'a', encoding='utf-8') as f:
                f.write(f"{file_path}\n")
        except Exception as e:
            print(f"⚠ 记录下载文件失败: {e}")
    
    def _save_pending_files(self):
        """保存待下载文件队列"""
        if self.pending_files:
            try:
                with open(self.pending_log, 'w', encoding='utf-8') as f:
                    json.dump({
                        'files': self.pending_files,
                        'total': len(self.pending_files)
                    }, f, indent=2)
                print(f"\n✓ 已保存待下载队列: {len(self.pending_files)} 个文件")
                print(f"  状态文件: {self.pending_log}")
            except Exception as e:
                print(f"\n⚠ 保存待下载队列失败: {e}")
    
    def _load_pending_files(self):
        """加载待下载文件队列"""
        if self.pending_log.exists():
            try:
                with open(self.pending_log, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.pending_files = data.get('files', [])
                if self.pending_files:
                    print(f"✓ 发现未完成的下载任务: {len(self.pending_files)} 个文件")
                    response = input("  是否继续上次的下载？(y/n): ").strip().lower()
                    if response == 'y':
                        return True
                    else:
                        self.pending_files = []
                        self.pending_log.unlink()
                        print("  已清除待下载队列")
            except Exception as e:
                print(f"⚠ 加载待下载队列失败: {e}")
        return False
    
    def _handle_interrupt(self, signum, frame):
        """处理中断信号"""
        print("\n\n⚠ 检测到中断信号 (Ctrl+C)...")
        self.interrupted = True
        self._save_pending_files()
        print("\n提示: 下次运行时可以继续未完成的下载")
        sys.exit(0)
        
    def try_request_with_mirrors(self, path, timeout=30, stream=False):
        """随机选择一个镜像源下载，失败时直接回退到源站
        
        Args:
            path: 相对路径（如 org/springframework/boot/）
            timeout: 超时时间
            stream: 是否使用流式下载
            
        Returns:
            (response, source_url) 或 (None, None)
        """
        # 随机选择一个镜像源
        if self.mirrors:
            mirror = random.choice(self.mirrors)
            url = urljoin(mirror, path)
            self._vlog(f"[mirror] {url}")
            try:
                response = requests.get(url, headers=self.headers, timeout=timeout, stream=stream)
                response.raise_for_status()
                return response, mirror
            except Exception as e:
                self._vlog(f"[mirror-fail] {url} -> {e}")
                # 镜像失败，不打印错误，直接尝试源站
                pass
        
        # 回退到源站
        url = urljoin(self.base_url, path)
        self._vlog(f"[origin] {url}")
        try:
            response = requests.get(url, headers=self.headers, timeout=timeout, stream=stream)
            response.raise_for_status()
            return response, self.base_url
        except Exception as e:
            print(f"下载失败（源站）: {path}, 错误: {e}")
            return None, None
    
    def group_id_to_path(self, group_id):
        """将 groupId 转换为路径，如 org.springframework.boot -> org/springframework/boot"""
        return group_id.replace('.', '/')
    
    def _should_exclude(self, group_id):
        """检查 groupId 是否应该被排除"""
        if not self.exclude_patterns:
            return False
        
        # 将 groupId 分割成部分，如 org.springframework.boot -> ['org', 'springframework', 'boot']
        parts = group_id.split('.')
        
        # 检查是否有任何部分匹配排除模式
        for pattern in self.exclude_patterns:
            # 支持完整匹配或部分匹配
            if pattern in parts or any(pattern in part for part in parts):
                return True
        
        return False
    
    def get_artifacts_list(self, group_path):
        """获取指定 group 路径下的所有 artifact"""
        """获取指定 group 路径下的所有 artifact，返回 (artifacts, subgroups)"""
        response, source = self.try_request_with_mirrors(group_path + '/')
        if response is None:
            return [], []
        
        try:
            soup = BeautifulSoup(response.text, 'html.parser')
            artifacts = []
            subgroups = []
            
            for link in soup.find_all('a'):
                href = link.get('href')
                if href and href.endswith('/') and href not in ['../', '../', '/']:
                    item_name = href.rstrip('/')
                    full_path = f"{group_path}/{item_name}"
                    
                    if self._is_artifact_directory(full_path):
                        artifacts.append(full_path)
                    else:
                        subgroups.append(full_path)
            
            return artifacts, subgroups
        except Exception as e:
            print(f"解析 artifact 列表失败: {e}")
            return [], []
    
    def _is_artifact_directory(self, path):
        """判断是否为 artifact 目录：优先依据 maven-metadata，其次看是否存在版本目录"""
        response, source = self.try_request_with_mirrors(path + '/')
        if response is None:
            return False
        
        try:
            soup = BeautifulSoup(response.text, 'html.parser')
            has_version_dir = False
            for link in soup.find_all('a'):
                href = link.get('href')
                if not href:
                    continue
                # 只要存在 maven-metadata*（排除签名文件），即可认定为 artifact 目录
                if href.startswith('maven-metadata') and not href.endswith('.asc'):
                    return True
                if href.endswith('/') and self._is_version_directory(href.rstrip('/')):
                    has_version_dir = True
            return has_version_dir
        except Exception:
            return False
    
    def _is_version_directory(self, dirname):
        """判断目录名是否像版本号"""
        return any(char.isdigit() for char in dirname)

    def _vlog(self, message):
        """verbose 日志输出"""
        if self.verbose:
            print(message)

    def _print_tree(self, group_id, artifacts, versions_map):
        """以树形结构打印待下载的文件计划"""
        print(f"\n└─ {group_id}")
        for artifact in sorted(artifacts):
            artifact_name = artifact.split('/')[-1]
            print(f"   ├─ {artifact_name}")
            versions = versions_map.get(artifact, [])
            for idx, (version_path, files) in enumerate(sorted(versions, key=lambda x: x[0])):
                connector = "└" if idx == len(versions) - 1 else "├"
                version_name = version_path.split('/')[-1]
                print(f"   │  {connector}─ {version_name}")
                file_connector_prefix = "   │     " if connector == "├" else "   │     "
                for f in sorted(files):
                    fname = f.split('/')[-1]
                    print(f"{file_connector_prefix}└─ {fname}")
    
    def get_versions_list(self, artifact_path):
        """获取指定 artifact 的所有版本"""
        response, source = self.try_request_with_mirrors(artifact_path + '/')
        if response is None:
            return []
        
        try:
            soup = BeautifulSoup(response.text, 'html.parser')
            versions = []
            
            for link in soup.find_all('a'):
                href = link.get('href')
                if href and href.endswith('/') and href not in ['../', '../', '/']:
                    version = href.rstrip('/')
                    # 跳过 maven-metadata 等特殊目录
                    if not version.startswith('maven-metadata'):
                        versions.append(f"{artifact_path}/{version}")
            
            return versions
        except Exception as e:
            print(f"解析版本列表失败: {e}")
            return []
    
    def get_files_in_version(self, version_path):
        """获取指定版本目录下的所有文件"""
        response, source = self.try_request_with_mirrors(version_path + '/')
        if response is None:
            return []
        
        try:
            soup = BeautifulSoup(response.text, 'html.parser')
            files = []
            
            for link in soup.find_all('a'):
                href = link.get('href')
                if href and not href.endswith('/') and href not in ['../', '../']:
                    # 跳过哈希文件，但保留 maven-metadata.xml
                    if not href.endswith(('.md5', '.sha1', '.sha256', '.sha512', '.asc')):
                        files.append(f"{version_path}/{href}")
            
            return files
        except Exception as e:
            print(f"解析文件列表失败: {e}")
            return []
    
    def get_artifact_metadata(self, artifact_path):
        """获取 artifact 级别的 maven-metadata.xml 文件"""
        response, source = self.try_request_with_mirrors(artifact_path + '/')
        if response is None:
            return []
        
        try:
            soup = BeautifulSoup(response.text, 'html.parser')
            metadata_files = []
            
            for link in soup.find_all('a'):
                href = link.get('href')
                if href and href.startswith('maven-metadata'):
                    # 收集 maven-metadata.xml 及其歾名文件，但不收集签名文件
                    if not href.endswith(('.asc',)):
                        metadata_files.append(f"{artifact_path}/{href}")
            
            return metadata_files
        except Exception as e:
            print(f"解析 artifact 元数据失败: {e}")
            return []
    
    def parse_pom_dependencies(self, pom_content):
        """解析 POM 文件中的依赖，只提取 groupId"""
        group_ids = set()
        
        try:
            # 移除命名空间以简化解析
            pom_content = re.sub(r'xmlns="[^"]+"', '', pom_content)
            root = ET.fromstring(pom_content)
            
            # 查找所有 dependency 标签，只提取 groupId
            for dependency in root.findall('.//dependency'):
                group_id = dependency.find('groupId')
                
                if group_id is not None and group_id.text:
                    # 过滤掉占位符变量（如 ${project.groupId}）
                    if not group_id.text.startswith('${'):
                        group_ids.add(group_id.text)
            
            return list(group_ids)
        except Exception as e:
            print(f"解析 POM 文件失败: {e}")
            return []
    
    def download_file(self, file_path, progress_bar=None):
        """下载单个文件并保存到本地"""
        with self.lock:
            if file_path in self.downloaded_files:
                if progress_bar:
                    progress_bar.update(1)
                return None
            self.downloaded_files.add(file_path)
        
        local_path = self.output_dir / file_path
        
        # 创建目录
        local_path.parent.mkdir(parents=True, exist_ok=True)
        
        # 如果文件已存在，跳过
        if local_path.exists():
            self._save_downloaded_file(file_path)
            if progress_bar:
                progress_bar.set_postfix_str(f"跳过: {file_path[-50:]}")
                progress_bar.update(1)
            # 如果是 POM 文件，也要解析依赖
            if file_path.endswith('.pom'):
                with open(local_path, 'r', encoding='utf-8') as f:
                    pom_content = f.read()
                group_ids = self.parse_pom_dependencies(pom_content)
                for group_id in group_ids:
                    self.new_dependencies.put(group_id)
            return None
        
        if progress_bar:
            progress_bar.set_postfix_str(f"下载: {file_path[-50:]}")
        
        response, source = self.try_request_with_mirrors(file_path, timeout=60, stream=True)
        self._vlog(f"[download] {file_path} from {source}")
        
        if response is None:
            if progress_bar:
                progress_bar.update(1)
            return None
        
        try:
            # 使用流式下载，分块写入（对于大文件更高效）
            with open(local_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
            
            # 记录已下载文件
            self._save_downloaded_file(file_path)
            
            if progress_bar:
                progress_bar.update(1)
            
            # 如果是 POM 文件，立即解析依赖并加入队列
            if file_path.endswith('.pom'):
                with open(local_path, 'r', encoding='utf-8') as f:
                    pom_content = f.read()
                group_ids = self.parse_pom_dependencies(pom_content)
                # 将新发现的依赖加入队列（在线程内部）
                for group_id in group_ids:
                    self.new_dependencies.put(group_id)
            
            return {'path': local_path, 'group_ids': []}
            
        except Exception as e:
            if progress_bar:
                progress_bar.write(f"✗ 保存失败: {file_path}, 错误: {e}")
                progress_bar.update(1)
            return None
    
    def download_group(self, group_id, include_dependencies=True, max_depth=2, dry_run=False, _current_depth=0, _processed_groups=None):
        """下载指定 groupId 的所有包
        
        Args:
            group_id: Maven groupId
            include_dependencies: 是否下载依赖
            max_depth: 最大递归深度，避免无限递归（默认2层）
            _current_depth: 当前递归深度（内部使用）
            _processed_groups: 已处理的 groupId 集合（内部使用）
        """
        # 设置信号处理器（仅在顶层）
        if _current_depth == 0:
            signal.signal(signal.SIGINT, self._handle_interrupt)
            
            # 尝试恢复上次未完成的下载
            if self._load_pending_files():
                return self._resume_download()
        
        # 初始化已处理集合
        if _processed_groups is None:
            _processed_groups = set()
        
        # 检查是否已处理过或超过最大深度
        if group_id in _processed_groups:
            return
        
        if _current_depth >= max_depth:
            return
        
        _processed_groups.add(group_id)
        group_path = self.group_id_to_path(group_id)
        
        indent = "  " * _current_depth
        print(f"\n{indent}{'=' * 60}")
        print(f"{indent}[深度 {_current_depth}] 📦 {group_id}")
        if _current_depth == 0:
            print(f"{indent}📁 输出: {self.output_dir}")
            print(f"{indent}🔧 线程: {self.max_workers}")
            print(f"{indent}🌐 镜像: {len(self.mirrors)} 个 + 源站")
            print(f"{indent}📊 已下载: {len(self.downloaded_files)} 个文件")
        print(f"{indent}{'=' * 60}")
        
        # 1. 获取所有 artifacts
        print(f"{indent}🔍 扫描 artifacts...")
        artifacts, subgroups = self.get_artifacts_list(group_path)
        
        # 过滤排除的 artifacts 和 subgroups
        excluded_artifacts = []
        excluded_subgroups = []
        
        if self.exclude_patterns:
            # 过滤 artifacts
            filtered_artifacts = []
            for artifact in artifacts:
                artifact_id = artifact.replace('/', '.')
                if self._should_exclude(artifact_id):
                    excluded_artifacts.append(artifact)
                else:
                    filtered_artifacts.append(artifact)
            artifacts = filtered_artifacts
            
            # 过滤 subgroups
            filtered_subgroups = []
            for subgroup in subgroups:
                subgroup_id = subgroup.replace('/', '.')
                if self._should_exclude(subgroup_id):
                    excluded_subgroups.append(subgroup)
                else:
                    filtered_subgroups.append(subgroup)
            subgroups = filtered_subgroups
            
            # 打印排除信息
            if excluded_artifacts or excluded_subgroups:
                total_excluded = len(excluded_artifacts) + len(excluded_subgroups)
                print(f"{indent}⊘ 排除 {total_excluded} 个项目 (artifacts: {len(excluded_artifacts)}, subgroups: {len(excluded_subgroups)})")
        
        if not artifacts and subgroups:
            print(f"{indent}📂 找到 {len(subgroups)} 个子group，继续探索...")
            for subgroup_path in subgroups:
                subgroup_id = subgroup_path.replace('/', '.')
                self.download_group(
                    group_id=subgroup_id,
                    include_dependencies=include_dependencies,
                    max_depth=max_depth,
                    dry_run=dry_run,
                    _current_depth=_current_depth,
                    _processed_groups=_processed_groups
                )
            return
        
        if not artifacts:
            print(f"{indent}⚠ 未找到任何 artifact 或子group")
            return
        print(f"{indent}✓ 找到 {len(artifacts)} 个 artifact")
        
        if subgroups:
            print(f"{indent}📂 同时找到 {len(subgroups)} 个子group")
            for subgroup_path in subgroups:
                subgroup_id = subgroup_path.replace('/', '.')
                self.download_group(
                    group_id=subgroup_id,
                    include_dependencies=include_dependencies,
                    max_depth=max_depth,
                    dry_run=dry_run,
                    _current_depth=_current_depth,
                    _processed_groups=_processed_groups
                )
        
        # 2. 获取所有版本
        print(f"{indent}🔍 扫描版本...", end='', flush=True)
        all_versions = []
        versions_map = defaultdict(list)  # artifact -> list[(version_path, files)]
        for artifact in artifacts:
            versions = self.get_versions_list(artifact)
            all_versions.extend(versions)
            for v in versions:
                versions_map[artifact].append((v, []))
        print(f"\r{indent}✓ 找到 {len(all_versions)} 个版本" + " " * 20)
        
        # 3. 获取所有文件
        print(f"{indent}🔍 扫描文件...", end='', flush=True)
        all_files = []
        for version in all_versions:
            files = self.get_files_in_version(version)
            all_files.extend(files)
            # 记录版本文件到 map
            for artifact in artifacts:
                if version.startswith(artifact + '/'):
                    versions_map[artifact] = [
                        (v_path, files if v_path == version else v_files)
                        for (v_path, v_files) in versions_map[artifact]
                    ]
                    break
        
        # 获取 artifact 级别的 maven-metadata.xml
        for artifact in artifacts:
            metadata_files = self.get_artifact_metadata(artifact)
            all_files.extend(metadata_files)
            if metadata_files:
                versions_map[artifact].append((f"{artifact}/maven-metadata", metadata_files))
        
        print(f"\r{indent}✓ 找到 {len(all_files)} 个文件" + " " * 20)
        
        if not all_files:
            print(f"{indent}⚠ 没有文件需要下载")
            return

        # dry-run 模式：仅打印计划，不下载
        if dry_run:
            print(f"{indent}📄 Dry-run 模式，仅展示待下载文件：")
            self._print_tree(group_id, artifacts, versions_map)
            return
        
        # 过滤已下载的文件
        files_to_download = [f for f in all_files if f not in self.downloaded_files]
        if not files_to_download:
            print(f"{indent}✓ 所有文件已下载")
        else:
            print(f"{indent}📥 需要下载 {len(files_to_download)} 个文件 (跳过 {len(all_files) - len(files_to_download)} 个已下载)")
        
        # 保存待下载列表（用于断点续传）
        self.pending_files = files_to_download
        
        # 4. 多线程下载（带进度条）
        downloaded_poms = []
        if files_to_download:
            print(f"{indent}⬇️  开始下载...")
            with tqdm(total=len(files_to_download), desc=f"{indent}下载进度", 
                     unit="文件", ncols=100, leave=True) as pbar:
                with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                    future_to_file = {executor.submit(self.download_file, file_path, pbar): file_path 
                                    for file_path in files_to_download}
                    
                    for future in as_completed(future_to_file):
                        if self.interrupted:
                            executor.shutdown(wait=False)
                            return
                        result = future.result()
                        if result and result.get('group_ids'):
                            downloaded_poms.append(result)
        
        # 清除待下载列表
        self.pending_files = []
        if self.pending_log.exists():
            self.pending_log.unlink()
        
        print(f"{indent}✓ 本次下载完成")
        
        # 5. 递归处理依赖（如果需要且未达到最大深度）
        if include_dependencies and _current_depth < max_depth - 1:
            print(f"\n{indent}🔗 处理依赖...")
            dependency_groups = set()
            
            # 收集所有新发现的依赖 groupId（从线程内部队列中）
            while not self.new_dependencies.empty():
                dep_group_id = self.new_dependencies.get()
                if dep_group_id not in _processed_groups:
                    dependency_groups.add(dep_group_id)
            
            if dependency_groups:
                print(f"{indent}✓ 发现 {len(dependency_groups)} 个依赖 group:")
                for dep_group in sorted(dependency_groups):
                    print(f"{indent}  • {dep_group}")
                
                # 递归下载每个依赖 group
                for dep_group_id in sorted(dependency_groups):
                    self.download_group(
                        group_id=dep_group_id,
                        include_dependencies=include_dependencies,
                        max_depth=max_depth,
                        dry_run=dry_run,
                        _current_depth=_current_depth + 1,
                        _processed_groups=_processed_groups
                    )
        
        # 只在顶层打印总结
        if _current_depth == 0:
            print("\n" + "=" * 60)
            print("✅ 全部下载完成！")
            print(f"  📊 处理了 {len(_processed_groups)} 个 group")
            print(f"  📥 共下载 {len(self.downloaded_files)} 个文件")
            print(f"  📁 保存位置: {self.output_dir.absolute()}")
            print("=" * 60)
    
    def _resume_download(self):
        """恢复中断的下载任务"""
        if not self.pending_files:
            return
        
        print(f"\n{'=' * 60}")
        print("🔄 恢复下载任务")
        print(f"  📥 待下载: {len(self.pending_files)} 个文件")
        print(f"{'=' * 60}\n")
        
        downloaded_poms = []
        with tqdm(total=len(self.pending_files), desc="恢复下载", 
                 unit="文件", ncols=100) as pbar:
            with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                future_to_file = {executor.submit(self.download_file, file_path, pbar): file_path 
                                for file_path in self.pending_files}
                
                for future in as_completed(future_to_file):
                    if self.interrupted:
                        executor.shutdown(wait=False)
                        return
                    result = future.result()
                    if result and result.get('group_ids'):
                        downloaded_poms.append(result)
        
        # 清除待下载列表
        self.pending_files = []
        if self.pending_log.exists():
            self.pending_log.unlink()
        
        print("\n" + "=" * 60)
        print("✅ 恢复下载完成！")
        print(f"  📥 共下载 {len(self.downloaded_files)} 个文件")
        print(f"  📁 保存位置: {self.output_dir.absolute()}")
        print("=" * 60)


def main():
    parser = argparse.ArgumentParser(description='从 Maven 仓库下载指定 groupId 的所有包')
    parser.add_argument('group_id', help='Maven groupId，如: org.springframework.boot')
    parser.add_argument('-o', '--output', default='./downloads', help='输出目录（默认: ./downloads）')
    parser.add_argument('-w', '--workers', type=int, default=10, help='线程数（默认: 10）')
    parser.add_argument('-d', '--depth', type=int, default=2, help='依赖递归深度（默认: 2）')
    parser.add_argument('--dry-run', action='store_true', help='仅打印待下载列表（tree 格式），不实际下载')
    parser.add_argument('-m', '--mirrors', nargs='*', help='自定义镜像源列表（多个URL用空格分隔）')
    parser.add_argument('--no-mirrors', action='store_true', help='不使用镜像源，直接从源站下载')
    parser.add_argument('--no-deps', action='store_true', help='不解析依赖')
    parser.add_argument('-e', '--exclude', nargs='*', help='排除的 subgroup 模式列表（如: boot data）')
    parser.add_argument('-v', '--verbose', action='store_true', help='输出详细日志（镜像选择、下载来源等）')
    
    args = parser.parse_args()
    
    # 处理镜像源配置
    mirrors = None
    if args.no_mirrors:
        mirrors = []  # 空列表表示不使用镜像
    elif args.mirrors:
        mirrors = args.mirrors
    # 否则使用默认镜像列表
    
    downloader = MavenDownloader(
        output_dir=args.output,
        max_workers=args.workers,
        mirrors=mirrors,
        verbose=args.verbose,
        exclude_patterns=args.exclude
    )
    
    downloader.download_group(
        group_id=args.group_id,
        include_dependencies=not args.no_deps,
        max_depth=args.depth,
        dry_run=args.dry_run
    )


if __name__ == "__main__":
    main()
