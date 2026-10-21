import re
import sys
import time
from pathlib import Path
from typing import Union, Any, Optional
from datetime import datetime, timedelta

# Ensure UTF-8 output streams on Windows systems with non-UTF8 codepages (e.g. CP932, CP1252)
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.rule import Rule
from rich.live import Live
from rich.text import Text

# ==============================================================================
# CONFIGURATION & SERVER DEFAULTS
# Edit these to customize your default server URLs, ports, delays, and settings.
# ==============================================================================

# Server Host & Distinct Server Ports
DEFAULT_HOST = "http://localhost"
DEFAULT_LLAMA_PORT = 8080
DEFAULT_SD_PORT = 8080          # Set to e.g. 8081 or 7860 if SD server runs on a different port
DEFAULT_COMFY_PORT = 8188       # Default ComfyUI server port

# API Endpoints (constructed from host and respective ports)
DEFAULT_LLAMA_API_URL = f"{DEFAULT_HOST}:{DEFAULT_LLAMA_PORT}/v1/chat/completions"
DEFAULT_SD_API_URL = f"{DEFAULT_HOST}:{DEFAULT_SD_PORT}/v1/images/edits"
DEFAULT_COMFY_API_URL = f"{DEFAULT_HOST}:{DEFAULT_COMFY_PORT}"

# Network & Operational Defaults
DEFAULT_DELAY = 60.0       # Delay in seconds between API calls
DEFAULT_TIMEOUT = 600      # Request timeout in seconds
DEFAULT_RETRIES = 2        # Retry attempts on network/timeout errors
RETRY_DELAY = 10.0         # Cooldown in seconds before a retry attempt

# llama-translate.py Defaults
DEFAULT_LANGUAGE = "chinese"
DEFAULT_OUTPUT_FILENAME = "translations.json"
DEFAULT_THINKING_EFFORT = "low"
DEFAULT_THINKING_BUDGET = 1024

# edit_comics.py Defaults
DEFAULT_OUTPUT_DIR = "edited"
DEFAULT_EXPORT_FILENAME = "prompts.txt"
DEFAULT_MAX_SIDE = 1280
DEFAULT_STEPS = 4
DEFAULT_CFG = 1.0
DEFAULT_SAMPLER = "euler"
DEFAULT_SCHEDULER = "simple"

# edit_comics_comfy.py Defaults
DEFAULT_COMFY_DELAY = 120.0     # Default delay in seconds between ComfyUI calls

# ==============================================================================

console = Console()

# --- Common Constants ---

LANGUAGE_MAP = {
    "zh": "Chinese",
    "chinese": "Chinese",
    "ja": "Japanese",
    "jp": "Japanese",
    "japanese": "Japanese",
    "ko": "Korean",
    "kr": "Korean",
    "korean": "Korean",
}

ALLOWED_DIALOGUE_TYPES = {"speech", "thought", "narration", "sfx"}
VALID_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}

TYPE_STYLES = {
    "speech": "bold cyan",
    "thought": "bold magenta",
    "narration": "bold yellow",
    "sfx": "bold green",
}


# --- Path & String Utilities ---

def clean_path(path_str: str) -> Path:
    """Strips surrounding quotes and whitespace, returning a Path object."""
    return Path(path_str.strip().strip('"').strip("'"))


def natural_sort_key(item: Union[str, Path]) -> list:
    """Natural numerical sorting for filenames or Path objects (e.g., page_2 comes before page_10)."""
    name = item.name if isinstance(item, Path) else str(item)
    return [int(text) if text.isdigit() else text.lower() for text in re.split(r'(\d+)', name)]


def resolve_language(lang_input: str, default: str = "") -> str:
    """Resolves language shorthand/aliases (e.g. 'zh' -> 'Chinese', 'ja' -> 'Japanese')."""
    cleaned = lang_input.strip()
    if not cleaned:
        return default
    return LANGUAGE_MAP.get(cleaned.lower(), cleaned.title())


