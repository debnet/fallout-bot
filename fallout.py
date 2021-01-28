# coding: utf-8
import argparse
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
from discord import utils, File
from discord.ext import commands
import chat_exporter


DISCORD_TOKEN = os.environ.get('DISCORD_TOKEN')
DISCORD_LOCALE = os.environ.get('DISCORD_LOCALE') or 'fr_FR'
DISCORD_OPERATOR = OP = os.environ.get('DISCORD_OPERATOR') or '!'
DISCORD_ROLE = ROLE = os.environ.get('DISCORD_ROLE') or 'MJ'
DISCORD_CATEGORY = os.environ.get('DISCORD_CATEGORY') or 'Joueurs'
DISCORD_WORLD = os.environ.get('DISCORD_WORLD') or 'Monde'
FALLOUT_TOKEN = os.environ.get('FALLOUT_TOKEN')
FALLOUT_URL = os.environ.get('FALLOUT_URL')
FALLOUT_DATE = parse_date(os.environ.get('FALLOUT_DATE') or datetime.utcnow().isoformat(), dayfirst=True)
FALLOUT_CAMPAIGN = int(os.environ.get('FALLOUT_CAMPAIGN') or 0) or None

REGEX_FLAGS = re.IGNORECASE | re.MULTILINE

log_handler = logging.StreamHandler()
log_handler.setFormatter(logging.Formatter('[%(asctime)s] %(levelname)7s: %(message)s'))

pw_logger = logging.getLogger('peewee')
pw_logger.setLevel(logging.DEBUG)
pw_logger.addHandler(log_handler)

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
logger.addHandler(log_handler)

db = pw.SqliteDatabase('fallout.db')


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


