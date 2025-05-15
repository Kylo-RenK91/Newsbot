import logging
import feedparser
import html
import httpx
import asyncio
import random
from bs4 import BeautifulSoup
from collections import deque
from urllib.parse import urlparse, urljoin
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, constants as telegram_constants
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from telegram.constants import ParseMode, ChatAction
from telegram.error import BadRequest, Forbidden
from telegram.helpers import escape_markdown
from datetime import datetime
from time import mktime

TELEGRAM_BOT_TOKEN = "8169062726:AAGM2oS3sJVD83PVS-Ol0lxGtQkI0h6PJto"

DEFAULT_RSS_URL = "https://lenta.ru/rss/news"
DEFAULT_ITEMS_PER_PAGE = 5
MAX_ARTICLE_LENGTH = 3800
MAX_RECENT_URLS_PER_USER = 200
FETCH_RSS_TIMEOUT = 20
FETCH_ARTICLE_TIMEOUT = 20
MAX_SAVED_ARTICLES = 25

PREDEFINED_SOURCES = {
    "lenta": {"name": "Lenta.ru", "url": "https://lenta.ru/rss/news"},
    "rbc": {"name": "РБК", "url": "https://rssexport.rbc.ru/rbcnews/news/30/full.rss"},
    "tass": {"name": "ТАСС", "url": "https://tass.ru/rss/v2.xml"},
    "kommersant": {"name": "Коммерсантъ", "url": "https://www.kommersant.ru/RSS/news.xml"},
    "rt_russian": {"name": "RT Russian", "url": "https://russian.rt.com/rss"},
}

USER_DATA_RSS_URL = "rss_url_v2"
USER_DATA_ITEMS_PER_PAGE = "items_per_page_v2"
USER_DATA_RECENTLY_SHOWN = "recently_shown_urls_v2"
USER_DATA_KEYWORD_FILTER = "keyword_filter_v2"
USER_DATA_SAVED_ARTICLES = "saved_articles_v2"

CHAT_DATA_CURRENT_NEWS_PAGE = "current_news_page_v5"
CHAT_DATA_FULL_NEWS_LIST = "full_news_list_v5"
CHAT_DATA_ARTICLES_ON_PAGE_CACHE = "articles_on_page_cache_v5"
CHAT_DATA_NEWS_LIST_MESSAGE_ID = "news_list_msg_id_v5"
CHAT_DATA_CURRENT_ARTICLE_MESSAGE_ID = "current_article_msg_id_v1"
CHAT_DATA_LAST_STATUS_MESSAGE_ID = "last_status_msg_id_v5"

CB_PREFIX_READ = "read_"
CB_PREFIX_PAGE = "page_"
CB_PREFIX_SETSRC = "setsrc_"
CB_PREFIX_SETTINGS_ACTION = "settings_action_"
CB_PREFIX_SETTINGS_INFO = "settings_info_"
CB_PREFIX_SAVE_ARTICLE = "saveart_"
CB_PREFIX_DELETE_SAVED = "del_saved_"
CB_PREFIX_SET_ITEMS = "setitems_"

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO
)
logger = logging.getLogger(__name__)

async def delete_message_if_exists(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id_key: str):
    message_id = context.chat_data.pop(message_id_key, None)
    if message_id:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
        except BadRequest as e:
            if "message to delete not found" not in str(e).lower():
                logger.warning(f"Не удалось удалить сообщение {message_id} (ключ {message_id_key}) в чате {chat_id}: {e}")
        except Exception as e:
            logger.warning(f"Общая ошибка при удалении сообщения {message_id} (ключ {message_id_key}) в чате {chat_id}: {e}")
    return None

