#!/usr/bin/env python3
"""
Pull fully-qualified emoji list from official Unicode data
and store the corresponding iOS / Apple emoji images as base64 in emojis.json.

Unicode note (from their charts):
  "While these charts use a particular version of the Unicode Emoji data files,
   the images and format may be updated at any time.
   For any production usage, consult those data files."

This script therefore uses the official data file:
  https://unicode.org/Public/emoji/latest/emoji-test.txt

Apple/iOS images are not hosted by Unicode. They are pulled from a maintained
public mirror of the Apple Color Emoji set (RealityRipple/emoji on jsDelivr).
On macOS you can alternatively extract directly from:
  /System/Library/Fonts/Apple Color Emoji.ttc
"""

import asyncio
import base64
import json
import re
from pathlib import Path

import aiohttp

UNICODE_TEST_URL = "https://unicode.org/Public/emoji/latest/emoji-test.txt"
APPLE_CDN = "https://cdn.jsdelivr.net/gh/RealityRipple/emoji/apple/{code}.png"
APPLE_FALLBACK_CDN = "https://cdn.jsdelivr.net/npm/emoji-datasource-apple@16.0.0/img/apple/64/{code}.png"

JSON_PATH = Path("emojis.json")


def codepoints_to_filename(codepoints: list[str]) -> str:
    """Turn ['1F600'] or ['1F1E6', '1F1E8'] into '1f600' / '1f1e6-1f1e8'."""
    return "-".join(cp.lower() for cp in codepoints)


async def fetch_text(session: aiohttp.ClientSession, url: str) -> str:
    async with session.get(url) as resp:
        resp.raise_for_status()
        return await resp.text()


def parse_emoji_test(text: str) -> list[dict]:
    """Parse official emoji-test.txt → list of fully-qualified emoji."""
    emoji_list = []
    current_group = None
    current_subgroup = None

    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            if line.startswith("# group:"):
                current_group = line.split(":", 1)[1].strip()
            elif line.startswith("# subgroup:"):
                current_subgroup = line.split(":", 1)[1].strip()
            continue

        # format: codepoints ; status # emoji name
        if ";" not in line:
            continue

        left, right = line.split(";", 1)
        codepoints = left.strip().split()
        status_part = right.split("#", 1)[0].strip()
        comment = right.split("#", 1)[1].strip() if "#" in right else ""

        if status_part != "fully-qualified":
            continue

        # comment looks like: 😀 E1.0 grinning face
        name_match = re.search(r"E\d+(?:\.\d+)?\s+(.+)$", comment)
        name = name_match.group(1).strip() if name_match else comment

        emoji_list.append({
            "codepoints": codepoints,
            "filename": codepoints_to_filename(codepoints),
            "emoji": "".join(chr(int(cp, 16)) for cp in codepoints),
            "name": name,
            "group": current_group,
            "subgroup": current_subgroup,
        })

    return emoji_list


async def download_apple_b64(session: aiohttp.ClientSession, emoji: dict) -> tuple[str | None, str]:
    """Returns (base64_or_None, source_or_error). Tries primary CDN then fallback."""
    code = emoji["filename"]
    sources = [
        (APPLE_CDN.format(code=code), "primary"),
        (APPLE_FALLBACK_CDN.format(code=code), "fallback"),
    ]

    for url, source in sources:
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    if data:
                        return base64.b64encode(data).decode("ascii"), source
        except (asyncio.TimeoutError, Exception):
            continue

    return None, "not found"


async def main():
    async with aiohttp.ClientSession() as session:
        print("Fetching official Unicode emoji-test.txt …")
        text = await fetch_text(session, UNICODE_TEST_URL)
        emojis = parse_emoji_test(text)
        print(f"Found {len(emojis)} fully-qualified emoji")

        print("Downloading iOS / Apple PNGs …")
        sem = asyncio.Semaphore(48)

        async def limited_download(e):
            async with sem:
                b64, source = await download_apple_b64(session, e)
                return e, b64, source

        results = await asyncio.gather(*(limited_download(e) for e in emojis))

        success = 0
        fallback_used = 0
        failures = []
        output = {}
        for e, b64, source in results:
            if b64:
                success += 1
                output[e["emoji"]] = b64
                if source == "fallback":
                    fallback_used += 1
            else:
                failures.append({
                    "emoji": e["emoji"],
                    "name": e["name"],
                    "filename": e["filename"],
                    "reason": source,
                })

        print(f"Downloaded {success}/{len(emojis)} Apple PNGs ({fallback_used} via fallback)")

        if failures:
            print(f"\nFailed ({len(failures)}):")
            for f in failures:
                print(f"  {f['emoji']}  {f['filename']:30}  {f['name']}  ({f['reason']})")
            Path("failed.json").write_text(json.dumps(failures, ensure_ascii=False, indent=2))
            print("Wrote failed.json")

        JSON_PATH.write_text(json.dumps(output, ensure_ascii=False))
        print(f"Wrote {JSON_PATH} ({len(output)} entries)")


if __name__ == "__main__":
    asyncio.run(main())
