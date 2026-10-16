"""
Функциональные тесты плагина lots_off_by_category.py на моках.
Запуск: cd FPC && python3 tests_plugin_lots_off.py
"""
import importlib.util
import json
import os
import sys
import time
import threading
from types import SimpleNamespace

sys.path.insert(0, ".")
sys.path.append("plugins")

from FunPayAPI import types as fp_types
from FunPayAPI.common import enums as fp_enums

# ───────────────────────────── загрузка плагина ─────────────────────────────
spec = importlib.util.spec_from_file_location("plugins.lots_off_by_category",
                                              "plugins/lots_off_by_category.py")
plugin = importlib.util.module_from_spec(spec)
sys.modules["plugins.lots_off_by_category"] = plugin
spec.loader.exec_module(plugin)

plugin.SAVE_DELAY = 0          # ускоряем тесты
# нейтрализуем все sleep'ы внутри плагина (time.sleep/time.time)
plugin.time = SimpleNamespace(sleep=lambda s: None, time=time.time)
plugin._watchdog_loop = lambda crd: None  # поток хранителя тестируем отдельно

PASS = []
FAIL = []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("✓ " if cond else "✗ FAIL: ") + name)


def reset_settings(watchdog=True, interval=60, managed=None):
    os.makedirs(plugin.PLUGIN_DIR, exist_ok=True)
    with open(plugin.SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump({"managed": managed or {}, "watchdog": watchdog, "interval": interval}, f)


# ───────────────────────────── мок-объекты ─────────────────────────────
class MockLotFields:
    def __init__(self, lot_id, active):
        self.lot_id = lot_id
        self.active = active


class MockAccount:
    def __init__(self, categories, lots_by_sub):
        self.is_initiated = True
        self._categories = categories
        self.lots_by_sub = lots_by_sub
        self.saved = []
        self.get_lot_fields_calls = 0
        self.errors_in = None  # id подкатегории, где кидать ошибку

    @property
    def categories(self):
        return self._categories

    @property
    def subcategories(self):
        result = []
        for cat in self._categories:
            result.extend(cat.get_subcategories())
        return result

    def get_subcategory(self, type_, id_):
        for cat in self._categories:
            sc = cat.get_subcategory(type_, id_)
            if sc:
                return sc
        return None

    def get_my_subcategory_lots(self, sub_id, locale=None):
        if self.errors_in == sub_id:
            raise Exception("mock request error")
        return list(self.lots_by_sub.get(sub_id, []))

    def get_lot_fields(self, lot_id, node_id=None):
        self.get_lot_fields_calls += 1
        for lst in self.lots_by_sub.values():
            for lot in lst:
                if lot.id == lot_id:
                    return MockLotFields(lot_id, lot.active)
        raise Exception("mock: lot not found")

    def save_lot(self, fields, locale=None):
        self.saved.append((fields.lot_id, fields.active))
        for lst in self.lots_by_sub.values():
            for lot in lst:
                if lot.id == fields.lot_id:
                    lot.active = fields.active


def make_world():
    cat1 = fp_types.Category(1, "Dota 2")
    s101 = fp_types.SubCategory(101, "Аккаунты", fp_enums.SubCategoryTypes.COMMON, cat1)
    s102 = fp_types.SubCategory(102, "Предметы", fp_enums.SubCategoryTypes.COMMON, cat1)
    s103 = fp_types.SubCategory(103, "Валюта", fp_enums.SubCategoryTypes.CURRENCY, cat1)
    for s in (s101, s102, s103):
        cat1.add_subcategory(s)
    cat2 = fp_types.Category(2, "Steam")
    s201 = fp_types.SubCategory(201, "Игры", fp_enums.SubCategoryTypes.COMMON, cat2)
    cat2.add_subcategory(s201)

    def lot(id_, sub, active, desc):
        return fp_types.MyLotShortcut(id_, None, None, desc, 5, 100.0,
                                      fp_enums.Currency.UNKNOWN, sub, False, active, "")

    lots_by_sub = {
        101: [lot(1001, s101, True, "Акк 1"), lot(1002, s101, False, "Акк 2"), lot(1003, s101, True, "Акк 3")],
        102: [lot(2001, s102, True, "Предмет 1")],
        201: [lot(3001, s201, True, "Игра 1"), lot(3002, s201, True, "Игра 2")],
    }
    return [cat1, cat2], lots_by_sub


class MockBot:
    def __init__(self):
        self.answers = []
        self.edits = []      # (text, keyboard)
        self.sent = []       # (chat_id, text) — для send_message
        self.replies = []

    def answer_callback_query(self, cb_id, text=None, show_alert=False):
        self.answers.append((cb_id, text, show_alert))

    def edit_message_text(self, text, chat_id, message_id, parse_mode=None, reply_markup=None):
        self.edits.append((text, reply_markup))
        return SimpleNamespace(id=message_id)

    def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        self.sent.append((chat_id, text))
        return SimpleNamespace(id=len(self.sent) + 100, chat=SimpleNamespace(id=chat_id))

    def reply_to(self, m, text, parse_mode=None):
        self.replies.append(text)

    def delete_message(self, chat_id, message_id):
        pass


def kb_buttons(kb) -> str:
    """Склеивает тексты всех кнопок клавиатуры в одну строку для проверок."""
    if kb is None:
        return ""
    try:
        return " | ".join(b.text for row in kb.keyboard for b in row)
    except Exception:
        return str(kb)


def kb_button_list(kb) -> list:
    """Список текстов кнопок клавиатуры (для проверок порядка)."""
    if kb is None:
        return []
    try:
        return [b.text for row in kb.keyboard for b in row]
    except Exception:
        return []


class MockTelegram:
    def __init__(self):
        self.bot = MockBot()
        self.cbq_handlers = []
        self.msg_handlers = []
        self.user_states = {}
        self.notification_settings = {}   # {str(chat_id): {тип: 0/1}} — как в реальном TGBot
        self.notifications = []           # (text, notification_type) — факт вызова рассылки

    def cbq_handler(self, handler, func, **kwargs):
        self.cbq_handlers.append((handler, func))

    def msg_handler(self, handler, **kwargs):
        self.msg_handlers.append((handler, kwargs))

    def set_state(self, chat_id, message_id, user_id, state, data=None):
        self.user_states[(chat_id, user_id)] = {"state": state, "mid": message_id}

    def check_state(self, chat_id, user_id, state):
        return self.user_states.get((chat_id, user_id), {}).get("state") == state

    def clear_state(self, chat_id, user_id, del_msg=False):
        st = self.user_states.pop((chat_id, user_id), None)
        return st.get("mid") if st else None

    def is_notification_enabled(self, chat_id, notification_type):
        """Как в реальном TGBot: чат должен быть в notification_settings с включенным типом."""
        try:
            return bool(self.notification_settings[str(chat_id)][notification_type])
        except KeyError:
            return False

    def send_notification(self, text, notification_type=None, **kwargs):
        """Как в реальном TGBot: доставляет только в чаты с включенным типом уведомления."""
        self.notifications.append((text, notification_type))
        for chat_id in list(self.notification_settings):
            if notification_type is not None and not self.is_notification_enabled(chat_id, notification_type):
                continue
            self.bot.send_message(int(chat_id), text)


class MockCardinal:
    def __init__(self, account, telegram):
        self.account = account
        self.telegram = telegram
        self.plugins = {plugin.UUID: SimpleNamespace(enabled=True)}
        self.autorestore_enabled = True
        self.registered_commands = None

    def add_telegram_commands(self, uuid, commands):
        self.registered_commands = commands


def make_crd(with_tg=True):
    cats, lots = make_world()
    acc = MockAccount(cats, lots)
    tg = MockTelegram() if with_tg else None
    return MockCardinal(acc, tg), acc, tg


# ═══════════════════════════ ТЕСТ 1: метаданные ═══════════════════════════
reset_settings()
check("1.1 NAME/VERSION/UUID заданы", bool(plugin.NAME and plugin.VERSION and plugin.UUID))
check("1.2 SETTINGS_PAGE=True", plugin.SETTINGS_PAGE is True)
check("1.3 BIND_TO_PRE_INIT содержит init", plugin.BIND_TO_PRE_INIT == [plugin.init])
check("1.4 BIND_TO_DELETE вызываемый", callable(plugin.BIND_TO_DELETE))

# ═══════════════════ ТЕСТ 2: init без Telegram + _set_lots_state ═══════════════
crd, acc, _ = make_crd(with_tg=False)
plugin.init(crd)
check("2.1 init без Telegram не падает", True)

# отключение лотов подкатегории 101 (лоты: 1001 вкл, 1002 выкл, 1003 вкл)
reset_settings(managed={"101": "Аккаунты Dota 2"})
plugin._set_lots_state(crd, [101], disable=True)
check("2.2 отключены только активные лоты (2 шт.)", acc.saved == [(1001, False), (1003, False)])

# включение обратно
acc.saved.clear()
plugin._set_lots_state(crd, [101], disable=False)
check("2.3 включены только неактивные лоты (3 шт.)", acc.saved == [(1001, True), (1002, True), (1003, True)])

# несколько подкатегорий + ошибка сети в одной
acc.saved.clear()
acc.errors_in = 201
plugin._set_lots_state(crd, [101, 102, 201], disable=True)
check("2.4 ошибка в подкатегории не ломает остальные",
      acc.saved == [(1001, False), (1002, False), (1003, False), (2001, False)])
acc.errors_in = None

# ═══════════════════ ТЕСТ 3: хранитель (_enforce_once) ═══════════════════
crd, acc, _ = make_crd(with_tg=False)
reset_settings(managed={"101": "Аккаунты Dota 2"})
# лоты 101 снова включены (кем-то)
acc.lots_by_sub[101][0].active = True
acc.lots_by_sub[101][2].active = True
plugin._enforce_once(crd)
check("3.1 хранитель снова отключил включенные лоты", acc.saved == [(1001, False), (1003, False)])

acc.saved.clear()
plugin._enforce_once(crd)
check("3.2 хранитель не трогает уже выключенные лоты", acc.saved == [])

# хранитель выключен настройкой
reset_settings(watchdog=False, managed={"101": "Аккаунты Dota 2"})
acc.lots_by_sub[101][0].active = True
acc.saved.clear()
plugin._enforce_once(crd)
check("3.3 хранитель ВЫКЛ — лоты не трогает", acc.saved == [])
acc.lots_by_sub[101][0].active = False

# ═══════════════════ ТЕСТ 4: перехват autoRestore (патч handlers) ═══════════════════
crd, acc, _ = make_crd(with_tg=False)
plugin.init(crd)  # применяет патч
import handlers
check("4.1 handlers.update_lot_state пропатчен", getattr(handlers, "_lso_patched", False) is True)

reset_settings(managed={"101": "Аккаунты Dota 2"})
sub101 = crd.account.get_subcategory(fp_enums.SubCategoryTypes.COMMON, 101)
sub201 = crd.account.get_subcategory(fp_enums.SubCategoryTypes.COMMON, 201)
managed_lot = fp_types.MyLotShortcut(1001, None, None, "Акк 1", 5, 100.0,
                                     fp_enums.Currency.UNKNOWN, sub101, False, False, "")
other_lot = fp_types.MyLotShortcut(3001, None, None, "Игра 1", 5, 100.0,
                                   fp_enums.Currency.UNKNOWN, sub201, False, False, "")

acc.get_lot_fields_calls = 0
acc.saved.clear()
res = handlers.update_lot_state(crd, managed_lot, 1)  # попытка автовосстановления
check("4.2 восстановление лота из отключенной категории заблокировано",
      res is True and acc.get_lot_fields_calls == 0 and acc.saved == [])

acc.lots_by_sub[201][0].active = False  # лот вне управляемой категории деактивирован
res = handlers.update_lot_state(crd, other_lot, 1)  # лот из другой категории
check("4.3 лоты вне отключенных категорий восстанавливаются как обычно",
      res is True and acc.saved == [(3001, True)])

acc.saved.clear()
res = handlers.update_lot_state(crd, managed_lot, -1)  # деактивация управляемого лота
check("4.4 деактивация управляемого лота проходит как обычно",
      res is True and acc.saved == [(1001, False)])

# плагин выключен через ПУ → автоrestore снова работает
crd.plugins[plugin.UUID].enabled = False
acc.get_lot_fields_calls = 0
acc.saved.clear()
managed_lot.active = False
res = handlers.update_lot_state(crd, managed_lot, 1)
check("4.5 при выключенном плагине autoRestore не блокируется",
      res is True and acc.saved == [(1001, True)])
crd.plugins[plugin.UUID].enabled = True

plugin.on_delete(crd)
check("4.6 on_delete снимает патч", getattr(handlers, "_lso_patched", False) is False)
plugin._patch_autorestore()  # возвращаем для следующих тестов
plugin._stop_event.clear()

# ═══════════════════ ТЕСТ 5: Telegram UI ═══════════════════
crd, acc, tg = make_crd(with_tg=True)
plugin.init(crd)
bot = tg.bot
check("5.1 команда /lotsoff зарегистрирована",
      any(kw.get("commands") == ["lotsoff"] for _, kw in tg.msg_handlers))
check("5.2 add_telegram_commands вызван", crd.registered_commands is not None
      and crd.registered_commands[0][0] == "lotsoff")


def dispatch_cb(data, chat_id=111, msg_id=555, user_id=222):
    c = SimpleNamespace(data=data, id="cb1",
                        message=SimpleNamespace(chat=SimpleNamespace(id=chat_id), id=msg_id),
                        from_user=SimpleNamespace(id=user_id))
    for handler, func in tg.cbq_handlers:
        try:
            matched = func(c)
        except Exception:
            matched = False
        if matched:
            handler(c)
            return handler.__name__
    return None


def last_text():
    if bot.edits:
        return bot.edits[-1][0]
    return bot.sent[-1][1] if bot.sent else ""


def last_kb():
    return kb_buttons(bot.edits[-1][1]) if bot.edits else ""


# кнопка «Настройки» со страницы плагина (CBT.PLUGIN_SETTINGS = "47")
name = dispatch_cb(f"47:{plugin.UUID}:0")
check("5.3 кнопка настроек плагина открывает главное меню",
      name == "open_home" and "Отключенных подкатегорий" in last_text())

# категории
reset_settings()
name = dispatch_cb(f"{plugin.CB_CATS}:0")
check("5.4 список категорий (Dota 2, Steam)",
      name == "open_cats" and "Dota 2" in last_kb() and "Steam" in last_kb())

# страница категории: 2 COMMON-подкатегории из 3 (валюта исключена)
name = dispatch_cb(f"{plugin.CB_CAT}:1:0")
check("5.5 страница категории показывает только лотовые подкатегории",
      name == "open_cat" and "Аккаунты" in last_kb() and "Предметы" in last_kb()
      and "Валюта" not in last_kb())

# отключение подкатегории 101
name = dispatch_cb(f"{plugin.CB_TSUB}:101:1:0")
check("5.6 toggle_sub: подкатегория добавлена в managed",
      name == "toggle_sub" and "101" in plugin._load_settings()["managed"])
# РЕГРЕСС-ПРОВЕРКА: статус 🔴 должен появиться СРАЗУ, без перехода на другую страницу
check("5.6b статус 🔴 обновился сразу после нажатия (страница перерисована)",
      "🔴 Аккаунты" in last_kb())
time.sleep(0.5)
check("5.6c лоты подкатегории действительно отключены",
      all(not lot.active for lot in acc.lots_by_sub[101]))

# страница категории по-прежнему показывает 🔴
dispatch_cb(f"{plugin.CB_CAT}:1:0")
check("5.7 статус 🔴 у отключенной подкатегории", "🔴 Аккаунты" in last_kb())

# отключение ВСЕЙ категории 1 (включая уже отключенную 101)
acc.saved.clear()
name = dispatch_cb(f"{plugin.CB_TCAT}:1:0")
time.sleep(0.5)
managed = plugin._load_settings()["managed"]
check("5.8 toggle_cat: обе подкатегории в managed, лоты 102 отключены",
      name == "toggle_cat" and "101" in managed and "102" in managed
      and not acc.lots_by_sub[102][0].active)
check("5.8b кнопка категории СРАЗУ переключилась на «🟢 Включить всю категорию»",
      "🟢 Включить всю категорию" in last_kb())

# вся категория отключена → кнопка теперь «Включить всю категорию»
dispatch_cb(f"{plugin.CB_CAT}:1:0")
check("5.9 кнопка категории переключилась на «Включить всю»", "🟢 Включить всю категорию" in last_kb())

# включение всей категории
acc.saved.clear()
dispatch_cb(f"{plugin.CB_TCAT}:1:0")
time.sleep(0.5)
managed = plugin._load_settings()["managed"]
check("5.10 toggle_cat: категория включена, managed очищен по ней",
      "101" not in managed and "102" not in managed
      and all(lot.active for lot in acc.lots_by_sub[101] + acc.lots_by_sub[102]))

# список отключенных + включение из списка
reset_settings(managed={"201": "Игры Steam"})
name = dispatch_cb(f"{plugin.CB_MANAGED}:0")
check("5.11 список отключенных показывает managed", name == "open_managed" and "Игры Steam" in last_kb())
name = dispatch_cb(f"{plugin.CB_UNM}:201:0")
time.sleep(0.5)
check("5.12 unmange: лоты включены, запись удалена",
      name == "unmanage" and plugin._load_settings()["managed"] == {}
      and all(lot.active for lot in acc.lots_by_sub[201]))

# «включить все» с подтверждением
reset_settings(managed={"101": "Аккаунты Dota 2", "201": "Игры Steam"})
acc.lots_by_sub[101][0].active = False
name = dispatch_cb(plugin.CB_ASK_ALLON)
check("5.13 запрос подтверждения «включить все»", name == "ask_all_on" and "Включить ВСЕ лоты?" in last_text())
name = dispatch_cb(plugin.CB_ALLON)
time.sleep(0.5)
check("5.14 «включить все»: managed очищен, лоты включены",
      name == "all_on" and plugin._load_settings()["managed"] == {}
      and acc.lots_by_sub[101][0].active)

# хранитель вкл/выкл
reset_settings(watchdog=True)
dispatch_cb(plugin.CB_WD)
check("5.15 хранитель переключается кнопкой", plugin._load_settings()["watchdog"] is False)
dispatch_cb(plugin.CB_WD)
check("5.16 хранитель переключается обратно", plugin._load_settings()["watchdog"] is True)

# интервал: кнопка → состояние → ввод числа
reset_settings(interval=60)
name = dispatch_cb(plugin.CB_INT)
check("5.17 кнопка интервала ставит состояние ввода",
      name == "act_edit_interval" and tg.user_states.get((111, 222), {}).get("state") == plugin.STATE_INT)

msg = SimpleNamespace(chat=SimpleNamespace(id=111), from_user=SimpleNamespace(id=222), text="45")
h, kw = next((h, kw) for h, kw in tg.msg_handlers if kw.get("func") and kw["func"](msg))
h(msg)
check("5.18 интервал сохранен (45с)", plugin._load_settings()["interval"] == 45 and bot.replies)

msg = SimpleNamespace(chat=SimpleNamespace(id=111), from_user=SimpleNamespace(id=222), text="99999")
tg.set_state(111, 1, 222, plugin.STATE_INT)
h(msg)
check("5.19 интервал зажимается в диапазон (99999 → 3600)", plugin._load_settings()["interval"] == 3600)

msg = SimpleNamespace(chat=SimpleNamespace(id=111), from_user=SimpleNamespace(id=222), text="abc")
tg.set_state(111, 1, 222, plugin.STATE_INT)
h(msg)
check("5.20 нечисловой ввод отклоняется", plugin._load_settings()["interval"] == 3600 and "❌" in bot.replies[-1])

# noop + главная
check("5.21 noop отвечает на callback", dispatch_cb(plugin.CB_NOOP) == "noop")
dispatch_cb(plugin.CB_HOME)
check("5.22 возврат в главное меню", "Массовое отключение" in last_text())

# ── избранное ──
reset_settings()
dispatch_cb(f"{plugin.CB_CAT}:1:0")
check("5.23 на странице категории есть кнопка «⭐ В избранное»", "⭐ В избранное" in last_kb())
name = dispatch_cb(f"{plugin.CB_FAV}:2:0")
check("5.24 избранное: категория добавляется",
      name == "toggle_fav" and 2 in plugin._load_settings()["favorites"])
dispatch_cb(f"{plugin.CB_CATS}:0")
btns = kb_button_list(bot.edits[-1][1])
steam_i = next((i for i, t in enumerate(btns) if "Steam" in t), None)
dota_i = next((i for i, t in enumerate(btns) if "Dota 2" in t), None)
check("5.25 избранное: любимая категория первой в списке",
      steam_i is not None and dota_i is not None and steam_i < dota_i)
check("5.26 избранное: ★ у любимой категории в списке",
      any(t.startswith("★") and "Steam" in t for t in btns))
dispatch_cb(f"{plugin.CB_CAT}:2:0")
check("5.27 страница избранной категории: кнопка «☆ Убрать из избранного» + отметка в заголовке",
      "☆ Убрать из избранного" in last_kb() and "(в избранном)" in last_text())
dispatch_cb(f"{plugin.CB_FAV}:2:0")
check("5.28 избранное: повторное нажатие убирает категорию",
      2 not in plugin._load_settings()["favorites"])
dispatch_cb(f"{plugin.CB_CATS}:0")
btns = kb_button_list(bot.edits[-1][1])
check("5.29 избранное: после удаления порядок прежний (Dota 2 первая)",
      next(i for i, t in enumerate(btns) if "Dota 2" in t)
      < next(i for i, t in enumerate(btns) if "Steam" in t))
reset_settings()

# ── сообщения о завершении операции («категория выключена/включена») ──
bot.sent.clear()
dispatch_cb(f"{plugin.CB_TSUB}:101:1:0")
time.sleep(0.5)
direct = [t for cid, t in bot.sent if cid == 111]
check("5.30 сообщение о завершении: «Категория ... выключена» приходит в чат, откуда нажали",
      len(direct) == 1 and "Категория «Аккаунты Dota 2» выключена" in direct[0]
      and "Всего лотов" in direct[0])

reset_settings(managed={"101": "Аккаунты Dota 2"})
bot.sent.clear()
dispatch_cb(f"{plugin.CB_UNM}:101:0")
time.sleep(0.5)
direct = [t for cid, t in bot.sent if cid == 111]
check("5.31 сообщение о завершении включения: «Категория ... включена»",
      len(direct) == 1 and "Категория «Аккаунты Dota 2» включена" in direct[0])

reset_settings()
bot.sent.clear()
dispatch_cb(f"{plugin.CB_TCAT}:1:0")
time.sleep(0.5)
direct = [t for cid, t in bot.sent if cid == 111]
check("5.32 вся категория: сообщение с названием категории «Dota 2»",
      len(direct) == 1 and "Категория «Dota 2» выключена" in direct[0])

# уведомления «деактивация лотов» для запрашивающего чата включены → дубля быть не должно
tg.notification_settings = {"111": {"7": 1, "6": 1}}
reset_settings()
bot.sent.clear()
dispatch_cb(f"{plugin.CB_TSUB}:101:1:0")
time.sleep(0.5)
direct = [t for cid, t in bot.sent if cid == 111 and "Аккаунты Dota 2" in t]
check("5.33 нет дублей: при включенных уведомлениях в чат приходит ровно одно сообщение",
      len(direct) == 1)
tg.notification_settings = {}
reset_settings()

# ═══════════════════ ТЕСТ 6: цикл хранителя (реальный поток) ═══════════════════
crd, acc, _ = make_crd(with_tg=False)
reset_settings(watchdog=True, interval=1, managed={"201": "Игры Steam"})
# грузим свежую копию модуля — в ней _watchdog_loop не подменен
spec2 = importlib.util.spec_from_file_location("lso_fresh", "plugins/lots_off_by_category.py")
fresh = importlib.util.module_from_spec(spec2)
spec2.loader.exec_module(fresh)
fresh.SAVE_DELAY = 0
fresh.INTERVAL_RANGE = (1, 3600)
fresh._cardinal = crd
t = threading.Thread(target=fresh._watchdog_loop, args=(crd,), daemon=True)
t.start()
time.sleep(1.5)
acc.lots_by_sub[201][0].active = True   # кто-то включил лот
time.sleep(8)
check("6.1 поток хранителя сам отключил вновь включенный лот", acc.lots_by_sub[201][0].active is False)
fresh._stop_event.set()
time.sleep(0.5)
check("6.2 поток хранителя останавливается по _stop_event", not t.is_alive())

# ═══════════════════ ТЕСТ 7: настройки ═══════════════════
reset_settings()
s = plugin._load_settings()
check("7.1 дефолты читаются",
      s == {"managed": {}, "watchdog": True, "interval": 60, "favorites": []})
with open(plugin.SETTINGS_PATH, "w") as f:
    f.write("{broken json")
s = plugin._load_settings()
check("7.2 битый JSON → дефолты, без падения", s["interval"] == 60 and s["managed"] == {})
s = {"managed": "not-a-dict", "watchdog": 1, "interval": 5}
plugin._save_settings(s)
s = plugin._load_settings()
check("7.3 некорректные значения нормализуются",
      s["managed"] == {} and s["watchdog"] is True and s["interval"] == 30)
s = plugin._load_settings()
s["favorites"] = "broken"
plugin._save_settings(s)
check("7.4 некорректные favorites нормализуются в []", plugin._load_settings()["favorites"] == [])
s = plugin._load_settings()
s["favorites"] = ["3", 5, "x", None]
plugin._save_settings(s)
check("7.5 favorites: строки приводятся к int, мусор отбрасывается",
      plugin._load_settings()["favorites"] == [3, 5])
reset_settings()

# ═══════════════════════════ ИТОГ ═══════════════════════════
print(f"\n{'=' * 60}\nПройдено: {len(PASS)}  Провалено: {len(FAIL)}")
if FAIL:
    print("Проваленные тесты:")
    for f in FAIL:
        print("  ✗", f)
    sys.exit(1)
print("ВСЕ ТЕСТЫ ПРОЙДЕНЫ ✅")
