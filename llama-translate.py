import os
import re
import sys
import json
import time
import base64
import mimetypes
import argparse
import requests
from pathlib import Path

from common import (
    console,
    clean_path,
    natural_sort_key,
    resolve_language,
    format_duration,
    SessionTracker,
    print_banner,
    print_step_header,
    preview_dialogues,
    countdown_delay,
    print_stats,
    print_summary,
    print_success,
    print_warning,
    print_error,
    ALLOWED_DIALOGUE_TYPES,
    VALID_IMAGE_EXTENSIONS,
    DEFAULT_LLAMA_API_URL,
    DEFAULT_LANGUAGE,
    DEFAULT_DELAY,
    DEFAULT_TIMEOUT,
    DEFAULT_RETRIES,
    RETRY_DELAY,
    DEFAULT_THINKING_EFFORT,
    DEFAULT_THINKING_BUDGET,
    DEFAULT_OUTPUT_FILENAME,
)

def build_system_prompt(language_name: str) -> str:
    return f"""You are an expert comic translator.
Identify all story-relevant {language_name} text in the image and translate it into natural English in reading order.

STRICT RULES:
1. IGNORE WATERMARKS: Completely ignore watermarks, website URLs, platform logos, scanlator/translator credits, and page numbers—especially those located at the corners or margins of the page.
2. STRICT JSON SCHEMA: Output ONLY a JSON object with a single top-level key "dialogues" (an array of objects).
3. ALLOWED KEYS ONLY: Each object in "dialogues" MUST contain ONLY these 3 keys:
   - "original": (string) The exact {language_name} text.
   - "translation": (string) The natural English translation.
   - "type": (string) Must be one of: "speech", "thought", "narration", or "sfx".
4. NO EXTRA FIELDS: Do NOT include "position", "style", "bbox", "coordinates", "speaker", "notes", or any other keys.
5. If a page has no story text (or only watermarks), return: {{"dialogues": []}}"""

TRANSLATION_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "dialogues": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "original": {"type": "string"},
                    "translation": {"type": "string"},
                    "type": {
                        "type": "string",
                        "enum": ["speech", "thought", "narration", "sfx"]
                    }
                },
                "required": ["original", "translation", "type"],
                "additionalProperties": False
            }
        }
    },
    "required": ["dialogues"],
    "additionalProperties": False
}

def encode_image_to_data_url(image_path: Path) -> str:
    mime_type, _ = mimetypes.guess_type(image_path)
    if not mime_type:
        mime_type = "image/jpeg"
    with open(image_path, "rb") as img_file:
        b64_data = base64.b64encode(img_file.read()).decode('utf-8')
    return f"data:{mime_type};base64,{b64_data}"

def is_valid_translation(entry) -> bool:
    """Returns True only if the page entry succeeded and has a valid 'dialogues' list."""
    return (
        isinstance(entry, dict)
        and "error" not in entry
        and "dialogues" in entry
        and isinstance(entry["dialogues"], list)
    )

def format_previous_context(prev_entry: dict) -> str:
    """Returns the entire previous page's translation JSON string for context."""
    if not is_valid_translation(prev_entry):
        return ""
    if not prev_entry.get("dialogues"):
        return ""
    return json.dumps(prev_entry, ensure_ascii=False, indent=2)

def sanitize_schema(parsed_json: dict) -> dict:
    """Enforces the exact 3-field schema in Python, stripping any rogue keys."""
    if not isinstance(parsed_json, dict):
        return {"dialogues": []}
    
    raw_dialogues = parsed_json.get("dialogues", [])
    if not isinstance(raw_dialogues, list):
        return {"dialogues": []}

    clean_dialogues = []
    for item in raw_dialogues:
        if isinstance(item, dict):
            orig = str(item.get("original", "")).strip()
            trans = str(item.get("translation", "")).strip()
            dtype = str(item.get("type", "speech")).strip().lower()
            if dtype not in ALLOWED_DIALOGUE_TYPES:
                dtype = "speech"
            if orig or trans:
                clean_dialogues.append({
                    "original": orig,
                    "translation": trans,
                    "type": dtype
                })

    return {"dialogues": clean_dialogues}

def clean_and_parse_json(raw_content: str) -> dict:
    cleaned = re.sub(r"<think>.*?</think>", "", raw_content, flags=re.DOTALL).strip()
    if "<think>" in cleaned:
        cleaned = cleaned.split("<think>")[0].strip()

    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
        
    if not cleaned:
        raise ValueError("Empty response content (model likely exhausted tokens during thinking)")
        
    parsed = json.loads(cleaned)
    return sanitize_schema(parsed)

