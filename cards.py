from json import JSONEncoder

# Colors
RED 		= 	0
YELLOW 		= 	1
GREEN 		= 	2
BLUE 		= 	3

# Special Faces (require matching color)
BLOCK 		= 	10
ROTATE 		= 	11
TAKE_TWO 	= 	12

# Special faces (don't require matching color)
PICK_COLOR 	= 	20
TAKE_FOUR 	= 	21


CARD_NAMES = {BLOCK : "Block", ROTATE : "Rotate", TAKE_TWO : "Take Two",
				PICK_COLOR : "Pick Color", TAKE_FOUR : "Take Four"}


ANSI_COLORS = {RED : "\033[31m", YELLOW : "\033[33m", GREEN : "\033[32m",
				BLUE : "\033[34m"}


class CardEncoder(JSONEncoder):
	def default(self, obj):
		if isinstance(obj, Card):
			return {
				"face" : obj.face,
				"color" : obj.color,
				}
		return JSONEncoder.default(self, obj)


class Card:
	# No factory class because no cards are created by the user
	# ALL_CARDS contains all cards
	def __init__(self, face, color=None):
		self.face = face
		self.color = color

	def can_play(self, card):
		# If special face
		if card.face in (PICK_COLOR, TAKE_FOUR):
			return True
		# If second on special card
		if self.face in (PICK_COLOR, TAKE_FOUR):
			return True
		# Same color (1 blue -> 2 blue, ...)
		if color != None and self.color == card.color:
			return True
		# Same face (1 -> 1, 2 -> 2, ...)
		if self.face == card.face:
			return True
		return False


	@property
	def can_take_two_turns(self):
		return self.face == PICK_COLOR


	def __repr__(self):
		if self.face <= 9:
			face = str(self.face)
		else:
			face = CARD_NAMES[self.face]
		if self.color != None:
			return "%s%s\033[0m" % (ANSI_COLORS[self.color], face)
		else:
			return face

	def __eq__(self, other):
		if type(other) is Card:
			if other.face == self.face and other.color == self.color:
				return True
		return False


def can_play(player_cards, card):
	for card_ in player_cards:
		if card.can_play(card_):
			return True
	return False


ALL_CARDS = []
REGULAR_CARDS = []

for color in range(RED, BLUE + 1):
	for face in range(0, TAKE_TWO + 1):
		card = Card(face, color)
		ALL_CARDS.append(card)
		if face <= 9:
			REGULAR_CARDS.append(card)


pick_color = Card(PICK_COLOR)
take_four = Card(TAKE_FOUR)

ALL_CARDS.append(pick_color)
ALL_CARDS.append(take_four)

