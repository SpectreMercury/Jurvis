# -*- coding: utf-8 -*-
from bs4 import BeautifulSoup
import requests
import os
import json


def generate_request_url(base_url, league_code, data_type):
  return base_url + league_code + '/' + data_type + '/'


def split_html(request_url):
  res = requests.get(request_url, "html.parser")
  soup = BeautifulSoup(res.text)
  table_team = soup.find("table", attrs={"class": "ltable"})
  table_body = table_team.find("tbody")
  rows = table_body.find_all('tr')
  return rows


def generate_league_data(rows):
  league_json_file = {}
  for row in rows:
    cols = row.find_all('td')
    idx = 0
    for col in cols:
      if idx == 1:
        team_base_info = col.find('a')
        team_number = team_base_info.attrs['href'].split(
            'http://liansai.500.com/team/')[1][:-1]
        team_name = team_base_info.text
        league_json_file[team_number] = team_name
      idx = idx + 1
  return league_json_file


def update_league_data_file(league_name, data):
  root_dir = os.path.dirname(os.getcwd()) + "/data/base_data/"
  file_name = league_name + ".json"
  file_name = os.path.join(root_dir, file_name)
  mode = 'w+'
  data = json.dumps(data, ensure_ascii=False)
  with open(file_name, mode) as f:
    f.write(data)


def request_data(league_code):
  base_url = 'http://liansai.500.com/'
  data_type = 'teams'
  league_code = league_code or 'zuqiu-4826'
  request_url = generate_request_url(base_url, league_code, data_type)
  html_content = split_html(request_url)
  leaguae_data = generate_league_data(html_content)
  update_league_data_file(league_code, leaguae_data)


if __name__ == "__main__":
  get_data('zuqiu-4826')
