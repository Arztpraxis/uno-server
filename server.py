from json import JSONEncoder
from random import choice, sample
from threading import Thread, Timer, Lock
from types import SimpleNamespace
from os import execv
from sys import argv, executable
from optparse import OptionParser
from shlex import split

# Networking
from wsgiref.simple_server import make_server
from ws4py.server.wsgirefserver import WebSocketWSGIRequestHandler
from highway import ServerWSGIApplication, WSGIServer
from highway import Server, Route

# Utilities built into highway
from highway import logging
from highway.utils import capture_trace

# Configuration utility
from Meh import Config, Option, ExceptionInConfigError

from cards import ALL_CARDS, REGULAR_CARDS
from cards import ROTATE, BLOCK, TAKE_TWO, TAKE_FOUR, PICK_COLOR
from cards import CardEncoder, can_play

from utils import broadcast

taken_names = []
lobbies = {}

CHEAT_PARSER = OptionParser()
CHEAT_PARSER.add_option("-f", "--face", action="store", type="int", 
	dest="face", default=0)
CHEAT_PARSER.add_option("-c", "--color", action="store", type="int", 
	dest="color", default=None)
CHEAT_PARSER.add_option("-a", "--amount", action="store", type="int", 
	dest="amount", default=1)
CHEAT_PARSER.add_option("-p", "--player", action="store", type="string", 
	dest="player", default=None)


def broadcast_to_resting(data, route, json_encoder=None):
	for user in [user for user in server.manager.websockets if not user.lobby]:
		user.send(data, route, json_encoder=json_encoder)


# Meant to be called from to REPL to troll
def give_cards(count, player_name):
	player = find_player(player_name)
	if player != None:
		player.lobby.give_cards(count, player)


def find_player(player_name):
	for lobby in lobbies:
		for player in lobbies[lobby].players:
			if player_name == player.name:
				return player
	return None


class LobbyEncoder(JSONEncoder):
	def default(self, obj):
		if isinstance(obj, Lobby):
			return {
				"host" : obj.host.name,
				"playerCount" : obj.player_count,
				"playing" : obj.playing
				}
		return JSONEncoder.default(self, obj)


class UserEncoder(JSONEncoder):
	def default(self, obj):
		if isinstance(obj, User):
			return obj.name
		return JSONEncoder.default(self, obj)


class Game:
	def __init__(self, lobby, debug=False):
		self.lobby = lobby
		self.debug = debug


	def player_leave(self, player):
		pass


	def stop(self):
		pass


