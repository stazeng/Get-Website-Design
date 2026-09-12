#!/usr/bin/env python3
"""
generate_design_md.py — get-web-design 主入口脚本。

工作流程：
  1. 读取 collected_data.json（由 chrome-devtools 采集，包含 meta + domSnapshot + engineeredCssEvidence）
  2. 读取 3 张截图（PNG/JPEG），编码成 base64 data URL
  3. 调用用户配置的多模态 LLM（OpenAI 兼容接口），结合 DOM + 截图生成风格分析
  4. 将 engineeredCssEvidence 通过 css_evidence.normalize/format 处理为 Markdown
  5. 拼装最终 DESIGN.md：measured token frontmatter + 使用说明 + 八节设计规范 + 验收要求 + 证据附录

环境变量（用户必填）：
  WEB_DESIGN_API_KEY     - 多模态模型 API Key
  WEB_DESIGN_BASE_URL    - 多模态模型 base URL（OpenAI 兼容，如 https://api.moonshot.cn/v1）
  WEB_DESIGN_MODEL       - 模型名（必须支持 vision，如 kimi-latest、gpt-4o、claude-3-5-sonnet-...）

CLI:
  python generate_design_md.py \\
    --collected /tmp/collected.json \\
    --screenshots shot1.png shot2.png shot3.png \\
    --hostname example.com \\
    [--output-dir output/example.com] \\
    [--output output/example.com/design.md] \\
    [--language zh|en]

Default output layout:
  output/<hostname>/design.md
  output/<hostname>/shot1.jpg
  output/<hostname>/shot2.jpg
  output/<hostname>/shot3.jpg
The script copies screenshots into the output dir if they are not already there.
"""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import re
import shutil
import sys
import urllib.error
import urllib.request
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent.parent
ASSETS = SKILL_ROOT / "assets"

sys.path.insert(0, str(SKILL_ROOT / "scripts"))
from css_evidence import format_css_evidence_markdown, normalize_css_evidence  # noqa: E402
from design_document import build_design_frontmatter, extract_design_tokens, validate_design_body  # noqa: E402


# ── 工具 ─────────────────────────────────────────────────────────


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


def encode_image_to_data_url(path: Path) -> str:
    mime, _ = mimetypes.guess_type(str(path))
    if not mime or not mime.startswith("image/"):
        mime = "image/jpeg"
    b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{b64}"


def strip_markdown_fence(text: str) -> str:
    text = (text or "").strip()
    outer_fence = re.fullmatch(r"```(?:markdown)?\s*\n([\s\S]*?)\n```", text, flags=re.IGNORECASE)
    return outer_fence.group(1).strip() if outer_fence else text


def build_frontmatter(hostname: str, tokens=None) -> str:
    return build_design_frontmatter(hostname, tokens)


# ── 构建 LLM 消息 ────────────────────────────────────────────────


