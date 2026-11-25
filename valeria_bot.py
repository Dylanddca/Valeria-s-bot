# ╔══════════════════════════════════════════════════════════════════════╗
# ║  VALERIA'S BOT — Recordatorios de sesiones de terapia (HVN Heaven)    ║
# ╠══════════════════════════════════════════════════════════════════════╣
# ║  Lee las citas de la Google Sheet (publicada como CSV) y:            ║
# ║   · 4 h antes de la sesión → DM a la persona agendada                ║
# ║   · a la hora exacta       → DM a la persona (entrar al waiting room)║
# ║   · a la hora exacta       → DM a Valeria con botón ✅ "Atendido"     ║
# ║                                                                      ║
# ║  Busca a cada persona por su USUARIO de Discord dentro del servidor. ║
# ║  Hora de referencia: Venezuela (VET, UTC-4, sin horario de verano).  ║
# ╚══════════════════════════════════════════════════════════════════════╝
import os, csv, io
from datetime import datetime, timedelta, date, time
from zoneinfo import ZoneInfo

import aiohttp
import discord
from discord.ext import tasks

# ── CONFIGURACIÓN ──────────────────────────────────────────────────────
TOKEN     = os.environ["VALERIA_BOT_TOKEN"]        # token del bot (Railway → Variables)
GUILD_ID  = 1498414113703989258                    # servidor HVN Heaven
VET       = ZoneInfo("America/Caracas")            # UTC-4, sin DST

# A quién avisar "ya es hora de atender a X".
# Opción A (recomendada): el ID de usuario de Valeria → DM solo a ella.
# Opción B: deja THERAPIST_USER_ID = 0 y usa el rol de abajo (DM a todos los que lo tengan).
THERAPIST_USER_ID = 0                              # ← pon aquí el ID de Valeria, o deja 0
THERAPIST_ROLE    = "Therapist"                    # usado solo si THERAPIST_USER_ID == 0

SHEET_CSV_URL = os.environ.get("SHEET_CSV_URL",
    "https://docs.google.com/spreadsheets/d/1EJ_fLaHFoolkCM6m0lj3mdzr3aI8NMsjcPLqtnx_8JQ/export?format=csv&gid=560847215")

# ── COLUMNAS DE LA HOJA (coinciden con tu form) ───────────────────────
COL_NAME     = "Nombre"                            # nombre de la persona
COL_DISCORD  = "Usuario de Discord"                # su usuario de Discord (texto)
COL_DATETIME = "Fecha y hora de la sesión"         # fecha + hora juntas (ej. 24/11/2026 3:00PM)
# Si el encabezado real de la columna H difiere, ajústalo arriba.

REMINDER_HOURS_BEFORE = 4

# ── MENSAJES ───────────────────────────────────────────────────────────
def msg_4h(hora_txt: str) -> str:
    return (f"¡Hola! Este mensaje es para recordarte que hoy es nuestra sesión "
            f"de terapia a las {hora_txt} VET (venezuela time) 🩷\n"
            f"Te espero en tu lugar seguro ✨")

MSG_START = ("¡Ya estamos por iniciar! Es momento de ingresar en el waiting room 👀\n"
             "Te espero 🩷")

def msg_therapist(nombre: str) -> str:
    return f"Ya es la hora de atender a **{nombre}**."

# ── BOT ────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.members = True                             # necesario para buscar por usuario
bot = discord.Client(intents=intents)

_sent: set = set()                                 # evita duplicados: (id_cita, tipo)

# ── PARSEO DE FECHA/HORA ───────────────────────────────────────────────
def _parse_datetime(s: str):
    """Acepta '24/11/2026 3:00PM', '24/11/2026 14:00:00', '27/11/2026 14:00', etc."""
    s = s.strip().upper().replace(".", "")
    s = s.replace("PM", " PM").replace("AM", " AM").replace("  ", " ").strip()
    fmts = ["%d/%m/%Y %I:%M %p", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M",
            "%Y-%m-%d %I:%M %p", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
            "%d/%m/%Y %I %p"]
    for f in fmts:
        try: return datetime.strptime(s, f)
        except ValueError: pass
    return None

def _fmt_time(dt: datetime) -> str:
    return dt.strftime("%I:%M %p").lstrip("0")      # 3:00 PM / 10:30 AM

