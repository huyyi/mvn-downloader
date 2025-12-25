# Maven Repository HTTP Crawler

一个用于批量下载Maven仓库文件的Python工具，支持多线程、断点续传和智能过滤。

## 功能特点

- 🚀 **多线程下载** - 使用线程池并发下载，提高效率
- 📦 **智能过滤** - 只下载 maven-metadata.xml、.pom 和 .jar 文件
- 💾 **断点续传** - 支持Ctrl+C中断后恢复下载
- 🔒 **路径安全** - 防止路径遍历攻击
- 📊 **进度显示** - 实时显示下载进度

## 安装

使用 `uv` 管理依赖（推荐）：

```bash
# 克隆仓库
git clone <repository-url>
cd mvn-downloader

# uv会自动安装依赖
```

依赖包：
- requests - HTTP请求
- beautifulsoup4 - HTML解析
- lxml - 快速XML/HTML处理
- tqdm - 进度条显示

## 使用方法

### 基本用法

```bash
# 下载指定路径下的所有Maven文件
uv run main.py https://repo1.maven.org/maven2/org/springframework/amqp

# 指定输出目录
uv run main.py https://repo1.maven.org/maven2/commons-io/commons-io -o output

# 设置下载线程数
uv run main.py <url> -w 5

# 启用详细日志
uv run main.py <url> -v
```

### URL格式

工具会自动识别Maven仓库的基础URL和artifact路径：

```bash
# 完整URL
uv run main.py https://repo1.maven.org/maven2/org/springframework/amqp

# 也可以只提供artifact路径（对于常见仓库）
uv run main.py org/springframework/amqp
```

### 断点续传

下载过程中按 `Ctrl+C` 可以安全中断，程序会保存当前状态到 `downloads/.crawler/` 目录：

- `downloaded.txt` - 已下载的文件列表
- `pending.json` - 待下载的URL列表

再次运行相同命令即可继续下载。

## 工作原理

1. **目录遍历** - 使用BFS（广度优先搜索）遍历Maven仓库的目录结构
2. **Artifact识别** - 检测包含 `maven-metadata.xml` 的目录作为artifact目录
3. **文件过滤** - 只下载以下类型的文件：
   - `maven-metadata.xml` - Maven元数据
   - `*.pom` - 项目对象模型文件
   - `*.jar` - Java归档文件
4. **并发下载** - 使用线程池并行下载多个文件

## 命令行参数

```
usage: main.py [-h] [-o OUTPUT] [-w WORKERS] [-v] url

positional arguments:
  url                   Maven仓库的基础URL或完整路径

optional arguments:
  -h, --help            显示帮助信息
  -o OUTPUT, --output OUTPUT
                        输出目录（默认：downloads）
  -w WORKERS, --workers WORKERS
                        下载线程数（默认：10）
  -v, --verbose         启用详细日志
```

## 示例

### 下载Spring AMQP

```bash
uv run main.py https://repo1.maven.org/maven2/org/springframework/amqp -w 5
```

### 下载Commons IO（带详细日志）

```bash
uv run main.py https://repo1.maven.org/maven2/commons-io/commons-io -v
```

### 自定义输出目录

```bash
uv run main.py <url> -o /path/to/output
```

## 安全特性

- **路径验证** - 所有文件名和目录名都经过安全检查，防止 `..` 和 `/` 等路径遍历攻击
- **单段路径** - 只接受单一路径段，不允许多级路径
- **URL清理** - 自动清理和验证URL，防止恶意输入

## 许可证

MIT License

## 贡献

欢迎提交 Issue 和 Pull Request！