class Uno(Game):
	LEFT  = 1
	RIGHT = 2

	def __init__(self, lobby, turn_time=20.0, debug=False):
		super().__init__(lobby, debug=debug)

		self._draw_card_stack = []
		# First card on the stack is never a special card
		self.card_stack = [choice(REGULAR_CARDS)]
		self.direction = Uno.RIGHT
		self.turn_time = turn_time


		for player in lobby.players:
			player.games.uno = SimpleNamespace()
			player.games.uno.turn_over = True
			player.games.uno.has_drawn_card = False
			player.games.uno.cards = sample(ALL_CARDS, 7)
			player.send(player.games.uno.cards, "uno_give_card",
				json_encoder=CardEncoder)

		self.playing_player = lobby.players[0]
		self.playing_player.games.uno.turn_over = False
		
		# Prevent race conditions if player draws or plays too quickly in
		# succession
		self.play_card_lock = Lock()
		self.draw_card_lock = Lock()

		self.turn_timer = None

		# Send the first card on the stack to all players
		broadcast(self.card_stack[0], "uno_card_stack", lobby.players,
			json_encoder=CardEncoder)
		# Send whos turn it is to all players
		broadcast(self.playing_player, "uno_turn", lobby.players,
			json_encoder=UserEncoder)

		self.reset_turn_timer()


	def reset_turn_timer(self):
		if self.turn_timer != None:
			self.turn_timer.cancel()

		self.turn_timer = Timer(self.turn_time, 
			lambda: self.end_turn(time_expired=True))
		self.turn_timer.start()


	@property
	def draw_card_stack(self):
		# Introduce a bit of randomness (does not respect card frequency)
		while len(self._draw_card_stack) < 30:
			self._draw_card_stack.append(choice(ALL_CARDS))
		return self._draw_card_stack


	def draw_card_from_stack(self):
		# Fetch from property to keep the stack filled
		card = self.draw_card_stack[0]
		# Delete reference from stack list
		del self._draw_card_stack[0]
		return card


	# For random cards
	def give_cards(self, count, player):
		cards = []
		for i in range(count):
			cards.append(self.draw_card_from_stack())
		# Save cards to player deck server-side
		player.games.uno.cards += cards
		# Send client cards
		player.send(cards, "uno_give_card", json_encoder=CardEncoder)


	# For specific cards (cheating mainly)
	def give_card(self, face, color, player):
		for card in ALL_CARDS:
			if card.face == face and card.color == color:
				# Save card to player deck server-side
				player.games.uno.cards.append(card)
				# Send the card to client
				player.send([card], "uno_give_card", 
					json_encoder=CardEncoder)
				return True
		return False


	def change_direction(self):
		if self.direction == Uno.LEFT:
			self.direction = Uno.RIGHT
		elif self.direction == Uno.RIGHT:
			self.direction = Uno.LEFT
		else:
			# Unexpected direction -> Direction is right
			self.direction = Uno.RIGHT
		broadcast(self.direction, "uno_direction", self.lobby.players)

		if self.debug:
			logging.info("Direction changed to '%s'" % 
				("left" if self.direction == Uno.LEFT else "right"))


	def get_next_player(self, player_inc=1):
		players = self.lobby.players
		player_index = players.index(self.playing_player)

		if self.direction == Uno.LEFT:
			next_player_overflowing_index = player_index - player_inc
		elif self.direction == Uno.RIGHT:
			next_player_overflowing_index = player_index + player_inc
		else:
			# Unexpected direction?
			# Repeating turn
			next_player_overflowing_index = player_index

		return players[next_player_overflowing_index % len(players)]


	@property
	def next_player(self):
		return self.get_next_player()


	def end_turn(self, player_inc=1, time_expired=False):
		next_player = self.get_next_player(player_inc)

		if self.debug:
			if time_expired:
				logging.info("Turn time of '%i' expired" % self.turn_time)
			logging.info("Next player: '%s'" % next_player)


		self.playing_player.games.uno.turn_over = True
		self.playing_player.games.uno.has_drawn_card = False
		next_player.games.uno.turn_over = False

		self.playing_player = next_player

		self.reset_turn_timer()

		broadcast(self.playing_player, "uno_turn", self.lobby.players,
			json_encoder=UserEncoder)


	def play_card(self, card_id, player):
		self.play_card_lock.acquire()

		successful = False
		# If it's the turn of the player who wants to play a card
		if player == self.playing_player:
			# Is the card_id valid?
			if card_id in range(len(player.games.uno.cards)):
				# Acquire the card
				card = player.games.uno.cards[card_id]

				if self.debug:
					logging.info("'%s' played: %s" % (player, card))
					logging.info("Cards of '%s': %s" % (player,
						player.games.uno.cards))

				# Does the played card fit on top of the card stack?
				if self.card_stack[-1].can_play(card):
					self.card_stack.append(card)

					# Send the played card to all players
					broadcast(card, "uno_card_stack", self.lobby.players,
						json_encoder=CardEncoder)

					# Change direction
					if card.face == ROTATE:
						# Only two players -> Next turn name player
						if len(self.lobby.players) != 2:
							self.change_direction()
							self.end_turn()
						# Turn goes on if only two players are playing
						# Player can draw another card if needed
						else:
							player.games.uno.has_drawn_card = False		
						

					# Skip player
					elif card.face == BLOCK:
						if len(self.lobby.players) != 2:
							self.end_turn(player_inc=2)
						# Turn goes on if only two players are playing
						# Player can draw another card if needed
						else:
							player.games.uno.has_drawn_card = False

					# Take two cards
					elif card.face == TAKE_TWO:
						self.give_cards(2, self.next_player)
						self.end_turn()


					# Take four cards
					elif card.face == TAKE_FOUR:
						self.give_cards(4, self.next_player)
						# Turn does not end

					# End turn only if the card does not require another card
					elif card.face != PICK_COLOR:
						self.end_turn()

					# Remove the card from the players deck
					del player.games.uno.cards[card_id]

					broadcast({
						"player" : player, 
						"count" : len(player.games.uno.cards)
						}, "uno_card_count", 
						self.lobby.players, exclude=player,
						json_encoder=UserEncoder)
					successful = True
				
				else:
					if self.debug:
						logging.warning("Card does not fit on top of stack. "
							"Is the client desynchronized? (player: '%s')" %
							player)

				# If player has no cards left
				if len(player.games.uno.cards) == 0:
					broadcast(player.name, "uno_win", self.lobby.players)
					self.lobby.stop()

		self.play_card_lock.release()			
		player.send(successful, "uno_play_card")


	def draw_card(self, player):
		self.draw_card_lock.acquire()

		successful = False
		# If it's the turn of player who wants to play a card and
		# he hasn't drawn a card this turn yet and
		# he has no card that fits the top of the stack
		if player == self.playing_player and \
			not player.games.uno.has_drawn_card and \
			not can_play(player.games.uno.cards, self.card_stack[-1]):

			# Give player 1 card
			self.give_cards(1, player)
			# Can he play now? (could be optimized)
			# No -> End turn
			if not can_play(player.games.uno.cards, self.card_stack[-1]):
				self.end_turn()
			# Yes -> Can't draw any more cards
			else:
				player.games.uno.has_drawn_card = True

			successful = True

			if self.debug:
				logging.info("Player '%s' drew card '%s'" % (player, 
					player.games.uno.cards[-1]))
		
		self.draw_card_lock.release()
		player.send(successful, "uno_draw_card")


	# If client desynchonises -> Should never happen but ¯\_(ツ)_/¯
	def sync(self, player):
		player.send(player.games.uno.cards, "uno_sync",
			json_encoder=CardEncoder)


	def stop(self):
		self.turn_timer.cancel()

		for player in self.lobby.players:
			player.games.uno = None


	def player_leave(self, player):
		if player == self.playing_player:
			self.end_turn(player_inc=1)


