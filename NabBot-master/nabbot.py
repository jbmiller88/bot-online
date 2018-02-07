import asyncio
import random
import re
import sys
import time
import traceback
from datetime import timedelta, datetime

import discord
import psutil
from discord.ext import commands

from config import *
from utils import checks
from utils.database import init_database, userDatabase, reload_worlds, tracked_worlds, tracked_worlds_list, \
    reload_welcome_messages, welcome_messages, reload_announce_channels
from utils.discord import get_member, send_log_message, get_region_string, get_channel_by_name, get_user_servers, \
    clean_string, get_role_list, get_member_by_name, get_announce_channel, get_user_worlds
from utils.general import command_list, join_list, get_uptime, TimeString, \
    single_line, is_numeric, getLogin, start_time, global_online_list
from utils.general import log
from utils.help_format import NabHelpFormat
from utils.messages import decode_emoji, deathmessages_player, deathmessages_monster, EMOJI, levelmessages, \
    weighedChoice, formatMessage
from utils.tibia import get_server_online, get_character, ERROR_NETWORK, ERROR_DOESNTEXIST, \
    get_voc_abb, get_highscores, tibia_worlds, get_pronouns, parse_tibia_time, get_voc_emoji

description = '''Mission: Destroy all humans.'''
bot = commands.Bot(command_prefix=["/"], description=description, pm_help=True, formatter=NabHelpFormat())
# We remove the default help command so we can override it
bot.remove_command("help")


@bot.event
@asyncio.coroutine
def on_ready():
    bot.load_extension("tibia")
    bot.load_extension("mod")
    bot.load_extension("owner")
    bot.load_extension("admin")
    print('Logged in as')
    print(bot.user)
    print(bot.user.id)
    if lite_mode:
        print("Running in Lite mode!")
    print('------')
    log.info('Bot is online and ready')

    # Populate command_list
    for command_name, command in bot.commands.items():
        command_list.append(command_name)

    # Notify reset author
    if len(sys.argv) > 1:
        user = get_member(bot, sys.argv[1])
        sys.argv[1] = 0
        if user is not None:
            yield from bot.send_message(user, "Restart complete")

    # Background tasks
    bot.loop.create_task(game_update())
    bot.loop.create_task(events_announce())
    bot.loop.create_task(scan_deaths())
    bot.loop.create_task(scan_online_chars())
    bot.loop.create_task(scan_highscores())


@bot.event
@asyncio.coroutine
def on_command(command, ctx):
    """Called when a command is called. Used to log commands on a file."""
    if ctx.message.channel.is_private:
        destination = 'PM'
    else:
        destination = '#{0.channel.name} ({0.server.name})'.format(ctx.message)
    message_decoded = decode_emoji(ctx.message.content)
    log.info('Command by {0} in {1}: {2}'.format(ctx.message.author.display_name, destination, message_decoded))


@bot.event
@asyncio.coroutine
def on_command_error(error, ctx):
    if isinstance(error, commands.errors.CommandNotFound):
        return
    elif isinstance(error, commands.NoPrivateMessage):
        yield from bot.send_message(ctx.message.author, "This command cannot be used in private messages.")
    elif isinstance(error, commands.CommandInvokeError):
        print('In {0.command.qualified_name}:'.format(ctx), file=sys.stderr)
        traceback.print_tb(error.original.__traceback__)
        print('{0.__class__.__name__}: {0}'.format(error.original), file=sys.stderr)
        # Bot returns error message on discord if an owner called the command
        if ctx.message.author.id in owner_ids:
            yield from bot.send_message(ctx.message.channel, '```Py\n{0.__class__.__name__}: {0}```'.format(error.original))


@bot.event
@asyncio.coroutine
def on_message(message):
    """Called every time a message is sent on a visible channel.

    This is used to make commands case insensitive."""
    # Ignore if message is from any bot
    if message.author.bot:
        return

    split = message.content.split(" ", 1)
    if split[0][:1] == "/" and split[0].lower()[1:] in command_list:
        if len(split) > 1:
            message.content = split[0].lower()+" "+split[1]
        else:
            message.content = message.content.lower()
    if len(split) == 2:
        if message.author.id != bot.user.id and (not split[0].lower()[1:] in command_list or not split[0][:1] == "/")\
                and not message.channel.is_private and message.channel.name == ask_channel_name:
            yield from bot.delete_message(message)
            return
    elif ask_channel_delete:
        # Delete messages in askchannel
        if message.author.id != bot.user.id \
                and (not message.content.lower()[1:] in command_list or not message.content[:1] == "/") \
                and not message.channel.is_private \
                and message.channel.name == ask_channel_name:
            yield from bot.delete_message(message)
            return
    yield from bot.process_commands(message)


@bot.event
@asyncio.coroutine
def on_server_join(server: discord.Server):
    log.info("Nab Bot added to server: {0.name} (ID: {0.id})".format(server))
    message = "Hello! I'm now in **{0.name}**. To see my available commands, type \help\n" \
              "I will reply to commands from any channel I can see, but if you create a channel called *{1}*, I will " \
              "give longer replies and more information there.\n" \
              "If you want a server log channel, create a channel called *{2}*, I will post logs in there. You might " \
              "want to make it private though.\n" \
              "To have all of Nab Bot's features, use `/setworld <tibia_world>`"
    formatted_message = message.format(server, ask_channel_name, log_channel_name)
    yield from bot.send_message(server.owner, formatted_message)


@bot.event
@asyncio.coroutine
def on_member_join(member: discord.Member):
    """Called every time a member joins a server visible by the bot."""
    log.info("{0.display_name} (ID: {0.id}) joined {0.server.name}".format(member))
    if lite_mode:
        return
    server_id = member.server.id
    server_welcome = welcome_messages.get(server_id, "")
    pm = (welcome_pm+"\n"+server_welcome).format(member, bot)
    log_message = "{0.mention} joined.".format(member)

    # Check if user already has characters registered
    # This could be because he rejoined the server or is in another server tracking the same worlds
    world = tracked_worlds.get(member.server.id)
    if world is not None:
        c = userDatabase.cursor()
        try:
            c.execute("SELECT name, vocation, ABS(last_level) as level "
                      "FROM chars WHERE user_id = ? and world = ?", (member.id, world,))
            results = c.fetchall()
            if len(results) > 0:
                pm += "\nYou already have these characters in {0} registered to you: {1}"\
                    .format(world, join_list([r["name"] for r in results], ", ", " and "))
                log_message += "\nPreviously registered characters:\n\t"
                log_message += "\n\t".join("{name} - {level} {vocation}".format(**r) for r in results)
        finally:
            c.close()

    yield from send_log_message(bot, member.server, log_message)
    yield from bot.send_message(member, pm)
    yield from bot.send_message(member.server, "Look who just joined! Welcome {0.mention}!".format(member))


@bot.event
@asyncio.coroutine
def on_member_remove(member: discord.Member):
    """Called when a member leaves or is kicked from a server."""
    log.info("{0.display_name} (ID:{0.id}) left or was kicked from {0.server.name}".format(member))
    yield from send_log_message(bot, member.server, "**{0.name}#{0.discriminator}** left or was kicked.".format(member))


@bot.event
@asyncio.coroutine
def on_member_ban(member: discord.Member):
    """Called when a member is banned from a server."""
    log.warning("{0.display_name} (ID:{0.id}) was banned from {0.server.name}".format(member))
    yield from send_log_message(bot, member.server, "**{0.name}#{0.discriminator}** was banned.".format(member))


@bot.event
@asyncio.coroutine
def on_member_unban(server: discord.Server, user: discord.User):
    """Called when a member is unbanned from a server"""
    log.warning("{1.name} (ID:{1.id}) was unbanned from {0.name}".format(server, user))
    yield from send_log_message(bot, server, "**{0.name}#{0.discriminator}** was unbanned.".format(user))


@bot.event
@asyncio.coroutine
def on_message_delete(message: discord.Message):
    """Called every time a message is deleted."""
    if message.channel.name == ask_channel_name:
        return

    message_decoded = decode_emoji(message.clean_content)
    attachment = ""
    if message.attachments:
        attachment = "\n\tAttached file: "+message.attachments[0]['filename']
    log.info("A message by @{0} was deleted in #{2} ({3}):\n\t'{1}'{4}".format(message.author.display_name,
                                                                               message_decoded, message.channel.name,
                                                                               message.server.name, attachment))


