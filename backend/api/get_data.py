import json
import os
from backend.models import get_train_data
from backend.models import train_data


def format_result_data(data):
	rlt = {
		'errmsg': 'susscess',
		'errno': 0,
		'data': data
	}
	if not data:
		rlt['errno'] = -1
		rlt['data'] = {}
	return rlt


def get_predict_result(home, away, league, win, draw, lost, company):
	result = train_data.train_data(home, away, league, win, draw, lost, company)
	return format_result_data(result)


def get_team_data(league_code, mode=0):
	league_data = get_train_data.get_league_data(league_code, mode)
	return format_result_data(league_data)


if __name__ == "__main__":
	print('ok')
