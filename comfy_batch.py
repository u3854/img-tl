import io
import os
import sys
import copy
import json
import time
import random
import requests
from pathlib import Path
from typing import Optional, Set
from PIL import Image

from common import (
    console,
    clean_path,
    natural_sort_key,
    format_duration,
    SessionTracker,
    RichArgumentParser,
    print_banner,
    print_step_header,
    countdown_delay,
    print_stats,
    print_summary,
    print_info,
    print_success,
    print_warning,
    print_error,
    load_prompts_file,
    VALID_IMAGE_EXTENSIONS,
    DEFAULT_COMFY_API_URL,
    DEFAULT_COMFY_DELAY,
    DEFAULT_T2I_DELAY,
    DEFAULT_OUTPUT_DIR,
    DEFAULT_T2I_OUTPUT_DIR,
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
    Identifies the positive text prompt node in the workflow.
    Checks KSampler positive conditioning links, preferred text encoder classes
    (excluding negative nodes), and falls back to any node accepting 'prompt' or 'text'.
    """
    # 1. Check KSampler positive conditioning input link
    for node_id, node in workflow.items():
        if isinstance(node, dict) and "KSampler" in node.get("class_type", ""):
            pos_link = node.get("inputs", {}).get("positive")
            if isinstance(pos_link, list) and len(pos_link) > 0:
                linked_id = str(pos_link[0])
                linked_node = workflow.get(linked_id)
                if isinstance(linked_node, dict):
                    inputs = linked_node.get("inputs", {})
                    if "prompt" in inputs or "text" in inputs:
                        return linked_id
                    for in_val in inputs.values():
                        if isinstance(in_val, list) and len(in_val) > 0:
                            cand_id = str(in_val[0])
                            cand_node = workflow.get(cand_id)
                            if isinstance(cand_node, dict) and ("prompt" in cand_node.get("inputs", {}) or "text" in cand_node.get("inputs", {})):
                                return cand_id

    # 2. Preferred text encoder classes
    preferred_classes = {
        "TextEncodeQwenImage21",
        "CLIPTextEncode",
        "CLIPTextEncodeSDXL",
        "CLIPTextEncodeFlux",
    }
    # Pass 2a: preferred class with title indicating positive or prompt (and not negative)
    for node_id, node in workflow.items():
        if isinstance(node, dict) and node.get("class_type") in preferred_classes:
            title = (node.get("_meta", {}).get("title") or "").lower()
            if "positive" in title or ("prompt" in title and "negative" not in title):
                return node_id

    # Pass 2b: preferred class without 'negative' in title
    for node_id, node in workflow.items():
        if isinstance(node, dict) and node.get("class_type") in preferred_classes:
            title = (node.get("_meta", {}).get("title") or "").lower()
            if "negative" not in title:
                return node_id

    # 3. Fallback: Any node accepting 'prompt' or 'text' inputs (ignoring negative nodes)
    for node_id, node in workflow.items():
        if isinstance(node, dict) and "inputs" in node:
            title = (node.get("_meta", {}).get("title") or "").lower()
            if "negative" in title:
                continue
            if "prompt" in node["inputs"] or "text" in node["inputs"]:
                return node_id

    return None

def inject_prompt_text(node: dict, prompt_text: str):
    """Sets prompt text into either 'prompt' or 'text' input widget as supported by the node."""
    inputs = node.setdefault("inputs", {})
    if "text" in inputs:
        inputs["text"] = prompt_text
    elif "prompt" in inputs:
        inputs["prompt"] = prompt_text
    else:
        inputs["prompt"] = prompt_text

def randomize_workflow_seeds(workflow: dict):
    """Randomizes seed and noise_seed integers across all sampler / seed nodes."""
    new_seed = random.randint(1, 2**32 - 1)
    for node in workflow.values():
        if isinstance(node, dict) and "inputs" in node:
            inputs = node["inputs"]
            if "seed" in inputs and isinstance(inputs["seed"], int):
                inputs["seed"] = new_seed
            if "noise_seed" in inputs and isinstance(inputs["noise_seed"], int):
                inputs["noise_seed"] = new_seed

def clean_workflow_graph(workflow: dict) -> dict:
    """Strips in-graph delay nodes and UI comparer nodes."""
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

def run_comfy_generation(
    base_url: str,
    workflow_template: dict,
    prompt_text: str,
    prompt_node_id: str,
    image_path: Optional[Path] = None,
    image_node_id: Optional[str] = None,
    random_seed: bool = False,
    timeout: int = DEFAULT_TIMEOUT
) -> Image.Image:
    """
    Executes a ComfyUI workflow via API.
    Supports pure text-to-image (image_path=None) or image-to-image.
    """
    wf = clean_workflow_graph(workflow_template)

    if random_seed:
        randomize_workflow_seeds(wf)

    # 1. Upload and inject image if provided (I2I)
    if image_path and image_node_id:
        uploaded_filename = upload_image_to_comfy(base_url, image_path, timeout=min(120, timeout))
        wf[image_node_id]["inputs"]["image"] = uploaded_filename

    # 2. Inject prompt text
    inject_prompt_text(wf[prompt_node_id], prompt_text)

    # 3. Queue prompt
    queue_resp = requests.post(f"{base_url}/prompt", json={"prompt": wf}, timeout=30)
    queue_resp.raise_for_status()
    prompt_id = queue_resp.json()["prompt_id"]

    # 4. Poll /history/<prompt_id>
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

    # 5. Download image from /view
    view_params = {
        "filename": output_image_meta["filename"],
        "subfolder": output_image_meta.get("subfolder", ""),
        "type": output_image_meta.get("type", "temp")
    }
    img_resp = requests.get(f"{base_url}/view", params=view_params, timeout=min(60, timeout))
    img_resp.raise_for_status()

    return Image.open(io.BytesIO(img_resp.content)).convert("RGB")

# --- Batch Mode Runners ---

def run_t2i_mode(args, workflow_template: dict, workflow_path: Path, base_url: str):
    """Executes batch Text-to-Image (T2I) generation from a JSON list of prompts."""
    prompt_node_id = args.prompt_node or find_prompt_node(workflow_template)
    if not prompt_node_id:
        print_error(f"No prompt text node found in workflow '{workflow_path.name}'. Use --prompt-node to specify.")
        return

    if not args.prompts:
        print_error("A prompts JSON file is required for T2I mode. Specify via --prompts <path>.")
        return

    prompts_file = clean_path(args.prompts)
    if not prompts_file.is_file():
        print_error(f"Prompts file not found at '{prompts_file}'.")
        return

    try:
        prompt_list = load_prompts_file(prompts_file)
    except Exception as e:
        print_error(f"Failed to load prompts from '{prompts_file}': {e}")
        return

    if not prompt_list:
        print_warning(f"No valid prompt strings found in '{prompts_file.name}'.")
        return

    # Resolve output directory (default: temp_output in repo root)
    if args.output_dir:
        output_dir = clean_path(args.output_dir)
        if not output_dir.is_absolute():
            output_dir = Path.cwd() / output_dir
    else:
        output_dir = Path.cwd() / DEFAULT_T2I_OUTPUT_DIR

    output_dir.mkdir(parents=True, exist_ok=True)

    pad_width = max(3, len(str(len(prompt_list))))
    inter_delay = args.delay if args.delay is not None else DEFAULT_T2I_DELAY

    # Startup Banner
    banner_info = {
        "Mode": "Text-to-Image (T2I Batch)",
        "Prompts File": prompts_file,
        "Workflow JSON": workflow_path,
        "Output Folder": output_dir,
        "ComfyUI URL": base_url,
        "Total Prompts": f"{len(prompt_list)} prompts queued",
        "Workflow Nodes": f"Prompt (Node {prompt_node_id}) │ Seed={'Randomized' if args.random_seed else 'From Workflow'}",
        "Constraints": f"Delay={inter_delay}s │ Retries={args.retries} │ Timeout={args.timeout}s",
    }
    print_banner(
        title="🎨 BATCH TEXT-TO-IMAGE (T2I) GENERATOR",
        subtitle="Generating images from JSON prompt list via ComfyUI API",
        fields=banner_info,
        border_style="bright_cyan"
    )

    tracker = SessionTracker(total_work_items=len(prompt_list))
    manifest = []
    manifest_file = output_dir / "manifest.json"

    for i, p_text in enumerate(prompt_list, start=1):
        filename = f"{i:0{pad_width}d}.png"
        out_img_path = output_dir / filename

        if not args.overwrite and out_img_path.exists():
            print_info(f"[{i}/{len(prompt_list)}] Output [bold cyan]{filename}[/bold cyan] already exists. Skipping.")
            continue

        preview_snip = p_text if len(p_text) <= 65 else (p_text[:62] + "...")
        print_step_header(
            queue_pos=i,
            queue_total=len(prompt_list),
            filename=filename,
            extra=f'"{preview_snip}"'
        )

        console.print(f"  [dim]Prompt:[/dim] [bold white]{p_text}[/bold white]")

        cycle_start = time.perf_counter()
        api_start = time.perf_counter()
        generated_successfully = False

        with console.status(
            f"  [bold bright_cyan]Generating Image[/bold bright_cyan] [bold bright_yellow]{filename}[/bold bright_yellow] [dim]({workflow_path.name})...[/dim]",
            spinner="dots",
            spinner_style="bold bright_cyan"
        ):
            for attempt in range(1, args.retries + 2):
                try:
                    gen_img = run_comfy_generation(
                        base_url=base_url,
                        workflow_template=workflow_template,
                        prompt_text=p_text,
                        prompt_node_id=prompt_node_id,
                        image_path=None,
                        image_node_id=None,
                        random_seed=args.random_seed,
                        timeout=args.timeout
                    )
                    temp_out = out_img_path.with_suffix(".tmp" + out_img_path.suffix)
                    gen_img.save(temp_out)
                    os.replace(temp_out, out_img_path)
                    generated_successfully = True
                    break
                except (requests.exceptions.RequestException, RuntimeError, TimeoutError) as e:
                    if attempt <= args.retries:
                        print_warning(f"Request failed (Attempt [bold]{attempt}/{args.retries + 1}[/bold]): {e}. Retrying in {int(RETRY_DELAY)}s...")
                        time.sleep(RETRY_DELAY)
                    else:
                        print_error(f"Failed after [bold]{args.retries + 1}[/bold] attempts: {e}")
                except Exception as e:
                    print_error(f"Unexpected error on prompt #{i}: {e}")
                    break

        api_elapsed = time.perf_counter() - api_start
        cycle_elapsed = time.perf_counter() - cycle_start

        manifest.append({
            "index": i,
            "filename": filename,
            "prompt": p_text,
            "success": generated_successfully,
            "duration": round(api_elapsed, 2),
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
        })
        try:
            with open(manifest_file, "w", encoding="utf-8") as mf:
                json.dump(manifest, mf, indent=2, ensure_ascii=False)
        except Exception:
            pass

        if generated_successfully:
            print_success(f"Image saved to [bold cyan]{filename}[/bold cyan] in [bold white]{format_duration(api_elapsed)}[/bold white]")
            tracker.record_step(api_time=api_elapsed, cycle_time=cycle_elapsed, success=True)

            rem_items = len(prompt_list) - i
            projected_sec, finish_clock = tracker.estimate_remaining(inter_item_delay=inter_delay)
            print_stats(
                api_duration=api_elapsed,
                cycle_duration=cycle_elapsed,
                avg_cycle=tracker.avg_cycle_time,
                remaining_items=rem_items,
                projected_seconds=projected_sec,
                eta_clock=finish_clock
            )

            if rem_items > 0 and inter_delay > 0:
                countdown_delay(inter_delay)
        else:
            print_error(f"Generation failed for prompt #{i}")
            tracker.record_step(api_time=api_elapsed, cycle_time=cycle_elapsed, success=False)

    print_summary(
        title="✨ BATCH T2I GENERATION COMPLETE",
        total_processed=len(prompt_list),
        successful=tracker.successful_items,
        failed=tracker.failed_items,
        total_time=tracker.total_elapsed,
        output_path=output_dir,
        extras={
            "Prompts Generated": tracker.successful_items,
            "Manifest": manifest_file.name,
            "Workflow Used": workflow_path.name
        },
        border_style="bright_cyan"
    )

def run_i2i_mode(args, workflow_template: dict, workflow_path: Path, base_url: str):
    """Executes batch Image-to-Image (I2I) processing with a constant prompt on a folder of images."""
    image_node_id = args.image_node or find_node_by_class(workflow_template, {"LoadImage"})
    if not image_node_id:
        print_error(f"No 'LoadImage' node found in workflow '{workflow_path.name}'. Use --image-node to specify.")
        return

    prompt_node_id = args.prompt_node or find_prompt_node(workflow_template)
    if not prompt_node_id:
        print_error(f"No prompt text node found in workflow '{workflow_path.name}'. Use --prompt-node to specify.")
        return

    # Resolve constant prompt text
    constant_prompt = ""
    if args.prompt_file:
        p_path = clean_path(args.prompt_file)
        if not p_path.is_file():
            print_error(f"Prompt file not found at '{p_path}'.")
            return
        with open(p_path, "r", encoding="utf-8") as f:
            constant_prompt = f.read().strip()
    elif args.prompt:
        constant_prompt = args.prompt.strip()

    if not constant_prompt:
        print_error("A constant prompt is required for I2I mode. Specify via --prompt \"...\" or --prompt-file <path>.")
        return

    if not args.images:
        print_error("An input image folder is required for I2I mode. Specify via --images <folder>.")
        return

    input_folder = clean_path(args.images)
    if not input_folder.is_dir():
        print_error(f"Image folder not found at '{input_folder}'.")
        return

    image_files = [
        f for f in input_folder.iterdir()
        if f.is_file() and f.suffix.lower() in VALID_IMAGE_EXTENSIONS
    ]
    image_files.sort(key=natural_sort_key)

    if not image_files:
        print_warning(f"No valid images ({', '.join(sorted(VALID_IMAGE_EXTENSIONS))}) found in '{input_folder}'.")
        return

    out_subfolder = args.output_dir if args.output_dir else DEFAULT_OUTPUT_DIR
    out_dir_path = clean_path(out_subfolder)
    output_dir = out_dir_path if out_dir_path.is_absolute() else (input_folder / out_dir_path)
    output_dir.mkdir(parents=True, exist_ok=True)

    inter_delay = args.delay if args.delay is not None else DEFAULT_COMFY_DELAY

    # Startup Banner
    preview_prompt = constant_prompt if len(constant_prompt) <= 65 else (constant_prompt[:62] + "...")
    banner_info = {
        "Mode": "Image-to-Image (I2I Constant Prompt)",
        "Image Folder": input_folder,
        "Constant Prompt": f'"{preview_prompt}"',
        "Workflow JSON": workflow_path,
        "Output Folder": output_dir,
        "ComfyUI URL": base_url,
        "Images Found": f"{len(image_files)} image(s)",
        "Workflow Nodes": f"LoadImage (Node {image_node_id}) │ Prompt (Node {prompt_node_id}) │ Seed={'Randomized' if args.random_seed else 'From Workflow'}",
        "Constraints": f"Delay={inter_delay}s │ Retries={args.retries} │ Timeout={args.timeout}s",
    }
    print_banner(
        title="🖼️ BATCH IMAGE-TO-IMAGE (I2I) PROCESSOR",
        subtitle="Processing folder images with custom constant prompt via ComfyUI API",
        fields=banner_info,
        border_style="yellow"
    )

    console.print(f"  [dim]Active Constant Prompt:[/dim] [bold white]{constant_prompt}[/bold white]\n")

    tracker = SessionTracker(total_work_items=len(image_files))

    for i, img_path in enumerate(image_files, start=1):
        filename = img_path.name
        out_img_path = output_dir / filename

        if not args.overwrite and out_img_path.exists():
            print_info(f"[{i}/{len(image_files)}] Output [bold cyan]{filename}[/bold cyan] already exists. Skipping.")
            continue

        print_step_header(
            queue_pos=i,
            queue_total=len(image_files),
            filename=filename,
            extra=f"({img_path.stat().st_size // 1024} KB)"
        )

        cycle_start = time.perf_counter()
        api_start = time.perf_counter()
        edited_successfully = False

        with console.status(
            f"  [bold yellow]Processing via ComfyUI[/bold yellow] [bold bright_yellow]{filename}[/bold bright_yellow] [dim]({workflow_path.name})...[/dim]",
            spinner="dots",
            spinner_style="bold yellow"
        ):
            for attempt in range(1, args.retries + 2):
                try:
                    edited_img = run_comfy_generation(
                        base_url=base_url,
                        workflow_template=workflow_template,
                        prompt_text=constant_prompt,
                        prompt_node_id=prompt_node_id,
                        image_path=img_path,
                        image_node_id=image_node_id,
                        random_seed=args.random_seed,
                        timeout=args.timeout
                    )
                    temp_out = out_img_path.with_suffix(".tmp" + out_img_path.suffix)
                    save_kwargs = {"quality": 95} if out_img_path.suffix.lower() in {".jpg", ".jpeg", ".webp"} else {}
                    edited_img.save(temp_out, **save_kwargs)
                    os.replace(temp_out, out_img_path)
                    edited_successfully = True
                    break
                except (requests.exceptions.RequestException, RuntimeError, TimeoutError) as e:
                    if attempt <= args.retries:
                        print_warning(f"Request failed (Attempt [bold]{attempt}/{args.retries + 1}[/bold]): {e}. Retrying in {int(RETRY_DELAY)}s...")
                        time.sleep(RETRY_DELAY)
                    else:
                        print_error(f"Failed after [bold]{args.retries + 1}[/bold] attempts: {e}")
                except Exception as e:
                    print_error(f"Unexpected error on {filename}: {e}")
                    break

        api_elapsed = time.perf_counter() - api_start
        cycle_elapsed = time.perf_counter() - cycle_start

        if edited_successfully:
            print_success(f"Image edited and saved in [bold white]{format_duration(api_elapsed)}[/bold white]")
            tracker.record_step(api_time=api_elapsed, cycle_time=cycle_elapsed, success=True)

            rem_items = len(image_files) - i
            projected_sec, finish_clock = tracker.estimate_remaining(inter_item_delay=inter_delay)
            print_stats(
                api_duration=api_elapsed,
                cycle_duration=cycle_elapsed,
                avg_cycle=tracker.avg_cycle_time,
                remaining_items=rem_items,
                projected_seconds=projected_sec,
                eta_clock=finish_clock
            )

            if rem_items > 0 and inter_delay > 0:
                countdown_delay(inter_delay)
        else:
            print_error(f"Image processing failed for {filename}")
            tracker.record_step(api_time=api_elapsed, cycle_time=cycle_elapsed, success=False)

    print_summary(
        title="✨ BATCH I2I PROCESSING COMPLETE",
        total_processed=len(image_files),
        successful=tracker.successful_items,
        failed=tracker.failed_items,
        total_time=tracker.total_elapsed,
        output_path=output_dir,
        extras={
            "Images Processed": tracker.successful_items,
            "Workflow Used": workflow_path.name
        },
        border_style="yellow"
    )

def main():
    parser = RichArgumentParser(
        prog="comfy_batch.py",
        description="Batch ComfyUI generation: Text-to-Image (T2I) from JSON prompts, or Image-to-Image (I2I) with constant prompt.",
        epilog="""[bold cyan]Batch Text-to-Image (T2I from Prompts JSON):[/bold cyan]
  uv run python comfy_batch.py --t2i --prompts "prompts.json" -w "t2i-workflow.json"
  uv run python comfy_batch.py --mode t2i --prompts "prompts.json" -w "t2i-workflow.json" --delay 5 --random-seed

[bold cyan]Batch Image-to-Image (I2I with Constant Prompt):[/bold cyan]
  uv run python comfy_batch.py --i2i --images "comics/ch01" --prompt "masterpiece, enhance details" -w "qwen21-gguf.json"
  uv run python comfy_batch.py --mode i2i --images "comics/ch01" --prompt-file "prompt.txt" -w "qwen21-gguf.json" """
    )

    # Workflow argument
    parser.add_argument(
        "workflow_file",
        nargs="?",
        default=None,
        help="Path to exported ComfyUI workflow JSON (API format)"
    )
    parser.add_argument(
        "--workflow", "-w",
        default=None,
        help="Path to exported ComfyUI workflow JSON (API format)"
    )

    # Mode flags
    parser.add_argument(
        "--mode", "-m",
        choices=["t2i", "i2i"],
        default=None,
        help="Batch mode: 't2i' (batch text-to-image from prompts JSON) or 'i2i' (batch image-to-image from folder with constant prompt)"
    )
    parser.add_argument(
        "--t2i",
        action="store_true",
        help="Shorthand for --mode t2i (Batch Text-to-Image from Prompts JSON)"
    )
    parser.add_argument(
        "--i2i",
        action="store_true",
        help="Shorthand for --mode i2i (Batch Image-to-Image from Folder with Constant Prompt)"
    )

    # T2I inputs
    parser.add_argument(
        "--prompts",
        default=None,
        help="Path to JSON file containing a list of prompts for batch T2I generation"
    )

    # I2I inputs
    parser.add_argument(
        "--images",
        default=None,
        help="Path to folder containing images to process for batch I2I"
    )
    parser.add_argument(
        "--prompt", "-p",
        default=None,
        help="Custom constant prompt string for batch I2I mode"
    )
    parser.add_argument(
        "--prompt-file",
        default=None,
        help="Path to text file containing custom constant prompt for batch I2I mode"
    )

    # Advanced node overrides & seed
    parser.add_argument(
        "--prompt-node",
        default=None,
        help="Explicit node ID in the workflow for the prompt text widget"
    )
    parser.add_argument(
        "--image-node",
        default=None,
        help="Explicit node ID in the workflow for the LoadImage widget (for I2I)"
    )
    parser.add_argument(
        "--random-seed",
        action="store_true",
        help="Randomize seeds on all sampler/seed nodes for each generation"
    )

    # Common options
    parser.add_argument(
        "--comfy-url",
        default=DEFAULT_COMFY_API_URL,
        help=f"ComfyUI server base URL (default: {DEFAULT_COMFY_API_URL})"
    )
    parser.add_argument(
        "--output-dir", "-o",
        default=None,
        help="Output directory or subfolder name to save generated images"
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=None,
        help=f"Delay in seconds between API calls (default: {DEFAULT_T2I_DELAY}s for T2I, {DEFAULT_COMFY_DELAY}s for I2I)"
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
        help="Overwrite existing images in output folder"
    )

    args = parser.parse_args()

    # Determine mode
    if args.t2i or args.mode == "t2i" or args.prompts:
        resolved_mode = "t2i"
    elif args.i2i or args.mode == "i2i" or (args.images and (args.prompt or args.prompt_file)):
        resolved_mode = "i2i"
    else:
        parser.error(
            "Please specify a batch mode:\n"
            "  For T2I: python comfy_batch.py --t2i --prompts <prompts.json> -w <workflow.json>\n"
            "  For I2I: python comfy_batch.py --i2i --images <folder> --prompt <text> -w <workflow.json>"
        )

    # Default delay per mode
    if args.delay is None:
        args.delay = DEFAULT_T2I_DELAY if resolved_mode == "t2i" else DEFAULT_COMFY_DELAY

    # Validate workflow JSON
    workflow_arg = args.workflow or args.workflow_file
    if not workflow_arg:
        parser.error(
            "A ComfyUI workflow JSON is mandatory.\n"
            "Specify via positional argument or --workflow <path>."
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

    base_url = args.comfy_url.rstrip("/")

    if resolved_mode == "t2i":
        run_t2i_mode(args, workflow_template, workflow_path, base_url)
    else:
        run_i2i_mode(args, workflow_template, workflow_path, base_url)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print()
        print_warning("Stopped by user (Ctrl+C). Progress has been safely saved.")
        sys.exit(0)