@bot.event
@asyncio.coroutine
def on_message_edit(before: discord.Message, after: discord.Message):
    """Called every time a message is edited."""

    if before.channel.is_private:
        return

    if before.author.id == bot.user.id:
        return

    if before.content == after.content:
        return

    before_decoded = decode_emoji(before.clean_content)
    after_decoded = decode_emoji(after.clean_content)

    log.info("@{0} edited a message in #{3} ({4}):\n\t'{1}'\n\t'{2}'".format(before.author.name, before_decoded,
                                                                                 after_decoded, before.channel,
                                                                                 before.server))


@bot.event
@asyncio.coroutine
def on_member_update(before: discord.Member, after: discord.Member):
    if before.nick != after.nick:
        reply = "{1.mention}: Nickname changed from **{0.nick}** to **{1.nick}**".format(before, after)
        yield from send_log_message(bot, after.server, reply)
    elif before.name != after.name:
        reply = "{1.mention}: Name changed from **{0.name}** to **{1.name}**".format(before, after)
        yield from send_log_message(bot, after.server, reply)
    return


@bot.event
@asyncio.coroutine
def on_server_update(before: discord.Server, after: discord.Server):
    if before.name != after.name:
        reply = "Server name changed from **{0.name}** to **{1.name}**".format(before, after)
        yield from send_log_message(bot, after, reply)
    elif before.region != after.region:
        reply = "Server region changed from {0} to {1}".format(get_region_string(before.region),
                                                               get_region_string(after.region))
        yield from send_log_message(bot, after, reply)


@asyncio.coroutine
def events_announce():
    # TODO: This needs some denezunifying
    if lite_mode:
        return
    yield from bot.wait_until_ready()
    while not bot.is_closed:
        """Announces when an event is close to starting."""
        first_announcement = 60*30
        second_announcement = 60*15
        third_announcement = 60*5
        c = userDatabase.cursor()
        try:
            # Current time
            date = time.time()
            # Find incoming events
            # First announcement
            c.execute("SELECT creator, start, name, id, server "
                      "FROM events "
                      "WHERE start < ? AND start > ? AND active = 1 AND status > 3 "
                      "ORDER by start ASC", (date+first_announcement+60, date+first_announcement,))
            results = c.fetchall()
            if len(results) > 0:
                for row in results:
                    author = "unknown" if get_member(bot, row["creator"]) is None else get_member(bot, row["creator"]).display_name
                    name = row["name"]
                    event_id = row["id"]
                    time_diff = timedelta(seconds=row["start"]-date)
                    days, hours, minutes = time_diff.days, time_diff.seconds//3600, (time_diff.seconds//60)%60
                    if days:
                        start = '{0} days, {1} hours and {2} minutes'.format(days, hours, minutes)
                    elif hours:
                        start = '{0} hours and {1} minutes'.format(hours, minutes)
                    else:
                        start = '{0} minutes'.format(minutes)

                    message = "**{0}** (by **@{1}**,*ID:{3}*) - Is starting in {2}.".format(name, author, start, event_id)
                    c.execute("UPDATE events SET status = 3 WHERE id = ?", (event_id,))
                    log.info("Announcing event: {0} (by @{1},ID:{3}) - In {2}".format(name, author, start, event_id))
                    destination = bot.get_server(str(row["server"]))
                    if destination:
                        yield from bot.send_message(destination, message)
                    # Send PM to subscribers:
                    c.execute("SELECT * FROM event_subscribers WHERE event_id = ?", (event_id,))
                    subscribers = c.fetchall()
                    if len(subscribers) > 0:
                        for subscriber in subscribers:
                            user = get_member(bot, subscriber["user_id"])
                            if user is None:
                                continue
                            yield from bot.send_message(user, message)
            # Second announcement
            c.execute("SELECT creator, start, name, id, server "
                      "FROM events "
                      "WHERE start < ? AND start > ? AND active = 1 AND status > 2 "
                      "ORDER by start ASC", (date+second_announcement+60, date+second_announcement,))
            results = c.fetchall()
            if len(results) > 0:
                for row in results:
                    author = "unknown" if get_member(bot, row["creator"]) is None else get_member(bot, row["creator"]).display_name
                    name = row["name"]
                    event_id = row["id"]
                    time_diff = timedelta(seconds=row["start"]-date)
                    days, hours, minutes = time_diff.days, time_diff.seconds//3600, (time_diff.seconds//60) % 60
                    if days:
                        start = '{0} days, {1} hours and {2} minutes'.format(days, hours, minutes)
                    elif hours:
                        start = '{0} hours and {1} minutes'.format(hours, minutes)
                    else:
                        start = '{0} minutes'.format(minutes)

                    message = "**{0}** (by **@{1}**,*ID:{3}*) - Is starting in {2}.".format(name, author, start, event_id)
                    c.execute("UPDATE events SET status = 2 WHERE id = ?", (event_id,))
                    log.info("Announcing event: {0} (by @{1},ID:{3}) - In {2}".format(name, author, start, event_id))
                    destination = bot.get_server(str(row["server"]))
                    if destination:
                        yield from bot.send_message(destination, message)
                    # Send PM to subscribers:
                    c.execute("SELECT * FROM event_subscribers WHERE event_id = ?", (event_id,))
                    subscribers = c.fetchall()
                    if len(subscribers) > 0:
                        for subscriber in subscribers:
                            user = get_member(bot, subscriber["user_id"])
                            if user is None:
                                continue
                            yield from bot.send_message(user, message)
            # Third announcement
            c.execute("SELECT creator, start, name, id, server "
                      "FROM events "
                      "WHERE start < ? AND start > ? AND active = 1 AND status > 1 "
                      "ORDER by start ASC", (date+third_announcement+60, date+third_announcement,))
            results = c.fetchall()
            if len(results) > 0:
                for row in results:
                    author = "unknown" if get_member(bot, row["creator"]) is None else get_member(bot, row["creator"]).display_name
                    name = row["name"]
                    event_id = row["id"]
                    time_diff = timedelta(seconds=row["start"]-date)
                    days, hours, minutes = time_diff.days, time_diff.seconds//3600, (time_diff.seconds//60) % 60
                    if days:
                        start = '{0} days, {1} hours and {2} minutes'.format(days, hours, minutes)
                    elif hours:
                        start = '{0} hours and {1} minutes'.format(hours, minutes)
                    else:
                        start = '{0} minutes'.format(minutes)

                    message = "**{0}** (by **@{1}**,*ID:{3}*) - Is starting in {2}!".format(name, author, start, event_id)
                    c.execute("UPDATE events SET status = 1 WHERE id = ?", (event_id,))
                    log.info("Announcing event: {0} (by @{1},ID:{3}) - In {2}".format(name, author, start, event_id))
                    destination = bot.get_server(str(row["server"]))
                    if destination:
                        yield from bot.send_message(destination, message)
                    # Send PM to subscribers:
                    c.execute("SELECT * FROM event_subscribers WHERE event_id = ?", (event_id,))
                    subscribers = c.fetchall()
                    if len(subscribers) > 0:
                        for subscriber in subscribers:
                            user = get_member(bot, subscriber["user_id"])
                            if user is None:
                                continue
                            yield from bot.send_message(user, message)
            # Last announcement
            c.execute("SELECT creator, start, name, id, server "
                      "FROM events "
                      "WHERE start < ? AND start > ? AND active = 1 AND status > 0 "
                      "ORDER by start ASC", (date+60,date,))
            results = c.fetchall()
            if len(results) > 0:
                for row in results:
                    author = "unknown" if get_member(bot, row["creator"]) is None else get_member(bot, row["creator"]).display_name
                    name = row["name"]
                    event_id = row["id"]
                    time_diff = timedelta(seconds=row["start"]-date)
                    days, hours, minutes = time_diff.days, time_diff.seconds//3600, (time_diff.seconds//60) % 60
                    if days:
                        start = '{0} days, {1} hours and {2} minutes'.format(days, hours, minutes)
                    elif hours:
                        start = '{0} hours and {1} minutes'.format(hours, minutes)
                    else:
                        start = '{0} minutes'.format(minutes)

                    message = "**{0}** (by **@{1}**,*ID:{3}*) - Is starting right now!".format(name, author, start, event_id)
                    c.execute("UPDATE events SET status = 0 WHERE id = ?", (event_id,))
                    log.info("Announcing event: {0} (by @{1},ID:{3}) - Starting ({2})".format(name, author, start, event_id))
                    destination = bot.get_server(str(row["server"]))
                    if destination:
                        yield from bot.send_message(destination, message)
                    # Send PM to subscribers:
                    c.execute("SELECT * FROM event_subscribers WHERE event_id = ?", (event_id,))
                    subscribers = c.fetchall()
                    if len(subscribers) > 0:
                        for subscriber in subscribers:
                            user = get_member(bot, subscriber["user_id"])
                            if user is None:
                                continue
                            yield from bot.send_message(user, message)
        except AttributeError:
            pass
        finally:
            userDatabase.commit()
            c.close()
        yield from asyncio.sleep(20)


