# mvn-downloader

Maven 仓库多线程下载工具，支持自动解析 POM 依赖。

## 功能特性

- ✅ 多线程并发下载
- ✅ 从 repo1.maven.org 下载 Maven 包
- ✅ **多镜像源支持**（阿里云、华为云、腾讯云等）
- ✅ **智能切换**：镜像失效时自动回退到源站
- ✅ **实时进度条**：显示下载进度和当前文件
- ✅ **断点续传**：支持 Ctrl+C 中断后恢复下载
- ✅ **状态记录**：已下载文件持久化存储
- ✅ 保留原始文件路径结构
- ✅ 自动解析 POM 文件依赖
- ✅ 支持按 groupId 批量下载
- ✅ 避免重复下载
- ✅ 模拟 Maven 客户端避免被拦截

## 安装

使用 uv 安装依赖：

```bash
uv sync
```

## 使用方法

### 基本用法

下载指定 groupId 的所有包：

```bash
uv run main.py org.springframework.boot
```

### 高级选项

```bash
# 指定输出目录
uv run main.py org.springframework.boot -o ./my-downloads

# 设置线程数（默认 10）
uv run main.py org.springframework.boot -w 20

# 设置依赖递归深度（默认 2）
uv run main.py org.springframework.boot -d 3

# 自定义镜像源
uv run main.py org.springframework.boot -m https://maven.aliyun.com/repository/public/

# 使用多个自定义镜像源
uv run main.py org.springframework.boot -m \
  https://maven.aliyun.com/repository/public/ \
  https://repo.huaweicloud.com/repository/maven/

# 不使用镜像，直接从源站下载
uv run main.py org.springframework.boot --no-mirrors

# 不解析依赖
uv run main.py org.springframework.boot --no-deps

# 排除特定的 subgroup（支持部分匹配）
uv run main.py org.springframework --exclude boot autoconfigure

# 排除多个 subgroup（只下载 org.springframework.kafka 和 org.springframework.integration）
uv run main.py org.springframework -e boot data web security cloud
```

### 完整参数说明

```bash
uv run main.py [-h] [-o OUTPUT] [-w WORKERS] [-d DEPTH] [-m [MIRRORS ...]] 
               [--no-mirrors] [--no-deps] [-e [EXCLUDE ...]] [-v] group_id

参数:
  group_id              Maven groupId，如: org.springframework.boot
  -o, --output          输出目录（默认: ./downloads）
  -w, --workers         线程数（默认: 10）
  -d, --depth           依赖递归深度（默认: 2，防止无限递归）
  -m, --mirrors         自定义镜像源列表（多个URL用空格分隔）
  --no-mirrors          不使用镜像源，直接从源站下载
  --no-deps             不解析依赖
  -e, --exclude         排除的 subgroup 模式列表（如: boot data）
  -v, --verbose         输出详细日志（镜像选择、下载来源等）
  -h, --help            显示帮助信息
```

## 镜像源配置

### 默认镜像源

程序默认使用以下镜像源（按优先级排序）：

1. **阿里云镜像**：`https://maven.aliyun.com/repository/public/`
2. **华为云镜像**：`https://repo.huaweicloud.com/repository/maven/`
3. **腾讯云镜像**：`https://mirrors.cloud.tencent.com/nexus/repository/maven-public/`
4. **Maven 中央仓库**：`https://repo1.maven.org/maven2/`（兜底）

### 镜像源工作原理

- **随机选择**：每次请求随机选择一个镜像源，分散负载
- **快速回退**：如果镜像失效，直接回退到 Maven 中央仓库（不轮询其他镜像）
- **高效可靠**：最多只尝试 2 次（1 个镜像 + 源站）
- 使用 Maven 客户端 User-Agent，避免被某些镜像站拦截

## 使用示例

### 下载 Spring Boot 包

```bash
uv run main.py org.springframework.boot
```

会下载 `org/springframework/boot/` 下的所有 artifact 和版本。

### 断点续传

