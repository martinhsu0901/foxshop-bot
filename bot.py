import os, math, time, json, asyncio, aiohttp, discord
from discord.ext import commands, tasks
from discord import app_commands
import gspread
from google.oauth2.service_account import Credentials

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))
ADMIN_ROLE_ID = int(os.getenv("1404509588572606585", "0"))
GSHEET_ID = os.getenv("e5f0e285efa11d86df46d982923b99dda83ae694")
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")

if not TOKEN:
    raise SystemExit("請設定環境變數 DISCORD_TOKEN")
if not GSHEET_ID or not GOOGLE_CREDENTIALS_JSON:
    raise SystemExit("請設定 GSHEET_ID 與 GOOGLE_CREDENTIALS_JSON")

# ---- Google Sheets 連線 ----
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
creds = Credentials.from_service_account_info(json.loads(GOOGLE_CREDENTIALS_JSON), scopes=SCOPES)
gc = gspread.authorize(creds)
sh = gc.open_by_key(GSHEET_ID)

def ensure_worksheets():
    names = [ws.title for ws in sh.worksheets()]
    if "meta" not in names:
        sh.add_worksheet("meta", rows=100, cols=3)
        sh.worksheet("meta").append_row(["key","val"])
    if "coupons" not in names:
        sh.add_worksheet("coupons", rows=1000, cols=4)
        sh.worksheet("coupons").append_row(["code","creator","active","created_at"])
    if "redemptions" not in names:
        sh.add_worksheet("redemptions", rows=5000, cols=4)
        sh.worksheet("redemptions").append_row(["user_id","code","channel_id","ts"])

ensure_worksheets()
ws_meta = sh.worksheet("meta")
ws_coupons = sh.worksheet("coupons")
ws_red = sh.worksheet("redemptions")

# ---- meta 存取 ----
def meta_get(key, default=None):
    try:
        cell = ws_meta.find(key)
        return ws_meta.cell(cell.row, 2).value
    except gspread.exceptions.CellNotFound:
        return default

def meta_set(key, val):
    try:
        cell = ws_meta.find(key)
        ws_meta.update_cell(cell.row, 2, str(val))
    except gspread.exceptions.CellNotFound:
        ws_meta.append_row([key, str(val)])

# ---- 狀態（從 meta 還原）----
state = {
    "price_channel_id": int(meta_get("price_channel_id", "0") or 0),
    "price_message_id": int(meta_get("price_message_id", "0") or 0),
    "rmb": float(meta_get("rmb", "313") or 313),
    "fx": float(meta_get("fx", "4.30") or 4.30),
    "fee_pct": float(meta_get("fee_pct", "1.5") or 1.5),
    "logo_url": meta_get("logo_url", "") or "",
}

INTENTS = discord.Intents.default()
INTENTS.message_content = False
bot = commands.Bot(command_prefix="!", intents=INTENTS)

# ---- 價格公式 ----
def calc_price(rmb: float, fx: float, fee_pct: float = 1.5) -> int:
    cost = rmb * fx * (1 + fee_pct/100)
    cost_up10 = math.ceil(cost / 10.0) * 10
    return int(cost_up10 + (80 if cost_up10 <= 1430 else 90))

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
    if state["logo_url"]:
        e.set_thumbnail(url=state["logo_url"])
    e.set_footer(text="Fox Shop · 自動更新看板")
    e.timestamp = discord.utils.utcnow()
    return e

# ---- 匯率自動更新 ----
FX_URL = "https://api.exchangerate.host/convert?from=CNY&to=TWD"

async def fetch_fx_2dp():
    async with aiohttp.ClientSession() as sess:
        async with sess.get(FX_URL, timeout=10) as r:
            data = await r.json()
            rate = float(data.get("result", 0))
            return round(rate, 2)

@tasks.loop(minutes=10)
async def refresh_fx_loop():
    try:
        fx = await fetch_fx_2dp()
        if fx > 0 and fx != state["fx"]:
            state["fx"] = fx
            meta_set("fx", fx)
            print("[FX] updated:", fx)
    except Exception as e:
        print("fx error:", e)

