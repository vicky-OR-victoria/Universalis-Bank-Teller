# irs_universalis_bot.py (v3.0) - Thread-based Kirztin AI Teller + existing calculator features
import discord
from discord import app_commands, ui
from discord.ext import commands, tasks
import json
import os
import random
import re
from pathlib import Path
from typing import Optional, List, Dict
from datetime import datetime, timedelta

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

# Kirztin + Bank Manager Role ID (user provided)
TELLER_NAME = "Kirztin"
BANK_MANAGER_ROLE_ID = 1382117937267347466  # provided by user

DICE_OPTIONS = [10, 12, 20, 25, 50, 100]

def load_settings():
    if Path(SETTINGS_FILE).exists():
        try:
            with open(SETTINGS_FILE, "r") as f:
                data = json.load(f)
                if "tax_brackets" not in data:
                    data["tax_brackets"] = DEFAULT_SETTINGS["tax_brackets"]
                if "ceo_tax_brackets" not in data:
                    data["ceo_tax_brackets"] = DEFAULT_SETTINGS["ceo_tax_brackets"]
                if "ceo_salary_percent" not in data:
                    data["ceo_salary_percent"] = DEFAULT_SETTINGS["ceo_salary_percent"]
                return data
        except (json.JSONDecodeError, IOError):
            print("Warning: Could not read settings file. Using defaults.")
            return DEFAULT_SETTINGS.copy()
    return DEFAULT_SETTINGS.copy()

def save_settings(settings):
    with open(SETTINGS_FILE, "w") as f:
        json.dump(settings, f, indent=2)

settings = load_settings()

def is_admin(interaction: discord.Interaction) -> bool:
    if not interaction.guild:
        return False
    if isinstance(interaction.user, discord.Member):
        return interaction.user.guild_permissions.administrator
    return False

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

def format_bracket_range(min_val: float, max_val) -> str:
    if max_val is None or max_val == float('inf'):
        return f"${min_val:,.0f}+"
    return f"${min_val:,.0f} - ${max_val:,.0f}"

def format_money(amount: float) -> str:
    return f"${amount:,.2f}"

def create_divider() -> str:
    return "─" * 30

def roll_dice(sides: int) -> int:
    return random.randint(1, sides)


class ThreadSession:
    """
    Manages a conversational session inside a forum thread.
    Sessions are keyed by thread.id and track a simple state machine.
    """
    def __init__(self, thread: discord.Thread, author: discord.Member):
        self.thread = thread
        self.thread_id = thread.id
        self.author = author
        self.created_at = datetime.utcnow()
        self.last_activity = datetime.utcnow()
        self.timeout_minutes = 30  # session timeout
        self.state = "AWAITING_CHOICE"  # other states: COMPANY_MENU, TAX_COLLECTING, TRANSFER_COLLECTING, LOAN_COLLECTING, FINISHED
        self.substate = None  # to track steps within a flow
        # Data containers
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

    def is_expired(self) -> bool:
        return datetime.utcnow() > self.last_activity + timedelta(minutes=self.timeout_minutes)

class ThreadSessionManager:
    def __init__(self):
        self.sessions: Dict[int, ThreadSession] = {}

    def create_session(self, thread: discord.Thread, author: discord.Member) -> ThreadSession:
        session = ThreadSession(thread, author)
        self.sessions[thread.id] = session
        return session

    def get_session(self, thread_id: int) -> Optional[ThreadSession]:
        session = self.sessions.get(thread_id)
        if session and session.is_expired():
            del self.sessions[thread_id]
            return None
        return session

    def remove_session(self, thread_id: int):
        if thread_id in self.sessions:
            del self.sessions[thread_id]

    def cleanup_expired(self):
        expired = [tid for tid, s in self.sessions.items() if s.is_expired()]
        for tid in expired:
            del self.sessions[tid]

thread_manager = ThreadSessionManager()