def format_duration(seconds: float) -> str:
    """Formats seconds into a clean human-readable string (e.g., 1h 05m 12s, 3m 45s, 14s)."""
    seconds = max(0, int(round(seconds)))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h > 0:
        return f"{h}h {m:02d}m {s:02d}s"
    elif m > 0:
        return f"{m}m {s:02d}s"
    return f"{s}s"


# --- Inpainting & Prompt Utilities ---

def extract_valid_dialogues(page_entry: dict) -> list:
    """Extracts valid dialogue entries matching ALLOWED_DIALOGUE_TYPES from a translation JSON entry."""
    if not isinstance(page_entry, dict) or "error" in page_entry:
        return []
    raw_dialogues = page_entry.get("dialogues", [])
    if not isinstance(raw_dialogues, list):
        return []
    return [
        d for d in raw_dialogues
        if isinstance(d, dict)
        and d.get("type", "speech").lower() in ALLOWED_DIALOGUE_TYPES
        and d.get("original", "").strip()
        and d.get("translation", "").strip()
    ]


def build_edit_prompt(dialogues: list, language_name: str = "") -> str:
    """Constructs a structured inpainting replacement prompt for the image generation model."""
    if language_name:
        header = f"Surgically remove original {language_name} text and inpaint corresponding English translation using the same font colours (All English characters must be large, clear and readable):"
    else:
        header = "Surgically remove original text and inpaint corresponding English translation using the same font colours (All English characters must be large, clear and readable):"

    lines = [header]
    for idx, item in enumerate(dialogues, start=1):
        dtype = item.get("type", "speech").strip().lower()
        orig = item.get("original", "").strip().replace('"', '\\"')
        trans = item.get("translation", "").strip().replace('"', '\\"')
        lines.append(f'{idx}. Replace {dtype} "{orig}" with "{trans}"')
    return "\n".join(lines)


def export_prompts_to_file(
    translations_data: dict,
    translated_filenames: list,
    folder_path: Path,
    language_name: str,
    export_path: Path
) -> int:
    """Exports image absolute paths and generated prompts to a text file."""
    entries = []
    for fname in translated_filenames:
        entry = translations_data.get(fname, {})
        dialogues = extract_valid_dialogues(entry)
        if not dialogues:
            continue
        src_img_path = folder_path / fname
        if not src_img_path.is_file():
            continue
        prompt = build_edit_prompt(dialogues, language_name)
        entries.append(f"{src_img_path.resolve()}\n{prompt}")

    if not entries:
        return 0

    export_path.parent.mkdir(parents=True, exist_ok=True)
    with open(export_path, "w", encoding="utf-8") as f:
        f.write("\n\n".join(entries) + "\n")

    return len(entries)


# --- ETA & Progress Tracker ---

class SessionTracker:
    """Tracks cycle durations, rolling averages, remaining items, and ETA clock."""

    def __init__(self, total_work_items: int):
        self.total_work_items = total_work_items
        self.completed_items = 0
        self.successful_items = 0
        self.failed_items = 0
        self.session_start = time.perf_counter()
        self.api_durations: list[float] = []
        self.cycle_durations: list[float] = []

    def record_step(self, api_time: float, cycle_time: float, success: bool = True):
        self.completed_items += 1
        if success:
            self.successful_items += 1
        else:
            self.failed_items += 1
        self.api_durations.append(api_time)
        self.cycle_durations.append(cycle_time)

    @property
    def avg_api_time(self) -> float:
        return sum(self.api_durations) / len(self.api_durations) if self.api_durations else 0.0

    @property
    def avg_cycle_time(self) -> float:
        return sum(self.cycle_durations) / len(self.cycle_durations) if self.cycle_durations else 0.0

    @property
    def remaining_items(self) -> int:
        return max(0, self.total_work_items - self.completed_items)

    def estimate_remaining(self, inter_item_delay: float = 0.0) -> tuple[float, str]:
        """Returns (projected_remaining_seconds, eta_wall_clock_time_str)."""
        rem = self.remaining_items
        if rem <= 0:
            return 0.0, datetime.now().strftime("%H:%M:%S")

        avg_cycle = self.avg_cycle_time
        if avg_cycle <= 0.0:
            avg_cycle = (self.avg_api_time + inter_item_delay) if self.avg_api_time > 0 else inter_item_delay

        # Don't double count the trailing delay on the final item
        projected_sec = max(0.0, (rem * avg_cycle) - (inter_item_delay if rem == 1 else 0.0))
        finish_time = datetime.now() + timedelta(seconds=projected_sec)
        return projected_sec, finish_time.strftime("%H:%M:%S")

    @property
    def total_elapsed(self) -> float:
        return time.perf_counter() - self.session_start


