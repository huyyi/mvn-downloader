import scrapy


class MvnSpider(scrapy.Spider):
    name = "mvn"
    allowed_domains = ["repo1.maven.org"]
    start_urls = ["https://repo1.maven.org"]

    def parse(self, response):
        pass
