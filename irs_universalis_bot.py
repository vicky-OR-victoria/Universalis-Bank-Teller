# irs_universalis_bot.py
# Universalis Bank Bot v3.3.1 - Kirztin

import os
import re
import json
import random
import asyncio
from pathlib import Path
from typing import Optional, List, Dict
from datetime import datetime, timedelta

import discord
from discord import ui, app_commands
from discord.ext import commands, tasks

import openai

# Thread memory storage (thread-based memory, persistent)
THREAD_MEMORY_FILE = "thread_memory.json"
THREAD_STATUS_FILE = "thread_status.json"
MAX_MEMORY_MESSAGES = 60  # total messages (user+assistant)
INACTIVITY_TIMEOUT_MINUTES = 10  # Kirztin stops replying after this many minutes of inactivity

COMPLETION_PHRASES = [
    "no thanks", "no thank you", "that's all", "thats all", "that is all",
    "nothing else", "i'm good", "im good", "all done", "we're done", "were done",
    "goodbye", "bye bye", "cya", "see ya later",
    "that will be all", "nothing more", "no more", "all set",
    "i'm done", "im done", "we are done", "that's everything", "thats everything",
    "no i'm good", "no im good", "nope that's all", "nope thats all"
]

def _ensure_thread_memory_file():
    if not Path(THREAD_MEMORY_FILE).exists():
        with open(THREAD_MEMORY_FILE, "w") as f:
            json.dump({}, f)

def _ensure_thread_status_file():
    if not Path(THREAD_STATUS_FILE).exists():
        with open(THREAD_STATUS_FILE, "w") as f:
            json.dump({}, f)

def load_thread_memory(thread_id: int) -> List[Dict]:
    
    _ensure_thread_memory_file()
    try:
        with open(THREAD_MEMORY_FILE, "r") as f:
            data = json.load(f)
        items = data.get(str(thread_id), [])
        # ensure proper format
        if isinstance(items, list):
            return items
        return []
    except Exception:
        return []

def save_thread_memory(thread_id: int, messages: List[Dict]):
    """
    Persist messages (list of dicts) for given thread id.
    Trims to MAX_MEMORY_MESSAGES (keep newest).
    """
    _ensure_thread_memory_file()
    try:
        with open(THREAD_MEMORY_FILE, "r") as f:
            data = json.load(f)
    except Exception:
        data = {}

    # trim messages to last MAX_MEMORY_MESSAGES
    trimmed = messages[-MAX_MEMORY_MESSAGES:]
    data[str(thread_id)] = trimmed
    with open(THREAD_MEMORY_FILE, "w") as f:
        json.dump(data, f, indent=2)

def load_thread_status(thread_id: int) -> Dict:
    """Load status for a thread (last_activity timestamp, completed flag)."""
    _ensure_thread_status_file()
    try:
        with open(THREAD_STATUS_FILE, "r") as f:
            data = json.load(f)
        return data.get(str(thread_id), {"last_activity": None, "completed": False})
    except Exception:
        return {"last_activity": None, "completed": False}

def save_thread_status(thread_id: int, last_activity: str, completed: bool = False):
    """Save status for a thread."""
    _ensure_thread_status_file()
    try:
        with open(THREAD_STATUS_FILE, "r") as f:
            data = json.load(f)
    except Exception:
        data = {}
    
    data[str(thread_id)] = {"last_activity": last_activity, "completed": completed}
    with open(THREAD_STATUS_FILE, "w") as f:
        json.dump(data, f, indent=2)

def is_thread_inactive(thread_id: int) -> bool:
    """Check if a thread has been inactive for more than INACTIVITY_TIMEOUT_MINUTES."""
    status = load_thread_status(thread_id)
    last_activity_str = status.get("last_activity")
    if not last_activity_str:
        return False
    try:
        last_activity = datetime.fromisoformat(last_activity_str)
        return datetime.utcnow() > last_activity + timedelta(minutes=INACTIVITY_TIMEOUT_MINUTES)
    except Exception:
        return False

def is_thread_completed(thread_id: int) -> bool:
    """Check if a thread has been marked as completed."""
    status = load_thread_status(thread_id)
    return status.get("completed", False)

def mark_thread_completed(thread_id: int):
    """Mark a thread as completed so Kirztin stops responding."""
    status = load_thread_status(thread_id)
    save_thread_status(thread_id, status.get("last_activity") or datetime.utcnow().isoformat(), completed=True)

def is_completion_message(message: str) -> bool:
    """Check if a message indicates the user is done with their request."""
    msg_lower = message.lower().strip()
    if len(msg_lower) > 100:
        return False
    for phrase in COMPLETION_PHRASES:
        if phrase in msg_lower:
            return True
    return False

async def fetch_thread_history_from_discord(message: discord.Message, limit: int = 25) -> List[Dict]:
    """
    Fetch recent messages directly from Discord (oldest_first) and convert to list of dicts.
    This is used only to seed memory the first time if a thread has no persisted memory.
    """
    msgs = []
    async for msg in message.channel.history(limit=limit, oldest_first=True):
        if msg.type.value != 0:
            continue
        role = "assistant" if msg.author.bot else "user"
        content = msg.content or ""
        # combine attachments or embeds if needed (simple)
        msgs.append({"role": role, "content": f"{msg.author.display_name}: {content}"})
    return msgs
    

# Configuration & Defaults
SETTINGS_FILE = "settings.json"
DEFAULT_SETTINGS = {
    "tax_brackets": [
        {"min": 0, "max": 50000, "rate": 10.0},
        {"min": 50000, "max": 100000, "rate": 15.0},
        {"min": 100000, "max": 500000, "rate": 20.0},
        {"min": 500000, "max": None, "rate": 25.0}
    ],
    "ceo_salary_percent": 10.0,
    "ceo_tax_brackets": [
        {"min": 0, "max": 10000, "rate": 5.0},
        {"min": 10000, "max": 50000, "rate": 10.0},
        {"min": 50000, "max": 100000, "rate": 15.0},
        {"min": 100000, "max": None, "rate": 20.0}
    ]
}