async def delete_last_status_message(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    await delete_message_if_exists(context, chat_id, CHAT_DATA_LAST_STATUS_MESSAGE_ID)

async def send_status_message(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str, parse_mode=None) -> None:
    await delete_last_status_message(context, chat_id)
    try:
        status_msg = await context.bot.send_message(chat_id, text, disable_web_page_preview=True, parse_mode=parse_mode)
        context.chat_data[CHAT_DATA_LAST_STATUS_MESSAGE_ID] = status_msg.message_id
    except Exception as e:
        logger.error(f"Не удалось отправить статусное сообщение: {e}")

def get_user_setting(context: ContextTypes.DEFAULT_TYPE, key: str, default_value):
    return context.user_data.get(key, default_value)

def get_user_recently_shown(context: ContextTypes.DEFAULT_TYPE) -> deque:
    if USER_DATA_RECENTLY_SHOWN not in context.user_data:
        context.user_data[USER_DATA_RECENTLY_SHOWN] = deque(maxlen=MAX_RECENT_URLS_PER_USER)
    return context.user_data[USER_DATA_RECENTLY_SHOWN]

def get_saved_articles(context: ContextTypes.DEFAULT_TYPE) -> list:
    if USER_DATA_SAVED_ARTICLES not in context.user_data:
        context.user_data[USER_DATA_SAVED_ARTICLES] = []
    return context.user_data[USER_DATA_SAVED_ARTICLES]

async def fetch_rss_news(url: str, fetch_limit: int):
    logger.info(f"Запрос RSS с {url} (лимит {fetch_limit})...")
    loop = asyncio.get_event_loop()
    try:
        feed = await asyncio.wait_for(
            loop.run_in_executor(None, feedparser.parse, url),
            timeout=FETCH_RSS_TIMEOUT
        )
        if feed.bozo:
            logger.warning(f"RSS лента {url} может быть некорректной: {feed.bozo_exception}")

        news_items = []
        if feed.entries:
            for entry in feed.entries[:fetch_limit * 2]:
                title = entry.get("title", "Без заголовка")
                link = entry.get("link", "#")
                published_str = "Дата неизвестна"
                if entry.get("published_parsed"):
                    try:
                        published_str = datetime.fromtimestamp(mktime(entry.published_parsed)).strftime("%d.%m.%y %H:%M")
                    except Exception:
                        published_str = entry.get("published", "Дата неизвестна")
                elif entry.get("published"):
                    published_str = entry.get("published")

                news_items.append({"title": title, "link": link, "published": published_str})
            return news_items
        else:
            logger.warning(f"В RSS ленте {url} нет новостей.")
            return "🤷‍♂️ В этой RSS ленте сейчас нет новостей."
    except asyncio.TimeoutError:
        logger.error(f"Тайм-аут при запросе RSS с {url}")
        return f"⌛️ Тайм-аут при загрузке RSS: {urlparse(url).netloc}"
    except Exception as e:
        logger.error(f"Ошибка при обработке RSS ({url}): {e}", exc_info=True)
        return f"🛠 Не удалось обработать RSS: {e.__class__.__name__}"

async def fetch_article_content_and_image(url: str):
    logger.info(f"Загрузка контента статьи с {url}...")
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/96.0.4664.110 Safari/537.36'}
    article_text_parts = []
    image_url = None

    try:
        async with httpx.AsyncClient(timeout=FETCH_ARTICLE_TIMEOUT, follow_redirects=True, headers=headers) as client:
            response = await client.get(url)
            response.raise_for_status()

        soup = BeautifulSoup(response.text, 'html.parser')

        meta_image_selectors = [
            ('meta', {'property': 'og:image'}), ('meta', {'property': 'og:image:secure_url'}),
            ('meta', {'name': 'twitter:image'}), ('link', {'rel': 'image_src'})
        ]
        for tag, attrs in meta_image_selectors:
            meta_tag = soup.find(tag, attrs)
            if meta_tag and meta_tag.get('content'):
                image_url = urljoin(url, meta_tag['content'])
                break
            elif meta_tag and meta_tag.get('href'):
                image_url = urljoin(url, meta_tag['href'])
                break

        if not image_url:
            itemprop_image_tag = soup.find(itemprop="image")
            if itemprop_image_tag:
                if itemprop_image_tag.name == 'img' and itemprop_image_tag.get('src'):
                    image_url = urljoin(url, itemprop_image_tag['src'])
                elif itemprop_image_tag.get('content'):
                    image_url = urljoin(url, itemprop_image_tag['content'])

        article_body_selectors = [
            ('div', {'class': 'article__text'}), ('article', {}), ('div', {'itemprop': 'articleBody'}),
            ('div', {'class': 'entry-content'}), ('div', {'class': 'content-text'}), ('div', {'class': 'post-content'}),
            ('div', {'class': 'td-post-content'}), ('div', {'class': 'story-body__inner'}), ('div', {'class': 'js-article-body'}),
            ('main', {}), ('div', {'role': 'main'})
        ]
        article_body = None
        for tag, attrs in article_body_selectors:
            article_body = soup.find(tag, attrs)
            if article_body: break

        if article_body:
            if not image_url:
                img_tag = article_body.find('img')
                if img_tag and img_tag.get('src'):
                    src_content = img_tag.get('src', '').lower()
                    width_ok = not (img_tag.get('width', '100').isdigit() and int(img_tag['width']) < 150)
                    height_ok = not (img_tag.get('height', '100').isdigit() and int(img_tag['height']) < 150)
                    if not src_content.startswith('data:image') and not "gif" in src_content and width_ok and height_ok:
                         image_url = urljoin(url, src_content)

            unwanted_selectors = [
                'script', 'style', 'iframe', 'aside', 'noscript', 'button', 'form', 'nav', 'footer',
                '.read-also', '.subscribe-form', '.gallery', '.infobox', 'table', 'figure.image',
                '.related-materials', '.news-widget', '.commercial', '.advertisement', '.ads', '.social-share',
                '[class*="sidebar"]', '[id*="sidebar"]', '[class*="comment"]', '[id*="comment"]',
                '[class*="promo"]', '[class*="banner"]', '[class*="sticky"]', 'header',
                '.top-banner-container', '.recommended-reads', '.author-bio', '.tags-links'
            ]
            for selector in unwanted_selectors:
                for unwanted_element in article_body.select(selector):
                    unwanted_element.decompose()

            paragraphs = article_body.find_all(['p', 'div'], recursive=True)
            if not paragraphs:
                 direct_text = article_body.get_text(separator='\n', strip=True)
                 if direct_text: article_text_parts.append(direct_text)

            for el in paragraphs:
                if el.name == 'div' and el.find('p'): continue
                text_content = el.get_text(separator=' ', strip=True)
                if text_content and len(text_content) > 20 and not any(forbidden in text_content.lower() for forbidden in ["читайте также", "подписывайтесь", "реклама"]):
                    article_text_parts.append(text_content)
        else:
            all_paragraphs = soup.find_all('p')
            for p_idx, p in enumerate(all_paragraphs):
                if p_idx > 25: break
                paragraph_text = p.get_text(separator=' ', strip=True)
                if paragraph_text and len(paragraph_text) > 50:
                    article_text_parts.append(paragraph_text)

        full_text = "\n\n".join(article_text_parts).strip()
        if not full_text:
            meta_desc = soup.find('meta', attrs={'name': 'description'})
            if meta_desc and meta_desc.get('content'):
                full_text = meta_desc.get('content')
            else:
                return {"text": "⚠️ Не удалось извлечь текст статьи.", "image_url": image_url}

        return {"text": full_text, "image_url": image_url}

    except httpx.HTTPStatusError as e:
        return {"text": f"⚠️ Не удалось загрузить статью (ошибка {e.response.status_code}).", "image_url": None}
    except httpx.RequestError as e:
        return {"text": f"⚠️ Не удалось загрузить статью: Проблема с сетью ({e.__class__.__name__}).", "image_url": None}
    except Exception as e:
        return {"text": f"⚠️ Ошибка при обработке статьи: {e.__class__.__name__}.", "image_url": None}

async def build_and_send_news_page(context: ContextTypes.DEFAULT_TYPE, chat_id: int, page_num: int, message_id_to_edit: int = None):
    await delete_last_status_message(context, chat_id)

    full_news_list = context.chat_data.get(CHAT_DATA_FULL_NEWS_LIST, [])
    items_per_page = get_user_setting(context, USER_DATA_ITEMS_PER_PAGE, DEFAULT_ITEMS_PER_PAGE)

    if not full_news_list:
        await context.bot.send_message(chat_id, "ℹ️ Список новостей пуст. Попробуйте команду /news для обновления.")
        return

    start_index = page_num * items_per_page
    end_index = start_index + items_per_page
    news_on_page = full_news_list[start_index:end_index]

    if not news_on_page and page_num > 0:
        page_num = 0
        start_index = 0
        end_index = items_per_page
        news_on_page = full_news_list[start_index:end_index]

    if not news_on_page:
        await context.bot.send_message(chat_id, "ℹ️ На этой странице нет новостей.")
        return

    context.chat_data[CHAT_DATA_ARTICLES_ON_PAGE_CACHE] = {i: item for i, item in enumerate(news_on_page)}

    rss_url_in_use = get_user_setting(context, USER_DATA_RSS_URL, DEFAULT_RSS_URL)
    source_domain = urlparse(rss_url_in_use).netloc
    source_name_display = source_domain
    for key, src_data in PREDEFINED_SOURCES.items():
        if src_data["url"] == rss_url_in_use:
            source_name_display = src_data["name"]
            break

    active_filter = get_user_setting(context, USER_DATA_KEYWORD_FILTER, None)
    filter_info = f" (🔎: \"{html.escape(active_filter)}\")" if active_filter else ""

    total_pages = (len(full_news_list) + items_per_page - 1) // items_per_page
    message_text_parts = [f"📰 <b>Новости: {html.escape(source_name_display)}{filter_info}</b> (Стр. {page_num + 1}/{total_pages})\n"]

    keyboard_buttons = []
    for i, item in enumerate(news_on_page):
        title = html.escape(item['title'])
        published_date = html.escape(item.get('published', ''))
        date_str = f" <i>({published_date})</i>" if published_date and published_date != "Дата неизвестна" else "" # Use <i> for date

        message_text_parts.append(f"<b>{start_index + i + 1}.</b> <a href='{item['link']}'>{title}</a>{date_str}")

        row = [
            InlineKeyboardButton(f"📖 Читать #{start_index + i + 1}", callback_data=f"{CB_PREFIX_READ}{i}"),
            InlineKeyboardButton("💾", callback_data=f"{CB_PREFIX_SAVE_ARTICLE}{i}")
        ]
        keyboard_buttons.append(row)

    pagination_row = []
    if page_num > 0:
        pagination_row.append(InlineKeyboardButton("⬅️ Пред.", callback_data=f"{CB_PREFIX_PAGE}{page_num - 1}"))
    if end_index < len(full_news_list):
        pagination_row.append(InlineKeyboardButton("След. ➡️", callback_data=f"{CB_PREFIX_PAGE}{page_num + 1}"))

    if pagination_row:
        keyboard_buttons.append(pagination_row)

    reply_markup = InlineKeyboardMarkup(keyboard_buttons)
    final_message_text = "\n\n".join(message_text_parts)

    try:
        if not message_id_to_edit:
            await delete_message_if_exists(context, chat_id, CHAT_DATA_NEWS_LIST_MESSAGE_ID)

        if message_id_to_edit:
            sent_message = await context.bot.edit_message_text(
                chat_id=chat_id, message_id=message_id_to_edit, text=final_message_text,
                parse_mode=ParseMode.HTML, reply_markup=reply_markup, disable_web_page_preview=True
            )
        else:
            sent_message = await context.bot.send_message(
                chat_id, final_message_text, parse_mode=ParseMode.HTML,
                reply_markup=reply_markup, disable_web_page_preview=True
            )
        context.chat_data[CHAT_DATA_NEWS_LIST_MESSAGE_ID] = sent_message.message_id
        context.chat_data[CHAT_DATA_CURRENT_NEWS_PAGE] = page_num
    except BadRequest as e_br:
        if "Message to edit not found" in str(e_br) or "message can't be edited" in str(e_br).lower():
            await delete_message_if_exists(context, chat_id, CHAT_DATA_NEWS_LIST_MESSAGE_ID)
            sent_message = await context.bot.send_message(
                chat_id, final_message_text, parse_mode=ParseMode.HTML,
                reply_markup=reply_markup, disable_web_page_preview=True
            )
            context.chat_data[CHAT_DATA_NEWS_LIST_MESSAGE_ID] = sent_message.message_id
            context.chat_data[CHAT_DATA_CURRENT_NEWS_PAGE] = page_num
        elif "Message is not modified" not in str(e_br):
            await context.bot.send_message(chat_id, "⚠️ Произошла ошибка (BadRequest) при обновлении списка новостей.")
    except Exception as e:
        if chat_id:
            await context.bot.send_message(chat_id, "⚠️ Произошла неизвестная ошибка при обновлении списка новостей.")


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_name = update.effective_user.first_name
    await delete_message_if_exists(context, update.effective_chat.id, CHAT_DATA_NEWS_LIST_MESSAGE_ID)
    await delete_message_if_exists(context, update.effective_chat.id, CHAT_DATA_CURRENT_ARTICLE_MESSAGE_ID)

    welcome_text = (
        f"Привет, {html.escape(user_name)}! 👋\n\n"
        "Я твой персональный агрегатор новостей. Используй команды ниже, чтобы начать:\n\n"
        "📰 /news - Показать последние новости\n"
        "⚙️ /settings - Настроить бота\n"
        "📚 /saved - Показать сохраненные статьи\n"
        "📜 /sources - Выбрать источник новостей\n"
        "❓ /help - Получить помощь по командам"
    )
    await update.message.reply_text(welcome_text, parse_mode=ParseMode.HTML)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await delete_message_if_exists(context, update.effective_chat.id, CHAT_DATA_NEWS_LIST_MESSAGE_ID)
    await delete_message_if_exists(context, update.effective_chat.id, CHAT_DATA_CURRENT_ARTICLE_MESSAGE_ID)

    help_text = (
        "🤖 *Доступные команды:*\n\n"
        "🚀 *Основные:*\n"
        "/start \\- Перезапустить бота и увидеть приветствие\n"
        "/help \\- Показать это сообщение помощи\n"
        "/news \\- 📰 Загрузить и показать свежие новости\n\n"
        "🛠 *Настройки:*\n"
        "/settings \\- ⚙️ Открыть меню настроек\n"
        "/set\\_items\\_per\\_page `<число 1-10>` \\- 🔢 Установить кол\\-во новостей на странице\n\n"
        "🌐 *Источники новостей:*\n"
        "/sources \\- 📜 Показать список предустановленных RSS\\-источников для выбора\n"
        "/set\\_rss `<URL>` \\- 🔗 Установить свой URL для RSS\\-ленты\n\n"
        "🔍 *Фильтрация:*\n"
        "/filter `<ключевое слово>` \\- 🔎 Установить фильтр по ключевому слову в заголовках\n"
        "/clear\\_filter \\- 🗑 Сбросить текущий фильтр по ключевому слову\n\n"
        "💾 *Сохраненные статьи:*\n"
        "/saved \\- 📚 Показать список сохраненных статей\n\n"
        "🧹 *Прочее:*\n"
        "/clear\\_history \\- 🔄 Очистить историю просмотренных URL \\(новости станут \"новыми\"\\)\n"
    )
    await update.message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN_V2)

