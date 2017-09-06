import logging
import json
import asyncio
from functools import lru_cache
from errbot.backends.base import (
	Message, Presence, ONLINE, AWAY, Person, UserDoesNotExistError,
	Room, RoomError, RoomDoesNotExistError, RoomOccupant,
)
from errbot.core import ErrBot
from errbot.rendering import md
from errbot.utils import split_string_after
from mattermostdriver import Driver
from mattermostdriver.exceptions import (
	InvalidOrMissingParameters, NoAccessTokenProvided, NotEnoughPermissions, FeatureDisabled,
	ContentTooLarge, ResourceNotFound
)

log = logging.getLogger('errbot.backends.mattermost')

# Mattermost message limit is 4000 chars, need to leave some space for
# backticks when messages are split
MATTERMOST_MESSAGE_LIMIT = 3994

# Default websocket timeout - this is needed to send a heartbeat
# to keep the connection alive
DEFAULT_TIMEOUT = 30


class MattermostPerson(Person):
	"""
	A Person in Mattermost
	"""
	def __init__(self, driver, userid=None, channelid=None, teamid=None):
		self._userid = userid
		self._channelid = channelid
		self._teamid = teamid
		self._driver = driver

	@property
	def userid(self) -> str:
		return self._userid

	@property
	def username(self) -> str:
		user = self._driver.api['users'].get_user(user_id=self.userid)
		if 'username' not in user:
			log.error("Can't find username for user with ID {}".format(self._userid))
			return "<{}>".format(self._userid)
		return user['username']

	@property
	def teamid(self) -> str:
		return self._teamid

	@property
	def channelid(self) -> str:
		return self._channelid

	@property
	def nick(self) -> str:
		return self.username

	@property
	def client(self) -> str:
		return self._channelid

	@property
	def domain(self) -> str:
		return self._driver.client.url

	@property
	def fullname(self):
		user = self._driver.api['users'].get_user(user_id=self.userid)
		if 'first_name' not in user and 'last_name' not in user:
			log.error("No first or last name for user with ID {}".format(self._userid))
			return "<{}>".format(self._userid)
		return "{} {}".format(user['first_name'], user['last_name'])

	@property
	def person(self):
		return "@{}".format(self.username)

	@property
	def person(self):
		return "@{}".format(self.username)

	@property
	def aclattr(self):
		return "@{}".format(self.username)

	def __unicode__(self):
		return "@{}".format(self.username)

	def __str__(self):
		return self.__unicode__()

	def __eq__(self, other):
		if not isinstance(other, MattermostPerson):
			log.warning('Tried to compare a MattermostPerson with a %s', type(other))
			return False
		return other.userid == self.userid


class MattermostRoomOccupant(RoomOccupant, MattermostPerson):
	"""
	A Person inside a Team (Room)
	"""
	def __init__(self, client, teamid, userid, channelid, bot):
		super().__init__(client, userid, channelid)
		self._teamid = teamid
		self._room = MattermostRoom(channelid=channelid, teamid=teamid, bot=bot)

	@property
	def room(self):
		return self._room

	def __unicode__(self):
		return "~{}/{}".format(self._room.name, self.username)

	def __str__(self):
		return self.__unicode__()

	def __eq__(self, other):
		if not isinstance(other, RoomOccupant):
			log.warning('tried to compare a MattermostRoomOccupant with a MattermostPerson \
					{} vs {}'.format(self, other))
			return False
		return other.room.id == self.room.id and other.userid == self.userid