def build_messages(collected: dict, screenshots: list[Path], language: str) -> list:
    meta = collected.get("meta") or {}
    dom_snapshot = collected.get("domSnapshot") or {}
    hostname = meta.get("hostname") or "unknown"
    title = meta.get("title") or ""
    measured = extract_design_tokens(collected.get("engineeredCssEvidence"))
    design_tokens = measured["tokens"]
    token_evidence = measured["sources"]

    if language == "zh":
        system_prompt = read_text(ASSETS / "system_prompt_zh.txt")
        user_intro = (
            f"网站：{hostname}\n"
            f"页面标题：{title}\n\n"
            f"下面是 DOM 文本与页面结构快照。请结合后续截图分析网站风格，重点是配色、字体、圆角、间距、阴影、质感、动效等设计细节，只用随附 designTokens 引用精确值，不从截图估算数值。\n"
            f"其中 distinctiveCandidates 是从真实 DOM 中挑出的模块候选，仅用于帮助你确认页面上真实存在的元素，不要为不存在的元素编造描述：\n"
            f"{json.dumps({'meta': meta, 'domSnapshot': dom_snapshot, 'designTokens': design_tokens, 'tokenEvidence': token_evidence}, ensure_ascii=False, indent=2)}"
        )
        trailing = "请按 system prompt 的八个英文 H2 标题输出中文正文。不包含 frontmatter、固定文案、CSS Evidence、代码块或下载说明。未知信息明确标注。"
    else:
        system_prompt = read_text(ASSETS / "system_prompt_en.txt")
        user_intro = (
            f"Website: {hostname}\n"
            f"Page title: {title}\n\n"
            f"Here is a DOM text and structure snapshot. Analyze the site style with the screenshots below, "
            f"focusing on design details (color, typography, radius, spacing, shadow, texture, motion). Use the supplied designTokens for exact values; never estimate measurements from screenshots.\n"
            f"The distinctiveCandidates field contains real DOM-derived module candidates. Use it only to confirm "
            f"which elements actually exist on the page; never invent elements that are not there:\n"
            f"{json.dumps({'meta': meta, 'domSnapshot': dom_snapshot, 'designTokens': design_tokens, 'tokenEvidence': token_evidence}, ensure_ascii=False, indent=2)}"
        )
        trailing = "Output the eight English H2 sections from the system prompt. No frontmatter, fixed copy, CSS Evidence, code fences or download instructions. Mark unknowns explicitly."

    user_content: list = [{"type": "text", "text": user_intro}]
    for shot in screenshots:
        user_content.append(
            {"type": "image_url", "image_url": {"url": encode_image_to_data_url(shot)}}
        )
    user_content.append({"type": "text", "text": trailing})

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]


# ── 调用 LLM ─────────────────────────────────────────────────────


def call_llm(messages: list, *, api_key: str, base_url: str, model: str, timeout: int = 600) -> str:
    url = f"{base_url.rstrip('/')}/chat/completions"
    payload = {"model": model, "stream": False, "messages": messages}
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        raise RuntimeError(f"LLM HTTP {e.code}: {err_body[:1000]}") from e

    parsed = json.loads(body)
    try:
        choice = parsed["choices"][0]
        msg = choice.get("message") or {}
        content = msg.get("content")
        if isinstance(content, list):
            # Anthropic-style content blocks
            text_parts = [
                blk.get("text", "")
                for blk in content
                if isinstance(blk, dict) and blk.get("type") in ("text", "output_text")
            ]
            return "".join(text_parts).strip()
        if isinstance(content, str):
            return content.strip()
        return (choice.get("text") or "").strip()
    except (KeyError, IndexError, TypeError) as e:
        raise RuntimeError(f"无法从 LLM 响应中提取内容: {body[:500]}") from e


# ── 主流程 ───────────────────────────────────────────────────────