@asyncio.coroutine
def scan_deaths():
    #################################################
    #             Nezune's cave                     #
    # Do not touch anything, enter at your own risk #
    #################################################
    if lite_mode:
        return
    yield from bot.wait_until_ready()
    while not bot.is_closed:
        yield from asyncio.sleep(death_scan_interval)
        if len(global_online_list) == 0:
            continue
        # Pop last char in queue, reinsert it at the beginning
        current_char = global_online_list.pop()
        global_online_list.insert(0, current_char)

        # Get rid of server name
        current_char = current_char.split("_", 1)[1]
        # Check for new death
        yield from check_death(bot, current_char)


@asyncio.coroutine
def scan_highscores():
    #################################################
    #             Nezune's cave                     #
    # Do not touch anything, enter at your own risk #
    #################################################
    if lite_mode:
        return
    yield from bot.wait_until_ready()
    while not bot.is_closed:
        if len(tracked_worlds_list) == 0:
            # If no worlds are tracked, just sleep, worlds might get registered later
            yield from asyncio.sleep(highscores_delay)
            continue
        for server in tracked_worlds_list:
            for category in highscores_categories:
                highscores = []
                for pagenum in range(1, 13):
                    # Special cases (ek/rp mls)
                    if category == "magic_ek":
                        scores = yield from get_highscores(server, "magic", pagenum, 3)
                    elif category == "magic_rp":
                        scores = yield from get_highscores(server, "magic", pagenum, 4)
                    else:
                        scores = yield from get_highscores(server, category, pagenum)
                    if not (scores == ERROR_NETWORK):
                        highscores += scores
                    yield from asyncio.sleep(highscores_page_delay)
                # Open connection to users.db
                c = userDatabase.cursor()
                scores_tuple = []
                ranks_tuple = []
                for score in highscores:
                    scores_tuple.append((score['rank'], score['value'], score['name']))
                    ranks_tuple.append((score['rank'], server))
                # Clear out old rankings
                c.executemany(
                    "UPDATE chars SET "+category+" = NULL, "+category+"_rank"+" = NULL WHERE "+category+"_rank"+" LIKE ? AND world LIKE ?",
                    ranks_tuple
                )
                # Add new rankings
                c.executemany(
                    "UPDATE chars SET "+category+"_rank"+" = ?, "+category+" = ? WHERE name LIKE ?",
                    scores_tuple
                )
                userDatabase.commit()
                c.close()
            yield from asyncio.sleep(10)


@asyncio.coroutine
def scan_online_chars():
    #################################################
    #             Nezune's cave                     #
    # Do not touch anything, enter at your own risk #
    #################################################
    if lite_mode:
        return
    yield from bot.wait_until_ready()
    while not bot.is_closed:
        # Pop last server in queue, reinsert it at the beginning
        current_server = tibia_worlds.pop()
        tibia_worlds.insert(0, current_server)

        if current_server.capitalize() not in tracked_worlds_list:
            yield from asyncio.sleep(0.1)
            continue

        yield from asyncio.sleep(online_scan_interval)
        # Get online list for this server
        current_server_online = yield from get_server_online(current_server)

        if len(current_server_online) > 0:
            # Open connection to users.db
            c = userDatabase.cursor()

            # Remove chars that are no longer online from the globalOnlineList
            offline_list = []
            for char in global_online_list:
                if char.split("_", 1)[0] == current_server:
                    offline = True
                    for server_char in current_server_online:
                        if server_char['name'] == char.split("_", 1)[1]:
                            offline = False
                            break
                    if offline:
                        offline_list.append(char)
            for now_offline_char in offline_list:
                global_online_list.remove(now_offline_char)
                # Check for deaths and level ups when removing from online list
                now_offline_char = yield from get_character(now_offline_char.split("_", 1)[1])
                if not (now_offline_char == ERROR_NETWORK or now_offline_char == ERROR_DOESNTEXIST):
                    c.execute("SELECT name, last_level, id FROM chars WHERE name LIKE ?", (now_offline_char['name'],))
                    result = c.fetchone()
                    if result:
                        last_level = result["last_level"]
                        c.execute(
                            "UPDATE chars SET last_level = ? WHERE name LIKE ?",
                            (now_offline_char['level'], now_offline_char['name'],)
                        )
                        if now_offline_char['level'] > last_level > 0:
                            # Saving level up date in database
                            c.execute(
                                "INSERT INTO char_levelups (char_id,level,date) VALUES(?,?,?)",
                                (result["id"], now_offline_char['level'], time.time(),)
                            )
                            # Announce the level up
                            yield from announce_level(bot, now_offline_char['level'], char=now_offline_char)
                    yield from check_death(bot, now_offline_char['name'])

            # Add new online chars and announce level differences
            for server_char in current_server_online:
                c.execute("SELECT name, last_level, id, user_id FROM chars WHERE name LIKE ?", (server_char['name'],))
                result = c.fetchone()
                if result:
                    # If its a stalked character
                    last_level = result["last_level"]
                    # We update their last level in the db
                    c.execute(
                        "UPDATE chars SET last_level = ? WHERE name LIKE ?",
                        (server_char['level'], server_char['name'],)
                    )

                    if not (current_server + "_" + server_char['name']) in global_online_list:
                        # If the character wasn't in the globalOnlineList we add them
                        # (We insert them at the beginning of the list to avoid messing with the death checks order)
                        global_online_list.insert(0, (current_server + "_" + server_char['name']))
                        # Since this is the first time we see them online we flag their last death time
                        # to avoid backlogged death announces
                        c.execute(
                            "UPDATE chars SET last_death_time = ? WHERE name LIKE ?",
                            (None, server_char['name'],)
                        )
                        yield from check_death(bot, server_char['name'])

                    # Else we check for levelup
                    elif server_char['level'] > last_level > 0:
                        # Saving level up date in database
                        c.execute(
                            "INSERT INTO char_levelups (char_id,level,date) VALUES(?,?,?)",
                            (result["id"], server_char['level'], time.time(),)
                        )
                        # Announce the level up
                        yield from announce_level(bot, server_char['level'], char_name=server_char["name"])

            # Close cursor and commit changes
            userDatabase.commit()
            c.close()


@asyncio.coroutine
def check_death(bot, character):
    """Checks if the player has new deaths"""
    char = yield from get_character(character)
    if type(char) is not dict:
        log.warning("check_death: couldn't fetch {0}".format(character))
        return
    character_deaths = char["deaths"]

    if character_deaths:
        c = userDatabase.cursor()
        c.execute("SELECT name, last_death_time, id FROM chars WHERE name LIKE ?", (character,))
        result = c.fetchone()
        if result:
            last_death = character_deaths[0]
            death_time = parse_tibia_time(last_death["time"]).timestamp()
            # Check if we have a death that matches the time
            c.execute("SELECT * FROM char_deaths "
                      "WHERE char_id = ? AND date >= ? AND date <= ? AND level = ? AND killer LIKE ?",
                      (result["id"], death_time-200, death_time+200, last_death["level"], last_death["killer"]))
            last_saved_death = c.fetchone()
            if last_saved_death is not None:
                # This death is already saved, so nothing else to do here.
                return

            c.execute(
                "INSERT INTO char_deaths (char_id,level,killer,byplayer,date) VALUES(?,?,?,?,?)",
                (result["id"], int(last_death['level']), last_death['killer'], last_death['byPlayer'], death_time,)
            )

            # If the death happened more than 1 hour ago, we don't announce it, but it's saved already.
            if time.time()-death_time >= (1*60*60):
                log.info("Death detected, but too old to announce: {0}({1}) | {2}".format(character,
                                                                                          last_death['level'],
                                                                                          last_death['killer']))
            else:
                yield from announce_death(bot, last_death['level'], last_death['killer'], last_death['byPlayer'],
                                          max(last_death["level"]-char["level"], 0), char)

        # Close cursor and commit changes
        userDatabase.commit()
        c.close()


