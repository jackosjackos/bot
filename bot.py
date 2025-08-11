import os
import json
import logging
from typing import Any, Dict, List, Optional

import discord
from discord.ext import commands
from openai import OpenAI

# ========= Config =========
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
# Comma-separated channel IDs that the bot should listen in, e.g. "123,456"
WATCH_CHANNEL_IDS = {
    int(x.strip()) for x in os.getenv("WATCH_CHANNEL_IDS", "").split(",") if x.strip().isdigit()
}

if not DISCORD_TOKEN:
    raise SystemExit("Missing DISCORD_TOKEN")
if not OPENAI_API_KEY:
    raise SystemExit("Missing OPENAI_API_KEY")
if not WATCH_CHANNEL_IDS:
    print("WARNING: No WATCH_CHANNEL_IDS set; the bot will not respond to any channel.")

# ========= Discord setup =========
intents = discord.Intents.default()
intents.message_content = True  # required to read text content
bot = commands.Bot(command_prefix="!", intents=intents)

# ========= OpenAI client =========
client = OpenAI(api_key=OPENAI_API_KEY)

SYSTEM_PROMPT = (
    "You are a precise nutrition assistant for calorie and macro calculations. "
    "Given a user message that may contain foods/quantities (e.g., '180g chicken breast and 120g broccoli') "
    "or body stats/goals (e.g., weight, body fat %, goal date), respond with structured JSON only. "
    "If foods are given, estimate calories and macros using common nutrition references; "
    "state any assumptions (brand defaults, cooked/raw, missing weights). "
    "If a daily macro plan is implied (goal/weight/activity), propose targets with:\n"
    "• Protein ≈ 1.6–2.2 g/kg bodyweight (adjust if on a cut/deficit)\n"
    "• Fat at least ~0.6 g/kg, remainder carbs\n"
    "• If goal is fat loss, set a modest deficit (~300–500 kcal/day) unless a different, explicit target is given.\n"
    "Be conservative with claims and do not provide medical advice."
)

# Strict JSON schema for the Responses API
JSON_SCHEMA: Dict[str, Any] = {
    "name": "nutrition_output",
    "schema": {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": ["food_log", "macro_plan"]},

            "items": {
                "type": "array",
                "description": "Per-food breakdown when the user lists foods.",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "quantity": {"type": "string", "description": "Human-friendly amount, e.g., '180 g' or '1 slice'"},
                        "calories_kcal": {"type": "number"},
                        "protein_g": {"type": "number"},
                        "carbs_g": {"type": "number"},
                        "fat_g": {"type": "number"}
                    },
                    "required": ["name", "calories_kcal"]
                }
            },

            "totals": {
                "type": "object",
                "description": "Totals across items if kind == 'food_log'.",
                "properties": {
                    "calories_kcal": {"type": "number"},
                    "protein_g": {"type": "number"},
                    "carbs_g": {"type": "number"},
                    "fat_g": {"type": "number"}
                }
            },

            "plan": {
                "type": "object",
                "description": "Daily macro targets if kind == 'macro_plan'.",
                "properties": {
                    "calories_kcal": {"type": "number"},
                    "protein_g": {"type": "number"},
                    "carbs_g": {"type": "number"},
                    "fat_g": {"type": "number"},
                    "notes": {"type": "string"}
                }
            },

            "assumptions": {"type": "string"}
        },
        "required": ["kind"]
    },
    "strict": True
}

def call_openai_for_nutrition(user_text: str) -> Dict[str, Any]:
    resp = client.responses.create(
        model="gpt-4o-mini",
        instructions=SYSTEM_PROMPT,
        response_format={"type": "json_schema", "json_schema": JSON_SCHEMA},
        input=user_text,
    )
    raw_text = resp.output_text
    return json.loads(raw_text)

