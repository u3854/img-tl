import io
import os
import sys
import json
import time
import shutil
import base64
import argparse
import requests
from pathlib import Path
from PIL import Image

from common import (
    console,
    clean_path,
    natural_sort_key,
    resolve_language,
    format_duration,
    SessionTracker,
    print_banner,
    print_step_header,
    preview_replacements,
    countdown_delay,
    print_stats,
    print_summary,
    print_info,
    print_success,
    print_warning,
    print_error,
    extract_valid_dialogues,
    build_edit_prompt,
    export_prompts_to_file,
    ALLOWED_DIALOGUE_TYPES,
    DEFAULT_SD_API_URL,
    DEFAULT_OUTPUT_DIR,
    DEFAULT_EXPORT_FILENAME,
    DEFAULT_MAX_SIDE,
    DEFAULT_STEPS,
    DEFAULT_CFG,
    DEFAULT_SAMPLER,
    DEFAULT_SCHEDULER,
    DEFAULT_DELAY,
    DEFAULT_TIMEOUT,
    DEFAULT_RETRIES,
    RETRY_DELAY,
)

def round_to_32(val: int) -> int:
    return max(32, int(round(val / 32.0)) * 32)

def ceil_to_32(val: int) -> int:
    return max(32, ((val + 31) // 32) * 32)

def prepare_image_for_sd(image_path: Path, max_side: int = 1280, pad_mode: bool = False):
    """
    Scales the image so its longest side is `max_side`, and aligns dimensions to multiples of 32.
    Returns (png_bytes, sd_width, sd_height, scaled_width, scaled_height, pad_box)
    """
    with Image.open(image_path) as img:
        img = img.convert("RGB")
        orig_w, orig_h = img.size

        scale = max_side / float(max(orig_w, orig_h))
        scaled_w = max(1, int(round(orig_w * scale)))
        scaled_h = max(1, int(round(orig_h * scale)))

        if not pad_mode:
            sd_w = round_to_32(scaled_w)
            sd_h = round_to_32(scaled_h)
            resized = img.resize((sd_w, sd_h), Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            resized.save(buf, format="PNG")
            return buf.getvalue(), sd_w, sd_h, scaled_w, scaled_h, None
        else:
            sd_w = ceil_to_32(scaled_w)
            sd_h = ceil_to_32(scaled_h)
            resized = img.resize((scaled_w, scaled_h), Image.Resampling.LANCZOS)

            canvas = Image.new("RGB", (sd_w, sd_h), (255, 255, 255))
            left = (sd_w - scaled_w) // 2
            top = (sd_h - scaled_h) // 2
            canvas.paste(resized, (left, top))

            buf = io.BytesIO()
            canvas.save(buf, format="PNG")
            pad_box = (left, top, left + scaled_w, top + scaled_h)
            return buf.getvalue(), sd_w, sd_h, scaled_w, scaled_h, pad_box

def edit_image_via_sd_server(
    image_path: Path,
    prompt: str,
    api_url: str,
    max_side: int = 1280,
    steps: int = 4,
    cfg_scale: float = 1.0,
    sampler: str = "euler",
    scheduler: str = "simple",
    pad_mode: bool = False,
    timeout: int = 600
) -> Image.Image:
    png_bytes, sd_w, sd_h, scaled_w, scaled_h, pad_box = prepare_image_for_sd(
        image_path, max_side=max_side, pad_mode=pad_mode
    )

    extra_args = {
        "steps": steps,
        "cfg_scale": cfg_scale,
        "sample_method": sampler,
        "scheduler": scheduler
    }
    full_prompt = f"{prompt} <sd_cpp_extra_args>{json.dumps(extra_args)}</sd_cpp_extra_args>"

    files = [
        ("image[]", (image_path.stem + ".png", png_bytes, "image/png"))
    ]
    data = {
        "prompt": full_prompt,
        "size": f"{sd_w}x{sd_h}",
        "n": "1",
        "response_format": "b64_json"
    }

    response = requests.post(api_url, files=files, data=data, timeout=timeout)
    response.raise_for_status()

    result = response.json()
    b64_img = result["data"][0]["b64_json"]
    out_bytes = base64.b64decode(b64_img)

    out_img = Image.open(io.BytesIO(out_bytes)).convert("RGB")

    if pad_mode and pad_box is not None:
        out_img = out_img.crop(pad_box)

    return out_img

def main():
    parser = argparse.ArgumentParser(description="Batch edit comic pages driven strictly by a translations JSON file.")
    parser.add_argument(
        "translations_file",
        help="Path to the translations JSON file (images are loaded from the same folder)"
    )
    parser.add_argument(
        "--lang", "-l",
        default="",
        help="Optional source language: chinese (zh), japanese (ja/jp), korean (ko/kr). If omitted, uses 'original text'."
    )
    parser.add_argument("--api-url", default=DEFAULT_SD_API_URL, help=f"sd-server images/edits URL (default: {DEFAULT_SD_API_URL})")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR, help=f"Subfolder name to save edited images (default: {DEFAULT_OUTPUT_DIR})")
    parser.add_argument("--max-side", type=int, default=DEFAULT_MAX_SIDE, help=f"Target longest side in pixels (default: {DEFAULT_MAX_SIDE})")
    parser.add_argument("--pad-to-32", action="store_true", help="Pad instead of slight 2%% squish when aligning to multiples of 32, then crop back")
    parser.add_argument("--steps", type=int, default=DEFAULT_STEPS, help=f"Sampling steps (default: {DEFAULT_STEPS} for Turbo)")
    parser.add_argument("--cfg", type=float, default=DEFAULT_CFG, help=f"CFG scale (default: {DEFAULT_CFG} for Turbo)")
    parser.add_argument("--sampler", default=DEFAULT_SAMPLER, help=f"Sampling method (default: {DEFAULT_SAMPLER})")
    parser.add_argument("--scheduler", default=DEFAULT_SCHEDULER, help=f"Scheduler (default: {DEFAULT_SCHEDULER})")
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY, help=f"Delay in seconds between API calls (default: {DEFAULT_DELAY})")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help=f"Request timeout in seconds (default: {DEFAULT_TIMEOUT})")
    parser.add_argument("--retries", type=int, default=DEFAULT_RETRIES, help=f"Number of retries on network/timeout errors (default: {DEFAULT_RETRIES})")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing images in output subfolder")
    parser.add_argument(
        "--export-prompts",
        nargs="?",
        const=DEFAULT_EXPORT_FILENAME,
        default=None,
        help=f"Export image paths and generated inpainting prompts to a text file (default: {DEFAULT_EXPORT_FILENAME} in comic folder)"
    )
    parser.add_argument(
        "--export-only",
        action="store_true",
        help="Export prompts to the text file and exit without running inpainting"
    )
    args = parser.parse_args()

    language_name = resolve_language(args.lang) if args.lang else ""
    input_path = clean_path(args.translations_file)

    if input_path.is_dir():
        translations_file = input_path / "translations.json"
        folder_path = input_path
    else:
        translations_file = input_path
        folder_path = translations_file.parent

    if not translations_file.is_file():
        print_error(f"Translations file not found at '{translations_file}'.")
        return

    with open(translations_file, "r", encoding="utf-8") as f:
        translations_data = json.load(f)

    if not isinstance(translations_data, dict) or not translations_data:
        print_warning(f"No translation entries found in '{translations_file.name}'.")
        return

    output_dir = folder_path / args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    translated_filenames = sorted(translations_data.keys(), key=natural_sort_key)

    # Export prompts to a text file if requested
    if args.export_only and not args.export_prompts:
        args.export_prompts = DEFAULT_EXPORT_FILENAME

    if args.export_prompts:
        export_target = clean_path(args.export_prompts)
        export_file = export_target if export_target.is_absolute() else (folder_path / export_target)

        exported_count = export_prompts_to_file(
            translations_data=translations_data,
            translated_filenames=translated_filenames,
            folder_path=folder_path,
            language_name=language_name,
            export_path=export_file
        )

        if exported_count > 0:
            print_success(f"Exported prompts for {exported_count} page(s) to: [bold cyan]{export_file}[/bold cyan]")
        else:
            print_warning("No pages with valid dialogues found to export.")

        if args.export_only:
            print_info("Prompt export complete. Exiting (--export-only specified).")
            return

    # Pre-partition queue to eliminate O(N^2) checks inside the loop
    pending_items = []
    edit_count = 0
    copy_count = 0

    for fname in translated_filenames:
        entry = translations_data[fname]
        if not isinstance(entry, dict) or "error" in entry:
            continue
        src_img_path = folder_path / fname
        if not src_img_path.is_file():
            print_warning(f"'{fname}' is in {translations_file.name} but missing in '{folder_path}'. Skipping.")
            continue
        out_img_path = output_dir / fname
        if not args.overwrite and out_img_path.exists():
            continue

        dialogues = extract_valid_dialogues(entry)
        is_edit = len(dialogues) > 0
        if is_edit:
            edit_count += 1
        else:
            copy_count += 1

        pending_items.append({
            "filename": fname,
            "dialogues": dialogues,
            "is_edit": is_edit,
            "src_path": src_img_path,
            "out_path": out_img_path
        })

    if not pending_items:
        print_success(f"All valid entries in '{translations_file.name}' have already been processed in '{output_dir}'!")
        return

    lang_display = language_name if language_name else "Generic ('original text')"
    banner_info = {
        "Source JSON": translations_file,
        "Image Folder": folder_path,
        "Output Folder": output_dir,
        "Language": lang_display,
        "Queue Size": f"{len(pending_items)} pages ({edit_count} to edit, {copy_count} to copy directly | {len(translated_filenames)} total)",
        "Model Settings": f"Steps={args.steps} │ CFG={args.cfg} │ Sampler={args.sampler}/{args.scheduler}",
        "Constraints": f"Max Side={args.max_side}px │ Delay={args.delay}s │ Pad Mode={args.pad_to_32}",
    }
    print_banner(
        title="🎨 COMIC SD INPAINTING EDITOR",
        subtitle="Surgically replacing comic text with translated typography",
        fields=banner_info,
        border_style="magenta"
    )

    # Tracker tracks the edit pages that take compute/time
    tracker = SessionTracker(total_work_items=edit_count)
    remaining_edits = edit_count

    for i, item in enumerate(pending_items, start=1):
        filename = item["filename"]
        dialogues = item["dialogues"]
        is_edit = item["is_edit"]
        src_path = item["src_path"]
        out_path = item["out_path"]
        overall_pos = translated_filenames.index(filename) + 1

        print_step_header(
            queue_pos=i,
            queue_total=len(pending_items),
            filename=filename,
            overall_pos=overall_pos,
            overall_total=len(translated_filenames),
            extra=f"({len(dialogues)} text items)" if is_edit else "(No text)"
        )

        if not is_edit:
            print_info(f"No story dialogue detected in [bold cyan]{filename}[/bold cyan] ➔ Copied original image directly.")
            shutil.copy2(src_path, out_path)
            continue

        preview_replacements(dialogues)
        prompt = build_edit_prompt(dialogues, language_name)

        cycle_start = time.perf_counter()
        api_start = time.perf_counter()
        edited_successfully = False

        status_msg = f"Editing [bold magenta]{filename}[/bold magenta] via SD Server ({args.max_side}px)..."
        with console.status(f"[bold magenta]{status_msg}[/bold magenta]", spinner="dots"):
            for attempt in range(1, args.retries + 2):
                try:
                    edited_img = edit_image_via_sd_server(
                        image_path=src_path,
                        prompt=prompt,
                        api_url=args.api_url,
                        max_side=args.max_side,
                        steps=args.steps,
                        cfg_scale=args.cfg,
                        sampler=args.sampler,
                        scheduler=args.scheduler,
                        pad_mode=args.pad_to_32,
                        timeout=args.timeout
                    )
                    temp_out = out_path.with_suffix(".tmp" + out_path.suffix)
                    save_kwargs = {"quality": 95} if out_path.suffix.lower() in {".jpg", ".jpeg", ".webp"} else {}
                    edited_img.save(temp_out, **save_kwargs)
                    os.replace(temp_out, out_path)
                    edited_successfully = True
                    break
                except requests.exceptions.RequestException as e:
                    if attempt <= args.retries:
                        print_warning(f"Request failed (Attempt {attempt}/{args.retries + 1}): {e}. Retrying in {int(RETRY_DELAY)}s...")
                        time.sleep(RETRY_DELAY)
                    else:
                        print_error(f"Failed after {args.retries + 1} attempts: {e}")
                except Exception as e:
                    print_error(f"Unexpected error on {filename}: {e}")
                    break

        api_elapsed = time.perf_counter() - api_start
        cycle_elapsed = time.perf_counter() - cycle_start

        if edited_successfully:
            remaining_edits -= 1
            print_success(f"Image edited and saved in {format_duration(api_elapsed)} ({len(dialogues)} text replacements)")
            tracker.record_step(api_time=api_elapsed, cycle_time=cycle_elapsed, success=True)

            projected_sec, finish_clock = tracker.estimate_remaining(inter_item_delay=args.delay)
            print_stats(
                api_duration=api_elapsed,
                cycle_duration=cycle_elapsed,
                avg_cycle=tracker.avg_cycle_time,
                remaining_items=remaining_edits,
                projected_seconds=projected_sec,
                eta_clock=finish_clock
            )

            # Wait delay only if there are more edit pages left in the queue
            if remaining_edits > 0 and args.delay > 0:
                countdown_delay(args.delay)
        else:
            remaining_edits -= 1
            print_error(f"Image editing failed for {filename}")
            tracker.record_step(api_time=api_elapsed, cycle_time=cycle_elapsed, success=False)

    print_summary(
        title="✨ COMIC EDITING COMPLETE",
        total_processed=len(pending_items),
        successful=tracker.successful_items + copy_count,
        failed=tracker.failed_items,
        total_time=tracker.total_elapsed,
        output_path=output_dir,
        extras={
            "Pages Inpainted": tracker.successful_items,
            "Pages Copied Direct": copy_count
        },
        border_style="bright_magenta"
    )

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print()
        print_warning("Stopped by user (Ctrl+C). Progress has been safely saved.")
        sys.exit(0)
