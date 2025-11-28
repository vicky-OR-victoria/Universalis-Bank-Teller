import discord
from discord import app_commands, ui
from discord.ext import commands
import json
import os
import random
from pathlib import Path
from typing import Optional, List

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

settings = load_settings()

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

class AddItemModal(ui.Modal, title="Add Item/Service"):
    item_name = ui.TextInput(
        label="Item/Service Name",
        placeholder="e.g., Premium Widget, Consulting Service",
        required=True,
        max_length=100
    )
    
    item_price = ui.TextInput(
        label="Price per Unit ($)",
        placeholder="e.g., 25.99",
        required=True,
        max_length=20
    )
    
    def __init__(self, calculator_view: 'CalculatorView'):
        super().__init__()
        self.calculator_view = calculator_view
    
    async def on_submit(self, interaction: discord.Interaction):
        try:
            price = float(self.item_price.value.replace(',', '').replace('$', ''))
            if price <= 0:
                await interaction.response.send_message(
                    "*\"Oh dear, the price needs to be a positive number!\"*",
                    ephemeral=True
                )
                return
        except ValueError:
            await interaction.response.send_message(
                "*\"Hmm, that doesn't look like a valid price. Please enter a number like 25.99\"*",
                ephemeral=True
            )
            return
        
        self.calculator_view.pending_item = {
            "name": self.item_name.value,
            "price": price,
            "dice": None,
            "quantity": None,
            "roll": None
        }
        
        await interaction.response.edit_message(
            embed=self.calculator_view.create_dice_selection_embed(),
            view=self.calculator_view
        )

class AddExpenseModal(ui.Modal, title="Add Business Expenses"):
    expense_amount = ui.TextInput(
        label="Total Expenses ($)",
        placeholder="e.g., 5000.00",
        required=True,
        max_length=20
    )
    
    def __init__(self, calculator_view: 'CalculatorView'):
        super().__init__()
        self.calculator_view = calculator_view
    
    async def on_submit(self, interaction: discord.Interaction):
        try:
            amount = float(self.expense_amount.value.replace(',', '').replace('$', ''))
            if amount < 0:
                await interaction.response.send_message(
                    "*\"Expenses can't be negative, dear!\"*",
                    ephemeral=True
                )
                return
        except ValueError:
            await interaction.response.send_message(
                "*\"That doesn't look like a valid amount. Please enter a number like 5000.00\"*",
                ephemeral=True
            )
            return
        
        self.calculator_view.expenses = amount
        await interaction.response.edit_message(
            embed=self.calculator_view.create_main_embed(),
            view=self.calculator_view
        )

class DiceSelect(ui.Select):
    def __init__(self, calculator_view: 'CalculatorView'):
        self.calculator_view = calculator_view
        options = [
            discord.SelectOption(label=f"d{dice}", value=str(dice), description=f"Roll 1-{dice} units sold")
            for dice in DICE_OPTIONS
        ]
        super().__init__(
            placeholder="Select dice type for quantity...",
            options=options,
            row=0
        )
    
    async def callback(self, interaction: discord.Interaction):
        if not self.calculator_view.pending_item:
            await interaction.response.send_message(
                "*\"Please add an item first before selecting a dice type!\"*",
                ephemeral=True
            )
            return
        
        dice = int(self.values[0])
        roll = roll_dice(dice)
        
        self.calculator_view.pending_item["dice"] = dice
        self.calculator_view.pending_item["quantity"] = roll
        self.calculator_view.pending_item["roll"] = roll
        
        self.calculator_view.items.append(self.calculator_view.pending_item)
        self.calculator_view.pending_item = None
        
        self.calculator_view.remove_item(self)
        self.calculator_view.dice_select = None
        
        await interaction.response.edit_message(
            embed=self.calculator_view.create_main_embed(),
            view=self.calculator_view
        )