如果下载过程中按 `Ctrl+C` 中断：

```bash
# 中断后会自动保存进度
^C
⚠ 检测到中断信号 (Ctrl+C)...
✓ 已保存待下载队列: 123 个文件
  状态文件: ./downloads/.mvn-downloader/pending.json

# 下次运行时会提示恢复
uv run main.py org.springframework.boot
✓ 发现未完成的下载任务: 123 个文件
  是否继续上次的下载？(y/n): y
🔄 恢复下载任务
```

### 排除特定的 subgroup

```bash
# 只下载 org.springframework 但排除 boot 相关的包
uv run main.py org.springframework --exclude boot

# 排除多个 subgroup
uv run main.py org.springframework -e boot data web security

# 示例：只下载 org.springframework.kafka，排除其他所有 spring 子项目
uv run main.py org.springframework -e boot data web security cloud integration batch amqp
```

### 下载其他包

```bash
# 下载 Apache Commons
uv run main.py org.apache.commons

# 下载 Google Guava
uv run main.py com.google.guava
```

## 工作原理

1. **扫描 Artifacts**: 根据 groupId 扫描 Maven 仓库获取所有 artifact
2. **获取版本**: 遍历每个 artifact 获取所有版本
3. **列出文件**: 获取每个版本目录下的所有文件（jar, pom, sources 等）
4. **状态检查**: 自动跳过已下载的文件（通过 `.mvn-downloader/downloaded.txt` 记录）
5. **多线程下载**: 使用线程池并发下载所有文件，实时显示进度条
6. **解析依赖**: 自动解析 POM 文件中的 dependencies，提取所有依赖的 **groupId**
7. **递归下载**: 对每个依赖的 groupId，递归下载该 group 下的所有包（可配置深度）
8. **保留路径**: 下载的文件保持 Maven 仓库的原始目录结构
9. **去重处理**: 自动跟踪已下载的 group，避免重复下载
10. **断点续传**: Ctrl+C 中断时保存待下载队列，下次启动可恢复

## 目录结构

下载的文件会保持原始的 Maven 仓库结构：

```
downloads/
├── .mvn-downloader/          # 状态目录
│   ├── downloaded.txt        # 已下载文件记录
│   └── pending.json          # 待下载队列（中断时生成）
└── org/
    └── springframework/
        └── boot/
            ├── spring-boot/
            │   ├── 2.7.0/
            │   │   ├── spring-boot-2.7.0.jar
            │   │   ├── spring-boot-2.7.0.pom
            │   │   └── spring-boot-2.7.0-sources.jar
            │   └── 3.0.0/
            │       └── ...
            └── spring-boot-starter/
                └── ...
```

## 注意事项

- 某些大型 groupId 可能包含大量文件，下载时间较长
- 建议根据网络情况调整线程数
- **依赖解析**: 只提取 POM 中的 groupId，然后下载该 group 下的所有包
- **递归控制**: 使用 `-d` 参数控制依赖递归深度（默认2层），避免下载整个 Maven 中央仓库
- **镜像源**: 默认使用国内镜像源加速下载，失败时自动切换
- **示例**: 下载 Spring Boot 时，会自动识别并下载其依赖的 group（如 `org.springframework`, `com.fasterxml.jackson` 等）

## 常见镜像源

如果需要自定义镜像源，以下是一些可用的 Maven 镜像：

- **阿里云**：`https://maven.aliyun.com/repository/public/`
- **华为云**：`https://repo.huaweicloud.com/repository/maven/`
- **腾讯云**：`https://mirrors.cloud.tencent.com/nexus/repository/maven-public/`
- **网易云**：`https://mirrors.163.com/maven/repository/maven-public/`
- **清华大学**：`https://mirrors.tuna.tsinghua.edu.cn/maven/`

## 开发

项目使用 uv 进行依赖管理：

```bash
# 添加新依赖
uv add <package-name>

# 更新依赖
uv sync

# 运行开发版本
uv run main.py
```