async def news_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    await delete_message_if_exists(context, chat_id, CHAT_DATA_CURRENT_ARTICLE_MESSAGE_ID)

    await send_status_message(context, chat_id, "🔄 Обновляю ленту новостей...")
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    rss_url = get_user_setting(context, USER_DATA_RSS_URL, DEFAULT_RSS_URL)
    items_per_page = get_user_setting(context, USER_DATA_ITEMS_PER_PAGE, DEFAULT_ITEMS_PER_PAGE)
    user_recently_shown = get_user_recently_shown(context)
    keyword_filter = get_user_setting(context, USER_DATA_KEYWORD_FILTER, None)

    fetch_limit_rss = MAX_RECENT_URLS_PER_USER + items_per_page * 5
    all_fetched_news_result = await fetch_rss_news(rss_url, fetch_limit_rss)

    await delete_last_status_message(context, chat_id)

    if isinstance(all_fetched_news_result, str):
        await update.message.reply_text(all_fetched_news_result)
        return

    if not all_fetched_news_result:
        await update.message.reply_text("😕 Не удалось получить данные из RSS ленты. Попробуйте позже или проверьте URL источника.")
        return

    filtered_news = []
    for item in all_fetched_news_result:
        is_new = item['link'] not in user_recently_shown
        matches_filter = not keyword_filter or keyword_filter.lower() in item['title'].lower()
        if is_new and matches_filter:
            filtered_news.append(item)

    if not filtered_news:
        source_domain = urlparse(rss_url).netloc
        msg = f"🤷‍♂️ Нет новых не показанных ранее новостей"
        if keyword_filter:
            msg += f" по фильтру «{html.escape(keyword_filter)}»"
        msg += f" с источника {html.escape(source_domain)}."

        suggestions = []
        if keyword_filter: suggestions.append("Попробуйте /clear_filter")
        suggestions.append("Обновите /news позже")
        suggestions.append("Смените /sources")
        suggestions.append("Очистите /clear_history (покажет все заново)")
        msg += "\n\n" + " или ".join(suggestions) + "."
        await update.message.reply_text(msg)
        return

    context.chat_data[CHAT_DATA_FULL_NEWS_LIST] = filtered_news

    for item in filtered_news:
        user_recently_shown.append(item['link'])

    await build_and_send_news_page(context, chat_id, page_num=0, message_id_to_edit=None)


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await delete_message_if_exists(context, update.effective_chat.id, CHAT_DATA_NEWS_LIST_MESSAGE_ID)
    await delete_message_if_exists(context, update.effective_chat.id, CHAT_DATA_CURRENT_ARTICLE_MESSAGE_ID)

    rss_url = get_user_setting(context, USER_DATA_RSS_URL, DEFAULT_RSS_URL)
    items_per_page = get_user_setting(context, USER_DATA_ITEMS_PER_PAGE, DEFAULT_ITEMS_PER_PAGE)
    keyword_filter = get_user_setting(context, USER_DATA_KEYWORD_FILTER, None)

    source_name = "Неизвестный источник"
    parsed_rss_url_display = urlparse(rss_url).netloc
    for key, src_data in PREDEFINED_SOURCES.items():
        if src_data["url"] == rss_url:
            source_name = src_data["name"]
            break
    if source_name == "Неизвестный источник" and parsed_rss_url_display:
        source_name = f"Свой URL: {parsed_rss_url_display}"

    settings_text_md = (
        f"⚙️ *Текущие настройки:*\n\n"
        f"🔗 *Источник RSS*: _{escape_markdown(source_name, version=2)}_\n"
        f"`{escape_markdown(rss_url, version=2)}`\n"
        f"🔢 *Новостей на странице*: *{items_per_page}*\n"
        f"🔎 *Фильтр по слову*: _{escape_markdown(keyword_filter, version=2) if keyword_filter else 'Не установлен'}_"
    )

    keyboard = [
        [
            InlineKeyboardButton("Изменить источник 📜", callback_data=f"{CB_PREFIX_SETTINGS_INFO}source"),
            InlineKeyboardButton("Изменить кол-во/стр 🔢", callback_data=f"{CB_PREFIX_SETTINGS_INFO}items_count")
        ],
        [
            InlineKeyboardButton("Задать фильтр 🔎", callback_data=f"{CB_PREFIX_SETTINGS_INFO}filter"),
        ]
    ]
    if keyword_filter:
        keyboard[-1].append(InlineKeyboardButton("Сбросить фильтр 🗑️", callback_data=f"{CB_PREFIX_SETTINGS_ACTION}clear_filter"))

    await update.message.reply_text(settings_text_md, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=InlineKeyboardMarkup(keyboard))

