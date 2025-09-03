# requirements:
#   discord.py==2.4.0
#   aiosqlite==0.20.0

import os, asyncio, math, time, aiosqlite
import aiohttp
import discord
from discord.ext import commands, tasks
from discord import app_commands

TOKEN = os.getenv("DISCORD_TOKEN")  # 從環境變數讀取
if not TOKEN:
    raise SystemExit("請先設定環境變數 DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))   # 可選：限制到特定伺服器
ADMIN_ROLE_ID = int(os.getenv(1404509588572606585, "0"))   # 可選：只有管理角色可用管理指令

DB_PATH = os.getenv("DB_PATH", "/data/coupons.db")

INTENTS = discord.Intents.default()
INTENTS.message_content = False
bot = commands.Bot(command_prefix="!", intents=INTENTS)

# ---- 狀態變數（會存 DB）----
state = {
    "price_channel_id": None,
    "price_message_id": None,
    "rmb": 313.0,
    "fx": 4.3,
    "fee_pct": 1.5,
    "logo_url": "https://i.imgur.com/3O7H8xP.png",  # 先放一個可替換的網址
}

# ---- 公式：依你規則計價 ----
def calc_price(rmb: float, fx: float, fee_pct: float = 1.5) -> int:
    cost = rmb * fx * (1 + fee_pct/100)  # 成本（含手續）
    cost_up10 = math.ceil(cost / 10.0) * 10  # 無條件進位到十位
    if cost_up10 <= 1430:
        return int(cost_up10 + 80)
    else:
        return int(cost_up10 + 90)

# ---- 嵌入卡片 ----
def build_embed():
    price = calc_price(state["rmb"], state["fx"], state["fee_pct"])
    e = discord.Embed(
        title="冠軍組合包 6825VP｜即時售價",
        description="價格會隨匯率與進貨價浮動；越接近上架日（9/9）越可能調整。",
        color=0x2ecc71,
    )
    e.add_field(name="人民幣 (RMB)", value=f"{state['rmb']:.0f}", inline=True)
    e.add_field(name="匯率", value=f"{state['fx']:.2f}", inline=True)
    e.add_field(name="手續", value=f"{state['fee_pct']}%", inline=True)
    e.add_field(name="💰 售價 (NTD)", value=f"**NT$ {price}**", inline=False)
    if state.get("logo_url"):
        e.set_thumbnail(url=state["logo_url"])
    e.set_footer(text="Fox Shop · 自動更新看板")
    e.timestamp = discord.utils.utcnow()
    return e

# ---- DB helpers ----
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS meta(
            key TEXT PRIMARY KEY, val TEXT
        )""")
        await db.execute("""
        CREATE TABLE IF NOT EXISTS coupons(
            code TEXT PRIMARY KEY,
            creator TEXT,
            active INTEGER DEFAULT 1,
            created_at INTEGER
        )""")
        await db.execute("""
        CREATE TABLE IF NOT EXISTS redemptions(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            code TEXT,
            channel_id INTEGER,
            ts INTEGER
        )""")
        await db.commit()

        # 載入 meta 到 state
        async with db.execute("SELECT key,val FROM meta") as cur:
            async for k,v in cur:
                if k in state:
                    # 型別處理
                    if k in ("price_channel_id","price_message_id"):
                        state[k] = int(v)
                    elif k in ("rmb","fx","fee_pct"):
                        state[k] = float(v)
                    else:
                        state[k] = v

async def save_meta(key, val):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO meta(key,val) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET val=excluded.val", (key, str(val)))
        await db.commit()

# ---- 背景任務：每分鐘更新 ----
async defp(seconds=60)
async def update_price_message():
    try:
        ch_id = state.get("price_channel_id")
        msg_id = state.get("price_message_id")
        if not ch_id:
            return
        channel = bot.get_channel(ch_id)
        if not channel:
            return
        embed = build_embed()
        if msg_id:
            try:
                msg = await channel.fetch_message(msg_id)
                await msg.edit(embed=embed)
            except discord.NotFound:
                m = await channel.send(embed=embed)
                state["price_message_id"] = m.id
                await save_meta("price_message_id", m.id)
        else:
            m = await channel.send(embed=embed)
            state["price_message_id"] = m.id
            await save_meta("price_message_id", m.id)
    except Exception as e:
        print("update error:", e)

# ---- 權限檢查（可選）----
def is_admin_interaction(inter: discord.Interaction) -> bool:
    if ADMIN_ROLE_ID:
        return any(r.id == ADMIN_ROLE_ID for r in inter.user.roles) if isinstance(inter.user, discord.Member) else False
    return inter.user.guild_permissions.manage_guild if isinstance(inter.user, discord.Member) else False

# ---- 管理指令 ----
class Admin(app_commands.Group):
    def __init__(self):
        super().__init__(name="admin", description="管理指令")

    @app_commands.command(name="bind_price_channel", description="將目前頻道設為價格看板")
    async def bind_price_channel(self, inter: discord.Interaction):
        if not is_admin_interaction(inter):
            return await inter.response.send_message("沒有權限。", ephemeral=True)
        state["price_channel_id"] = inter.channel.id
        await save_meta("price_channel_id", inter.channel.id)
        state["price_message_id"] = None
        await save_meta("price_message_id", "")
        await inter.response.send_message("已綁定本頻道為價格看板。", ephemeral=True)

    @app_commands.command(name="set_price", description="設定人民幣單價（6825VP）")
    @app_commands.describe(rmb="人民幣數值，例如 313")
    async def set_price(self, inter: discord.Interaction, rmb: float):
        if not is_admin_interaction(inter):
            return await inter.response.send_message("沒有權限。", ephemeral=True)
        state["rmb"] = rmb
        await save_meta("rmb", rmb)
        await inter.response.send_message(f"已設定 RMB = {rmb:.0f}", ephemeral=True)

    @app_commands.command(name="set_fx", description="設定匯率（TWD/RMB）")
    @app_commands.describe(rate="例如 4.30")
    async def set_fx(self, inter: discord.Interaction, rate: float):
        if not is_admin_interaction(inter):
            return await inter.response.send_message("沒有權限。", ephemeral=True)
        state["fx"] = rate
        await save_meta("fx", rate)
        await inter.response.send_message(f"已設定 匯率 = {rate:.4g}", ephemeral=True)

    @app_commands.command(name="set_logo", description="設定嵌入卡片頭貼 URL")
    async def set_logo(self, inter: discord.Interaction, url: str):
        if not is_admin_interaction(inter):
            return await inter.response.send_message("沒有權限。", ephemeral=True)
        state["logo_url"] = url
        await save_meta("logo_url", url)
        await inter.response.send_message("已更新頭貼 URL。", ephemeral=True)

bot.tree.add_command(Admin())

# ---- 優惠碼：上架/下架/列表 ----
@app_commands.command(name="coupon_add", description="上架優惠碼（必須以 CHAMPION 結尾）")
@app_commands.describe(code="創作者碼，例如 AMY2025CHAMPION", creator="創作者識別名")
async def coupon_add(inter: discord.Interaction, code: str, creator: str):
    if not is_admin_interaction(inter):
        return await inter.response.send_message("沒有權限。", ephemeral=True)
    code_up = code.strip().upper()
    if not code_up.endswith("CHAMPION"):
        return await inter.response.send_message("優惠碼必須以 CHAMPION 結尾。", ephemeral=True)
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute("INSERT INTO coupons(code,creator,active,created_at) VALUES(?,?,1,?)",
                             (code_up, creator, int(time.time())))
            await db.commit()
        except aiosqlite.IntegrityError:
            return await inter.response.send_message("此優惠碼已存在。", ephemeral=True)
    await inter.response.send_message(f"已上架優惠碼 **{code_up}**（創作者：{creator}）", ephemeral=True)

@app_commands.command(name="coupon_remove", description="下架優惠碼")
async def coupon_remove(inter: discord.Interaction, code: str):
    if not is_admin_interaction(inter):
        return await inter.response.send_message("沒有權限。", ephemeral=True)
    code_up = code.strip().upper()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE coupons SET active=0 WHERE code=?", (code_up,))
        await db.commit()
    await inter.response.send_message(f"已下架 **{code_up}**", ephemeral=True)

@app_commands.command(name="coupon_list", description="列出有效優惠碼（前50）")
async def coupon_list(inter: discord.Interaction):
    if not is_admin_interaction(inter):
        return await inter.response.send_message("沒有權限。", ephemeral=True)
    rows = []
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT code,creator,active FROM coupons ORDER BY created_at DESC LIMIT 50") as cur:
            async for code, creator, active in cur:
                status = "啟用" if active else "關閉"
                rows.append(f"- {code}（{creator}，{status}）")
    txt = "\n".join(rows) if rows else "（無）"
    await inter.response.send_message(txt, ephemeral=True)

bot.tree.add_command(coupon_add)
bot.tree.add_command(coupon_remove)
bot.tree.add_command(coupon_list)

# ---- 兌換：/redeem <code> ----
class ConfirmView(discord.ui.View):
    def __init__(self, code_up: str):
        super().__init__(timeout=60)
        self.code_up = code_up
        self.result = None

    @discord.ui.button(label="✅ 我已開單，確認兌換", style=discord.ButtonStyle.success)
    async def yes(self, inter: discord.Interaction, button: discord.ui.Button):
        self.result = True
        self.stop()
        await inter.response.defer()

    @discord.ui.button(label="取消", style=discord.ButtonStyle.secondary)
    async def no(self, inter: discord.Interaction, button: discord.ui.Button):
        self.result = False
        self.stop()
        await inter.response.defer()

@app_commands.command(name="redeem", description="兌換 20 元優惠碼（僅限 CHAMPION 活動，每人一次）")
async def redeem(inter: discord.Interaction, code: str):
    # 只能在 ticket 內使用：這裡用簡單規則（頻道名含 ticket / 單）
    ch_name = inter.channel.name.lower() if isinstance(inter.channel, discord.TextChannel) else ""
    if "ticket" not in ch_name and "單" not in ch_name:
        return await inter.response.send_message("請在你的工單／ticket 頻道內兌換。", ephemeral=True)

    code_up = code.strip().upper()

    # 檢查優惠碼存在且啟用
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT active FROM coupons WHERE code=?", (code_up,)) as cur:
            row = await cur.fetchone()
            if not row:
                return await inter.response.send_message("優惠碼不存在。", ephemeral=True)
            if row[0] != 1:
                return await inter.response.send_message("此優惠碼已關閉。", ephemeral=True)

        # 檢查此人是否已兌換過任何 CHAMPION 結尾優惠碼
        async with db.execute("""
            SELECT COUNT(*) FROM redemptions
             WHERE user_id=? AND code LIKE '%CHAMPION'
        """, (inter.user.id,)) as cur2:
            cnt = (await cur2.fetchone())[0]
            if cnt > 0:
                return await inter.response.send_message("你已兌換過本活動優惠碼，無法重複兌換。", ephemeral=True)

    # 顯示警語＋按鈕確認
    warn = ("**請確認：** 若要在購買時使用此優惠碼，請確保你已經開單，並在該 ticket 內輸入本優惠碼。\n"
            "若在其他地方兌換則不算數；且兌換後**不得重複兌換本活動優惠碼**。\n\n"
            f"要以此優惠碼 **{code_up}** 完成兌換嗎？")
    view = ConfirmView(code_up)
    await inter.response.send_message(warn, view=view, ephemeral=True)
    await view.wait()
    if view.result is not True:
        return  # 取消或逾時

    # 寫入兌換紀錄
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO redemptions(user_id, code, channel_id, ts) VALUES(?,?,?,?)",
                         (inter.user.id, code_up, inter.channel.id, int(time.time())))
        await db.commit()

    # 公開回覆在 ticket 讓大家看得到
    await inter.followup.send(
        f"🎫 <@{inter.user.id}> 已成功兌換優惠碼 **{code_up}**（折抵 $20）",
        ephemeral=False
    )

bot.tree.add_command(redeem)

# ---- 啟動 ----
@bot.event
async def on_ready():
    await init_db()
    try:
        if GUILD_ID:
            guild = bot.get_guild(GUILD_ID)
            await bot.tree.sync(guild=guild)
        else:
            await bot.tree.sync()
    except Exception as e:
        print("sync error:", e)
    update_price_message.start()   # 每分鐘更新價格看板
    refresh_fx_loop.start()        # 每10分鐘抓匯率
    print(f"Logged in as {bot.user}")


FX_URL = "https://api.exchangerate.host/convert?from=CNY&to=TWD"

async def fetch_fx_2dp():
    """抓 CNY→TWD 匯率並四捨五入到小數 2 位。"""
    async with aiohttp.ClientSession() as sess:
        async with sess.get(FX_URL, timeout=10) as r:
            data = await r.json()
            rate = float(data.get("result", 0))  # e.g. 4.312345
            return round(rate, 2)

@tasks.loop(minutes=10)
async def refresh_fx_loop():
    try:
        fx = await fetch_fx_2dp()
        if fx > 0:
            state["fx"] = fx
            await save_meta("fx", fx)
            print("[FX] updated:", fx)
    except Exception as e:
        print("fx error:", e)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("請設好 DISCORD_TOKEN 環境變數")
    bot.run(TOKEN)
