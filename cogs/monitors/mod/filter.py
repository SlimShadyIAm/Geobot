import json
import re
from datetime import datetime, timezone

import aiohttp
import discord
from aiocache.decorators import cached
from data.services import guild_service
from discord.ext import commands
from utils import cfg, logger, scam_cache
from utils.framework import gatekeeper, find_triggered_filters
from utils.mod import mute
from utils.views import manual_report, report


class Filter(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.invite_filter = r'(?:https?://)?discord(?:(?:app)?\.com/invite|\.gg)\/{1,}[a-zA-Z0-9]+/?'
        self.spoiler_filter = r'\|\|(.*?)\|\|'
        self.spam_cooldown = commands.CooldownMapping.from_cooldown(
            2, 10.0, commands.BucketType.member)

    @commands.Cog.listener()
    async def on_reaction_add(self, reaction: discord.Reaction, reacter: discord.Member):
        """Generate a report when a moderator reacts the stop sign emoji on a message

        Parameters
        ----------
        reaction : discord.Reaction
            [description]
        reacter : discord.Member
            [description]
        """
        if reaction.message.guild is None:
            return
        if reaction.message.guild.id != cfg.guild_id:
            return
        if reaction.message.author.bot:
            return
        if reaction.emoji != '🛑':
            return
        if not gatekeeper.has(reacter.guild, reacter, 5):
            return
        if reacter.top_role <= reaction.message.author.top_role:
            return

        await reaction.message.remove_reaction(reaction.emoji, reacter)
        await manual_report(reacter, reaction.message)

    @commands.Cog.listener()
    async def on_message(self, message):
        await self.run_filter(message)

    @commands.Cog.listener()
    async def on_message_edit(self, _, message):
        await self.run_filter(message)

    @commands.Cog.listener()
    async def on_member_update(self, _, member: discord.Member):
        await self.nick_filter(member)

    async def run_filter(self, message: discord.Message):
        if not message.guild:
            return
        if message.guild.id != cfg.guild_id:
            return
        if message.author.bot:
            return
        if gatekeeper.has(message.guild, message.author, 7):
            return
        db_guild = guild_service.get_guild()
        role_submod = message.guild.get_role(db_guild.role_sub_mod)
        if role_submod is not None and role_submod in message.author.roles:
            return

        # run through filters
        if message.content and await self.bad_word_filter(message, db_guild):
            return

        if gatekeeper.has(message.guild, message.author, 6):
            return

        if message.content and await self.scam_filter(message):
            return

        if gatekeeper.has(message.guild, message.author, 5):
            return

        if message.content and await self.do_invite_filter(message, db_guild):
            return
        # if await self.do_spoiler_newline_filter(message, db_guild):
        #     return

        # await self.detect_cij_or_eta(message, db_guild)

    async def nick_filter(self, member):
        triggered_words = find_triggered_filters(
            member.display_name, member)

        if not triggered_words:
            return

        await member.edit(nick="change name pls")
        embed = discord.Embed(title="Nickname changed",
                              color=discord.Color.orange())
        embed.description = f"Your nickname contained the word **{triggered_words[0].word}** which is a filtered word. Please change your nickname or ask a Moderator to do it for you."
        try:
            await member.send(embed=embed)
        except Exception:
            pass

    async def bad_word_filter(self, message, db_guild) -> bool:
        triggered_words = find_triggered_filters(
            message.content, message.author)
        if not triggered_words:
            return

        dev_role = message.guild.get_role(db_guild.role_dev)

        triggered = False
        for word in triggered_words:
            if word.piracy:
                # ignore if it's a dev saying piracy in #development
                if message.channel.id == db_guild.channel_development and dev_role in message.author.roles:
                    continue

            if word.notify:
                await self.delete(message)
                await self.ratelimit(message)
                await self.do_filter_notify(message, word.word)
                await report(self.bot, message, word.word)
                return

            triggered = True

        if triggered:
            await self.delete(message)
            await self.ratelimit(message)
            await self.do_filter_notify(message, word.word)

        return triggered

    async def do_invite_filter(self, message, db_guild):
        invites = re.findall(self.invite_filter, message.content, flags=re.S)
        if not invites:
            return

        whitelist = db_guild.filter_excluded_guilds
        for invite in invites:
            try:
                invite = await self.bot.fetch_invite(invite)

                id = None
                if isinstance(invite, discord.Invite):
                    if invite.guild is not None:
                        id = invite.guild.id
                    else:
                        id = 123
                elif isinstance(invite, discord.PartialInviteGuild) or isinstance(invite, discord.PartialInviteChannel):
                    id = invite.id

                if id not in whitelist:
                    await self.delete(message)
                    await self.ratelimit(message)
                    await report(self.bot, message, invite, invite=invite)
                    return True

            except discord.NotFound:
                await self.delete(message)
                await self.ratelimit(message)
                await report(self.bot, message, invite, invite=invite)
                return True

        return False

    async def do_spoiler_newline_filter(self, message: discord.Message, db_guild):
        """
        SPOILER FILTER
        """
        if re.search(self.spoiler_filter, message.content, flags=re.S):
            # ignore if dev in dev channel
            dev_role = message.guild.get_role(db_guild.role_dev)
            if message.channel.id == db_guild.channel_development and dev_role in message.author.roles:
                return False

            await self.delete(message)
            return True

        for a in message.attachments:
            if a.is_spoiler():
                await self.delete(message)
                return True

        """
        NEWLINE FILTER
        """
        if len(message.content.splitlines()) > 100:
            dev_role = message.guild.get_role(db_guild.role_dev)
            if not dev_role or dev_role not in message.author.roles:
                await self.delete(message)
                await self.ratelimit(message)
                return True

        return False

    async def scam_filter(self, message: discord.Message):
        for url in scam_cache.scam_jb_urls:
            if url in message.content.lower():
                embed = discord.Embed(
                    title="Fake or scam jailbreak", color=discord.Color.red())
                embed.description = f"Your message contained the link to a **fake jailbreak** ({url}).\n\nIf you installed this jailbreak, remove it from your device immediately and try to get a refund if you paid for it. Jailbreaks *never* cost money and will not ask for any form of payment or survey to install them."
                await self.delete(message)
                await self.ratelimit(message)
                await message.channel.send(f"{message.author.mention}", embed=embed)
                return True

        for url in scam_cache.scam_unlock_urls:
            if url in message.content.lower():
                embed = discord.Embed(
                    title="Fake or scam unlock", color=discord.Color.red())
                embed.description = f"Your message contained the link to a **fake unlock** ({url}).\n\nIf you bought a phone second-hand and it arrived iCloud locked, contact the seller to remove it [using these instructions](https://support.apple.com/en-us/HT201351), or get a refund.\n\nIf you or a relative are the original owner of the device and you can provide the original proof of purchase, Apple Support can remove the lock.\nPlease refer to these articles: [How to remove Activation Lock](https://support.apple.com/HT201441) or [If you forgot your iPhone passcode](https://support.apple.com/HT204306)."
                await self.delete(message)
                await self.ratelimit(message)
                await message.channel.send(f"{message.author.mention}", embed=embed)
                return True

        return False

    async def ratelimit(self, message: discord.Message):
        current = message.created_at.replace(tzinfo=timezone.utc).timestamp()
        bucket = self.spam_cooldown.get_bucket(message)
        if bucket.update_rate_limit(current) and not message.author.is_timed_out():
            try:
                ctx = await self.bot.get_context(message)
                await mute(ctx, message.author, mod=ctx.guild.me, dur_seconds=15*60, reason="Filter spam")
            except Exception:
                return

    async def do_filter_notify(self, message: discord.Message, word):
        member = message.author
        channel = message.channel
        message_to_user = f"Your message contained a word you aren't allowed to say in {member.guild.name}. This could be either hate speech or the name of a piracy tool/source. Please refrain from saying it!"
        footer = "Repeatedly triggering the filter will automatically result in a mute."
        try:
            embed = discord.Embed(
                description=f"{message_to_user}\n\nFiltered word found: **{word}**", color=discord.Color.orange())
            embed.set_footer(text=footer)
            await member.send(embed=embed)
        except Exception:
            embed = discord.Embed(description=message_to_user,
                                  color=discord.Color.orange())
            embed.set_footer(text=footer)
            await channel.send(member.mention, embed=embed, delete_after=10)

        log_embed = discord.Embed(title="Filter Triggered")
        log_embed.color = discord.Color.red()
        log_embed.add_field(
            name="Member", value=f"{member} ({member.mention})")
        log_embed.add_field(name="Word", value=word)
        log_embed.add_field(
            name="Message", value=message.content, inline=False)
        log_embed.timestamp = datetime.utcnow()
        log_embed.set_footer(text=message.author.id)

        log_channel = message.guild.get_channel(
            guild_service.get_guild().channel_private)
        if log_channel is not None:
            await log_channel.send(embed=log_embed)

    async def delete(self, message):
        try:
            await message.delete()
        except Exception:
            pass

    @cached(ttl=3600)
    async def fetch_cij_or_news_database(self):
        async with aiohttp.ClientSession() as session:
            async with session.get("https://raw.githubusercontent.com/DiscordGIR/CIJOrNewsFilter/main/database.json") as resp:
                if resp.status == 200:
                    data = await resp.text()
                    return json.loads(data)

                return {}

    async def detect_cij_or_eta(self, message: discord.Message, db_guild):
        if message.edited_at is not None:
            return
        if gatekeeper.has(message.guild, message.author, 1):
            return

        cij_filter_response = await self.fetch_cij_or_news_database()
        intent_cij = cij_filter_response.get("intent_cij")
        intent_news = cij_filter_response.get("intent_news")

        verb = cij_filter_response.get("verb")
        subject = cij_filter_response.get("subject")

        if None in [intent_cij, intent_news, verb]:
            logger.error(
                f"Something went wrong with CIJ or ETA filter; {intent_cij}, {intent_news}, {verb}")
            return

        text = message.content.lower()
        subject_and_word_in_message = any(
            v in text for v in verb) and any(s in text for s in subject)

        intent_news_triggered = any(intent in text for intent in intent_news)
        intent_cij_triggered = any(intent in text for intent in intent_cij)
        
        if (intent_news_triggered or intent_cij_triggered) and subject_and_word_in_message and message.channel.id == guild_service.get_guild().channel_general:
            view = discord.ui.View()
            embed = discord.Embed(color=discord.Color.orange())
            embed.description = f"Please keep support or jailbreak related messages in the appropriate channels. Thanks!"
            embed.set_footer(
                text="This action was performed automatically. Please disregard if incorrect.")
            view.add_item(discord.ui.Button(label='Genius Bar', emoji="<:Genius:947545923028922448>",
                                            url=f"https://discord.com/channels/349243932447604736/688124678707216399", style=discord.ButtonStyle.url))
            view.add_item(discord.ui.Button(label='Jailbreak Channel', emoji="<:Channel2:947546361715388417>",
                                            url=f"https://discord.com/channels/349243932447604736/688122301975363591", style=discord.ButtonStyle.url))
            res = await message.reply(embed=embed, view=view, delete_after=20)
        elif intent_news_triggered and subject_and_word_in_message:
            embed = discord.Embed(color=discord.Color.orange())
            embed.description = f"It appears you are asking about future jailbreaks. Nobody knows when a jailbreak will be released, but you can subscribe to notifications about releases by going to <id:customize>."
            embed.set_footer(
                text="This action was performed automatically. Please disregard if incorrect.")
            res = await message.reply(embed=embed, delete_after=20)
        elif intent_cij_triggered and subject_and_word_in_message:
            embed = discord.Embed(color=discord.Color.orange())
            embed.description = "It appears you are asking if you can jailbreak your device, you can find out that information by using `/canijailbreak` or in the \"Get Started\" section of ios.cfw.guide."
            embed.set_footer(
                text="This action was performed automatically. Please disregard if incorrect.")
            view = discord.ui.View()
            view.add_item(discord.ui.Button(label='Get Started', emoji="<:Guide:947350624385794079>",
                                            url=f"https://ios.cfw.guide/get-started/#required-reading", style=discord.ButtonStyle.url))
            view.add_item(discord.ui.Button(label='Jailbreak Chart', emoji="<:Search2:947525874297757706>",
                                            url=f"https://appledb.dev/", style=discord.ButtonStyle.url))

            await message.reply(embed=embed, view=view, delete_after=20)


async def setup(bot):
    await bot.add_cog(Filter(bot))