def assemble_design_md(
    *, hostname: str, ai_analysis: str, css_evidence_md: str, design_tokens=None, token_evidence=None
) -> str:
    safe_ai_analysis = strip_markdown_fence(ai_analysis)
    validate_design_body(safe_ai_analysis)
    provenance = "\n".join("- " + name + ": " + json.dumps(source, ensure_ascii=False)
                           for name, source in (token_evidence or {}).items())
    appendix = re.sub(r"^(#{2,5}) ", r"#\1 ", (css_evidence_md or "").strip(), flags=re.M)
    parts = [
        build_frontmatter(hostname, design_tokens),
        read_text(ASSETS / "design_thinking.md"),
        safe_ai_analysis,
        read_text(ASSETS / "core_principles.md"),
        "### Token evidence\n" + provenance if provenance else "",
        "## Evidence Appendix\n\nSampling summary only. Heuristic roles and flattened color summaries below are not normative tokens; retain the exact values and alpha in the frontmatter. This snapshot does not establish unobserved states, themes or viewport behavior.\n\n" + appendix if appendix else "",
    ]
    return "\n\n".join(p for p in parts if p)


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate DESIGN.md from collected web data.")
    parser.add_argument("--collected", required=True, type=Path,
                        help="JSON file containing meta + domSnapshot + engineeredCssEvidence")
    parser.add_argument("--screenshots", nargs="+", required=True, type=Path,
                        help="Up to 3 screenshot image files (top / mid / lower)")
    parser.add_argument("--hostname", default="", help="Hostname; defaults to value in meta")
    parser.add_argument(
        "--output-dir", default=None, type=Path, dest="output_dir",
        help="Output directory. Defaults to ./output/<hostname>/. "
             "design.md and copies of the screenshots are written here."
    )
    parser.add_argument(
        "--output", default=None, type=Path,
        help="Explicit design.md path. Overrides --output-dir for the markdown only. "
             "Defaults to <output-dir>/design.md."
    )
    parser.add_argument("--language", choices=["zh", "en"], default="en")
    parser.add_argument("--api-key", default=os.environ.get("WEB_DESIGN_API_KEY", ""))
    parser.add_argument("--base-url", default=os.environ.get("WEB_DESIGN_BASE_URL", ""))
    parser.add_argument("--model", default=os.environ.get("WEB_DESIGN_MODEL", ""))
    args = parser.parse_args()

    if not args.api_key or not args.base_url or not args.model:
        sys.stderr.write(
            "[ERROR] Missing LLM config. Set --api-key / --base-url / --model "
            "or env WEB_DESIGN_API_KEY / WEB_DESIGN_BASE_URL / WEB_DESIGN_MODEL.\n"
        )
        return 2

    if not args.collected.exists():
        sys.stderr.write(f"[ERROR] collected file not found: {args.collected}\n")
        return 2
    for shot in args.screenshots:
        if not shot.exists():
            sys.stderr.write(f"[ERROR] screenshot not found: {shot}\n")
            return 2

    collected = json.loads(args.collected.read_text(encoding="utf-8"))
    hostname = args.hostname or (collected.get("meta") or {}).get("hostname") or "unknown"

    output_dir = args.output_dir or Path("output") / hostname
    output_dir.mkdir(parents=True, exist_ok=True)

    output_path = args.output or (output_dir / "design.md")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    args.output = output_path

    copied: list[Path] = []
    for idx, shot in enumerate(args.screenshots, start=1):
        src = shot.resolve()
        dst = (output_dir / f"shot{idx}{shot.suffix.lower() or '.jpg'}").resolve()
        if src == dst:
            copied.append(dst)
            continue
        try:
            shutil.copy2(src, dst)
            copied.append(dst)
        except OSError as e:
            sys.stderr.write(f"[WARN] failed to copy screenshot {src} -> {dst}: {e}\n")
    if copied:
        print(f"[INFO] Screenshots placed in {output_dir} ({len(copied)} files)",
              file=sys.stderr)

    raw_evidence = collected.get("engineeredCssEvidence") or {
        "error": "engineeredCssEvidence missing from collected data",
        "diagnostics": ["engineeredCssEvidence missing from collected data"],
    }
    normalized = normalize_css_evidence(raw_evidence)
    css_md = format_css_evidence_markdown(normalized, language=args.language)

    print(f"[INFO] Calling LLM model={args.model} screenshots={len(args.screenshots)}",
          file=sys.stderr)
    messages = build_messages(collected, args.screenshots, args.language)
    ai_analysis = call_llm(
        messages,
        api_key=args.api_key,
        base_url=args.base_url,
        model=args.model,
    )
    if not ai_analysis:
        sys.stderr.write("[WARN] LLM returned empty content.\n")

    final_md = assemble_design_md(
        hostname=hostname,
        ai_analysis=ai_analysis,
        css_evidence_md=css_md,
        design_tokens=extract_design_tokens(raw_evidence)["tokens"],
        token_evidence=extract_design_tokens(raw_evidence)["sources"],
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(final_md, encoding="utf-8")
    print(f"[OK] Wrote {args.output} ({len(final_md)} chars)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