class Lobby:
	def __init__(self, name, host):
		self.name = name
		self.host = host

		self.players = [host]

		self.playing = False

		self.game = None

		if config.lobby_debug:
			logging.info("Lobby '%s' created by '%s'" % (self, self.host))


	@property
	def player_count(self):
		return len(self.players)


	def join(self, player):
		successful = False

		if player not in self.players and not self.playing:
			player.lobby = self
			# Announce new player
			broadcast(player.name, "lobby_user_join", self.players)
			self.players.append(player)
			# Players currently in lobby (including you)
			player.send(self.players, "lobby_players",
				json_encoder=UserEncoder)
			# If host has changed since lobby_list
			player.send(self.host.name, "lobby_host")
			successful = True

			if config.lobby_debug:
				logging.info("Player '%s' joined lobby '%s'" % (player, 
					self))

		player.send(successful, "lobby_join")


	def leave(self, player):
		successful = False

		if player in self.players:
			# If game is currently being played
			if self.playing:
				# Just to be sure
				if self.game != None:
					# Invoke player leave hook *before* removing player from 
					# self.players
					self.game.player_leave(player)


			# Leave doesn't block, this could kick an unrelated player by
			# accident
			player_index = self.players.index(player)
			player.lobby = None
			

			del self.players[player_index]
			# Broadcast that a player has left
			broadcast(player_index, "lobby_user_leave", self.players)
			
			successful = True



			if self.playing:
				# Game stops when all but 1 player leaves
				if self.player_count <= 1:
					lobbies[self.name].stop()

					if config.lobby_debug:
						logging.info("Too few players in '%s'. Stopping game..." % 
							self)

			# No players left in lobby -> delete Lobby
			if self.player_count == 0:
				lobbies[self.name].stop()
				del lobbies[self.name]
				lobby_deleted = True

				if config.lobby_debug:
					logging.info("Lobby '%s' is empty. Deleting..." % 
						self)

			# Still players left
			# Host left -> Random player becomes host
			elif player == self.host:
				self.host = choice(self.players)
				broadcast(self.host.name, "lobby_host", self.players)

				if config.lobby_debug:
					logging.info("Lobby '%s' has new host '%s'" % (self, 
						self.host))



			if config.lobby_debug:
				logging.info("Player '%s' left lobby '%s'" % (player, 
					self))

		player.send(successful, "lobby_leave")


	def kick(self, player_to_be_kicked, issuing_player):
		successful = False
		if issuing_player == self.host:
			for player in self.players:
				if player.name == player_to_be_kicked:
					self.leave(player)
					successful = True
					break
		issuing_player.send(successful, "lobby_kick")



	def start(self, player):
		successful = False
		if player == self.host and not self.playing and self.player_count >= 2:
			self.playing = True
			broadcast(True, "lobby_playing", self.players)
			# Game possible replaceable in the future
			self.game = Uno(self, debug=config.game_debug)
			successful = True

			if config.lobby_debug:
				logging.info("Game '%s' started in lobby '%s'" % (self.game, 
					self))

		player.send(successful, "lobby_start")


	def stop(self, player=None):
		if player != None:
			successful = False
			if player == self.host and self.playing:
				self._stop()
				successful = True
			player.send(successful, "lobby_stop")
		else:
			self._stop()


	def _stop(self):
		if self.playing:
			self.playing = False
			# "Deallocation" and user games namespace cleanup
			self.game.stop()
			self.game = None
			broadcast(False, "lobby_playing", self.players)

			if config.lobby_debug:
				logging.info("Lobby '%s' stopped" % self)


	def chat_message_received(self, message, player):
		successful = True
		forward_message = True
		if message.startswith("/debug"):
			forward_message = False
			if player.in_game(Uno):
				options, args = CHEAT_PARSER.parse_args(
					split(message[len("/debug") + 1:]))
				if options.player == None:
					player_ = player
				else:
					player_ = find_player(options.player)
					if player_ == None:
						successful = False
					else:
						for _ in range(options.amount):
							self.game.give_card(options.face, 
								options.color, player_)

		
		if forward_message:
			broadcast({"player" : player, "message" : message}, 
				"lobby_chat_message", self.players, json_encoder=UserEncoder)

		# Nothing can go wrong (yet)
		player.send(successful, "lobby_chat")


	def __eq__(self, other):
		return type(other) is Lobby and other.name == self.name


	def __str__(self):
		return self.name