# ---- 每分鐘更新看板 ----
@tasks.loop(seconds=60)
async def update_price_message():
    try:
        ch_id = state.get("price_channel_id")
        if not ch_id:
            return
        channel = bot.get_channel(ch_id)
        if not channel:
            return
        embed = build_embed()
        msg_id = state.get("price_message_id")
        if msg_id:
            try:
                msg = await channel.fetch_message(msg_id)
                await msg.edit(embed=embed)
            except discord.NotFound:
                m = await channel.send(embed=embed)
                state["price_message_id"] = m.id
                meta_set("price_message_id", m.id)
        else:
            m = await channel.send(embed=embed)
            state["price_message_id"] = m.id
            meta_set("price_message_id", m.id)
    except Exception as e:
        print("update error:", e)

# ---- 權限判斷 ----
def is_admin_inter(inter: discord.Interaction) -> bool:
    if ADMIN_ROLE_ID:
        return any(r.id == ADMIN_ROLE_ID for r in inter.user.roles) if isinstance(inter.user, discord.Member) else False
    return inter.user.guild_permissions.manage_guild if isinstance(inter.user, discord.Member) else False

# ---- Admin 群組 ----
class Admin(app_commands.Group):
    def __init__(self):
        super().__init__(name="admin", description="管理指令")

    @app_commands.command(name="bind_price_channel", description="本頻道設為價格看板")
    async def bind_price_channel(self, inter: discord.Interaction):
        if not is_admin_inter(inter): return await inter.response.send_message("沒有權限。", ephemeral=True)
        state["price_channel_id"] = inter.channel.id
        state["price_message_id"] = 0
        meta_set("price_channel_id", inter.channel.id)
        meta_set("price_message_id", 0)
        await inter.response.send_message("已綁定本頻道為價格看板。", ephemeral=True)

    @app_commands.command(name="set_price", description="設定人民幣單價（6825VP）")
    async def set_price(self, inter: discord.Interaction, rmb: float):
        if not is_admin_inter(inter): return await inter.response.send_message("沒有權限。", ephemeral=True)
        state["rmb"] = rmb
        meta_set("rmb", rmb)
        await inter.response.send_message(f"已設定 RMB = {rmb:.0f}", ephemeral=True)

    @app_commands.command(name="set_fx", description="手動設定匯率（TWD/RMB）")
    async def set_fx(self, inter: discord.Interaction, rate: float):
        if not is_admin_inter(inter): return await inter.response.send_message("沒有權限。", ephemeral=True)
        state["fx"] = rate
        meta_set("fx", rate)
        await inter.response.send_message(f"已設定 匯率 = {rate:.2f}", ephemeral=True)

    @app_commands.command(name="set_logo", description="設定看板縮圖 URL")
    async def set_logo(self, inter: discord.Interaction, url: str):
        if not is_admin_inter(inter): return await inter.response.send_message("沒有權限。", ephemeral=True)
        state["logo_url"] = url
        meta_set("logo_url", url)
        await inter.response.send_message("已更新頭貼 URL。", ephemeral=True)

bot.tree.add_command(Admin())

# ---- 優惠碼：上架/下架/列表/統計 ----
@app_commands.command(name="coupon_add", description="上架優惠碼（必須以 CHAMPION 結尾）")
async def coupon_add(inter: discord.Interaction, code: str, creator: str):
    if not is_admin_inter(inter): return await inter.response.send_message("沒有權限。", ephemeral=True)
    code_up = code.strip().upper()
    if not code_up.endswith("CHAMPION"):
        return await inter.response.send_message("優惠碼必須以 CHAMPION 結尾。", ephemeral=True)
    rows = ws_coupons.col_values(1)  # code 欄
    if code_up in rows:
        return await inter.response.send_message("此優惠碼已存在。", ephemeral=True)
    ws_coupons.append_row([code_up, creator, "1", str(int(time.time()))])
    await inter.response.send_message(f"已上架 **{code_up}**（創作者：{creator}）", ephemeral=True)

@app_commands.command(name="coupon_remove", description="下架優惠碼")
async def coupon_remove(inter: discord.Interaction, code: str):
    if not is_admin_inter(inter): return await inter.response.send_message("沒有權限。", ephemeral=True)
    code_up = code.strip().upper()
    try:
        cell = ws_coupons.find(code_up)
        ws_coupons.update_cell(cell.row, 3, "0")  # active=0
        await inter.response.send_message(f"已下架 **{code_up}**", ephemeral=True)
    except gspread.exceptions.CellNotFound:
        await inter.response.send_message("找不到此優惠碼。", ephemeral=True)