def translate_image(
    image_path: Path, 
    api_url: str,
    language_name: str = "Chinese",
    prev_context: str = "",
    thinking_effort: str = "low", 
    thinking_budget: int = 1024, 
    timeout: int = 600
) -> dict:
    data_url = encode_image_to_data_url(image_path)
    
    user_prompt = (
        f"Translate all {language_name} comic dialogue/narration/sfx on this current page to English. "
        "Ignore corner watermarks and logos. "
        "Return strictly {\"dialogues\": [{\"original\": \"...\", \"translation\": \"...\", \"type\": \"...\"}]} "
        "with no other fields."
    )
    
    if prev_context:
        user_prompt = (
            "Context from the immediately preceding comic page (use ONLY for character name, pronoun, and story continuity; "
            "do NOT include these lines in your output):\n"
            f"{prev_context}\n\n"
            f"{user_prompt}"
        )

    payload = {
        "messages": [
            {"role": "system", "content": build_system_prompt(language_name)},
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": data_url}},
                    {"type": "text", "text": user_prompt}
                ]
            }
        ],
        "temperature": 0.1,
        "max_tokens": thinking_budget + 2048 if thinking_budget > 0 else 2048,
        "id_slot": 0,
        "cache_prompt": False,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "comic_translation",
                "strict": True,
                "schema": TRANSLATION_JSON_SCHEMA
            }
        },
        "reasoning_effort": thinking_effort,
        "reasoning_budget": thinking_budget,
        "chat_template_kwargs": {
            "enable_thinking": thinking_budget > 0 and thinking_effort != "none",
            "reasoning_effort": thinking_effort,
            "thinking_budget": thinking_budget
        }
    }

    response = requests.post(api_url, json=payload, timeout=timeout)
    response.raise_for_status()
    
    result = response.json()
    message = result["choices"][0]["message"]
    content = message.get("content", "")
    
    return clean_and_parse_json(content)