@asyncio.coroutine
def announce_death(bot, death_level, death_killer, death_by_player, levels_lost=0, char=None, char_name=None):
    """Announces a level up on the corresponding servers"""
    # Don't announce for low level players
    if int(death_level) < announce_threshold:
        return
    if char is None:
        if char_name is None:
            log.error("announce_death: no character or character name passed.")
            return
        char = yield from get_character(char_name)
    if type(char) is not dict:
        log.warning("announce_death: couldn't fetch character (" + char_name + ")")
        return

    log.info("Announcing death: {0}({1}) | {2}".format(char["name"], death_level, death_killer))

    # Get correct pronouns
    pronoun = get_pronouns(char["gender"])

    # Find killer article (a/an)
    death_killer_article = ""
    if not death_by_player:
        death_killer_article = death_killer.split(" ", 1)
        if death_killer_article[0] in ["a", "an"] and len(death_killer_article) > 1:
            death_killer = death_killer_article[1]
            death_killer_article = death_killer_article[0]+" "
        else:
            death_killer_article = ""

    # Select a message
    # Todo: Add levels lost to weighedChoice, is always 0 or greater.
    message = weighedChoice(deathmessages_player, char['vocation'], int(death_level)) if death_by_player else weighedChoice(deathmessages_monster, char['vocation'], int(death_level), death_killer)
    # Format message with death information
    deathInfo = {'charName': char["name"], 'deathLevel': death_level, 'deathKiller': death_killer,
                 'deathKillerArticle': death_killer_article, 'pronoun1': pronoun[0], 'pronoun2': pronoun[1],
                 'pronoun3': pronoun[2]}
    message = message.format(**deathInfo)
    # Format extra stylization
    message = formatMessage(message)
    message = EMOJI[":skull_crossbones:"] + " " + message

    for server_id, tracked_world in tracked_worlds.items():
        server = bot.get_server(server_id)
        if char["world"] == tracked_world and server is not None \
                and server.get_member(str(char["owner_id"])) is not None:
            yield from bot.send_message(get_announce_channel(bot, server), message[:1].upper()+message[1:])


@asyncio.coroutine
def announce_level(bot, new_level, char_name=None, char=None):
    """Announces a level up on corresponding servers

    One of these must be passed:
    char is a character dictionary
    char_name is a character's name

    If char_name is passed, the character is fetched here."""
    # Don't announce low level players
    if int(new_level) < announce_threshold:
        return
    if char is None:
        if char_name is None:
            log.error("announce_level: no character or character name passed.")
            return
        char = yield from get_character(char_name)
    if type(char) is not dict:
        log.warning("announce_level: couldn't fetch character (" + char_name + ")")
        return

    log.info("Announcing level up: {0} ({1})".format(char["name"], new_level))

    # Get pronouns based on gender
    pronoun = get_pronouns(char['gender'])

    # Select a message
    message = weighedChoice(levelmessages, char['vocation'], int(new_level))
    # Format message with level information
    level_info = {'charName': char["name"], 'newLevel': new_level, 'pronoun1': pronoun[0], 'pronoun2': pronoun[1],
                 'pronoun3': pronoun[2]}
    message = message.format(**level_info)
    # Format extra stylization
    message = formatMessage(message)
    message = EMOJI[":star2:"]+" "+message

    for server_id, tracked_world in tracked_worlds.items():
        server = bot.get_server(server_id)
        if char["world"] == tracked_world and server is not None \
                and server.get_member(str(char["owner_id"])) is not None:
            yield from bot.send_message(get_announce_channel(bot, server), message)


# Bot commands
@bot.command(pass_context=True, aliases=["commands"])
@asyncio.coroutine
def help(ctx, *commands: str):
    """Shows this message."""
    _mentions_transforms = {
        '@everyone': '@\u200beveryone',
        '@here': '@\u200bhere'
    }
    _mention_pattern = re.compile('|'.join(_mentions_transforms.keys()))

    bot = ctx.bot
    destination = ctx.message.channel if ctx.message.channel.name == ask_channel_name else ctx.message.author

    def repl(obj):
        return _mentions_transforms.get(obj.group(0), '')

    # help by itself just lists our own commands.
    if len(commands) == 0:
        pages = bot.formatter.format_help_for(ctx, bot)
    elif len(commands) == 1:
        # try to see if it is a cog name
        name = _mention_pattern.sub(repl, commands[0])
        command = None
        if name in bot.cogs:
            command = bot.cogs[name]
        else:
            command = bot.commands.get(name)
            if command is None:
                yield from bot.send_message(destination, bot.command_not_found.format(name))
                return
            destination = ctx.message.channel if command.no_pm else destination

        pages = bot.formatter.format_help_for(ctx, command)
    else:
        name = _mention_pattern.sub(repl, commands[0])
        command = bot.commands.get(name)
        if command is None:
            yield from bot.send_message(destination, bot.command_not_found.format(name))
            return

        for key in commands[1:]:
            try:
                key = _mention_pattern.sub(repl, key)
                command = command.commands.get(key)
                if command is None:
                    yield from bot.send_message(destination, bot.command_not_found.format(key))
                    return
            except AttributeError:
                yield from bot.send_message(destination, bot.command_has_no_subcommands.format(command, key))
                return

        pages = bot.formatter.format_help_for(ctx, command)

    for page in pages:
        yield from bot.send_message(destination, page)


@bot.command(pass_context=True, description='For when you wanna settle the score some other way')
@asyncio.coroutine
def choose(ctx, *choices: str):
    """Chooses between multiple choices."""
    if choices is None:
        return
    user = ctx.message.author
    yield from bot.say('Alright, **@{0}**, I choose: "{1}"'.format(user.display_name, random.choice(choices)))


@bot.command(pass_context=True, aliases=["i'm", "iam"])
@checks.is_not_lite()
@asyncio.coroutine
def im(ctx, *, char_name: str):
    """Lets you add your tibia character(s) for the bot to track.

    If you need to add any more characters or made a mistake, please message an admin."""
    # This is equivalent to someone using /stalk addacc on themselves.
    # If im_new_only is True it will only work on users who have no characters added to their account.

    user = ctx.message.author
    # List of servers the user shares with the bot
    user_servers = get_user_servers(bot, user.id)
    # List of Tibia worlds tracked in the servers the user is
    user_tibia_worlds = [world for server, world in tracked_worlds.items() if server in [s.id for s in user_servers]]
    # Remove duplicate entries from list
    user_tibia_worlds = list(set(user_tibia_worlds))

    if not ctx.message.channel.is_private and tracked_worlds.get(ctx.message.server.id) is None:
        yield from bot.say("This server is not tracking any tibia worlds.")
        return

    if len(user_tibia_worlds) == 0:
        return

    c = userDatabase.cursor()
    try:
        valid_mods = []
        for id in (owner_ids + mod_ids):
            mod = get_member(bot, id, ctx.message.server)
            if mod is not None:
                valid_mods.append(mod.mention)
        admins_message = join_list(valid_mods, ", ", " or ")
        yield from bot.send_typing(ctx.message.channel)
        char = yield from get_character(char_name)
        if type(char) is not dict:
            if char == ERROR_NETWORK:
                yield from bot.say("I couldn't fetch the character, please try again.")
            elif char == ERROR_DOESNTEXIST:
                yield from bot.say("That character doesn't exists.")
            return
        chars = char['chars']
        # If the char is hidden,we still add the searched character, if we have just one, we replace it with the
        # searched char, so we don't have to look him up again
        if len(chars) == 0 or len(chars) == 1:
            chars = [char]

        skipped = []
        updated = []
        added = []
        existent = []
        for char in chars:
            if char["world"] not in user_tibia_worlds:
                skipped.append(char)
                continue
            c.execute("SELECT name, user_id as owner FROM chars WHERE name LIKE ?", (char["name"],))
            db_char = c.fetchone()
            if db_char is not None:
                owner = get_member(bot, db_char["owner"])
                # Previous owner doesn't exist anymore
                if owner is None:
                    updated.append({'name': char['name'], 'world': char['world'], 'prevowner': db_char["owner"],
                                    'guild': char.get("guild", "No guild")})
                    continue
                # Char already registered to this user
                elif owner.id == user.id:
                    existent.append("{name} ({world})".format(**char))
                    continue
                # Character is registered to another user
                else:
                    reply = "Sorry, a character in that account ({0}) is already claimed by **{1.mention}**.\n" \
                            "Maybe you made a mistake? Or someone claimed a character of yours? " \
                            "Message {2} if you need help!"
                    yield from bot.say(reply.format(db_char["name"], owner, admins_message))
                    return
            # If we only have one char, it already contains full data
            if len(chars) > 1:
                yield from bot.send_typing(ctx.message.channel)
                char = yield from get_character(char["name"])
                if char == ERROR_NETWORK:
                    yield from bot.reply("I'm having network troubles, please try again.")
                    return
            if char.get("deleted", False):
                skipped.append(char)
                continue
            char["guild"] = char.get("guild", "No guild")
            added.append(char)

        if len(skipped) == len(chars):
            reply = "Sorry, I couldn't find any characters from the servers I track ({0})."
            yield from bot.reply(reply.format(join_list(user_tibia_worlds, ", ", " and ")))
            return

        reply = ""
        log_reply = dict().fromkeys([server.id for server in user_servers], "")
        if len(existent) > 0:
            reply += "\nThe following characters were already registered to you: {0}" \
                .format(join_list(existent, ", ", " and "))

        if len(added) > 0:
            reply += "\nThe following characters were added to your account: {0}" \
                .format(join_list(["{name} ({world})".format(**c) for c in added], ", ", " and "))
            for char in added:
                log.info("Character {0} was assigned to {1.display_name} (ID: {1.id})".format(char['name'], user))
                # Announce on server log of each server
                for server in user_servers:
                    # Only announce on worlds where the character's world is tracked
                    if tracked_worlds.get(server.id, None) == char["world"]:
                        log_reply[server.id] += "\n\t{name} - {level} {vocation} - **{guild}**".format(**char)

        if len(updated) > 0:
            reply += "\nThe following characters were reassigned to you: {0}" \
                .format(join_list(["{name} ({world})".format(**c)for c in updated], ", ", " and "))
            for char in updated:
                log.info("Character {0} was reassigned to {1.display_name} (ID: {1.id})".format(char['name'], user))
                # Announce on server log of each server
                for server in user_servers:
                    # Only announce on worlds where the character's world is tracked
                    if tracked_worlds.get(server.id, None) == char["world"]:
                        log_reply[server.id] += "\n\t{name} (Reassigned)".format(**char)

        for char in updated:
            c.execute("UPDATE chars SET user_id = ? WHERE name LIKE ?", (user.id, char['name']))
        for char in added:
            c.execute(
                "INSERT INTO chars (name,last_level,vocation,user_id, world) VALUES (?,?,?,?,?)",
                (char['name'], char['level']*-1, char['vocation'], user.id, char["world"])
            )

        c.execute("INSERT OR IGNORE INTO users (id, name) VALUES (?, ?)", (user.id, user.display_name,))
        c.execute("UPDATE users SET name = ? WHERE id = ?", (user.display_name, user.id, ))

        yield from bot.reply(reply)
        for server_id, message in log_reply.items():
            if message:
                message = user.mention + " registered the following characters: " + message
                yield from send_log_message(bot, bot.get_server(server_id), message)

    finally:
        c.close()
        userDatabase.commit()