def parse_money(text: str) -> Optional[float]:
    """
    Parse common money formats like "5k", "2,500", "$3,200.50", "1200"
    Returns float or None if cannot parse.
    """
    if not text:
        return None
    text = text.strip().lower()
    # Replace currency symbols
    text = text.replace('$', '').replace('uc', '').strip()
    # Shorthand: 2k, 1.5m
    match = re.match(r'^([0-9,.]*\d)(\s*[km])?$', text)
    if match:
        num_str = match.group(1).replace(',', '')
        suffix = match.group(2)
        try:
            val = float(num_str)
            if suffix:
                suffix = suffix.strip()
                if suffix == 'k':
                    val *= 1_000
                elif suffix == 'm':
                    val *= 1_000_000
            return val
        except ValueError:
            return None
    # Try to extract a number anywhere in the string
    match_any = re.search(r'([0-9][0-9,\.]*\d)', text)
    if match_any:
        try:
            return float(match_any.group(1).replace(',', ''))
        except ValueError:
            return None
    return None

def parse_choice(text: str) -> str:
    """
    Return a normalized choice token from text.
    """
    t = text.strip().lower()
    if t in ("a", "a)", "company", "company services", "company service", "company transaction", "company transactions", "services"):
        return "A"
    if t in ("b", "b)", "loan", "loan request", "request loan", "loans"):
        return "B"
    if "tax" in t or "calculate" in t or "taxes" in t:
        return "TAX"
    if "transfer" in t or "move" in t:
        return "TRANSFER"
    if t in ("finish", "done", "calculate", "report", "end"):
        return "FINISH"
    return ""

def parse_dice(text: str) -> Optional[int]:
    """
    Extract a dice value like d20, 20, d100 from user text.
    """
    if not text:
        return None
    m = re.search(r'd\s*([0-9]{1,3})', text.lower())
    if m:
        val = int(m.group(1))
        if val in DICE_OPTIONS:
            return val
    m2 = re.search(r'\b(' + '|'.join(str(x) for x in DICE_OPTIONS) + r')\b', text)
    if m2:
        return int(m2.group(1))
    return None

def generate_tax_report_embed(company_data: dict) -> discord.Embed:
    """
    Generate a tax report embed using the same tax logic from the original calculator,
    but using provided company_data: income, expenses, ceo salary handling not required here.
    """
    income = company_data.get("income") or 0.0
    expenses = company_data.get("expenses") or 0.0
    company_name = company_data.get("company_name") or "Unknown Company"
    player_name = company_data.get("player_name") or "Unknown"
    period = company_data.get("period") or "Period"

    gross_profit = income
    gross_expenses = expenses
    net_profit = gross_profit - gross_expenses

    embed = discord.Embed(
        title=f"UNIVERSALIS BANK — Tax Assessment Report",
        description=f"*Kirztin prepares your tax assessment for {company_name} ({player_name}) — {period}*",
        color=discord.Color.from_rgb(255, 193, 7),
        timestamp=datetime.utcnow()
    )

    embed.add_field(
        name="Overview",
        value=(
            f"**Company:** {company_name}\n"
            f"**Client:** {player_name}\n"
            f"**Period:** {period}\n"
            f"**Gross Income:** {format_money(gross_profit)}\n"
            f"**Expenses:** {format_money(gross_expenses)}\n"
        ),
        inline=False
    )

    if net_profit <= 0:
        embed.add_field(
            name="Result",
            value=(
                f"Net Profit: {format_money(net_profit)}\n\n"
                "*No business income tax applies when there is no profit.*"
            ),
            inline=False
        )
        embed.set_footer(text=f"Teller: {TELLER_NAME} | Universalis Bank")
        return embed

    business_tax, business_breakdown = calculate_progressive_tax(net_profit, settings["tax_brackets"])
    profit_after_tax = net_profit - business_tax

    business_tax_text = ""
    for item in business_breakdown:
        bracket_range = format_bracket_range(item["min"], item["max"])
        business_tax_text += f"{bracket_range} @ {item['rate']}%\n   Tax: {format_money(item['tax'])}\n"
    business_tax_text += f"\nTotal Business Tax: {format_money(business_tax)}"

    embed.add_field(
        name="Tax Calculation",
        value=f"```\nNet Profit: {format_money(net_profit)}\n\n{business_tax_text}\n```",
        inline=False
    )

    embed.add_field(
        name="After Tax",
        value=(
            f"```\n"
            f"Profit After Tax: {format_money(profit_after_tax)}\n"
            f"```"
        ),
        inline=False
    )

    embed.set_footer(text=f"Teller: {TELLER_NAME} | Universalis Bank")
    return embed

