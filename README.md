# Comic Translation & Editing Tools

A modular pipeline for translating and inpainting comic pages using local LLM Vision and image generation backends (Stable Diffusion server or ComfyUI API).

## Installation

Ensure dependencies are installed:
```powershell
uv sync
```

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
uv run python edit_comics_comfy.py "path/to/comic_folder/translations.json" "path/to/workflow.json" --lang chinese
```
*(You can also pass the workflow via `-w`: `uv run python edit_comics_comfy.py "path/to/comic_folder" -w "path/to/workflow.json" --lang chinese`)*

### Common Options:
- `translations_file` *(mandatory)*: Path to `translations.json` or comic directory.
- `workflow_file` / `--workflow`, `-w` *(mandatory)*: Path to exported ComfyUI workflow JSON (API format).
- `--lang`, `-l`: Source language (default: generic `'original text'`).
- `--comfy-url`: ComfyUI server base URL (default: `http://localhost:8188`).
- `--output-dir`: Subfolder name for output images (default: `edited`).
- `--delay`: Delay in seconds between edits (default: `120.0`).
- `--timeout`: Request timeout in seconds (default: `600`).
- `--retries`: Retry attempts on network/timeout errors (default: `2`).
- `--overwrite`: Overwrite already edited images in the output folder.
- `--export-prompts [FILE]`: Export image paths and prompt strings to a text file (default: `prompts.txt` in the comic folder).
- `--export-only`: Only export prompts to the text file without executing inpainting edits.