# changeable defaults
DICE_OPTIONS = [10, 12, 20, 25, 50, 100]
TELLER_NAME = "Kirztin"
THREAD_HISTORY_LIMIT = 60  # number of recent messages to include in conversation history (tweak to taste)


# Secrets from environment
DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
OPENAI_KEY = os.getenv("OPENAI_KEY")
ROLE_ID_SECRET = os.getenv("ROLE_ID")
FORUM_ID_SECRET = os.getenv("FORUM_ID")

if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_BOT_TOKEN environment variable required.")
if not OPENAI_KEY:
    raise RuntimeError("OPENAI_KEY environment variable required.")
if not ROLE_ID_SECRET:
    raise RuntimeError("ROLE_ID environment variable required.")
if not FORUM_ID_SECRET:
    raise RuntimeError("FORUM_ID environment variable required.")

try:
    BANK_MANAGER_ROLE_ID = int(ROLE_ID_SECRET)
    WATCH_FORUM_ID = int(FORUM_ID_SECRET)
except ValueError:
    raise RuntimeError("ROLE_ID and FORUM_ID must be numeric strings convertible to int.")

# Setup OpenAI (legacy usage works fine here)
openai.api_key = OPENAI_KEY


# Settings persistence
def load_settings():
    if Path(SETTINGS_FILE).exists():
        try:
            with open(SETTINGS_FILE, "r") as f:
                data = json.load(f)
            data.setdefault("tax_brackets", DEFAULT_SETTINGS["tax_brackets"])
            data.setdefault("ceo_salary_percent", DEFAULT_SETTINGS["ceo_salary_percent"])
            data.setdefault("ceo_tax_brackets", DEFAULT_SETTINGS["ceo_tax_brackets"])
            return data
        except Exception:
            return DEFAULT_SETTINGS.copy()
    return DEFAULT_SETTINGS.copy()

def save_settings(s):
    with open(SETTINGS_FILE, "w") as f:
        json.dump(s, f, indent=2)

settings = load_settings()


# Utilities
def format_money(amount: float) -> str:
    try:
        return f"${amount:,.2f}"
    except Exception:
        return f"${float(amount):,.2f}"

def create_divider() -> str:
    return "─" * 30

def calculate_progressive_tax(amount: float, brackets: list) -> tuple:
    if amount <= 0:
        return 0.0, []
    total_tax = 0.0
    breakdown = []
    sorted_brackets = sorted(brackets, key=lambda x: x["min"])
    for bracket in sorted_brackets:
        bracket_min = bracket["min"]
        bracket_max = bracket["max"] if bracket["max"] is not None else float('inf')
        rate = bracket["rate"]
        if amount <= bracket_min:
            continue
        if bracket_max == float('inf'):
            taxable_in_bracket = max(0, amount - bracket_min)
        else:
            upper = min(amount, bracket_max)
            taxable_in_bracket = max(0, upper - bracket_min)
        if taxable_in_bracket > 0:
            tax_for_bracket = taxable_in_bracket * (rate / 100)
            total_tax += tax_for_bracket
            breakdown.append({
                "min": bracket_min,
                "max": bracket_max,
                "rate": rate,
                "taxable": taxable_in_bracket,
                "tax": tax_for_bracket
            })
    return total_tax, breakdown

def roll_dice(sides: int) -> int:
    return random.randint(1, sides)

def parse_money_local(text: str) -> Optional[float]:
    if not text:
        return None
    t = text.strip().lower()
    t = t.replace('$', '').replace('uc', '')
    match = re.search(r'([0-9]+(?:[.,][0-9]+)?)(\s*[km])?', t)
    if not match:
        return None
    num_str = match.group(1).replace(',', '')
    suffix = (match.group(2) or "").strip()
    try:
        val = float(num_str)
    except Exception:
        return None
    if suffix == 'k':
        val *= 1_000
    elif suffix == 'm':
        val *= 1_000_000
    return val


# Calculator: preserved from original file (cleaned)
class CalculatorSession:
    def __init__(self, user_id: int, channel_id: int, interaction: discord.Interaction):
        self.user_id = user_id
        self.channel_id = channel_id
        self.interaction = interaction
        self.include_ceo_salary = True
        self.ceo_salary_multiplier = 100
        self.items: List[dict] = []
        self.expenses: float = 0.0
        self.dialogue = "*The bank teller greets you with a warm smile...*\n\n*\"Welcome Let's calculate your business finances. Use the buttons below to build your financial report!\"*"
        self.created_at = datetime.utcnow()
        self.timeout_minutes = 10

    def is_expired(self) -> bool:
        return datetime.utcnow() > self.created_at + timedelta(minutes=self.timeout_minutes)

class SessionManager:
    def __init__(self):
        self.sessions: Dict[int, CalculatorSession] = {}

    def create_session(self, user_id: int, channel_id: int, interaction: discord.Interaction) -> CalculatorSession:
        session = CalculatorSession(user_id, channel_id, interaction)
        self.sessions[user_id] = session
        return session

    def get_session(self, user_id: int) -> Optional[CalculatorSession]:
        session = self.sessions.get(user_id)
        if session and session.is_expired():
            self.remove_session(user_id)
            return None
        return session

    def remove_session(self, user_id: int):
        if user_id in self.sessions:
            del self.sessions[user_id]

    def get_session_by_channel(self, user_id: int, channel_id: int) -> Optional[CalculatorSession]:
        session = self.get_session(user_id)
        if session and session.channel_id == channel_id:
            return session
        return None

session_manager = SessionManager()