async def set_items_command(update: Update, context: ContextTypes.DEFAULT_TYPE, from_callback: bool = False, count_override: int = None) -> None:
    target_message = update.callback_query.message if from_callback else update.message
    chat_id = target_message.chat_id

    try:
        if count_override is not None:
            count = count_override
        else:
            count = int(context.args[0])
    except (IndexError, ValueError):
        await target_message.reply_text("❗️Неверный формат. Используйте: /set_items_per_page `<число>` (например, /set_items_per_page 5)")
        return

    if 1 <= count <= 10:
        context.user_data[USER_DATA_ITEMS_PER_PAGE] = count
        context.chat_data.pop(CHAT_DATA_CURRENT_NEWS_PAGE, None)
        context.chat_data.pop(CHAT_DATA_NEWS_LIST_MESSAGE_ID, None)
        context.chat_data.pop(CHAT_DATA_FULL_NEWS_LIST, None)
        context.chat_data.pop(CHAT_DATA_ARTICLES_ON_PAGE_CACHE, None)

        reply_text = f"✅ Количество новостей на странице установлено: {count}."
        if from_callback:
            try:
                await update.callback_query.edit_message_text(reply_text + "\n\nЧтобы увидеть изменения, вызовите /news.", reply_markup=None)
            except BadRequest:
                 await context.bot.send_message(chat_id, reply_text + "\n\nЧтобы увидеть изменения, вызовите /news.")
        else:
            await target_message.reply_text(reply_text)

    else:
        await target_message.reply_text("❗️Пожалуйста, выберите число от 1 до 10.")