@bot.command(pass_context=True, aliases=["i'mnot"])
@checks.is_not_lite()
@asyncio.coroutine
def imnot(ctx, *, name):
    """Removes a character assigned to you

    All registered level ups and deaths will be lost forever."""
    c = userDatabase.cursor()
    try:
        c.execute("SELECT id, name, ABS(last_level) as level, user_id, vocation, world "
                  "FROM chars WHERE name LIKE ?", (name, ))
        char = c.fetchone()
        if char is None:
            yield from bot.say("There's no character registered with that name.")
            return
        if str(char["user_id"]) != ctx.message.author.id:
            yield from bot.say("The character **{0}** is not registered to you.".format(char["name"]))
            return

        yield from bot.say("Are you sure you want to unregister **{name}** ({level} {vocation})? `yes/no`"
                           "\n*All registered level ups and deaths will be lost forever.*"
                           .format(**char))
        reply = yield from bot.wait_for_message(timeout=50.0, author=ctx.message.author, channel=ctx.message.channel)
        if reply is None:
            yield from bot.say("I guess you changed your mind.")
            return
        elif reply.content.lower() not in ["yes", "y"]:
            yield from bot.say("No then? Ok.")
            return

        c.execute("DELETE FROM chars WHERE id = ?", (char["id"], ))
        c.execute("DELETE FROM char_levelups WHERE char_id = ?", (char["id"], ))
        c.execute("DELETE FROM char_deaths WHERE char_id = ?", (char["id"], ))
        yield from bot.say("**{0}** is no longer registered to you.".format(char["name"]))

        user_servers = [s.id for s in get_user_servers(bot, ctx.message.author.id)]
        for server_id, world in tracked_worlds.items():
            if char["world"] == world and server_id in user_servers:
                message = "{0} unregistered **{1}**".format(ctx.message.author.mention, char["name"])
                yield from send_log_message(bot, bot.get_server(server_id), message)
    finally:
        userDatabase.commit()
        c.close()


@bot.command(pass_context=True)
@checks.is_not_lite()
@asyncio.coroutine
def online(ctx):
    """Tells you which users are online on Tibia

    This list gets updated based on Tibia.com online list, so it takes a couple minutes to be updated.

    If used in a server, only characters from users of the server are shown
    If used on PM, only  characters from users of servers you're in are shown"""
    if ctx.message.channel.is_private:
        user_servers = get_user_servers(bot, ctx.message.author.id)
        user_worlds = get_user_worlds(bot, ctx.message.author.id)
    else:
        user_servers = [ctx.message.server]
        user_worlds = [tracked_worlds.get(ctx.message.server.id)]
        if user_worlds[0] is None:
            yield from bot.say("This server is not tracking any tibia worlds.")
            return
    c = userDatabase.cursor()
    now = datetime.utcnow()
    uptime = (now-start_time).total_seconds()
    count = 0
    online_list = {world: "" for world in user_worlds}
    try:
        for char in global_online_list:
            char = char.split("_", 1)
            world = char[0]
            name = char[1]
            if world not in user_worlds:
                continue
            c.execute("SELECT name, user_id, vocation, ABS(last_level) as level FROM chars WHERE name LIKE ?", (name,))
            row = c.fetchone()
            if row is None:
                continue
            # Only show members on this server or members visible to author if it's a pm
            owner = get_member(bot, row["user_id"], server_list=user_servers)
            if owner is None:
                continue
            row["owner"] = owner.display_name
            row['emoji'] = get_voc_emoji(row['vocation'])
            row['vocation'] = get_voc_abb(row['vocation'])
            online_list[world] += "\n\t{name} (Lvl {level} {vocation}{emoji}, **@{owner}**)".format(**row)
            count += 1

        if count == 0:
            if uptime < 60:
                yield from bot.say("I just started, give me some time to check online lists..."+EMOJI[":clock2:"])
            else:
                yield from bot.say("There is no one online from Discord.")
            return

        # Remove worlds with no players online
        online_list = {k: v for k, v in online_list.items() if v is not ""}
        reply = "The following discord users are online:"
        if len(user_worlds) == 1:
            reply += online_list[user_worlds[0]]
        else:
            for world, content in online_list.items():
                reply += "\n__**{0}**__{1}".format(world, content)

        yield from bot.say(reply)
    finally:
        c.close()


@bot.command()
@asyncio.coroutine
def uptime():
    """Shows how long the bot has been running"""
    yield from bot.say("I have been running for {0}.".format(get_uptime(True)))


@bot.command(pass_context=True)
@asyncio.coroutine
def about(ctx):
    """Shows information about the bot"""
    permissions = ctx.message.channel.permissions_for(get_member(bot, bot.user.id, ctx.message.server))
    if not permissions.embed_links:
        yield from bot.say("Sorry, I need `Embed Links` permission for this command.")
        return
    user_count = 0
    char_count = 0
    deaths_count = 0
    levels_count = 0
    if not lite_mode:
        c = userDatabase.cursor()
        try:
            c.execute("SELECT COUNT(*) as count FROM users")
            result = c.fetchone()
            if result is not None:
                user_count = result["count"]
            c.execute("SELECT COUNT(*) as count FROM chars")
            result = c.fetchone()
            if result is not None:
                char_count = result["count"]
            c.execute("SELECT COUNT(*) as count FROM char_deaths")
            result = c.fetchone()
            if result is not None:
                deaths_count = result["count"]
            c.execute("SELECT COUNT(*) as count FROM char_levelups")
            result = c.fetchone()
            if result is not None:
                levels_count = result["count"]
        finally:
            c.close()

    embed = discord.Embed(description="*Beep boop beep boop*. I'm just a bot!")
    embed.set_author(name="NabBot", url="https://github.com/Galarzaa90/NabBot",
                     icon_url="https://assets-cdn.github.com/favicon.ico")
    embed.add_field(name="Authors", value="@Galarzaa#8515, @Nezune#2269")
    embed.add_field(name="Platform", value="Python " + EMOJI[":snake:"])
    embed.add_field(name="Created", value="March 30th 2016")
    embed.add_field(name="Servers", value="{0:,}".format(len(bot.servers)))
    embed.add_field(name="Members", value="{0:,}".format(len(set(bot.get_all_members()))))
    if not lite_mode:
        embed.add_field(name="Tracked users", value="{0:,}".format(user_count))
        embed.add_field(name="Tracked chars", value="{0:,}".format(char_count))
        embed.add_field(name="Tracked deaths", value="{0:,}".format(deaths_count))
        embed.add_field(name="Tracked level ups", value="{0:,}".format(levels_count))

    embed.add_field(name="Uptime", value=get_uptime())
    memory_usage = psutil.Process().memory_full_info().uss / 1024 ** 2
    embed.add_field(name='Memory Usage', value='{:.2f} MiB'.format(memory_usage))
    yield from bot.say(embed=embed)