# UI Modals & View (mostly unchanged behavior)
class AddItemModal(ui.Modal, title="Add Item/Service"):
    item_name = ui.TextInput(label="Item/Service Name", placeholder="e.g., Apple, Cola, Repair Service", max_length=100, required=True)
    item_price = ui.TextInput(label="Price per Unit", placeholder="e.g., 25.99 or 1000", max_length=20, required=True)
    dice_type = ui.TextInput(label="Dice Type (10, 12, 20, 25, 50, or 100)", placeholder="e.g., 20", max_length=3, required=True)

    def __init__(self, session: CalculatorSession, view: 'CalculatorView'):
        super().__init__()
        self.session = session
        self.calculator_view = view

    async def on_submit(self, interaction: discord.Interaction):
        name = self.item_name.value.strip()
        if not name:
            self.session.dialogue = "*\"I didn't catch that. Please enter a name for your item or service!\"*"
            await interaction.response.edit_message(embed=self.calculator_view.create_embed(), view=self.calculator_view)
            return
        try:
            price = float(self.item_price.value.replace(',', '').replace('$', ''))
            if price <= 0:
                self.session.dialogue = "*\"The price needs to be a positive number, dear!\"*"
                await interaction.response.edit_message(embed=self.calculator_view.create_embed(), view=self.calculator_view)
                return
        except ValueError:
            self.session.dialogue = "*\"That doesn't look like a valid price. Please enter a number like 25.99!\"*"
            await interaction.response.edit_message(embed=self.calculator_view.create_embed(), view=self.calculator_view)
            return
        try:
            dice = int(self.dice_type.value.replace('d', '').replace('D', ''))
            if dice not in DICE_OPTIONS:
                self.session.dialogue = f"*\"I only have d10, d12, d20, d25, d50, and d100 dice available. Please choose one of those!\"*"
                await interaction.response.edit_message(embed=self.calculator_view.create_embed(), view=self.calculator_view)
                return
        except ValueError:
            self.session.dialogue = "*\"Please enter a valid dice number: 10, 12, 20, 25, 50, or 100!\"*"
            await interaction.response.edit_message(embed=self.calculator_view.create_embed(), view=self.calculator_view)
            return

        roll_result = roll_dice(dice)
        self.session.items.append({"name": name, "price": price, "dice": dice, "quantity": roll_result})
        self.session.dialogue = f"*The bank teller rolls the d{dice}...*\n\n🎲 **Rolled a {roll_result}!**\n\n*\"{name} sold {roll_result} units! I've added it to your ledger. Anything else?\"*"
        await interaction.response.edit_message(embed=self.calculator_view.create_embed(), view=self.calculator_view)

class SetExpensesModal(ui.Modal, title="Set Business Expenses"):
    expenses = ui.TextInput(label="Total Business Expenses", placeholder="e.g., 5000.00 or 2500", max_length=20, required=True)
    def __init__(self, session: CalculatorSession, view: 'CalculatorView'):
        super().__init__()
        self.session = session
        self.calculator_view = view
    async def on_submit(self, interaction: discord.Interaction):
        try:
            amount = float(self.expenses.value.replace(',', '').replace('$', ''))
            if amount < 0:
                self.session.dialogue = "*\"Expenses can't be negative, dear!\"*"
                await interaction.response.edit_message(embed=self.calculator_view.create_embed(), view=self.calculator_view)
                return
            self.session.expenses = amount
            self.session.dialogue = f"*The bank teller records the expenses...*\n\n*\"Got it! Business expenses set to {format_money(amount)}. What's next?\"*"
            await interaction.response.edit_message(embed=self.calculator_view.create_embed(), view=self.calculator_view)
        except ValueError:
            self.session.dialogue = "*\"That doesn't look like a valid amount. Please enter a number like 5000.00!\"*"
            await interaction.response.edit_message(embed=self.calculator_view.create_embed(), view=self.calculator_view)

class SetCEOPercentModal(ui.Modal, title="Set CEO Salary Percentage"):
    percentage = ui.TextInput(label="Percentage of CEO Salary (1-100)", placeholder="e.g., 50 or 100", max_length=3, required=True)
    def __init__(self, session: CalculatorSession, view: 'CalculatorView'):
        super().__init__()
        self.session = session
        self.calculator_view = view
    async def on_submit(self, interaction: discord.Interaction):
        try:
            percent = int(self.percentage.value.replace('%', ''))
            if percent < 1 or percent > 100:
                self.session.dialogue = "*\"Please enter a percentage between 1 and 100!\"*"
                await interaction.response.edit_message(embed=self.calculator_view.create_embed(), view=self.calculator_view)
                return
            self.session.ceo_salary_multiplier = percent
            effective = settings["ceo_salary_percent"] * (percent / 100)
            self.session.dialogue = f"*The bank teller updates the compensation form...*\n\n*\"Perfect! You'll take {percent}% of the allowed {settings['ceo_salary_percent']}% CEO salary, which is {effective:.1f}% of post-tax profit.\"*"
            await interaction.response.edit_message(embed=self.calculator_view.create_embed(), view=self.calculator_view)
        except ValueError:
            self.session.dialogue = "*\"Please enter a valid percentage number between 1 and 100!\"*"
            await interaction.response.edit_message(embed=self.calculator_view.create_embed(), view=self.calculator_view)

