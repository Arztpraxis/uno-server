def broadcast(data, route, users, exclude=None, json_encoder=None):
	for user in users:
		if user != exclude:
			user.send(data, route, json_encoder=json_encoder)