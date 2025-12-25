# Maven Downloader - AI Coding Instructions

## Project Overview

Python CLI tool for bulk downloading Maven artifacts with multi-threading, mirror support, and dependency parsing. Uses BeautifulSoup for HTML scraping since Maven repos expose directory listings as HTML pages.

## Core Architecture

### Single-File Design
- All code in [main.py](main.py) - one `MavenDownloader` class + CLI entrypoint
- No modular split - keep related functionality together when editing

### Key Components
1. **Mirror System**: Dual mirror lists for browsing (`index_mirrors`) vs downloading (`download_mirrors`)
   - Browsing uses USTC mirror (HTTP directory listing friendly)
   - Downloading uses CN mirrors (Aliyun, Huawei, Tencent) for speed
   - Random mirror selection per request, immediate fallback to `base_url` on failure
   - See `_try_request_with_mirrors()` implementation pattern

2. **Path Safety**: All directory/file parsing uses security helpers
   - `_clean_href_dir()`, `_clean_href_file()` - validate single-segment paths only
   - `_is_safe_segment()` - blocks `..`, `/`, multi-level paths
   - Never use `os.path.join()` directly - use `_join_path()` wrapper

3. **State Management**:
   - `downloads/.mvn-downloader/downloaded.txt` - line-delimited completed files
   - `downloads/.mvn-downloader/pending.json` - JSON snapshot for Ctrl+C resume
   - Load state in `__init__`, save on download/interrupt

4. **Dependency Parsing**: Extracts groupIds from POM XML
   - Uses `xml.etree.ElementTree` after stripping namespaces with regex
   - Only extracts `<groupId>` tags, filters `${...}` placeholders
   - See `parse_pom_dependencies()` for the strip-then-parse pattern

## Development Workflows

### Running
```bash
# Use uv (not pip/python directly)
uv run main.py <groupId> [options]

# Common flags:
# -v             Verbose logging (mirror selection, sources)
# --dry-run      Tree view of download plan without downloading
# -e, --exclude  Filter subgroups (e.g., -e boot data web)
# -d, --depth    Dependency recursion limit (default 2)
```

### Testing Downloads
- Always test with `--dry-run` first to verify tree structure
- Use `-v` to debug mirror fallback behavior
- Test interruption with Ctrl+C, verify `pending.json` created

### Adding Features
- User-Agent header mimics Maven 3.9.6 to avoid blocks - preserve in `self.headers`
- Progress bars use `tqdm` - pass instance to `download_file()` for updates
- Thread-safe: use `self.lock` when modifying `self.downloaded_files` set

## Project Conventions

### Path Handling
```python
# CORRECT: Single-segment validation
dirname = self._clean_href_dir(href)  # Returns None if unsafe
if dirname:
    full_path = self._join_path(parent, dirname)

# WRONG: Direct concatenation or os.path.join
full_path = parent + '/' + href  # Vulnerable to traversal
```

### HTML Parsing Pattern
```python
soup = BeautifulSoup(response.text, 'html.parser')
for link in soup.find_all('a'):
    href = link.get('href')
    # Always validate before using:
    if href.endswith('/'):
        dirname = self._clean_href_dir(href)
    else:
        filename = self._clean_href_file(href)
```

### Version Detection Heuristic
- Directory is an **artifact** if it contains `maven-metadata*` files (non-.asc)
- Otherwise, it's an artifact if subdirs look like versions (contain digits via `_is_version_directory()`)
- See `_is_artifact_directory()` for this two-stage check

### Recursion Control
- Use `_processed_groups` set to prevent cycles
- Check `_current_depth >= max_depth` before recursing
- Pass depth state through `_current_depth` internal param

## Common Tasks

### Modifying Mirror Lists
Edit `__init__` defaults for `index_mirrors` or `download_mirrors`. Keep USTC for index (works with HTML), prioritize fast CN mirrors for downloads.

### Changing Download Behavior
File downloads happen in `download_file()` - modify for new file types, validation, or post-processing. POM parsing triggers in-thread, pushes groupIds to `self.new_dependencies` queue.

### Adjusting Exclusion Logic
`_should_exclude()` does substring matching on groupId parts. For more sophisticated patterns, modify the split-and-compare logic there.

## Error Handling Patterns
- Mirror failures are silent (logged with `_vlog` only) - don't print errors until source also fails
- BeautifulSoup exceptions in parsing methods return empty lists, not re-raised
- Signal handler (`_handle_interrupt`) saves state cleanly before exit

## Dependencies
- `uv` for env management (not pip)
- `requests` for HTTP with custom headers
- `beautifulsoup4` + `lxml` for HTML parsing
- `tqdm` for progress bars
- `pathlib` for all filesystem ops (not `os.path`)

## Anti-Patterns to Avoid
❌ Adding configuration files - keep CLI args in `argparse` setup
❌ Splitting into modules - maintain single-file simplicity
❌ Using `os.path.join` directly - breaks path security model
❌ Hardcoding mirrors - always allow empty list for source-only mode
❌ Blocking thread pool on I/O - use `stream=True` for large files