class CalculatorView(ui.View):
    def __init__(self, session: CalculatorSession):
        super().__init__(timeout=600)
        self.session = session

    def create_embed(self) -> discord.Embed:
        embed = discord.Embed(title="Universalis Bank - Financial Calculator",
                              description=self.session.dialogue,
                              color=discord.Color.from_rgb(0, 123, 255))
        if self.session.include_ceo_salary:
            ceo_base = settings["ceo_salary_percent"]
            effective_ceo = ceo_base * (self.session.ceo_salary_multiplier / 100)
            ceo_status = f"Yes ({self.session.ceo_salary_multiplier}% of {ceo_base}% = {effective_ceo:.1f}%)"
        else:
            ceo_status = "No"
        embed.add_field(name="CEO Salary", value=f"```{ceo_status}```", inline=True)
        embed.add_field(name="Business Expenses", value=f"```{format_money(self.session.expenses)}```", inline=True)

        if self.session.items:
            items_text = ""
            total_revenue = 0.0
            for item in self.session.items:
                revenue = item["price"] * item["quantity"]
                total_revenue += revenue
                items_text += f"**{item['name']}**\n  {format_money(item['price'])} × {item['quantity']} (d{item['dice']}) = {format_money(revenue)}\n"
            embed.add_field(name=f"Items/Services ({len(self.session.items)})", value=items_text, inline=False)
            embed.add_field(name="Total Gross Revenue", value=f"```{format_money(total_revenue)}```", inline=False)
        else:
            embed.add_field(name="Items/Services", value="*No items added yet. Click \"Add Item\" to get started!*", inline=False)

        embed.set_footer(text="Use the buttons below to build your financial report!")
        return embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.session.user_id:
            await interaction.response.send_message("*\"I'm sorry, but this calculator session belongs to someone else!\"*", ephemeral=True)
            return False
        return True

    @ui.button(label="Toggle CEO Salary", style=discord.ButtonStyle.secondary, emoji="👔", row=0)
    async def toggle_ceo(self, interaction: discord.Interaction, button: ui.Button):
        self.session.include_ceo_salary = not self.session.include_ceo_salary
        if self.session.include_ceo_salary:
            self.session.dialogue = "*The bank teller nods approvingly...*\n\n*\"CEO salary is now enabled! Would you like to adjust the percentage?\"*"
        else:
            self.session.dialogue = "*The bank teller makes a note...*\n\n*\"Understood! No CEO salary will be calculated this time.\"*"
        await interaction.response.edit_message(embed=self.create_embed(), view=self)

    @ui.button(label="Set CEO %", style=discord.ButtonStyle.secondary, emoji="💼", row=0)
    async def set_ceo_percent(self, interaction: discord.Interaction, button: ui.Button):
        if not self.session.include_ceo_salary:
            self.session.dialogue = "*\"CEO salary is currently disabled. Enable it first with the toggle button!\"*"
            await interaction.response.edit_message(embed=self.create_embed(), view=self)
            return
        modal = SetCEOPercentModal(self.session, self)
        await interaction.response.send_modal(modal)

    @ui.button(label="Add Item", style=discord.ButtonStyle.primary, emoji="📦", row=1)
    async def add_item(self, interaction: discord.Interaction, button: ui.Button):
        if len(self.session.items) >= 10:
            self.session.dialogue = "*\"Oh my, that's quite a lot! We can only handle up to 10 items at a time.\"*"
            await interaction.response.edit_message(embed=self.create_embed(), view=self)
            return
        modal = AddItemModal(self.session, self)
        await interaction.response.send_modal(modal)

    @ui.button(label="Set Expenses", style=discord.ButtonStyle.secondary, emoji="💸", row=1)
    async def set_expenses(self, interaction: discord.Interaction, button: ui.Button):
        modal = SetExpensesModal(self.session, self)
        await interaction.response.send_modal(modal)

    @ui.button(label="Clear All", style=discord.ButtonStyle.danger, emoji="🗑️", row=2)
    async def clear_all(self, interaction: discord.Interaction, button: ui.Button):
        self.session.items = []
        self.session.expenses = 0.0
        self.session.include_ceo_salary = True
        self.session.ceo_salary_multiplier = 100
        self.session.dialogue = "*The bank teller clears her ledger...*\n\n*\"All cleared! Let's start fresh. What would you like to do?\"*"
        await interaction.response.edit_message(embed=self.create_embed(), view=self)

    @ui.button(label="Calculate", style=discord.ButtonStyle.success, emoji="🧮", row=2)
    async def calculate(self, interaction: discord.Interaction, button: ui.Button):
        if not self.session.items:
            self.session.dialogue = "*\"Oh dear, you haven't added any items yet! Please add at least one item or service first.\"*"
            await interaction.response.edit_message(embed=self.create_embed(), view=self)
            return

        gross_profit = sum(item["price"] * item["quantity"] for item in self.session.items)
        result_embed = await self.generate_financial_report(gross_profit, self.session.expenses)
        result_embed.set_footer(text=f"Report generated for {interaction.user.display_name} | Universalis Bank | Here to help your business thrive")
        session_manager.remove_session(self.session.user_id)
        self.stop()
        await interaction.response.defer()
        await interaction.channel.send(embed=result_embed)

    async def generate_financial_report(self, gross_profit: float, gross_expenses: float) -> discord.Embed:
        ceo_base_rate = settings["ceo_salary_percent"]
        effective_ceo_rate = ceo_base_rate * (self.session.ceo_salary_multiplier / 100) if self.session.include_ceo_salary else 0
        business_brackets = settings["tax_brackets"]
        ceo_brackets = settings["ceo_tax_brackets"]

        net_profit = gross_profit - gross_expenses
        if net_profit <= 0:
            embed = discord.Embed(title="Universalis Bank",
                                  description="*The bank teller looks over her glasses with a gentle, sympathetic smile...*",
                                  color=discord.Color.from_rgb(220, 53, 69))
            sales_text = ""
            for item in self.session.items:
                revenue = item["price"] * item["quantity"]
                sales_text += f"{item['name']}: {item['quantity']} sold @ {format_money(item['price'])} = {format_money(revenue)}\n"
            embed.add_field(name="Sales Breakdown", value=f"```\n{sales_text}```", inline=False)
            embed.add_field(
                name="Financial Summary",
                value=(f"```\nGross Revenue:   {format_money(gross_profit):>15}\nGross Expenses:  {format_money(gross_expenses):>15}\n{create_divider()}\nNet Profit:      {format_money(net_profit):>15}\n```"),
                inline=False
            )
            embed.add_field(name="Assessment",
                            value="*\"Oh dear, it looks like your expenses exceeded your earnings this period. Don't worry though - no taxes or salary deductions apply when there's no profit. Let me know if you need any help planning for next quarter!\"*"),
            return embed