@app_commands.command(name="coupon_list", description="列出最近的優惠碼（前50）")
async def coupon_list(inter: discord.Interaction):
    if not is_admin_inter(inter): return await inter.response.send_message("沒有權限。", ephemeral=True)
    vals = ws_coupons.get_all_values()[1:][:50]
    lines = []
    for code, creator, active, created_at in vals:
        status = "啟用" if active=="1" else "關閉"
        lines.append(f"- {code}（{creator}，{status}）")
    await inter.response.send_message("\n".join(lines) if lines else "（無）", ephemeral=True)

@app_commands.command(name="coupon_stats", description="各創作者兌換數")
async def coupon_stats(inter: discord.Interaction):
    if not is_admin_inter(inter): return await inter.response.send_message("沒有權限。", ephemeral=True)
    # 讀全表做彙總
    coupons = {row[0]: row[1] for row in ws_coupons.get_all_values()[1:]}  # code->creator
    reds = ws_red.get_all_values()[1:]
    cnt = {}
    for _, code, _, _ in reds:
        creator = coupons.get(code, "未知")
        cnt[creator] = cnt.get(creator, 0) + 1
    lines = [f"- {k}: {v}" for k,v in sorted(cnt.items(), key=lambda x:-x[1])] or ["（尚無兌換）"]
    await inter.response.send_message("\n".join(lines), ephemeral=True)

# ---- 兌換流程 ----
class ConfirmView(discord.ui.View):
    def __init__(self, code_up: str):
        super().__init__(timeout=60)
        self.code_up = code_up
        self.ok = None
    @discord.ui.button(label="✅ 我已開單，確認兌換", style=discord.ButtonStyle.success)
    async def yes(self, inter: discord.Interaction, button: discord.ui.Button):
        self.ok = True; await inter.response.defer(); self.stop()
    @discord.ui.button(label="取消", style=discord.ButtonStyle.secondary)
    async def no(self, inter: discord.Interaction, button: discord.ui.Button):
        self.ok = False; await inter.response.defer(); self.stop()

@app_commands.command(name="redeem", description="兌換 20 元優惠碼（CHAMPION 活動，每人一次）")
async def redeem(inter: discord.Interaction, code: str):
    ch_name = inter.channel.name.lower() if isinstance(inter.channel, discord.TextChannel) else ""
    if "ticket" not in ch_name and "單" not in ch_name:
        return await inter.response.send_message("請在你的工單／ticket 頻道內兌換。", ephemeral=True)

    code_up = code.strip().upper()
    # 檢查是否存在且啟用
    try:
        cell = ws_coupons.find(code_up)
        row = ws_coupons.row_values(cell.row)
        active = row[2] == "1"
        if not active:
            return await inter.response.send_message("此優惠碼已關閉。", ephemeral=True)
    except gspread.exceptions.CellNotFound:
        return await inter.response.send_message("優惠碼不存在。", ephemeral=True)

    # 是否已兌換過任何 *CHAMPION 結尾*
    reds = ws_red.get_all_values()[1:]
    for uid, c, _, _ in reds:
        if int(uid) == inter.user.id and c.endswith("CHAMPION"):
            return await inter.response.send_message("你已兌換過本活動優惠碼，無法重複兌換。", ephemeral=True)

    warn = ("**請確認：** 若要在購買時使用此優惠碼，請確保你已經開單，並在該 ticket 內輸入本優惠碼。\n"
            "若在其他地方兌換則不算數；且兌換後**不得重複兌換本活動優惠碼**。\n\n"
            f"要以此優惠碼 **{code_up}** 完成兌換嗎？")
    view = ConfirmView(code_up)
    await inter.response.send_message(warn, view=view, ephemeral=True)
    await view.wait()
    if view.ok is not True: return

    ws_red.append_row([str(inter.user.id), code_up, str(inter.channel.id), str(int(time.time()))])
    await inter.followup.send(f"🎫 <@{inter.user.id}> 已成功兌換優惠碼 **{code_up}**（折抵 $20）", ephemeral=False)

# ---- 啟動 ----
@bot.event
async def on_ready():
    try:
        if GUILD_ID:
            await bot.tree.sync(guild=bot.get_guild(GUILD_ID))
        else:
            await bot.tree.sync()
    except Exception as e:
        print("sync error:", e)
    update_price_message.start()
    refresh_fx_loop.start()
    print(f"Logged in as {bot.user}")

if __name__ == "__main__":
    bot.run(TOKEN)

