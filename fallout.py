# coding: utf-8
import argparse
import asyncio

import httpx
import io
import locale
import logging
import os
import peewee as pw
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from dateutil.parser import parse as parse_date
from discord import utils, Colour, File, Intents
from discord.embeds import Embed
from discord.ext import commands
import chat_exporter


DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
DISCORD_LOCALE = os.environ.get("DISCORD_LOCALE") or "fr_FR"
DISCORD_OPERATOR = OP = os.environ.get("DISCORD_OPERATOR") or "!"
DISCORD_ADMIN_ROLE = os.environ.get("DISCORD_ROLE") or "MJ"
DISCORD_PLAYER_ROLE = os.environ.get("DISCORD_PLAYER") or "PJ"
DISCORD_CATEGORY = os.environ.get("DISCORD_CATEGORY") or "Joueurs"
DISCORD_WORLD = os.environ.get("DISCORD_WORLD") or "Monde"
FALLOUT_TOKEN = os.environ.get("FALLOUT_TOKEN")
FALLOUT_URL = os.environ.get("FALLOUT_URL")
FALLOUT_DATE = parse_date(os.environ.get("FALLOUT_DATE") or datetime.now().isoformat(), dayfirst=True)
FALLOUT_CAMPAIGN = int(os.environ.get("FALLOUT_CAMPAIGN") or 0) or None

REGEX_FLAGS = re.IGNORECASE | re.MULTILINE

log_handler = logging.StreamHandler()
log_handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)7s: %(message)s"))

pw_logger = logging.getLogger("peewee")
pw_logger.setLevel(logging.DEBUG)
pw_logger.addHandler(log_handler)

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
logger.addHandler(log_handler)

db = pw.SqliteDatabase("fallout.db")


class Channel(pw.Model):
    id = pw.BigIntegerField(primary_key=True)
    name = pw.CharField()
    topic = pw.TextField(null=True)
    campaign_id = pw.IntegerField(null=True)
    date = pw.DateTimeField(null=True)

    class Meta:
        database = db


class User(pw.Model):
    id = pw.BigIntegerField(primary_key=True)
    name = pw.CharField()
    level = pw.IntegerField(default=0)
    player_id = pw.IntegerField(null=True)
    character_id = pw.IntegerField(null=True)
    my_channel_id = pw.BigIntegerField(null=True)
    channel = pw.ForeignKeyField(Channel, null=True)

    class Meta:
        database = db


@dataclass
class Creature:
    id: int
    name: str
    character_id: int
    campaign_id: int
    my_channel_id: int = 0


class Parser(argparse.ArgumentParser):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.message = ""

    def parse_args(self, args=None, namespace=None):
        result = self.parse_known_args(args, namespace)
        if self.message:
            return
        args, argv = result
        return args

    def print_help(self, file=None):
        if self.message:
            return
        self.message = self.format_help()

    def error(self, message):
        if self.message:
            return
        self.message = self.format_usage() + message

    def exit(self, status=0, message=None):
        pass