def generate_loan_notice_embed(loan: dict, requester: discord.Member) -> discord.Embed:
    pname = loan.get("player_name") or "Unknown"
    amount = float(loan.get("amount") or 0.0)
    purpose = loan.get("purpose") or "No purpose given"
    collateral = loan.get("collateral") or "None"
    e = discord.Embed(title="UNIVERSALIS BANK — Loan Request",
                      description="*A loan request has been submitted and requires manager attention.*",
                      color=discord.Color.from_rgb(220, 53, 69),
                      timestamp=datetime.utcnow())
    e.add_field(name="Requester", value=f"{pname} ({requester.display_name})", inline=False)
    e.add_field(name="Amount", value=format_money(amount), inline=True)
    e.add_field(name="Purpose", value=purpose, inline=True)
    e.add_field(name="Collateral", value=collateral, inline=False)
    e.set_footer(text=f"Teller: {TELLER_NAME} | Universalis Bank")
    return e

def generate_tax_report_embed(data: dict) -> discord.Embed:
    company_name = data.get("company_name", "Unknown Company")
    player_name = data.get("player_name", "Unknown Player")
    income = float(data.get("income") or 0.0)
    expenses = float(data.get("expenses") or 0.0)
    period = data.get("period", "Unspecified Period")
    modifiers = data.get("modifiers", "None")
    
    net_profit = income - expenses
    
    if net_profit <= 0:
        embed = discord.Embed(
            title="UNIVERSALIS BANK — Tax Report (No Tax)",
            description="*The bank teller reviews the numbers with a sympathetic smile...*",
            color=discord.Color.from_rgb(220, 53, 69),
            timestamp=datetime.utcnow()
        )
        embed.add_field(name="Company", value=company_name, inline=True)
        embed.add_field(name="Player", value=player_name, inline=True)
        embed.add_field(name="Period", value=period, inline=True)
        embed.add_field(
            name="Financial Summary",
            value=f"```\nGross Income:    {format_money(income):>15}\nGross Expenses:  {format_money(expenses):>15}\n{create_divider()}\nNet Profit:      {format_money(net_profit):>15}\n```",
            inline=False
        )
        embed.add_field(
            name="Assessment",
            value="*\"No profit this period means no tax liability. Better luck next time!\"*",
            inline=False
        )
        embed.set_footer(text=f"Teller: {TELLER_NAME} | Universalis Bank")
        return embed
    
    tax_amount, tax_breakdown = calculate_progressive_tax(net_profit, settings["tax_brackets"])
    after_tax_profit = net_profit - tax_amount
    
    embed = discord.Embed(
        title="UNIVERSALIS BANK — Tax Report",
        description="*The bank teller calculates your taxes with precision...*",
        color=discord.Color.from_rgb(0, 123, 255),
        timestamp=datetime.utcnow()
    )
    embed.add_field(name="Company", value=company_name, inline=True)
    embed.add_field(name="Player", value=player_name, inline=True)
    embed.add_field(name="Period", value=period, inline=True)
    embed.add_field(
        name="Financial Summary",
        value=f"```\nGross Income:    {format_money(income):>15}\nGross Expenses:  {format_money(expenses):>15}\n{create_divider()}\nNet Profit:      {format_money(net_profit):>15}\nTax Owed:        {format_money(tax_amount):>15}\n{create_divider()}\nAfter-Tax:       {format_money(after_tax_profit):>15}\n```",
        inline=False
    )
    if modifiers and modifiers.lower() not in ("none", "no", "n/a"):
        embed.add_field(name="Modifiers", value=modifiers, inline=False)
    embed.set_footer(text=f"Teller: {TELLER_NAME} | Universalis Bank")
    return embed

def generate_transfer_report_embed(data: dict) -> discord.Embed:
    source = data.get("source", "Unknown Source")
    destination = data.get("destination", "Unknown Destination")
    amount = float(data.get("amount") or 0.0)
    reason = data.get("reason", "No reason provided")
    
    embed = discord.Embed(
        title="UNIVERSALIS BANK — Company Fund Transfer",
        description="*The bank teller prepares your transfer document...*",
        color=discord.Color.from_rgb(40, 167, 69),
        timestamp=datetime.utcnow()
    )
    embed.add_field(name="Source", value=source, inline=True)
    embed.add_field(name="Destination", value=destination, inline=True)
    embed.add_field(name="Amount", value=format_money(amount), inline=True)
    embed.add_field(name="Reason", value=reason, inline=False)
    embed.add_field(
        name="Confirmation",
        value=f"*\"Transfer of {format_money(amount)} from {source} to {destination} has been documented. Please execute this transfer in-game and keep this record.\"*",
        inline=False
    )
    embed.set_footer(text=f"Teller: {TELLER_NAME} | Universalis Bank")
    return embed

class ThreadSession:
    def __init__(self, thread: discord.Thread, starter: Optional[discord.Member]):
        self.thread_id = thread.id
        self.thread = thread
        self.starter = starter
        self.created_at = datetime.utcnow()
        self.last_activity = datetime.utcnow()
        self.state = "INITIAL"
        self.substate = None
        self.messages = []
        self.company_data = {
            "company_name": None,
            "player_name": None,
            "income": None,
            "expenses": None,
            "period": None,
            "modifiers": None
        }
        self.transfer_data = {
            "source": None,
            "destination": None,
            "amount": None,
            "reason": None
        }
        self.loan_data = {
            "player_name": None,
            "amount": None,
            "purpose": None,
            "collateral": None
        }
    
    def touch(self):
        self.last_activity = datetime.utcnow()
    
    def is_expired(self, minutes: int = 30) -> bool:
        return datetime.utcnow() > self.last_activity + timedelta(minutes=minutes)