@bot.group(pass_context=True, aliases=["event"], invoke_without_command=True)
@checks.is_not_lite()
@asyncio.coroutine
def events(ctx):
    """Shows a list of current active events"""
    permissions = ctx.message.channel.permissions_for(get_member(bot, bot.user.id, ctx.message.server))
    if not permissions.embed_links:
        yield from bot.say("Sorry, I need `Embed Links` permission for this command.")
        return
    time_threshold = 60 * 30
    now = time.time()
    c = userDatabase.cursor()
    server = ctx.message.server
    try:
        # If this is used on a PM, show events for all shared servers
        if ctx.message.channel.is_private:
            servers = get_user_servers(bot, ctx.message.author.id)
        else:
            servers = [ctx.message.server]
        servers_ids = [s.id for s in servers]
        placeholders = ", ".join("?" for s in servers)
        embed = discord.Embed(description="For more info about an event, use `/event info (id)`"
                                          "\nTo receive notifications for an event, use `/event sub (id)`")
        c.execute("SELECT creator, start, name, id, server FROM events "
                  "WHERE start < {0} AND start > {1} AND active = 1 AND server IN ({2}) "
                  "ORDER by start ASC".format(now, now - time_threshold, placeholders), tuple(servers_ids))
        recent_events = c.fetchall()
        c.execute("SELECT creator, start, name, id, server FROM events "
                  "WHERE start > {0} AND active = 1 AND server IN ({1})"
                  "ORDER BY start ASC".format(now, placeholders), tuple(servers_ids))
        upcoming_events = c.fetchall()
        if len(recent_events) + len(upcoming_events) == 0:
            yield from bot.say("There are no upcoming events.")
            return
        # Recent events
        if recent_events:
            name = "Recent events"
            value = ""
            for event in recent_events:
                author = get_member(bot, event["creator"], server)
                event["author"] = "unknown" if author is None else (author.display_name if server else author.name)
                time_diff = timedelta(seconds=now - event["start"])
                minutes = round((time_diff.seconds/60) % 60)
                event["start_str"] = "Started {0} minutes ago".format(minutes)
                value += "\n**{name}** (by **@{author}**,*ID:{id}*) - {start_str}".format(**event)
            embed.add_field(name=name, value=value, inline=False)
        # Upcoming events
        if upcoming_events:
            name = "Upcoming events"
            value = ""
            for event in upcoming_events:
                author = get_member(bot, event["creator"])
                event["author"] = "unknown" if author is None else (author.display_name if server else author.name)
                time_diff = timedelta(seconds=event["start"]-now)
                days, hours, minutes = time_diff.days, time_diff.seconds // 3600, (time_diff.seconds // 60) % 60
                if days:
                    event["start_str"] = 'In {0} days, {1} hours and {2} minutes'.format(days, hours, minutes)
                elif hours:
                    event["start_str"] = 'In {0} hours and {1} minutes'.format(hours, minutes)
                elif minutes > 0:
                    event["start_str"] = 'In {0} minutes'.format(minutes)
                else:
                    event["start_str"] = 'Starting now!'
                value += "\n**{name}** (by **@{author}**,*ID:{id}*) - {start_str}".format(**event)
            embed.add_field(name=name, value=value, inline=False)
        yield from bot.say(embed=embed)
    finally:
        c.close()


@events.command(pass_context=True, name="info", aliases=["show", "details"])
@checks.is_not_lite()
@asyncio.coroutine
def event_info(ctx, event_id: int):
    """Displays an event's info"""
    permissions = ctx.message.channel.permissions_for(get_member(bot, bot.user.id, ctx.message.server))
    if not permissions.embed_links:
        yield from bot.say("Sorry, I need `Embed Links` permission for this command.")
        return
    c = userDatabase.cursor()
    server = ctx.message.server
    try:
        # If this is used on a PM, show events for all shared servers
        if ctx.message.channel.is_private:
            servers = get_user_servers(bot, ctx.message.author.id)
        else:
            servers = [ctx.message.server]
        servers_ids = [s.id for s in servers]
        placeholders = ", ".join("?" for s in servers)

        c.execute("SELECT * FROM events "
                  "WHERE id = {0} AND active = 1 and server IN ({1})".format(event_id, placeholders), tuple(servers_ids))
        event = c.fetchone()
        if not event:
            yield from bot.say("There's no event with that id.")
            return
        start = datetime.utcfromtimestamp(event["start"])
        embed = discord.Embed(title=event["name"], description=event["description"], timestamp=start)
        author = get_member(bot, event["creator"], server)
        footer = "Start time"
        footer_icon = ""
        if author is not None:
            if server is None:
                author_name = author.name
            else:
                author_name = author.display_name
            footer = "Created by "+author_name+" | Start time"
            footer_icon = author.avatar_url if author.avatar_url else author.default_avatar_url
        embed.set_footer(text=footer, icon_url=footer_icon)
        yield from bot.say(embed=embed)
    finally:
        c.close()


@events.command(name="add", pass_context=True)
@checks.is_not_lite()
@asyncio.coroutine
def event_add(ctx, starts_in: TimeString, *, params):
    """Adds an event

    The syntax is:
    /event starts_in name
    /event starts_in name,description

    starts_in means in how much time the event will start since the moment of creation
    The time can be set using units such as 'd' for days, 'h' for hours, 'm' for minutes and 'd' for seconds.
    Examples: 1d20h5m, 1d30m, 1h40m, 40m
    """
    now = time.time()
    creator = ctx.message.author.id
    start = now+starts_in.seconds
    params = params.split(",", 1)
    name = single_line(clean_string(ctx, params[0]))
    event_description = ""
    if len(params) > 1:
        event_description = clean_string(ctx, params[1])

    c = userDatabase.cursor()
    try:
        c.execute("SELECT creator FROM events WHERE creator = ? AND active = 1 AND start > ?", (creator, now,))
        result = c.fetchall()
        if len(result) > 1 and creator not in owner_ids+mod_ids:
            yield from bot.say("You can only have two running events simultaneously. Delete or edit an active event")
            return

        servers = get_user_servers(bot, creator)
        # If message is via PM, but user only shares one server, we just consider that server
        if ctx.message.channel.is_private and len(servers) == 1:
            server = servers[0]
        # Not a private message, so we just take current server
        elif not ctx.message.channel.is_private:
            server = ctx.message.server
        # PM and user shares multiple servers, we must ask him for which server is the event
        else:
            yield from bot.say("For which server is this event? Choose one (number only)" +
                               "\n\t0: *Cancel*\n\t" +
                               "\n\t".join(["{0}: **{1.name}**".format(i+1, j) for i, j in enumerate(servers)]))
            reply = yield from bot.wait_for_message(author=ctx.message.author, channel=ctx.message.channel,
                                                    timeout=50.0)
            if reply is None:
                yield from bot.say("Nothing? Forget it then.")
                return
            elif is_numeric(reply.content):
                answer = int(reply.content)
                if answer == 0:
                    yield from bot.say("Changed your mind? Typical human.")
                    return
                try:
                    server = servers[answer-1]
                except IndexError:
                    yield from bot.say("That wasn't in the choices, you ruined it. Start from the beginning.")
                    return
            else:
                yield from bot.say("That's not a valid answer, try the command again.")
                return

        c.execute("INSERT INTO events (creator,server,start,name,description) VALUES(?,?,?,?,?)",
                  (creator, server.id, start, name, event_description))
        event_id = c.lastrowid
        reply = "Event registered successfully.\n\t**{0}** in *{1}*.\n*To edit this event use ID {2}*"
        yield from bot.say(reply.format(name, starts_in.original, event_id))
    finally:
        userDatabase.commit()
        c.close()


@event_add.error
@checks.is_not_lite()
@asyncio.coroutine
def event_add_error(error, ctx):
    if isinstance(error, commands.BadArgument):
        yield from bot.say(str(error))


