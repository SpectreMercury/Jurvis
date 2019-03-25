# -*- coding: utf-8 -*-
from bs4 import BeautifulSoup
import requests

base_url = 'http://liansai.500.com/'
league_code = 'zuqiu-4825'
data_type = 'teams'

request_url = base_url + league_code + '/' + data_type

res = requests.get(request_url,"html.parser")
soup = BeautifulSoup(res.text)
table_team = soup.find("table", attrs={"class": "ltable"})
table_body = table_team.find("tbody")

rows = table_body.find_all('tr')

for row in rows:
  cols = row.find_all('td')
  idx = 0
  for col in cols:
    if idx == 1:
      team_base_info = col.find('a')
      team_number = team_base_info.attrs['href'].split('http://liansai.500.com/team/')[1][:-1]
      team_name = team_base_info.text
    idx = idx + 1

#print table_body