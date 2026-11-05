PYTHON ?= python3
SWITCH_PULL = bash scripts/pull-switch-compose.sh

.PHONY: setup drop-dirty demo test switch-pull switch-up switch-down live-loop

setup:
	$(PYTHON) -m pip install -r requirements.txt

drop-dirty:
	$(PYTHON) -m data_room.drop

demo:
	$(PYTHON) -m data_room.demo

test:
	$(PYTHON) -m pytest -q

switch-pull:
	$(SWITCH_PULL) pull

switch-up:
	$(SWITCH_PULL) up

switch-down:
	$(SWITCH_PULL) down

live-loop:
	$(PYTHON) scripts/live_room_loop.py