# --- UI & Display Helpers ---

def print_banner(title: str, subtitle: str, fields: dict[str, Any], border_style: str = "bright_blue"):
    """Displays a clean configuration panel at the start of a run."""
    table = Table(box=None, show_header=False, pad_edge=False, padding=(0, 2))
    table.add_column("Field", style="bold white")
    table.add_column("Value", style="cyan")

    for key, value in fields.items():
        table.add_row(f"{key}:", str(value))

    panel = Panel(
        table,
        title=f"[bold white]{title}[/bold white]",
        subtitle=f"[dim]{subtitle}[/dim]" if subtitle else None,
        border_style=border_style,
        padding=(1, 2)
    )
    console.print()
    console.print(panel)
    console.print()


def print_step_header(
    queue_pos: int, 
    queue_total: int, 
    filename: str, 
    overall_pos: Optional[int] = None, 
    overall_total: Optional[int] = None, 
    extra: str = ""
):
    """Prints a styled divider rule marking the current page being processed."""
    pos_str = f"Page {overall_pos}/{overall_total} | " if (overall_pos and overall_total) else ""
    extra_str = f" {extra}" if extra else ""
    header_text = f"[bold white]Queue {queue_pos}/{queue_total}[/bold white] │ {pos_str}[bold yellow]{filename}[/bold yellow]{extra_str}"
    console.print(Rule(title=header_text, style="dim white", align="left"))


def preview_dialogues(dialogues: list[dict], max_items: int = 5):
    """Renders a preview table of translated dialogues."""
    if not dialogues:
        console.print("  [dim italic](No dialogue or text detected on this page)[/dim italic]")
        return

    table = Table(
        box=None,
        show_header=True,
        header_style="bold dim",
        pad_edge=False,
        padding=(0, 2)
    )
    table.add_column("#", style="dim", justify="right", width=3)
    table.add_column("Type", width=11)
    table.add_column("Original", style="dim", max_width=35, overflow="ellipsis")
    table.add_column("Translation", style="white", max_width=45, overflow="ellipsis")

    display_items = dialogues[:max_items]
    for idx, item in enumerate(display_items, start=1):
        dtype = item.get("type", "speech").strip().lower()
        color = TYPE_STYLES.get(dtype, "white")
        orig = item.get("original", "").strip().replace("\n", " ")
        trans = item.get("translation", "").strip().replace("\n", " ")
        table.add_row(str(idx), f"[{color}]{dtype}[/{color}]", orig, trans)

    console.print(table)
    if len(dialogues) > max_items:
        console.print(f"  [dim]... and {len(dialogues) - max_items} more text items[/dim]")


