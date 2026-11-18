from aiogram.fsm.state import State, StatesGroup


class OrderFlow(StatesGroup):
    waiting_player_id = State()
    confirming = State()


class UidCheckFlow(StatesGroup):
    waiting_uid = State()


class DepositFlow(StatesGroup):
    waiting_amount = State()
    waiting_txn_id = State()
    waiting_sender_number = State()


class AutoDepositFlow(StatesGroup):
    waiting_amount = State()
    waiting_recover_txn = State()


class AdminPackageFlow(StatesGroup):
    waiting_name = State()
    waiting_diamond_amount = State()
    waiting_price = State()
    waiting_provider_service_id = State()
    waiting_cost_price = State()


class AdminProviderFlow(StatesGroup):
    waiting_type = State()
    waiting_name = State()
    waiting_base_url = State()
    waiting_api_key = State()
    waiting_api_secret = State()
    waiting_currency = State()


class AdminPaymentMethodFlow(StatesGroup):
    waiting_name = State()
    waiting_account_number = State()
    waiting_account_type = State()
    waiting_instructions = State()


class AdminDeliveryNoteFlow(StatesGroup):
    waiting_note = State()


class AdminUserBalanceFlow(StatesGroup):
    waiting_amount = State()


class AdminDepositRejectFlow(StatesGroup):
    waiting_reason = State()


class AdminBroadcastFlow(StatesGroup):
    waiting_message = State()


class AdminReferralSettingsFlow(StatesGroup):
    waiting_amount = State()


class AdminMrAiPayFlow(StatesGroup):
    waiting_api_key = State()
    waiting_secret_key = State()
    waiting_brand_key = State()


class AdminImportFlow(StatesGroup):
    waiting_game = State()     # admin types a game name to search the provider catalog
    browsing = State()         # picking products from a category
    waiting_rate = State()     # exchange rate (provider currency -> BDT)
    waiting_markup = State()   # profit: "10%" or "15"