def save_results(output_file: Path, data: dict):
    temp_file = output_file.with_suffix(".tmp")
    with open(temp_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(temp_file, output_file)

def main():
    parser = argparse.ArgumentParser(description="Translate comic pages using llama-server Vision API.")
    parser.add_argument("folder", help="Path to the folder containing comic images")
    parser.add_argument(
        "--lang", "-l", 
        default=DEFAULT_LANGUAGE, 
        help=f"Source language: chinese (zh), japanese (ja/jp), korean (ko/kr). Default: {DEFAULT_LANGUAGE}"
    )
    parser.add_argument("--api-url", default=DEFAULT_LLAMA_API_URL, help=f"llama-server chat completions URL (default: {DEFAULT_LLAMA_API_URL})")
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY, help=f"Delay in seconds between API calls (default: {DEFAULT_DELAY})")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help=f"Request timeout in seconds (default: {DEFAULT_TIMEOUT})")
    parser.add_argument("--thinking-effort", default=DEFAULT_THINKING_EFFORT, choices=["none", "low", "medium", "high"], help=f"Reasoning effort level (default: {DEFAULT_THINKING_EFFORT})")
    parser.add_argument("--thinking-budget", type=int, default=DEFAULT_THINKING_BUDGET, help=f"Max thinking tokens (default: {DEFAULT_THINKING_BUDGET}, use 0 to disable, -1 for unlimited)")
    parser.add_argument("--no-context", action="store_true", help="Disable passing previous page's translation as context")
    parser.add_argument("--skip-errors", action="store_true", help="Skip earlier failed pages and only resume strictly after the last successful page")
    parser.add_argument("--output", default=DEFAULT_OUTPUT_FILENAME, help=f"Output JSON filename (saved inside input folder, default: {DEFAULT_OUTPUT_FILENAME})")
    parser.add_argument("--retries", type=int, default=DEFAULT_RETRIES, help=f"Number of retries on network/timeout/parse errors (default: {DEFAULT_RETRIES})")
    args = parser.parse_args()

    language_name = resolve_language(args.lang, default="Chinese")
    folder_path = clean_path(args.folder)

    if not folder_path.is_dir():
        print_error(f"'{folder_path}' is not a valid directory.")
        return

    output_file = folder_path / args.output

    all_images = sorted(
        [p for p in folder_path.iterdir() if p.is_file() and p.suffix.lower() in VALID_IMAGE_EXTENSIONS],
        key=natural_sort_key
    )

    if not all_images:
        print_warning("No valid images found in the specified folder.")
        return

    results = {}
    if output_file.exists():
        try:
            with open(output_file, "r", encoding="utf-8") as f:
                results = json.load(f)
        except Exception as e:
            print_warning(f"Could not read existing {output_file.name} ({e}). Starting from page 1.")

    if args.skip_errors:
        last_translated_idx = -1
        for i, img_path in enumerate(all_images):
            if is_valid_translation(results.get(img_path.name)):
                last_translated_idx = i
        start_idx = last_translated_idx + 1 if last_translated_idx >= 0 else 0
        pending_images = [
            img for img in all_images[start_idx:]
            if not is_valid_translation(results.get(img.name))
        ]
    else:
        error_pages = [
            img.name for img in all_images 
            if img.name in results and not is_valid_translation(results.get(img.name))
        ]
        if error_pages:
            print_warning(f"Found {len(error_pages)} previously failed page(s) to retry: {', '.join(error_pages[:5])}{'...' if len(error_pages) > 5 else ''}")
        
        pending_images = [
            img for img in all_images
            if not is_valid_translation(results.get(img.name))
        ]

    if not pending_images:
        print_success("All pages have already been successfully translated! Nothing left to do.")
        return

    # Banner
    banner_info = {
        "Input Folder": folder_path,
        "Output File": output_file,
        "Language": language_name,
        "Queue Size": f"{len(pending_images)} pages to translate ({len(all_images)} total in folder)",
        "Context Memory": "Disabled (--no-context)" if args.no_context else "Enabled (passing previous page context)",
        "Settings": f"Delay={args.delay}s │ Timeout={args.timeout}s │ Thinking: effort={args.thinking_effort}, budget={args.thinking_budget}",
    }
    print_banner(
        title="🦙 LLAMA VISION TRANSLATOR",
        subtitle="Extracting and translating comic dialogue",
        fields=banner_info,
        border_style="bright_blue"
    )

    tracker = SessionTracker(total_work_items=len(pending_images))

    for i, img_path in enumerate(pending_images, start=1):
        filename = img_path.name
        overall_idx = all_images.index(img_path)
        overall_pos = overall_idx + 1

        prev_context = ""
        if not args.no_context and overall_idx > 0:
            prev_page_name = all_images[overall_idx - 1].name
            prev_entry = results.get(prev_page_name)
            prev_context = format_previous_context(prev_entry)

        context_badge = "[+prev context]" if prev_context else ""
        print_step_header(
            queue_pos=i,
            queue_total=len(pending_images),
            filename=filename,
            overall_pos=overall_pos,
            overall_total=len(all_images),
            extra=f"({language_name}) {context_badge}".strip()
        )

        cycle_start = time.perf_counter()
        call_start = time.perf_counter()
        translation_data = None
        is_success = False

        status_msg = f"Calling Vision API for [bold cyan]{filename}[/bold cyan]..."
        with console.status(f"[bold cyan]{status_msg}[/bold cyan]", spinner="dots"):
            for attempt in range(1, args.retries + 2):
                try:
                    translation_data = translate_image(
                        img_path, 
                        args.api_url,
                        language_name=language_name,
                        prev_context=prev_context,
                        thinking_effort=args.thinking_effort,
                        thinking_budget=args.thinking_budget,
                        timeout=args.timeout
                    )
                    results[filename] = translation_data
                    is_success = True
                    break
                except (requests.exceptions.RequestException, json.JSONDecodeError, ValueError) as e:
                    if attempt <= args.retries:
                        print_warning(f"Attempt {attempt}/{args.retries + 1} failed ({e}). Retrying in {int(RETRY_DELAY)}s...")
                        time.sleep(RETRY_DELAY)
                    else:
                        print_error(f"Failed after {args.retries + 1} attempts: {e}")
                        results[filename] = {"error": f"Failed: {str(e)}"}
                except Exception as e:
                    print_error(f"Unexpected error: {e}")
                    results[filename] = {"error": f"Unexpected error: {str(e)}"}
                    break

        api_elapsed = time.perf_counter() - call_start

        if is_success and translation_data:
            dialogues = translation_data.get("dialogues", [])
            print_success(f"Translation completed in {format_duration(api_elapsed)} ({len(dialogues)} dialogues found)")
            preview_dialogues(dialogues)
        else:
            print_error(f"Translation failed for {filename}")

        # Keep keys in translations.json neatly ordered by page number
        ordered_results = {
            img.name: results[img.name]
            for img in all_images
            if img.name in results
        }
        save_results(output_file, ordered_results)

        cycle_elapsed = time.perf_counter() - cycle_start
        tracker.record_step(api_time=api_elapsed, cycle_time=cycle_elapsed, success=is_success)

        # Estimate remaining time and display stats
        remaining_pages = len(pending_images) - i
        projected_sec, finish_clock = tracker.estimate_remaining(inter_item_delay=args.delay)
        print_stats(
            api_duration=api_elapsed,
            cycle_duration=cycle_elapsed,
            avg_cycle=tracker.avg_cycle_time,
            remaining_items=remaining_pages,
            projected_seconds=projected_sec,
            eta_clock=finish_clock
        )

        if remaining_pages > 0 and args.delay > 0:
            countdown_delay(args.delay)

    # Print summary panel
    print_summary(
        title="✨ TRANSLATION COMPLETE",
        total_processed=tracker.completed_items,
        successful=tracker.successful_items,
        failed=tracker.failed_items,
        total_time=tracker.total_elapsed,
        output_path=output_file
    )

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print()
        print_warning("Stopped by user (Ctrl+C). Progress has been safely saved.")
        sys.exit(0)
