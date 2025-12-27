from aiogram.fsm.state import State, StatesGroup

class AddServer(StatesGroup):
    name = State()
    host = State()
    port = State()
    user = State()
    pw = State()

class EditServer(StatesGroup):
    field = State()
    value = State()

class AdminAdd(StatesGroup):
    uid = State()
