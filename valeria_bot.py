# ╔══════════════════════════════════════════════════════════════════════╗
# ║  VALERIA'S BOT — Recordatorios de sesiones de terapia (HVN Heaven)    ║
# ╠══════════════════════════════════════════════════════════════════════╣
# ║  Fuentes (Google Sheet publicada como CSV):                          ║
# ║   · Pestaña del MES actual  → citas agendadas (Paciente, Fecha/hora) ║
# ║   · Form_Responses          → usuario de Discord de cada persona     ║
# ║  El bot cruza ambas por NOMBRE para saber a quién mandar el DM.      ║
# ║                                                                      ║
# ║  Recordatorios:                                                      ║
# ║   · 4 h antes de la sesión → DM a la persona                         ║
# ║   · a la hora exacta       → DM a la persona (waiting room)          ║
# ║   · a la hora exacta       → DM a cada Therapist con botón ✅         ║
# ║  Hora de referencia: Venezuela (VET, UTC-4, sin horario de verano).  ║
# ╚══════════════════════════════════════════════════════════════════════╝
import os, csv, io, re, unicodedata
from datetime import datetime, date, time
from zoneinfo import ZoneInfo

import aiohttp
import discord
from discord.ext import tasks

# ── CONFIGURACIÓN ──────────────────────────────────────────────────────
TOKEN          = os.environ["VALERIA_BOT_TOKEN"]
GUILD_ID       = 1498414113703989258
VET            = ZoneInfo("America/Caracas")
THERAPIST_ROLE    = "Therapist"            # (respaldo) por si no se usa el ID
THERAPIST_ROLE_ID = 1573830639612854362    # rol que recibe el aviso "ya es hora"
REMINDER_HOURS_BEFORE = 4

# ID de la hoja (el de la URL del navegador: /spreadsheets/d/<ESTE>/edit)
SHEET_ID = os.environ.get("SHEET_ID", "1EJ_fLaHFoolkCM6m0lj3mdzr3aI8NMsjcPLqtnx_8JQ")

# gid de cada pestaña (número que sale en la URL al abrir esa pestaña: ...#gid=XXXX)
GID_FORM = os.environ.get("GID_FORM", "560847215")     # Form_Responses (usuarios de Discord)
# Meses: el bot elige la pestaña según el mes actual.
MONTH_GIDS = {
    # "YYYY-MM": "gid",
    "2026-11": os.environ.get("GID_NOV2026", "372335700"),   # Noviembre 2026 (de tu captura)
    # Agrega aquí los gids de los otros meses cuando los tengas:
    # "2026-12": "XXXXXXXX",   # Diciembre 2026
    # "2027-01": "XXXXXXXX",   # Enero 2027
}

def csv_url(gid: str) -> str:
    # Usa la exportación directa (requiere que la hoja esté compartida como "cualquiera con el enlace").
    return f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid={gid}"

# ── NOMBRES DE COLUMNAS ────────────────────────────────────────────────
# Form_Responses:
FR_NAME    = "Nombre"
FR_SURNAME = "Apellido"
FR_DISCORD = "Usuario de Discord"
# Pestaña de mes (la tabla real empieza donde está el encabezado "Paciente"):
MM_PATIENT  = "Paciente"
MM_DATETIME = "Fecha y hora de la sesión"

# ── MENSAJES ───────────────────────────────────────────────────────────
def msg_4h(h): return (f"¡Hola! Este mensaje es para recordarte que hoy es nuestra sesión "
                       f"de terapia a las {h} VET (venezuela time) 🩷\nTe espero en tu lugar seguro ✨")
MSG_START = "¡Ya estamos por iniciar! Es momento de ingresar en el waiting room 👀\nTe espero 🩷"
def msg_therapist(n): return f"Ya es la hora de atender a **{n}**."

# ── UTILIDADES ─────────────────────────────────────────────────────────
def norm(s: str) -> str:
    """minúsculas, sin acentos, sin espacios dobles — para comparar nombres."""
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", s).strip().lower()

def parse_dt(s: str):
    s = (s or "").strip().lower().replace(".", "")
    s = s.replace("a m", "am").replace("p m", "pm").replace("  ", " ")
    s = re.sub(r"(\d)(am|pm)", r"\1 \2", s)
    for f in ("%d/%m/%Y %I:%M %p","%d/%m/%Y %H:%M:%S","%d/%m/%Y %H:%M",
              "%Y-%m-%d %I:%M %p","%Y-%m-%d %H:%M:%S","%Y-%m-%d %H:%M","%d/%m/%Y %I %p"):
        try: return datetime.strptime(s, f)
        except ValueError: pass
    return None

