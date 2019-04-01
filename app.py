from flask import Flask, jsonify, request
from flask_cors import CORS
from backend.api import get_data

# configuration


# instantiate the app
app = Flask(__name__)
app.config.from_object(__name__)
app.debug = True

# enable CORS
CORS(app)


@app.route('/get_team_data', methods=['GET'])
def get_league_data():
	league = request.args.get('league')
	mode = request.args.get('mode')
	if not league or not mode:
		rlt = {
			'errno': 403,
			'errmsg': 'missing value'
		}
		return jsonify(rlt)
	league_data = get_data.get_team_data(league, mode)
	return jsonify(league_data)


@app.route('/get_predict', methods=['GET'])
def get_team_data():
	h = request.args.get('home')
	a = request.args.get('away')
	l = request.args.get("league")
	c = request.args.get("company")
	win = request.args.get('win')
	draw = request.args.get('draw')
	lost = request.args.get('lost')


	if not h or not a or not l or not win or not draw or not lost:
		rlt = {
			'errno': 403,
			'errmsg': 'missing value'
		}
		return jsonify(rlt)
	if not c or not int(c):
		c = 0
	team_data = get_data.get_predict_result(h, a, l, win, draw, lost, c)
	return jsonify(team_data)


if __name__ == '__main__':
	app.run(debug=True)
