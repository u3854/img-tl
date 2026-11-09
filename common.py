import re
import sys
import time
import argparse
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
from rich import box
from rich.progress import (
    Progress,
    SpinnerColumn,
    TextColumn,
    BarColumn,
    TimeRemainingColumn,
)
from rich.traceback import install as install_rich_traceback

# Install rich traceback handler for beautiful, readable exception traces
install_rich_traceback(show_locals=False, extra_lines=1)

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

# Use legacy_windows=False to ensure true Unicode rounded corners (╭─╮, ╰─╯) and glyphs
console = Console(legacy_windows=False)

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

TYPE_BADGES = {
    "speech": "[bold cyan]🗣 Speech[/bold cyan]",
    "thought": "[bold magenta]💭 Thought[/bold magenta]",
    "narration": "[bold yellow]📜 Narration[/bold yellow]",
    "sfx": "[bold green]⚡ SFX[/bold green]",
}

FIELD_ICONS = {
    "input folder": "📁",
    "image folder": "📁",
    "source json": "📄",
    "workflow json": "🧩",
    "output file": "💾",
    "output folder": "📂",
    "language": "🌐",
    "queue size": "📊",
    "context memory": "🧠",
    "settings": "⚙️",
    "model settings": "⚙️",
    "constraints": "⏱️",
    "comfyui url": "🔌",
    "workflow nodes": "🔗",
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


# --- Custom Rich Argument Parser ---

class RichArgumentParser(argparse.ArgumentParser):
    """An argparse.ArgumentParser subclass that renders rich, modern help and error messages."""

    def error(self, message: str):
        console.print()
        console.print(f"  [bold bright_red]✖ Argument Error:[/bold bright_red] [white]{message}[/white]")
        console.print(f"\n  [dim]Tip: Run with [bold bright_cyan]--help[/bold bright_cyan] to inspect all options and usage examples.[/dim]\n")
        sys.exit(2)

    def print_help(self, file=None):
        console.print()

        desc = (self.description or "").strip()
        usage_text = self.format_usage().strip()
        if usage_text.startswith("usage: "):
            usage_text = usage_text[7:]

        header_lines = []
        if desc:
            header_lines.append(f"[bold bright_white]{desc}[/bold bright_white]")
            header_lines.append("")
        header_lines.append(f"[dim]Usage:[/dim] [bold cyan]{usage_text}[/bold cyan]")

        console.print(Panel(
            "\n".join(header_lines),
            title=f"[bold bright_cyan] ⚙️  {self.prog} [/bold bright_cyan]",
            box=box.ROUNDED,
            border_style="bright_blue",
            padding=(1, 2)
        ))

        pos_actions = [a for a in self._actions if not a.option_strings and a.dest != "help"]
        if pos_actions:
            pos_table = Table(
                box=box.ROUNDED,
                border_style="dim blue",
                show_header=True,
                header_style="bold bright_cyan",
                title="[bold bright_white]Positional Arguments[/bold bright_white]",
                title_justify="left",
                pad_edge=True,
                padding=(0, 1)
            )
            pos_table.add_column("Argument", style="bold bright_yellow", width=20)
            pos_table.add_column("Description", style="white")
            for a in pos_actions:
                pos_table.add_row(a.dest, a.help or "")
            console.print()
            console.print(pos_table)

        opt_actions = [a for a in self._actions if a.option_strings and a.dest != "help"]
        if opt_actions:
            opt_table = Table(
                box=box.ROUNDED,
                border_style="dim blue",
                show_header=True,
                header_style="bold bright_cyan",
                title="[bold bright_white]Options & Flags[/bold bright_white]",
                title_justify="left",
                pad_edge=True,
                padding=(0, 1)
            )
            opt_table.add_column("Option Flag(s)", style="bold cyan", width=22)
            opt_table.add_column("Default", style="dim", max_width=32, overflow="fold")
            opt_table.add_column("Description", style="white")
            for a in opt_actions:
                flags = ", ".join(a.option_strings)
                default_val = str(a.default) if a.default is not None and a.default != argparse.SUPPRESS else ""
                if a.const is True:
                    default_val = "False"

                help_desc = a.help or ""
                if a.choices:
                    choices_str = ", ".join(str(c) for c in a.choices)
                    help_desc += f" [dim](choices: {choices_str})[/dim]"

                opt_table.add_row(flags, f"[dim]{default_val}[/dim]" if default_val else "-", help_desc)
            console.print()
            console.print(opt_table)

        if self.epilog:
            console.print()
            console.print(Panel(
                self.epilog.strip(),
                title="[bold bright_white] 💡 Quick Examples [/bold bright_white]",
                border_style="dim cyan",
                box=box.ROUNDED,
                padding=(1, 2)
            ))

        console.print()


# --- UI & Display Helpers ---

def print_banner(title: str, subtitle: str, fields: dict[str, Any], border_style: str = "bright_blue"):
    """Displays an elegant, informative startup panel."""
    table = Table.grid(padding=(0, 2))
    table.add_column("Field", style="bold white", justify="right")
    table.add_column("Value", style="cyan")

    for key, value in fields.items():
        icon = FIELD_ICONS.get(key.lower(), "•")
        val_str = str(value)
        val_str = val_str.replace("Enabled", "[bold bright_green]Enabled[/bold bright_green]")
        val_str = val_str.replace("Disabled", "[dim yellow]Disabled[/dim yellow]")
        table.add_row(f"{icon} {key}:", val_str)

    panel = Panel(
        table,
        title=f"[bold bright_white] {title} [/bold bright_white]",
        subtitle=f"[dim italic] {subtitle} [/dim italic]" if subtitle else None,
        border_style=border_style,
        box=box.ROUNDED,
        padding=(1, 3)
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
    pct = int((queue_pos / queue_total) * 100) if queue_total > 0 else 0
    pos_badge = f"[dim](Page {overall_pos}/{overall_total})[/dim] " if (overall_pos and overall_total) else ""
    extra_badge = f" [dim]{extra}[/dim]" if extra else ""

    header_text = (
        f" [bold bright_cyan]Queue {queue_pos}/{queue_total}[/bold bright_cyan] "
        f"[dim]({pct}%)[/dim] │ "
        f"{pos_badge}"
        f"[bold bright_yellow]📄 {filename}[/bold bright_yellow]"
        f"{extra_badge} "
    )
    console.print()
    console.print(Rule(title=header_text, style="cyan", characters="─", align="left"))


def preview_dialogues(dialogues: list[dict], max_items: int = 6):
    """Renders an attractive, well-aligned preview table of translated dialogues."""
    if not dialogues:
        console.print("  [dim italic]💬 No story dialogue or narration detected on this page[/dim italic]")
        return

    table = Table(
        box=box.ROUNDED,
        border_style="dim blue",
        header_style="bold bright_cyan",
        show_lines=False,
        pad_edge=True,
        padding=(0, 1),
    )
    table.add_column("#", style="dim", justify="center", width=3)
    table.add_column("Type", width=14, justify="left")
    table.add_column("Original Text", style="dim italic", max_width=40, overflow="fold")
    table.add_column("English Translation", style="bold white", max_width=50, overflow="fold")

    display_items = dialogues[:max_items]
    for idx, item in enumerate(display_items, start=1):
        dtype = item.get("type", "speech").strip().lower()
        badge = TYPE_BADGES.get(dtype, f"[white]● {dtype.capitalize()}[/white]")
        orig = item.get("original", "").strip().replace("\n", " ")
        trans = item.get("translation", "").strip().replace("\n", " ")
        table.add_row(str(idx), badge, orig, trans)

    console.print(table)
    if len(dialogues) > max_items:
        console.print(f"  [dim italic]... and {len(dialogues) - max_items} more items saved to JSON[/dim italic]")


def preview_replacements(dialogues: list[dict], max_items: int = 6):
    """Renders a preview table of text replacements for inpainting."""
    if not dialogues:
        console.print("  [dim italic]ℹ No dialogue replacements needed for this page[/dim italic]")
        return

    table = Table(
        box=box.ROUNDED,
        border_style="dim magenta",
        header_style="bold bright_magenta",
        show_lines=False,
        pad_edge=True,
        padding=(0, 1),
    )
    table.add_column("#", style="dim", justify="center", width=3)
    table.add_column("Type", width=14, justify="left")
    table.add_column("Original Text", style="dim italic", max_width=35, overflow="fold")
    table.add_column("", justify="center", width=3)
    table.add_column("Inpaint Replacement", style="bold white", max_width=45, overflow="fold")

    display_items = dialogues[:max_items]
    for idx, item in enumerate(display_items, start=1):
        dtype = item.get("type", "speech").strip().lower()
        badge = TYPE_BADGES.get(dtype, f"[white]● {dtype.capitalize()}[/white]")
        orig = item.get("original", "").strip().replace("\n", " ")
        trans = item.get("translation", "").strip().replace("\n", " ")
        table.add_row(
            str(idx),
            badge,
            orig,
            "[bold bright_green]➔[/bold bright_green]",
            trans
        )

    console.print(table)
    if len(dialogues) > max_items:
        console.print(f"  [dim italic]... and {len(dialogues) - max_items} more inpaint replacements scheduled[/dim italic]")


def countdown_delay(delay_seconds: float):
    """Interactive ticking countdown for the delay cooldown between pages."""
    if delay_seconds <= 0:
        return

    int_delay = int(round(delay_seconds))
    with Progress(
        SpinnerColumn(spinner_name="clock", style="bold bright_cyan"),
        TextColumn("[bold bright_cyan]Cooldown Delay[/bold bright_cyan]"),
        BarColumn(bar_width=22, style="dim white", complete_style="bright_cyan", finished_style="bright_green"),
        TextColumn("[bold white]{task.fields[rem]:.0f}s[/bold white] remaining"),
        TextColumn("[dim]• Ctrl+C to pause/stop[/dim]"),
        console=console,
        transient=True,
        refresh_per_second=10,
    ) as progress:
        task_id = progress.add_task("cooldown", total=delay_seconds, rem=delay_seconds)
        start_t = time.perf_counter()
        while True:
            elapsed = time.perf_counter() - start_t
            rem = max(0.0, delay_seconds - elapsed)
            progress.update(task_id, completed=elapsed, rem=rem)
            if rem <= 0.05:
                break
            time.sleep(0.1)


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
        eta_part = f"[dim]⏳ Left:[/dim] [bold bright_yellow]{remaining_items}p[/bold bright_yellow] [dim](~{format_duration(projected_seconds)})[/dim]  [dim]•[/dim]  [dim]🎯 ETA:[/dim] [bold bright_cyan]{eta_clock}[/bold bright_cyan]"
    else:
        eta_part = "[bold bright_green]✔ Queue complete![/bold bright_green]"

    console.print(
        f"  [dim]⏱  API:[/dim] [bold white]{format_duration(api_duration)}[/bold white]  "
        f"[dim]•[/dim]  [dim]⚡ Step:[/dim] [bold white]{format_duration(cycle_duration)}[/bold white]  "
        f"[dim]•[/dim]  [dim]📊 Avg:[/dim] [bold white]{format_duration(avg_cycle)}/p[/bold white]  "
        f"[dim]•[/dim]  {eta_part}"
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
    table = Table.grid(padding=(0, 2))
    table.add_column("Key", style="bold white", justify="right")
    table.add_column("Value", style="cyan")

    table.add_row("📊 Total Processed:", f"[bold white]{total_processed}[/bold white] pages")
    table.add_row("✔ Successful:", f"[bold bright_green]{successful}[/bold bright_green]")
    if failed > 0:
        table.add_row("✖ Failed:", f"[bold bright_red]{failed}[/bold bright_red]")
        border_style = "bright_yellow" if successful > 0 else "bright_red"
    else:
        table.add_row("✖ Failed:", "[dim]0[/dim]")

    pct = int((successful / total_processed) * 100) if total_processed > 0 else 100
    table.add_row("🎯 Success Rate:", f"[bold bright_green]{pct}%[/bold bright_green]")
    table.add_row("⏱ Total Duration:", f"[bold cyan]{format_duration(total_time)}[/bold cyan]")

    avg_time = (total_time / total_processed) if total_processed > 0 else 0
    table.add_row("⚡ Average Throughput:", f"{format_duration(avg_time)} / page")

    if extras:
        for k, v in extras.items():
            table.add_row(f"⚙️ {k}:", str(v))

    table.add_row("💾 Saved Output:", f"[bold bright_cyan]{output_path}[/bold bright_cyan]")

    panel = Panel(
        table,
        title=f"[bold bright_white] {title} [/bold bright_white]",
        border_style=border_style,
        box=box.ROUNDED,
        padding=(1, 3)
    )
    console.print()
    console.print(panel)
    console.print()


def print_info(msg: str):
    console.print(f"  [bold bright_cyan]ℹ[/bold bright_cyan]  {msg}")


def print_success(msg: str):
    console.print(f"  [bold bright_green]✔[/bold bright_green]  {msg}")


def print_warning(msg: str):
    console.print(f"  [bold bright_yellow]▲[/bold bright_yellow]  {msg}")


def print_error(msg: str):
    console.print(f"  [bold bright_red]✖[/bold bright_red]  {msg}")