class ThreadManager:
    def __init__(self):
        self.sessions: Dict[int, ThreadSession] = {}
    
    def create(self, thread: discord.Thread, starter: Optional[discord.Member]) -> ThreadSession:
        session = ThreadSession(thread, starter)
        self.sessions[thread.id] = session
        return session
    
    def get(self, thread_id: int) -> Optional[ThreadSession]:
        return self.sessions.get(thread_id)
    
    def remove(self, thread_id: int):
        if thread_id in self.sessions:
            del self.sessions[thread_id]
    
    def cleanup(self):
        expired = [tid for tid, s in self.sessions.items() if s.is_expired()]
        for tid in expired:
            self.remove(tid)

thread_manager = ThreadManager()

# AI Prompt for OpenAI Kirztin
KIRZTIN_SYSTEM_PROMPT = f"""
You are {TELLER_NAME}, a female assistant built for a Discord roleplay economy, do not mention that it is Discord or that it is in roleplay, if player asks the environment, mention that you're in the National Bank of the Union of Universalis. You have two primary functions: Financial Reporting and Loan Processing. Follow the instructions below exactly.

------------------------------------------------------------
1. FINANCIAL REPORT FUNCTION
------------------------------------------------------------
When a user requests a financial report, follow this workflow:

Step 1: Ask the user for the company's name.
Step 2: Ask the user, multiple times if needed until confirmed done, what item they are selling and the name of it and at what price.
Step 3: Roll a d100 sided die for each item listed by the user and multiply the sided die to the price of each item.
Step 4: Calculate gross revenue by totalling (adding up) all items multiplied by d100. (Don't show calculation, only show solution)
Step 4: Ask the user for the company's gross expenses percentage compared to total gross revenue.

After receiving both values, ask:
"Would you like to include a CEO salary percentage deducted from the company's net profit after taxes? If yes, provide the percentage."

After collecting the information, calculate and produce a formatted financial report containing:
- Gross Revenue
- Gross Expenses
- Gross Profit
- Tax Amount
- Net Profit After Taxes
- CEO Salary Deduction (if provided)
- Final Profit After All Deductions

No need to explain the math, .

------------------------------------------------------------
2. LOAN APPLICATION FUNCTION
------------------------------------------------------------
When a user requests a loan application or asks to apply for a loan:

Step 1: Ask for:
- Desired loan amount
- Purpose of the loan
- Desired repayment term
- Any additional details required by the server

Step 2: Summarize the application in a formatted block.

Step 3: Ask the user to then ask the relevant roles for the reviewing or approving of loans. (Roles are server-defined.)

Step 4: Inform the user that the request has been submitted for review.

------------------------------------------------------------
2. TRANSFER OF COMPANY FUNDS
------------------------------------------------------------
When a user requests to transfer company funds to another company:

Step 1:
- Desired amount to transfer
- Origin of the transfer
- Recipient of the transfer
- Discord ping of the recipient

Step 2: Summarize the transfer request in a formatted block

Step 3: Ping the the Recipient

Step 4: Inform the user that the transfer request is officially concluded.

TAX RULES:
- Taxes are progressive.
- Tax brackets are adjustable depending on the user's Discord rank.
- Taxes apply to gross profit (gross revenue - gross expenses).

------------------------------------------------------------
PROGRESSIVE TAX RULES
------------------------------------------------------------
PROGRESSIVE TAX BRACKETS (Default Example):
These are the base brackets unless the server overrides them with rank-specific ones:
• 8% tax: 100,000 profit  
• 10% tax: 100,001–500,000
• 12% tax: 500,001–1,000,000 
• 13% tax: 1,000,001-10,000,000
• 15% tax on anything equal to or above 10,000,001 

CEO Income Tax Brackets
$0 - $3,000: 10.0%
$3,001 - $6,000: 15.0%
$6,001 - $12,000: 20.0%
$12,001 - $24,000: 30.0%
$24,001 - $50,000: 35.0%
$50,001+: 40.0%
CEO Salary Rate
10.0% of post-tax business profit (adjustable per calculation)

You must calculate tax by applying EACH bracket progressively, not by using a single-rate flat tax.

------------------------------------------------------------
FORMAT FOR FINANCIAL REPORTS
------------------------------------------------------------
This is the format you will be using for financial reports:

"(Player's Company Name) - Financial Report
Fiscal Report
```(Gross Revenue) - (Player input)
(Gross Expenses) - (Player input)
(Gross profit after calculating revenue minus expenses)```
Progressive Taxation
```(Include tax brackets and simple calculations here)
(Include sum of gross profit after taxes)```
CEO Salary
```(If player asked for CEO salary, this is CEO salary percentage minus gross profit after taxes)
(Total amount of CEO salary)
(Total amount of gross profit after CEO salary)```"

------------------------------------------------------------
GENERAL BEHAVIOR RULES
------------------------------------------------------------
- Dont include in your messages "Universalis Bank Teller:"
- Maintain a professional, roleplay-friendly tone unless asked otherwise.
- Never skip required steps when gathering financial or loan data.
- Always confirm user inputs before performing calculations.
- Follow Discord-friendly formatting.
- If user inputs are unclear or invalid, request clarification."""

def openai_chat_completion(messages: List[Dict], max_tokens: int = 500) -> str:
    try:
        response = openai.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            max_tokens=max_tokens
        )
        return response.choices[0].message.content
    except Exception as e:
        return f"Error: {e}"

