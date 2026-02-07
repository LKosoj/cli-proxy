from __future__ import annotations

import asyncio
import logging
import os
import urllib.parse
from io import BytesIO
from typing import Any, Dict, List, Optional, Tuple

import httpx
from bs4 import BeautifulSoup
from openai import AsyncOpenAI

from agent.openai_client import create_async_openai_client
from agent.plugins.base import ToolPlugin
from agent.tooling.spec import ToolSpec

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
}

LANG_QUERIES_PROMPTS = {
    "ru": {
        "prompt": (
            "Сформулируй {n} коротких поисковых запроса (ключевые фразы, не длиннее 5-6 слов) на русском языке "
            "по теме: {query}. Не используй номера, не добавляй лишних слов, только сами поисковые фразы."
        ),
        "system": "Ты — эксперт по поисковым системам. Отвечай только списком поисковых фраз на русском языке."
    },
    "en": {
        "prompt": (
            "Generate {n} short search queries (keywords, no more than 5-6 words each) in English "
            "for the topic: {query}. No numbering, just the queries."
        ),
        "system": "You are a search engine expert. Reply with a list of short search queries in English only."
    },
    "zh": {
        "prompt": "请用中文为主题\"{query}\"生成{n}个简短的搜索引擎关键词（每个不超过6个字），不要编号，只列出关键词。",
        "system": "你是一名搜索引擎专家。只用中文列出搜索关键词，每行一个。"
    }
}