class MattermostBackend(ErrBot):
	def __init__(self, config):
		super().__init__(config)
		identity = config.BOT_IDENTITY
		self._login = identity.get('login')
		self._password = identity.get('password')
		self.team = identity.get('team')
		self._scheme = identity.get('scheme', 'https')
		self._port = identity.get('port', 8065)
		self.url = identity.get('server').rstrip('/')
		self.insecure = identity.get('insecure', False)
		self.timeout = identity.get('timeout', DEFAULT_TIMEOUT)
		self.teamid = ''
		self.token = ''
		self.bot_identifier = None
		self.driver = None
		self.md = md()

	@property
	def userid(self):
		return "{}".format(self.bot_identifier.userid)

	@property
	def mode(self):
		return 'mattermost'

	def username_to_userid(self, name):
		"""Converts a name prefixed with @ to the userid"""
		name = name.lstrip('@')
		user = self.driver.api['users'].get_user_by_username(username=name)
		if user is None:
			raise UserDoesNotExistError("Cannot find user {}".format(name))
		return user['id']

	@asyncio.coroutine
	def mattermost_event_handler(self, payload):
		if not payload:
			return

		payload = json.loads(payload)
		if 'event' not in payload:
			log.debug("Message contains no event: {}".format(payload))
			return

		event_handlers = {
			'posted': self._message_event_handler,
			'status_change': self._status_change_event_handler,
			'hello': self._hello_event_handler,
			'user_added': self._room_joined_event_handler,
			'user_removed': self._room_left_event_handler,
		}

		event = payload['event']
		event_handler = event_handlers.get(event)

		if event_handler is None:
			log.debug("No event handler available for {}, ignoring.".format(event))
			return
		# noinspection PyBroadException
		try:
			event_handler(payload)
		except Exception:
			log.exception("{} event handler raised an exception".format(event))

	def _room_joined_event_handler(self, message):
		log.debug('User added to channel')
		if message['data']['user_id'] == self.userid:
			self.callback_room_joined(self)

	def _room_left_event_handler(self, message):
		log.debug('User removed from channel')
		if message['broadcast']['user_id'] == self.userid:
			self.callback_room_left(self)

	def _message_event_handler(self, message):
		log.debug(message)
		data = message['data']

		# In some cases (direct messages) team_id is an empty string
		if data['team_id'] != '' and self.teamid != data['team_id']:
			log.info("Message came from another team ({}), ignoring...".format(data['team_id']))
			return

		broadcast = message['broadcast']

		if 'channel_id' in data:
			channelid = data['channel_id']
		elif 'channel_id' in broadcast:
			channelid = broadcast['channel_id']
		else:
			log.error("Couldn't find a channelid for event {}".format(message))
			return

		channel_type = data['channel_type']

		if channel_type != 'D':
			channel = data['channel_name']
		else:
			channel = channelid

		text = ''
		post_id = ''
		file_ids = None
		userid = None

		if 'post' in data:
			post = json.loads(data['post'])
			text = post['message']
			userid = post['user_id']
			if 'file_ids' in post:
				file_ids = post['file_ids']
			post_id = post['id']
			if 'type' in post and post['type'] == 'system_add_remove':
				log.info("Ignoring message from System")
				return

		if 'user_id' in data:
			userid = data['user_id']

		if not userid:
			log.error('No userid in event {}'.format(message))
			return

		mentions = []
		if 'mentions' in data:
			# TODO: Only user, not channel mentions are in here at the moment
			mentions = self.mentions_build_identifier(json.loads(data['mentions']))

		msg = Message(
			text,
			extras={
				'mattermost_event': message,
				'url': '{scheme:s}://{domain:s}:{port:s}/{teamname:s}/pl/{postid:s}'.format(
					scheme=self.driver.options['scheme'],
					domain=self.driver.options['url'],
					port=str(self.driver.options['port']),
					teamname=self.team,
					postid=post_id
				)
			}
		)
		if file_ids:
			msg.extras['attachments'] = file_ids

		# TODO: Slack handles bots here, but I am not sure if bot users is a concept in mattermost
		if channel_type == 'D':
			msg.frm = MattermostPerson(self.driver, userid=userid, channelid=channelid, teamid=self.teamid)
			msg.to = MattermostPerson(
				self.driver, userid=self.bot_identifier.userid, channelid=channelid, teamid=self.teamid)
		elif channel_type == 'O' or channel_type == 'P':
			msg.frm = MattermostRoomOccupant(self.driver, userid=userid, channelid=channelid, teamid=self.teamid, bot=self)
			msg.to = MattermostRoom(channel, teamid=self.teamid, bot=self)
		else:
			log.warning('Unknown channel type \'{}\'! Unable to handle {}.'.format(
				channel_type,
				channel
			))
			return

		self.callback_message(msg)

		if mentions:
			self.callback_mention(msg, mentions)

	def _status_change_event_handler(self, message):
		"""Event handler for the 'presence_change' event"""
		idd = MattermostPerson(self.driver, message['data']['user_id'])
		status = message['data']['status']
		if status == 'online':
			status = ONLINE
		elif status == 'away':
			status = AWAY
		else:
			log.error(
				"It appears the Mattermost API changed, I received an unknown status type %s" % status
			)
			status = ONLINE
		self.callback_presence(Presence(identifier=idd, status=status))

	def _hello_event_handler(self, message):
		"""Event handler for the 'hello' event"""
		self.connect_callback()
		self.callback_presence(Presence(identifier=self.bot_identifier, status=ONLINE))

	@lru_cache(1024)
	def get_direct_channel(self, userid, other_user_id):
		"""
		Get the direct channel to another user.
		If it does not exist, it will be created.
		"""
		try:
			return self.driver.api['channels'].create_direct_message_channel(options=[userid, other_user_id])
		except (InvalidOrMissingParameters, NotEnoughPermissions):
			raise RoomDoesNotExistError("Could not find Direct Channel for users with ID {} and {}".format(
				userid, other_user_id
			))

	def build_identifier(self, txtrep):
		"""
		Convert a textual representation into a :class:`~MattermostPerson` or :class:`~MattermostRoom`

		Supports strings with the following formats::

			@username
			~channelname
			channelid
		"""
		txtrep = txtrep.strip()
		if txtrep.startswith('~'):
			# Channel
			channelid = self.channelname_to_channelid(txtrep[1:])
			if channelid is not None:
				return MattermostRoom(channelid=channelid, teamid=self.teamid, bot=self)
		else:
			# Assuming either a channelid or a username
			if txtrep.startswith('@'):
				# Username
				userid = self.username_to_userid(txtrep[1:])
			else:
				# Channelid
				userid = txtrep

			if userid is not None:
				return MattermostPerson(
					self.driver,
					userid=userid,
					channelid=self.get_direct_channel(self.userid, userid)['id'],
					teamid=self.teamid
				)
		raise Exception(
			'Invalid or unsupported Mattermost identifier: %s' % txtrep
		)

	def mentions_build_identifier(self, mentions):
		identifier = []
		for mention in mentions:
			if mention != self.bot_identifier.userid:
				identifier.append(
					self.build_identifier(mention)
				)
		return identifier

	def serve_once(self):
		self.driver = Driver({
			'scheme': self._scheme,
			'url': self.url,
			'port': self._port,
			'verify': not self.insecure,
			'timeout': self.timeout,
			'login_id': self._login,
			'password': self._password
		})
		self.driver.login()

		self.teamid = self.driver.api['teams'].get_team_by_name(name=self.team)['id']
		userid = self.driver.api['users'].get_user(user_id='me')['id']

		self.token = self.driver.client.token

		self.bot_identifier = MattermostPerson(self.driver, userid=userid, teamid=self.teamid)

		# noinspection PyBroadException
		try:
			loop = self.driver.init_websocket(event_handler=self.mattermost_event_handler)
			self.reset_reconnection_count()
			loop.run_forever()
		except KeyboardInterrupt:
			log.info("Interrupt received, shutting down..")
			return True
		except Exception:
			log.exception("Error reading from RTM stream:")
		finally:
			log.debug("Triggering disconnect callback")
			self.disconnect_callback()

	def send_message(self, message):
		super().send_message(message)
		to_name = "<unknown>"
		try:
			if message.is_group:
				to_channel_id = message.to.id
				if message.to.name:
					to_name = message.to.name
				else:
					self.channelid_to_channelname(channelid=to_channel_id)
			else:
				to_name = message.to.username

				if isinstance(message.to, RoomOccupant):  # private to a room occupant -> this is a divert to private !
					log.debug("This is a divert to private message, sending it directly to the user.")
					channel = self.get_direct_channel(self.userid, self.username_to_userid(to_name))
					to_channel_id = channel['id']
				else:
					to_channel_id = message.to.channelid

			message_type = "direct" if message.is_direct else "channel"
			log.debug('Sending %s message to %s (%s)' % (message_type, to_name, to_channel_id))

			body = self.md.convert(message.body)
			log.debug('Message size: %d' % len(body))

			limit = min(self.bot_config.MESSAGE_SIZE_LIMIT, MATTERMOST_MESSAGE_LIMIT)
			parts = self.prepare_message_body(body, limit)

			for part in parts:
				self.driver.api['posts'].create_post(options={
					'channel_id': to_channel_id,
					'message': part,
				})
		except (InvalidOrMissingParameters, NotEnoughPermissions):
			log.exception(
				"An exception occurred while trying to send the following message "
				"to %s: %s" % (to_name, message.body)
			)

	def prepare_message_body(self, body, size_limit):
		"""
		Returns the parts of a message chunked and ready for sending.
		This is a staticmethod for easier testing.
		Args:
			body (str)
			size_limit (int): chunk the body into sizes capped at this maximum
		Returns:
			[str]
		"""
		fixed_format = body.startswith('```')  # hack to fix the formatting
		parts = list(split_string_after(body, size_limit))

		if len(parts) == 1:
			# If we've got an open fixed block, close it out
			if parts[0].count('```') % 2 != 0:
				parts[0] += '\n```\n'
		else:
			for i, part in enumerate(parts):
				starts_with_code = part.startswith('```')

				# If we're continuing a fixed block from the last part
				if fixed_format and not starts_with_code:
					parts[i] = '```\n' + part

				# If we've got an open fixed block, close it out
				if parts[i].count('```') % 2 != 0:
					parts[i] += '\n```\n'

		return parts

	def change_presence(self, status: str=ONLINE, message: str=''):
		pass  # Mattermost does not have a request/websocket event to change the presence

	def is_from_self(self, message: Message):
		return self.bot_identifier.userid == message.frm.userid

	def shutdown(self):
		self.driver.logout()
		super().shutdown()

	def query_room(self, room):
		""" Room can either be a name or a channelid """
		return MattermostRoom(room, teamid=self.teamid, bot=self)

	def prefix_groupchat_reply(self, message: Message, identifier):
		super().prefix_groupchat_reply(message, identifier)
		message.body = '@{0}: {1}'.format(identifier.nick, message.body)

	def build_reply(self, message, text=None, private=False, threaded=False):
		response = self.build_message(text)
		response.frm = self.bot_identifier
		if private:
			response.to = message.frm
		else:
			response.to = message.frm.room if isinstance(message.frm, RoomOccupant) else message.frm
		return response

	def get_public_channels(self):
		channels = []
		page = 0
		channel_page_limit = 200
		while True:
			channel_list = self.driver.api['channels'].get_public_channels(
				team_id=self.teamid,
				params={'page': page, 'per_page': channel_page_limit}
			)
			if len(channel_list) == 0:
				break
			else:
				channels.extend(channel_list)
			page += 1
		return channels

	def channels(self, joined_only=False):
		channels = []
		channels.extend(self.driver.api['channels'].get_channels_for_user(user_id=self.userid, team_id=self.teamid))
		if not joined_only:
			public_channels = self.get_public_channels()
			for channel in public_channels:
				if channel not in channels:
					channels.append(channel)
		return channels

	def rooms(self):
		"""Return public and private channels, but no direct channels"""
		rooms = self.channels(joined_only=True)
		channels = [channel for channel in rooms if channel['type'] != 'D']
		return [MattermostRoom(channelid=channel['id'], teamid=channel['team_id'], bot=self) for channel in channels]

	def channelid_to_channelname(self, channelid):
		"""Convert the channelid in the current team to the channel name"""
		channel = self.driver.api['channels'].get_channel(channel_id=channelid)
		if 'name' not in channel:
			raise RoomDoesNotExistError("No channel with ID {} exists in team with ID {}".format(
				id, self.teamid
			))
		return channel['name']

	def channelname_to_channelid(self, name):
		"""Convert the channelname in the current team to the channel id"""
		channel = self.driver.api['channels'].get_channel_by_name(team_id=self.teamid, channel_name=name)
		if 'id' not in channel:
			raise RoomDoesNotExistError("No channel with name {} exists in team with ID {}".format(
				name, self.teamid
			))
		return channel['id']

	def __hash__(self):
		return 0  # This is a singleton anyway


