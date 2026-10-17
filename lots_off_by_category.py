"""
Отключение лотов по категориям (LotsOff) — плагин для FunPay Cardinal v0.1.17+

Позволяет в пару кликов отключать (деактивировать) и включать обратно ВСЕ лоты
выбранной категории (игры) или подкатегории FunPay через Telegram-меню.

Особенности:
 • Меню категорий/подкатегорий со статусами (🔴 отключено / 🟢 включено);
   статусы обновляются СРАЗУ после нажатия кнопки (без перехода на другую страницу).
 • ⭐ Избранные категории — показываются первыми в общем списке категорий.
 • Массовое отключение/включение лотов целой категории или отдельной подкатегории.
 • «Хранитель» — удерживает лоты в отключенных категориях выключенными:
   перехватывает автовосстановление Кардинала + периодическая фоновая проверка.
 • ✅ По завершении отключения/включения пишет сообщение в чат, откуда запущена
   операция: «Категория «...» выключена/включена» + статистика по лотам.
 • Настройка интервала проверки хранителя.
 • Настройки сохраняются в storage/plugins/lots_off/settings.json.

Команды Telegram:
 • /lotsoff — открыть меню плагина.

Установка: положить этот файл в папку plugins/ и перезапустить Кардинал.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import TYPE_CHECKING, Callable

from telebot.types import InlineKeyboardMarkup as K, InlineKeyboardButton as B, CallbackQuery, Message
from telebot.apihelper import ApiTelegramException

try:
    from tg_bot import CBT, static_keyboards as skb, utils as tg_utils
except Exception:  # загрузка вне Кардинала (например, при проверке синтаксиса)
    CBT = None
    skb = None
    tg_utils = None

if TYPE_CHECKING:
    from cardinal import Cardinal

from FunPayAPI.common import enums as fp_enums

logger = logging.getLogger("FPC.LotsOff")

# ══════════════════════════════ МЕТАДАННЫЕ ПЛАГИНА ══════════════════════════════
NAME = "LotsOff (Deactivate Lots by Categories)"
VERSION = "1.2.1"
DESCRIPTION = ("Массовое отключение/включение лотов по категориям и подкатегориям FunPay через Telegram-меню. "
               "Избранные категории — в начале списка. По завершении операции пишет в чат "
               "«Категория выключена/включена». «Хранитель» удерживает лоты отключенных категорий "
               "выключенными, даже если включено автовосстановление Кардинала. Команда: /lotsoff")
CREDITS = "yungspider"
UUID = "b70a0b81-ce8d-431f-b387-b781d8461345"
SETTINGS_PAGE = True

# ══════════════════════════════ НАСТРОЙКИ ПЛАГИНА ═══════════════════════════════
PLUGIN_DIR = os.path.join("storage", "plugins", "lots_off")
SETTINGS_PATH = os.path.join(PLUGIN_DIR, "settings.json")

DEFAULT_SETTINGS = {
    "managed": {},        # {str(id подкатегории): "название"} — категории, лоты которых держим ВЫКЛЮЧЕННЫМИ
    "watchdog": True,     # хранитель вкл/выкл
    "interval": 60,       # интервал проверки хранителя, сек
    "favorites": [],      # [id категории, ...] — избранные категории, показываются первыми в списке
}
INTERVAL_RANGE = (30, 3600)
PER_PAGE = 10             # категорий/подкатегорий на страницу меню
SAVE_DELAY = 0.8          # пауза между сохранениями лотов, сек (анти-флуд)

# ══════════════════════════════ CALLBACK-КОДЫ ═══════════════════════════════════
CB_HOME = "lso:home"
CB_CATS = "lso:cats"        # lso:cats:{offset}
CB_CAT = "lso:cat"          # lso:cat:{cat_id}:{offset}
CB_TSUB = "lso:tsub"        # lso:tsub:{sub_id}:{cat_id}:{offset} — переключить подкатегорию
CB_TCAT = "lso:tcat"        # lso:tcat:{cat_id}:{offset}          — переключить всю категорию
CB_FAV = "lso:fav"          # lso:fav:{cat_id}:{offset}           — добавить/убрать категорию из избранного
CB_MANAGED = "lso:mng"      # lso:mng:{offset}           — список отключенных
CB_UNM = "lso:unm"          # lso:unm:{sub_id}:{offset}  — включить подкатегорию из списка
CB_ASK_ALLON = "lso:askallon"
CB_ALLON = "lso:allon"
CB_WD = "lso:wd"            # хранитель вкл/выкл
CB_INT = "lso:int"          # изменить интервал
CB_NOOP = "lso:noop"
STATE_INT = "lso:int_state"

_stop_event = threading.Event()
_op_lock = threading.Lock()
_settings_lock = threading.Lock()
_orig_update_lot_state = None
_patched_wrapper = None
_cardinal: "Cardinal | None" = None


# ══════════════════════════════ УТИЛИТЫ ═════════════════════════════════════════
def _clamp(value, min_v, max_v):
    return max(min_v, min(max_v, value))


def _load_settings() -> dict:
    with _settings_lock:
        data = {}
        if os.path.exists(SETTINGS_PATH):
            try:
                with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                logger.error("LotsOff: не удалось прочитать настройки, использую значения по умолчанию.")
                logger.debug("TRACEBACK", exc_info=True)
                data = {}
        settings = json.loads(json.dumps(DEFAULT_SETTINGS))
        if isinstance(data, dict):
            settings.update({k: v for k, v in data.items() if k in settings})
        if not isinstance(settings["managed"], dict):
            settings["managed"] = {}
        settings["watchdog"] = bool(settings["watchdog"])
        try:
            settings["interval"] = _clamp(int(settings["interval"] or 60), *INTERVAL_RANGE)
        except (TypeError, ValueError):
            settings["interval"] = DEFAULT_SETTINGS["interval"]
        favs = []
        if isinstance(settings.get("favorites"), list):
            for i in settings["favorites"]:
                try:
                    favs.append(int(i))
                except (TypeError, ValueError):
                    pass
        settings["favorites"] = favs
        return settings


def _save_settings(settings: dict) -> None:
    with _settings_lock:
        try:
            os.makedirs(PLUGIN_DIR, exist_ok=True)
            with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
                json.dump(settings, f, ensure_ascii=False, indent=2)
        except Exception:
            logger.error("LotsOff: не удалось сохранить настройки.")
            logger.debug("TRACEBACK", exc_info=True)


def _short(text: str, limit: int = 40) -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _escape(text) -> str:
    if tg_utils:
        return tg_utils.escape(str(text))
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _plugin_enabled(crd: "Cardinal") -> bool:
    try:
        return crd.plugins[UUID].enabled
    except Exception:
        return False  # плагин удален/не зарегистрирован — ничего не блокируем


def _sub_name(crd: "Cardinal", sub_id: int) -> str:
    """Возвращает название подкатегории («Название Игра»), если возможно."""
    try:
        sc = crd.account.get_subcategory(fp_enums.SubCategoryTypes.COMMON, int(sub_id))
        if sc:
            return sc.fullname
    except Exception:
        pass
    return _load_settings()["managed"].get(str(sub_id), f"ID {sub_id}")


def _cats_with_common(crd: "Cardinal") -> list[tuple]:
    """
    [(Category, [SubCategory(COMMON), ...]), ...] — только категории с обычными лотами.
    Избранные категории идут первыми (в порядке добавления в избранное), затем остальные.
    """
    favorites = _load_settings()["favorites"]
    result = []
    for cat in crd.account.categories:
        subs = [s for s in cat.get_subcategories() if s.is_common]
        if subs:
            result.append((cat, subs))
    favs = sorted((c for c in result if c[0].id in favorites),
                  key=lambda x: favorites.index(x[0].id))
    rest = [c for c in result if c[0].id not in favorites]
    return favs + rest


def _notify(crd: "Cardinal", text: str, notification_type: str | None = None) -> None:
    if not crd.telegram:
        return
    kwargs = {}
    if notification_type and tg_utils:
        kwargs["notification_type"] = notification_type
    try:
        crd.telegram.send_notification(text, **kwargs)
    except Exception:
        logger.debug("TRACEBACK", exc_info=True)


def _ntype(disable: bool) -> str | None:
    if not tg_utils:
        return None
    return tg_utils.NotificationTypes.lots_deactivate if disable else tg_utils.NotificationTypes.lots_restore


def _send_completion(crd: "Cardinal", text: str, disable: bool, chat_id: int | None = None) -> None:
    """
    Доставляет сообщение о завершении операции:
    • в чат, откуда запущена операция, — напрямую (если рассылка уведомлений его не покроет,
      чтобы не было дублей);
    • во все чаты уведомлений — через стандартную рассылку Кардинала
      (тип уведомления «Деактивация лотов» / «Восстановление лотов»).
    """
    tg = crd.telegram
    if tg is None:
        return
    ntype = _ntype(disable)

    covered = False  # доставит ли рассылка сообщение в запрашивающий чат
    if chat_id is not None and ntype:
        try:
            covered = bool(tg.is_notification_enabled(chat_id, ntype))
        except Exception:
            covered = False

    if chat_id is not None and not covered:
        try:
            tg.bot.send_message(chat_id, text, parse_mode="HTML")
        except Exception:
            logger.error("LotsOff: не удалось отправить сообщение о завершении операции в чат.")
            logger.debug("TRACEBACK", exc_info=True)

    try:
        kwargs = {"notification_type": ntype} if ntype else {}
        tg.send_notification(text, **kwargs)
    except Exception:
        logger.debug("TRACEBACK", exc_info=True)


# ═════════════════════════ РАБОТА СОСТОЯНИЯМИ ЛОТОВ ═════════════════════════════
def _set_lots_state(crd: "Cardinal", sub_ids: list[int], disable: bool,
                    chat_id: int | None = None, title: str | None = None) -> None:
    """
    Массово отключает (disable=True) или включает (disable=False) лоты переданных подкатегорий.
    Вызывается в отдельном потоке.

    По завершении отправляет сообщение «Категория ... выключена/включена»:
    :param chat_id: чат, откуда запущена операция (туда сообщение придет в любом случае).
    :param title: название категории для заголовка сообщения (если операция для всей категории).
    """
    names, total, changed, errors = [], 0, 0, 0
    with _op_lock:
        for sid in sub_ids:
            if _stop_event.is_set():
                return
            name = _sub_name(crd, sid)
            names.append(name)
            try:
                lots = crd.account.get_my_subcategory_lots(int(sid))
            except Exception:
                logger.error(f"LotsOff: не удалось получить список лотов подкатегории «{name}» (ID {sid}).")
                logger.debug("TRACEBACK", exc_info=True)
                errors += 1
                continue
            for lot in lots:
                total += 1
                # нужное состояние: active == (not disable). Если лот уже в нужном состоянии — пропускаем.
                if lot.active != disable:
                    continue
                try:
                    fields = crd.account.get_lot_fields(int(lot.id))
                    fields.active = not disable
                    crd.account.save_lot(fields)
                    changed += 1
                    logger.info(f"LotsOff: {'деактивировал' if disable else 'активировал'} лот "
                                f"{lot.id} — {lot.description or name}.")
                    time.sleep(SAVE_DELAY)
                except Exception:
                    errors += 1
                    logger.error(f"LotsOff: не удалось изменить состояние лота {lot.id} ({name}).")
                    logger.debug("TRACEBACK", exc_info=True)
                    time.sleep(0.5)
            time.sleep(0.4)

    already = total - changed - errors
    if title or len(names) == 1:
        head_name = title or (names[0] if names else "")
        text = (f"{'🔴' if disable else '🟢'} <b>Категория «{_escape(head_name)}» "
                f"{'выключена' if disable else 'включена'}.</b>\n\n")
    else:
        cats_text = "\n".join(_escape(i) for i in names)
        text = (f"{'🔴' if disable else '🟢'} <b>LotsOff: категории "
                f"{'выключены' if disable else 'включены'}.</b>\n\n"
                f"<b>Категории:</b>\n<code>{cats_text}</code>\n\n")
    text += (f"Всего лотов: <b>{total}</b>\n"
             f"{'Выключено' if disable else 'Включено'}: <b>{changed}</b>\n"
             f"Уже были {'выключены' if disable else 'включены'}: <b>{already}</b>\n"
             f"Ошибок: <b>{errors}</b>")
    if disable:
        text += "\n\n🛡 Хранитель будет удерживать эти лоты выключенными."
    _send_completion(crd, text, disable, chat_id)


def _enforce_once(crd: "Cardinal") -> None:
    """Хранитель: один проход — снова отключает лоты, которые включились в управляемых категориях."""
    settings = _load_settings()
    if not settings["watchdog"] or not settings["managed"]:
        return
    reactivated_total = 0
    names = []
    for sid_str, name in list(settings["managed"].items()):
        if _stop_event.is_set():
            return
        try:
            lots = crd.account.get_my_subcategory_lots(int(sid_str))
        except Exception:
            logger.debug(f"LotsOff (хранитель): не удалось проверить подкатегорию ID {sid_str}.")
            logger.debug("TRACEBACK", exc_info=True)
            continue
        for lot in lots:
            if not lot.active:
                continue
            try:
                fields = crd.account.get_lot_fields(int(lot.id))
                fields.active = False
                crd.account.save_lot(fields)
                reactivated_total += 1
                if name not in names:
                    names.append(name)
                logger.info(f"LotsOff (хранитель): деактивировал лот {lot.id} — {lot.description or name}.")
                time.sleep(SAVE_DELAY)
            except Exception:
                logger.error(f"LotsOff (хранитель): не удалось деактивировать лот {lot.id}.")
                logger.debug("TRACEBACK", exc_info=True)
        time.sleep(0.4)

    if reactivated_total:
        cats_text = "\n".join(_escape(i) for i in names)
        text = (f"🛡 <b>LotsOff (хранитель): снова отключил лоты.</b>\n\n"
                f"<b>Категории:</b>\n<code>{cats_text}</code>\n\n"
                f"Кто-то (или автовосстановление) включил <b>{reactivated_total}</b> лот(ов) "
                f"в отключенных категориях — хранитель вернул их в выключенное состояние.")
        _notify(crd, text, _ntype(True))


def _watchdog_loop(crd: "Cardinal") -> None:
    logger.info("LotsOff: хранитель запущен.")
    last_run = 0.0
    while not _stop_event.wait(5):
        try:
            settings = _load_settings()
            if not settings["watchdog"] or not settings["managed"] or not _plugin_enabled(crd):
                last_run = time.time()
                continue
            if not crd.account.is_initiated:
                continue
            if time.time() - last_run < settings["interval"]:
                continue
            last_run = time.time()
            with _op_lock:
                _enforce_once(crd)
        except Exception:
            logger.error("LotsOff (хранитель): произошла ошибка в цикле проверки.")
            logger.debug("TRACEBACK", exc_info=True)


# ═══════════════ ПЕРЕХВАТ АВТОВОССТАНОВЛЕНИЯ КАРДИНАЛА (autoRestore) ════════════
def _patch_autorestore() -> None:
    """
    Оборачивает handlers.update_lot_state, чтобы Кардинал не восстанавливал (не включал)
    лоты из категорий, отключенных этим плагином.
    """
    global _orig_update_lot_state, _patched_wrapper
    try:
        import handlers
        if getattr(handlers, "_lso_patched", False):
            return
        _orig_update_lot_state = handlers.update_lot_state

        def _patched_update_lot_state(cardinal, lot, task):
            try:
                if task == 1 and _plugin_enabled(cardinal):
                    sub_id = getattr(getattr(lot, "subcategory", None), "id", None)
                    if sub_id is not None and str(sub_id) in _load_settings()["managed"]:
                        logger.info(f"LotsOff: пропустил автовосстановление лота {lot.id} "
                                    f"(подкатегория отключена плагином).")
                        return True
            except Exception:
                logger.debug("TRACEBACK", exc_info=True)
            return _orig_update_lot_state(cardinal, lot, task)

        _patched_wrapper = _patched_update_lot_state
        handlers.update_lot_state = _patched_update_lot_state
        handlers._lso_patched = True
        logger.info("LotsOff: автовосстановление Кардинала взято под контроль плагина.")
    except Exception:
        logger.warning("LotsOff: не удалось перехватить автовосстановление — "
                       "хранитель будет отключать лоты по таймеру.")
        logger.debug("TRACEBACK", exc_info=True)


def _unpatch_autorestore() -> None:
    try:
        import handlers
        # снимаем патч, только если сверху никто не обернул (например, другой плагин)
        if getattr(handlers, "_lso_patched", False) and _orig_update_lot_state is not None and \
                (_patched_wrapper is None or handlers.update_lot_state is _patched_wrapper):
            handlers.update_lot_state = _orig_update_lot_state
            handlers._lso_patched = False
    except Exception:
        logger.debug("TRACEBACK", exc_info=True)


# ══════════════════════════ TELEGRAM-ИНТЕРФЕЙС ══════════════════════════════════
def _home_text(crd: "Cardinal") -> str:
    settings = _load_settings()
    wd = "ВКЛ 🟢" if settings["watchdog"] else "ВЫКЛ 🔴"
    text = (f"🔌 <b>{_escape(NAME)}</b> <i>v{VERSION}</i>\n\n"
            f"Массовое отключение и включение лотов по категориям/подкатегориям FunPay.\n\n"
            f"📋 Отключенных подкатегорий: <b>{len(settings['managed'])}</b>\n"
            f"🛡 Хранитель: <b>{wd}</b>, интервал <b>{settings['interval']}</b> сек.\n\n"
            f"<i>Хранитель перехватывает автовосстановление Кардинала и периодически проверяет, "
            f"что лоты в отключенных категориях остаются выключенными.</i>")
    try:
        if crd.autorestore_enabled and not settings["watchdog"]:
            text += ("\n\n⚠️ <b>У Кардинала включено автовосстановление лотов, а хранитель выключен.</b> "
                     "Отключенные лоты могут быть снова включены. Рекомендую включить хранителя.")
    except Exception:
        pass
    return text


def _home_kb() -> K:
    settings = _load_settings()
    kb = K(row_width=2)
    kb.add(B("🎮 Категории", None, f"{CB_CATS}:0"))
    kb.add(B(f"📋 Отключенные ({len(settings['managed'])})", None, f"{CB_MANAGED}:0"))
    kb.add(B(f"🛡 Хранитель: {'ВКЛ' if settings['watchdog'] else 'ВЫКЛ'}", None, CB_WD))
    kb.add(B(f"⏱ Интервал: {settings['interval']}с", None, CB_INT))
    kb.add(B("🟢 Включить ВСЕ лоты", None, CB_ASK_ALLON))
    if CBT:
        kb.add(B("🔙 Назад к плагину", None, f"{CBT.EDIT_PLUGIN}:{UUID}:0"))
    return kb


def _pagination(kb: K, offset: int, total_items: int, cb_builder: Callable[[int], str]) -> K:
    """Добавляет строку пагинации (⬅️ / страница / ➡️), если страниц больше одной."""
    pages = max(1, -(-total_items // PER_PAGE))
    if pages > 1:
        row = []
        if offset > 0:
            row.append(B("⬅️", None, cb_builder(offset - PER_PAGE)))
        row.append(B(f"{offset // PER_PAGE + 1}/{pages}", None, CB_NOOP))
        if offset + PER_PAGE < total_items:
            row.append(B("➡️", None, cb_builder(offset + PER_PAGE)))
        kb.row(*row)
    return kb


def _cats_kb(crd: "Cardinal", offset: int) -> tuple[str, K]:
    cats = _cats_with_common(crd)
    if not cats:
        kb = K().add(B("🏠 Главное меню", None, CB_HOME))
        return ("🎮 <b>Категории</b>\n\nДанные аккаунта еще не загружены или на аккаунте нет категорий "
                "с обычными лотами.\n\nОбновите профиль Кардинала и попробуйте позже."), kb
    settings = _load_settings()
    managed, favorites = settings["managed"], settings["favorites"]
    page = cats[offset:offset + PER_PAGE]
    text = ("🎮 <b>Категории</b> (лотовые)\n\nВыберите категорию, чтобы отключить/включить лоты "
            "всех ее подкатегорий:\n\n🟢 — все включено, 🟡 — частично отключено, 🔴 — все отключено\n"
            "★ — избранное (всегда в начале списка)")
    kb = K(row_width=1)
    for cat, subs in page:
        off_count = sum(1 for s in subs if str(s.id) in managed)
        mark = "🔴" if off_count == len(subs) else ("🟡" if off_count else "🟢")
        star = "★ " if cat.id in favorites else ""
        kb.add(B(f"{star}{mark} {_short(cat.name, 42)} ({off_count}/{len(subs)})", None,
                 f"{CB_CAT}:{cat.id}:0"))
    _pagination(kb, offset, len(cats), lambda off: f"{CB_CATS}:{off}")
    kb.add(B("🏠 Главное меню", None, CB_HOME))
    return text, kb


def _cat_kb(crd: "Cardinal", cat_id: int, offset: int) -> tuple[str, K] | None:
    settings = _load_settings()
    managed = settings["managed"]
    cat = None
    for c_ in crd.account.categories:
        if c_.id == cat_id:
            cat = c_
            break
    if cat is None:
        return None
    subs = [s for s in cat.get_subcategories() if s.is_common]
    if not subs:
        return None
    off_count = sum(1 for s in subs if str(s.id) in managed)
    is_fav = cat_id in settings["favorites"]
    text = (f"{'⭐' if is_fav else '🎮'} <b>{_escape(cat.name)}</b>"
            f"{'  <i>(в избранном)</i>' if is_fav else ''}\n\n"
            f"Подкатегорий: <b>{len(subs)}</b> | Отключено: <b>{off_count}</b>\n\n"
            f"🔴 — лоты держатся отключенными, 🟢 — лоты включены:")
    kb = K(row_width=1)
    kb.add(B("☆ Убрать из избранного" if is_fav else "⭐ В избранное (в начало списка)",
             None, f"{CB_FAV}:{cat_id}:{offset}"))
    if off_count == len(subs):
        kb.add(B(f"🟢 Включить всю категорию ({len(subs)} подкат.)", None, f"{CB_TCAT}:{cat_id}:{offset}"))
    else:
        kb.add(B(f"🔴 Отключить всю категорию ({len(subs)} подкат.)", None, f"{CB_TCAT}:{cat_id}:{offset}"))
    page = subs[offset:offset + PER_PAGE]
    for sc in page:
        mark = "🔴" if str(sc.id) in managed else "🟢"
        kb.add(B(f"{mark} {_short(sc.name, 45)}", None, f"{CB_TSUB}:{sc.id}:{cat_id}:{offset}"))
    _pagination(kb, offset, len(subs), lambda off: f"{CB_CAT}:{cat_id}:{off}")
    kb.row(B("🔙 Категории", None, f"{CB_CATS}:0"), B("🏠 Главное меню", None, CB_HOME))
    return text, kb


def _managed_kb(crd: "Cardinal", offset: int) -> tuple[str, K]:
    settings = _load_settings()
    items = list(settings["managed"].items())
    if not items:
        return ("📋 <b>Отключенные категории</b>\n\nСписок пуст — сейчас плагин не удерживает "
                "ни одну категорию выключенной.\n\nОтключите категорию через 🎮 «Категории».",
                K().add(B("🏠 Главное меню", None, CB_HOME)))
    text = ("📋 <b>Отключенные категории</b>\n\nЛоты этих подкатегорий удерживаются выключенными.\n"
            "Нажмите на подкатегорию, чтобы <b>включить</b> ее лоты и убрать из списка:")
    kb = K(row_width=1)
    page = items[offset:offset + PER_PAGE]
    for sid, name in page:
        kb.add(B(f"🟢 {_short(name, 50)}", None, f"{CB_UNM}:{sid}:{offset}"))
    _pagination(kb, offset, len(items), lambda off: f"{CB_MANAGED}:{off}")
    kb.add(B("🏠 Главное меню", None, CB_HOME))
    return text, kb


def _answer(bot, c: CallbackQuery, text: str | None = None, alert: bool = False) -> None:
    try:
        bot.answer_callback_query(c.id, text, show_alert=alert)
    except Exception:
        pass


def _edit_or_send(bot, c: CallbackQuery, text: str, kb: K | None) -> None:
    try:
        bot.edit_message_text(text, c.message.chat.id, c.message.id, parse_mode="HTML", reply_markup=kb)
    except ApiTelegramException as e:
        if "message is not modified" not in str(e):
            try:
                bot.send_message(c.message.chat.id, text, parse_mode="HTML", reply_markup=kb)
            except Exception:
                logger.debug("TRACEBACK", exc_info=True)
    except Exception:
        try:
            bot.send_message(c.message.chat.id, text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            logger.debug("TRACEBACK", exc_info=True)


def init(crd: "Cardinal", *args) -> None:
    """Инициализация плагина (BIND_TO_PRE_INIT): регистрация хэндлеров + запуск хранителя."""
    global _cardinal
    _cardinal = crd
    _load_settings()  # читаем/создаем файл настроек
    _patch_autorestore()

    tg = crd.telegram
    if tg is not None:
        bot = tg.bot

        # ---------- главное меню ----------
        def open_home(c: CallbackQuery) -> None:
            _answer(bot, c)
            _edit_or_send(bot, c, _home_text(crd), _home_kb())

        # ---------- категории ----------
        def open_cats(c: CallbackQuery) -> None:
            _answer(bot, c)
            offset = int(c.data.split(":")[2])
            text, kb = _cats_kb(crd, offset)
            _edit_or_send(bot, c, text, kb)

        def open_cat(c: CallbackQuery) -> None:
            _answer(bot, c)
            parts = c.data.split(":")
            cat_id, offset = int(parts[2]), int(parts[3])
            result = _cat_kb(crd, cat_id, offset)
            if result is None:
                result = _cats_kb(crd, 0)
            text, kb = result
            _edit_or_send(bot, c, text, kb)

        # ---------- переключение ----------
        def _refresh_cat_page(c: CallbackQuery, cat_id: int | None, offset: int) -> None:
            """Сразу перерисовывает страницу категории, чтобы статусы 🔴/🟢 обновились без перехода по страницам."""
            if cat_id is None:
                return
            result = _cat_kb(crd, cat_id, offset)
            if result is None:
                result = _cats_kb(crd, 0)
            text, kb = result
            _edit_or_send(bot, c, text, kb)

        def toggle_sub(c: CallbackQuery) -> None:
            parts = c.data.split(":")
            sub_id = int(parts[2])
            cat_id = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else None
            offset = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 0
            settings = _load_settings()
            disable = str(sub_id) not in settings["managed"]
            name = _sub_name(crd, sub_id)
            if disable:
                settings["managed"][str(sub_id)] = name
            else:
                settings["managed"].pop(str(sub_id), None)
            _save_settings(settings)
            _answer(bot, c, f"⏳ Запустил {'ОТКЛЮЧЕНИЕ' if disable else 'ВКЛЮЧЕНИЕ'} лотов:\n"
                            f"{name}\n\nКогда закончу — напишу сообщение в этот чат.", alert=True)
            threading.Thread(target=_set_lots_state, args=(crd, [sub_id], disable),
                             kwargs={"chat_id": c.message.chat.id}, daemon=True).start()
            _refresh_cat_page(c, cat_id, offset)

        def toggle_cat(c: CallbackQuery) -> None:
            parts = c.data.split(":")
            cat_id = int(parts[2])
            offset = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
            cat = None
            for cat_ in crd.account.categories:
                if cat_.id == cat_id:
                    cat = cat_
                    break
            if cat is None:
                _answer(bot, c, "Категория не найдена. Обновите меню.", alert=True)
                return
            subs = [s for s in cat.get_subcategories() if s.is_common]
            if not subs:
                _answer(bot, c, "В категории нет подкатегорий с лотами.", alert=True)
                return
            settings = _load_settings()
            all_off = all(str(s.id) in settings["managed"] for s in subs)
            sub_ids = [s.id for s in subs]
            if all_off:  # включаем всю категорию
                for s in subs:
                    settings["managed"].pop(str(s.id), None)
                disable = False
            else:  # отключаем всю категорию
                for s in subs:
                    settings["managed"][str(s.id)] = s.fullname
                disable = True
            _save_settings(settings)
            _answer(bot, c, f"⏳ Запустил {'ОТКЛЮЧЕНИЕ' if disable else 'ВКЛЮЧЕНИЕ'} лотов категории "
                            f"«{cat.name}» ({len(sub_ids)} подкатегорий).\n\n"
                            f"Когда закончу — напишу сообщение в этот чат.",
                    alert=True)
            threading.Thread(target=_set_lots_state, args=(crd, sub_ids, disable),
                             kwargs={"chat_id": c.message.chat.id, "title": cat.name}, daemon=True).start()
            _refresh_cat_page(c, cat_id, offset)

        def toggle_fav(c: CallbackQuery) -> None:
            parts = c.data.split(":")
            cat_id = int(parts[2])
            offset = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
            settings = _load_settings()
            if cat_id in settings["favorites"]:
                settings["favorites"].remove(cat_id)
                _save_settings(settings)
                _answer(bot, c, "☆ Категория убрана из избранного.")
            else:
                settings["favorites"].append(cat_id)
                _save_settings(settings)
                _answer(bot, c, "⭐ Категория добавлена в избранное — теперь она первая в списке «🎮 Категории».")
            _refresh_cat_page(c, cat_id, offset)

        # ---------- список отключенных ----------
        def open_managed(c: CallbackQuery) -> None:
            _answer(bot, c)
            offset = int(c.data.split(":")[2])
            text, kb = _managed_kb(crd, offset)
            _edit_or_send(bot, c, text, kb)

        def unmanage(c: CallbackQuery) -> None:
            parts = c.data.split(":")
            sid, offset = int(parts[2]), int(parts[3])
            settings = _load_settings()
            if str(sid) not in settings["managed"]:
                _answer(bot, c, "Эта категория уже не в списке.", alert=True)
            else:
                settings["managed"].pop(str(sid), None)
                _save_settings(settings)
                _answer(bot, c, f"⏳ Включаю лоты:\n{_sub_name(crd, sid)}\n\n"
                                f"Когда закончу — напишу сообщение в этот чат.",
                        alert=True)
                threading.Thread(target=_set_lots_state, args=(crd, [sid], False),
                                 kwargs={"chat_id": c.message.chat.id}, daemon=True).start()
            text, kb = _managed_kb(crd, offset)
            _edit_or_send(bot, c, text, kb)

        # ---------- включить всё ----------
        def ask_all_on(c: CallbackQuery) -> None:
            _answer(bot, c)
            kb = K(row_width=2)
            kb.add(B("✅ Да, включить все", None, CB_ALLON), B("❌ Отмена", None, CB_HOME))
            _edit_or_send(bot, c, "🟢 <b>Включить ВСЕ лоты?</b>\n\nПлагин включит лоты во всех отключенных "
                                  "категориях и очистит список. Это может занять время.", kb)

        def all_on(c: CallbackQuery) -> None:
            settings = _load_settings()
            sub_ids = [int(i) for i in settings["managed"]]
            settings["managed"] = {}
            _save_settings(settings)
            _answer(bot, c, f"⏳ Включаю лоты в {len(sub_ids)} подкатегориях...\n\n"
                            f"Когда закончу — напишу сообщение в этот чат.", alert=True)
            if sub_ids:
                threading.Thread(target=_set_lots_state, args=(crd, sub_ids, False),
                                 kwargs={"chat_id": c.message.chat.id}, daemon=True).start()
            else:
                _edit_or_send(bot, c, _home_text(crd), _home_kb())

        # ---------- хранитель ----------
        def toggle_watchdog(c: CallbackQuery) -> None:
            settings = _load_settings()
            settings["watchdog"] = not settings["watchdog"]
            _save_settings(settings)
            state = "ВКЛЮЧЕН 🟢" if settings["watchdog"] else "ВЫКЛЮЧЕН 🔴"
            _answer(bot, c, f"🛡 Хранитель: {state}")
            _edit_or_send(bot, c, _home_text(crd), _home_kb())

        # ---------- интервал ----------
        def act_edit_interval(c: CallbackQuery) -> None:
            settings = _load_settings()
            result = bot.send_message(
                c.message.chat.id,
                f"⏱ Введите интервал проверки хранителя в секундах "
                f"(<code>{INTERVAL_RANGE[0]}</code> - <code>{INTERVAL_RANGE[1]}</code>).\n\n"
                f"Текущее значение: <b>{settings['interval']}</b> сек.",
                parse_mode="HTML", reply_markup=skb.CLEAR_STATE_BTN() if skb else None)
            tg.set_state(c.message.chat.id, result.id, c.from_user.id, STATE_INT)
            _answer(bot, c)

        def edit_interval(m: Message) -> None:
            tg.clear_state(m.chat.id, m.from_user.id, True)
            try:
                interval = int(str(m.text).strip())
            except (ValueError, AttributeError):
                bot.reply_to(m, "❌ Нужно ввести число. Попробуйте еще раз: /lotsoff → ⏱ Интервал.")
                return
            interval = _clamp(interval, *INTERVAL_RANGE)
            settings = _load_settings()
            settings["interval"] = interval
            _save_settings(settings)
            bot.reply_to(m, f"✅ Интервал проверки хранителя: <b>{interval}</b> сек.", parse_mode="HTML")

        # ---------- команда ----------
        def cmd_open(m: Message) -> None:
            bot.send_message(m.chat.id, _home_text(crd), parse_mode="HTML", reply_markup=_home_kb())

        # ---------- noop (индикатор страницы) ----------
        def noop(c: CallbackQuery) -> None:
            _answer(bot, c)

        # ---------- регистрация хэндлеров ----------
        tg.cbq_handler(open_home, lambda c: c.data.startswith(f"{CBT.PLUGIN_SETTINGS}:{UUID}"))
        tg.cbq_handler(open_home, lambda c: c.data == CB_HOME)
        tg.cbq_handler(open_cats, lambda c: c.data.startswith(f"{CB_CATS}:"))
        tg.cbq_handler(open_cat, lambda c: c.data.startswith(f"{CB_CAT}:"))
        tg.cbq_handler(toggle_sub, lambda c: c.data.startswith(f"{CB_TSUB}:"))
        tg.cbq_handler(toggle_cat, lambda c: c.data.startswith(f"{CB_TCAT}:"))
        tg.cbq_handler(toggle_fav, lambda c: c.data.startswith(f"{CB_FAV}:"))
        tg.cbq_handler(open_managed, lambda c: c.data.startswith(f"{CB_MANAGED}:"))
        tg.cbq_handler(unmanage, lambda c: c.data.startswith(f"{CB_UNM}:"))
        tg.cbq_handler(ask_all_on, lambda c: c.data == CB_ASK_ALLON)
        tg.cbq_handler(all_on, lambda c: c.data == CB_ALLON)
        tg.cbq_handler(toggle_watchdog, lambda c: c.data == CB_WD)
        tg.cbq_handler(act_edit_interval, lambda c: c.data == CB_INT)
        tg.cbq_handler(noop, lambda c: c.data == CB_NOOP)
        tg.msg_handler(edit_interval, func=lambda m: tg.check_state(m.chat.id, m.from_user.id, STATE_INT))
        tg.msg_handler(cmd_open, commands=["lotsoff"])

        try:
            crd.add_telegram_commands(UUID, [("lotsoff", "Открытие меню отключения лотов по категориям.", True)])
        except Exception:
            logger.debug("TRACEBACK", exc_info=True)

        logger.info(f"LotsOff v{VERSION}: Telegram-меню зарегистрировано (команда /lotsoff).")
    else:
        logger.info(f"LotsOff v{VERSION}: Telegram выключен — работает только хранитель.")

    threading.Thread(target=_watchdog_loop, args=(crd,), daemon=True).start()


def on_delete(crd: "Cardinal", c=None) -> None:
    """Вызывается при удалении плагина через ПУ (BIND_TO_DELETE)."""
    _stop_event.set()
    _unpatch_autorestore()


# ══════════════════════════ ПРИВЯЗКА К СОБЫТИЯМ КАРДИНАЛА ═══════════════════════
BIND_TO_PRE_INIT = [init]
BIND_TO_DELETE = on_delete