@events.command(name="editname", pass_context=True)
@checks.is_not_lite()
@asyncio.coroutine
def event_edit_name(ctx, event_id: int, *, new_name):
    """Changes an event's name

    Only the creator of the event or mods can edit an event's name
    Only upcoming events can be edited"""
    c = userDatabase.cursor()
    now = time.time()
    new_name = single_line(clean_string(ctx, new_name))
    try:
        c.execute("SELECT creator, name FROM events WHERE id = ? AND active = 1 AND start > ?", (event_id, now,))
        event = c.fetchone()
        if not event:
            yield from bot.say("There are no active events with that ID.")
            return
        if event["creator"] != int(ctx.message.author.id) and ctx.message.author.id not in mod_ids+owner_ids:
            yield from bot.say("You can only edit your own events.")
            return
        yield from bot.say("Do you want to change the name of **{0}**? `(yes/no)`".format(event["name"]))
        answer = yield from bot.wait_for_message(author=ctx.message.author, channel=ctx.message.channel, timeout=30.0)
        if answer is None:
            yield from bot.say("I will take your silence as a no...")
        elif answer.content.lower() in ["yes", "y"]:
            c.execute("UPDATE events SET name = ? WHERE id = ?", (new_name, event_id,))
            yield from bot.say("Your event was renamed successfully to **{0}**.".format(new_name))
        else:
            yield from bot.say("Ok, nevermind.")
    finally:
        userDatabase.commit()
        c.close()


@events.command(name="editdesc", aliases=["editdescription"], pass_context=True)
@checks.is_not_lite()
@asyncio.coroutine
def event_edit_description(ctx, event_id: int, *, new_description):
    """Changes an event's description

    Only the creator of the event or mods can edit an event's description
    Only upcoming events can be edited"""
    c = userDatabase.cursor()
    now = time.time()
    new_description = clean_string(ctx, new_description)
    try:
        c.execute("SELECT creator FROM events WHERE id = ? AND active = 1 AND start > ?", (event_id, now,))
        event = c.fetchone()
        if not event:
            yield from bot.say("There are no active events with that ID.")
            return
        if event["creator"] != int(ctx.message.author.id) and ctx.message.author.id not in mod_ids+owner_ids:
            yield from bot.say("You can only edit your own events.")
            return
        yield from bot.say("Do you want to change the description of **{0}**? `(yes/no)`")
        answer = yield from bot.wait_for_message(author=ctx.message.author, channel=ctx.message.channel, timeout=30.0)
        if answer is None:
            yield from bot.say("I will take your silence as a no...")
        elif answer.content.lower() in ["yes", "y"]:
            c.execute("UPDATE events SET description = ? WHERE id = ?", (new_description, event_id,))
            yield from bot.say("Your event's description was changed successfully to **{0}**.".format(new_description))
        else:
            yield from bot.say("Ok, nevermind.")

    finally:
        userDatabase.commit()
        c.close()


@events.command(name="edittime", aliases=["editstart"], pass_context=True)
@checks.is_not_lite()
@asyncio.coroutine
def event_edit_time(ctx, event_id: int, starts_in: TimeString):
    """Changes an event's time

    Only the creator of the event or mods can edit an event's time
    Only upcoming events can be edited"""
    c = userDatabase.cursor()
    now = time.time()
    try:
        c.execute("SELECT creator, name FROM events WHERE id = ? AND active = 1 AND start > ?", (event_id, now,))
        event = c.fetchone()
        if not event:
            yield from bot.say("There are no active events with that ID.")
            return
        if event["creator"] != int(ctx.message.author.id) and ctx.message.author.id not in mod_ids+owner_ids:
            yield from bot.say("You can only edit your own events.")
            return
        yield from bot.say("Do you want to change the start time of '**{0}**'? `(yes/no)`".format(event["name"]))
        answer = yield from bot.wait_for_message(author=ctx.message.author, channel=ctx.message.channel, timeout=30.0)
        if answer is None:
            yield from bot.say("I will take your silence as a no...")
        elif answer.content.lower() in ["yes", "y"]:
            c.execute("UPDATE events SET start = ? WHERE id = ?", (now+starts_in.seconds, event_id,))
            yield from bot.say(
                "Your event's start time was changed successfully to **{0}**.".format(starts_in.original))
        else:
            yield from bot.say("Ok, nevermind.")
    finally:
        userDatabase.commit()
        c.close()


@events.command(name="delete", aliases=["remove"], pass_context=True)
@checks.is_not_lite()
@asyncio.coroutine
def event_remove(ctx, event_id: int):
    """Deletes an event

    Only the creator of the event or mods can delete an event
    Only upcoming events can be edited"""
    c = userDatabase.cursor()
    now = time.time()
    try:
        c.execute("SELECT creator,name FROM events WHERE id = ? AND active = 1 AND start > ?", (event_id, now,))
        event = c.fetchone()
        if not event:
            yield from bot.say("There are no active events with that ID.")
            return
        if event["creator"] != int(ctx.message.author.id) and ctx.message.author.id not in mod_ids+owner_ids:
            yield from bot.say("You can only delete your own events.")
            return
        yield from bot.say("Do you want to delete the event '**{0}**'? `(yes/no)`".format(event["name"]))
        answer = yield from bot.wait_for_message(author=ctx.message.author, channel=ctx.message.channel, timeout=30.0)
        if answer is None:
            yield from bot.say("I will take your silence as a no...")
        elif answer.content.lower() in ["yes", "y"]:
            c.execute("UPDATE events SET active = 0 WHERE id = ?", (event_id,))
            yield from bot.say("Your event was deleted successfully.")
        else:
            yield from bot.say("Ok, nevermind.")
    finally:
        userDatabase.commit()
        c.close()


@events.command(pass_context=True, name="make", aliases=["creator", "maker"])
@checks.is_not_lite()
@asyncio.coroutine
def event_make(ctx):
    """Creates an event guiding you step by step

    Instead of using confusing parameters, commas and spaces, this commands has the bot ask you step by step."""
    author = ctx.message.author
    creator = author.id
    now = time.time()
    c = userDatabase.cursor()
    try:
        c.execute("SELECT creator FROM events WHERE creator = ? AND active = 1 AND start > ?", (creator, now,))
        event = c.fetchall()
        if len(event) > 1 and creator not in owner_ids + mod_ids:
            return
        yield from bot.say("Let's create an event. What would you like the name to be?")

        name = yield from bot.wait_for_message(author=author, channel=ctx.message.channel, timeout=50.0)
        if name is None:
            yield from bot.say("...You took to long. Try the command again.")
            return
        name = single_line(name.clean_content)

        yield from bot.say("Alright, what description would you like the event to have? `(no/none = no description)`")
        event_description = yield from bot.wait_for_message(author=author, channel=ctx.message.channel, timeout=50.0)
        if event_description is None:
            yield from bot.say("...You took too long. Try the command again.")
            return
        elif event_description.content.lower().strip() in ["no", "none"]:
            yield from bot.say("No description then? Alright, now tell me the start time of the event from now. "
                               "`e.g. 2d1h20m, 2d3h`")
            event_description = ""
        else:
            event_description = event_description.clean_content
            yield from bot.say("Alright, now tell me the start time of the event from now. `e.g. 2d1h20m, 2d3h`")

        starts_in = yield from bot.wait_for_message(author=author, channel=ctx.message.channel, timeout=50.0)
        if starts_in is None:
            yield from bot.say("...You took too long. Try the command again.")
            return
        try:
            starts_in = TimeString(starts_in.content)
        except commands.BadArgument:
            yield from bot.say("Invalid time. Try  the command again. `Time examples: 1h2m, 2d30m, 40m, 5h`")
            return

        servers = get_user_servers(bot, creator)
        # If message is via PM, but user only shares one server, we just consider that server
        if ctx.message.channel.is_private and len(servers) == 1:
            server = servers[0]
        # Not a private message, so we just take current server
        elif not ctx.message.channel.is_private:
            server = ctx.message.server
        # PM and user shares multiple servers, we must ask him for which server is the event
        else:
            yield from bot.say("One more question...for which server is this event? Choose one (number only)" +
                               "\n\t0: *Cancel*\n\t" +
                               "\n\t".join(["{0}: **{1.name}**".format(i+1, j) for i, j in enumerate(servers)]))
            reply = yield from bot.wait_for_message(author=ctx.message.author, channel=ctx.message.channel,
                                                    timeout=50.0)
            if reply is None:
                yield from bot.say("Nothing? Forget it then.")
                return
            elif is_numeric(reply.content):
                answer = int(reply.content)
                if answer == 0:
                    yield from bot.say("Changed your mind? Typical human.")
                    return
                try:
                    server = servers[answer-1]
                except IndexError:
                    yield from bot.say("That wasn't in the choices, you ruined it. Start from the beginning.")
                    return
            else:
                yield from bot.say("That's not a valid answer, try the command again.")
                return

        now = time.time()
        c.execute("INSERT INTO events (creator,server,start,name,description) VALUES(?,?,?,?,?)",
                  (creator, server.id, now+starts_in.seconds, name, event_description))
        event_id = c.lastrowid
        reply = "Event registered successfully.\n\t**{0}** in *{1}*.\n*To edit this event use ID {2}*"
        yield from bot.say(reply.format(name, starts_in.original, event_id))
    finally:
        userDatabase.commit()
        c.close()


