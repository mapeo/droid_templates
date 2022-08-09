import logging

from nio import (
    AsyncClient,
    InviteMemberEvent,
    JoinError,
    MatrixRoom,
    MegolmEvent,
    RoomGetEventError,
    RoomMessageText,
    UnknownEvent,
    RoomMemberEvent,
    JoinedRoomsError,
    JoinedMembersError,
)

from droid.bot_commands import Command
from droid.chat_functions import make_pill, react_to_event, send_text_to_room
from droid.config import Config
from droid.message_responses import Message
from droid.storage import Storage

logger = logging.getLogger(__name__)


class Callbacks:
    def __init__(self, client: AsyncClient, store: Storage, config: Config):
        """
        Args:
            client: nio client used to interact with matrix.

            store: Bot storage.

            config: Bot configuration parameters.
        """
        self.client = client
        self.store = store
        self.config = config
        self.command_prefix = config.command_prefix
        self.err_count=0

    async def message(self, room: MatrixRoom, event: RoomMessageText) -> None:
        """Callback for when a message event is received

        Args:
            room: The room the event came from.

            event: The event defining the message.
        """
        # Extract the message text
        msg = event.body

        # Ignore messages from ourselves
        if event.sender == self.client.user:
            return
        logger.debug(
            f"Bot message received for room {room.display_name} | "
            f"{room.user_name(event.sender)}: {msg}"
        )
        logger.debug(event.__dict__)

        # Process as message if in a public room without command prefix
        has_command_prefix = msg.startswith(self.command_prefix)

        # room.is_group is often a DM, but not always.
        # room.is_group does not allow room aliases
        # room.member_count > 2 ... we assume a public room
        # room.member_count <= 2 ... we assume a DM
        if not has_command_prefix and room.member_count > 2:
            # General message listener
            message = Message(self.client, self.store, self.config, msg, room, event)
            await message.process()
            return

        # Otherwise if this is in a 1-1 with the bot or features a command prefix,
        # treat it as a command
        if has_command_prefix:
            # Remove the command prefix
            msg = msg[len(self.command_prefix) :]

        command = Command(self.client, self.store, self.config, msg, room, event)
        await command.process()

    async def invite(self, room: MatrixRoom, event: InviteMemberEvent) -> None:
        """Callback for when an invite is received. Join the room specified in the invite.

        Args:
            room: The room that we are invited to.

            event: The invite event.
        """
        logger.debug(f"Got invite to {room.room_id} from {event.sender}.")

        # Attempt to join 3 times before giving up
        for attempt in range(3):
            result = await self.client.join(room.room_id)
            if type(result) == JoinError:
                logger.error(
                    f"Error joining room {room.room_id} (attempt %d): %s",
                    attempt,
                    result.message,
                )
            else:
                break
        else:
            logger.error("Unable to join room: %s", room.room_id)

        # Successfully joined room
        logger.info(f"Joined {room.room_id}")

    async def invite_event_filtered_callback(
        self, room: MatrixRoom, event: InviteMemberEvent
    ) -> None:
        """
        Since the InviteMemberEvent is fired for every m.room.member state received
        in a sync response's `rooms.invite` section, we will receive some that are
        not actually our own invite event (such as the inviter's membership).
        This makes sure we only call `callbacks.invite` with our own invite events.
        """
        if event.state_key == self.client.user_id:
            # This is our own membership (invite) event
            await self.invite(room, event)

    async def _reaction(
        self, room: MatrixRoom, event: UnknownEvent, reacted_to_id: str
    ) -> None:
        """A reaction was sent to one of our messages. Let's send a reply acknowledging it.

        Args:
            room: The room the reaction was sent in.

            event: The reaction event.

            reacted_to_id: The event ID that the reaction points to.
        """
        logger.debug(f"Got reaction to {room.room_id} from {event.sender}.")

        # Get the original event that was reacted to
        event_response = await self.client.room_get_event(room.room_id, reacted_to_id)
        if isinstance(event_response, RoomGetEventError):
            logger.warning(
                "Error getting event that was reacted to (%s)", reacted_to_id
            )
            return
        reacted_to_event = event_response.event

        # Only acknowledge reactions to events that we sent
        if reacted_to_event.sender != self.config.user_id:
            return

        # Send a message acknowledging the reaction
        reaction_sender_pill = make_pill(event.sender)
        reaction_content = (
            event.source.get("content", {}).get("m.relates_to", {}).get("key")
        )
        message = (
            f"{reaction_sender_pill} reacted to this event with `{reaction_content}`!"
        )
        await send_text_to_room(
            self.client,
            room.room_id,
            message,
            reply_to_event_id=reacted_to_id,
        )

    async def decryption_failure(self, room: MatrixRoom, event: MegolmEvent) -> None:
        """Callback for when an event fails to decrypt. Inform the user.

        Args:
            room: The room that the event that we were unable to decrypt is in.

            event: The encrypted event that we were unable to decrypt.
        """
        logger.error(
            f"Failed to decrypt event '{event.event_id}' in room '{room.room_id}'!"
            f"\n\n"
            f"Tip: try using a different device ID in your config file and restart."
            f"\n\n"
            f"If all else fails, delete your store directory and let the bot recreate "
            f"it (your reminders will NOT be deleted, but the bot may respond to existing "
            f"commands a second time)."
        )

        red_x_and_lock_emoji = "❌ 🔐"

        # React to the undecryptable event with some emoji
        await react_to_event(
            self.client,
            room.room_id,
            event.event_id,
            red_x_and_lock_emoji,
        )

    async def unknown(self, room: MatrixRoom, event: UnknownEvent) -> None:
        """Callback for when an event with a type that is unknown to matrix-nio is received.
        Currently this is used for reaction events, which are not yet part of a released
        matrix spec (and are thus unknown to nio).

        Args:
            room: The room the reaction was sent in.

            event: The event itself.
        """
        if event.type == "m.reaction":
            # Get the ID of the event this was a reaction to
            relation_dict = event.source.get("content", {}).get("m.relates_to", {})

            reacted_to = relation_dict.get("event_id")
            if reacted_to and relation_dict.get("rel_type") == "m.annotation":
                await self._reaction(room, event, reacted_to)
                return

        logger.debug(
            f"Got unknown event with type to {event.type} from {event.sender} in {room.room_id}."
        )


    async def find_dm_rooms_or_invite(self,
        users: list
    ) -> list:
        """Find the rooms to send to.

        Users can be specified with --user for send and listen operations.
        These rooms we label DM (direct messaging) rooms.
        By that we means rooms that only have 2 members, and these two
        members being the sender and the recipient in question.
        We do not care about 'is_group' or 'is_direct' flags (hints).

        If given a user and known the sender, we try to find a matching room.
        There might be 0, 1, or more matching rooms. If 0, then giver error
        and the user should run --room-invite first. if 1 found, use it.
        If more than 1 found, just use 1 of them arbitrarily.

        The steps are:
        - get all rooms where sender is member
        - get all members to these rooms
        - check if there is a room with just 2 members and them
        being sender and recipient (user from users arg)

        In order to match a user to a RoomMember we allow 3 choices:
        - user_id: perfect match, is unique, full user id, e.g. "@user:example.org"
        - user_id without homeserver domain: partial user id, e.g. "@user"
        this partial user will be completed by adding the homeserver of the
        sender to the end, i.e. assuming that sender and receiver are on the
        same homeserver.
        - display name: be careful, display names are NOT unique, you could be
        mistaken and by error send to the wrong person.
        '--joined-members "*"' shows you the display names in the middle column

        Arguments:
        ---------
            users: list(str): list of user_ids
                try to find a matching DM room for each user
            client: AsyncClient: client, allows as to query the server
            credentials: dict: allows to get the user_id of sender

        Returns a list of found DM rooms. List may be empty if no matches were
        found.

        """
        rooms = []
        if not users:
            # logger.debug(f"Room(s) from --user: {rooms}, no users were specified.")
            return rooms
        # sender = credentials["user_id"]  # who am i
        sender = self.config.user_id  # who am i
        logger.debug(f"Trying to get members for all rooms of sender: {sender}")
        # resp = await client.joined_rooms()
        resp = await self.client.joined_rooms()
        if isinstance(resp, JoinedRoomsError):
            logger.error(
                f"joined_rooms failed with {resp}. Not able to "
                "get all rooms. "
                f"Not able to find DM rooms for sender {sender}. "
                f"Not able to send to receivers {users}."
            )
            self.err_count += 1
            senderrooms = []
        else:
            # logger.debug(f"joined_rooms successful with {resp}")
            senderrooms = resp.rooms
        room_found_for_users = []
        for room in senderrooms:
            resp = await self.client.joined_members(room)
            if isinstance(resp, JoinedMembersError):
                logger.error(
                    f"joined_members failed with {resp}. Not able to "
                    f"get room members for room {room}. "
                    f"Not able to find DM rooms for sender {sender}. "
                    f"Not able to send to some of these receivers {users}."
                )
                self.err_count += 1
            else:
                # resp.room_id
                # resp.members = List[RoomMember] ; RoomMember
                # member.user_id
                # member.display_name
                # member.avatar_url
                # logger.debug(f"joined_members successful with {resp}")
                if resp.members and len(resp.members) == 2:
                    if resp.members[0].user_id == sender:
                        # sndr = resp.members[0]
                        rcvr = resp.members[1]
                    elif resp.members[1].user_id == sender:
                        # sndr = resp.members[1]
                        rcvr = resp.members[0]
                    else:
                        # sndr = None
                        rcvr = None
                        logger.error(f"Sender does not match {resp}")
                        self.err_count += 1
                    for user in users:
                        if (
                            rcvr
                            and user == rcvr.user_id
                            or user == rcvr.display_name
                        ):
                            room_found_for_users.append(user)
                            rooms.append(resp.room_id)
        for user in users:
            if user not in room_found_for_users:
                logger.error(
                    "Room(s) were specified for a DM (direct messaging) "
                    "send operation via --room. But no DM room was "
                    f"found for user '{user}'. "
                    "Try setting up a room first via --room-create and "
                    "--room-invite option."
                )
                self.err_count += 1
        rooms = list(dict.fromkeys(rooms))  # remove duplicates in list
        logger.debug(f"Room(s) from --user: {rooms}")
        if rooms:
            return rooms
        else:
            # create and invite
            logger.debug('TODO: Create DM room and invite user')


    async def room_member_event(self, room: MatrixRoom, event: RoomMemberEvent) -> None:
        """Callback for when an event with a type that is unknown to matrix-nio is received.
        Currently this is used for reaction events, which are not yet part of a released
        matrix spec (and are thus unknown to nio).

        Args:
            room: The room the reaction was sent in.

            event: The event itself.
        """
        event_dict = event.source
        if 'type' in event_dict:
            if event_dict["type"] == "m.room.message":
                logger.debug("RoomMessage.parse_event(event_dict)")
                # return 
            elif event_dict["type"] == "m.room.create":
                logger.debug("RoomCreateEvent.from_dict(event_dict)")
                # return 
            elif event_dict["type"] == "m.room.guest_access":
                logger.debug("RoomGuestAccessEvent.from_dict(event_dict)")
                # return 
            elif event_dict["type"] == "m.room.join_rules":
                logger.debug("RoomJoinRulesEvent.from_dict(event_dict)")
                # return 
            elif event_dict["type"] == "m.room.history_visibility":
                logger.debug("RoomHistoryVisibilityEvent.from_dict(event_dict)")
                # return 
            elif event_dict["type"] == "m.room.member":
                logger.debug("RoomMemberEvent.from_dict(event_dict)")
                member_event = RoomMemberEvent.from_dict(event_dict)
                if member_event.membership == 'leave':
                    msg = 'Why leaving so soom?'
                if member_event.membership == 'join':
                    msg = 'Be welcome to our channel',
                # Member exiting room event
                try:
                    sender = member_event.sender
                    room_ids = await self.find_dm_rooms_or_invite([sender])
                    if room_ids:
                        await send_text_to_room(
                            self.client,
                            room_ids[-1],
                            msg,
                            markdown_convert=False
                        )
                except Exception as e:
                    logger.error(e,exc_info=True)
                    pass
                # return 
            elif event_dict["type"] == "m.room.canonical_alias":
                logger.debug("RoomAliasEvent.from_dict(event_dict)")
                # return 
            elif event_dict["type"] == "m.room.name":
                logger.debug("RoomNameEvent.from_dict(event_dict)")
                # return 
            elif event_dict["type"] == "m.room.topic":
                logger.debug("RoomTopicEvent.from_dict(event_dict)")
                # return 
            elif event_dict["type"] == "m.room.avatar":
                logger.debug("RoomAvatarEvent.from_dict(event_dict)")
                # return 
            elif event_dict["type"] == "m.room.power_levels":
                logger.debug("PowerLevelsEvent.from_dict(event_dict)")
                # return 
            elif event_dict["type"] == "m.room.encryption":
                logger.debug("RoomEncryptionEvent.from_dict(event_dict)")
                # return 
            elif event_dict["type"] == "m.room.redaction":
                logger.debug("RedactionEvent.from_dict(event_dict)")
                # return 
            elif event_dict["type"] == "m.room.tombstone":
                logger.debug("RoomUpgradeEvent.from_dict(event_dict)")
                # return 
            elif event_dict["type"] == "m.room.encrypted":
                logger.debug("Event.parse_encrypted_event(event_dict)")
                # return 
            elif event_dict["type"] == "m.sticker":
                logger.debug("StickerEvent.from_dict(event_dict)")
                # return 
            elif event_dict["type"].startswith("m.call"):
                logger.debug("CallEvent.parse_event(event_dict)")
                # return 
        logger.debug(
            f"Got RoomMemberEvent event with type to:\n {event.__dict__}"
        )