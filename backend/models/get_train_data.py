# -*- coding: utf-8 -*-
import json
import os, sys
from webcrew import request_match_data
from webcrew import request_team_data


def combine_useful_data(data):
	useful_data = {
		"win": [],
		"lost": [],
		"draw": []
	}
	for k, v in data.items():
		single_match_item = [v['WIN'], v['LOST'], v['DRAW']]
		if v['lpl_on'] > 0:
			useful_data['win'].append(single_match_item)
		elif v['lpl_on'] == 0:
			useful_data['draw'].append(single_match_item)
		elif v['lpl_on'] < 0:
			useful_data['lost'].append(single_match_item)
	print(useful_data)
	return useful_data


def data_reactification(team_code, data):
	reactification_data = data
	# print(reactification_data)
	for k, v in reactification_data.items():
		if v['lpl_on'] == "胜":
			v['lpl_on'] = 1
		if v['lpl_on'] == "负":
			v['lpl_on'] = 0
		if v['lpl_on'] == "平":
			v['lpl_on'] = -1
		if v['HOMETEAMID'] != team_code:
			v['lpl_on'] = v['lpl_on'] * -1
			v['WIN'], v['LOST'] = v['LOST'], v['WIN']
	return reactification_data


def read_local_file(code):
	file_path = os.path.dirname(os.getcwd()) + \
							"/data/base_data/" + str(code) + '.json'
	if os.path.exists(file_path):
		return {}
	file_content = json.loads(open(file_path).read())
	return file_content


"""
@:param mode 
0: get local data
1: get the lastest data
@:param: team_code
team_code: team code
"""


def get_match_train_data(team_code, mode=0):
	if mode == 0:
		origin_data = request_match_data.get_team_data(team_code)
	else:
		origin_data = read_local_file(team_code)
		if origin_data == {}:
			origin_data = read_local_file(team_code)
	origin_data = data_reactification(team_code, origin_data)
	final_data = combine_useful_data(origin_data)
	return final_data


"""
@:param mode 
0: get local data
1: get the lastest data
@:param: league
league_code: team code
"""


def get_league_data(league_code, mode=0):
	if mode == 0:
		league_data = read_local_file(league_code)
	else:
		league_data = request_team_data.request_data(league_code)
		if league_data == {}:
			league_data = read_local_file(league_code)
	return league_data


if __name__ == "__main__":
	print('==>start')
	get_match_train_data(1072)