def generate_transfer_report_embed(transfer_data: dict) -> discord.Embed:
    src = transfer_data.get("source") or "Unknown"
    dst = transfer_data.get("destination") or "Unknown"
    amount = transfer_data.get("amount") or 0.0
    reason = transfer_data.get("reason") or "No reason provided"
    embed = discord.Embed(
        title="UNIVERSALIS BANK — Transfer Report",
        description=f"*Kirztin processes the transfer...*",
        color=discord.Color.from_rgb(0, 123, 255),
        timestamp=datetime.utcnow()
    )
    embed.add_field(name="Details", value=(
        f"**From:** {src}\n"
        f"**To:** {dst}\n"
        f"**Amount:** {format_money(amount)}\n"
        f"**Reason:** {reason}\n"
    ), inline=False)
    embed.add_field(name="Status", value="✔️ Completed", inline=False)
    embed.set_footer(text=f"Teller: {TELLER_NAME} | Universalis Bank")
    return embed

def generate_loan_notice_embed(loan_data: dict, requester: discord.Member) -> discord.Embed:
    player_name = loan_data.get("player_name") or "Unknown"
    amount = loan_data.get("amount") or 0.0
    purpose = loan_data.get("purpose") or "No purpose given"
    collateral = loan_data.get("collateral") or "None"
    embed = discord.Embed(
        title="UNIVERSALIS BANK — Loan Request",
        description=f"*A loan request has been submitted and requires manager attention.*",
        color=discord.Color.from_rgb(220, 53, 69),
        timestamp=datetime.utcnow()
    )
    embed.add_field(name="Requester", value=f"{player_name} ({requester.display_name})", inline=False)
    embed.add_field(name="Amount", value=format_money(amount), inline=True)
    embed.add_field(name="Purpose", value=purpose, inline=True)
    embed.add_field(name="Collateral", value=collateral, inline=False)
    embed.set_footer(text=f"Teller: {TELLER_NAME} | Universalis Bank")
    return embed

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.messages = True
intents.members = True
intents.reactions = True
intents.integrations = True
intents.dm_messages = True
intents.typing = False

bot = commands.Bot(command_prefix="!", intents=intents)

@tasks.loop(minutes=5.0)
async def cleanup_sessions():
    thread_manager.cleanup_expired()

@cleanup_sessions.before_loop
async def before_cleanup_sessions():
    await bot.wait_until_ready()

cleanup_sessions.start()

@bot.event
async def on_ready():
    print(f"{bot.user} is now open for business!")
    print(f"Connected to {len(bot.guilds)} guild(s)")
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} command(s)")
    except Exception as e:
        print(f"Failed to sync commands: {e}")

@bot.event
async def on_thread_create(thread: discord.Thread):
    # Only react to threads created in Forum channels (thread.parent.type == forum)
    try:
        parent = thread.parent
    except AttributeError:
        parent = None

    if parent is None:
        return

    # Check parent is a forum channel or thread is a public/private thread created from a forum post
    if getattr(parent, "type", None) != discord.ChannelType.forum:
        return

    # Attempt to get the user who started the thread (creator)
    starter = None
    # thread.owner_id is available on Thread objects; try to resolve Member
    if hasattr(thread, "owner_id") and thread.owner_id:
        try:
            guild = thread.guild
            starter = guild.get_member(thread.owner_id) or await guild.fetch_member(thread.owner_id)
        except Exception:
            starter = None

    # Create a ThreadSession and greet
    if starter:
        session = thread_manager.create_session(thread, starter)
    else:
        # If we cannot resolve starter, still create session with author=None
        session = thread_manager.create_session(thread, None)

    # Kirztin greeting with options
    greeting = (
        f"👋 **Welcome to Universalis Bank.**\n"
        f"I am **{TELLER_NAME}**, your virtual bank teller. How may I assist you today?\n\n"
        f"Please reply in this thread with one of the choices below:\n"
        f"**A)** Company Services — tax calculation or company transfer\n"
        f"**B)** Loan Request — request a loan (a Bank Manager will be notified)\n\n"
        f"You can reply with `A` or `B`, or write the words (e.g., 'company' or 'loan')."
    )

    try:
        await thread.send(greeting)
    except Exception:
        # fallback: try to send to parent channel
        try:
            await parent.send(greeting)
        except Exception:
            pass