class MattermostRoom(Room):
	def __init__(self, name=None, channelid=None, teamid=None, bot=None):
		if channelid is not None and name is not None:
			raise ValueError("channelid and name are mutually exclusive")
		if teamid is None:
			raise ValueError("teamid is not optional")

		if name is not None:
			if name.startswith('~'):
				self._name = name[1:]
			else:
				self._name = name
		else:
			self._name = bot.channelid_to_channelname(channelid)

		self._teamid = teamid
		self._id = None if channelid is None else channelid
		self._bot = bot
		self.driver = bot.driver

	@property
	def teamid(self):
		return self._teamid

	@property
	def name(self):
		return self._name

	@property
	def id(self):
		if self._id is None:
			self._id = self._channel['id']
		return self._id

	channelid = id

	@property
	def _channel(self):
		channel = self.driver.api['channels'].get_channel_by_name(team_id=self.teamid, channel_name=self.name)
		if 'status_code' in channel and channel['status_code'] != 200:
			raise RoomDoesNotExistError("{}: {}".format(channel['status_code'], channel['message']))
		return channel

	@property
	def _channel_info(self):
		return NotImplementedError("TODO")

	@property
	def private(self):
		return self._channel.type == 'P'

	@property
	def exists(self):
		channels = []
		channels.extend(self.driver.api['channels'].get_channels_for_user(user_id='me', team_id=self.teamid))
		public_channels = self._bot.get_public_channels()
		for channel in public_channels:
			if channel not in channels:
				channels.append(channel)
		return len([c for c in channels if c['name'] == self.name]) > 0

	@property
	def joined(self):
		channels = self.driver.api['channels'].get_channels_for_user(user_id='me', team_id=self.teamid)
		return len([c for c in channels if c['name'] == self.name]) > 0

	@property
	def topic(self):
		if self._channel['header'] == '':
			return None
		else:
			return self._channel['header']

	@topic.setter
	def topic(self, topic):
		self.driver.api['channels'].update_channel(
			channel_id=self.id,
			options={'header': topic, 'id': self.id}
		)

	@property
	def purpose(self):
		if self._channel['purpose'] == '':
			return None
		else:
			return self._channel['purpose']

	@purpose.setter
	def purpose(self, purpose):
		self.driver.api['channels'].update_channel(
			channel_id=self.id,
			options={'purpose': purpose, 'id': self.id}
		)

	@property
	def occupants(self):
		member_count = self.driver.api['channels'].get_channel_statistics(channel_id=self.id)['member_count']
		members = {}
		user_page_limit = 200
		for start in range(0, member_count, user_page_limit):
			members.update(
				self.driver.api['channels'].get_channel_members(
					channel_id=self.id,
					params={'page': start, 'per_page': user_page_limit}
				)
			)
		return [MattermostRoomOccupant(self.driver, userid=m, teamid=self.teamid, channelid=self.id, bot=self._bot) for m in members]

	def create(self, private=False):
		channel_type = 'O'
		if private:
			log.info("Creating private group {}".format(str(self)))
			channel_type = 'P'
		else:
			log.info("Creating public channel {}".format(str(self)))
		try:
			self.driver.api['channels'].create_channel(options={
				'team_id': self.teamid,
				'name': self.name,
				'display_name': self.name,
				'type': channel_type
			})
			self.driver.api['channels'].get_channel_by_name(team_id=self.teamid, channel_name=self.name)
			self._bot.callback_room_joined(self)
		except (NotEnoughPermissions, ResourceNotFound) as e:
			raise RoomError(e)

	def join(self, username: str=None, password: str=None):
		if not self.exists:
			log.info("Channel {} doesn't seem exist, trying to create it.".format(str(self)))
			self.create()  # This always creates a public room!
		log.info("Joining channel {}".format(str(self)))
		try:
			self.driver.api['channels'].add_user(
				channel_id=self._id,
				options={
					'user_id': self._bot.userid
				}
			)
			self._bot.callback_room_joined(self)
		except (InvalidOrMissingParameters, NotEnoughPermissions) as e:
			raise RoomError(e)

	def leave(self, reason: str=None):
		log.info('Leaving channel {} ({})'.format(str(self), self.id))
		try:
			self.driver.api['channels'].remove_user_from_channel(channel_id=self.id, user_id=self._bot.id)
			self._bot.callback_room_left(self)
		except (InvalidOrMissingParameters, NotEnoughPermissions) as e:
			raise RoomError(e)

	def destroy(self):
		try:
			self.driver.api['channels'].delete_channel(channel_id=self.id)
			self._bot.callback_room_left(self)
		except (InvalidOrMissingParameters, NotEnoughPermissions) as e:
			log.debug('Could not delete the channel. Are you a member of the channel?')
			raise RoomError(e)
		self._id = None

	def invite(self, *args):
		user_count = self.driver.api['teams'].get_team_stats(team_id=self.teamid)['total_member_count']
		user_page_limit = 200
		users_not_in_channel = []
		for start in range(0, user_count, user_page_limit):
			users_not_in_channel.extend(self.driver.api['users'].get_users(
				params={
					'page': start,
					'per_page': user_page_limit,
					'in_team': self.teamid,
					'not_in_channel': self.id
				}
			))
		users = {}
		for user in users_not_in_channel:
			users.update({users_not_in_channel[user]['username']: users_not_in_channel[user]['id']})
		for user in args:
			if user not in users:
				raise UserDoesNotExistError('User \'{}\' not found'.format(user))
			log.info('Inviting {} into {} ({})'.format(user, str(self), self.id))

			try:
				self.driver.api['channels'].add_user(channel_id=self.id, options={'user_id': users[user]})
			except (InvalidOrMissingParameters, NotEnoughPermissions):
				raise RoomError("Unable to invite {} to channel {} ({})".format(
					user, str(self), self.id
				))

	def __str__(self):
		return "~{}".format(self._name)

	def __eq__(self, other):
		if not isinstance(other, MattermostRoom):
			return False
		return self.id == other.id
