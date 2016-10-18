# -*- coding: utf-8 -*-

"""
The MIT License (MIT)

Copyright (c) 2015-2016 Rapptz

Permission is hereby granted, free of charge, to any person obtaining a
copy of this software and associated documentation files (the "Software"),
to deal in the Software without restriction, including without limitation
the rights to use, copy, modify, merge, publish, distribute, sublicense,
and/or sell copies of the Software, and to permit persons to whom the
Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS
OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
DEALINGS IN THE SOFTWARE.
"""

from .guild import Guild
from .user import User
from .game import Game
from .emoji import Emoji
from .reaction import Reaction
from .message import Message
from .channel import *
from .member import Member
from .role import Role
from . import utils, compat
from .enums import Status, ChannelType, try_enum
from .calls import GroupCall

from collections import deque, namedtuple
import copy, enum, math
import datetime
import asyncio
import logging

class ListenerType(enum.Enum):
    chunk = 0

Listener = namedtuple('Listener', ('type', 'future', 'predicate'))
StateContext = namedtuple('StateContext', 'try_insert_user http')
log = logging.getLogger(__name__)
ReadyState = namedtuple('ReadyState', ('launch', 'guilds'))

class ConnectionState:
    def __init__(self, *, dispatch, chunker, syncer, http, loop, **options):
        self.loop = loop
        self.max_messages = max(options.get('max_messages', 5000), 100)
        self.dispatch = dispatch
        self.chunker = chunker
        self.syncer = syncer
        self.is_bot = None
        self._listeners = []
        self.ctx = StateContext(try_insert_user=self.try_insert_user, http=http)
        self.clear()

    def clear(self):
        self.user = None
        self.sequence = None
        self.session_id = None
        self._calls = {}
        self._users = {}
        self._guilds = {}
        self._voice_clients = {}
        self._private_channels = {}
        # extra dict to look up private channels by user id
        self._private_channels_by_user = {}
        self.messages = deque(maxlen=self.max_messages)

    def process_listeners(self, listener_type, argument, result):
        removed = []
        for i, listener in enumerate(self._listeners):
            if listener.type != listener_type:
                continue

            future = listener.future
            if future.cancelled():
                removed.append(i)
                continue

            try:
                passed = listener.predicate(argument)
            except Exception as e:
                future.set_exception(e)
                removed.append(i)
            else:
                if passed:
                    future.set_result(result)
                    removed.append(i)
                    if listener.type == ListenerType.chunk:
                        break

        for index in reversed(removed):
            del self._listeners[index]

    @property
    def voice_clients(self):
        return self._voice_clients.values()

    def _get_voice_client(self, guild_id):
        return self._voice_clients.get(guild_id)

    def _add_voice_client(self, guild_id, voice):
        self._voice_clients[guild_id] = voice

    def _remove_voice_client(self, guild_id):
        self._voice_clients.pop(guild_id, None)

    def _update_references(self, ws):
        for vc in self.voice_clients:
            vc.main_ws = ws

    def try_insert_user(self, data):
        # this way is 300% faster than `dict.setdefault`.
        user_id = int(data['id'])
        try:
            return self._users[user_id]
        except KeyError:
            self._users[user_id] = user = User(state=self.ctx, data=data)
            return user

    @property
    def guilds(self):
        return self._guilds.values()

    def _get_guild(self, guild_id):
        return self._guilds.get(guild_id)

    def _add_guild(self, guild):
        self._guilds[guild.id] = guild

    def _remove_guild(self, guild):
        self._guilds.pop(guild.id, None)

    @property
    def private_channels(self):
        return self._private_channels.values()

    def _get_private_channel(self, channel_id):
        return self._private_channels.get(channel_id)

    def _get_private_channel_by_user(self, user_id):
        return self._private_channels_by_user.get(user_id)

    def _add_private_channel(self, channel):
        self._private_channels[channel.id] = channel
        if isinstance(channel, DMChannel):
            self._private_channels_by_user[channel.recipient.id] = channel

    def _remove_private_channel(self, channel):
        self._private_channels.pop(channel.id, None)
        if isinstance(channel, DMChannel):
            self._private_channels_by_user.pop(channel.recipient.id, None)

    def _get_message(self, msg_id):
        return utils.find(lambda m: m.id == msg_id, self.messages)

    def _add_guild_from_data(self, guild):
        guild = Guild(data=guild, state=self.ctx)
        Guild.me = property(lambda s: s.get_member(self.user.id))
        Guild.voice_client = property(lambda s: self._get_voice_client(s.id))
        self._add_guild(guild)
        return guild

    def chunks_needed(self, guild):
        for chunk in range(math.ceil(guild._member_count / 1000)):
            yield self.receive_chunk(guild.id)

    @asyncio.coroutine
    def _delay_ready(self):
        launch = self._ready_state.launch
        while not launch.is_set():
            # this snippet of code is basically waiting 2 seconds
            # until the last GUILD_CREATE was sent
            launch.set()
            yield from asyncio.sleep(2, loop=self.loop)

        guilds = self._ready_state.guilds

        # get all the chunks
        chunks = []
        for guild in guilds:
            chunks.extend(self.chunks_needed(guild))

        # we only want to request ~75 guilds per chunk request.
        splits = [guilds[i:i + 75] for i in range(0, len(guilds), 75)]
        for split in splits:
            yield from self.chunker(split)

        # wait for the chunks
        if chunks:
            try:
                yield from asyncio.wait(chunks, timeout=len(chunks) * 30.0, loop=self.loop)
            except asyncio.TimeoutError:
                log.info('Somehow timed out waiting for chunks.')

        # remove the state
        try:
            del self._ready_state
        except AttributeError:
            pass # already been deleted somehow

        # call GUILD_SYNC after we're done chunking
        if not self.is_bot:
            log.info('Requesting GUILD_SYNC for %s guilds' % len(self.guilds))
            yield from self.syncer([s.id for s in self.guilds])

        # dispatch the event
        self.dispatch('ready')

    def parse_ready(self, data):
        self._ready_state = ReadyState(launch=asyncio.Event(), guilds=[])
        self.user = self.try_insert_user(data['user'])
        guilds = data.get('guilds')

        guilds = self._ready_state.guilds
        for guild_data in guilds:
            guild = self._add_server_from_data(guild_data)
            if not self.is_bot or guild.large:
                guilds.append(guild)

        for pm in data.get('private_channels'):
            factory, _ = _channel_factory(pm['type'])
            self._add_private_channel(factory(me=self.user, data=pm, state=self.ctx))

        compat.create_task(self._delay_ready(), loop=self.loop)

    def parse_resumed(self, data):
        self.dispatch('resumed')

    def parse_message_create(self, data):
        channel = self.get_channel(int(data['channel_id']))
        message = Message(channel=channel, data=data, state=self.ctx)
        self.dispatch('message', message)
        self.messages.append(message)

    def parse_message_delete(self, data):
        message_id = int(data['id'])
        found = self._get_message(message_id)
        if found is not None:
            self.dispatch('message_delete', found)
            self.messages.remove(found)

    def parse_message_delete_bulk(self, data):
        message_ids = set(map(int, data.get('ids', [])))
        to_be_deleted = list(filter(lambda m: m.id in message_ids, self.messages))
        for msg in to_be_deleted:
            self.dispatch('message_delete', msg)
            self.messages.remove(msg)

    def parse_message_update(self, data):
        message = self._get_message(int(data['id']))
        if message is not None:
            older_message = copy.copy(message)
            if 'call' in data:
                # call state message edit
                message._handle_call(data['call'])
            elif 'content' not in data:
                # embed only edit
                message.embeds = data['embeds']
            else:
                message._update(channel=message.channel, data=data)

            self.dispatch('message_edit', older_message, message)

    def parse_message_reaction_add(self, data):
        message = self._get_message(data['message_id'])
        if message is not None:
            emoji = self._get_reaction_emoji(**data.pop('emoji'))
            reaction = utils.get(message.reactions, emoji=emoji)

            is_me = data['user_id'] == self.user.id

            if not reaction:
                reaction = Reaction(
                    message=message, emoji=emoji, me=is_me, **data)
                message.reactions.append(reaction)
            else:
                reaction.count += 1
                if is_me:
                    reaction.me = True

            channel = self.get_channel(data['channel_id'])
            member = self._get_member(channel, data['user_id'])

            self.dispatch('reaction_add', reaction, member)

    def parse_message_reaction_remove_all(self, data):
        message =  self._get_message(data['message_id'])
        if message is not None:
            old_reactions = message.reactions.copy()
            message.reactions.clear()
            self.dispatch('reaction_clear', message, old_reactions)

    def parse_message_reaction_remove(self, data):
        message = self._get_message(data['message_id'])
        if message is not None:
            emoji = self._get_reaction_emoji(**data['emoji'])
            reaction = utils.get(message.reactions, emoji=emoji)

            # Eventual consistency means we can get out of order or duplicate removes.
            if not reaction:
                log.warning("Unexpected reaction remove {}".format(data))
                return

            reaction.count -= 1
            if data['user_id'] == self.user.id:
                reaction.me = False
            if reaction.count == 0:
                message.reactions.remove(reaction)

            channel = self.get_channel(data['channel_id'])
            member = self._get_member(channel, data['user_id'])

            self.dispatch('reaction_remove', reaction, member)

    def parse_presence_update(self, data):
        guild = self._get_guild(utils._get_as_snowflake(data, 'guild_id'))
        if guild is None:
            return

        status = data.get('status')
        user = data['user']
        member_id = user['id']
        member = guild.get_member(member_id)
        if member is None:
            if 'username' not in user:
                # sometimes we receive 'incomplete' member data post-removal.
                # skip these useless cases.
                return

            member = self._make_member(guild, data)
            guild._add_member(member)

        old_member = copy.copy(member)
        member._presence_update(data=data, user=user)
        self.dispatch('member_update', old_member, member)

    def parse_user_update(self, data):
        self.user = User(state=self.ctx, data=data)

    def parse_channel_delete(self, data):
        guild =  self._get_guild(utils._get_as_snowflake(data, 'guild_id'))
        channel_id = int(data['id'])
        if guild is not None:
            channel = guild.get_channel(channel_id)
            if channel is not None:
                guild._remove_channel(channel)
                self.dispatch('channel_delete', channel)
        else:
            # the reason we're doing this is so it's also removed from the
            # private channel by user cache as well
            channel = self._get_private_channel(channel_id)
            self._remove_private_channel(channel)

    def parse_channel_update(self, data):
        channel_type = try_enum(ChannelType, data.get('type'))
        channel_id = int(data['id'])
        if channel_type is ChannelType.group:
            channel = self._get_private_channel(channel_id)
            old_channel = copy.copy(channel)
            channel._update_group(data)
            self.dispatch('channel_update', old_channel, channel)
            return

        guild = self._get_guild(utils._get_as_snowflake(data, 'guild_id'))
        if guild is not None:
            channel = guild.get_channel(channel_id)
            if channel is not None:
                old_channel = copy.copy(channel)
                channel._update(guild, data)
                self.dispatch('channel_update', old_channel, channel)

    def parse_channel_create(self, data):
        factory, ch_type = _channel_factory(data['type'])
        channel = None
        if ch_type in (ChannelType.group, ChannelType.private):
            channel = factory(me=self.user, data=data, state=self.ctx)
            self._add_private_channel(channel)
        else:
            guild = self._get_guild(utils._get_as_snowflake(data, 'guild_id'))
            if guild is not None:
                channel = factory(guild=guild, state=self.ctx, data=data)
                guild._add_channel(channel)

        self.dispatch('channel_create', channel)

    def parse_channel_recipient_add(self, data):
        channel = self._get_private_channel(int(data['channel_id']))
        user = self.try_insert_user(data['user'])
        channel.recipients.append(user)
        self.dispatch('group_join', channel, user)

    def parse_channel_recipient_remove(self, data):
        channel = self._get_private_channel(int(data['channel_id']))
        user = self.try_insert_user(data['user'])
        try:
            channel.recipients.remove(user)
        except ValueError:
            pass
        else:
            self.dispatch('group_remove', channel, user)

    def _make_member(self, guild, data):
        roles = [guild.default_role]
        for roleid in data.get('roles', []):
            role = utils.get(guild.roles, id=roleid)
            if role is not None:
                roles.append(role)

        data['roles'] = sorted(roles, key=lambda r: r.id)
        return Member(guild=guild, data=data, state=self.ctx)

    def parse_guild_member_add(self, data):
        guild = self._get_guild(int(data['guild_id']))
        member = self._make_member(guild, data)
        guild._add_member(member)
        guild._member_count += 1
        self.dispatch('member_join', member)

    def parse_guild_member_remove(self, data):
        guild = self._get_guild(int(data['guild_id']))
        if guild is not None:
            user_id = data['user']['id']
            member = guild.get_member(user_id)
            if member is not None:
                guild._remove_member(member)
                guild._member_count -= 1

                # remove them from the voice channel member list
                vc = guild._voice_state_for(user_id)
                if vc:
                    voice_channel = vc.channel
                    if voice_channel is not None:
                        try:
                            voice_channel.voice_members.remove(member)
                        except ValueError:
                            pass

                self.dispatch('member_remove', member)

    def parse_guild_member_update(self, data):
        guild = self._get_guild(int(data['guild_id']))
        user = data['user']
        user_id = user['id']
        member = guild.get_member(user_id)
        if member is not None:
            old_member = copy.copy(member)
            member._update(data, user)
            self.dispatch('member_update', old_member, member)

    def parse_guild_emojis_update(self, data):
        guild = self._get_guild(int(data['guild_id']))
        before_emojis = guild.emojis
        guild.emojis = [Emoji(guild=guild, data=e, state=self.ctx) for e in data.get('emojis', [])]
        self.dispatch('guild_emojis_update', before_emojis, guild.emojis)

    def _get_create_guild(self, data):
        if data.get('unavailable') == False:
            # GUILD_CREATE with unavailable in the response
            # usually means that the guild has become available
            # and is therefore in the cache
            guild = self._get_guild(data.get('id'))
            if guild is not None:
                guild.unavailable = False
                guild._from_data(data)
                return guild

        return self._add_guild_from_data(data)

    @asyncio.coroutine
    def _chunk_and_dispatch(self, guild, unavailable):
        yield from self.chunker(guild)
        chunks = list(self.chunks_needed(guild))
        if chunks:
            try:
                yield from asyncio.wait(chunks, timeout=len(chunks), loop=self.loop)
            except asyncio.TimeoutError:
                log.info('Somehow timed out waiting for chunks.')

        if unavailable == False:
            self.dispatch('guild_available', guild)
        else:
            self.dispatch('guild_join', guild)

    def parse_guild_create(self, data):
        unavailable = data.get('unavailable')
        if unavailable == True:
            # joined a guild with unavailable == True so..
            return

        guild = self._get_create_guild(data)

        # check if it requires chunking
        if guild.large:
            if unavailable == False:
                # check if we're waiting for 'useful' READY
                # and if we are, we don't want to dispatch any
                # event such as guild_join or guild_available
                # because we're still in the 'READY' phase. Or
                # so we say.
                try:
                    state = self._ready_state
                    state.launch.clear()
                    state.guilds.append(guild)
                except AttributeError:
                    # the _ready_state attribute is only there during
                    # processing of useful READY.
                    pass
                else:
                    return

            # since we're not waiting for 'useful' READY we'll just
            # do the chunk request here
            compat.create_task(self._chunk_and_dispatch(guild, unavailable), loop=self.loop)
            return

        # Dispatch available if newly available
        if unavailable == False:
            self.dispatch('guild_available', guild)
        else:
            self.dispatch('guild_join', guild)

    def parse_guild_sync(self, data):
        guild = self._get_guild(int(data['id']))
        guild._sync(data)

    def parse_guild_update(self, data):
        guild = self._get_guild(int(data['id']))
        if guild is not None:
            old_guild = copy.copy(guild)
            guild._from_data(data)
            self.dispatch('guild_update', old_guild, guild)

    def parse_guild_delete(self, data):
        guild = self._get_guild(int(data['id']))
        if guild is None:
            return

        if data.get('unavailable', False) and guild is not None:
            # GUILD_DELETE with unavailable being True means that the
            # guild that was available is now currently unavailable
            guild.unavailable = True
            self.dispatch('guild_unavailable', guild)
            return

        # do a cleanup of the messages cache
        self.messages = deque((msg for msg in self.messages if msg.guild != guild), maxlen=self.max_messages)

        self._remove_guild(guild)
        self.dispatch('guild_remove', guild)

    def parse_guild_ban_add(self, data):
        # we make the assumption that GUILD_BAN_ADD is done
        # before GUILD_MEMBER_REMOVE is called
        # hence we don't remove it from cache or do anything
        # strange with it, the main purpose of this event
        # is mainly to dispatch to another event worth listening to for logging
        guild = self._get_guild(int(data['guild_id']))
        if guild is not None:
            user_id = data.get('user', {}).get('id')
            member = utils.get(guild.members, id=user_id)
            if member is not None:
                self.dispatch('member_ban', member)

    def parse_guild_ban_remove(self, data):
        guild = self._get_guild(int(data['guild_id']))
        if guild is not None:
            if 'user' in data:
                user = self.try_insert_user(data['user'])
                self.dispatch('member_unban', guild, user)

    def parse_guild_role_create(self, data):
        guild = self._get_guild(int(data['guild_id']))
        role_data = data['role']
        role = Role(guild=guild, data=role_data, state=self.ctx)
        guild._add_role(role)
        self.dispatch('guild_role_create', role)

    def parse_guild_role_delete(self, data):
        guild = self._get_guild(int(data['guild_id']))
        if guild is not None:
            role_id = int(data['role_id'])
            role = utils.find(lambda r: r.id == role_id, guild.roles)
            try:
                guild._remove_role(role)
            except ValueError:
                return
            else:
                self.dispatch('guild_role_delete', role)

    def parse_guild_role_update(self, data):
        guild = self._get_guild(int(data['guild_id']))
        if guild is not None:
            role_data = data['role']
            role_id = int(role_data['id'])
            role = utils.find(lambda r: r.id == role_id, guild.roles)
            if role is not None:
                old_role = copy.copy(role)
                role._update(role_data)
                self.dispatch('guild_role_update', old_role, role)

    def parse_guild_members_chunk(self, data):
        guild = self._get_guild(int(data['guild_id']))
        members = data.get('members', [])
        for member in members:
            m = self._make_member(guild, member)
            existing = guild.get_member(m.id)
            if existing is None or existing.joined_at is None:
                guild._add_member(m)

        log.info('processed a chunk for {} members.'.format(len(members)))
        self.process_listeners(ListenerType.chunk, guild, len(members))

    def parse_voice_state_update(self, data):
        guild = self._get_guild(utils._get_as_snowflake(data, 'guild_id'))
        channel_id = utils._get_as_snowflake(data, 'channel_id')
        if guild is not None:
            if int(data['user_id']) == self.user.id:
                voice = self._get_voice_client(guild.id)
                if voice is not None:
                    voice.channel = guild.get_channel(channel_id)

            member, before, after = guild._update_voice_state(data, channel_id)
            if after is not None:
                self.dispatch('voice_state_update', member, before, after)
        else:
            # in here we're either at private or group calls
            call = self._calls.get(channel_id)
            if call is not None:
                call._update_voice_state(data)

    def parse_typing_start(self, data):
        channel = self.get_channel(int(data['channel_id']))
        if channel is not None:
            member = None
            user_id = utils._get_as_snowflake(data, 'user_id')
            if isinstance(channel, DMChannel):
                member = channel.recipient
            elif isinstance(channel, TextChannel):
                member = channel.guild.get_member(user_id)
            elif isinstance(channel, GroupChannel):
                member = utils.find(lambda x: x.id == user_id, channel.recipients)

            if member is not None:
                timestamp = datetime.datetime.utcfromtimestamp(data.get('timestamp'))
                self.dispatch('typing', channel, member, timestamp)

    def parse_call_create(self, data):
        message = self._get_message(int(data['message_id']))
        if message is not None:
            call = GroupCall(call=message, **data)
            self._calls[int(data['channel_id'])] = call
            self.dispatch('call', call)

    def parse_call_update(self, data):
        call = self._calls.get(int(data['channel_id']))
        if call is not None:
            before = copy.copy(call)
            call._update(**data)
            self.dispatch('call_update', before, call)

    def parse_call_delete(self, data):
        call = self._calls.pop(int(data['channel_id']), None)
        if call is not None:
            self.dispatch('call_remove', call)

    def _get_member(self, channel, id):
        if channel.is_private:
            return utils.get(channel.recipients, id=id)
        else:
            return channel.server.get_member(id)

    def _create_message(self, **message):
        """Helper mostly for injecting reactions."""
        reactions = [
            self._create_reaction(**r) for r in message.pop('reactions', [])
        ]
        return Message(channel=message.pop('channel'),
                       reactions=reactions, **message)

    def _create_reaction(self, **reaction):
        emoji = self._get_reaction_emoji(**reaction.pop('emoji'))
        return Reaction(emoji=emoji, **reaction)

    def _get_reaction_emoji(self, **data):
        id = data['id']

        if not id:
            return data['name']

        for server in self.servers:
            for emoji in server.emojis:
                if emoji.id == id:
                    return emoji
        return Emoji(server=None, **data)

    def get_channel(self, id):
        if id is None:
            return None

        for guild in self.guilds:
            channel = guild.get_channel(id)
            if channel is not None:
                return channel

        pm = self._get_private_channel(id)
        if pm is not None:
            return pm

    def receive_chunk(self, guild_id):
        future = asyncio.Future(loop=self.loop)
        listener = Listener(ListenerType.chunk, future, lambda s: s.id == guild_id)
        self._listeners.append(listener)
        return future