@events.command(pass_context=True, name="subscribe", aliases=["sub"])
@checks.is_not_lite()
@asyncio.coroutine
def event_subscribe(ctx, event_id: int):
    """Subscribe to receive a PM when an event is happening."""
    c = userDatabase.cursor()
    author = ctx.message.author
    now = time.time()
    try:
        # If this is used on a PM, show events for all shared servers
        if ctx.message.channel.is_private:
            servers = get_user_servers(bot, ctx.message.author.id)
        else:
            servers = [ctx.message.server]
        servers_ids = [s.id for s in servers]
        placeholders = ", ".join("?" for s in servers)
        c.execute("SELECT * FROM events "
                  "WHERE id = {0} AND active = 1 AND start > {1} AND server IN ({2})".format(event_id, now, placeholders)
                  , tuple(servers_ids))
        event = c.fetchone()
        if event is None:
            yield from bot.say("There are no active events with that id.")
            return

        c.execute("SELECT * FROM event_subscribers WHERE event_id = ? AND user_id = ?", (event_id, author.id))
        subscription = c.fetchone()
        if subscription is not None:
            yield from bot.say("You're already subscribed to this event.")
            return
        yield from bot.say("Do you want to subscribe to **{0}**? `(yes/no)`".format(event["name"]))
        reply = yield from bot.wait_for_message(author=author, channel=ctx.message.channel, timeout=30.0)
        if reply is None:
            yield from bot.say("No answer? Nevermind then.")
        elif reply.content.lower() in ["yes", "y"]:
            c.execute("INSERT INTO event_subscribers (event_id, user_id) VALUES(?,?)", (event_id, author.id))
            yield from bot.say("You have subscribed successfully to this event. I'll let you know when it's happening.")
        else:
            yield from bot.say("No? Alright then...")
    finally:
        c.close()
        userDatabase.commit()


@event_add.error
@asyncio.coroutine
def event_error(error, ctx):
    if isinstance(error, commands.BadArgument):
        yield from bot.say(str(error))
    elif isinstance(error, commands.errors.MissingRequiredArgument):
        yield from bot.say("You're missing a required argument. `Type /help {0}`".format(ctx.invoked_subcommand))


@event_edit_name.error
@event_edit_description.error
@event_edit_time.error
@event_remove.error
@event_subscribe.error
@asyncio.coroutine
def event_error(error, ctx):
    if isinstance(error, commands.BadArgument):
        yield from bot.say("Invalid arguments used. `Type /help {0}`".format(ctx.invoked_subcommand))
    elif isinstance(error, commands.errors.MissingRequiredArgument):
        yield from bot.say("You're missing a required argument. `Type /help {0}`".format(ctx.invoked_subcommand))


@bot.command(pass_context=True, no_pm=True, name="server", aliases=["serverinfo", "server_info"])
@asyncio.coroutine
def info_server(ctx):
    """Shows the server's information."""
    permissions = ctx.message.channel.permissions_for(get_member(bot, bot.user.id, ctx.message.server))
    if not permissions.embed_links:
        yield from bot.say("Sorry, I need `Embed Links` permission for this command.")
        return
    embed = discord.Embed()
    _server = ctx.message.server  # type: discord.Server
    embed.set_thumbnail(url=_server.icon_url)
    embed.description = _server.name
    # Check if owner has a nickname
    if _server.owner.name == _server.owner.display_name:
        owner = "{0.name}#{0.discriminator}".format(_server.owner)
    else:
        owner = "{0.display_name}\n({0.name}#{0.discriminator})".format(_server.owner)
    embed.add_field(name="Owner", value=owner)
    embed.add_field(name="Created", value=_server.created_at.strftime("%d/%m/%y"))
    embed.add_field(name="Server Region", value=get_region_string(_server.region))

    # Channels
    text_channels = 0
    for channel in _server.channels:
        if channel.type == discord.ChannelType.text:
            text_channels += 1
    voice_channels = len(_server.channels) - text_channels
    embed.add_field(name="Text channels", value=text_channels)
    embed.add_field(name="Voice channels", value=voice_channels)
    embed.add_field(name="Members", value=_server.member_count)
    embed.add_field(name="Roles", value=len(_server.roles))
    embed.add_field(name="Emojis", value=len(_server.emojis))
    embed.add_field(name="Bot joined", value=_server.me.joined_at.strftime("%d/%m/%y"))
    yield from bot.say(embed=embed)


@bot.command(pass_context=True, no_pm=True)
@asyncio.coroutine
def roles(ctx, *userName:str):
    """Shows a list of roles or an user's roles"""
    userName = " ".join(userName).strip()
    msg = "These are the active roles for "

    if not userName:
        msg += "this server:\r\n"

        for role in get_role_list(ctx.message.server):
            msg += role.name + "\r\n"
    else:
        user = get_member_by_name(bot, userName)

        if user is None:
            msg = "I don't see any user named **" + userName + "**. \r\n"
            msg += "I can only check roles from an username registered on this server."
        else:
            msg += "**" + user.display_name + "**:\r\n"
            roles = []

            # Ignoring "default" roles
            for role in user.roles:
                if role.name not in ["@everyone", "Nab Bot"]:
                    roles.append(role.name)

            # There shouldn't be anyone without active roles, but since people can check for NabBot,
            # might as well show a specific message.
            if roles:
                for roleName in roles:
                    msg += roleName + "\r\n"
            else:
                msg = "There are no active roles for **" + user.display_name + "**."

    yield from bot.say(msg)
    return


@bot.command(pass_context=True, no_pm=True)
@asyncio.coroutine
def role(ctx, *roleName: str):
    """Shows member list within the specified role"""
    roleName = " ".join(roleName).strip()
    lowerRoleName = roleName.lower()
    roleDict = {}

    # Need to get all roles and check all members because there's
    # no API call like role.getMembers
    for role in get_role_list(ctx.message.server):
        if role.name.lower() == lowerRoleName:
            roleDict[role] = []

    if len(roleDict) > 0:
        # Check every member and add to dict for each role he is in
        # In this case, the dict will only have the specific role searched
        for member in ctx.message.server.members:
            for role in member.roles:
                if role in roleDict:
                    roleDict[role].append(member.display_name)
                    # Getting the name directly from server to respect case
                    roleName = role.name

        # Create return message
        msg = "These are the members from **" + roleName + "**:\r\n"

        for key, value in roleDict.items():
            if len(value) < 1:
                msg = "There are no members for this role yet."
            else:
                for memberName in roleDict[key]:
                    msg += "\t" + memberName + "\r\n"

        yield from bot.say(msg)
    else:
        yield from bot.say("I couldn't find a role with that name.")

    return


@asyncio.coroutine
def game_update():
    game_list = ["Half-Life 3", "Tibia on Steam", "DOTA 3", "Human Simulator 2017", "Russian Roulette",
                 "with my toy humans", "with fire"+EMOJI[":fire:"], "God", "innocent", "the part", "hard to get",
                 "with my human minions", "Singularity", "Portal 3", "Dank Souls"]
    yield from bot.wait_until_ready()
    while not bot.is_closed:
        yield from bot.change_presence(game=discord.Game(name=random.choice(game_list)))
        yield from asyncio.sleep(60*20)  # Change game every 20 minutes


if __name__ == "__main__":
    init_database()
    reload_worlds()
    reload_welcome_messages()
    reload_announce_channels()

    print("Attempting login...")

    login = getLogin()
    try:
        token = login.token
    except NameError:
        token = ""

    try:
        email = login.email
        password = login.password
    except NameError:
        email = ""
        password = ""
    try:
        if token:
            bot.run(token)
        elif email and password:
            bot.run(login.email, login.password)
        else:
            print("No login data found. Edit or delete login.py and restart.")
            input("\nPress any key to continue...")
            quit()
    except discord.errors.LoginFailure:
        print("Incorrect login data. Edit or delete login.py and restart.")
        input("\nPress any key to continue...")
        quit()
    finally:
        bot.session.close()

    log.error("NabBot crashed")
