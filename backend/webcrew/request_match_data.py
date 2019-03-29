# -*- coding: utf-8 -*-
import requests
import json
import os


def request_match_data(team_code):
  base_url = "http://liansai.500.com/index.php?"
  base_params = {
      'c': 'teams',
      'a': 'ajax_fixture',
      'records': '100',
      'tid': team_code,
      'hoa': '0'
  }
  origin_data = requests.get(base_url, params=base_params)
  origin_data = json.loads(origin_data.text)
  save_league_type(origin_data['legs'])
  extract_userful_data(origin_data['list'], team_code)
  last_data = get_last_data(team_code)
  return last_data
  
def save_league_type(data):
  file_path = os.path.dirname(os.getcwd()) + "/Jurvis/backend/data/base_data/league.json"
  save_action(data, file_path)


def get_match_id(data):
  match_id_set = []
  for i in data:
    match_id_set.append(i['FIXTUREID'])
  return match_id_set


def extract_userful_data(data, team_code):
  userful_data = {}
  userful_key = ['AWAYTEAMID', 'FIXTUREID', 'HOMETEAMID',
                 'MATCHID', 'WIN', 'DRAW', 'LOST', 'lpl_on', 'HANDICAPLINE']
  for i in data:
    single_item = {}
    FIXTUREID = i['FIXTUREID']

    for j in userful_key:
      single_item[j] = i[j]
    userful_data[FIXTUREID] = single_item
  save_match_data(userful_data, team_code)
  return userful_data


def save_match_data(data, team_code):
  file_path = os.path.dirname(os.getcwd()) + \
      "/Jurvis/backend/data/base_data/" + str(team_code) + '.json'
  save_action(data, file_path)


def save_action(data, file_path):
  if os.path.exists(file_path):
    file_content = open(file_path, 'r+')
    file_content = json.loads(file_content.read())
    data = dict(file_content, **data)
  data = json.dumps(data, ensure_ascii=False)
  with open(file_path, 'w') as f:
    f.write(data)
  

def get_last_data(team_code):
  file_path = os.path.dirname(os.getcwd()) + \
      "/Jurvis/backend/data/base_data/" + str(team_code) + '.json'
  data = json.loads(open(file_path).read())
  return data

def get_team_data(team_code) :
  last_data = request_match_data(team_code)
  return last_data

  
if __name__ == "__main__":
  print('==>')
  get_team_data(516)