class User(Server):
	def __init__(self, sock, routes, debug=False):
		super().__init__(sock, routes, debug=debug)

		self.name = None
		self.lobby = None
		self.wins = 0

		self.games = SimpleNamespace()


	@property
	def logged_in(self):
		return self.name != None


	def in_game(self, game):
		if self.lobby != None:
			return type(self.lobby.game) is game
		return False


	def closed(self, code, reason):
		# Leave the lobby
		if self.lobby != None:
			self.lobby.leave(self)
		# Free up taken user name
		if self.name != None:
			del taken_names[taken_names.index(self.name)]

		if type(reason) is bytes:
			reason = reason.decode()

		if self.logged_in:
			logging.info("User '%s' disconnected ('%s': %d)" % (self.name,
				reason, code))
		else:
			logging.info("Unauthenticated user disconnected. ('%s': '%d')" % (
				reason, code))


	def __str__(self):
		return self.name if self.name else ""


class Login(Route):
	def run(self, data, handler):
		successful = False
		if type(data) is str:
			if not data in taken_names:
				taken_names.append(data)

				handler.name = data

				successful = True
		handler.send(successful, "login")


class LobbyList(Route):
	def run(self, data, handler):
		handler.send(lobbies, "lobby_list", json_encoder=LobbyEncoder)


class LobbyCreate(Route):
	def run(self, data, handler):
		successful = False
		if handler.logged_in:
			if type(data) is str and len(data) > 0:
				# If already in a lobby leave
				if handler.lobby:
					handler.lobby.leave(handler)
					handler.lobby = None
				# If lobby name not taken
				if not data in lobbies:
					lobby = Lobby(data, handler)

					lobbies[data] = lobby
					handler.lobby = lobby

					successful = True
		handler.send(successful, "lobby_create")