def preview_replacements(dialogues: list[dict], max_items: int = 5):
    """Renders a preview list of text replacements for inpainting."""
    if not dialogues:
        console.print("  [dim italic](No dialogue replacements)[/dim italic]")
        return

    table = Table(
        box=None,
        show_header=True,
        header_style="bold dim",
        pad_edge=False,
        padding=(0, 2)
    )
    table.add_column("#", style="dim", justify="right", width=3)
    table.add_column("Type", width=11)
    table.add_column("Original ➔ Replacement", style="white", max_width=75, overflow="ellipsis")

    display_items = dialogues[:max_items]
    for idx, item in enumerate(display_items, start=1):
        dtype = item.get("type", "speech").strip().lower()
        color = TYPE_STYLES.get(dtype, "white")
        orig = item.get("original", "").strip().replace("\n", " ")
        trans = item.get("translation", "").strip().replace("\n", " ")
        table.add_row(
            str(idx),
            f"[{color}]{dtype}[/{color}]",
            f'[dim]"{orig}"[/dim]  [bold bright_green]➔[/bold bright_green]  [white]"{trans}"[/white]'
        )

    console.print(table)
    if len(dialogues) > max_items:
        console.print(f"  [dim]... and {len(dialogues) - max_items} more replacements[/dim]")


def countdown_delay(delay_seconds: float):
    """Interactive ticking countdown for the delay cooldown between pages."""
    if delay_seconds <= 0:
        return

    int_delay = int(round(delay_seconds))
    with Live(console=console, transient=True, refresh_per_second=4) as live:
        for remaining in range(int_delay, 0, -1):
            bar_pct = (int_delay - remaining) / int_delay
            filled = int(bar_pct * 15)
            bar = f"[{'━' * filled}{'─' * (15 - filled)}]"
            live.update(
                Text.from_markup(
                    f"  [dim]⏳ Cooldown delay {bar} [bold white]{remaining}s[/bold white] remaining (Ctrl+C to stop)[/dim]"
                )
            )
            time.sleep(1)


def print_stats(
    api_duration: float, 
    cycle_duration: float, 
    avg_cycle: float, 
    remaining_items: int, 
    projected_seconds: float, 
    eta_clock: str
):
    """Prints a consistent, compact timing and ETA line."""
    if remaining_items > 0:
        eta_part = f" │ [bold]Left:[/bold] {remaining_items} (~{format_duration(projected_seconds)}, [bold cyan]ETA {eta_clock}[/bold cyan])"
    else:
        eta_part = " │ [bold green]Queue completed![/bold green]"

    console.print(
        f"  [dim]⏱[/dim] [bold]API:[/bold] {format_duration(api_duration)} │ "
        f"[bold]Total:[/bold] {format_duration(cycle_duration)} │ "
        f"[bold]Session Avg:[/bold] {format_duration(avg_cycle)}/page"
        f"{eta_part}"
    )


def print_summary(
    title: str, 
    total_processed: int, 
    successful: int, 
    failed: int, 
    total_time: float, 
    output_path: Union[Path, str], 
    extras: Optional[dict[str, Any]] = None,
    border_style: str = "bright_green"
):
    """Displays a closing summary panel with stats and destination."""
    table = Table(box=None, show_header=False, pad_edge=False, padding=(0, 2))
    table.add_column("Metric", style="bold white")
    table.add_column("Value", style="cyan")

    table.add_row("Total Processed:", str(total_processed))
    table.add_row("Successful:", f"[bold green]{successful}[/bold green]")
    if failed > 0:
        table.add_row("Failed:", f"[bold red]{failed}[/bold red]")
    table.add_row("Total Session Time:", format_duration(total_time))
    avg_time = (total_time / total_processed) if total_processed > 0 else 0
    table.add_row("Average per Page:", f"{format_duration(avg_time)}/page")

    if extras:
        for k, v in extras.items():
            table.add_row(f"{k}:", str(v))

    table.add_row("Saved Output:", str(output_path))

    panel = Panel(
        table,
        title=f"[bold white]{title}[/bold white]",
        border_style=border_style,
        padding=(1, 2)
    )
    console.print()
    console.print(panel)
    console.print()


def print_info(msg: str):
    console.print(f"[bold blue]ℹ[/bold blue] {msg}")


def print_success(msg: str):
    console.print(f"[bold green]✔[/bold green] {msg}")


def print_warning(msg: str):
    console.print(f"[bold yellow]⚠[/bold yellow] {msg}")


def print_error(msg: str):
    console.print(f"[bold red]✖[/bold red] {msg}")
