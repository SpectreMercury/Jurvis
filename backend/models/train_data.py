import os
import json
import numpy
from sklearn.linear_model import LogisticRegression
from backend.models import get_train_data
import warnings
import logging

warnings.filterwarnings("ignore")


def create_label_arry(data):
	odd_data = []  # rate
	odd_label = []  # result
	match_num = 0
	for k, v in data.items():
		odd_label.extend([k for i in range(len(v))])
		match_num = match_num + len(v)
		odd_data.extend(v)
	return odd_data, odd_label, match_num


def transfer_arr2_numpyarr(data, label, odd):
	t_data = numpy.array(data, dtype=object)
	t_label = numpy.array(label, dtype=object)
	t_odd = numpy.array(odd, dtype=object)
	return t_data, t_label, t_odd


def get_team_name(home, away, league):
	file_path = os.path.dirname(os.getcwd()) + \
							"/Jurvis/backend/data/base_data/" + str(league) + '.json'
	teams = json.loads(open(file_path).read())
	return {
		'home': teams[home],
		'away': teams[away]
	}


"""
	@params 	
	home: use to get team data of home
	away: use to get team data of away
	rate: current odd for predict
"""


def train_data(home, away, league, win, draw, lost, company=0):
	odd = [[win, draw, lost]]
	logging.info(company)
	origin_data = get_train_data.get_match_train_data(home, company)
	base_train_data, base_train_label, match_num = create_label_arry(origin_data)
	t_data, t_label, t_odd = transfer_arr2_numpyarr(base_train_data, base_train_label, odd)
	# init algorithm model
	clf = LogisticRegression(solver='liblinear')
	# train data
	clf.fit(t_data, t_label)
	# result
	result = clf.predict_proba(t_odd)
	# team name
	team_info = get_team_name(home, away, league)
	team_info['result'] = result[0].tolist()
	team_info['match_num'] = match_num
	return team_info


if __name__ == "__main__":
	train_data(565, 1075, 'zuqiu-4826', '1', '2', '3', '3')