async def ask_openai_for_intent(user_message: str) -> dict:
    prompt = f"""Parse this user message and extract intent and fields:
User: {user_message}

Return JSON with:
- intent: one of "tax", "transfer", "loan", "choice", "finish", "unknown"
- fields: dict with any of company_name, player_name, income, expenses, source, destination, amount, purpose, collateral, choice

Example: {{"intent": "tax", "fields": {{"company_name": "IronWorks", "income": 12000, "expenses": 3000}}}}
"""
    try:
        response = openai.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "system", "content": "You are a JSON parser. Return only valid JSON."}, {"role": "user", "content": prompt}],
            max_tokens=200
        )
        result_text = response.choices[0].message.content.strip()
        return json.loads(result_text)
    except Exception:
        return {"intent": "unknown", "fields": {}}

client = openai.OpenAI(api_key=OPENAI_KEY)

# Bot init + intents
intents = discord.Intents.default()
intents.message_content = True
intents.messages = True
intents.guilds = True
intents.members = True
intents.messages = True

bot = commands.Bot(command_prefix="ub!", intents=intents)


# Cleanup background task (start inside on_ready)
@tasks.loop(minutes=5.0)
async def cleanup_sessions_task():
    thread_manager.cleanup()
    # also remove expired calculator sessions
    # (session_manager is small; we can prune any expired entries)
    expired_users = [uid for uid, s in session_manager.sessions.items() if s.is_expired()]
    for uid in expired_users:
        session_manager.remove_session(uid)

@cleanup_sessions_task.before_loop
async def before_cleanup():
    await bot.wait_until_ready()


# Event handlers
@bot.event
async def on_ready():
    if not cleanup_sessions_task.is_running():
        cleanup_sessions_task.start()
    print(f"{bot.user} is ready. Watching forum id {WATCH_FORUM_ID} for thread creations.")

@bot.event
async def on_thread_create(thread: discord.Thread):
    parent = getattr(thread, "parent", None)
    if parent is None:
        return
    # Only react if this thread came from the configured forum channel
    try:
        if parent.id != WATCH_FORUM_ID:
            return
    except Exception:
        return

    starter = None
    if hasattr(thread, "owner_id") and thread.owner_id:
        try:
            starter = thread.guild.get_member(thread.owner_id) or await thread.guild.fetch_member(thread.owner_id)
        except Exception:
            starter = None

    session = thread_manager.create(thread, starter)
    
    # Initialize thread status with current timestamp
    save_thread_status(thread.id, datetime.utcnow().isoformat(), completed=False)
    
    greeting = (
        f"""🏛️ *You walk in a luxurious gilded hall. You are surrounded by people busy with their requests at the bank.. You find an available bank teller and head to her, you spot her tag which says her name: Kirztin.*
-# Note: To close this post, type in "all done". If you are gone for more than 10 minutes without closing, this post will automatically close!"""
    )
    try:
        await thread.send(greeting)
    except Exception:
        try:
            await parent.send(greeting)
        except Exception:
            pass

@bot.event
async def on_message(message: discord.Message):
    # ignore bots
    if message.author.bot:
        return

    # allow commands to work
    await bot.process_commands(message)

    # only operate on threads under watched forum
    if not isinstance(message.channel, discord.Thread):
        return
    if message.channel.parent_id != WATCH_FORUM_ID:
        return

    thread_id = message.channel.id

    # Check if thread is inactive (10 minutes without activity) or completed
    if is_thread_inactive(thread_id):
        return
    if is_thread_completed(thread_id):
        return

    # Check if this message indicates the user is done
    if is_completion_message(message.content):
        mark_thread_completed(thread_id)
        farewell = (
            f"*{TELLER_NAME} smiles warmly and nods.*\n\n"
            f"\"Thank you for visiting Universalis Bank! If you need anything else in the future, "
            f"just open a new thread. Have a wonderful day!\" 💼✨"
        )
        try:
            await message.channel.send(farewell)
        except Exception:
            pass
        return

    # build or load memory for this thread
    memory = load_thread_memory(thread_id)

    # if there is no persisted memory yet, seed it from last few Discord messages
    if not memory:
        seeded = await fetch_thread_history_from_discord(message, limit=THREAD_HISTORY_LIMIT)
        # convert seeded history into "user"/"assistant" roles properly (no names in system)
        memory = []
        for m in seeded:
            # Keep the content only (already prefixed with authorname in fetch)
            memory.append({"role": m["role"], "content": m["content"]})

    # Append incoming user message to the memory list (as user role)
    user_entry_content = f"{message.author.display_name}: {message.content}"
    memory.append({"role": "user", "content": user_entry_content})

    # Build the OpenAI messages payload: system prompt + memory
    messages_for_ai = [{"role": "system", "content": KIRZTIN_SYSTEM_PROMPT}]
    # append trimmed stored memory (we rely on memory being role/content pairs)
    for m in memory:
        # Protect against overly long single entries
        content = m.get("content", "")
        if len(content) > 3000:
            content = content[:3000] + "..."
        messages_for_ai.append({"role": m.get("role", "user"), "content": content})

    # Now call OpenAI in executor to avoid blocking
    try:
        async with message.channel.typing():
            loop = asyncio.get_running_loop()
            # call the sync wrapper in executor
            ai_reply = await loop.run_in_executor(None, openai_chat_completion, messages_for_ai, 700)

            # enforce short replies: if too long, trim to first 6 sentences
            if ai_reply and len(ai_reply) > 1200:
                sentences = re.split(r'(?<=[.!?])\s+', ai_reply)
                ai_reply = " ".join(sentences[:6]) + " ... (reply trimmed; ask for more)"

    except Exception as e:
        ai_reply = f"⚠️ Error while processing request: {e}"

    # Send the reply
    try:
        await message.channel.send(ai_reply)
    except Exception:
        try:
            await message.reply(ai_reply)
        except Exception:
            pass

    # Append assistant reply to memory and persist
    memory.append({"role": "assistant", "content": ai_reply})
    try:
        save_thread_memory(thread_id, memory)
    except Exception:
        print(f"Warning: failed to save memory for thread {thread_id}")

    # Update last activity timestamp for this thread
    try:
        save_thread_status(thread_id, datetime.utcnow().isoformat(), completed=False)
    except Exception:
        print(f"Warning: failed to save status for thread {thread_id}")
        