async def set_rss_command(update: Update, context: ContextTypes.DEFAULT_TYPE, from_callback_data: str = None) -> None:
    target_message = update.callback_query.message if from_callback_data else update.message
    chat_id = target_message.chat_id
    new_rss_url = None

    if from_callback_data:
        source_key = from_callback_data.split(CB_PREFIX_SETSRC)[1]
        if source_key in PREDEFINED_SOURCES:
            new_rss_url = PREDEFINED_SOURCES[source_key]["url"]
            source_name = PREDEFINED_SOURCES[source_key]["name"]
        else:
            await update.callback_query.answer("❗️Неверный источник.", show_alert=True)
            return
    else:
        try:
            new_rss_url = context.args[0]
        except IndexError:
            await target_message.reply_text("❗️Неверный формат. Используйте: /set_rss `<URL>`")
            return
        source_name = urlparse(new_rss_url).netloc

    if not (new_rss_url.startswith("http://") or new_rss_url.startswith("https://")):
        await target_message.reply_text("❗️URL должен начинаться с http:// или https://.")
        return
    try:
        parsed = urlparse(new_rss_url)
        if not parsed.scheme or not parsed.netloc:
            raise ValueError("Некорректный URL")
    except ValueError:
        await target_message.reply_text("❗️Вы ввели некорректный URL. Пожалуйста, проверьте и попробуйте снова.")
        return

    context.user_data[USER_DATA_RSS_URL] = new_rss_url
    keys_to_pop = [CHAT_DATA_FULL_NEWS_LIST, CHAT_DATA_CURRENT_NEWS_PAGE,
                   CHAT_DATA_NEWS_LIST_MESSAGE_ID, CHAT_DATA_ARTICLES_ON_PAGE_CACHE,
                   CHAT_DATA_CURRENT_ARTICLE_MESSAGE_ID]
    for key in keys_to_pop:
        await delete_message_if_exists(context, chat_id, key)
        context.chat_data.pop(key, None)

    reply_text = f"✅ RSS источник обновлен: {html.escape(source_name)}\nИспользуйте /news для загрузки новостей."

    if from_callback_data:
        try:
            await update.callback_query.edit_message_text(reply_text, reply_markup=None)
        except BadRequest:
             await context.bot.send_message(chat_id, reply_text)
    else:
        await target_message.reply_text(reply_text)


async def sources_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await delete_message_if_exists(context, update.effective_chat.id, CHAT_DATA_NEWS_LIST_MESSAGE_ID)
    await delete_message_if_exists(context, update.effective_chat.id, CHAT_DATA_CURRENT_ARTICLE_MESSAGE_ID)

    keyboard = []
    for key, data in PREDEFINED_SOURCES.items():
        keyboard.append([InlineKeyboardButton(data["name"], callback_data=f"{CB_PREFIX_SETSRC}{key}")])
    keyboard.append([InlineKeyboardButton("📝 Указать свой URL", callback_data=f"{CB_PREFIX_SETTINGS_INFO}custom_rss")])

    await update.message.reply_text("📜 Выберите источник новостей из списка или укажите свой:", reply_markup=InlineKeyboardMarkup(keyboard))

async def filter_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("❗️Пожалуйста, укажите ключевое слово для фильтра. Пример: /filter экономика")
        return

    keyword = " ".join(context.args).strip()
    if len(keyword) < 2:
        await update.message.reply_text("❗️Ключевое слово должно быть не менее 2 символов.")
        return

    context.user_data[USER_DATA_KEYWORD_FILTER] = keyword
    context.chat_data.pop(CHAT_DATA_FULL_NEWS_LIST, None)
    context.chat_data.pop(CHAT_DATA_CURRENT_NEWS_PAGE, None)
    await delete_message_if_exists(context, update.effective_chat.id, CHAT_DATA_NEWS_LIST_MESSAGE_ID)

    await update.message.reply_text(f"✅ Фильтр по слову «{html.escape(keyword)}» успешно установлен. Новости будут отфильтрованы при следующем вызове /news.")