@bot.event
async def on_message(message: discord.Message):
    # let commands process as well
    await bot.process_commands(message)

    # Ignore messages from bots
    if message.author.bot:
        return

    # Only handle messages in threads
    if not message.channel or not isinstance(message.channel, discord.Thread):
        return

    thread = message.channel
    session = thread_manager.get_session(thread.id)
    # If no session existed, ignore (we only trigger on thread_create)
    if not session:
        return

    # Ensure only the thread starter or an admin interacts with the session (admins can assist)
    if session.author and message.author.id != session.author.id:
        # Allow admins to interact (guild admins)
        member = message.author
        if message.guild and isinstance(member, discord.Member) and member.guild_permissions.administrator:
            pass
        else:
            # Ignore other users
            try:
                await message.reply("*Kirztin says: Please let the original requester interact with this session, or ask an admin for help.*", mention_author=False)
            except Exception:
                pass
            return

    session.touch()
    content = message.content.strip()

    # Normalize quick choices
    choice = parse_choice(content)

    # State machine
    if session.state == "AWAITING_CHOICE":
        if choice == "A":
            session.state = "COMPANY_MENU"
            await thread.send(f"*\"Excellent. Company Services it is. Would you like 'tax' (calculate taxes) or 'transfer' (company transfer)?\"*")
            return
        elif choice == "B":
            session.state = "LOAN_COLLECTING"
            session.substate = "ASK_NAME"
            await thread.send(f"*\"A loan request — understood. To begin, what's your character name?\"*")
            return
        else:
            # Try to interpret full-text choices
            if "company" in content.lower():
                session.state = "COMPANY_MENU"
                await thread.send(f"*\"Excellent. Company Services it is. Would you like 'tax' (calculate taxes) or 'transfer' (company transfer)?\"*")
                return
            if "loan" in content.lower():
                session.state = "LOAN_COLLECTING"
                session.substate = "ASK_NAME"
                await thread.send(f"*\"A loan request — understood. To begin, what's your character name?\"*")
                return
            await thread.send(f"*\"I'm sorry, I didn't quite catch that. Please reply with `A` for Company Services or `B` for Loan Request.\"*")
            return

    # Company menu
    if session.state == "COMPANY_MENU":
        if choice == "TAX":
            session.state = "TAX_COLLECTING"
            session.substate = "ASK_COMPANY"
            await thread.send(f"*\"Very well — Tax calculation. What is the company name?\"*")
            return
        elif choice == "TRANSFER":
            session.state = "TRANSFER_COLLECTING"
            session.substate = "ASK_SOURCE"
            await thread.send(f"*\"Understood — Company Transfer. Who is the source of funds? (e.g., CompanyName or PlayerName)\"*")
            return
        else:
            # Try to detect keywords
            low = content.lower()
            if "tax" in low or "calculate" in low:
                session.state = "TAX_COLLECTING"
                session.substate = "ASK_COMPANY"
                await thread.send(f"*\"Very well — Tax calculation. What is the company name?\"*")
                return
            if "transfer" in low or "move" in low:
                session.state = "TRANSFER_COLLECTING"
                session.substate = "ASK_SOURCE"
                await thread.send(f"*\"Understood — Company Transfer. Who is the source of funds? (e.g., CompanyName or PlayerName)\"*")
                return
            await thread.send(f"*\"Please specify 'tax' or 'transfer' so I know which service to perform.\"*")
            return

    # TAX collection flows
    if session.state == "TAX_COLLECTING":
        sub = session.substate
        if sub == "ASK_COMPANY":
            session.company_data["company_name"] = content.strip()
            session.substate = "ASK_PLAYER"
            await thread.send(f"*\"Recorded company name as **{session.company_data['company_name']}**. What is the character/player name?\"*")
            return
        if sub == "ASK_PLAYER":
            session.company_data["player_name"] = content.strip()
            session.substate = "ASK_INCOME"
            await thread.send(f"*\"Great. What is the gross income for the period? (e.g., 12000 or 12k)\"*")
            return
        if sub == "ASK_INCOME":
            parsed = parse_money(content)
            if parsed is None:
                await thread.send(f"*\"I couldn't parse that amount — please enter a number like 12000 or 12k (you may use 'k' or 'm').\"*")
                return
            session.company_data["income"] = parsed
            session.substate = "ASK_EXPENSES"
            await thread.send(f"*\"Income recorded: {format_money(parsed)}. What are the total expenses? (enter 0 if none)\"*")
            return
        if sub == "ASK_EXPENSES":
            parsed = parse_money(content)
            if parsed is None:
                await thread.send(f"*\"I couldn't parse that amount — please enter a number like 5000 or 5k.\"*")
                return
            session.company_data["expenses"] = parsed
            session.substate = "ASK_PERIOD"
            await thread.send(f"*\"Expenses recorded: {format_money(parsed)}. What is the fiscal period? (e.g., 'This month', 'Q3 1425')\"*")
            return
        if sub == "ASK_PERIOD":
            session.company_data["period"] = content.strip()
            session.substate = "ASK_MODIFIERS"
            await thread.send(f"*\"Any modifiers or special notes? (e.g., 'charity deduction 10%' or reply 'no')\"*")
            return
        if sub == "ASK_MODIFIERS":
            session.company_data["modifiers"] = content.strip()
            # Completed gathering data. Provide summary and instructions to 'calculate'
            summary = (
                f"**Summary so far:**\n"
                f"- Company: {session.company_data['company_name']}\n"
                f"- Player: {session.company_data['player_name']}\n"
                f"- Income: {format_money(session.company_data['income'])}\n"
                f"- Expenses: {format_money(session.company_data['expenses'])}\n"
                f"- Period: {session.company_data['period']}\n"
                f"- Modifiers: {session.company_data['modifiers']}\n\n"
                f"Type `calculate` or `finish` to get the full tax report."
            )
            session.substate = "READY"
            await thread.send(f"*\"All set. {summary}\"*")
            return

    # TRANSFER collection flows
    if session.state == "TRANSFER_COLLECTING":
        sub = session.substate
        if sub == "ASK_SOURCE":
            session.transfer_data["source"] = content.strip()
            session.substate = "ASK_DEST"
            await thread.send(f"*\"Source recorded: {session.transfer_data['source']}. Who is the destination?\"*")
            return
        if sub == "ASK_DEST":
            session.transfer_data["destination"] = content.strip()
            session.substate = "ASK_AMOUNT"
            await thread.send(f"*\"Destination recorded: {session.transfer_data['destination']}. How much would you like to transfer?\"*")
            return
        if sub == "ASK_AMOUNT":
            parsed = parse_money(content)
            if parsed is None:
                await thread.send(f"*\"I couldn't parse that amount — please enter a number like 12000 or 12k.\"*")
                return
            session.transfer_data["amount"] = parsed
            session.substate = "ASK_REASON"
            await thread.send(f"*\"Amount recorded: {format_money(parsed)}. Any reason/notes for the transfer? (or 'none')\"*")
            return
        if sub == "ASK_REASON":
            session.transfer_data["reason"] = content.strip()
            session.substate = "READY"
            await thread.send(f"*\"Transfer details recorded. Type `finish` to process and see the transfer report.*\"")
            return

    # LOAN collection flows
    if session.state == "LOAN_COLLECTING":
        sub = session.substate
        if sub == "ASK_NAME":
            session.loan_data["player_name"] = content.strip()
            session.substate = "ASK_AMOUNT"
            await thread.send(f"*\"Thanks. How much would you like to request as a loan?\"*")
            return
        if sub == "ASK_AMOUNT":
            parsed = parse_money(content)
            if parsed is None:
                await thread.send(f"*\"I couldn't parse that amount — please enter a number like 12000 or 12k.\"*")
                return
            session.loan_data["amount"] = parsed
            session.substate = "ASK_PURPOSE"
            await thread.send(f"*\"Amount noted: {format_money(parsed)}. What's the purpose of the loan?\"*")
            return
        if sub == "ASK_PURPOSE":
            session.loan_data["purpose"] = content.strip()
            session.substate = "ASK_COLLATERAL"
            await thread.send(f"*\"Any collateral to list? If none, reply 'none'.\"*")
            return
        if sub == "ASK_COLLATERAL":
            session.loan_data["collateral"] = content.strip()
            # Completed loan request
            embed = generate_loan_notice_embed(session.loan_data, message.author)
            notice = f"<@&{BANK_MANAGER_ROLE_ID}> — A new loan request requires your attention."
            await thread.send(content=notice, embed=embed)
            session.state = "FINISHED"
            return

    # READY / FINISH handling
    if session.substate == "READY" or choice == "FINISH":
        if session.state == "TAX_COLLECTING" or session.state == "COMPANY_MENU":
            # Generate tax report
            embed = generate_tax_report_embed(session.company_data)
            await thread.send(embed=embed)
            session.state = "FINISHED"
            thread_manager.remove_session(thread.id)
            return
        if session.state == "TRANSFER_COLLECTING":
            embed = generate_transfer_report_embed(session.transfer_data)
            await thread.send(embed=embed)
            session.state = "FINISHED"
            thread_manager.remove_session(thread.id)
            return

    # If we get here, message didn't match expected flow
    await thread.send(f"*\"I'm not sure how to handle that message in the current step. Please follow the prompts, or type 'finish' to end and see the report.\"*")