def fmt_time(dt): return dt.strftime("%I:%M %p").lstrip("0")

async def get_csv(gid: str):
    async with aiohttp.ClientSession() as s:
        async with s.get(csv_url(gid)) as r:
            r.raise_for_status()
            return await r.text()

def rows_from(text: str, header_must_have: str):
    """Lee un CSV cuyo encabezado real puede no estar en la 1ª línea
    (las pestañas de mes tienen títulos arriba). Busca la fila que
    contenga 'header_must_have' y la usa como encabezado."""
    raw = list(csv.reader(io.StringIO(text)))
    hidx = None
    for i, row in enumerate(raw):
        if any(norm(c) == norm(header_must_have) for c in row):
            hidx = i; break
    if hidx is None:
        return []
    header = [c.strip() for c in raw[hidx]]
    out = []
    for row in raw[hidx+1:]:
        if not any(c.strip() for c in row):
            continue
        out.append({header[j]: (row[j] if j < len(row) else "") for j in range(len(header))})
    return out

async def discord_directory():
    """{nombre_normalizado: usuario_discord} desde Form_Responses."""
    text = await get_csv(GID_FORM)
    d = {}
    for r in rows_from(text, FR_NAME):
        full = norm(f"{r.get(FR_NAME,'')} {r.get(FR_SURNAME,'')}")
        user = (r.get(FR_DISCORD,"") or "").strip().lstrip("@")
        if full and user:
            d[full] = user
    return d

async def fetch_today_appointments():
    key = datetime.now(VET).strftime("%Y-%m")
    gid = MONTH_GIDS.get(key)
    if not gid:
        print(f"[SHEET] no tengo gid para el mes {key} — agrégalo en MONTH_GIDS"); return []
    text = await get_csv(gid)
    directory = await discord_directory()
    today = datetime.now(VET).date()
    out = []
    for r in rows_from(text, MM_PATIENT):
        name = (r.get(MM_PATIENT,"") or "").strip()
        dt = parse_dt(r.get(MM_DATETIME,""))
        if not (name and dt): continue
        if dt.date() != today: continue
        user = directory.get(norm(name))
        when = dt.replace(tzinfo=VET)
        out.append({"id": f"{key}|{name}|{r.get(MM_DATETIME,'')}", "name": name,
                    "user": user, "when": when, "hora_txt": fmt_time(when)})
    return out

# ── DISCORD ────────────────────────────────────────────────────────────
intents = discord.Intents.default(); intents.members = True

class ValeriaBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = discord.app_commands.CommandTree(self)
    async def setup_hook(self):
        guild = discord.Object(id=GUILD_ID)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)

bot = ValeriaBot()
_sent = set()
ADMIN = discord.app_commands.checks.has_permissions(manage_guild=True)

def find_member(guild, username):
    if not username: return None
    u = norm(username).lstrip("@"); base = u.split("#")[0]
    for m in guild.members:
        names = {norm(m.name)}
        if m.global_name: names.add(norm(m.global_name))
        if m.nick: names.add(norm(m.nick))
        if u in names or base in names:
            return m
    return None

async def dm(member, content, view=None):
    try: await member.send(content, view=view); return True
    except Exception as e: print(f"[DM] {member}: {e}"); return False

class AttendedView(discord.ui.View):
    def __init__(self, name): super().__init__(timeout=None); self.name = name
    @discord.ui.button(label="✅ Atendido", style=discord.ButtonStyle.success)
    async def mark(self, interaction, button):
        button.disabled = True
        button.label = f"✅ Atendido · {datetime.now(VET).strftime('%I:%M %p').lstrip('0')}"
        await interaction.response.edit_message(view=self)

def therapists(guild):
    role = guild.get_role(THERAPIST_ROLE_ID) or discord.utils.get(guild.roles, name=THERAPIST_ROLE)
    return list(role.members) if role else []

@tasks.loop(minutes=1)
async def scheduler():
    try: appts = await fetch_today_appointments()
    except Exception as e: print(f"[SHEET] error: {e}"); return
    guild = bot.get_guild(GUILD_ID)
    if not guild: print("[BOT] servidor no encontrado"); return
    now = datetime.now(VET)
    for a in appts:
        member = find_member(guild, a["user"]) if a["user"] else None
        diff = (a["when"] - now).total_seconds()/60.0
        k4 = (a["id"],"4h")
        if 0 <= diff - REMINDER_HOURS_BEFORE*60 < 1 and k4 not in _sent:
            if member: await dm(member, msg_4h(a["hora_txt"]))
            else: print(f"[AVISO] sin Discord para '{a['name']}' (recordatorio 4h)")
            _sent.add(k4)
        k0 = (a["id"],"start")
        if 0 <= diff < 1 and k0 not in _sent:
            if member: await dm(member, MSG_START)
            else: print(f"[AVISO] sin Discord para '{a['name']}' (inicio)")
            for th in therapists(guild):
                await dm(th, msg_therapist(a["name"]), view=AttendedView(a["name"]))
            _sent.add(k0)

