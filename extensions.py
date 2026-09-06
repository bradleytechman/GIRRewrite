import json
import os
from pathlib import Path

# The upstream bot was built for r/Jailbreak. Start with the general moderation,
# utility, logging, XP, and self-service features that make sense in any Discord
# server. The original jailbreak/news integrations remain available explicitly.
default_extensions = [
    "cogs.commands.info.stats",
    "cogs.commands.info.help",
    "cogs.commands.info.tags",
    "cogs.commands.info.userinfo",
    "cogs.commands.misc.admin",
    "cogs.commands.misc.giveaway",
    "cogs.commands.misc.misc",
    "cogs.commands.misc.timezones",
    "cogs.commands.mod.antiraid",
    "cogs.commands.mod.filter",
    "cogs.commands.mod.modactions",
    "cogs.commands.mod.modutils",
    "cogs.monitors.misc.boosteremojis",
    "cogs.monitors.misc.fixsocials",
    "cogs.monitors.mod.antiraid",
    "cogs.monitors.mod.logging",
    "cogs.monitors.mod.filter",
    "cogs.monitors.utils.birthday",
    "cogs.monitors.utils.xp",
]

feature_file = Path(os.environ.get(
    "GIR_FEATURE_FILE", str(Path.home() / "Library/Application Support/SowensServer/GIRRuntime/dashboard/data/features.json")))
try:
    enabled_extensions = set(json.loads(feature_file.read_text()).get("enabled", []))
    initial_extensions = [name for name in default_extensions if name in enabled_extensions]
except (OSError, ValueError, TypeError):
    initial_extensions = list(default_extensions)

if os.environ.get("GIR_LEGACY_JAILBREAK_FEATURES") == "True":
    initial_extensions += [
        "cogs.commands.info.devices",
        "cogs.commands.misc.canister",
        "cogs.commands.misc.genius_submod",
        "cogs.commands.misc.ioscfw",
        "cogs.commands.misc.memes",
        "cogs.monitors.misc.songs",
        "cogs.monitors.mod.sabbath",
        "cogs.monitors.mod.unban_appeals",
        "cogs.monitors.utils.applenews",
        "cogs.monitors.utils.jailbreak_monitors",
    ]