async def clear_filter_command(update: Update, context: ContextTypes.DEFAULT_TYPE, query_message=None) -> None:
    target_message_obj = query_message if query_message else update.message
    chat_id = target_message_obj.chat_id

    if USER_DATA_KEYWORD_FILTER in context.user_data:
        del context.user_data[USER_DATA_KEYWORD_FILTER]
        context.chat_data.pop(CHAT_DATA_FULL_NEWS_LIST, None)
        context.chat_data.pop(CHAT_DATA_CURRENT_NEWS_PAGE, None)
        await delete_message_if_exists(context, chat_id, CHAT_DATA_NEWS_LIST_MESSAGE_ID)

        reply_text = "✅ Фильтр по ключевому слову сброшен."
        if query_message:
            try: await target_message_obj.edit_text(reply_text + "\n\nНастройки обновлены. Используйте /settings для просмотра или /news.", reply_markup=None)
            except BadRequest: await context.bot.send_message(chat_id, reply_text)
        else: await target_message_obj.reply_text(reply_text)
    else:
        reply_text = "ℹ️ Фильтр по ключевому слову не был установлен."
        if query_message: await update.callback_query.answer(reply_text, show_alert=True)
        else: await target_message_obj.reply_text(reply_text)

async def clear_history_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if USER_DATA_RECENTLY_SHOWN in context.user_data and context.user_data[USER_DATA_RECENTLY_SHOWN]:
        context.user_data[USER_DATA_RECENTLY_SHOWN].clear()
        await update.message.reply_text("✅ История просмотренных URL очищена. При следующем запросе /news все новости будут считаться новыми.")
    else:
        await update.message.reply_text("ℹ️ История просмотренных URL уже пуста.")

async def tyz_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    percent = random.randint(0, 100)
    captions = {
        (0, 10): "Новость настолько не в теме ({percent}%), что её даже никто не читал. 0/10, просто беда.",
        (11, 30): "Ну такое, на троечку ({percent}%). Видали и получше. В общем, не очень.",
        (31, 50): "Серединка на половинку ({percent}%). И не ерунда, и не супер. Бывает, ну да.",
        (51, 70): "Уже что-то, проблески качества ({percent}%)! Не восхитительно, конечно, но сойдёт.",
        (71, 90): "Хороший такой уровень ({percent}%), годный! Можно и порадоваться этому.",
        (91, 99): "Почти максимальный класс ({percent}%)! Ничего себе, вот это удача!",
        (100, 100): "МАКСИМАЛЬНЫЙ УРОВЕНЬ ({percent}%)! НЕВЕРОЯТНО! ЭТО ШЕДЕВР, СУПЕР! ВСЕ СЮДА!"
    }
    caption_template = "Трудно сказать ({percent}%)."
    for (low, high), template in captions.items():
        if low <= percent <= high:
            caption_template = template
            break

    final_caption = caption_template.format(percent=percent)
    safe_caption = escape_markdown(final_caption, version=2)

    await update.message.reply_text(f"🎲 Ваш текущий процент \"тузовости\": *{percent}%*\n\n_{safe_caption}_", parse_mode=ParseMode.MARKDOWN_V2)


