# Comic Translation & Editing Tools

A modular pipeline for translating and inpainting comic pages using local LLM Vision and image generation backends (Stable Diffusion server or ComfyUI API).

## Installation

Ensure dependencies are installed:
```powershell
uv sync
```

## Quick Start: Interactive Menu Launcher

You can run the full pipeline interactively from anywhere using the PowerShell menu launcher with native Windows file/folder pickers:

```powershell
# Run from PowerShell (works from any working directory)
pwsh "path/to/img-tl/menu.ps1"
# or with Windows PowerShell 5.1:
powershell -ExecutionPolicy Bypass -File "path/to/img-tl/menu.ps1"
```
Or simply double-click **[`run_menu.bat`](run_menu.bat)** in Windows Explorer!

Features:
- **Native Windows GUI Pickers**: Pick comic folders, JSON translation files, and ComfyUI workflow JSONs via standard Windows dialogs.
- **Run From Anywhere**: Automatically resolves the package directory and uses `uv` or local virtual environment.
- **Workflow Memory**: Automatically remembers your last selected comic folder and ComfyUI workflow across sessions.
- **Full End-to-End Pipelines**: Chain Translation and ComfyUI / SD inpainting back-to-back.
- **Server Health Check**: Verify if llama-server, ComfyUI, and SD servers are online and responsive.

---

## Configuration & Server Defaults

All default server URLs, ports, delays, and model settings are centralized in **[`common.py`](common.py)**:
```python
DEFAULT_HOST = "http://localhost"
DEFAULT_LLAMA_PORT = 8080
DEFAULT_SD_PORT = 8080          # Set to e.g. 8081 if SD server runs on another port
DEFAULT_COMFY_PORT = 8188       # Default ComfyUI server port

DEFAULT_LLAMA_API_URL = f"{DEFAULT_HOST}:{DEFAULT_LLAMA_PORT}/v1/chat/completions"
DEFAULT_SD_API_URL = f"{DEFAULT_HOST}:{DEFAULT_SD_PORT}/v1/images/edits"
DEFAULT_COMFY_API_URL = f"{DEFAULT_HOST}:{DEFAULT_COMFY_PORT}"
DEFAULT_DELAY = 60.0            # Delay in seconds between API requests
DEFAULT_TIMEOUT = 600           # Timeout in seconds
DEFAULT_RETRIES = 2             # Retry attempts
```
You can customize them directly in `common.py` or override them per run via CLI flags (e.g. `--api-url`, `--comfy-url`, `--delay`).

---

## 1. Translate Comic Pages (`llama-translate.py`)

Extracts and translates comic text using the llama-server Vision API, generating a `translations.json` file inside the image folder.

```powershell
uv run python llama-translate.py "path/to/comic_folder" --lang chinese
```

### Common Options:
- `--lang`, `-l`: Source language: `chinese` (`zh`), `japanese` (`ja`), or `korean` (`ko`) (default: `chinese`).
- `--delay`: Delay in seconds between API calls (default: `60.0`).
- `--thinking-effort`: Reasoning effort: `none`, `low`, `medium`, or `high` (default: `low`).
- `--api-url`: Vision API endpoint (default: `http://localhost:8080/v1/chat/completions`).
- `--no-context`: Disable passing the previous page's translation for continuity.
- `--skip-errors`: Resume strictly after the last successful page instead of retrying earlier failures.

---

## 2. Inpaint Translated Text via SD Server (`edit_comics.py`)

Surgically replaces the original comic text with English translations using an SD server (sd.cpp / OpenAI-compatible `/v1/images/edits`).

```powershell
uv run python edit_comics.py "path/to/comic_folder/translations.json" --lang chinese
```
*(You can also pass the folder directly: `uv run python edit_comics.py "path/to/comic_folder" --lang chinese`)*

Edited images are saved to a subfolder named `edited/` inside the image directory. Pages without story dialogue are copied directly.

