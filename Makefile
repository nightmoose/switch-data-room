PYTHON ?= python3

.PHONY: setup drop-dirty demo test

setup:
	$(PYTHON) -m pip install -r requirements.txt

drop-dirty:
	$(PYTHON) -m data_room.drop

demo:
	$(PYTHON) -m data_room.demo

test:
	$(PYTHON) -m pytest -q