# Slash commands (kept and adapted)

def is_admin(interaction: discord.Interaction) -> bool:
    if not interaction.guild:
        return False
    if isinstance(interaction.user, discord.Member):
        return interaction.user.guild_permissions.administrator
    return False

@bot.tree.command(name="calculate", description="Open the interactive financial calculator")
async def calculate(interaction: discord.Interaction):
    existing_session = session_manager.get_session(interaction.user.id)
    if existing_session:
        session_manager.remove_session(interaction.user.id)
    session = session_manager.create_session(interaction.user.id, interaction.channel.id, interaction)
    view = CalculatorView(session)
    await interaction.response.send_message(embed=view.create_embed(), view=view, ephemeral=True)

@bot.tree.command(name="view_rates", description="View the current tax brackets and CEO salary rates")
async def view_rates(interaction: discord.Interaction):
    business_brackets = settings["tax_brackets"]
    ceo_brackets = settings["ceo_tax_brackets"]
    ceo_rate = settings["ceo_salary_percent"]
    embed = discord.Embed(title="Universalis Bank - Tax Rate Schedule",
                          description=f"*{TELLER_NAME} pulls up the current rates with a helpful smile...*\n\n*\"Here's our complete tax structure!\"*",
                          color=discord.Color.from_rgb(0, 123, 255))
    business_text = ""
    sorted_business = sorted(business_brackets, key=lambda x: x["min"])
    for bracket in sorted_business:
        bracket_range = format_bracket_range(bracket["min"], bracket["max"])
        business_text += f"{bracket_range}: {bracket['rate']}%\n"
    embed.add_field(name="Business Income Tax Brackets", value=f"```\n{business_text}```", inline=False)
    ceo_text = ""
    sorted_ceo = sorted(ceo_brackets, key=lambda x: x["min"])
    for bracket in sorted_ceo:
        bracket_range = format_bracket_range(bracket["min"], bracket["max"])
        ceo_text += f"{bracket_range}: {bracket['rate']}%\n"
    embed.add_field(name="CEO Income Tax Brackets", value=f"```\n{ceo_text}```", inline=False)
    embed.add_field(name="CEO Salary Rate", value=f"```\n{ceo_rate}% of post-tax business profit (adjustable per calculation)\n```", inline=False)
    embed.set_footer(text="Use /calculate to run your private calculator!")
    await interaction.response.send_message(embed=embed, ephemeral=True)


# For example:
@bot.tree.command(name="set_bracket", description="[ADMIN] Set or update a business tax bracket")
@app_commands.describe(bracket_min="Minimum amount for this bracket (e.g., 0, 50000, 100000)", bracket_max="Maximum amount for this bracket (leave empty for unlimited)", rate="Tax rate percentage for this bracket (0-100)")
@app_commands.default_permissions(administrator=True)
async def set_bracket(interaction: discord.Interaction, bracket_min: float, rate: float, bracket_max: Optional[float] = None):
    if not is_admin(interaction):
        embed = discord.Embed(title="Access Restricted", description="*Only administrators may adjust tax brackets.*", color=discord.Color.from_rgb(220, 53, 69))
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    if rate < 0 or rate > 100 or bracket_min < 0 or (bracket_max is not None and bracket_max <= bracket_min):
        embed = discord.Embed(title="Invalid Entry", description="*Invalid bracket values provided.*", color=discord.Color.from_rgb(255, 193, 7))
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    brackets = settings["tax_brackets"]
    existing_idx = next((i for i, b in enumerate(brackets) if b["min"] == bracket_min), None)
    new_bracket = {"min": bracket_min, "max": bracket_max, "rate": rate}
    action = "Added"
    old_info = ""
    if existing_idx is not None:
        old_bracket = brackets[existing_idx]
        brackets[existing_idx] = new_bracket
        action = "Updated"
        old_info = f"\n\nPrevious: {format_bracket_range(old_bracket['min'], old_bracket['max'])} @ {old_bracket['rate']}%"
    else:
        brackets.append(new_bracket)
    settings["tax_brackets"] = brackets
    save_settings(settings)
    bracket_range = format_bracket_range(bracket_min, bracket_max)
    embed = discord.Embed(title=f"Business Tax Bracket {action}", description="*The bank teller updates the tax schedule...*", color=discord.Color.from_rgb(40, 167, 69))
    embed.add_field(name="New Bracket Details", value=f"```\nBracket: {bracket_range}\nRate: {rate}%\n```{old_info}", inline=False)
    embed.add_field(name="Confirmation", value=f"*\"Perfect! I've {action.lower()} the {bracket_range} bracket at {rate}%.\"*", inline=False)
    embed.set_footer(text=f"Authorized by {interaction.user.display_name}")
    await interaction.response.send_message(embed=embed)

# (Repeat similarly for remove_bracket, set_ceo_bracket, remove_ceo_bracket, set_ceo_salary)
# For completeness, paste the remaining admin commands from your original file if you wish.


# Helper formatting function small
def format_bracket_range(min_val: float, max_val) -> str:
    if max_val is None or max_val == float('inf'):
        return f"${min_val:,.0f}+"
    return f"${min_val:,.0f} - ${max_val:,.0f}"

# Run the bot
async def sync_commands():
    try:
        await bot.tree.sync()
        print("Slash commands synced successfully!")
    except Exception as e:
        print(f"Failed to sync commands: {e}")

# Run the bot
if __name__ == "__main__":
    print("Starting Universalis Bank Bot v3.0 (Kirztin)...")
    bot.run(DISCORD_TOKEN)
