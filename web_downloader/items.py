# Define here the models for your scraped items
#
# See documentation in:
# https://docs.scrapy.org/en/latest/topics/items.html

import scrapy


class WebDownloaderItem(scrapy.Item):
    file_urls = scrapy.Field()
    files = scrapy.Field()
    source_url = scrapy.Field()
