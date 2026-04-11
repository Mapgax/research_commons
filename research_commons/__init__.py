"""research_commons — shared data + LLM layer for the MSARN/Companies_News/Idee_Scraping ecosystem."""

from research_commons.db_news import init_news_db

__version__ = "0.1.0"

__all__ = ["__version__", "init_news_db"]