async def saved_articles_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await delete_message_if_exists(context, update.effective_chat.id, CHAT_DATA_NEWS_LIST_MESSAGE_ID)
    await delete_message_if_exists(context, update.effective_chat.id, CHAT_DATA_CURRENT_ARTICLE_MESSAGE_ID)

    saved_articles = get_saved_articles(context)
    if not saved_articles:
        await update.message.reply_text("📚 У вас пока нет сохраненных статей. Вы можете сохранить статью из списка новостей.")
        return

    message_text_parts = ["📚 *Ваши сохраненные статьи:*\n"]
    keyboard = []
    for i, article in enumerate(saved_articles):
        title = escape_markdown(article['title'], version=2)
        link = article['link']
        source_name = escape_markdown(article.get('source_name', 'Неизвестный источник'), version=2)
        message_text_parts.append(f"{i+1}\\. [{title}]({link}) \\- _{source_name}_")
        keyboard.append([InlineKeyboardButton(f"🗑️ Удалить #{i+1}", callback_data=f"{CB_PREFIX_DELETE_SAVED}{i}")])

    final_message_text = "\n".join(message_text_parts)
    try:
        await update.message.reply_text(final_message_text, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=InlineKeyboardMarkup(keyboard), disable_web_page_preview=True)
    except BadRequest as e:
        await update.message.reply_text("⚠️ Ошибка при отображении сохраненных статей. Возможно, список слишком длинный.")


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data
    chat_id = query.message.chat_id

    if data.startswith(CB_PREFIX_READ):
        await delete_message_if_exists(context, chat_id, CHAT_DATA_NEWS_LIST_MESSAGE_ID)

        try:
            page_article_index = int(data.split(CB_PREFIX_READ)[1])
            articles_on_page = context.chat_data.get(CHAT_DATA_ARTICLES_ON_PAGE_CACHE)

            if not articles_on_page or page_article_index not in articles_on_page:
                await send_status_message(context, chat_id, "❗️Новость не найдена или список устарел. Пожалуйста, обновите /news.")
                return

            article_data_item = articles_on_page[page_article_index]
            article_url, article_title = article_data_item['link'], article_data_item['title']

            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            await send_status_message(context, chat_id, f"⏳ Загружаю статью «{html.escape(article_title[:50])}...»")

            content_data = await fetch_article_content_and_image(article_url)
            await delete_last_status_message(context, chat_id)

            article_text, image_url = content_data['text'], content_data['image_url']

            response_header = f"📖 <a href='{article_url}'><b>{html.escape(article_title)}</b></a>\n\n"
            final_text_content_escaped = html.escape(article_text)

            kb_layout = [[InlineKeyboardButton("⬅️ К списку новостей", callback_data="back_to_list")]]
            if article_url != "#":
                 kb_layout[0].append(InlineKeyboardButton("🔗 Оригинал", url=article_url))
            reply_markup = InlineKeyboardMarkup(kb_layout)

            full_article_message_body = response_header + final_text_content_escaped

            sent_message = None
            if image_url:
                try:
                    caption_text = response_header
                    if len(final_text_content_escaped) > telegram_constants.MessageLimit.MAX_TEXT_LENGTH:
                        chars_for_caption = telegram_constants.MessageLimit.CAPTION_LENGTH - len(response_header) - 20
                        if chars_for_caption > 100:
                            caption_text += final_text_content_escaped[:chars_for_caption] + "..."
                            remaining_text = final_text_content_escaped[chars_for_caption:]
                        else:
                            remaining_text = final_text_content_escaped
                    elif len(response_header + final_text_content_escaped) <= telegram_constants.MessageLimit.CAPTION_LENGTH:
                        caption_text = response_header + final_text_content_escaped
                        remaining_text = None
                    else:
                        remaining_text = final_text_content_escaped

                    if len(caption_text) > telegram_constants.MessageLimit.CAPTION_LENGTH:
                         caption_text = caption_text[:telegram_constants.MessageLimit.CAPTION_LENGTH - 3] + "..."

                    sent_message = await context.bot.send_photo(
                        chat_id=chat_id, photo=image_url, caption=caption_text,
                        parse_mode=ParseMode.HTML, reply_markup=(reply_markup if not remaining_text else None)
                    )
                    if remaining_text:
                        if len(remaining_text) > MAX_ARTICLE_LENGTH:
                            remaining_text = remaining_text[:MAX_ARTICLE_LENGTH - 3] + "..."
                        if len(remaining_text) > telegram_constants.MessageLimit.MAX_TEXT_LENGTH:
                             remaining_text = remaining_text[:telegram_constants.MessageLimit.MAX_TEXT_LENGTH -3] + "..."

                        if len(remaining_text.strip()) > 10:
                            await context.bot.send_message(chat_id, remaining_text, parse_mode=ParseMode.HTML, disable_web_page_preview=True, reply_markup=reply_markup)
                        else:
                            await context.bot.edit_message_reply_markup(chat_id=chat_id, message_id=sent_message.message_id, reply_markup=reply_markup)


                except Exception as e_img_send:
                    await context.bot.send_message(chat_id, "🖼 Не удалось загрузить изображение к статье. Показываю только текст.")
                    if len(full_article_message_body) > MAX_ARTICLE_LENGTH:
                        full_article_message_body = full_article_message_body[:MAX_ARTICLE_LENGTH - 3] + "..."
                    if len(full_article_message_body) > telegram_constants.MessageLimit.MAX_TEXT_LENGTH:
                        full_article_message_body = full_article_message_body[:telegram_constants.MessageLimit.MAX_TEXT_LENGTH -3] + "..."
                    sent_message = await context.bot.send_message(chat_id, full_article_message_body, parse_mode=ParseMode.HTML, disable_web_page_preview=True, reply_markup=reply_markup)
            else:
                if len(full_article_message_body) > MAX_ARTICLE_LENGTH:
                    full_article_message_body = full_article_message_body[:MAX_ARTICLE_LENGTH - 3] + "..."
                if len(full_article_message_body) > telegram_constants.MessageLimit.MAX_TEXT_LENGTH:
                    full_article_message_body = full_article_message_body[:telegram_constants.MessageLimit.MAX_TEXT_LENGTH -3] + "..."
                sent_message = await context.bot.send_message(chat_id, full_article_message_body, parse_mode=ParseMode.HTML, disable_web_page_preview=True, reply_markup=reply_markup)

            if sent_message:
                context.chat_data[CHAT_DATA_CURRENT_ARTICLE_MESSAGE_ID] = sent_message.message_id

        except (IndexError, ValueError, TypeError, KeyError) as e:
            await delete_last_status_message(context, chat_id)
            await query.message.reply_text("❗️Произошла ошибка при попытке прочитать новость. Пожалуйста, попробуйте /news снова.")

    elif data.startswith(CB_PREFIX_PAGE):
        try:
            page_num = int(data.split(CB_PREFIX_PAGE)[1])
            list_message_id = context.chat_data.get(CHAT_DATA_NEWS_LIST_MESSAGE_ID)
            if not list_message_id:
                 await query.message.reply_text("❗️Список новостей не найден для пагинации. Попробуйте /news.")
                 return
            await build_and_send_news_page(context, chat_id, page_num=page_num, message_id_to_edit=list_message_id)
        except (IndexError, ValueError):
            await query.message.reply_text("❗️Ошибка навигации по страницам. Пожалуйста, попробуйте /news снова.")

    elif data.startswith(CB_PREFIX_SETSRC):
        await set_rss_command(update, context, from_callback_data=data)

    elif data == "back_to_list":
        await delete_message_if_exists(context, chat_id, CHAT_DATA_CURRENT_ARTICLE_MESSAGE_ID)
        if query.message and query.message.message_id != context.chat_data.get(CHAT_DATA_CURRENT_ARTICLE_MESSAGE_ID):
            try: await query.message.delete()
            except Exception: pass

        current_page = context.chat_data.get(CHAT_DATA_CURRENT_NEWS_PAGE, 0)
        if context.chat_data.get(CHAT_DATA_FULL_NEWS_LIST):
            await build_and_send_news_page(context, chat_id, page_num=current_page, message_id_to_edit=None)
        else:
            await context.bot.send_message(chat_id, "ℹ️ Список новостей устарел или был очищен. Пожалуйста, используйте /news для загрузки свежих новостей.")

    elif data.startswith(CB_PREFIX_SETTINGS_INFO):
        info_key = data.split(CB_PREFIX_SETTINGS_INFO)[1]
        if info_key == "items_count":
            keyboard = [
                [InlineKeyboardButton(str(i), callback_data=f"{CB_PREFIX_SET_ITEMS}{i}") for i in range(1, 6)],
                [InlineKeyboardButton(str(i), callback_data=f"{CB_PREFIX_SET_ITEMS}{i}") for i in range(6, 11)],
                [InlineKeyboardButton("⬅️ Назад к настройкам", callback_data=f"{CB_PREFIX_SETTINGS_ACTION}back_to_main_settings")]
            ]
            await query.edit_message_text("🔢 Выберите количество новостей на странице:", reply_markup=InlineKeyboardMarkup(keyboard))
        elif info_key == "source":
            await query.answer()
            await sources_command(Update(update.update_id, message=query.message), context)
            try: await query.message.delete()
            except: pass
        elif info_key == "filter":
            await query.message.reply_text("⌨️ Чтобы установить фильтр, отправьте команду: /filter `<ваше слово>`\nНапример: /filter технологии")
        elif info_key == "custom_rss":
            await query.message.reply_text("⌨️ Чтобы установить свой RSS URL, отправьте команду: /set_rss `<URL ленты>`")

    elif data.startswith(CB_PREFIX_SET_ITEMS):
        count = int(data.split(CB_PREFIX_SET_ITEMS)[1])
        await set_items_command(update, context, from_callback=True, count_override=count)

    elif data.startswith(CB_PREFIX_SETTINGS_ACTION):
        action_key = data.split(CB_PREFIX_SETTINGS_ACTION)[1]
        if action_key == "clear_filter":
            await clear_filter_command(update, context, query_message=query.message)
        elif action_key == "back_to_main_settings":
            await query.answer()
            await settings_command(Update(update.update_id, message=query.message), context)
            try: await query.message.delete()
            except: pass

    elif data.startswith(CB_PREFIX_SAVE_ARTICLE):
        try:
            page_article_index = int(data.split(CB_PREFIX_SAVE_ARTICLE)[1])
            articles_on_page = context.chat_data.get(CHAT_DATA_ARTICLES_ON_PAGE_CACHE)
            if not articles_on_page or page_article_index not in articles_on_page:
                await query.answer("❗️Новость для сохранения не найдена.", show_alert=True)
                return

            article_to_save = articles_on_page[page_article_index]
            saved_articles = get_saved_articles(context)

            if any(s['link'] == article_to_save['link'] for s in saved_articles):
                await query.answer("ℹ️ Эта статья уже сохранена.", show_alert=True)
                return

            if len(saved_articles) >= MAX_SAVED_ARTICLES:
                await query.answer(f"❗️Достигнут лимит в {MAX_SAVED_ARTICLES} сохраненных статей. Удалите старые, чтобы добавить новые.", show_alert=True)
                return

            rss_url_in_use = get_user_setting(context, USER_DATA_RSS_URL, DEFAULT_RSS_URL)
            source_name = urlparse(rss_url_in_use).netloc
            for key, src_data in PREDEFINED_SOURCES.items():
                if src_data["url"] == rss_url_in_use:
                    source_name = src_data["name"]
                    break

            saved_articles.append({
                "title": article_to_save['title'],
                "link": article_to_save['link'],
                "source_name": source_name,
                "saved_at": datetime.now().isoformat()
            })
            context.user_data[USER_DATA_SAVED_ARTICLES] = saved_articles
            await query.answer("✅ Статья сохранена!", show_alert=True)

        except Exception as e:
            await query.answer("❗️Не удалось сохранить статью.", show_alert=True)

    elif data.startswith(CB_PREFIX_DELETE_SAVED):
        try:
            article_index_to_delete = int(data.split(CB_PREFIX_DELETE_SAVED)[1])
            saved_articles = get_saved_articles(context)
            if 0 <= article_index_to_delete < len(saved_articles):
                deleted_article = saved_articles.pop(article_index_to_delete)
                context.user_data[USER_DATA_SAVED_ARTICLES] = saved_articles
                await query.answer(f"🗑️ Статья «{deleted_article['title'][:30]}...» удалена.", show_alert=True)
                if not saved_articles:
                    await query.edit_message_text("📚 У вас больше нет сохраненных статей.")
                else:
                    message_text_parts = ["📚 *Ваши сохраненные статьи:*\n"]
                    keyboard = []
                    for i, article in enumerate(saved_articles):
                        title = escape_markdown(article['title'], version=2)
                        link = article['link']
                        source_name = escape_markdown(article.get('source_name', 'Неизвестный источник'), version=2)
                        message_text_parts.append(f"{i+1}\\. [{title}]({link}) \\- _{source_name}_")
                        keyboard.append([InlineKeyboardButton(f"🗑️ Удалить #{i+1}", callback_data=f"{CB_PREFIX_DELETE_SAVED}{i}")])

                    final_message_text = "\n".join(message_text_parts)
                    await query.edit_message_text(final_message_text, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=InlineKeyboardMarkup(keyboard), disable_web_page_preview=True)
            else:
                await query.answer("❗️Статья для удаления не найдена.", show_alert=True)
        except Exception as e:
            await query.answer("❗️Не удалось удалить статью.", show_alert=True)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error(f'Исключение при обработке обновления {update}:', exc_info=context.error)

    if isinstance(context.error, Forbidden):
        if "bot was blocked by the user" in str(context.error).lower():
            return
        elif "user is deactivated" in str(context.error).lower():
            return

    if update and hasattr(update, 'effective_message') and hasattr(update.effective_message, 'reply_text'):
        try:
            await update.effective_message.reply_text("😕 Ой, что-то пошло не так... Пожалуйста, попробуйте вашу команду еще раз позже.")
        except Exception as e:
            logger.error(f"Не удалось отправить сообщение об ошибке пользователю: {e}")


def main() -> None:
    if not TELEGRAM_BOT_TOKEN or "ВАШ_TELEGRAM_BOT_TOKEN" in TELEGRAM_BOT_TOKEN:
        logger.critical("Токен Telegram бота не установлен! Пожалуйста, установите переменную TELEGRAM_BOT_TOKEN.")
        return

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    command_handlers = [
        CommandHandler("start", start_command),
        CommandHandler("help", help_command),
        CommandHandler("news", news_command),
        CommandHandler("settings", settings_command),
        CommandHandler("set_items_per_page", set_items_command),
        CommandHandler("set_rss", set_rss_command),
        CommandHandler("sources", sources_command),
        CommandHandler("filter", filter_command),
        CommandHandler("clear_filter", clear_filter_command),
        CommandHandler("clear_history", clear_history_command),
        CommandHandler("tyz", tyz_command),
        CommandHandler("saved", saved_articles_command),
    ]
    application.add_handlers(command_handlers)
    application.add_handler(CallbackQueryHandler(button_callback))
    application.add_error_handler(error_handler)

    logger.info("Бот запускается...")
    application.run_polling()
    logger.info("Бот остановлен.")

if __name__ == '__main__':
    main()