### Common Options:
- `--lang`, `-l`: Source language (default: generic `'original text'`).
- `--output-dir`: Subfolder name for output images (default: `edited`).
- `--max-side`: Target longest side in pixels (default: `1280`).
- `--steps`: Sampling steps (default: `4` for Turbo).
- `--cfg`: CFG scale (default: `1.0` for Turbo).
- `--delay`: Delay in seconds between edits (default: `60.0`).
- `--overwrite`: Overwrite already edited images in the output folder.
- `--export-prompts [FILE]`: Export image paths and prompt strings to a text file (default: `prompts.txt` in the comic folder).
- `--export-only`: Only export prompts to the text file without executing inpainting edits.

---

## 3. Inpaint Translated Text via ComfyUI (`edit_comics_comfy.py`)

Surgically replaces comic text with English translations using the ComfyUI API and an exported workflow JSON (`Save (API format)`). 

Only the input comic image and the generated translation prompt are injected by the script—all model loaders, samplers, steps, CFG, seed, and VAE settings remain 100% controlled by your exported workflow.

```powershell
uv run python edit_comics_comfy.py "path/to/comic_folder" -w "qwen21-gguf.json" --lang chinese
```

### Options:
- `translations_file` *(mandatory)*: Path to `translations.json` or comic directory.
- `workflow_file` / `--workflow`, `-w` *(mandatory)*: Path to exported ComfyUI workflow JSON (API format).
- `--lang`, `-l`: Source language (default: generic `'original text'`).
- `--comfy-url`: ComfyUI server base URL (default: `http://localhost:8188`).
- `--output-dir`: Subfolder name for output images (default: `edited`).
- `--delay`: Delay in seconds between edits (default: `120.0`).
- `--timeout`: Request timeout in seconds (default: `600`).
- `--retries`: Retry attempts on network/timeout errors (default: `2`).
- `--overwrite`: Overwrite already edited images in the output folder.
- `--export-prompts [FILE]`: Export image paths and prompt strings to a text file.
- `--export-only`: Only export prompts to the text file without executing inpainting.

---

## 4. Batch ComfyUI Generation (`comfy_batch.py`)

Standalone batch runner for generating images via ComfyUI API workflows:

### A. Batch Text-to-Image (T2I from Prompts JSON)
```powershell
uv run python comfy_batch.py --t2i --prompts "prompts.json" -w "t2i-workflow.json"
# With custom options:
uv run python comfy_batch.py --mode t2i --prompts "prompts.json" -w "t2i-workflow.json" --delay 5 --random-seed --output-dir "temp_output"
```
*Supports prompt lists formatted as either a plain string array `["prompt 1", "prompt 2"]` or an object `{"prompt": ["prompt 1", "prompt 2"]}`. Outputs are saved to an untracked `temp_output/` directory alongside a structured `manifest.json`.*

### B. Batch Image-to-Image (I2I with Constant Prompt)
```powershell
uv run python comfy_batch.py --i2i --images "path/to/images" --prompt "masterpiece, enhance details" -w "workflow.json"
# Or using a prompt file:
uv run python comfy_batch.py --mode i2i --images "path/to/images" --prompt-file "prompt.txt" -w "workflow.json"
```

### Options:
- `workflow_file` / `--workflow`, `-w` *(mandatory)*: Path to exported ComfyUI workflow JSON (API format).
- `--mode`, `-m`: Batch mode (`t2i` or `i2i`). Auto-detected from `--prompts` or `--images`/`--prompt`.
- `--prompts`: Path to prompts JSON file (for T2I).
- `--images`: Path to folder containing images to process (for I2I).
- `--prompt`, `-p`: Constant prompt string for all images (for I2I).
- `--prompt-file`: Path to text file containing constant prompt (for I2I).
- `--random-seed`: Randomize seeds across all sampler nodes for each generation.
- `--prompt-node`: Override prompt node ID in the workflow.
- `--image-node`: Override LoadImage node ID in the workflow (for I2I).
- `--output-dir`, `-o`: Subfolder or output path (default: `temp_output` for T2I, `edited` for I2I).
- `--delay`: Delay between generations in seconds (default: `5.0`s for T2I, `120.0`s for I2I).
- `--comfy-url`: ComfyUI server base URL (default: `http://localhost:8188`).
- `--timeout`: Request timeout in seconds (default: `600`).
- `--retries`: Retry attempts on network/timeout errors (default: `2`).
- `--overwrite`: Overwrite existing images in output folder.