class WebResearchTool(ToolPlugin):
    """
    Плагин для поиска релевантных статей по смысловому запросу.
    Использует OpenAI для генерации поисковых запросов и Jina AI для поиска ссылок.
    """

    JINA_SEARCH_URL = "https://s.jina.ai"
    JINA_READER_URL = "https://r.jina.ai"

    def get_source_name(self) -> str:
        return "Web Research"

    def get_spec(self) -> ToolSpec:
        return ToolSpec(
            name="web_research",
            description="Поиск релевантных статей в интернете по смысловому запросу с использованием нескольких стратегий поиска. "
                        "Поддерживает мультиязычный поиск (RU, EN, ZH), скачивание и анализ содержимого найденных страниц.",
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Тема исследования или поисковый запрос"
                    },
                    "max_results_per_lang": {
                        "type": "integer",
                        "description": "Максимум результатов на язык (по умолчанию: 10)",
                        "minimum": 1,
                        "maximum": 20,
                        "default": 10
                    },
                    "analyze_content": {
                        "type": "boolean",
                        "description": "Анализировать скачанный контент и предоставить релевантный ответ (по умолчанию: true)",
                        "default": True
                    }
                },
                "required": ["query"],
            },
            parallelizable=False,
            timeout_ms=180_000,
        )

    def _get_jina_api_key(self) -> Optional[str]:
        """Получает Jina API ключ из окружения или конфига."""
        key = os.getenv("JINA_API_KEY")
        if key:
            return key
        cfg = getattr(self, "config", None)
        if cfg:
            defaults = getattr(cfg, "defaults", None)
            if defaults:
                return getattr(defaults, "jina_api_key", None)
        return None

    def _get_tavily_api_key(self) -> Optional[str]:
        """Получает Tavily API ключ из окружения или конфига."""
        key = os.getenv("TAVILY_API_KEY")
        if key:
            return key
        cfg = getattr(self, "config", None)
        if cfg:
            defaults = getattr(cfg, "defaults", None)
            if defaults:
                return getattr(defaults, "tavily_api_key", None)
        return None

    def _get_zai_api_key(self) -> Optional[str]:
        """Получает Z.AI API ключ из окружения или конфига."""
        key = os.getenv("ZAI_API_KEY")
        if key:
            return key
        cfg = getattr(self, "config", None)
        if cfg:
            defaults = getattr(cfg, "defaults", None)
            if defaults:
                return getattr(defaults, "zai_api_key", None)
        return None

    def _get_proxy_url(self) -> Optional[str]:
        """Получает URL прокси из окружения."""
        return os.getenv("PROXY_URL")

    def _get_openai_client(self) -> AsyncOpenAI:
        """Создает OpenAI клиент из окружения или конфига."""
        cfg = getattr(self, "config", None)
        defaults = getattr(cfg, "defaults", None) if cfg else None

        api_key = os.getenv("OPENAI_API_KEY") or (getattr(defaults, "openai_api_key", None) if defaults else None)
        base_url = os.getenv("OPENAI_BASE_URL") or (getattr(defaults, "openai_base_url", None) if defaults else None)

        return create_async_openai_client(api_key=api_key, base_url=base_url or None)

    def _get_model(self, big: bool = False) -> str:
        """Возвращает модель для использования."""
        cfg = getattr(self, "config", None)
        defaults = getattr(cfg, "defaults", None) if cfg else None

        if big:
            return (
                os.getenv("OPENAI_BIG_MODEL")
                or (getattr(defaults, "openai_big_model", None) if defaults else None)
                or (getattr(defaults, "big_model_to_use", None) if defaults else None)
                or "gpt-4o"
            )
        return os.getenv("OPENAI_MODEL") or (getattr(defaults, "openai_model", None) if defaults else None) or "gpt-4o-mini"

    async def _call_openai_for_queries(self, user_prompt: str, system_prompt: Optional[str] = None) -> List[str]:
        """Генерирует поисковые запросы через OpenAI API."""
        try:
            client = self._get_openai_client()
            model = self._get_model(big=False)

            messages = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": user_prompt})

            resp = await client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=0.7,
                max_tokens=300,
            )

            response_text = (resp.choices[0].message.content or "").strip()
            if response_text:
                queries = [
                    line.strip().lstrip("0123456789.- ").strip()
                    for line in response_text.split("\n")
                    if line.strip()
                ]
                return queries[:5]
            return []
        except Exception as e:
            logger.error(f"Ошибка генерации поисковых запросов: {e}")
            return []

    async def _jina_search(self, query: str, max_results: int = 5) -> List[str]:
        """Выполняет поиск через Jina AI Search API."""
        jina_api_key = self._get_jina_api_key()
        if not jina_api_key:
            logger.error("Jina AI API ключ не настроен")
            return []

        try:
            url = f"{self.JINA_SEARCH_URL}/?q={urllib.parse.quote(query)}"
            headers = {
                "Accept": "application/json",
                "Authorization": f"Bearer {jina_api_key}",
                "X-Respond-With": "no-content"
            }

            async with httpx.AsyncClient() as client:
                response = await client.get(url, headers=headers, timeout=30.0)
                response.raise_for_status()

                data = response.json()

                if data.get("code") != 200:
                    logger.error(f"Jina AI API вернул ошибку: {data.get('status')}")
                    return []

                items = data.get("data", [])[:max_results]
                links = [item.get("url", "") for item in items if item.get("url")]

                logger.info(f"Jina AI поиск '{query}': найдено {len(links)} ссылок")
                return links

        except Exception as e:
            logger.error(f"Ошибка поиска через Jina AI для запроса '{query}': {e}")
            return []

    async def _tavily_search(self, query: str, max_results: int = 5) -> List[str]:
        """Выполняет поиск через Tavily Search API."""
        tavily_key = self._get_tavily_api_key()
        if not tavily_key:
            return []

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    "https://api.tavily.com/search",
                    json={"api_key": tavily_key, "query": query, "max_results": max_results},
                    timeout=30.0,
                )
                response.raise_for_status()
                data = response.json() or {}
                items = data.get("results", [])[:max_results]
                links = [item.get("url") or item.get("link", "") for item in items
                         if item.get("url") or item.get("link")]
                logger.info(f"Tavily поиск '{query}': найдено {len(links)} ссылок")
                return links
        except Exception as e:
            logger.warning(f"Ошибка поиска через Tavily для запроса '{query}': {e}")
            return []

    async def _zai_search(self, query: str, max_results: int = 5) -> List[str]:
        """Выполняет поиск через Z.AI Search API."""
        zai_key = self._get_zai_api_key()
        if not zai_key:
            return []

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    "https://api.z.ai/api/paas/v4/web_search",
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {zai_key}",
                    },
                    json={
                        "search_engine": "search-prime",
                        "search_query": query,
                        "count": max_results,
                    },
                    timeout=30.0,
                )
                response.raise_for_status()
                data = response.json() or {}
                items = data.get("search_result", [])[:max_results]
                links = [item.get("url") or item.get("link", "") for item in items
                         if item.get("url") or item.get("link")]
                logger.info(f"Z.AI поиск '{query}': найдено {len(links)} ссылок")
                return links
        except Exception as e:
            logger.warning(f"Ошибка поиска через Z.AI для запроса '{query}': {e}")
            return []

    async def _proxy_search(self, query: str, max_results: int = 5) -> List[str]:
        """Выполняет поиск через Proxy."""
        proxy_url = self._get_proxy_url()
        if not proxy_url:
            return []

        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{proxy_url}/zai/search",
                    params={"q": query},
                    timeout=30.0,
                )
                response.raise_for_status()
                data = response.json() or {}
                items = data.get("search_result", [])[:max_results]
                links = [item.get("url") or item.get("link", "") for item in items
                         if item.get("url") or item.get("link")]
                logger.info(f"Proxy поиск '{query}': найдено {len(links)} ссылок")
                return links
        except Exception as e:
            logger.warning(f"Ошибка поиска через Proxy для запроса '{query}': {e}")
            return []

    async def _search(self, query: str, max_results: int = 5) -> List[str]:
        """Выполняет поиск с fallback между провайдерами."""
        search_providers: List[tuple] = []
        if self._get_proxy_url():
            search_providers.append(("Proxy", self._proxy_search))
        if self._get_tavily_api_key():
            search_providers.append(("Tavily", self._tavily_search))
        if self._get_jina_api_key():
            search_providers.append(("Jina", self._jina_search))
        if self._get_zai_api_key():
            search_providers.append(("Z.AI", self._zai_search))

        for name, func in search_providers:
            try:
                results = await func(query, max_results)
                if results:
                    return results
                logger.warning(f"Поиск через {name} вернул пустые результаты для '{query}'")
            except Exception as e:
                logger.warning(f"Поиск через {name} завершился ошибкой для '{query}': {e}")

        logger.error(f"Все поисковые провайдеры не дали результатов для '{query}'")
        return []

    async def _download_content(self, url: str) -> Dict[str, str]:
        """Асинхронно скачивает и очищает содержимое страницы."""
        # Очистка URL от лишних символов
        if "(" in url:
            url = url.split("(")[1]
        url = url.strip(")").strip("(").strip('"').strip("'").strip()

        if not url.startswith("http"):
            logger.warning(f"Некорректный URL: {url}")
            return {"url": url, "title": "", "content": ""}

        title = ""

        # ── Stage 1: Прямой запрос через httpx (без API-ключей) ──
        try:
            enhanced_headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) "
                              "Chrome/91.0.4472.124 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                          "image/webp,*/*;q=0.8",
                "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
                "Accept-Encoding": "gzip, deflate, br",
                "DNT": "1",
                "Connection": "keep-alive",
                "Upgrade-Insecure-Requests": "1",
            }

            async with httpx.AsyncClient(follow_redirects=True) as client:
                response = await client.get(url, headers=enhanced_headers, timeout=20.0)
                response.raise_for_status()

                # Проверка на PDF
                content_type = response.headers.get("Content-Type", "").lower()
                if url.lower().endswith(".pdf") or "application/pdf" in content_type:
                    logger.info(f"Обнаружен PDF: {url}")
                    try:
                        pdf_content, pdf_title = await self._extract_pdf_content(response.content, url)
                        return {"url": url, "title": pdf_title, "content": pdf_content}
                    except Exception as e:
                        logger.warning(f"Ошибка извлечения PDF {url}: {e}")
                        return {
                            "url": url,
                            "title": url.split("/")[-1] or "PDF-документ",
                            "content": "PDF-документ (ошибка извлечения содержимого)",
                        }

                html_content = response.text
                title = self._extract_title(html_content)

                # trafilatura (основной метод)
                try:
                    import trafilatura
                    clean_content = trafilatura.extract(
                        html_content,
                        include_formatting=True,
                        include_links=True,
                        include_tables=True,
                        include_images=True,
                        include_comments=False,
                        output_format="markdown",
                    )
                    if clean_content and clean_content.strip():
                        clean_content = self._clean_extra_spaces(clean_content)
                        logger.info(f"Успешно загружен через trafilatura: {url}")
                        return {"url": url, "title": title, "content": clean_content}
                except ImportError:
                    logger.debug("trafilatura не установлен, используем BeautifulSoup")
                except Exception as e:
                    logger.warning(f"Ошибка trafilatura для {url}: {e}")

                # BeautifulSoup fallback
                clean_content = self._clean_html_content(html_content)
                if clean_content and clean_content.strip():
                    logger.info(f"Успешно загружен через BeautifulSoup: {url}")
                    return {"url": url, "title": title, "content": clean_content}

                logger.warning(f"Пустой контент после extraction для {url}, пробуем API readers")

        except httpx.TimeoutException:
            logger.warning(f"Тайм-аут при загрузке URL: {url}")
        except httpx.HTTPError as e:
            logger.warning(f"HTTP ошибка при загрузке URL: {url}, ошибка: {e}")
        except Exception as e:
            logger.warning(f"Непредвиденная ошибка при загрузке URL: {url}, ошибка: {e}")

        # ── Stage 2: API readers (fallback) ──

        # Proxy Reader
        proxy_url = self._get_proxy_url()
        if proxy_url:
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.get(
                        f"{proxy_url}/zai/read",
                        params={"url": url},
                        timeout=30.0,
                    )
                    resp.raise_for_status()
                    data = (resp.json() or {}).get("reader_result") or {}
                    api_content = data.get("content")
                    if api_content and api_content.strip():
                        api_title = data.get("title") or title
                        logger.info(f"Успешно загружен через Proxy Reader (fallback): {url}")
                        return {"url": url, "title": api_title, "content": api_content}
            except Exception as e:
                logger.warning(f"Ошибка Proxy Reader fallback для {url}: {e}")

        # Tavily Extract
        tavily_key = self._get_tavily_api_key()
        if tavily_key:
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.post(
                        "https://api.tavily.com/extract",
                        headers={
                            "Content-Type": "application/json",
                            "Authorization": f"Bearer {tavily_key}",
                        },
                        json={"urls": [url]},
                        timeout=30.0,
                    )
                    resp.raise_for_status()
                    data = resp.json() or {}
                    results = data.get("results") or data.get("data") or []
                    if isinstance(results, dict):
                        results = [results]
                    item = results[0] if results else {}
                    api_content = item.get("content") or item.get("raw_content") or ""
                    if api_content and api_content.strip():
                        api_title = item.get("title") or title
                        logger.info(f"Успешно загружен через Tavily Extract (fallback): {url}")
                        return {"url": url, "title": api_title, "content": api_content}
            except Exception as e:
                logger.warning(f"Ошибка Tavily Extract fallback для {url}: {e}")

        # Jina Reader
        jina_key = self._get_jina_api_key()
        if jina_key:
            try:
                content, jina_title = await self._get_clean_text_jina(url)
                if content and content.strip():
                    logger.info(f"Успешно загружен через Jina Reader (fallback): {url}")
                    return {"url": url, "title": jina_title or title, "content": content}
            except Exception as e:
                logger.warning(f"Ошибка Jina Reader fallback для {url}: {e}")

        # Z.AI Reader
        zai_key = self._get_zai_api_key()
        if zai_key:
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.post(
                        "https://api.z.ai/api/paas/v4/reader",
                        headers={
                            "Content-Type": "application/json",
                            "Authorization": f"Bearer {zai_key}",
                        },
                        json={
                            "url": url,
                            "return_format": "markdown",
                            "retain_images": False,
                            "timeout": 20,
                        },
                        timeout=30.0,
                    )
                    resp.raise_for_status()
                    data = (resp.json() or {}).get("reader_result") or {}
                    api_content = data.get("content")
                    if api_content and api_content.strip():
                        api_title = data.get("title") or title
                        logger.info(f"Успешно загружен через Z.AI Reader (fallback): {url}")
                        return {"url": url, "title": api_title, "content": api_content}
            except Exception as e:
                logger.warning(f"Ошибка Z.AI Reader fallback для {url}: {e}")

        return {"url": url, "title": title, "content": ""}

    async def _extract_pdf_content(self, pdf_bytes: bytes, url: str) -> Tuple[str, str]:
        """Извлекает содержимое PDF."""
        try:
            from pdfminer.high_level import extract_text

            pdf_stream = BytesIO(pdf_bytes)
            text = extract_text(pdf_stream)

            if text and text.strip():
                clean_text = self._clean_extra_spaces(text)
                title = url.split("/")[-1] or "PDF-документ"
                logger.info(f"Успешно извлечен текст из PDF: {url} ({len(clean_text)} символов)")
                return clean_text, title
            else:
                logger.warning(f"PDF пустой или не содержит текста: {url}")
                return "PDF-документ не содержит извлекаемого текста", url.split("/")[-1] or "PDF-документ"

        except ImportError:
            logger.error("pdfminer не установлен. Установите: pip install pdfminer.six")
            return "PDF-документ (pdfminer не установлен)", url.split("/")[-1] or "PDF-документ"
        except Exception as e:
            logger.error(f"Ошибка извлечения текста из PDF {url}: {e}")
            raise

    async def _get_clean_text_jina(self, url: str) -> Tuple[str, str]:
        """Получает очищенный текст через Jina Reader API."""
        jina_api_key = self._get_jina_api_key()
        if not jina_api_key:
            raise Exception("Jina API ключ не настроен")

        headers = {
            "Authorization": f"Bearer {jina_api_key}",
            "Content-Type": "application/json",
            "X-Base": "final",
            "X-Engine": "browser",
            "X-Timeout": "20000",
            "X-No-Gfm": "true"
        }
        data = {"url": url}

        async with httpx.AsyncClient() as client:
            response = await client.post(self.JINA_READER_URL, headers=headers, json=data, timeout=30.0)
            response.raise_for_status()

            text_response = response.text
            lines = text_response.splitlines()

            extracted_title = ""
            markdown_content_lines = []
            markdown_section_started = False

            for line in lines:
                if line.startswith("Title:"):
                    if not markdown_section_started:
                        extracted_title = line.replace("Title:", "").strip()
                elif line.startswith("URL Source:"):
                    pass
                elif line.startswith("Markdown Content:"):
                    markdown_section_started = True
                    content_on_label_line = line.replace("Markdown Content:", "").strip()
                    if content_on_label_line:
                        markdown_content_lines.append(content_on_label_line)
                elif markdown_section_started:
                    markdown_content_lines.append(line)

            markdown_content = "\n".join(markdown_content_lines).strip() if markdown_content_lines else ""

            if extracted_title and markdown_content:
                return markdown_content, extracted_title
            else:
                raise Exception("Не удалось извлечь контент через Jina API")

    def _extract_title(self, html_content: str) -> str:
        """Извлекает заголовок из HTML."""
        try:
            soup = BeautifulSoup(html_content, "html.parser")

            title_tag = soup.find("title")
            if title_tag and title_tag.string:
                return title_tag.string.strip()

            og_title = soup.find("meta", property="og:title")
            if og_title and og_title.get("content"):
                return og_title.get("content").strip()

            h1_tag = soup.find("h1")
            if h1_tag:
                return h1_tag.get_text().strip()

        except Exception as e:
            logger.warning(f"Ошибка извлечения заголовка: {e}")

        return "Без заголовка"

    def _clean_extra_spaces(self, text: str) -> str:
        """Удаляет лишние пробелы и переносы строк."""
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        return "\n".join(lines)

    def _clean_html_content(self, html_content: str) -> str:
        """Fallback очистка HTML контента через BeautifulSoup."""
        try:
            soup = BeautifulSoup(html_content, "html.parser")

            for element in soup(["script", "style", "iframe", "noscript", "nav",
                                 "footer", "header", "aside", "form", "button"]):
                element.decompose()

            for element in soup(["br", "p", "h1", "h2", "h3", "h4", "h5", "h6",
                                 "ul", "ol", "li", "div", "table", "tr", "td", "th"]):
                element.append("\n")

            text = soup.get_text(separator="\n", strip=True)
            return self._clean_extra_spaces(text)

        except Exception as e:
            logger.error(f"Ошибка при очистке HTML: {e}")
            return html_content

    async def _generate_search_queries_lang(self, user_query: str, lang: str, n: int) -> List[str]:
        """Генерирует поисковые запросы для конкретного языка."""
        if lang not in LANG_QUERIES_PROMPTS:
            lang = "en"

        prompt_data = LANG_QUERIES_PROMPTS[lang]
        prompt = prompt_data["prompt"].format(query=user_query, n=n)
        system_prompt = prompt_data["system"]

        queries = await self._call_openai_for_queries(prompt, system_prompt)
        return queries[:n]

    async def _find_articles_for_language(self, user_query: str, lang: str,
                                          num_queries: int, max_results: int) -> List[str]:
        """Находит статьи для конкретного языка."""
        queries = await self._generate_search_queries_lang(user_query, lang, num_queries)
        logger.info(f"Поисковые запросы {lang.upper()}: {queries}")

        search_tasks = []
        for query in queries:
            task = self._search(query, max_results=max_results // num_queries + 1)
            search_tasks.append(task)

        search_results = await asyncio.gather(*search_tasks, return_exceptions=True)

        all_results = []
        for result in search_results:
            if isinstance(result, list):
                all_results.append(result)
            else:
                logger.warning(f"Ошибка в поиске: {result}")
                all_results.append([])

        return self._round_robin_merge(all_results)

    def _round_robin_merge(self, lists: List[List[str]]) -> List[str]:
        """Объединяет списки методом round-robin без дубликатов."""
        merged = []
        seen = set()
        maxlen = max(len(lst) for lst in lists) if lists else 0

        for i in range(maxlen):
            for lst in lists:
                if i < len(lst):
                    link = lst[i]
                    if link and link not in seen:
                        merged.append(link)
                        seen.add(link)

        return merged

    async def _analyze_content_with_llm(self, user_query: str, articles: List[Dict[str, str]]) -> str:
        """Анализирует содержимое статей с помощью большой модели."""
        try:
            valid_articles = [article for article in articles if article.get("content", "").strip()]

            if not valid_articles:
                return "Не удалось скачать содержимое статей для анализа."

            content_parts = []
            for i, article in enumerate(valid_articles, 1):
                content_parts.append(f"=== СТАТЬЯ {i} ===")
                content_parts.append(f"URL: {article['url']}")
                content_parts.append(f"Заголовок: {article['title']}")
                # Ограничиваем размер контента каждой статьи
                article_content = article["content"][:8000]
                content_parts.append(f"Содержимое: {article_content}")
                content_parts.append("")

            combined_content = "\n".join(content_parts)

            system_message = (
                "Ты - самый лучший эксперт-аналитик, который анализирует веб-контент и предоставляет "
                "точные, структурированные ответы на основе найденной информации."
            )

            analysis_prompt = f"""На основе предоставленных статей дай подробный и релевантный ответ на запрос пользователя.

Запрос пользователя: {user_query}

Найденные статьи:
--------------------------------
{combined_content}
--------------------------------
Инструкции:
1. Проанализируй содержимое всех статей
2. Выдели наиболее релевантную информацию для ответа на запрос
3. Структурируй ответ логично и понятно
4. Укажи источники информации (URL) в конце ответа
5. Если информации недостаточно, честно об этом скажи

Ответ:"""

            client = self._get_openai_client()
            model = self._get_model(big=True)

            resp = await client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_message},
                    {"role": "user", "content": analysis_prompt},
                ],
                temperature=0.3,
                max_tokens=2000,
            )

            response = (resp.choices[0].message.content or "").strip()

            if response:
                logger.info(f"Анализ завершен, длина ответа: {len(response)} символов")
                return response
            else:
                logger.error("Пустой ответ от большой модели")
                return "Ошибка при анализе содержимого статей."

        except Exception as e:
            logger.error(f"Ошибка анализа содержимого: {e}")
            return f"Ошибка при анализе содержимого: {str(e)}"

    async def execute(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        try:
            has_any_search = any([
                self._get_jina_api_key(),
                self._get_tavily_api_key(),
                self._get_zai_api_key(),
                self._get_proxy_url(),
            ])
            if not has_any_search:
                return {
                    "success": False,
                    "error": "Не настроен ни один поисковый API. Проверьте переменные окружения: "
                             "JINA_API_KEY, TAVILY_API_KEY, ZAI_API_KEY или PROXY_URL"
                }

            query = (args.get("query") or "").strip()
            if not query:
                return {"success": False, "error": "Запрос не может быть пустым"}

            max_results_per_lang = int(args.get("max_results_per_lang") or 10)
            max_results_per_lang = max(1, min(max_results_per_lang, 20))
            analyze_content = args.get("analyze_content", True)
            if analyze_content is None:
                analyze_content = True

            logger.info(f"Начинаем веб-исследование для запроса: {query}")

            # Асинхронно ищем статьи для всех языков
            tasks = [
                self._find_articles_for_language(query, "ru", 2, max_results_per_lang),
                self._find_articles_for_language(query, "en", 3, max_results_per_lang),
                self._find_articles_for_language(query, "zh", 2, max_results_per_lang)
            ]

            results = await asyncio.gather(*tasks, return_exceptions=True)

            links_ru = results[0] if isinstance(results[0], list) else []
            links_en = results[1] if isinstance(results[1], list) else []
            links_zh = results[2] if isinstance(results[2], list) else []

            logger.info(f"Найдено ссылок - RU: {len(links_ru)}, EN: {len(links_en)}, ZH: {len(links_zh)}")

            # Объединяем все найденные ссылки для скачивания содержимого
            all_links = links_ru[:8] + links_en[:8] + links_zh[:4]

            if not all_links:
                return {"success": True, "output": "Не найдено релевантных статей по запросу."}

            logger.info(f"Скачиваем содержимое {len(all_links)} статей...")

            # Асинхронно скачиваем содержимое всех найденных статей
            content_tasks = [self._download_content(link) for link in all_links]
            articles_data = await asyncio.gather(*content_tasks, return_exceptions=True)

            # Фильтруем успешно скачанные статьи
            valid_articles = []
            for article in articles_data:
                if isinstance(article, dict) and article.get("content", "").strip():
                    valid_articles.append(article)

            logger.info(f"Успешно скачано содержимое {len(valid_articles)} статей")

            # Формируем результат
            output_parts = []
            output_parts.append(f"Найдено ссылок: RU={len(links_ru)}, EN={len(links_en)}, ZH={len(links_zh)}")
            output_parts.append(f"Скачано статей: {len(valid_articles)}")
            output_parts.append("")

            # Ссылки по языкам
            if links_ru:
                output_parts.append("=== Русские источники ===")
                for link in links_ru[:max_results_per_lang]:
                    output_parts.append(f"• {link}")
                output_parts.append("")

            if links_en:
                output_parts.append("=== English sources ===")
                for link in links_en[:max_results_per_lang]:
                    output_parts.append(f"• {link}")
                output_parts.append("")

            if links_zh:
                output_parts.append("=== 中文来源 ===")
                for link in links_zh[:max_results_per_lang]:
                    output_parts.append(f"• {link}")
                output_parts.append("")

            # Анализируем содержимое с помощью большой модели
            if analyze_content and valid_articles:
                logger.info("Анализируем содержимое статей с помощью большой модели...")
                analysis = await self._analyze_content_with_llm(query, valid_articles)
                output_parts.append("=== АНАЛИЗ ===")
                output_parts.append(analysis)
            elif not valid_articles:
                output_parts.append("Не удалось скачать содержимое статей для анализа.")

            return {"success": True, "output": "\n".join(output_parts)}

        except Exception as e:
            error_msg = f"Ошибка выполнения веб-исследования: {str(e)}"
            logger.error(error_msg)
            return {"success": False, "error": error_msg}