class CalculatorView(ui.View):
    def __init__(self, user: discord.User):
        super().__init__(timeout=300)
        self.user = user
        self.include_ceo_salary = True
        self.items: List[dict] = []
        self.expenses: float = 0.0
        self.pending_item: Optional[dict] = None
        self.dice_select = None
        
    def create_main_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="Universalis Bank - Financial Calculator",
            description="*The bank teller greets you with a warm smile...*\n\n*\"Welcome! Let's calculate your business finances. Add your items sold below, and I'll handle all the tax calculations for you!\"*",
            color=discord.Color.from_rgb(0, 123, 255)
        )
        
        ceo_status = "Yes" if self.include_ceo_salary else "No"
        embed.add_field(
            name="CEO Salary Included",
            value=f"```{ceo_status}```",
            inline=True
        )
        
        embed.add_field(
            name="Business Expenses",
            value=f"```{format_money(self.expenses)}```",
            inline=True
        )
        
        if self.items:
            items_text = ""
            total_revenue = 0.0
            for item in self.items:
                revenue = item["price"] * item["quantity"]
                total_revenue += revenue
                items_text += f"**{item['name']}**\n"
                items_text += f"  Price: {format_money(item['price'])} × {item['quantity']} (d{item['dice']} roll)\n"
                items_text += f"  Revenue: {format_money(revenue)}\n\n"
            
            embed.add_field(
                name=f"Items/Services ({len(self.items)})",
                value=items_text,
                inline=False
            )
            
            embed.add_field(
                name="Total Gross Revenue",
                value=f"```{format_money(total_revenue)}```",
                inline=False
            )
        else:
            embed.add_field(
                name="Items/Services",
                value="*No items added yet. Click \"Add Item\" to get started!*",
                inline=False
            )
        
        embed.set_footer(text="Add items, set expenses, then click Calculate when ready!")
        return embed
    
    def create_dice_selection_embed(self) -> discord.Embed:
        if not self.pending_item:
            return self.create_main_embed()
        
        embed = discord.Embed(
            title="Universalis Bank - Roll for Quantity",
            description=f"*The bank teller prepares the dice...*\n\n*\"Now, let's see how many **{self.pending_item['name']}** units you sold! Choose a dice type below and I'll roll it for you.\"*",
            color=discord.Color.from_rgb(255, 193, 7)
        )
        
        embed.add_field(
            name="Item Details",
            value=f"```\nItem: {self.pending_item['name']}\nPrice: {format_money(self.pending_item['price'])}\n```",
            inline=False
        )
        
        dice_info = "\n".join([f"**d{d}**: Roll 1-{d} units" for d in DICE_OPTIONS])
        embed.add_field(
            name="Available Dice",
            value=dice_info,
            inline=False
        )
        
        embed.set_footer(text="Select a dice type from the dropdown below!")
        
        if self.dice_select is None:
            self.dice_select = DiceSelect(self)
            self.add_item(self.dice_select)
        
        return embed
    
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message(
                "*\"I'm sorry, but this calculator session belongs to someone else!\"*",
                ephemeral=True
            )
            return False
        return True
    
    @ui.button(label="Toggle CEO Salary", style=discord.ButtonStyle.secondary, emoji="👔", row=1)
    async def toggle_ceo(self, interaction: discord.Interaction, button: ui.Button):
        self.include_ceo_salary = not self.include_ceo_salary
        await interaction.response.edit_message(embed=self.create_main_embed(), view=self)
    
    @ui.button(label="Add Item", style=discord.ButtonStyle.primary, emoji="📦", row=1)
    async def add_item(self, interaction: discord.Interaction, button: ui.Button):
        if len(self.items) >= 10:
            await interaction.response.send_message(
                "*\"Oh my, that's quite a lot! We can only handle up to 10 items at a time.\"*",
                ephemeral=True
            )
            return
        modal = AddItemModal(self)
        await interaction.response.send_modal(modal)
    
    @ui.button(label="Set Expenses", style=discord.ButtonStyle.secondary, emoji="💸", row=1)
    async def set_expenses(self, interaction: discord.Interaction, button: ui.Button):
        modal = AddExpenseModal(self)
        await interaction.response.send_modal(modal)
    
    @ui.button(label="Clear All", style=discord.ButtonStyle.danger, emoji="🗑️", row=2)
    async def clear_all(self, interaction: discord.Interaction, button: ui.Button):
        self.items = []
        self.expenses = 0.0
        self.include_ceo_salary = True
        self.pending_item = None
        if self.dice_select:
            self.remove_item(self.dice_select)
            self.dice_select = None
        await interaction.response.edit_message(embed=self.create_main_embed(), view=self)
    
    @ui.button(label="Calculate", style=discord.ButtonStyle.success, emoji="🧮", row=2)
    async def calculate(self, interaction: discord.Interaction, button: ui.Button):
        if not self.items:
            await interaction.response.send_message(
                "*\"Oh dear, you haven't added any items yet! Please add at least one item or service first.\"*",
                ephemeral=True
            )
            return
        
        gross_profit = sum(item["price"] * item["quantity"] for item in self.items)
        
        result_embed = await self.generate_financial_report(gross_profit, self.expenses)
        
        self.stop()
        await interaction.response.edit_message(embed=result_embed, view=None)
    
    async def generate_financial_report(self, gross_profit: float, gross_expenses: float) -> discord.Embed:
        ceo_rate = settings["ceo_salary_percent"] if self.include_ceo_salary else 0
        business_brackets = settings["tax_brackets"]
        ceo_brackets = settings["ceo_tax_brackets"]
        
        net_profit = gross_profit - gross_expenses
        
        if net_profit <= 0:
            embed = discord.Embed(
                title="Universalis Bank",
                description="*The bank teller looks over her glasses with a gentle, sympathetic smile...*",
                color=discord.Color.from_rgb(220, 53, 69)
            )
            
            sales_text = ""
            for item in self.items:
                revenue = item["price"] * item["quantity"]
                sales_text += f"{item['name']}: {item['quantity']} sold @ {format_money(item['price'])} = {format_money(revenue)}\n"
            
            embed.add_field(
                name="Sales Breakdown",
                value=f"```\n{sales_text}```",
                inline=False
            )
            
            embed.add_field(
                name="Financial Summary",
                value=(
                    f"```\n"
                    f"Gross Revenue:   {format_money(gross_profit):>15}\n"
                    f"Gross Expenses:  {format_money(gross_expenses):>15}\n"
                    f"{create_divider()}\n"
                    f"Net Profit:      {format_money(net_profit):>15}\n"
                    f"```"
                ),
                inline=False
            )
            embed.add_field(
                name="Assessment",
                value="*\"Oh dear, it looks like your expenses exceeded your earnings this period. Don't worry though - no taxes or salary deductions apply when there's no profit. Let me know if you need any help planning for next quarter!\"*",
                inline=False
            )
            embed.set_footer(text="Universalis Bank | Here to help your business thrive")
            return embed
        
        business_tax, business_breakdown = calculate_progressive_tax(net_profit, business_brackets)
        profit_after_tax = net_profit - business_tax
        
        if self.include_ceo_salary:
            gross_ceo_salary = profit_after_tax * (ceo_rate / 100)
            ceo_tax, ceo_breakdown = calculate_progressive_tax(gross_ceo_salary, ceo_brackets)
            net_ceo_salary = gross_ceo_salary - ceo_tax
            final_profit = profit_after_tax - gross_ceo_salary
        else:
            gross_ceo_salary = 0
            ceo_tax = 0
            net_ceo_salary = 0
            ceo_breakdown = []
            final_profit = profit_after_tax
        
        business_effective_rate = (business_tax / net_profit * 100) if net_profit > 0 else 0
        ceo_effective_rate = (ceo_tax / gross_ceo_salary * 100) if gross_ceo_salary > 0 else 0
        
        embed = discord.Embed(
            title="Universalis Bank",
            description="*The bank teller smiles warmly as she prepares your detailed financial report...*",
            color=discord.Color.from_rgb(255, 193, 7)
        )
        
        sales_text = ""
        for item in self.items:
            revenue = item["price"] * item["quantity"]
            sales_text += f"🎲 {item['name']}\n"
            sales_text += f"   d{item['dice']} → {item['quantity']} units @ {format_money(item['price'])} = {format_money(revenue)}\n"
        
        embed.add_field(
            name="Sales Results (Dice Rolls)",
            value=sales_text,
            inline=False
        )
        
        embed.add_field(
            name="Revenue Overview",
            value=(
                f"```\n"
                f"Gross Revenue:   {format_money(gross_profit):>15}\n"
                f"Gross Expenses:  {format_money(gross_expenses):>15}\n"
                f"{create_divider()}\n"
                f"Net Profit:      {format_money(net_profit):>15}\n"
                f"```"
            ),
            inline=False
        )
        
        business_tax_text = ""
        for item in business_breakdown:
            bracket_range = format_bracket_range(item["min"], item["max"])
            business_tax_text += f"{bracket_range} @ {item['rate']}%\n   Tax: {format_money(item['tax'])}\n"
        business_tax_text += f"\nTotal: {format_money(business_tax)} (Effective: {business_effective_rate:.1f}%)"
        
        embed.add_field(
            name="Business Income Tax",
            value=f"```\n{business_tax_text}\n```",
            inline=False
        )
        
        embed.add_field(
            name="After Business Tax",
            value=(
                f"```\n"
                f"Net Profit:      {format_money(net_profit):>15}\n"
                f"Business Tax:   -{format_money(business_tax):>15}\n"
                f"{create_divider()}\n"
                f"Remaining:       {format_money(profit_after_tax):>15}\n"
                f"```"
            ),
            inline=False
        )
        
        if self.include_ceo_salary:
            embed.add_field(
                name=f"CEO Compensation ({ceo_rate}% of post-tax profit)",
                value=(
                    f"```\n"
                    f"Gross CEO Salary: {format_money(gross_ceo_salary):>14}\n"
                    f"```"
                ),
                inline=False
            )
            
            ceo_tax_text = ""
            for item in ceo_breakdown:
                bracket_range = format_bracket_range(item["min"], item["max"])
                ceo_tax_text += f"{bracket_range} @ {item['rate']}%\n   Tax: {format_money(item['tax'])}\n"
            ceo_tax_text += f"\nTotal: {format_money(ceo_tax)} (Effective: {ceo_effective_rate:.1f}%)"
            
            embed.add_field(
                name="CEO Income Tax",
                value=f"```\n{ceo_tax_text}\n```",
                inline=False
            )
            
            embed.add_field(
                name="CEO Take-Home",
                value=(
                    f"```\n"
                    f"Gross Salary:    {format_money(gross_ceo_salary):>15}\n"
                    f"CEO Tax:        -{format_money(ceo_tax):>15}\n"
                    f"{create_divider()}\n"
                    f"Net Salary:      {format_money(net_ceo_salary):>15}\n"
                    f"```"
                ),
                inline=False
            )
            
            embed.add_field(
                name="Final Business Summary",
                value=(
                    f"```\n"
                    f"Profit After Tax: {format_money(profit_after_tax):>14}\n"
                    f"CEO Salary:      -{format_money(gross_ceo_salary):>14}\n"
                    f"{create_divider()}\n"
                    f"Business Profit:  {format_money(final_profit):>14}\n"
                    f"```"
                ),
                inline=False
            )
            
            total_taxes = business_tax + ceo_tax
            embed.add_field(
                name="Summary",
                value=(
                    f"*\"Wonderful news! Here's your complete breakdown:*\n\n"
                    f"*Business paid **{format_money(business_tax)}** in taxes.*\n"
                    f"*CEO receives **{format_money(net_ceo_salary)}** after their personal tax of **{format_money(ceo_tax)}**.*\n"
                    f"*The business retains **{format_money(final_profit)}**.*\n\n"
                    f"*Total taxes collected: **{format_money(total_taxes)}**. You're doing great!\"*"
                ),
                inline=False
            )
        else:
            embed.add_field(
                name="Final Business Summary (No CEO Salary)",
                value=(
                    f"```\n"
                    f"Profit After Tax: {format_money(profit_after_tax):>14}\n"
                    f"CEO Salary:       {format_money(0):>14}\n"
                    f"{create_divider()}\n"
                    f"Business Profit:  {format_money(final_profit):>14}\n"
                    f"```"
                ),
                inline=False
            )
            
            embed.add_field(
                name="Summary",
                value=(
                    f"*\"Here's your complete breakdown:*\n\n"
                    f"*Business paid **{format_money(business_tax)}** in taxes.*\n"
                    f"*No CEO salary was allocated this period.*\n"
                    f"*The business retains **{format_money(final_profit)}**.*\n\n"
                    f"*Great work managing your finances!\"*"
                ),
                inline=False
            )
        
        embed.set_footer(text="Universalis Bank | Here to help your business thrive")
        
        return embed