class Parser(argparse.ArgumentParser):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.message = ''

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
        0: ':zero:', 1: ':one:', 2: ':two:', 3: ':three:', 4: ':four:', 5: ':five:',
        6: ':six:', 7: ':seven:', 8: ':eight:', 9: ':nine:', 10: ':keycap_ten:'}
    STATUS = {(0, 0): ':warning:', (1, 0): ':ok:', (0, 1): ':skull:', (1, 1): ':trophy:'}
    DAMAGE_ICONS = {
        'normal': ':muscle:',
        'laser': ':sparkler:',
        'plasma': ':sparkle:',
        'explosive': ':boom:',
        'fire': ':fire:',
        'poison': ':biohazard:',
        'radiation': ':radioactive:',
        'gas_contact': ':cloud:',
        'gas_inhaled': ':cloud:',
        'raw': ':skull:',
        'thirst': ':arrow_up: :droplet:',
        'hunger': ':arrow_up: :meat_on_bone:',
        'sleep': ':arrow_up: :zzz:',
        'heal_thirst': ':arrow_down: :droplet:',
        'heal_hunger': ':arrow_down: :meat_on_bone:',
        'heal_sleep': ':arrow_down: :zzz:',
        'heal': ':heart:',
        'heal_rad': ':syringue:',
    }
    SPECIAL = {
        ('s', 'str', 'for', 'force'): 'strength',
        ('p', 'per'): 'perception',
        ('e', 'end'): 'endurance',
        ('c', 'cha', 'charisme'): 'charisma',
        ('i', 'int'): 'intelligence',
        ('a', 'agl', 'agilité'): 'agility',
        ('l', 'chance'): 'luck',
    }
    SKILLS = {
        ('sg', 'small', 'light',  'léger', 'légère', 'légères', ): 'small_guns',
        ('bg', 'big', 'heavy', 'lourd', 'lourde', 'lourdes', ): 'big_guns',
        ('ew', 'energy', 'energie', 'laser', 'plasma', ): 'energy_weapons',
        ('un', 'hand', 'main', 'mains', 'cac', 'contact', ): 'unarmed',
        ('mw', 'melee', 'mêlée', ): 'melee_weapons',
        ('th', 'throw', 'jet', 'lance', 'lancer', 'grenade', ): 'throwing',
        ('at', 'ath', 'athletism', 'athlétisme', ): 'athletics',
        ('dt', 'detect', 'search', 'détection', 'recherche', 'rechercher', 'trouver', ): 'detection',
        ('fa', 'first', 'aid', 'premiers', 'secours', ): 'first_aid',
        ('do', 'doc', 'med', 'médecin', 'médecine', ): 'doctor',
        ('ch', 'chem', 'chimie', 'pharma', 'pharmacologie', ): 'chems',
        ('sn', 'discret', 'discrétion', 'cacher', ): 'sneak',
        ('lp', 'lock', 'crochetage', ): 'lockpick',
        ('st', 'vol', 'voler', 'pickpocket', ): 'steal',
        ('tr', 'piège', 'pièges', ): 'traps',
        ('ex', 'exp', 'explosif', 'explosifs', ): 'explosives',
        ('sc', ): 'science',
        ('rp', 'mech', 'meca', 'mécanisme', 'réparer', 'réparation', 'craft', ): 'repair',
        ('cp', 'comp', 'info', 'programmer', 'pirater', 'piratage', 'hacker', 'hacking', ): 'computers',
        ('el', 'elec', 'electro', ): 'electronics',
        ('sp', 'discours', 'pe', 'persuader', 'parler', 'convaincre', ): 'speech',
        ('de', 'tromper', 'tromperie', 'mentir', ): 'deception',
        ('ba', 'marchandage', 'commerce', 'négocier', ): 'barter',
        ('su', 'survie', 'outdoorsman', ): 'survival',
        ('kn', 'connaissance', 'culture', ): 'knowledge',
    }
    STATS = {**SPECIAL, **SKILLS}
    STATS = {k: v for ks, v in STATS.items() for k in ks + (v,)}
    SKILLS = {k: v for ks, v in SKILLS.items() for k in ks + (v,)}
    BODY_PARTS = {
        ('t', 'torse', 'corps'): 'torso',
        ('l', 'j', 'jambe', 'jambes', 'p', 'pied', 'pieds'): 'legs',
        ('a', 'b', 'bras', 'm', 'main', 'mains'): 'arms',
        ('h', 'tête'): 'head',
        ('e', 'oeil', 'y', 'yeux'): 'eyes',
    }
    BODY_PARTS = {k: v for ks, v in BODY_PARTS.items() for k in ks + (v,)}
    DAMAGES = {
        ('n',): 'normal',
        ('l',): 'laser',
        ('p',): 'plasma',
        ('e', 'exp'): 'explosive',
        ('f', 'feu'): 'fire',
        ('ps',): 'poison',
        ('r', 'rad'): 'radiation',
        ('gc',): 'gas_contact',
        ('gi',): 'gas_inhaled',
        ('raw',): 'raw',
        ('t', 'soif'): 'thirst',
        ('h', 'faim'): 'hunger',
        ('s', 'sommeil'): 'sleep',
        ('ht', 'boire'): 'heal_thirst',
        ('hh', 'manger'): 'heal_hunger',
        ('hs', 'dormir'): 'heal_sleep',
        ('+', 'soin'): 'heal',
        ('hr',): 'heal_rad',
    }
    DAMAGES = {k: v for ks, v in DAMAGES.items() for k in ks + (v,)}

    def __init__(self, bot):
        self.bot = bot
        self.session = httpx.AsyncClient()
        self.session.headers = {
            'Content-Type': 'application/json',
            'Authorization': f'TOKEN {FALLOUT_TOKEN}',
        }
        self.users = {}
        self.channels = {}
        self.creatures = {}

    @commands.Cog.listener()
    async def on_ready(self):
        chat_exporter.init_exporter(self.bot)

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
        command = f'{ctx.prefix}{ctx.command.name}'
        parser = Parser(
            prog=command,
            description="Crée un nouveau personnage avec les statistiques choisies.",
            epilog="La somme totale des statistiques doit être strictement égale à 40.")
        parser.add_argument('strength', metavar='S', type=int, choices=choices, help="force (entre 1 et 10)")
        parser.add_argument('perception', metavar='P', type=int, choices=choices, help="perception (entre 1 et 10)")
        parser.add_argument('endurance', metavar='E', type=int, choices=choices, help="endurance (entre 1 et 10)")
        parser.add_argument('charisma', metavar='C', type=int, choices=choices, help="charisme (entre 1 et 10)")
        parser.add_argument('intelligence', metavar='I', type=int, choices=choices, help="intelligence (entre 1 et 10)")
        parser.add_argument('agility', metavar='A', type=int, choices=choices, help="agilité (entre 1 et 10)")
        parser.add_argument('luck', metavar='L', type=int, choices=choices, help="chance (entre 1 et 10)")
        parser.add_argument('--tag', '-t', dest='tag_skills', type=str, nargs='*', help="spécialité (maximum 3)")
        parser.add_argument('--user', '-u', dest='player', type=str, help="Utilisateur")
        args = parser.parse_args(args)
        if parser.message:
            await ctx.author.send(f"```{parser.message}```")
            return

        if not args.player and user.character_id:
            await ctx.author.send(f":no_entry:  Vous avez déjà créé votre personnage.")
            return
        data = vars(args).copy()
        if sum(data[stats] for stats in self.SPECIAL.values()) != 40 and user.level < 1:
            await ctx.author.send(f":no_entry:  La somme totale de vos statistiques doit valoir exactement **40**.")
            return
        data['tag_skills'] = []
        if user.level > 0 and args.tag_skills:
            data['tag_skills'] = [
                self.SKILLS[t] for t in map(lambda e: e.strip().lower(), data['tag_skills']) if t in self.SKILLS]
            if len(args.tag_skills) > 3:
                await ctx.author.send(f":no_entry:  Vous ne pouvez sélectionner que 3 spécialités au maximum.")
                return
        player = data.pop('player', None)
        if user.level > 0 and player:
            player = await self.get_user(player)
            if player and player.player_id:
                data['player'] = player.player_id
        await self.create_user(user, **data)
        url = await self.get_character_url(user)
        await ctx.author.send(
            f":white_check_mark:  Votre personnage a été créé avec succès ! Fiche de personnage : {url}")
        # Create private channel
        channel_name = user.name.lower().replace('#', '').replace(' ', '-').replace('_', '-')
        category = utils.get(ctx.channel.guild.categories, name=DISCORD_CATEGORY)
        new_channel = utils.get(ctx.channel.guild.text_channels, name=channel_name, category=category)
        if not new_channel:
            new_channel = await ctx.channel.guild.create_text_channel(channel_name, category=category, topic=user.name)
            everyone = utils.get(ctx.channel.guild.roles, name='@everyone')
            gm_role = utils.get(ctx.channel.guild.roles, name=DISCORD_ROLE)
            await new_channel.set_permissions(everyone, read_messages=False)
            await new_channel.set_permissions(gm_role, read_messages=True)
            await new_channel.set_permissions(user.user, read_messages=True)
            user.my_channel_id = new_channel.id
            user.save(only=('my_channel_id',))

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
                f":warning:  Vous n'avez pas encore de personnage actif, tapez `{ctx.prefix}new` pour en créer un.")
        await ctx.author.send(f":link:  Accéder à votre fiche de personnage : {url}")

    @commands.command()
    @commands.guild_only()
    @commands.has_role(ROLE)
    async def move(self, ctx, *args):
        """Déplace un ou plusieurs joueurs dans un autre canal."""
        await ctx.message.delete()
        user = await self.get_user(ctx.author)
        command = f'{ctx.prefix}{ctx.command.name}'
        parser = Parser(
            prog=command,
            description="Déplace un ou plusieurs joueurs dans un autre canal.")
        parser.add_argument('channel', type=str, help="Nom du canal de destination")
        parser.add_argument('players', metavar='player', type=str, nargs='+', help="Nom du joueur")
        parser.add_argument('--topic', '-t', type=str, help="Description du canal")
        parser.add_argument('--date', '-d', type=str, help="Date ")
        args = parser.parse_args(args)
        if parser.message:
            await ctx.author.send(f"```{parser.message}```")
            return

        channel_id = self.extract_id(args.channel)
        category = utils.get(ctx.channel.guild.categories, name=DISCORD_WORLD)
        if not channel_id:
            channel_name = args.channel.lower().replace('#', '').replace(' ', '-').replace('_', '-')
            new_channel = utils.get(ctx.channel.guild.text_channels, name=channel_name, category=category)
            if not new_channel:
                new_channel = await ctx.channel.guild.create_text_channel(
                    channel_name, category=category, topic=args.topic)
                everyone = utils.get(ctx.channel.guild.roles, name='@everyone')
                await new_channel.set_permissions(everyone, read_messages=False)
        else:
            new_channel = bot.get_channel(channel_id)
        _old_channel = await self.get_channel(ctx.channel, user) if ctx.channel.category == category else None
        _new_channel = await self.get_channel(
            new_channel, user, date=parse_date(args.date, dayfirst=True) if args.date else None)
        players_in_channel = User.select().where(User.channel == _new_channel)
        if _old_channel and _new_channel and players_in_channel.count() == 0 and _new_channel.date != _old_channel.date:
            _new_channel.date = _old_channel.date
            _new_channel.save(only=('date',))
            await self.request(f'campaign/{_new_channel.campaign_id}/', method='patch', data=dict(
                start_game_date=_new_channel.date.isoformat(), current_game_date=_new_channel.date.isoformat()))
        if _new_channel.channel.members:
            deleted_messages = await _new_channel.channel.purge()
            transcript = await chat_exporter.raw_export(_new_channel.channel, deleted_messages, 'Europe/Paris')
            if transcript:
                channel_name = _new_channel.channel.name
                file = File(io.BytesIO(transcript.encode()), filename=f'historique-{channel_name}.html')
                for member in _new_channel.channel.members:
                    if member.bot:
                        continue
                    await ctx.send(f":door:  Un ou plusieurs joueurs sont entrés dans **#{channel_name}**, "
                                   f"les messages du canal ont été purgés par soucis de discrétion. "
                                   f"Vous pouvez retrouver l'historique de messages ci-dessous :", file=file)
        users = []
        for player_name in args.players:
            player = await self.get_user(player_name)
            if player and player.channel_id:
                old_channel = self.bot.get_channel(player.channel_id)
                if old_channel:
                    await old_channel.set_permissions(player.user, overwrite=None)
            player.channel_id = new_channel.id
            player.save(only=('channel_id',))
            users.append(player)
            await new_channel.set_permissions(player.user, read_messages=True)
            await self.request(f'character/{player.character_id}/', method='patch', data=dict(
                campaign=_new_channel.campaign_id))
        gm_role = utils.get(ctx.channel.guild.roles, name=DISCORD_ROLE)
        await new_channel.set_permissions(gm_role, read_messages=True)
        users_names = ', '.join([f'<@{user.id}>' for user in users])
        if len(users) > 1:
            if _old_channel:
                await _old_channel.channel.send(f":outbox_tray:  {users_names} partent de <#{_old_channel.id}>.")
            await _new_channel.channel.send(f":inbox_tray:  {users_names} arrivent dans <#{_new_channel.id}>.")
            return
        if _old_channel:
            await _old_channel.channel.send(f":outbox_tray:  {users_names} part de <#{_old_channel.id}>.")
        await _new_channel.channel.send(f":inbox_tray:  {users_names} arrive dans <#{_new_channel.id}>.")

    @commands.command()
    @commands.guild_only()
    @commands.has_role(ROLE)
    async def roll(self, ctx, *args):
        """Réalise un jet de compétence ou de S.P.E.C.I.A.L. pour un ou plusieurs joueurs."""
        await ctx.message.delete()
        user = await self.get_user(ctx.author)
        command = f'{ctx.prefix}{ctx.command.name}'
        parser = Parser(
            prog=command,
            description="Réalise un jet de compétence ou de S.P.E.C.I.A.L. pour un ou plusieurs joueurs.")
        parser.add_argument('stats', type=str, help="Nom ou code de la statistique")
        parser.add_argument('players', metavar='player', type=str, nargs='+', help="Nom du joueur")
        parser.add_argument('--modifier', '-m', metavar='MOD', default=0, type=int, help="Modificateur")
        parser.add_argument('--xp', '-x', action='store_true', default=False, help="Expérience ?")
        args = parser.parse_args(args)
        if parser.message:
            await ctx.author.send(f"```{parser.message}```")
            return

        args.stats = self.try_get(args.stats, self.STATS)
        data = vars(args).copy()
        data.pop('players')
        for player_name in args.players:
            player = await self.get_user(player_name)
            if not player or not player.character_id:
                continue
            ret = await self.request(f'character/{player.character_id}/roll/', method='post', data=data)
            if ret is None:
                await ctx.author.send(
                    f":warning:  Une erreur s'est produite pendant l'exécution de la commande `{command}`.")
                return
            success, critical, label = ret['success'], ret['critical'], ret['long_label']
            await ctx.channel.send(f":game_die:  {self.STATUS[success, critical]}  <@{player.id}> {label}")

    @commands.command()
    @commands.guild_only()
    @commands.has_role(ROLE)
    async def damage(self, ctx, *args):
        """Inflige des dégâts à un ou plusieurs joueurs."""
        await ctx.message.delete()
        user = await self.get_user(ctx.author)
        command = f'{ctx.prefix}{ctx.command.name}'
        parser = Parser(
            prog=command,
            description="Inflige des dégâts à un ou plusieurs joueurs.")
        parser.add_argument('min_damage', metavar='min', type=int, default=0, help="Dégâts minimals")
        parser.add_argument('max_damage', metavar='max', type=int, default=0, help="Dégâts maximals")
        parser.add_argument('raw_damage', metavar='raw', type=int, default=0, help="Dégâts bruts")
        parser.add_argument(
            '--type', '-t', metavar='TYPE', dest='damage_type', type=str,
            default='normal', help="Type de dégâts (par défaut : normal)")
        parser.add_argument(
            '--part', '-p', metavar='PART', dest='body_part', type=str,
            default='torso', help="Partie du corps touchée (par défaut: torse)")
        parser.add_argument(
            '--threshold', '-m', metavar='MOD', dest='threshold_modifier', type=int,
            default=0, help="Modificateur d'absorption")
        parser.add_argument(
            '--resistance', '-r', metavar='MOD', dest='resistance_modifier', type=int,
            default=0, help="Modificateur de résistance")
        parser.add_argument('--simulation', '-s', action='store_true', default=False, help="Simulation ?")
        parser.add_argument('players', metavar='player', type=str, nargs='+', help="Nom du joueur")
        args = parser.parse_args(args)
        if parser.message:
            await ctx.author.send(f"```{parser.message}```")
            return

        args.damage_type = self.try_get(args.damage_type, self.DAMAGES)
        args.body_part = self.try_get(args.body_part, self.BODY_PARTS)
        data = vars(args).copy()
        data.pop('players')
        for player_name in args.players:
            player = await self.get_user(player_name)
            if not player or not player.character_id:
                continue
            ret = await self.request(f'character/{player.character_id}/damage/', method='post', data=data)
            if ret is None:
                await ctx.author.send(
                    f":warning:  Une erreur s'est produite pendant l'exécution de la commande `{command}`.")
                return
            message = f"{self.DAMAGE_ICONS[args.damage_type]}  <@{player.id}> a reçu {ret['label']}"
            await ctx.channel.send(message)

    @commands.command()
    @commands.guild_only()
    @commands.has_role(ROLE)
    async def fight(self, ctx, *args):
        """Fait s'affronter deux joueurs entre eux."""
        await ctx.message.delete()
        user = await self.get_user(ctx.author)
        command = f'{ctx.prefix}{ctx.command.name}'
        parser = Parser(
            prog=command,
            description="Fait s'affronter deux joueurs entre eux.")
        parser.add_argument('attacker', type=str, help="Joueur attaquant")
        parser.add_argument('target', metavar='defender', type=str, help="Joueur défenseur")
        parser.add_argument(
            '--range', '-r', metavar='RANGE', dest='target_range', type=int,
            default=1, help="Distance entre les deux joueurs")
        parser.add_argument(
            '--part', '-p', metavar='PART', dest='target_body_part', type=str,
            default='torso', help="Partie du corps touchée (par défaut: torse)")
        parser.add_argument(
            '--modifier', '-m', metavar='MOD', dest='hit_chance_modifier', type=int,
            default=0, help="Modificateur de précision")
        parser.add_argument(
            '--action', '-a', dest='is_action', action='store_true',
            default=False, help="Action ?")
        parser.add_argument(
            '--unarmed', '-u', dest='no_weapon', action='store_true',
            default=False, help="Sans arme ?")
        parser.add_argument(
            '--success', '-f', dest='force_success', action='store_true',
            default=False, help="Succès ?")
        parser.add_argument(
            '--critical', '-c', dest='force_critical', action='store_true',
            default=False, help="Critique ?")
        parser.add_argument(
            '--raw', '-x', dest='force_raw_damage', action='store_true',
            default=False, help="Dégâts bruts ?")
        parser.add_argument(
            '--simulation', '-s', action='store_true',
            default=False, help="Simulation ?")
        args = parser.parse_args(args)
        if parser.message:
            await ctx.author.send(f"```{parser.message}```")
            return

        args.body_part = self.try_get(args.target_body_part, self.BODY_PARTS)
        attacker, target = await self.get_user(args.attacker), await self.get_user(args.target)
        if not attacker or not target or not attacker.character_id or not target.character_id:
            await ctx.author.send(
                f":warning:  Les joueurs sélectionnés ne peuvent combattre car ils n'ont pas de personnage.")
            return
        data = vars(args).copy()
        data.pop('attacker')
        data['target'] = target.character_id
        ret = await self.request(f'character/{attacker.character_id}/fight/', method='post', data=data)
        if ret is None:
            await ctx.author.send(
                f":warning:  Une erreur s'est produite pendant l'exécution de la commande `{command}`.")
            return
        attacker = f"<@{attacker.id}>" if attacker.id else f"{attacker.name} ({attacker.character_id})"
        target = f"<@{target.id}>" if target.id else f"{target.name} ({target.character_id})"
        success, critical, label = ret['success'], ret['critical'], ret['long_label']
        status = self.STATUS[success, critical]
        await ctx.channel.send(f":crossed_swords:  {status}  {attacker} **vs.** {target} : {label}")

    @commands.command()
    @commands.guild_only()
    @commands.has_role(ROLE)
    async def copy(self, ctx, *args):
        """Copie un ou plusieurs personnages dans la campagne courante."""
        await ctx.message.delete()
        user = await self.get_user(ctx.author)
        command = f'{ctx.prefix}{ctx.command.name}'
        parser = Parser(
            prog=command,
            description="Copie un ou plusieurs personnages dans la campagne courante.")
        parser.add_argument('character', type=int, help="Identifiant du personnage")
        parser.add_argument('--name', '-n', type=str, help="Nouveau nom du personnage")
        parser.add_argument('--count', '-c', type=int, default=1, help="Nombre de personnages")
        args = parser.parse_args(args)
        if parser.message:
            await ctx.author.send(f"```{parser.message}```")
            return

        _channel = await self.get_channel(ctx.channel, user)
        if not _channel or not _channel.campaign_id:
            return
        data = vars(args).copy()
        data.pop('character')
        data.update(campaign=_channel.campaign_id)
        ret = await self.request(f'character/{args.character}/copy/', method='post', data=data)
        if ret is None:
            return
        creatures = []
        for creature in ret:
            self.creatures[args.character] = creature = Creature(
                id=0, name=creature['name'], character_id=creature['id'], campaign_id=creature['campaign'])
            creatures.append(creature)
        creatures_names = ', '.join([f'**{c.name}** ({c.character_id})' for c in creatures])
        if len(creatures) > 1:
            await ctx.channel.send(f":door:  {creatures_names} apparaissent dans <#{ctx.channel.id}>.")
            return
        await ctx.channel.send(f":door:  {creatures_names} apparaît dans <#{ctx.channel.id}>.")

    @commands.command()
    @commands.guild_only()
    @commands.has_role(ROLE)
    async def time(self, ctx, *args):
        """Avance dans le temps et passe éventuellement au tour du personnage suivant."""
        await ctx.message.delete()
        user = await self.get_user(ctx.author)
        command = f'{ctx.prefix}{ctx.command.name}'
        parser = Parser(
            prog=command,
            description="Avance dans le temps et passe éventuellement au tour du personnage suivant.")
        parser.add_argument('--seconds', '-S', type=int, default=0, help="Nombre de secondes écoulées")
        parser.add_argument('--minutes', '-M', type=int, default=0, help="Nombre de minutes écoulées")
        parser.add_argument('--hours', '-H', type=int, default=0, help="Nombre de minutes écoulées")
        parser.add_argument('--sleep', '-s', dest='resting', action='store_true', default=False, help="Repos ?")
        parser.add_argument('--turn', '-t', action='store_true', default=False, help="Tour suivant ?")
        args = parser.parse_args(args)
        if parser.message:
            await ctx.author.send(f"```{parser.message}```")
            return

        _channel = await self.get_channel(ctx.channel, user)
        if not _channel or not _channel.campaign_id:
            return
        seconds = int(timedelta(seconds=args.seconds, minutes=args.minutes, hours=args.hours).total_seconds())
        data = dict(resting=args.resting, reset=not args.turn, seconds=seconds)
        ret = await self.request(f'campaign/{_channel.campaign_id}/next/', method='post', data=data)
        if ret is None:
            return
        if seconds:
            date = parse_date(ret['campaign']['current_game_date'])
            await ctx.channel.send(
                f":hourglass:  **{args.hours:02}:{args.minutes:02}:{args.seconds:02}** se sont écoulés, "
                f"nous sommes le **{date:%A %d %B %Y}** et il est **{date:%H:%M:%S}**...")
        if not ret.get('character'):
            return
        try:
            _user = User.get(User.character_id == ret['character']['id'])
            await ctx.channel.send(
                f":repeat:  C'est désormais au tour de **<@{_user.id}>**.")
        except:
            character = ret['character']
            await ctx.channel.send(
                f":repeat:  C'est désormais au tour de **{character['name']}** ({character['id']}).")

    @commands.command()
    @commands.guild_only()
    @commands.has_role(ROLE)
    async def give(self, ctx, *args):
        """Donne un ou plusieurs objets à un personnage donné."""
        await ctx.message.delete()
        user = await self.get_user(ctx.author)
        command = f'{ctx.prefix}{ctx.command.name}'
        parser = Parser(
            prog=command,
            description="Donne un ou plusieurs objets à un personnage donné.")
        parser.add_argument('item', type=str, help="Nom ou identifiant de l'objet")
        parser.add_argument('player', type=str, help="Nom du joueur")
        parser.add_argument('--quantity', '-q', type=int, default=1, help="Nombre d'objets")
        parser.add_argument('--condition', '-c', type=int, default=100, help="Etat de l'objet")
        parser.add_argument('--silent', '-s', action='store_true', default=False, help="Pas de notification")
        args = parser.parse_args(args)
        if parser.message:
            await ctx.author.send(f"```{parser.message}```")
            return

        _user = await self.get_user(args.player)
        if not _user:
            return
        if args.item.isdigit():
            ret = await self.request(f'item/?id={args.item}&fields=id,nameall=1')
        else:
            ret = await self.request(f'item/?name.icontains={args.item}&fields=id,name&all=1')
        if not ret or len(ret) > 1:
            await ctx.author.send(f":warning:  Aucun ou trop ({len(ret)}) d'objets correspondent à la recherche.")
            return
        data = vars(args).copy()
        data.pop('player')
        silent = data.pop('silent')
        data['item'], item_name = ret[0]['id'], ret[0]['name']
        ret = await self.request(f'character/{_user.character_id}/item/', method='post', data=data)
        if not ret:
            await ctx.author.send(
                f":warning:  Une erreur s'est produite pendant l'exécution de la commande `{OP}give`.")
            return
        if not silent:
            await ctx.channel.send(
                f":gift:  <@{_user.id}> a récupéré {args.quantity} **{item_name}** !")

    @commands.command()
    @commands.guild_only()
    @commands.has_role(ROLE)
    async def loot(self, ctx, *args):
        """Ouvre un butin avec éventuellement un personnage donné."""
        await ctx.message.delete()
        user = await self.get_user(ctx.author)
        command = f'{ctx.prefix}{ctx.command.name}'
        parser = Parser(
            prog=command,
            description="Ouvre un butin avec éventuellement un personnage donné.")
        parser.add_argument('loot', type=str, help="Nom ou identifiant du butin")
        parser.add_argument('--player', '-p', type=str, help="Joueur")
        args = parser.parse_args(args)
        if parser.message:
            await ctx.author.send(f"```{parser.message}```")
            return

        _channel = await self.get_channel(ctx.channel, user)
        if not _channel or not _channel.campaign_id:
            return
        data = vars(args).copy()
        silent = data.pop('silent')
        _user = await self.get_user(args.player)
        if _user:
            data['character'] = _user.character_id
        if args.loot.isdigit():
            ret = await self.request(f'loottemplate/?id={args.loot}&fields=id,name&all=1')
        else:
            ret = await self.request(f'loottemplate/?name.icontains="{args.loot}"&fields=id,name&all=1')
        loot_id, loot_name = ret[0]['id'], ret[0]['name']
        ret = await self.request(f'loottemplate/{loot_id}/open/', method='post', data=data)
        if not ret:
            await ctx.author.send(
                f":warning:  Une erreur s'est produite pendant l'exécution de la commande `{OP}loot`.")
            return
        if not silent:
            if _user:
                await ctx.channel.send(f":package:  **{loot_name}** a été ouvert par <@{_user.id}> !")
            else:
                await ctx.channel.send(f":package:  **{loot_name}** a été ouvert !")

    @commands.command()
    @commands.guild_only()
    @commands.has_role(ROLE)
    async def say(self, ctx, *args):
        """Ouvre une fenêtre de dialogue riche."""
        await ctx.message.delete()
        user = await self.get_user(ctx.author)
        command = f'{ctx.prefix}{ctx.command.name}'
        parser = Parser(
            prog=command,
            description="Ouvre une fenêtre de dialogue riche.")
        parser.add_argument('text', type=str, help="Texte du dialogue")
        parser.add_argument('--title', '-t', type=str, help="Titre du dialogue")
        parser.add_argument('--portrait', '-p', type=str, help="URL de la miniature")
        parser.add_argument('--image', '-i', type=str, help="URL de l'image")
        parser.add_argument('--color', '-c', type=str, help="Couleur du dialogue")
        args = parser.parse_args(args)
        if parser.message:
            await ctx.author.send(f"```{parser.message}```")
            return

        from discord.embeds import Embed, EmptyEmbed
        empty = EmptyEmbed
        embed = Embed(title=args.title or empty, description=args.text, color=args.color or empty)
        if args.portrait:
            embed.set_thumbnail(url=args.portrait)
        if args.image:
            embed.set_image(url=args.image)
        await ctx.channel.send(embed=embed)

    @commands.command()
    @commands.guild_only()
    @commands.has_role(ROLE)
    async def xp(self, ctx, *args):
        """Ajoute de l'expérience à un ou plusieurs personnages."""
        await ctx.message.delete()
        user = await self.get_user(ctx.author)
        command = f'{ctx.prefix}{ctx.command.name}'
        parser = Parser(
            prog=command,
            description="Ajoute de l'expérience à un ou plusieurs personnages.")
        parser.add_argument('amount', type=int, help="Quantité d'expérience")
        parser.add_argument('players', metavar='player', type=str, nargs='+', help="Nom du joueur")
        args = parser.parse_args(args)
        if parser.message:
            await ctx.author.send(f"```{parser.message}```")
            return

        data = vars(args).copy()
        data.pop('players')
        for player_name in args.players:
            player = await self.get_user(player_name)
            if not player or not player.character_id:
                continue
            ret = await self.request(f'character/{player.character_id}/xp/', method='post', data=data)
            if ret is None:
                await ctx.author.send(
                    f":warning:  Une erreur s'est produite pendant l'exécution de la commande `{command}`.")
                return
            required_xp, next_level = ret['required_experience'], ret['level'] + 1
            await ctx.channel.send(
                f":up:  <@{player.id}> a gagné **{args.amount}** points d'expérience ! "
                f"Il a encore besoin de **{required_xp}** points d'expérience pour passer au niveau **{next_level}**.")

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
            await self.request(f'campaign/{_channel.campaign_id}/', method='delete')
            User.update(channel_id=None).where(User.channel_id == _channel.id).execute()
            _channel.delete_instance()

    async def cog_command_error(self, ctx, error):
        if hasattr(ctx.message.channel, 'name'):
            await ctx.author.send(
                f":warning:  **Erreur :** {error} (`{ctx.message.content}` on `{ctx.message.channel.name}`)")
            logger.error(f"[{ctx.message.channel.name}] {error} ({ctx.message.content})")
        else:
            await ctx.author.send(
                f":warning:  **Erreur :** {error} (`{ctx.message.content}`)")
            logger.error(f"{error} ({ctx.message.content})")
        raise

    async def get_character_url(self, user):
        if not user.player_id:
            return ''
        ret = await self.request(f'common/token/?all=1&user_id={user.player_id}')
        extra = [f'?character={user.character_id}'] if user.character_id else ['']
        return '/'.join([FALLOUT_URL, 'token', ret[0]['key']] + extra)

    async def create_user(self, user, **kwargs):
        data = dict(
            name=user.name,
            player=user.player_id,
            campaign=FALLOUT_CAMPAIGN,
            is_player=True,
            has_stats=True,
            has_needs=True,
            **kwargs)
        ret = await self.request('character/', method='post', data=data)
        if ret is None:
            return
        user.character_id = ret['id']
        user.save(only=('character_id',))
        return user

    async def get_user(self, user):
        if isinstance(user, str):
            if user.isdigit():
                creature = self.creatures.get(user)
                if not creature:
                    ret = await self.request(f'character/{user}/')
                    if ret:
                        creature = Creature(
                            id=0, name=ret['name'], character_id=ret['id'], campaign_id=ret['campaign_id'])
                        self.creatures[user] = creature
                return creature
            user_id = self.extract_id(user)
            if user_id:
                user = self.bot.get_user(user_id)
            else:
                user = utils.find(lambda u: user.lower() in (u.nick or u.name).lower(), self.bot.get_all_members())
        if not user:
            return None
        _user = self.users.get(user.id)
        if not _user:
            _user, created = User.get_or_create(id=user.id, defaults=dict(name=user.nick or user.name))
        if not _user.player_id:
            ret = await self.request('player/', method='post', data=dict(
                username=_user.id, nickname=_user.name, password=uuid.uuid4().hex))
            _user.player_id = ret['id']
            _user.save(only=('player_id',))
        if (user.nick or user.name) != _user.name:
            _user.name = user.nick or user.name
            _user.save(only=('name',))
            if _user.player_id:
                await self.request(f'player/{_user.player_id}/', method='patch', data=dict(nickname=_user.name))
            if _user.character_id:
                await self.request(f'character/{_user.character_id}/', method='patch', data=dict(name=_user.name))
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
            _channel, created = Channel.get_or_create(
                id=channel.id, defaults=dict(name=channel.name, date=date))
        channel_name = channel.name.replace('#', '').replace('-', ' ').replace('_', ' ').title()
        if not _channel.campaign_id:
            ret = await self.request('campaign/', method='post', data=dict(
                name=channel_name, game_master=user.player_id if user else None, description=channel.topic or '',
                start_game_date=FALLOUT_DATE.isoformat(), current_game_date=date.isoformat()))
            _channel.campaign_id = ret['id']
            _channel.save(only=('campaign_id',))
        else:
            ret = await self.request(f'campaign/{_channel.campaign_id}/', method='get')
            _channel.date = parse_date(ret['current_game_date'])
            _channel.save(only=('date',))
        if _channel.name != channel.name or _channel.topic != channel.topic:
            _channel.name, _channel.topic = channel.name, channel.topic
            _channel.save(only=('name', 'topic',))
            await self.request(f'campaign/{_channel.campaign_id}/', method='patch', data=dict(
                name=channel_name, description=channel.topic or ''))
        _channel.channel = channel
        self.channels[_channel.id] = _channel
        return _channel

    async def request(self, endpoint, data=None, method=None, **options):
        data, method = data or {}, (method or 'get').lower()
        url = '/'.join([FALLOUT_URL, 'api', endpoint])
        func = getattr(self.session, method)
        if method in ('get', 'delete'):
            resp = await func(url, **options)
        else:
            resp = await func(url, json=data, **options)
        result = ''
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
        groups = re.match(r'<[@!#]+(\d+)>', string)
        if groups:
            return int(groups[1])
        return groups

    def try_get(self, value, enum, default=True):
        value = value.strip().lower()
        return enum.get(value, value if default else None)


if __name__ == '__main__':
    locale.setlocale(locale.LC_ALL, DISCORD_LOCALE)
    db.create_tables((Channel, User))
    bot = commands.Bot(command_prefix=DISCORD_OPERATOR)
    bot.add_cog(Fallout(bot))
    bot.run(DISCORD_TOKEN)