@scheduler.before_loop
async def _w(): await bot.wait_until_ready()

@bot.event
async def on_ready():
    print(f"Valeria's Bot conectado como {bot.user}")
    if not scheduler.is_running(): scheduler.start()


# ── COMANDOS DE PRUEBA (solo admins) ───────────────────────────────────
@bot.tree.command(name="test_dm", description="[Prueba] El bot te envía a TI los dos mensajes de recordatorio")
@ADMIN
async def test_dm(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    ok1 = await dm(interaction.user, "🧪 (PRUEBA)\n" + msg_4h("3:00 PM"))
    ok2 = await dm(interaction.user, "🧪 (PRUEBA)\n" + MSG_START)
    await interaction.followup.send(
        ("✅ Te envié los 2 mensajes por DM." if ok1 and ok2
         else "⚠️ No pude enviarte DM. Revisa que tengas los MD abiertos para este servidor."),
        ephemeral=True)

@bot.tree.command(name="test_cita", description="[Prueba] Simula el aviso a los Therapist con el botón ✅")
@discord.app_commands.describe(nombre="Nombre del paciente de prueba")
@ADMIN
async def test_cita(interaction: discord.Interaction, nombre: str = "Paciente de Prueba"):
    await interaction.response.defer(ephemeral=True)
    ths = therapists(interaction.guild)
    if not ths:
        await interaction.followup.send(f"⚠️ No encontré a nadie con el rol **{THERAPIST_ROLE}**.", ephemeral=True); return
    n = 0
    for th in ths:
        if await dm(th, "🧪 (PRUEBA)\n" + msg_therapist(nombre), view=AttendedView(nombre)): n += 1
    await interaction.followup.send(f"✅ Enviado a {n} Therapist(s) con el botón de atendido.", ephemeral=True)

@bot.tree.command(name="test_hoja", description="[Prueba] Lee la hoja y muestra las citas de HOY (sin enviar nada)")
@ADMIN
async def test_hoja(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        appts = await fetch_today_appointments()
    except Exception as e:
        await interaction.followup.send(f"❌ Error al leer la hoja: `{e}`", ephemeral=True); return
    if not appts:
        await interaction.followup.send("No encontré citas para **hoy** en la pestaña del mes.", ephemeral=True); return
    lines = []
    for a in appts:
        m = find_member(interaction.guild, a["user"]) if a["user"] else None
        estado = f"→ {m.mention}" if m else (f"⚠️ usuario '{a['user']}' no está en el server" if a["user"] else "❌ sin usuario de Discord (no está en Form_Responses)")
        lines.append(f"• **{a['name']}** — {a['when'].strftime('%d/%m %I:%M %p')} {estado}")
    await interaction.followup.send("**Citas de hoy:**\n" + "\n".join(lines[:20]), ephemeral=True)

@bot.tree.command(name="test_proximas", description="[Prueba] Muestra las próximas citas de hoy y a qué hora avisa")
@ADMIN
async def test_proximas(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        appts = await fetch_today_appointments()
    except Exception as e:
        await interaction.followup.send(f"❌ Error: `{e}`", ephemeral=True); return
    now = datetime.now(VET)
    fut = sorted([a for a in appts if a["when"] >= now], key=lambda x: x["when"])
    if not fut:
        await interaction.followup.send("No quedan citas por venir hoy.", ephemeral=True); return
    lines = []
    for a in fut[:10]:
        rec4 = (a["when"] - __import__("datetime").timedelta(hours=REMINDER_HOURS_BEFORE)).strftime("%I:%M %p").lstrip("0")
        lines.append(f"• **{a['name']}** a las {a['hora_txt']} · recordatorio 4h a las {rec4}")
    await interaction.followup.send("**Próximas citas de hoy:**\n" + "\n".join(lines), ephemeral=True)

@test_dm.error
@test_cita.error
@test_hoja.error
@test_proximas.error
async def _test_err(interaction: discord.Interaction, error):
    try:
        await interaction.response.send_message("⛔ Solo admins pueden usar los comandos de prueba.", ephemeral=True)
    except Exception:
        pass


bot.run(TOKEN)
