# shrink-epub

shrink-epub is a command-line tool that reduces EPUB file sizes by concurrently
converting embedded images to WebP and updating all internal references.

## Prerequisites

* **Python:** 3.14+ (older versions may work but are untested)
* **[uv](https://github.com/astral-sh/uv)**

## Setup

```bash
git clone https://github.com/daoofcoding/shrink-epub
cd shrink-epub
uv sync

```

## Usage

### Basic

```bash
uv run main.py -i src/manuscript.epub -o dist/

```

### Advanced

```bash
uv run main.py \
    --input assets/raw_epubs \
    --output assets/optimized_epubs \
    --quality 85 \
    --level 5 \
    --max-size 1500 \
    --verbose

```

## Options

| Short | Long | Description | Default |
| --- | --- | --- | --- |
| `-i` | `--input` | Source EPUB file or directory. | **Required** |
| `-o` | `--output` | Output directory for processed files. | **Required** |
| `-q` | `--quality` | WebP compression quality (0-100). | `80` |
| `-l` | `--level` | WebP compression level (0-6). | `6` |
| `-m` | `--max-size` | WebP max width/height in pixels. | `1200` |
| `-s` | `--silent` | Suppress output except for errors. | `False` |
| `-V` | `--verbose` | Show debug logs. | `False` |
| `-v` | `--version` | Show program's version number and exit. | - |
| `-h` | `--help` | Show the help message and exit. | - |