# ── LEER LA HOJA ───────────────────────────────────────────────────────
async def fetch_appointments() -> list:
    async with aiohttp.ClientSession() as s:
        async with s.get(SHEET_CSV_URL) as r:
            r.raise_for_status()
            text = await r.text()
    reader = csv.DictReader(io.StringIO(text))
    today = datetime.now(VET).date()
    out = []
    for row in reader:
        raw = (row.get(COL_DATETIME, "") or "").strip()
        dt = _parse_datetime(raw)
        user = (row.get(COL_DISCORD, "") or "").strip().lstrip("@")
        name = (row.get(COL_NAME, "") or "").strip()
        if not (dt and user):                       # fila incompleta → saltar
            continue
        when = dt.replace(tzinfo=VET)
        if when.date() != today:                    # solo las de hoy
            continue
        out.append({"id": f"{raw}|{user}", "name": name, "user": user,
                    "when": when, "hora_txt": _fmt_time(when)})
    return out

# ── BUSCAR A LA PERSONA POR SU USUARIO ─────────────────────────────────
def find_member(guild: discord.Guild, username: str):
    u = username.lower().lstrip("@")
    base = u.split("#")[0]                           # por si puso usuario#1234
    for m in guild.members:
        names = {m.name.lower()}
        if m.global_name: names.add(m.global_name.lower())
        if m.nick: names.add(m.nick.lower())
        if u in names or base in names or f"{m.name.lower()}#{m.discriminator}" == u:
            return m
    return None

async def dm_member(member: discord.Member, content: str, view=None) -> bool:
    try:
        await member.send(content, view=view)
        return True
    except Exception as e:
        print(f"[DM] no pude enviar a {member}: {e}")
        return False

# ── BOTÓN "ATENDIDO" ───────────────────────────────────────────────────
class AttendedView(discord.ui.View):
    def __init__(self, name: str):
        super().__init__(timeout=None)
        self.name = name
    @discord.ui.button(label="✅ Atendido", style=discord.ButtonStyle.success)
    async def mark(self, interaction: discord.Interaction, button: discord.ui.Button):
        button.disabled = True
        button.label = f"✅ Atendido · {datetime.now(VET).strftime('%I:%M %p').lstrip('0')}"
        await interaction.response.edit_message(view=self)

def therapist_targets(guild: discord.Guild) -> list:
    if THERAPIST_USER_ID:
        m = guild.get_member(THERAPIST_USER_ID)
        return [m] if m else []
    role = discord.utils.get(guild.roles, name=THERAPIST_ROLE)
    return list(role.members) if role else []

# ── BUCLE PRINCIPAL (cada minuto) ──────────────────────────────────────
@tasks.loop(minutes=1)
async def scheduler():
    try:
        appts = await fetch_appointments()
    except Exception as e:
        print(f"[SHEET] error al leer la hoja: {e}")
        return
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        print("[BOT] no encuentro el servidor"); return
    now = datetime.now(VET)
    for a in appts:
        member = find_member(guild, a["user"])
        diff_min = (a["when"] - now).total_seconds() / 60.0
        # 4 horas antes
        k4 = (a["id"], "4h")
        if 0 <= diff_min - REMINDER_HOURS_BEFORE*60 < 1 and k4 not in _sent:
            if member:
                await dm_member(member, msg_4h(a["hora_txt"]))
            else:
                print(f"[AVISO] no encontré a '{a['user']}' (recordatorio 4h de {a['name']})")
            _sent.add(k4)
        # a la hora exacta
        k0 = (a["id"], "start")
        if 0 <= diff_min < 1 and k0 not in _sent:
            if member:
                await dm_member(member, MSG_START)
            else:
                print(f"[AVISO] no encontré a '{a['user']}' (inicio de {a['name']})")
            for th in therapist_targets(guild):
                await dm_member(th, msg_therapist(a["name"]), view=AttendedView(a["name"]))
            _sent.add(k0)

@scheduler.before_loop
async def _wait(): await bot.wait_until_ready()

@bot.event
async def on_ready():
    print(f"Valeria's Bot conectado como {bot.user}")
    if not scheduler.is_running():
        scheduler.start()

bot.run(TOKEN)

# ════════════════════════════════════════════════════════════════════════
#  AJUSTES RÁPIDOS
#  · THERAPIST_USER_ID : pon el ID de Valeria para que el aviso le llegue
#    solo a ella (clic derecho sobre su nombre en Discord → Copiar ID de
#    usuario; necesitas Modo Desarrollador activado en Ajustes → Avanzado).
#  · PUBLICAR LA HOJA : Archivo → Compartir → Publicar en la web →
#    pestaña Form_Responses → formato CSV → copia esa URL en SHEET_CSV_URL.
# ════════════════════════════════════════════════════════════════════════
