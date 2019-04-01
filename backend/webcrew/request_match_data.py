# -*- coding: utf-8 -*-
import requests
import json
import os
import copy

base_url = "http://liansai.500.com/index.php"
base_file_path = os.path.dirname(os.getcwd())
league_file_path = base_file_path + "/Jurvis/backend/data/base_data/league.json"


def request_url_data(params):
	global base_url
	origin_data = requests.get(base_url, params=params)
	origin_data = json.loads(origin_data.text)
	return origin_data


def request_base_data(team_code, company):
	base_params = {
		'c': 'teams',
		'records': '100',
		'hoa': '0'
	}

	base_request_params = copy.deepcopy(base_params)
	base_request_params.update({
		'a': 'ajax_fixture',
		'records': '100',
		'tid': team_code,
	})
	origin_data = request_url_data(base_request_params)
	save_league_type(origin_data['legs'])
	userful_data, match_ids = extract_userful_data(origin_data['list'], team_code, company)
	if company == 0:
		last_data = get_last_data(team_code)
	else:
		# print(company)
		print(match_ids)
		company_params = copy.deepcopy(base_params)
		company_params.update({
			'fids': match_ids,
			'cid': company,
			'a': 'ajax_pl',
		})
		company_data = request_url_data(company_params)
		# print(origin_data)
		extract_company_data(userful_data, company_data['list'], team_code, company)
		last_data = get_last_data(team_code, company)

	return last_data


def save_league_type(data):
	global league_file_path
	save_action(data, league_file_path)


def get_match_id(data):
	match_id_set = []
	for i in data:
		match_id_set.append(i['FIXTUREID'])
	return match_id_set


def extract_userful_data(data, team_code, company=0):
	match_ids = []
	if company != 0:
		match_ids = get_match_id(data)
	userful_data = {}
	userful_key = ['AWAYTEAMID', 'FIXTUREID', 'HOMETEAMID',
								 'MATCHID', 'WIN', 'DRAW', 'LOST', 'lpl_on', 'HANDICAPLINE']
	for i in data:
		single_item = {}
		FIXTUREID = i['FIXTUREID']

		for j in userful_key:
			single_item[j] = i[j]
		userful_data[FIXTUREID] = single_item
	save_match_data(userful_data, team_code, company)
	return userful_data, match_ids


def extract_company_data(useful_data, company_data, team_code, company=0):
	t = {}
	u = useful_data
	c = company_data
	for v in c:
		if v['FIXTUREID'] in u.keys():
			u[v['FIXTUREID']]['WIN'] = v['WIN']
			u[v['FIXTUREID']]['LOST'] = v['LOST']
			u[v['FIXTUREID']]['DRAW'] = v['DRAW']
			t[v['FIXTUREID']] = u[v['FIXTUREID']]
	save_match_data(t, team_code, company)

def save_match_data(data, team_code, company):
	global base_file_path
	# print(company)
	file_name = str(team_code)
	if company != 0:
		file_name = file_name + '_' + str(company)
	file_path = base_file_path + "/Jurvis/backend/data/base_data/" + file_name + '.json'
	save_action(data, file_path)


def save_action(data, file_path):
	if os.path.exists(file_path):
		file_content = open(file_path, 'r+')
		file_content = json.loads(file_content.read())
		data = dict(file_content, **data)
	data = json.dumps(data, ensure_ascii=False)
	with open(file_path, 'w') as f:
		f.write(data)


def get_last_data(team_code, company=0):
	file_name = str(team_code);
	if company != 0:
		file_name = file_name + '_' + str(company)
	file_path = base_file_path + "/Jurvis/backend/data/base_data/" + file_name + '.json'
	data = json.loads(open(file_path).read())
	return data


def get_team_data(team_code, company):
	last_data = request_base_data(team_code, company)
	return last_data


if __name__ == "__main__":
	print('==>')
	get_team_data(1072, 293)