@bot.event
async def on_ready():
    print(f"{bot.user} is now open for business!")
    print(f"Connected to {len(bot.guilds)} guild(s)")
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} command(s)")
    except Exception as e:
        print(f"Failed to sync commands: {e}")

@bot.tree.command(name="calculate", description="Open the interactive financial calculator")
async def calculate(interaction: discord.Interaction):
    view = CalculatorView(interaction.user)
    await interaction.response.send_message(
        embed=view.create_main_embed(),
        view=view,
        ephemeral=True
    )

@bot.tree.command(name="view_rates", description="View the current tax brackets and CEO salary rates")
async def view_rates(interaction: discord.Interaction):
    business_brackets = settings["tax_brackets"]
    ceo_brackets = settings["ceo_tax_brackets"]
    ceo_rate = settings["ceo_salary_percent"]
    
    embed = discord.Embed(
        title="Universalis Bank - Tax Rate Schedule",
        description="*The bank teller pulls up the current rates with a helpful smile...*\n\n*\"Here's our complete tax structure!\"*",
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
        value=f"```\n{ceo_rate}% of post-tax business profit\n```",
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
    
    embed.set_footer(text="Use /calculate to run your numbers!")
    
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="set_bracket", description="[ADMIN] Set or update a business tax bracket")
@app_commands.describe(
    bracket_min="Minimum amount for this bracket (e.g., 0, 50000, 100000)",
    bracket_max="Maximum amount for this bracket (leave empty for unlimited)",
    rate="Tax rate percentage for this bracket (0-100)"
)
@app_commands.default_permissions(administrator=True)
async def set_bracket(interaction: discord.Interaction, bracket_min: float, rate: float, bracket_max: Optional[float] = None):
    if not is_admin(interaction):
        embed = discord.Embed(
            title="Access Restricted",
            description="*The bank teller gives an apologetic smile...*\n\n*\"I'm so sorry, but only authorized administrators can adjust tax brackets. Is there anything else I can help you with today?\"*",
            color=discord.Color.from_rgb(220, 53, 69)
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    
    if rate < 0 or rate > 100:
        embed = discord.Embed(
            title="Invalid Entry",
            description="*The bank teller tilts her head kindly...*\n\n*\"Oh, that doesn't seem quite right! The tax rate needs to be between 0% and 100%. Would you like to try again?\"*",
            color=discord.Color.from_rgb(255, 193, 7)
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    
    if bracket_min < 0:
        embed = discord.Embed(
            title="Invalid Entry",
            description="*The bank teller shakes her head gently...*\n\n*\"The minimum amount can't be negative, dear. Let's try again with a positive number!\"*",
            color=discord.Color.from_rgb(255, 193, 7)
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    
    if bracket_max is not None and bracket_max <= bracket_min:
        embed = discord.Embed(
            title="Invalid Entry",
            description="*The bank teller looks puzzled...*\n\n*\"The maximum needs to be higher than the minimum. Would you like to try again?\"*",
            color=discord.Color.from_rgb(255, 193, 7)
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    
    brackets = settings["tax_brackets"]
    updated = False
    for i, bracket in enumerate(brackets):
        if bracket["min"] == bracket_min:
            brackets[i] = {"min": bracket_min, "max": bracket_max, "rate": rate}
            updated = True
            break
    
    if not updated:
        brackets.append({"min": bracket_min, "max": bracket_max, "rate": rate})
    
    settings["tax_brackets"] = sorted(brackets, key=lambda x: x["min"])
    save_settings(settings)
    
    action = "updated" if updated else "added"
    bracket_range = format_bracket_range(bracket_min, bracket_max)
    
    embed = discord.Embed(
        title="Business Tax Bracket Updated",
        description="*The bank teller updates the system with a cheerful nod...*",
        color=discord.Color.from_rgb(40, 167, 69)
    )
    embed.add_field(
        name="Changes Applied",
        value=f"```\nBracket: {bracket_range}\nRate: {rate}%\nAction: {action.title()}\n```",
        inline=False
    )
    embed.add_field(
        name="Confirmation",
        value=f"*\"All done! I've {action} the business tax bracket for {bracket_range} at {rate}%. Is there anything else you need?\"*",
        inline=False
    )
    embed.set_footer(text=f"Authorized by {interaction.user.display_name}")
    
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="remove_bracket", description="[ADMIN] Remove a business tax bracket")
@app_commands.describe(bracket_min="The minimum amount of the bracket to remove")
@app_commands.default_permissions(administrator=True)
async def remove_bracket(interaction: discord.Interaction, bracket_min: float):
    if not is_admin(interaction):
        embed = discord.Embed(
            title="Access Restricted",
            description="*The bank teller gives an apologetic smile...*\n\n*\"I'm so sorry, but only authorized administrators can remove tax brackets. Is there anything else I can help you with today?\"*",
            color=discord.Color.from_rgb(220, 53, 69)
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    
    brackets = settings["tax_brackets"]
    
    if len(brackets) <= 1:
        embed = discord.Embed(
            title="Cannot Remove",
            description="*The bank teller looks concerned...*\n\n*\"Oh dear, we need at least one tax bracket in the system. I can't remove the last one!\"*",
            color=discord.Color.from_rgb(255, 193, 7)
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    
    found = None
    for i, bracket in enumerate(brackets):
        if bracket["min"] == bracket_min:
            found = brackets.pop(i)
            break
    
    if not found:
        embed = discord.Embed(
            title="Not Found",
            description=f"*The bank teller checks her records...*\n\n*\"Hmm, I don't see a bracket starting at ${bracket_min:,.0f}. Would you like to check /view_rates to see the current brackets?\"*",
            color=discord.Color.from_rgb(255, 193, 7)
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    
    settings["tax_brackets"] = brackets
    save_settings(settings)
    
    bracket_range = format_bracket_range(found["min"], found["max"])
    
    embed = discord.Embed(
        title="Business Tax Bracket Removed",
        description="*The bank teller updates the records...*",
        color=discord.Color.from_rgb(40, 167, 69)
    )
    embed.add_field(
        name="Removed Bracket",
        value=f"```\nBracket: {bracket_range}\nRate: {found['rate']}%\n```",
        inline=False
    )
    embed.add_field(
        name="Confirmation",
        value=f"*\"Done! I've removed the {bracket_range} bracket from our system. The remaining brackets are still in place.\"*",
        inline=False
    )
    embed.set_footer(text=f"Authorized by {interaction.user.display_name}")
    
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="set_ceo_bracket", description="[ADMIN] Set or update a CEO income tax bracket")
@app_commands.describe(
    bracket_min="Minimum amount for this bracket (e.g., 0, 10000, 50000)",
    bracket_max="Maximum amount for this bracket (leave empty for unlimited)",
    rate="Tax rate percentage for this bracket (0-100)"
)
@app_commands.default_permissions(administrator=True)
async def set_ceo_bracket(interaction: discord.Interaction, bracket_min: float, rate: float, bracket_max: Optional[float] = None):
    if not is_admin(interaction):
        embed = discord.Embed(
            title="Access Restricted",
            description="*The bank teller gives an apologetic smile...*\n\n*\"I'm so sorry, but only authorized administrators can adjust CEO tax brackets. Is there anything else I can help you with today?\"*",
            color=discord.Color.from_rgb(220, 53, 69)
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    
    if rate < 0 or rate > 100:
        embed = discord.Embed(
            title="Invalid Entry",
            description="*The bank teller tilts her head kindly...*\n\n*\"Oh, that doesn't seem quite right! The tax rate needs to be between 0% and 100%. Would you like to try again?\"*",
            color=discord.Color.from_rgb(255, 193, 7)
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    
    if bracket_min < 0:
        embed = discord.Embed(
            title="Invalid Entry",
            description="*The bank teller shakes her head gently...*\n\n*\"The minimum amount can't be negative, dear. Let's try again with a positive number!\"*",
            color=discord.Color.from_rgb(255, 193, 7)
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    
    if bracket_max is not None and bracket_max <= bracket_min:
        embed = discord.Embed(
            title="Invalid Entry",
            description="*The bank teller looks puzzled...*\n\n*\"The maximum needs to be higher than the minimum. Would you like to try again?\"*",
            color=discord.Color.from_rgb(255, 193, 7)
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    
    brackets = settings["ceo_tax_brackets"]
    updated = False
    for i, bracket in enumerate(brackets):
        if bracket["min"] == bracket_min:
            brackets[i] = {"min": bracket_min, "max": bracket_max, "rate": rate}
            updated = True
            break
    
    if not updated:
        brackets.append({"min": bracket_min, "max": bracket_max, "rate": rate})
    
    settings["ceo_tax_brackets"] = sorted(brackets, key=lambda x: x["min"])
    save_settings(settings)
    
    action = "updated" if updated else "added"
    bracket_range = format_bracket_range(bracket_min, bracket_max)
    
    embed = discord.Embed(
        title="CEO Tax Bracket Updated",
        description="*The bank teller updates the system with a cheerful nod...*",
        color=discord.Color.from_rgb(40, 167, 69)
    )
    embed.add_field(
        name="Changes Applied",
        value=f"```\nBracket: {bracket_range}\nRate: {rate}%\nAction: {action.title()}\n```",
        inline=False
    )
    embed.add_field(
        name="Confirmation",
        value=f"*\"All done! I've {action} the CEO tax bracket for {bracket_range} at {rate}%. Is there anything else you need?\"*",
        inline=False
    )
    embed.set_footer(text=f"Authorized by {interaction.user.display_name}")
    
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="remove_ceo_bracket", description="[ADMIN] Remove a CEO income tax bracket")
@app_commands.describe(bracket_min="The minimum amount of the bracket to remove")
@app_commands.default_permissions(administrator=True)
async def remove_ceo_bracket(interaction: discord.Interaction, bracket_min: float):
    if not is_admin(interaction):
        embed = discord.Embed(
            title="Access Restricted",
            description="*The bank teller gives an apologetic smile...*\n\n*\"I'm so sorry, but only authorized administrators can remove CEO tax brackets. Is there anything else I can help you with today?\"*",
            color=discord.Color.from_rgb(220, 53, 69)
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    
    brackets = settings["ceo_tax_brackets"]
    
    if len(brackets) <= 1:
        embed = discord.Embed(
            title="Cannot Remove",
            description="*The bank teller looks concerned...*\n\n*\"Oh dear, we need at least one CEO tax bracket in the system. I can't remove the last one!\"*",
            color=discord.Color.from_rgb(255, 193, 7)
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    
    found = None
    for i, bracket in enumerate(brackets):
        if bracket["min"] == bracket_min:
            found = brackets.pop(i)
            break
    
    if not found:
        embed = discord.Embed(
            title="Not Found",
            description=f"*The bank teller checks her records...*\n\n*\"Hmm, I don't see a CEO tax bracket starting at ${bracket_min:,.0f}. Would you like to check /view_rates to see the current brackets?\"*",
            color=discord.Color.from_rgb(255, 193, 7)
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    
    settings["ceo_tax_brackets"] = brackets
    save_settings(settings)
    
    bracket_range = format_bracket_range(found["min"], found["max"])
    
    embed = discord.Embed(
        title="CEO Tax Bracket Removed",
        description="*The bank teller updates the records...*",
        color=discord.Color.from_rgb(40, 167, 69)
    )
    embed.add_field(
        name="Removed Bracket",
        value=f"```\nBracket: {bracket_range}\nRate: {found['rate']}%\n```",
        inline=False
    )
    embed.add_field(
        name="Confirmation",
        value=f"*\"Done! I've removed the {bracket_range} CEO tax bracket from our system. The remaining brackets are still in place.\"*",
        inline=False
    )
    embed.set_footer(text=f"Authorized by {interaction.user.display_name}")
    
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="set_ceo_salary", description="[ADMIN] Set the CEO salary percentage")
@app_commands.describe(percentage="The new CEO salary percentage (0-100)")
@app_commands.default_permissions(administrator=True)
async def set_ceo_salary(interaction: discord.Interaction, percentage: float):
    if not is_admin(interaction):
        embed = discord.Embed(
            title="Access Restricted",
            description="*The bank teller gives an apologetic smile...*\n\n*\"I'm so sorry, but only authorized administrators can adjust CEO compensation rates. Is there anything else I can help you with today?\"*",
            color=discord.Color.from_rgb(220, 53, 69)
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    
    if percentage < 0 or percentage > 100:
        embed = discord.Embed(
            title="Invalid Entry",
            description="*The bank teller tilts her head kindly...*\n\n*\"Hmm, that number doesn't look right! The salary rate should be between 0% and 100%. Want to give it another try?\"*",
            color=discord.Color.from_rgb(255, 193, 7)
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    
    old_rate = settings["ceo_salary_percent"]
    settings["ceo_salary_percent"] = percentage
    save_settings(settings)
    
    embed = discord.Embed(
        title="CEO Salary Rate Updated",
        description="*The bank teller updates the compensation schedule...*",
        color=discord.Color.from_rgb(40, 167, 69)
    )
    embed.add_field(
        name="Changes Applied",
        value=f"```\nPrevious Rate: {old_rate}%\nNew Rate: {percentage}%\n```",
        inline=False
    )
    embed.add_field(
        name="Confirmation",
        value=f"*\"Perfect! I've updated the CEO salary rate from {old_rate}% to {percentage}% of post-tax profit. This will apply to all future calculations.\"*",
        inline=False
    )
    embed.set_footer(text=f"Authorized by {interaction.user.display_name}")
    
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="help_finance", description="View the help guide for the finance calculator")
async def help_finance(interaction: discord.Interaction):
    embed = discord.Embed(
        title="Universalis Bank - Help Guide",
        description="*The bank teller hands you a helpful brochure...*\n\n*\"Here's everything you need to know about our services!\"*",
        color=discord.Color.from_rgb(111, 66, 193)
    )
    
    embed.add_field(
        name="Basic Commands",
        value=(
            "**`/calculate`** - Open the interactive financial calculator\n"
            "**`/view_rates`** - See all current tax brackets and rates\n"
            "**`/help_finance`** - View this help guide"
        ),
        inline=False
    )
    
    embed.add_field(
        name="How to Use /calculate",
        value=(
            "1. Run `/calculate` to open your private calculator\n"
            "2. Click **Add Item** to add products/services sold\n"
            "3. Enter item name and price per unit\n"
            "4. Select a dice type (d10, d12, d20, d25, d50, d100)\n"
            "5. The dice roll determines how many units sold!\n"
            "6. Toggle **CEO Salary** on/off as needed\n"
            "7. Set your **Business Expenses**\n"
            "8. Click **Calculate** for your financial report"
        ),
        inline=False
    )
    
    embed.add_field(
        name="Admin Commands - Business Tax",
        value=(
            "**`/set_bracket`** - Add/update a business tax bracket\n"
            "**`/remove_bracket`** - Remove a business tax bracket"
        ),
        inline=False
    )
    
    embed.add_field(
        name="Admin Commands - CEO Tax",
        value=(
            "**`/set_ceo_bracket`** - Add/update a CEO income tax bracket\n"
            "**`/remove_ceo_bracket`** - Remove a CEO income tax bracket\n"
            "**`/set_ceo_salary`** - Set CEO salary percentage"
        ),
        inline=False
    )
    
    embed.add_field(
        name="How It Works",
        value=(
            "```\n"
            "1. Add items/services with prices\n"
            "2. Roll dice to determine quantities sold\n"
            "3. Total Revenue = Sum of (price × quantity)\n"
            "4. Net Profit = Revenue - Expenses\n"
            "5. Business Tax (progressive brackets)\n"
            "6. CEO Salary = % of post-tax profit\n"
            "7. CEO Tax (progressive brackets)\n"
            "8. Final totals calculated\n"
            "```"
        ),
        inline=False
    )
    
    embed.add_field(
        name="Dice Types",
        value=(
            "Choose from these dice for quantity rolls:\n"
            "**d10**: 1-10 units | **d12**: 1-12 units\n"
            "**d20**: 1-20 units | **d25**: 1-25 units\n"
            "**d50**: 1-50 units | **d100**: 1-100 units"
        ),
        inline=False
    )
    
    embed.set_footer(text="Universalis Bank | Here to help your business thrive")
    
    await interaction.response.send_message(embed=embed)

if __name__ == "__main__":
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        print("Error: DISCORD_BOT_TOKEN not found in environment variables!")
        print("Please set your Discord bot token in the Secrets tab.")
        exit(1)
    
    print("Starting the Universalis Bank Bot...")
    bot.run(token)
