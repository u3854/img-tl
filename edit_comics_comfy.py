import io
import os
import sys
import copy
import json
import time
import shutil
import argparse
import requests
from pathlib import Path
from typing import Optional, Set
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
    DEFAULT_COMFY_API_URL,
    DEFAULT_COMFY_DELAY,
    DEFAULT_OUTPUT_DIR,
    DEFAULT_EXPORT_FILENAME,
    DEFAULT_TIMEOUT,
    DEFAULT_RETRIES,
    RETRY_DELAY,
)

def find_node_by_class(workflow: dict, class_types: Set[str]) -> Optional[str]:
    """Finds the first node ID in the workflow matching one of the given class types."""
    for node_id, node in workflow.items():
        if isinstance(node, dict) and node.get("class_type") in class_types:
            return node_id
    return None

def find_prompt_node(workflow: dict) -> Optional[str]:
    """
    Identifies the text prompt node in the workflow. Checks common text encoder class types first,
    then falls back to any node accepting a 'prompt' input.
    """
    preferred_classes = {"TextEncodeQwenImage21", "CLIPTextEncode", "CLIPTextEncodeSDXL"}
    for node_id, node in workflow.items():
        if isinstance(node, dict) and node.get("class_type") in preferred_classes:
            return node_id
    for node_id, node in workflow.items():
        if isinstance(node, dict) and "inputs" in node and "prompt" in node["inputs"]:
            return node_id
    return None

def clean_workflow_graph(workflow: dict) -> dict:
    """
    Strips in-graph delay nodes (e.g. 'Add Delay') and UI comparer nodes (e.g. 'Image Comparer')
    so that Python cleanly handles inter-page delays without ComfyUI blocking internally.
    """
    wf = copy.deepcopy(workflow)
    nodes_to_remove = []
    for node_id, node in wf.items():
        if isinstance(node, dict):
            c_type = node.get("class_type", "")
            if c_type in {"Add Delay", "Image Comparer (rgthree)", "Image Comparer"} or node_id in {"103", "104"}:
                nodes_to_remove.append(node_id)
    for node_id in nodes_to_remove:
        wf.pop(node_id, None)
    return wf

def upload_image_to_comfy(base_url: str, image_path: Path, timeout: int = 120) -> str:
    """Uploads an image to ComfyUI's input directory and returns the server-side filename."""
    with open(image_path, "rb") as f:
        files = {"image": (image_path.name, f, "application/octet-stream")}
        data = {"overwrite": "true", "type": "input"}
        resp = requests.post(f"{base_url}/upload/image", files=files, data=data, timeout=timeout)
        resp.raise_for_status()
        res_json = resp.json()
        subfolder = res_json.get("subfolder", "")
        name = res_json["name"]
        return f"{subfolder}/{name}" if subfolder else name

def edit_image_via_comfy(
    base_url: str,
    workflow_template: dict,
    image_path: Path,
    prompt_text: str,
    image_node_id: str,
    prompt_node_id: str,
    timeout: int = DEFAULT_TIMEOUT
) -> Image.Image:
    """
    Uploads the source image, injects the image filename and replacement prompt into the
    workflow graph, queues the execution in ComfyUI, and retrieves the generated PIL Image.
    All model configurations, samplers, steps, seeds, and VAE settings remain untouched from the workflow.
    """
    # 1. Upload image to ComfyUI
    uploaded_filename = upload_image_to_comfy(base_url, image_path, timeout=min(120, timeout))

    # 2. Prepare workflow payload (strip in-graph delays/comparers so Python manages delay)
    wf = clean_workflow_graph(workflow_template)

    # 3. Inject only the input image and replacement prompt
    wf[image_node_id]["inputs"]["image"] = uploaded_filename
    wf[prompt_node_id]["inputs"]["prompt"] = prompt_text

    # 4. Queue prompt
    queue_resp = requests.post(f"{base_url}/prompt", json={"prompt": wf}, timeout=30)
    queue_resp.raise_for_status()
    prompt_id = queue_resp.json()["prompt_id"]

    # 5. Poll /history/<prompt_id> until complete
    start_time = time.perf_counter()
    output_image_meta = None

    while (time.perf_counter() - start_time) < timeout:
        hist_resp = requests.get(f"{base_url}/history/{prompt_id}", timeout=30)
        hist_resp.raise_for_status()
        hist_data = hist_resp.json()

        if prompt_id in hist_data:
            job_info = hist_data[prompt_id]
            status_info = job_info.get("status", {})
            if status_info.get("status_str") == "error":
                messages = status_info.get("messages", [])
                raise RuntimeError(f"ComfyUI execution error: {messages or status_info}")

            outputs = job_info.get("outputs", {})
            # Look for preview/save image output
            if "101" in outputs and "images" in outputs["101"] and outputs["101"]["images"]:
                output_image_meta = outputs["101"]["images"][0]
                break
            for node_out in outputs.values():
                if "images" in node_out and node_out["images"]:
                    output_image_meta = node_out["images"][0]
                    break
            if output_image_meta:
                break

        time.sleep(1.0)

    if not output_image_meta:
        raise TimeoutError(f"ComfyUI job {prompt_id} timed out after {timeout}s.")

    # 6. Download finished image from /view
    view_params = {
        "filename": output_image_meta["filename"],
        "subfolder": output_image_meta.get("subfolder", ""),
        "type": output_image_meta.get("type", "temp")
    }
    img_resp = requests.get(f"{base_url}/view", params=view_params, timeout=min(60, timeout))
    img_resp.raise_for_status()

    return Image.open(io.BytesIO(img_resp.content)).convert("RGB")