def build_embed_from_payload(payload: Dict[str, Any], author: discord.Member) -> discord.Embed:
    kind = payload.get("kind")
    assumptions = payload.get("assumptions")
    embed = discord.Embed(color=discord.Color.blurple())

    if kind == "food_log":
        embed.title = "Calories & Macros (Food Log)"
        totals = payload.get("totals") or {}
        embed.add_field(
            name="Totals",
            value=(
                f"**Calories:** {totals.get('calories_kcal', 0):.0f} kcal\n"
                f"**Protein:** {totals.get('protein_g', 0):.1f} g  "
                f"**Carbs:** {totals.get('carbs_g', 0):.1f} g  "
                f"**Fat:** {totals.get('fat_g', 0):.1f} g"
            ),
            inline=False,
        )
        items: List[Dict[str, Any]] = payload.get("items") or []
        if items:
            lines = []
            for it in items[:20]:
                q = f" — {it.get('quantity')}" if it.get("quantity") else ""
                lines.append(
                    f"• **{it.get('name','?')}**{q}: "
                    f"{it.get('calories_kcal',0):.0f} kcal | "
                    f"P {it.get('protein_g',0):.1f} • C {it.get('carbs_g',0):.1f} • F {it.get('fat_g',0):.1f}"
                )
            embed.add_field(name="Items", value="\n".join(lines), inline=False)

    elif kind == "macro_plan":
        embed.title = "Daily Macro Targets"
        plan = payload.get("plan") or {}
        embed.add_field(
            name="Targets",
            value=(
                f"**Calories:** {plan.get('calories_kcal', 0):.0f} kcal/day\n"
                f"**Protein:** {plan.get('protein_g', 0):.0f} g  "
                f"**Carbs:** {plan.get('carbs_g', 0):.0f} g  "
                f"**Fat:** {plan.get('fat_g', 0):.0f} g"
            ),
            inline=False,
        )
        if plan.get("notes"):
            embed.add_field(name="Notes", value=plan["notes"], inline=False)

    else:
        embed.title = "Nutrition Result"
        embed.description = "I couldn't determine whether this was a food log or macro plan."

    if assumptions:
        embed.set_footer(text=f"Assumptions: {assumptions[:1900]}")

    embed.set_author(name=str(author), icon_url=getattr(author.avatar, "url", discord.Embed.Empty))
    return embed

@bot.event
async def on_ready():
    logging.info(f"Logged in as {bot.user} (id={bot.user.id})")
    if WATCH_CHANNEL_IDS:
        logging.info(f"Watching channel IDs: {', '.join(map(str, WATCH_CHANNEL_IDS))}")

@bot.event
async def on_message(message: discord.Message):
    # ignore ourselves and other bots
    if message.author.bot:
        return
    # only act in the configured channels
    if WATCH_CHANNEL_IDS and message.channel.id not in WATCH_CHANNEL_IDS:
        return

    # basic guard: skip empty messages
    content = (message.content or "").strip()
    if not content:
        return

    try:
        async with message.channel.typing():
            payload = await bot.loop.run_in_executor(None, call_openai_for_nutrition, content)
            embed = build_embed_from_payload(payload, message.author)
        await message.reply(embed=embed, mention_author=False)

    except json.JSONDecodeError:
        try:
            async with message.channel.typing():
                def _fallback_call(txt: str) -> str:
                    r = client.responses.create(
                        model="gpt-4o-mini",
                        instructions=SYSTEM_PROMPT,
                        input=txt,
                    )
                    return r.output_text
                raw = await bot.loop.run_in_executor(None, _fallback_call, content)
            await message.reply(raw[:1900], mention_author=False)
        except Exception:
            logging.exception("OpenAI fallback failed")
            await message.reply("Sorry — I couldn't process that just now.", mention_author=False)


    except Exception as e:
        logging.exception("OpenAI call failed")
        await message.reply("Sorry — I couldn't process that just now.", mention_author=False)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    bot.run(DISCORD_TOKEN)