"""
All routes that wrap an instance method only implement
parameter and state checking. Logic specific to the class is
always handled in the class. This includes reporting errors
to the user and state corrections.

Linear state progression (A -> B -> C) is preferred, only the last
state has to to be checked this way. Every state should have a
default value indicating that it has not been reached. If that's
impossible for some good reason the use of helper functions is
encouraged.

If a certain function sigature is required the data is validated
before executing *any* further logic. (Variable definitions are allowed)
The first line in routes that return success or failure is always the
definiton of *successful* with an appropriate default value.
If failure is the only possible outcome outside of the instance method call,
successful should not be defined and return statements must be used to
speed up cancellation.
"""

class LobbyJoin(Route):
	def run(self, data, handler):
		successful = True
		if type(data) is str:
			if handler.logged_in:
				if handler.lobby != None:
					successful = False
				elif data in lobbies:
					lobbies[data].join(handler)



class LobbyLeave(Route):
	def run(self, data, handler):
		if handler.lobby:
			handler.lobby.leave(handler)
			return
		handler.send(False, "lobby_leave")


class LobbyStart(Route):
	def run(self, data, handler):
		if handler.lobby:
			handler.lobby.start(handler)
			return
		handler.send(False, "lobby_start")


class LobbyKick(Route):
	def run(self, data, handler):
		if type(data) is str:
			if handler.lobby:
				handler.lobby.kick(data, handler)
				return
		handler.send(False, "lobby_kick")


class LobbyChat(Route):
	def run(self, data, handler):
		if type(data) is str:
			if handler.lobby:
				handler.lobby.chat_message_received(data, handler)
				return
		handler.send(False, "lobby_chat")


class UnoPlayCard(Route):
	def run(self, data, handler):
		if handler.in_game(Uno):
			if type(data) is int:
				handler.lobby.game.play_card(data, handler)
				return
		handler.send(False, "uno_play_card")


class UnoDrawCard(Route):
	def run(self, data, handler):
		if handler.in_game(Uno):
			handler.lobby.game.draw_card(handler)
			return
		handler.send(False, "uno_draw_card")


class UnoSync(Route):
	def run(self, data, handler):
		if handler.in_game(Uno):
			handler.lobby.game.sync(handler)
			return
		handler.send(False, "uno_sync")


class REPL(Thread):
	def __init__(self):
		super().__init__()
		self.daemon = True

	def run(self):
		logging.header("REPL started. Type in Python code to introspect. "
			"(^D to restart)")
		while True:
			try:
				exec(input(""))
			except Exception as e:
				if type(e) is EOFError:
					execv(executable, ["python3"] + argv)
				else:
					capture_trace()

config = Config()
config.add(Option("address", "127.0.0.1"))
config.add(Option("port", 8500, validator=lambda port: type(port) is int))
config.add(Option("network_debug", False, validator=lambda debug: type(debug) is bool))
config.add(Option("game_debug", False, validator=lambda debug: type(debug) is bool))
config.add(Option("lobby_debug", False, validator=lambda debug: type(debug) is bool))
config.add(Option("repl", False, validator=lambda repl: type(repl) is bool))

CONFIG_PATH = "uno.cfg"

try:
	config = config.load(CONFIG_PATH)
except (IOError, ExceptionInConfigError):
	config.dump(CONFIG_PATH)
	config = config.load(CONFIG_PATH)

server = make_server(config.address, config.port,
	server_class=WSGIServer, handler_class=WebSocketWSGIRequestHandler,
	app=ServerWSGIApplication(User, routes={
		"login" : Login(),
		"lobby_list" : LobbyList(),
		"lobby_create" : LobbyCreate(),
		"lobby_join" : LobbyJoin(),
		"lobby_start" : LobbyStart(),
		"lobby_leave" : LobbyLeave(),
		"lobby_kick" : LobbyKick(),
		"lobby_chat" : LobbyChat(),
		"uno_play_card" : UnoPlayCard(),
		"uno_draw_card" : UnoDrawCard(),
		"uno_sync" : UnoSync()
	}, debug=config.network_debug))

server.initialize_websockets_manager()

if config.repl:
	logging.warning("Toggle the 'repl' flag before deploying!")
	repl = REPL()
	repl.start()

try:
	server.serve_forever()
except KeyboardInterrupt:
	server.server_close()