def main():
    parser = argparse.ArgumentParser(
        description="Batch edit comic pages via ComfyUI API using an exported workflow JSON."
    )
    parser.add_argument(
        "translations_file",
        help="Path to the translations JSON file or comic folder (images loaded from the same folder)"
    )
    parser.add_argument(
        "workflow_file",
        nargs="?",
        default=None,
        help="Path to exported ComfyUI workflow JSON (API format). Mandatory."
    )
    parser.add_argument(
        "--workflow", "-w",
        default=None,
        help="Alternative flag for path to exported ComfyUI workflow JSON (API format)"
    )
    parser.add_argument(
        "--lang", "-l",
        default="",
        help="Optional source language: chinese (zh), japanese (ja/jp), korean (ko/kr). If omitted, uses 'original text'."
    )
    parser.add_argument(
        "--comfy-url",
        default=DEFAULT_COMFY_API_URL,
        help=f"ComfyUI server base URL (default: {DEFAULT_COMFY_API_URL})"
    )
    parser.add_argument(
        "--output-dir",
        default=DEFAULT_OUTPUT_DIR,
        help=f"Subfolder name to save edited images (default: {DEFAULT_OUTPUT_DIR})"
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=DEFAULT_COMFY_DELAY,
        help=f"Delay in seconds between API calls (default: {DEFAULT_COMFY_DELAY})"
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT,
        help=f"Request timeout in seconds (default: {DEFAULT_TIMEOUT})"
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=DEFAULT_RETRIES,
        help=f"Number of retries on network/timeout errors (default: {DEFAULT_RETRIES})"
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing images in output subfolder"
    )
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

    # 1. Validate mandatory workflow JSON
    workflow_arg = args.workflow or args.workflow_file
    if not workflow_arg:
        parser.error(
            "A ComfyUI workflow JSON is mandatory.\n"
            "Usage: python edit_comics_comfy.py <translations.json> <workflow.json>\n"
            "   or: python edit_comics_comfy.py <translations.json> --workflow <workflow.json>"
        )

    workflow_path = clean_path(workflow_arg)
    if not workflow_path.is_file():
        print_error(f"Workflow JSON file not found at '{workflow_path}'.")
        return

    try:
        with open(workflow_path, "r", encoding="utf-8") as f:
            workflow_template = json.load(f)
    except json.JSONDecodeError as e:
        print_error(f"Failed to parse workflow JSON '{workflow_path}': {e}")
        return

    if not isinstance(workflow_template, dict) or not workflow_template:
        print_error(f"Invalid or empty workflow JSON in '{workflow_path}'.")
        return

    # 2. Inspect workflow nodes
    image_node_id = find_node_by_class(workflow_template, {"LoadImage"})
    if not image_node_id:
        print_error(f"No 'LoadImage' node found in workflow '{workflow_path.name}'.")
        return

    prompt_node_id = find_prompt_node(workflow_template)
    if not prompt_node_id:
        print_error(f"No prompt text node found in workflow '{workflow_path.name}'.")
        return

    # 3. Resolve translations file & comic folder
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

    # 4. Export prompts to a text file if requested
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

    # 5. Pre-partition queue
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

    # 6. Display banner
    base_url = args.comfy_url.rstrip("/")
    lang_display = language_name if language_name else "Generic ('original text')"
    banner_info = {
        "Source JSON": translations_file,
        "Workflow JSON": workflow_path,
        "Image Folder": folder_path,
        "Output Folder": output_dir,
        "ComfyUI URL": base_url,
        "Language": lang_display,
        "Queue Size": f"{len(pending_items)} pages ({edit_count} to edit, {copy_count} to copy directly | {len(translated_filenames)} total)",
        "Workflow Nodes": f"LoadImage (Node {image_node_id}) │ Prompt (Node {prompt_node_id}) │ Seed=From Workflow",
        "Constraints": f"Delay={args.delay}s │ Retries={args.retries} │ Timeout={args.timeout}s",
    }
    print_banner(
        title="🎨 COMIC COMFYUI INPAINTING EDITOR",
        subtitle="Surgically replacing comic text using ComfyUI workflow JSON",
        fields=banner_info,
        border_style="magenta"
    )

    # 7. Processing loop
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

        status_msg = f"Editing [bold magenta]{filename}[/bold magenta] via ComfyUI ({workflow_path.name})..."
        with console.status(f"[bold magenta]{status_msg}[/bold magenta]", spinner="dots"):
            for attempt in range(1, args.retries + 2):
                try:
                    edited_img = edit_image_via_comfy(
                        base_url=base_url,
                        workflow_template=workflow_template,
                        image_path=src_path,
                        prompt_text=prompt,
                        image_node_id=image_node_id,
                        prompt_node_id=prompt_node_id,
                        timeout=args.timeout
                    )
                    temp_out = out_path.with_suffix(".tmp" + out_path.suffix)
                    save_kwargs = {"quality": 95} if out_path.suffix.lower() in {".jpg", ".jpeg", ".webp"} else {}
                    edited_img.save(temp_out, **save_kwargs)
                    os.replace(temp_out, out_path)
                    edited_successfully = True
                    break
                except (requests.exceptions.RequestException, RuntimeError, TimeoutError) as e:
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
        title="✨ COMIC COMFYUI EDITING COMPLETE",
        total_processed=len(pending_items),
        successful=tracker.successful_items + copy_count,
        failed=tracker.failed_items,
        total_time=tracker.total_elapsed,
        output_path=output_dir,
        extras={
            "Pages Inpainted": tracker.successful_items,
            "Pages Copied Direct": copy_count,
            "Workflow Used": workflow_path.name
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