@bot.tree.command(name="view_rates", description="View the current tax brackets and CEO salary rates")
async def view_rates(interaction: discord.Interaction):
    business_brackets = settings["tax_brackets"]
    ceo_brackets = settings["ceo_tax_brackets"]
    ceo_rate = settings["ceo_salary_percent"]
    
    embed = discord.Embed(
        title="Universalis Bank - Tax Rate Schedule",
        description=f"*{TELLER_NAME} pulls up the current rates with a helpful smile...*\n\n*\"Here's our complete tax structure!\"*",
        color=discord.Color.from_rgb(0, 123, 255)
    )
    
    business_text = ""
    sorted_business = sorted(business_brackets, key=lambda x: x["min"])
    for bracket in sorted_business:
        bracket_range = format_bracket_range(bracket["min"], bracket["max"])
        business_text += f"{bracket_range}: {bracket['rate']}%\n"
    
    embed.add_field(
        name="Business Income Tax Brackets",
        value=f"```\n{business_text}```",
        inline=False
    )
    
    ceo_text = ""
    sorted_ceo = sorted(ceo_brackets, key=lambda x: x["min"])
    for bracket in sorted_ceo:
        bracket_range = format_bracket_range(bracket["min"], bracket["max"])
        ceo_text += f"{bracket_range}: {bracket['rate']}%\n"
    
    embed.add_field(
        name="CEO Income Tax Brackets",
        value=f"```\n{ceo_text}```",
        inline=False
    )
    
    embed.add_field(
        name="CEO Salary Rate",
        value=f"```\n{ceo_rate}% of post-tax business profit (adjustable per calculation)\n```",
        inline=False
    )
    
    embed.add_field(
        name="How Progressive Tax Works",
        value=(
            "*Each bracket only applies to income within that range.*\n\n"
            "**Example:** $75,000 income with brackets:\n"
            "- $0-$50k @ 10% and $50k-$100k @ 15%\n"
            "- First $50,000 taxed at 10% = $5,000\n"
            "- Remaining $25,000 taxed at 15% = $3,750\n"
            "- **Total tax: $8,750** (Effective rate: 11.7%)"
        ),
        inline=False
    )
    
    embed.set_footer(text="Use /calculate to run your private calculator!")
    
    await interaction.response.send_message(embed=embed)

if __name__ == "__main__":
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        print("Error: DISCORD_BOT_TOKEN not found in environment variables!")
        print("Please set your Discord bot token in the Secrets tab.")
        exit(1)
    
    print("Starting the Universalis Bank Bot v3.0...")
    bot.run(token)
