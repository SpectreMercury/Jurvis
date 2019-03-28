import os
import json
import numpy
from sklearn.linear_model import LogisticRegression
import get_train_data
import warnings
warnings.filterwarnings("ignore")


def create_label_arry(data):
	odd_data = []  # rate
	odd_label = []  # result
	for k, v in data.items():
		odd_label.extend([k for i in range(len(v))])
		odd_data.extend(v)
	return odd_data, odd_label


def transfer_arr2_numpyarr(data, label, odd):
	t_data = numpy.array(data, dtype=object)
	t_label = numpy.array(label, dtype=object)
	t_odd = numpy.array(odd, dtype=object)
	return t_data, t_label, t_odd


"""
	@params 	
	home: use to get team data of home
	away: use to get team data of away
	rate: current odd for predict
"""


def train_data(home, away, league, odd):
	origin_data = get_train_data.get_match_train_data(home)
	base_train_data, base_train_label = create_label_arry(origin_data)
	t_data, t_label, t_odd = transfer_arr2_numpyarr(base_train_data, base_train_label, odd)
	# init algorithm model
	clf = LogisticRegression(solver='liblinear')
	#train data
	clf.fit(t_data, t_label)
	#result
	result = clf.predict_proba(t_odd)
	return result[0]


if __name__ == "__main__":
	train_data(516, 554, 'zuqiu-4826', [[1.12,9.70,22.22]])