class Fallout(commands.Cog):

    INDICES = {
        0: "0️",
        1: "1️",
        2: "2️",
        3: "3️",
        4: "4️",
        5: "5️",
        6: "6️",
        7: "7️",
        8: "8️",
        9: "9️",
        10: "🔟",
    }
    STATUS = {(0, 0): "⚠️", (1, 0): "🆗", (0, 1): "💀", (1, 1): "🏆"}
    COLORS = {(0, 0): "orange", (1, 0): "green", (0, 1): "red", (1, 1): "blue"}
    SPECIAL = {
        "s": "strength",
        "str": "strength",
        "for": "strength",
        "force": "strength",
        "strength": "strength",
        "p": "perception",
        "per": "perception",
        "perception": "perception",
        "e": "endurance",
        "end": "endurance",
        "endurance": "endurance",
        "c": "charisma",
        "cha": "charisma",
        "charisme": "charisma",
        "charisma": "charisma",
        "i": "intelligence",
        "int": "intelligence",
        "intelligence": "intelligence",
        "a": "agility",
        "agl": "agility",
        "agilité": "agility",
        "agility": "agility",
        "l": "luck",
        "lck": "luck",
        "chance": "luck",
        "luck": "luck",
    }
    SKILLS = {
        "sg": "small_guns",
        "small": "small_guns",
        "light": "small_guns",
        "léger": "small_guns",
        "légère": "small_guns",
        "légères": "small_guns",
        "small_guns": "small_guns",
        "bg": "big_guns",
        "big": "big_guns",
        "heavy": "big_guns",
        "lourd": "big_guns",
        "lourde": "big_guns",
        "lourdes": "big_guns",
        "big_guns": "big_guns",
        "ew": "energy_weapons",
        "energy": "energy_weapons",
        "energie": "energy_weapons",
        "laser": "energy_weapons",
        "plasma": "energy_weapons",
        "energy_weapons": "energy_weapons",
        "un": "unarmed",
        "hand": "unarmed",
        "main": "unarmed",
        "mains": "unarmed",
        "cac": "unarmed",
        "contact": "unarmed",
        "unarmed": "unarmed",
        "mw": "melee_weapons",
        "melee": "melee_weapons",
        "mêlée": "melee_weapons",
        "melee_weapons": "melee_weapons",
        "th": "throwing",
        "throw": "throwing",
        "jet": "throwing",
        "lance": "throwing",
        "lancer": "throwing",
        "grenade": "throwing",
        "throwing": "throwing",
        "at": "athletics",
        "ath": "athletics",
        "athletism": "athletics",
        "athlétisme": "athletics",
        "athletics": "athletics",
        "dt": "detection",
        "detect": "detection",
        "search": "detection",
        "détection": "detection",
        "recherche": "detection",
        "rechercher": "detection",
        "trouver": "detection",
        "detection": "detection",
        "fa": "first_aid",
        "first": "first_aid",
        "aid": "first_aid",
        "premiers": "first_aid",
        "secours": "first_aid",
        "first_aid": "first_aid",
        "do": "doctor",
        "doc": "doctor",
        "med": "doctor",
        "médecin": "doctor",
        "médecine": "doctor",
        "doctor": "doctor",
        "ch": "chems",
        "chem": "chems",
        "chimie": "chems",
        "pharma": "chems",
        "pharmacologie": "chems",
        "chems": "chems",
        "sn": "sneak",
        "discret": "sneak",
        "discrétion": "sneak",
        "cacher": "sneak",
        "sneak": "sneak",
        "lp": "lockpick",
        "lock": "lockpick",
        "crochetage": "lockpick",
        "lockpick": "lockpick",
        "st": "steal",
        "pp": "steal",
        "vol": "steal",
        "voler": "steal",
        "pickpocket": "steal",
        "steal": "steal",
        "tr": "traps",
        "trap": "traps",
        "piège": "traps",
        "pièges": "traps",
        "traps": "traps",
        "ex": "explosives",
        "exp": "explosives",
        "explosif": "explosives",
        "explosifs": "explosives",
        "explosives": "explosives",
        "sc": "science",
        "science": "science",
        "rp": "repair",
        "rep": "repair",
        "mech": "repair",
        "meca": "repair",
        "méca": "repair",
        "mécanisme": "repair",
        "réparer": "repair",
        "réparation": "repair",
        "craft": "repair",
        "repair": "repair",
        "cp": "computers",
        "comp": "computers",
        "info": "computers",
        "prog": "computers",
        "programmer": "computers",
        "pirater": "computers",
        "piratage": "computers",
        "hacker": "computers",
        "hacking": "computers",
        "computers": "computers",
        "el": "electronics",
        "elec": "electronics",
        "electro": "electronics",
        "electronics": "electronics",
        "sp": "speech",
        "discours": "speech",
        "pe": "speech",
        "persuader": "speech",
        "parler": "speech",
        "convaincre": "speech",
        "speech": "speech",
        "de": "deception",
        "tromper": "deception",
        "tromperie": "deception",
        "mentir": "deception",
        "deception": "deception",
        "ba": "barter",
        "marchandage": "barter",
        "commerce": "barter",
        "négocier": "barter",
        "barter": "barter",
        "su": "survival",
        "survie": "survival",
        "outdoorsman": "survival",
        "survival": "survival",
        "kn": "knowledge",
        "connaissance": "knowledge",
        "culture": "knowledge",
        "knowledge": "knowledge",
    }
    STATS = {**SPECIAL, **SKILLS}
    BODY_PARTS = {
        "t": "torso",
        "torse": "torso",
        "corps": "torso",
        "torso": "torso",
        "l": "legs",
        "j": "legs",
        "jambe": "legs",
        "jambes": "legs",
        "p": "legs",
        "pied": "legs",
        "pieds": "legs",
        "legs": "legs",
        "a": "arms",
        "b": "arms",
        "bras": "arms",
        "m": "arms",
        "main": "arms",
        "mains": "arms",
        "arms": "arms",
        "h": "head",
        "tête": "head",
        "head": "head",
        "e": "eyes",
        "oeil": "eyes",
        "y": "eyes",
        "yeux": "eyes",
        "eyes": "eyes",
    }
    DAMAGES = {
        "n": "normal",
        "-": "normal",
        "normal": "normal",
        "l": "laser",
        "laser": "laser",
        "p": "plasma",
        "plasma": "plasma",
        "e": "explosive",
        "exp": "explosive",
        "explosive": "explosive",
        "f": "fire",
        "feu": "fire",
        "fire": "fire",
        "ps": "poison",
        "poison": "poison",
        "r": "radiation",
        "rad": "radiation",
        "radiation": "radiation",
        "gc": "gas_contact",
        "gas_contact": "gas_contact",
        "gi": "gas_inhaled",
        "gas_inhaled": "gas_inhaled",
        "raw": "raw",
        "t": "thirst",
        "soif": "thirst",
        "+t": "thirst",
        "thirst": "thirst",
        "h": "hunger",
        "faim": "hunger",
        "+h": "hunger",
        "hunger": "hunger",
        "s": "sleep",
        "sommeil": "sleep",
        "+s": "sleep",
        "sleep": "sleep",
        "ht": "heal_thirst",
        "boire": "heal_thirst",
        "-t": "heal_thirst",
        "heal_thirst": "heal_thirst",
        "hh": "heal_hunger",
        "manger": "heal_hunger",
        "-h": "heal_hunger",
        "heal_hunger": "heal_hunger",
        "hs": "heal_sleep",
        "dormir": "heal_sleep",
        "-s": "heal_sleep",
        "heal_sleep": "heal_sleep",
        "+": "heal",
        "soin": "heal",
        "heal": "heal",
        "hr": "heal_rad",
        "heal_rad": "heal_rad",
        "gain": "add_money",
        "money": "add_money",
        "argent": "add_money",
        "+$": "add_money",
        "add_money": "add_money",
        "loss": "remove_money",
        "perte": "remove_money",
        "-$": "remove_money",
        "remove_money": "remove_money",
        "good": "add_karma",
        "karma": "add_karma",
        "add_karma": "add_karma",
        "evil": "remove_karma",
        "bad": "remove_karma",
        "remove_karma": "remove_karma",
    }

    def __init__(self, bot):
        self.bot = bot
        self.session = httpx.AsyncClient()
        self.session.headers = {
            "Content-Type": "application/json",
            "Authorization": f"TOKEN {FALLOUT_TOKEN}",
            "Accept-Language": "fr",
        }
        self.users = {}
        self.channels = {}
        self.creatures = {}

    @commands.Cog.listener()
    async def on_ready(self):
        pass  # chat_exporter.init_exporter(self.bot)

    @commands.Cog.listener()
    async def on_member_update(self, before, after):
        if not after.bot:
            await self.get_user(after)

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot or not message.guild:
            return
        await self.get_user(message.author)

    @commands.command()
    @commands.guild_only()
    async def new(self, ctx, *args):
        """Crée un nouveau personnage avec les statistiques choisies."""
        await ctx.message.delete()
        user = await self.get_user(ctx.author)
        choices = tuple(range(1, 11))
        command = f"{ctx.prefix}{ctx.command.name}"
        parser = Parser(
            prog=command,
            description="Crée un nouveau personnage avec les statistiques choisies.",
            epilog="La somme totale des statistiques doit être strictement égale à 40.",
        )
        parser.add_argument("strength", metavar="S", type=int, choices=choices, help="force (entre 1 et 10)")
        parser.add_argument("perception", metavar="P", type=int, choices=choices, help="perception (entre 1 et 10)")
        parser.add_argument("endurance", metavar="E", type=int, choices=choices, help="endurance (entre 1 et 10)")
        parser.add_argument("charisma", metavar="C", type=int, choices=choices, help="charisme (entre 1 et 10)")
        parser.add_argument("intelligence", metavar="I", type=int, choices=choices, help="intelligence (entre 1 et 10)")
        parser.add_argument("agility", metavar="A", type=int, choices=choices, help="agilité (entre 1 et 10)")
        parser.add_argument("luck", metavar="L", type=int, choices=choices, help="chance (entre 1 et 10)")
        parser.add_argument("--tag", "-t", dest="tag_skills", type=str, nargs="*", help="spécialité (maximum 3)")
        parser.add_argument("--user", "-u", dest="player", type=str, help="Utilisateur")
        args = parser.parse_args(args)
        if parser.message:
            await ctx.author.send(f"```{parser.message}```")
            return

        if not args.player and user.character_id:
            await ctx.author.send(f"⛔ Vous avez déjà créé votre personnage.")
            return
        data = vars(args).copy()
        if sum(data[stats] for stats in self.SPECIAL.values()) != 40 and not self.has_role(ctx.author):
            await ctx.author.send(f"⛔ La somme totale de vos statistiques doit valoir exactement **40**.")
            return
        data["tag_skills"] = []
        if args.tag_skills:
            data["tag_skills"] = [
                self.SKILLS[t] for t in map(lambda e: e.strip().lower(), args.tag_skills) if t in self.SKILLS
            ]
            if len(args.tag_skills) > 3 and not self.has_role(ctx.author):
                await ctx.author.send(f"⛔ Vous ne pouvez sélectionner que 3 spécialités au maximum.")
                return
        player = data.pop("player", None)
        if self.has_role(ctx.author) and player:
            player = await self.get_user(player)
            if player and player.player_id:
                data["player"] = player.player_id
        await self.create_user(user, **data)
        url = await self.get_character_url(user)
        await ctx.author.send(f"✅ Votre personnage a été créé avec succès ! Fiche de personnage : {url}")
        player_role = utils.get(ctx.channel.guild.roles, name=DISCORD_PLAYER_ROLE)
        await user.user.add_roles(player_role, reason="Nouveau joueur")
        # Create private channel
        channel_name = user.name.lower().replace("#", "").replace(" ", "-").replace("_", "-")
        category = utils.get(ctx.channel.guild.categories, name=DISCORD_CATEGORY)
        new_channel = utils.get(ctx.channel.guild.text_channels, name=channel_name, category=category)
        if not new_channel:
            new_channel = await ctx.channel.guild.create_text_channel(channel_name, category=category, topic=user.name)
            everyone = utils.get(ctx.channel.guild.roles, name="@everyone")
            gm_role = utils.get(ctx.channel.guild.roles, name=DISCORD_ADMIN_ROLE)
            await new_channel.set_permissions(everyone, read_messages=False)
            await new_channel.set_permissions(gm_role, read_messages=True)
            await new_channel.set_permissions(user.user, read_messages=True)
            user.my_channel_id = new_channel.id
            user.save(only=("my_channel_id",))

    @commands.command()
    @commands.guild_only()
    async def link(self, ctx, *args):
        """Retourne un lien vers votre fiche de personnage."""
        await ctx.message.delete()
        user = await self.get_user(ctx.author)
        if not user or not user.player_id:
            return
        url = await self.get_character_url(user)
        if not user.character_id:
            await ctx.author.send(
                f"⚠️ Vous n'avez pas encore de personnage actif, tapez `{ctx.prefix}new` pour en créer un."
            )
        await ctx.author.send(f"🔗 Accéder à votre fiche de personnage : {url}")

    @commands.command()
    @commands.guild_only()
    @commands.has_role(DISCORD_ADMIN_ROLE)
    async def move(self, ctx, *args):
        """Déplace un ou plusieurs joueurs dans un autre canal."""
        await ctx.message.delete()
        user = await self.get_user(ctx.author)
        command = f"{ctx.prefix}{ctx.command.name}"
        parser = Parser(prog=command, description="Déplace un ou plusieurs joueurs dans un autre canal.")
        parser.add_argument("channel", type=str, help="Nom du canal de destination")
        parser.add_argument("players", metavar="player", type=str, nargs="+", help="Nom du joueur")
        parser.add_argument("--topic", "-t", type=str, help="Description du canal")
        parser.add_argument("--date", "-d", type=str, help="Date ")
        args = parser.parse_args(args)
        if parser.message:
            await ctx.author.send(f"```{parser.message}```")
            return

        channel_id = self.extract_id(args.channel)
        category = utils.get(ctx.channel.guild.categories, name=DISCORD_WORLD)
        if not channel_id:
            channel_name = args.channel.lower().replace("#", "").replace(" ", "-").replace("_", "-")
            new_channel = utils.get(ctx.channel.guild.text_channels, name=channel_name, category=category)
            if not new_channel:
                new_channel = await ctx.channel.guild.create_text_channel(
                    channel_name, category=category, topic=args.topic
                )
                everyone = utils.get(ctx.channel.guild.roles, name="@everyone")
                await new_channel.set_permissions(everyone, read_messages=False)
        else:
            new_channel = self.bot.get_channel(channel_id)
        _old_channel = await self.get_channel(ctx.channel, user) if ctx.channel.category == category else None
        _new_channel = await self.get_channel(
            new_channel, user, date=parse_date(args.date, dayfirst=True) if args.date else None
        )
        players_in_channel = User.select().where(User.channel == _new_channel)
        if _old_channel and _new_channel and players_in_channel.count() == 0 and _new_channel.date != _old_channel.date:
            _new_channel.date = _old_channel.date
            _new_channel.save(only=("date",))
            await self.request(
                f"campaign/{_new_channel.campaign_id}/",
                method="patch",
                data=dict(
                    start_game_date=_new_channel.date.isoformat(), current_game_date=_new_channel.date.isoformat()
                ),
            )
        if new_channel.members:
            deleted_messages = await new_channel.purge()
            if deleted_messages:
                transcript = await chat_exporter.raw_export(new_channel, deleted_messages, set_timezone="Europe/Paris")
                if transcript:
                    for player in players_in_channel:
                        if not player.my_channel_id:
                            continue
                        channel = self.bot.get_channel(player.my_channel_id)
                        if channel:
                            file = File(io.BytesIO(transcript.encode()), filename=f"{new_channel.name}.html")
                            await channel.send(
                                f"🚪 Un ou plusieurs joueurs sont entrés dans **#{new_channel.name}**, "
                                f"les messages du canal ont été purgés par soucis de discrétion.\n"
                                f"⌚ Vous pouvez retrouver l'historique des messages ci-dessous :",
                                file=file,
                            )
        arriving_users, leaving_users = [], {}
        for player_name in args.players:
            player = await self.get_user(player_name)
            if not player:
                logger.warning(f"Player '{player_name}' not found!")
                continue
            if player and player.channel_id:
                old_channel = self.bot.get_channel(player.channel_id)
                if old_channel:
                    if player.my_channel_id:
                        channel = self.bot.get_channel(player.my_channel_id)
                        transcript = await chat_exporter.export(old_channel, set_timezone="Europe/Paris")
                        if channel and transcript:
                            file = File(io.BytesIO(transcript.encode()), filename=f"{old_channel.name}.html")
                            await channel.send(
                                f"🚪 Vous avez été déplacé de **#{old_channel.name}** "
                                f"vers **#{new_channel.name}**.\n"
                                f"⌚ Vous pouvez retrouver l'historique des messages ci-dessous :",
                                file=file,
                            )
                    await old_channel.set_permissions(player.user, overwrite=None)
                    leaving_users.setdefault(old_channel.id, []).append(player)
            player.channel_id = new_channel.id
            player.save(only=("channel_id",))
            arriving_users.append(player)
            await new_channel.set_permissions(player.user, read_messages=True)
            await self.request(
                f"character/{player.character_id}/",
                method="patch",
                data=dict(campaign=_new_channel.campaign_id),
            )
        gm_role = utils.get(ctx.channel.guild.roles, name=DISCORD_ADMIN_ROLE)
        await new_channel.set_permissions(gm_role, read_messages=True)
        for channel_id, users in leaving_users.items():
            old_channel = self.bot.get_channel(channel_id)
            if not old_channel:
                continue
            user_names = ", ".join([f"<@{user.id}>" for user in users])
            if len(users) > 1:
                await old_channel.send(f"📤 {user_names} partent de <#{old_channel.id}>.")
                continue
            await old_channel.send(f"📤 {user_names} part de <#{old_channel.id}>.")
        user_names = ", ".join([f"<@{user.id}>" for user in arriving_users])
        if len(arriving_users) > 1:
            await new_channel.send(f"📥 {user_names} arrivent dans <#{new_channel.id}>.")
            return
        await new_channel.send(f"📥 {user_names} arrive dans <#{new_channel.id}>.")

    @commands.command()
    @commands.guild_only()
    @commands.has_role(DISCORD_ADMIN_ROLE)
    async def roll(self, ctx, *args):
        """Réalise un jet de compétence ou de S.P.E.C.I.A.L. pour un ou plusieurs joueurs."""
        await ctx.message.delete()
        user = await self.get_user(ctx.author)
        command = f"{ctx.prefix}{ctx.command.name}"
        parser = Parser(
            prog=command, description="Réalise un jet de compétence ou de S.P.E.C.I.A.L. pour un ou plusieurs joueurs."
        )
        parser.add_argument("stats", type=str, help="Nom ou code de la statistique")
        parser.add_argument("players", metavar="player", type=str, nargs="+", help="Nom du joueur")
        parser.add_argument("--modifier", "-m", metavar="MOD", default=0, type=int, help="Modificateur")
        parser.add_argument("--xp", "-x", action="store_true", default=False, help="Expérience ?")
        parser.add_argument("--reason", "-r", type=str, help="Explication")
        args = parser.parse_args(args)
        if parser.message:
            await ctx.author.send(f"```{parser.message}```")
            return

        args.stats = self.try_get(args.stats, self.STATS)
        data = vars(args).copy()
        data.pop("reason")
        data.pop("players")
        for player_name in args.players:
            player = await self.get_user(player_name)
            if not player or not player.character_id:
                continue
            ret = await self.request(f"character/{player.character_id}/roll/", method="post", data=data)
            if ret is None:
                await ctx.author.send(f"⚠️ Une erreur s'est produite pendant l'exécution de la commande `{command}`.")
                return
            success, critical, stats, label = ret["success"], ret["critical"], ret["stats_display"], ret["long_label"]
            experience, level_up, level = ret["experience"], ret["level_up"], ret["character"]["level"]
            if args.reason:
                message = f"> {args.reason}\n\n{self.STATUS[success, critical]}  <@{player.id}> : {label}"
            else:
                message = f"{self.STATUS[success, critical]}  <@{player.id}> : {label}"
            if level_up:
                message = f"{message}\n🆙 Passage au niveau **{level}** !"
            elif experience:
                message = f"{message}\n⬆️ **+{experience}** points d'expérience gagnés."
            # await ctx.channel.send(message)
            embed = Embed(
                title=f"🎲 Test de {stats}",
                description=message,
                color=self.get_color(self.COLORS[success, critical]),
            )
            await ctx.channel.send(embed=embed)

    @commands.command()
    @commands.guild_only()
    @commands.has_role(DISCORD_ADMIN_ROLE)
    async def damage(self, ctx, *args):
        """Inflige des dégâts à un ou plusieurs joueurs."""
        await ctx.message.delete()
        user = await self.get_user(ctx.author)
        command = f"{ctx.prefix}{ctx.command.name}"
        parser = Parser(prog=command, description="Inflige des dégâts à un ou plusieurs joueurs.")
        parser.add_argument("min_damage", metavar="min", type=int, default=0, help="Dégâts minimals")
        parser.add_argument("max_damage", metavar="max", type=int, default=0, help="Dégâts maximals")
        parser.add_argument("raw_damage", metavar="raw", type=int, default=0, help="Dégâts bruts")
        parser.add_argument(
            "--type",
            "-t",
            metavar="TYPE",
            dest="damage_type",
            type=str,
            default="normal",
            help="Type de dégâts (par défaut : normal)",
        )
        parser.add_argument(
            "--part",
            "-p",
            metavar="PART",
            dest="body_part",
            type=str,
            # default="torso",
            help="Partie du corps touchée (par défaut: torse)",
        )
        parser.add_argument(
            "--threshold",
            "-m",
            metavar="MOD",
            dest="threshold_modifier",
            type=int,
            default=0,
            help="Modificateur d'absorption",
        )
        parser.add_argument(
            "--resistance",
            "-r",
            metavar="MOD",
            dest="resistance_modifier",
            type=int,
            default=0,
            help="Modificateur de résistance",
        )
        parser.add_argument("--simulation", "-s", action="store_true", default=False, help="Simulation ?")
        parser.add_argument("--reason", "-r", type=str, help="Explication")
        parser.add_argument("players", metavar="player", type=str, nargs="+", help="Nom du joueur")
        args = parser.parse_args(args)
        if parser.message:
            await ctx.author.send(f"```{parser.message}```")
            return

        args.damage_type = self.try_get(args.damage_type, self.DAMAGES) if args.damage_type else "normal"
        args.body_part = self.try_get(args.body_part, self.BODY_PARTS) if args.body_part else None
        data = vars(args).copy()
        data.pop("reason")
        data.pop("players")
        for player_name in args.players:
            player = await self.get_user(player_name)
            if not player or not player.character_id:
                continue
            ret = await self.request(f"character/{player.character_id}/damage/", method="post", data=data)
            if ret is None:
                await ctx.author.send(f"⚠️ Une erreur s'est produite pendant l'exécution de la commande `{command}`.")
                return
            if args.reason:
                message = f"> {args.reason}\n\n<@{player.id}> a reçu **{ret['long_label']}**"
            else:
                message = f"<@{player.id}> a reçu **{ret['long_label']}**"
            message = f"{message} et a été **tué** !" if ret["character"]["health"] <= 0 else f"{message}."
            # await ctx.channel.send(message)
            embed = Embed(
                title=f"{ret['icon']}  {ret['label'].capitalize()}",
                description=message,
                color=self.get_color("green") if ret["is_heal"] else self.get_color("red"),
            )
            await ctx.channel.send(embed=embed)

    @commands.command()
    @commands.guild_only()
    @commands.has_role(DISCORD_ADMIN_ROLE)
    async def fight(self, ctx, *args):
        """Fait s'affronter deux joueurs entre eux."""
        await ctx.message.delete()
        user = await self.get_user(ctx.author)
        command = f"{ctx.prefix}{ctx.command.name}"
        parser = Parser(prog=command, description="Fait s'affronter deux joueurs entre eux.")
        parser.add_argument("attacker", type=str, help="Joueur attaquant")
        parser.add_argument("target", metavar="defender", type=str, help="Joueur défenseur")
        parser.add_argument(
            "--range",
            "-r",
            metavar="RANGE",
            dest="target_range",
            type=int,
            default=1,
            help="Distance entre les deux joueurs",
        )
        parser.add_argument(
            "--part",
            "-p",
            metavar="PART",
            dest="target_body_part",
            type=str,
            default="torso",
            help="Partie du corps touchée (par défaut: torse)",
        )
        parser.add_argument(
            "--modifier",
            "-m",
            metavar="MOD",
            dest="hit_chance_modifier",
            type=int,
            default=0,
            help="Modificateur de précision",
        )
        parser.add_argument(
            "--action",
            "-a",
            dest="is_action",
            action="store_true",
            default=False,
            help="Action ?",
        )
        parser.add_argument(
            "--weapon",
            "-w",
            metavar="WEAPON",
            dest="weapon_type",
            type=str,
            default="primary",
            help="Type d'arme",
        )
        parser.add_argument(
            "--success",
            "-f",
            dest="force_success",
            action="store_true",
            default=False,
            help="Succès ?",
        )
        parser.add_argument(
            "--critical",
            "-c",
            dest="force_critical",
            action="store_true",
            default=False,
            help="Critique ?",
        )
        parser.add_argument(
            "--raw",
            "-x",
            dest="force_raw_damage",
            action="store_true",
            default=False,
            help="Dégâts bruts ?",
        )
        parser.add_argument(
            "--simulation",
            "-s",
            action="store_true",
            default=False,
            help="Simulation ?",
        )
        args = parser.parse_args(args)
        if parser.message:
            await ctx.author.send(f"```{parser.message}```")
            return

        args.body_part = self.try_get(args.target_body_part, self.BODY_PARTS)
        attacker, target = await self.get_user(args.attacker), await self.get_user(args.target)
        if not attacker or not target or not attacker.character_id or not target.character_id:
            await ctx.author.send(f"⚠️ Les joueurs sélectionnés ne peuvent combattre car ils n'ont pas de personnage.")
            return
        data = vars(args).copy()
        data.pop("attacker")
        data["target"] = target.character_id
        ret = await self.request(f"character/{attacker.character_id}/fight/", method="post", data=data)
        if ret is None:
            await ctx.author.send(f"⚠️ Une erreur s'est produite pendant l'exécution de la commande `{command}`.")
            return
        attacker = f"**<@{attacker.id}>**" if attacker.id else f"**{attacker.name}** (*{attacker.character_id}*)"
        target = f"**<@{target.id}>**" if target.id else f"**{target.name}** (*{target.character_id}*)"
        success, critical, label = ret["success"], ret["critical"], ret["long_label"]
        experience, level_up, level = ret["experience"], ret["level_up"], ret["character"]["level"]
        message = f"{self.STATUS[success, critical]}  {attacker} vs. {target} : {label}"
        if level_up:
            message = f"{message}\n🆙 Passage au niveau **{level}** !"
        elif experience:
            message = f"{message}\n⬆️ **+{experience}** points d'expérience gagnés."
        # await ctx.channel.send(message)
        embed = Embed(
            title=f"⚔️ Attaque !",
            description=f"{message}.",
            color=self.get_color(self.COLORS[success, critical]),
        )
        await ctx.channel.send(embed=embed)

    @commands.command()
    @commands.guild_only()
    @commands.has_role(DISCORD_ADMIN_ROLE)
    async def copy(self, ctx, *args):
        """Copie un ou plusieurs personnages dans la campagne courante."""
        await ctx.message.delete()
        user = await self.get_user(ctx.author)
        command = f"{ctx.prefix}{ctx.command.name}"
        parser = Parser(prog=command, description="Copie un ou plusieurs personnages dans la campagne courante.")
        parser.add_argument("character", type=int, help="Identifiant du personnage")
        parser.add_argument("--name", "-n", type=str, default="", help="Nouveau nom du personnage")
        parser.add_argument("--count", "-c", type=int, default=1, help="Nombre de personnages")
        args = parser.parse_args(args)
        if parser.message:
            await ctx.author.send(f"```{parser.message}```")
            return

        _channel = await self.get_channel(ctx.channel, user)
        if not _channel or not _channel.campaign_id:
            return
        data = vars(args).copy()
        data.pop("character")
        data.update(campaign=_channel.campaign_id)
        ret = await self.request(f"character/{args.character}/copy/", method="post", data=data)
        if ret is None:
            return
        creatures = []
        for creature in ret:
            self.creatures[args.character] = creature = Creature(
                id=0, name=creature["name"], character_id=creature["id"], campaign_id=creature["campaign"]
            )
            creatures.append(creature)
        creature_names = ", ".join([f"**{c.name}** (*{c.character_id}*)" for c in creatures])
        if len(creatures) > 1:
            await ctx.channel.send(f"🚪 {creature_names} apparaissent dans <#{ctx.channel.id}>.")
            return
        await ctx.channel.send(f"🚪 {creature_names} apparaît dans <#{ctx.channel.id}>.")

    @commands.command()
    @commands.guild_only()
    @commands.has_role(DISCORD_ADMIN_ROLE)
    async def time(self, ctx, *args):
        """Avance dans le temps et passe éventuellement au tour du personnage suivant."""
        await ctx.message.delete()
        user = await self.get_user(ctx.author)
        command = f"{ctx.prefix}{ctx.command.name}"
        parser = Parser(
            prog=command, description="Avance dans le temps et passe éventuellement au tour du personnage suivant."
        )
        parser.add_argument("--seconds", "-S", type=int, default=0, help="Nombre de secondes écoulées")
        parser.add_argument("--minutes", "-M", type=int, default=0, help="Nombre de minutes écoulées")
        parser.add_argument("--hours", "-H", type=int, default=0, help="Nombre de minutes écoulées")
        parser.add_argument("--sleep", "-s", dest="resting", action="store_true", default=False, help="Repos ?")
        parser.add_argument("--turn", "-t", action="store_true", default=False, help="Tour suivant ?")
        parser.add_argument("--all", "-a", action="store_true", default=False, help="Pour tous ?")
        parser.add_argument("--reason", "-r", type=str, help="Reason")
        args = parser.parse_args(args)
        if parser.message:
            await ctx.author.send(f"```{parser.message}```")
            return

        async def proceed(_channel):
            channel = _channel.channel
            seconds = int(timedelta(seconds=args.seconds, minutes=args.minutes, hours=args.hours).total_seconds())
            data = dict(resting=args.resting, reset=not args.turn, seconds=seconds)
            ret = await self.request(f"campaign/{_channel.campaign_id}/next/", method="post", data=data)
            if ret is None:
                return
            date = parse_date(ret["campaign"]["current_game_date"])
            messages = []
            if args.reason:
                messages.append(f"> {args.reason}\n")
            if seconds:
                messages.append(f"⌛ **{args.hours:02}:{args.minutes:02}:{args.seconds:02}** se sont écoulées !")
            messages.append(f"📅 Nous sommes désormais le **{date:%A %d %B %Y}** et il est **{date:%H:%M:%S}**.")
            if ret.get("character"):
                try:
                    _user = User.get(User.character_id == ret["character"]["id"])
                    messages.append(f"🔁 C'est désormais au tour de **<@{_user.id}>**.")
                except:
                    character = ret["character"]
                    messages.append(f"🔁 C'est désormais au tour de **{character['name']}** ({character['id']}).")
            for damage in ret.get("damages", []):
                messages.append(f"> {ret['icon']}  **{damage['character']['name']}** a reçu **{ret['long_label']}**")
            embed = Embed(title=f"⏰ Le temps passe...", description="\n".join(messages))
            await channel.send(embed=embed)

        if args.all:
            for _channel in Channel.select().where(Channel.campaign_id.is_null(False)):
                _channel.channel = self.bot.get_channel(_channel.id)
                await proceed(_channel)
        else:
            _channel = await self.get_channel(ctx.channel, user)
            if not _channel or not _channel.campaign_id:
                return
            await proceed(_channel)

    @commands.command()
    @commands.guild_only()
    @commands.has_role(DISCORD_ADMIN_ROLE)
    async def give(self, ctx, *args):
        """Donne un ou plusieurs objets à un personnage donné."""
        await ctx.message.delete()
        user = await self.get_user(ctx.author)
        command = f"{ctx.prefix}{ctx.command.name}"
        parser = Parser(prog=command, description="Donne un ou plusieurs objets à un personnage donné.")
        parser.add_argument("item", type=str, help="Nom ou identifiant de l'objet")
        parser.add_argument("player", type=str, help="Nom du joueur")
        parser.add_argument("--quantity", "-q", type=int, default=1, help="Nombre d'objets")
        parser.add_argument("--condition", "-c", type=int, default=100, help="Etat de l'objet")
        parser.add_argument("--image", "-i", type=str, help="Image de l'objet")
        parser.add_argument("--silent", "-s", action="store_true", default=False, help="Pas de notification")
        args = parser.parse_args(args)
        if parser.message:
            await ctx.author.send(f"```{parser.message}```")
            return

        _user = await self.get_user(args.player)
        if not _user:
            return
        if args.item.isdigit():
            ret = await self.request(f"item/?id={args.item}&fields=id,name&all=1")
        else:
            ret = await self.request(
                f'item/?filters=or(name_fr.icontains:"{args.item}",'
                f'name_en.icontains:"{args.item}")&fields=id,name&all=1'
            )
        if not ret or len(ret) > 1:
            await ctx.author.send(f"⚠️ Aucun ou trop (**{len(ret)}**) d'objets correspondent à la recherche.")
            return
        data = vars(args).copy()
        data.pop("player")
        data.pop("image")
        silent = data.pop("silent")
        data["item"], item_name = ret[0]["id"], ret[0]["name"]
        ret = await self.request(f"character/{_user.character_id}/item/", method="post", data=data)
        if not ret:
            await ctx.author.send(f"⚠️ Une erreur s'est produite pendant l'exécution de la commande `{OP}give`.")
            return
        if not silent:
            embed = Embed(
                title=f"🎁 Nouvel objet trouvé !",
                description=f"<@{_user.id}> a récupéré **{item_name}** (x{args.quantity}) !",
            )
            if args.image:
                embed.set_image(url=args.image)
            elif ret["item"]["image"]:
                embed.set_image(url=ret["item"]["image"])
            elif ret["item"]["thumbnail"]:
                embed.set_image(url="/".join([FALLOUT_URL, "static/fallout/img/", ret["item"]["thumbnail"]]))
            await ctx.channel.send(embed=embed)

    @commands.command()
    @commands.guild_only()
    @commands.has_role(DISCORD_ADMIN_ROLE)
    async def open(self, ctx, *args):
        """Ouvre un butin avec éventuellement un personnage donné."""
        await ctx.message.delete()
        user = await self.get_user(ctx.author)
        command = f"{ctx.prefix}{ctx.command.name}"
        parser = Parser(prog=command, description="Ouvre un butin avec éventuellement un personnage donné.")
        parser.add_argument("loot", type=str, help="Nom ou identifiant du butin")
        parser.add_argument("--player", "-p", type=str, help="Joueur")
        parser.add_argument("--silent", "-s", action="store_true", default=False, help="Pas de notification")
        args = parser.parse_args(args)
        if parser.message:
            await ctx.author.send(f"```{parser.message}```")
            return

        _channel = await self.get_channel(ctx.channel, user)
        if not _channel or not _channel.campaign_id:
            return
        data = {"campaign": _channel.campaign_id}
        _user = await self.get_user(args.player)
        if _user:
            data["character"] = _user.character_id
        if args.loot.isdigit():
            ret = await self.request(f"loottemplate/?id={args.loot}&fields=id,name&all=1")
        else:
            ret = await self.request(
                f'loottemplate/?filters=or(name_fr.icontains:"{args.loot}",'
                f'name_en.icontains:"{args.loot}")&fields=id,name&all=1'
            )
        if not ret or len(ret) > 1:
            await ctx.author.send(f"⚠️ Aucun ou trop (**{len(ret)}**) de butins correspondent à la recherche.")
            return
        loot_id, loot_name = ret[0]["id"], ret[0]["name"]
        ret = await self.request(f"loottemplate/{loot_id}/open/", method="post", data=data)
        if not args.silent:
            if _user:
                description = f"**{loot_name}** a été ouvert par <@{_user.id}> !"
            else:
                description = f"**{loot_name}** a été ouvert !"
            content = []
            for res in ret:
                id, name, quantity, condition = res["id"], res["item"]["name"], res["quantity"], res["condition"]
                if condition:
                    content.append(f"> {name} (x{quantity}, état {int(condition * 100)}%)")
                else:
                    content.append(f"> {name} (x{quantity})")
            if content:
                description = f"{description}\nIl contient les objets suivants :\n{'\n'.join(content)}"
                colour = self.get_color("green")
            else:
                description = f"{description}\nIl ne contient malheureusement aucun objet de valeur..."
                colour = self.get_color("orange")
            # await ctx.channel.send(message)
            embed = Embed(title="📦 Butin trouvé !", description=description, colour=colour)
            if content:
                embed.set_footer(text="Vous pouvez choisir quoi ramasser depuis l'écran `butins` de la campagne.")
            await ctx.channel.send(embed=embed)

    @commands.command()
    @commands.guild_only()
    @commands.has_role(DISCORD_ADMIN_ROLE)
    async def say(self, ctx, *args):
        """Ouvre une fenêtre de dialogue riche."""
        await ctx.message.delete()
        user = await self.get_user(ctx.author)
        command = f"{ctx.prefix}{ctx.command.name}"
        parser = Parser(prog=command, description="Ouvre une fenêtre de dialogue riche.")
        parser.add_argument("text", type=str, help="Texte du dialogue")
        parser.add_argument("--title", "-t", type=str, help="Titre du dialogue")
        parser.add_argument("--portrait", "-p", type=str, help="URL de la miniature")
        parser.add_argument("--image", "-i", type=str, help="URL de l'image")
        parser.add_argument("--color", "-c", type=str, help="Couleur du dialogue")
        args = parser.parse_args(args)
        if parser.message:
            await ctx.author.send(f"```{parser.message}```")
            return

        embed = Embed(title=args.title or None, description=args.text, color=self.get_color(args.color))
        if args.portrait:
            embed.set_thumbnail(url=args.portrait)
        if args.image:
            embed.set_image(url=args.image)
        await ctx.channel.send(embed=embed)

    @commands.command()
    @commands.guild_only()
    @commands.has_role(DISCORD_ADMIN_ROLE)
    async def xp(self, ctx, *args):
        """Ajoute de l'expérience à un ou plusieurs personnages."""
        await ctx.message.delete()
        user = await self.get_user(ctx.author)
        command = f"{ctx.prefix}{ctx.command.name}"
        parser = Parser(prog=command, description="Ajoute de l'expérience à un ou plusieurs personnages.")
        parser.add_argument("amount", type=int, help="Quantité d'expérience")
        parser.add_argument("players", metavar="player", type=str, nargs="+", help="Nom du joueur")
        parser.add_argument("--reason", "-r", type=str, help="Raison")
        args = parser.parse_args(args)
        if parser.message:
            await ctx.author.send(f"```{parser.message}```")
            return

        xp = args.amount
        data = vars(args).copy()
        data.pop("players")
        for player_name in args.players:
            player = await self.get_user(player_name)
            if not player or not player.character_id:
                continue
            ret = await self.request(f"character/{player.character_id}/xp/", method="post", data=data)
            if ret is None:
                await ctx.author.send(
                    f"⚠️ Une erreur s'est produite pendant l'exécution de la commande `{OP}{command}`."
                )
                return
            req_xp, level, level_up = ret["required_experience"], ret["level"], ret["level_up"]
            if level_up:
                embed = Embed(
                    title=f"🆙 Passage de niveau !",
                    description=(
                        f"> {args.reason}\n" if args.reason else "" +
                        f"<@{player.id}> a gagné **{xp}** points d'expérience et est passé au niveau **{level}** !\n"
                        f"Il faut désormais **{req_xp}** points d'expérience pour passer au niveau **{level+1}**."
                    ),
                )
            else:
                embed = Embed(
                    title=f"⬆️ Gain d'expérience !",
                    description=(
                        f"> {args.reason}\n" if args.reason else "" +
                        f"<@{player.id}> a gagné **{xp}** points d'expérience !\n"
                        f"Il a encore besoin de **{req_xp}** points d'expérience pour passer au niveau **{level+1}**."
                    ),
                )
            await ctx.channel.send(embed=embed)

    @commands.command()
    @commands.guild_only()
    @commands.has_role(DISCORD_ADMIN_ROLE)
    async def purge(self, ctx):
        await ctx.message.delete()
        players_in_channel = User.select().where(User.channel == ctx.channel.id)
        deleted_messages = await ctx.channel.purge()
        if deleted_messages:
            transcript = await chat_exporter.raw_export(ctx.channel, deleted_messages, tz_info="Europe/Paris")
            if transcript:
                for player in players_in_channel:
                    if not player.my_channel_id:
                        continue
                    channel = self.bot.get_channel(player.my_channel_id)
                    if channel:
                        file = File(io.BytesIO(transcript.encode()), filename=f"{ctx.channel.name}.html")
                        await channel.send(
                            f"♻️ Le canal <#{ctx.channel.id}> a été purgé !\n"
                            f"⌚ Vous pouvez retrouver l'historique des messages ci-dessous :",
                            file=file,
                        )

    @commands.Cog.listener()
    async def on_member_update(self, before, after):
        if not after.bot:
            await self.get_user(after)

    @commands.Cog.listener()
    async def on_guild_channel_update(self, before, after):
        if Channel.get_or_none(Channel.id == after.id):
            await self.get_channel(after)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel):
        _channel = Channel.get_or_none(Channel.id == channel.id)
        if _channel:
            await self.request(f"campaign/{_channel.campaign_id}/", method="delete")
            User.update(channel_id=None).where(User.channel_id == _channel.id).execute()
            _channel.delete_instance()

    async def cog_command_error(self, ctx, error):
        if hasattr(ctx.message.channel, "name"):
            await ctx.author.send(f"⚠️ **Erreur :** {error} (`{ctx.message.content}` on `{ctx.message.channel.name}`)")
            logger.error(f"[{ctx.message.channel.name}] {error} ({ctx.message.content})")
        else:
            await ctx.author.send(f"⚠️ **Erreur :** {error} (`{ctx.message.content}`)")
            logger.error(f"{error} ({ctx.message.content})")
        import traceback

        traceback.print_exception(error)

    async def get_character_url(self, user):
        if not user.player_id:
            return ""
        ret = await self.request(f"common/token/?all=1&user_id={user.player_id}")
        extra = [f"?character={user.character_id}"] if user.character_id else [""]
        return "/".join([FALLOUT_URL, "token", ret[0]["key"]] + extra)

    async def create_user(self, user, **kwargs):
        data = dict(
            name=user.name,
            player=user.player_id,
            campaign=FALLOUT_CAMPAIGN,
            is_player=True,
            has_stats=True,
            has_needs=True,
            enable_levelup=True,
            enable_stats=True,
            enable_logs=True,
            **kwargs,
        )
        ret = await self.request("character/", method="post", data=data)
        if ret is None:
            return
        user.character_id = ret["id"]
        user.save(only=("character_id",))
        return user

    async def get_user(self, user):
        if isinstance(user, str):
            if user.isdigit():
                creature = self.creatures.get(user)
                if not creature:
                    ret = await self.request(f"character/{user}/")
                    if ret:
                        creature = Creature(
                            id=0,
                            name=ret["name"],
                            character_id=ret["id"],
                            campaign_id=ret["campaign_id"],
                        )
                        self.creatures[user] = creature
                return creature
            user_id = self.extract_id(user)
            if user_id:
                user = self.bot.get_user(user_id)
            else:
                func = lambda u: any(
                    user.lower() in value.lower() for value in (u.nick, u.name, u.display_name) if value
                )
                user = utils.find(func, self.bot.get_all_members())
        if not user:
            return None
        _user = self.users.get(user.id)
        if not _user:
            _user, created = User.get_or_create(id=user.id, defaults=dict(name=user.nick or user.name))
        if not _user.player_id:
            ret = await self.request(
                "player/",
                method="post",
                data=dict(
                    username=_user.id,
                    nickname=_user.name,
                    password=uuid.uuid4().hex,
                ),
            )
            if not ret:
                raise Exception(f"Unable to retrieve data from backend.")
            _user.player_id = ret["id"]
            _user.save(only=("player_id",))
        if (user.nick or user.name) != _user.name:
            _user.name = user.nick or user.name
            _user.save(only=("name",))
            if _user.player_id:
                await self.request(
                    f"player/{_user.player_id}/",
                    method="patch",
                    data=dict(nickname=_user.name),
                )
            if _user.character_id:
                await self.request(
                    f"character/{_user.character_id}/",
                    method="patch",
                    data=dict(name=_user.name),
                )
            if _user.my_channel_id:
                channel = self.bot.get_channel(_user.my_channel_id)
                if channel:
                    await channel.edit(name=_user.name)
        _user.user = user
        self.users[_user.id] = _user
        return _user

    async def get_channel(self, channel, user=None, date=None):
        date = date or FALLOUT_DATE
        if isinstance(channel, str):
            channel_id = self.extract_id(channel)
            if channel_id:
                channel = self.bot.get_channel(channel_id)
            else:
                channel = utils.find(lambda c: channel.lower() == c.name.lower(), self.bot.get_all_channels())
        if not channel:
            return None
        _channel = self.channels.get(channel.id)
        if not _channel:
            _channel, created = Channel.get_or_create(id=channel.id, defaults=dict(name=channel.name, date=date))
        channel_name = channel.name.replace("#", "").replace("-", " ").replace("_", " ").title()
        if not _channel.campaign_id:
            ret = await self.request(
                "campaign/",
                method="post",
                data=dict(
                    name=channel_name,
                    game_master=user.player_id if user else None,
                    description=channel.topic or "",
                    start_game_date=FALLOUT_DATE.isoformat(),
                    current_game_date=date.isoformat(),
                ),
            )
            _channel.campaign_id = ret["id"]
            _channel.save(only=("campaign_id",))
        else:
            ret = await self.request(f"campaign/{_channel.campaign_id}/", method="get")
            _channel.date = parse_date(ret["current_game_date"])
            _channel.save(only=("date",))
        if _channel.name != channel.name or _channel.topic != channel.topic:
            _channel.name, _channel.topic = channel.name, channel.topic
            _channel.save(
                only=(
                    "name",
                    "topic",
                )
            )
            await self.request(
                f"campaign/{_channel.campaign_id}/",
                method="patch",
                data=dict(name=channel_name, description=channel.topic or ""),
            )
        _channel.channel = channel
        self.channels[_channel.id] = _channel
        return _channel

    async def request(self, endpoint, data=None, method=None, **options):
        data, method = data or {}, (method or "get").lower()
        url = "/".join([FALLOUT_URL, "api", endpoint])
        func = getattr(self.session, method)
        if method in ("get", "delete"):
            resp = await func(url, **options)
        else:
            resp = await func(url, json=data, **options)
        result = ""
        try:
            result = resp.json()
            if resp.status_code < 300:
                return result
            return None
        except:
            return None
        finally:
            logger.debug(f"[{method.upper()}] [{resp.status_code}] {url} {data} {result}")

    def extract_id(self, string):
        groups = re.match(r"<[@!#]+(\d+)>", string)
        if groups:
            return int(groups[1])
        return groups

    def try_get(self, value, enum, default=True):
        value = value.strip().lower()
        return enum.get(value, value if default else None)

    def has_role(self, member, target=DISCORD_ADMIN_ROLE):
        for role in member.roles:
            if role.name == target:
                return True
        return False

    def get_color(self, code):
        if not code:
            return None
        code = code.lower()
        color = getattr(Colour, code, lambda: None)()
        if not color and any(c in "0123456789abcdef" for c in code) and len(code) == 6:
            color = int(code, 16)
        return color


async def main():
    locale.setlocale(locale.LC_ALL, DISCORD_LOCALE)
    db.create_tables((Channel, User))
    bot = commands.Bot(command_prefix=DISCORD_OPERATOR, intents=Intents.all())
    await bot.add_cog(Fallout(bot))
    await bot.start(DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
